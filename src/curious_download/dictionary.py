"""Data dictionaries: one CSV per activity describing its items across every saved applet version.

Built from `GET /applets/{id}/versions` and `/versions/{version}`, so no applet password and no
data access are needed; any role on the applet can read them. Item IDs survive edits, so each item
is followed from version to version and gets a new row only when its definition changes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import CuriousClient
from .csvout import file_slug, write_csv
from .jscompat import coalesce, is_number, number_to_string, to_string
from .report import parse_options
from .subscales import FINAL_SCORE_COLUMN, FINAL_TEXT_COLUMN, TOTAL_SCORE_TYPES, clean_name, is_system_item

DICTIONARY_COLUMNS = [
    "applet_id",
    "applet_name",
    "activity_id",
    "activity_name",
    "activity_flows",
    "item_order",
    "item_id",
    "item_name",
    "item_type",
    "item_prompt",
    "item_response_options",
    "value_labels",
    "scores",
    "min_value",
    "max_value",
    "min_label",
    "max_label",
    "rows",
    "response_rules",
    "other_text_option",
    "skippable",
    "hidden",
    "shown_if",
    "first_version",
    "last_version",
    "applet_versions",
    "status",
    "change",
]

INDEX_COLUMNS = [
    "applet_id",
    "applet_name",
    "activity_id",
    "activity_name",
    "file",
    "items",
    "items_changed",
    "first_version",
    "last_version",
    "status",
    "note",
]

# Fields of an item that hold stored JSON; the version endpoint returns them as saved (usually snake_case).
_STORED_JSON = ("responseValues", "config", "conditionalLogic")
_SELECT_TYPES = ("singleSelect", "multiSelect")
_SELECT_ROW_TYPES = ("singleSelectRows", "multiSelectRows")
_NO_RESPONSE_TYPES = ("message", "phrasalTemplate")
_APPEARANCE_KEYS = {
    "color",
    "image",
    "tooltip",
    "paletteName",
    "minImage",
    "maxImage",
    "rowImage",
    "drawingExample",
    "drawingBackground",
}
_SCORING = {"sum": "Sum of {}", "average": "Average of {}", "percentage": "Sum of {} as a percentage of its maximum"}
_MISSING = object()


# -- fetching -------------------------------------------------------------------


def _camel_key(key: str) -> str:
    """The backend's to_camelcase."""
    first, *rest = key.split("_")
    return first + "".join(word.capitalize() for word in rest)


def camelize(value: Any) -> Any:
    """Convert dict keys to camelCase recursively, so stored and re-serialized JSON look the same."""
    if isinstance(value, dict):
        return {(_camel_key(k) if isinstance(k, str) and "_" in k else k): camelize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [camelize(v) for v in value]
    return value


def normalize_version(applet: dict) -> dict:
    """camelCase the stored JSON of every activity and item in one version's details."""
    activities = []
    for activity in applet.get("activities") or []:
        items = [
            {**item, **{key: camelize(item.get(key)) for key in _STORED_JSON}} for item in activity.get("items") or []
        ]
        activities.append({**activity, "subscaleSetting": camelize(activity.get("subscaleSetting")), "items": items})
    # Flow items embed a full copy of their activity; only its ID is needed.
    flows = [
        {
            **flow,
            "items": [
                {**flow_item, "activity": {"id": (flow_item.get("activity") or {}).get("id")}}
                for flow_item in flow.get("items") or []
            ],
        }
        for flow in applet.get("activityFlows") or []
    ]
    return {**applet, "activities": activities, "activityFlows": flows}


@dataclass
class AppletHistory:
    applet_id: str
    applet_name: str
    versions: list[str]  # oldest first
    snapshots: list[dict]  # version details, same order


def fetch_history(
    client: CuriousClient,
    applet_id: str,
    applet_name: str,
    on_progress: Callable[[int, int], None] | None = None,
    *,
    current_only: bool = False,
) -> AppletHistory:
    """Download every saved version of an applet (one request per version), or only the current one."""
    listed = client.applet_versions(applet_id)[::-1]  # the API lists newest first
    listed.sort(key=lambda row: row.get("createdAt") or "")
    names = [row["version"] for row in listed]
    if current_only:
        names = names[-1:]
    snapshots = []
    for done, name in enumerate(names, start=1):
        snapshots.append(normalize_version(client.applet_version(applet_id, name)))
        if on_progress:
            on_progress(done, len(names))
    return AppletHistory(applet_id, applet_name, names, snapshots)


# -- following items across versions ---------------------------------------------


@dataclass
class _Entry:
    """One item (or score column) as defined in one version."""

    key: str  # stays the same across versions
    signature: str  # equal signatures mean an unchanged definition
    data: dict
    position: int = 0


@dataclass
class _Row:
    """One definition of an item and the versions it was in effect."""

    entry: _Entry  # as in the last of `versions`
    activity: dict  # the activity in the last of `versions`
    versions: list[int]
    change: str
    status: str = "current"


def _signature(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _track(
    per_version: list[tuple[dict, list[_Entry]] | None],
    names: list[str],
    describe: Callable[[dict, dict], str],
) -> list[_Row]:
    """Rows for every distinct definition; `per_version` is None where the activity doesn't exist."""
    rows: list[_Row] = []
    open_rows: dict[str, _Row] = {}
    seen: set[str] = set()
    first = next((index for index, version in enumerate(per_version) if version is not None), None)
    for index, version in enumerate(per_version):
        activity, entries = version if version is not None else ({}, [])
        present = {entry.key for entry in entries}
        for key in [key for key in open_rows if key not in present]:
            open_rows.pop(key).status = f"removed in {names[index]}"
        for entry in entries:
            row = open_rows.get(entry.key)
            if row is not None and row.entry.signature == entry.signature:
                row.versions.append(index)
                row.entry, row.activity = entry, activity
                continue
            if row is not None:
                row.status = f"changed in {names[index]}"
                change = describe(row.entry.data, entry.data)
            elif entry.key in seen:
                change = "added again"
            else:
                change = "" if index == first else "added"
            seen.add(entry.key)
            open_rows[entry.key] = _Row(entry, activity, [index], change)
            rows.append(open_rows[entry.key])
    return rows


def _merged_order(key_lists: Iterable[list[str]]) -> dict[str, int]:
    """Position of every key: the newest version's order, with keys that only older versions
    have placed after the key they followed there."""
    sequence: list[str] = []
    placed: set[str] = set()
    for keys in reversed(list(key_lists)):
        previous = None
        for key in keys:
            if key not in placed:
                sequence.insert(0 if previous is None else sequence.index(previous) + 1, key)
                placed.add(key)
            previous = key
    return {key: position for position, key in enumerate(sequence)}


def _sorted_rows(rows: list[_Row], per_version: list[tuple[dict, list[_Entry]] | None]) -> list[_Row]:
    order = _merged_order([entry.key for entry in version[1]] for version in per_version if version is not None)
    return sorted(rows, key=lambda row: (order[row.entry.key], row.versions[0]))


def _item_keys(activities: list[dict | None]) -> list[list[str]]:
    """Per version, a stable key for each item: its ID, or the key of the item it replaced when an
    item with the same name came back under a new ID (seen in re-imported applets)."""
    alias: dict[str, str] = {}
    seen_ids: set[str] = set()
    previous: dict[str, str] = {}  # name -> key, in the previous version
    result = []
    for activity in activities:
        items = activity["items"] if activity else []
        keys = [alias.get(item["id"], item["id"]) for item in items]
        present = set(keys)
        for position, item in enumerate(items):
            replaced = previous.get(item.get("name"))
            if item["id"] not in seen_ids and item["id"] not in alias and replaced and replaced not in present:
                alias[item["id"]] = keys[position] = replaced
                present.add(replaced)
        seen_ids.update(item["id"] for item in items)
        previous = {item.get("name"): key for item, key in zip(items, keys)}
        result.append(keys)
    return result


# -- describing changes ----------------------------------------------------------


def _flatten(value: Any, path: tuple = ()) -> Iterable[tuple[tuple, Any]]:
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _flatten(child, (*path, key))
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from _flatten(child, (*path, index))
    else:
        yield path, value


def _changed_paths(old: Any, new: Any) -> list[tuple]:
    old_flat, new_flat = dict(_flatten(old)), dict(_flatten(new))
    paths = list(dict.fromkeys([*new_flat, *old_flat]))
    return [path for path in paths if old_flat.get(path, _MISSING) != new_flat.get(path, _MISSING)]


def _response_value_category(path: tuple) -> str:
    keys = [part for part in path if isinstance(part, str)]
    if {"alert", "alerts"} & set(keys):
        return "alerts"
    if {"score", "scores"} & set(keys):
        return "scores"
    if keys and keys[-1] in _APPEARANCE_KEYS:
        return "appearance"
    return "response options"


def _words(key: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", key).lower()


def describe_item_change(old: dict, new: dict) -> str:
    labels = []
    if old.get("id") != new.get("id"):
        labels.append(f"item ID changed (was {old.get('id')})")
    if old.get("name") != new.get("name"):
        labels.append(f"renamed (was {old.get('name')})")
    if old.get("responseType") != new.get("responseType"):
        labels.append(f"item type (was {old.get('responseType')})")
    if old.get("question") != new.get("question"):
        labels.append("prompt")
    categories = {
        _response_value_category(p) for p in _changed_paths(old.get("responseValues"), new.get("responseValues"))
    }
    labels += [c for c in ("response options", "scores", "alerts", "appearance") if c in categories]
    if old.get("conditionalLogic") != new.get("conditionalLogic"):
        labels.append("skip logic")
    if bool(old.get("isHidden")) != bool(new.get("isHidden")):
        labels.append("now hidden" if new.get("isHidden") else "no longer hidden")
    settings = list(dict.fromkeys(str(p[0]) for p in _changed_paths(old.get("config"), new.get("config")) if p))
    if bool(old.get("allowEdit")) != bool(new.get("allowEdit")):
        settings.append("allowEdit")
    if settings:
        labels.append("settings: " + ", ".join(_words(key) for key in settings))
    return "; ".join(labels) or "changed"


def describe_score_change(old: dict, new: dict) -> str:
    labels = []
    if old.get("scoring") != new.get("scoring"):
        labels.append(f"scoring (was {old.get('scoring')})")
    if old.get("items") != new.get("items"):
        labels.append("items")
    if old.get("table") != new.get("table"):
        labels.append("lookup table")
    return "; ".join(labels) or "changed"


# -- formatting cells ------------------------------------------------------------


def display_text(value: Any, language: str = "en") -> str:
    """A translatable field: the language asked for, else the first one available."""
    if isinstance(value, dict):
        if value.get(language):
            return to_string(value[language])
        return next((to_string(text) for text in value.values() if text), "")
    return "" if value is None else to_string(value)


def _num(value: Any) -> str:
    if is_number(value):
        return number_to_string(value)
    return "" if value is None else to_string(value)


def _range_scores(values: dict, separator: str = " | ") -> str:
    scores, low = values.get("scores") or [], values.get("minValue")
    if not is_number(low):
        return ""
    return separator.join(f"{_num(low + offset)}={_num(score)}" for offset, score in enumerate(scores))


def _value_labels(response_type: str, values: dict) -> str:
    if response_type in _SELECT_TYPES:
        parts = []
        for option in values.get("options") or []:
            text = f"{_num(option.get('value'))}={_num(option.get('text'))}"
            flags = [
                flag
                for flag, on in (("none of the above", option.get("isNoneAbove")), ("hidden", option.get("isHidden")))
                if on
            ]
            parts.append(f"{text} ({', '.join(flags)})" if flags else text)
        return " | ".join(parts)
    if response_type == "slider":
        ends = (("minValue", "minLabel"), ("maxValue", "maxLabel"))
        return " | ".join(f"{_num(values.get(v))}={values[label]}" for v, label in ends if values.get(label))
    if response_type in _SELECT_ROW_TYPES:
        return " | ".join(_num(option.get("text")) for option in values.get("options") or [])
    if response_type == "requestHealthRecordData":
        return " | ".join(f"{o.get('id')}={_num(o.get('label'))}" for o in values.get("optInOutOptions") or [])
    return ""


def _scores(response_type: str, values: dict) -> str:
    if response_type in _SELECT_TYPES:
        options = values.get("options") or []
        return " | ".join(
            f"{_num(o.get('value'))}={_num(o.get('score'))}" for o in options if is_number(o.get("score"))
        )
    if response_type == "slider":
        return _range_scores(values)
    if response_type == "sliderRows":
        rows = values.get("rows") or []
        return " | ".join(f"{_num(r.get('label'))}: {_range_scores(r, ', ')}" for r in rows if r.get("scores"))
    if response_type in _SELECT_ROW_TYPES:
        option_texts = {o.get("id"): o.get("text") for o in values.get("options") or []}
        row_names = {r.get("id"): r.get("rowName") for r in values.get("rows") or []}
        parts = []
        for data_row in values.get("dataMatrix") or []:
            scored = [
                f"{_num(option_texts.get(o.get('optionId'), o.get('optionId')))}={_num(o.get('score'))}"
                for o in data_row.get("options") or []
                if is_number(o.get("score"))
            ]
            if scored:
                parts.append(
                    f"{_num(row_names.get(data_row.get('rowId'), data_row.get('rowId')))}: {', '.join(scored)}"
                )
        return " | ".join(parts)
    return ""


def _rows(response_type: str, values: dict) -> str:
    if response_type in _SELECT_ROW_TYPES:
        return " | ".join(_num(row.get("rowName")) for row in values.get("rows") or [])
    if response_type == "sliderRows":

        def end(row: dict, value: str, label: str) -> str:
            return f"{_num(row.get(value))}={row[label]}" if row.get(label) else _num(row.get(value))

        return " | ".join(
            f"{_num(row.get('label'))} ({end(row, 'minValue', 'minLabel')} to {end(row, 'maxValue', 'maxLabel')})"
            for row in values.get("rows") or []
        )
    return ""


def _response_rules(response_type: str, values: dict, config: dict) -> str:
    rules = []
    if response_type in ("text", "paragraphText"):
        if config.get("maxResponseLength"):
            rules.append(f"max {_num(config['maxResponseLength'])} characters")
        if config.get("numericalResponseRequired"):
            rules.append("numbers only")
        if config.get("correctAnswerRequired"):
            rules.append(f"correct answer: {_num(config.get('correctAnswer'))}")
        if config.get("responseRequired"):
            rules.append("response required")
        if config.get("responseDataIdentifier") or config.get("isIdentifier"):
            rules.append("used as response identifier")
    if response_type == "audio" and values.get("maxDuration"):
        rules.append(f"max {_num(values['maxDuration'])} seconds")
    if config.get("randomizeOptions"):
        rules.append("options in random order")
    if config.get("continuousSlider"):
        rules.append("continuous slider")
    if config.get("playOnce"):
        rules.append("plays once")
    if config.get("timer"):
        rules.append(f"time limit {_num(config['timer'])} seconds")
    return "; ".join(rules)


def _other_text_option(config: dict) -> str:
    extra = config.get("additionalResponseOption") or {}
    if not extra.get("textInputOption"):
        return ""
    return "required" if extra.get("textInputRequired") else "optional"


def _skippable(response_type: str, config: dict, activity: dict) -> str:
    if response_type in _NO_RESPONSE_TYPES:
        return ""
    if activity.get("isSkippable") or config.get("skippableItem"):
        return "yes"
    return "no" if "skippableItem" in config else ""


# -- skip logic -------------------------------------------------------------------

_CONDITION_SUFFIX = re.compile(r"_(ROW_OPTION|OPTION|SLIDER_ROWS|TIMES?_RANGE|TIMES?|DATES?)$")
_OPERATORS = {
    "EQUAL": "=",
    "NOT_EQUAL": "!=",
    "INCLUDES": "includes",
    "NOT_INCLUDES": "does not include",
    "GREATER_THAN": ">",
    "LESS_THAN": "<",
    "BETWEEN": "between",
    "OUTSIDE_OF": "not between",
}


def _operand(value: Any) -> str:
    if isinstance(value, dict) and "hours" in value:
        return f"{int(value.get('hours') or 0):02d}:{int(value.get('minutes') or 0):02d}"
    return "?" if value is None else _num(value)


def _first(payload: dict, *keys: str) -> Any:
    return next((payload[key] for key in keys if payload.get(key) is not None), None)


def _option_label(item: dict, option_value: Any) -> str:
    options = (item.get("responseValues") or {}).get("options") or []
    by_value = [
        o for o in options if o.get("value") is not None and to_string(o.get("value")) == to_string(option_value)
    ]
    match = (by_value or [o for o in options if o.get("id") == option_value] or [None])[0]
    if match is None:
        return f'"{_num(option_value)}"'
    if item.get("responseType") in _SELECT_TYPES:
        return f'"{_num(match.get("text"))}" ({_num(match.get("value"))})'
    return f'"{_num(match.get("text"))}"'


def _row_label(item: dict, row_index: Any) -> str:
    rows = (item.get("responseValues") or {}).get("rows") or []
    index = int(row_index) if str(row_index).isdigit() else -1
    if 0 <= index < len(rows):
        return _num(rows[index].get("rowName") or rows[index].get("label"))
    return f"row {_num(row_index)}"


def describe_condition(condition: dict, items_by_name: dict[str, dict]) -> str:
    name = condition.get("itemName") or ""
    kind = condition.get("type") or ""
    payload = condition.get("payload") or {}
    item = items_by_name.get(name) or {}
    subject = name
    if payload.get("rowIndex") is not None:
        subject += f" [{_row_label(item, payload['rowIndex'])}]"
    side = payload.get("fieldName") or {"startTime": "from", "endTime": "to"}.get(payload.get("type"))
    if side:
        subject += " (start)" if side == "from" else " (end)"

    operator = _OPERATORS.get(_CONDITION_SUFFIX.sub("", kind).removesuffix("_TO"))
    if operator is None:
        return f"{subject} {kind} {_signature(payload)}"
    if "optionValue" in payload:
        return f"{subject} {operator} {_option_label(item, payload['optionValue'])}"
    low, high = _first(payload, "minValue", "minDate", "minTime"), _first(payload, "maxValue", "maxDate", "maxTime")
    value = _first(payload, "value", "date", "time")
    if operator in ("between", "not between") or (value is None and low is not None):
        return f"{subject} {operator} {_operand(low)} and {_operand(high)}"
    return f"{subject} {operator} {_operand(value)}"


def describe_logic(logic: Any, items_by_name: dict[str, dict]) -> str:
    if not isinstance(logic, dict) or not logic.get("conditions"):
        return ""
    joiner = " OR " if logic.get("match") == "any" else " AND "
    return joiner.join(describe_condition(condition, items_by_name) for condition in logic["conditions"])


# -- entries ------------------------------------------------------------------------


def _item_entries(activity: dict, keys: list[str]) -> list[_Entry]:
    signed = ("id", "name", "responseType", "question", "responseValues", "config", "isHidden", "conditionalLogic")
    return [
        _Entry(key, _signature({k: item.get(k) for k in (*signed, "allowEdit")}), item, position)
        for position, (item, key) in enumerate(zip(activity["items"], keys), start=1)
    ]


def _lookup_table(rows: Any) -> str:
    parts = []
    for row in rows or []:
        text = f"raw {_num(row.get('rawScore'))}"
        if row.get("score") not in (None, ""):
            text += f" -> {_num(row['score'])}"
        qualifiers = [
            f"sex {row['sex']}" if row.get("sex") else "",
            f"age {_num(row['age'])}" if row.get("age") not in (None, "") else "",
            _num(row.get("severity")),
        ]
        if any(qualifiers):
            text += f" ({', '.join(q for q in qualifiers if q)})"
        if row.get("optionalText"):
            text += f": {row['optionalText']}"
        parts.append(text)
    return " | ".join(parts)


def _score_entries(activity: dict) -> list[_Entry]:
    """The score columns responses.csv gets for this activity (getSubscales.ts)."""
    setting = activity.get("subscaleSetting") or {}
    subscales = setting.get("subscales") or []
    if not subscales:
        return []  # the admin adds score columns only when there is at least one subscale

    def entry(column: str, data: dict) -> _Entry:
        return _Entry(column, _signature(data), {"column": column, **data})

    entries = []
    if setting.get("calculateTotalScore"):
        items = [
            item.get("name")
            for item in activity["items"]
            if not item.get("isHidden") and item.get("responseType") in TOTAL_SCORE_TYPES and not is_system_item(item)
        ]
        table = setting.get("totalScoresTableData") or []
        entries.append(
            entry(
                FINAL_SCORE_COLUMN,
                {"kind": "total_score", "scoring": setting["calculateTotalScore"], "items": items, "table": table},
            )
        )
        entries.append(entry(FINAL_TEXT_COLUMN, {"kind": "lookup_text", "of": "the total score", "table": table}))
    for subscale in subscales:
        name = to_string(subscale.get("name"))
        items = [
            f"{part.get('name')} (subscale)" if part.get("type") == "subscale" else to_string(part.get("name"))
            for part in subscale.get("items") or []
        ]
        table = subscale.get("subscaleTableData") or []
        entries.append(
            entry(
                f"subscale_name_{clean_name(name)}",
                {
                    "kind": "subscale_score",
                    "name": name,
                    "scoring": subscale.get("scoring"),
                    "items": items,
                    "table": table,
                },
            )
        )
        if any(row.get("optionalText") for row in table):
            entries.append(
                entry(
                    f"subscale_lookup_text_{clean_name(name)}",
                    {"kind": "lookup_text", "of": f"subscale {name}", "table": table},
                )
            )
    return entries


def _item_cells(entry: _Entry, activity: dict) -> dict[str, Any]:
    return item_cells(entry.data, activity, entry.position)


def item_cells(item: dict, activity: dict, position: int = 0) -> dict[str, Any]:
    """The dictionary cells of one item; `activity` gives the context for skip logic and skippability."""
    response_type = item.get("responseType") or ""
    values = item.get("responseValues") if isinstance(item.get("responseValues"), dict) else {}
    config = item.get("config") if isinstance(item.get("config"), dict) else {}
    ranged = response_type in ("slider", "numberSelect")
    return {
        "item_order": position,
        "item_id": item.get("id"),
        "item_name": item.get("name"),
        "item_type": response_type,
        "item_prompt": display_text(item.get("question")),
        # Exactly what responses.csv shows for this item.
        "item_response_options": to_string(coalesce(parse_options(item.get("responseValues"), response_type), "")),
        "value_labels": _value_labels(response_type, values),
        "scores": _scores(response_type, values),
        "min_value": _num(values.get("minValue")) if ranged else "",
        "max_value": _num(values.get("maxValue")) if ranged else "",
        "min_label": _num(values.get("minLabel")) if response_type == "slider" else "",
        "max_label": _num(values.get("maxLabel")) if response_type == "slider" else "",
        "rows": _rows(response_type, values),
        "response_rules": _response_rules(response_type, values, config),
        "other_text_option": _other_text_option(config),
        "skippable": _skippable(response_type, config, activity),
        "hidden": "yes" if item.get("isHidden") else "no",
        "shown_if": describe_logic(item.get("conditionalLogic"), {i.get("name"): i for i in activity["items"]}),
    }


def score_rows(activity: dict) -> list[dict[str, Any]]:
    """The score rows a download writes for the activity as it is now."""
    return [_score_cells(entry) for entry in _score_entries(activity)]


def subscale_cells(name: str, scoring: str, items: list[str], table: list | None = None) -> dict[str, Any]:
    """The row of one subscale, as a download writes it. Nested subscales in `items` end in ' (subscale)'."""
    column = f"subscale_name_{clean_name(name)}"
    data = {"column": column, "kind": "subscale_score", "name": name, "scoring": scoring, "items": items}
    return _score_cells(_Entry(column, "", {**data, "table": table or []}))


def _score_cells(entry: _Entry) -> dict[str, Any]:
    data = entry.data
    if data["kind"] == "lookup_text":
        prompt = f"Text from the lookup table of {data['of']}"
    else:
        formula = _SCORING.get(data["scoring"], f"{data['scoring']} of {{}}").format(", ".join(data["items"]))
        prompt = f"Total score: {formula}" if data["kind"] == "total_score" else f"Subscale {data['name']}: {formula}"
    return {
        "item_name": data["column"],
        "item_type": data["kind"],
        "item_prompt": prompt,
        "value_labels": _lookup_table(data["table"]),
    }


# -- one dictionary per activity ---------------------------------------------------


@dataclass
class ActivityDictionary:
    activity_id: str
    activity_name: str
    rows: list[dict]
    items: int
    items_changed: int
    first_version: str
    last_version: str
    status: str
    notes: list[str] = field(default_factory=list)


def _flows_by_activity(snapshot: dict) -> dict[str, list[str]]:
    flows: dict[str, list[str]] = {}
    for flow in snapshot.get("activityFlows") or []:
        for flow_item in flow.get("items") or []:
            activity_id = (flow_item.get("activity") or {}).get("id") or flow_item.get("activityId")
            if activity_id and flow.get("name") not in flows.get(activity_id, []):
                flows.setdefault(activity_id, []).append(flow.get("name"))
    return flows


def build_dictionaries(history: AppletHistory) -> list[ActivityDictionary]:
    names = history.versions
    by_version = [{a["id"]: a for a in snapshot.get("activities") or []} for snapshot in history.snapshots]
    flows = [_flows_by_activity(snapshot) for snapshot in history.snapshots]
    activity_order = _merged_order(
        [a["id"] for a in snapshot.get("activities") or []] for snapshot in history.snapshots
    )

    dictionaries = []
    for activity_id in sorted(activity_order, key=activity_order.get):
        activities = [version.get(activity_id) for version in by_version]
        present = [index for index, activity in enumerate(activities) if activity is not None]
        latest = activities[present[-1]]
        item_versions = [
            (activity, _item_entries(activity, keys)) if activity is not None else None
            for activity, keys in zip(activities, _item_keys(activities))
        ]
        score_versions = [
            (activity, _score_entries(activity)) if activity is not None else None for activity in activities
        ]
        item_rows = _sorted_rows(_track(item_versions, names, describe_item_change), item_versions)
        score_rows = _sorted_rows(_track(score_versions, names, describe_score_change), score_versions)

        rows = []
        for row, cells in [(r, _item_cells(r.entry, r.activity)) for r in item_rows] + [
            (r, _score_cells(r.entry)) for r in score_rows
        ]:
            flow_names = list(
                dict.fromkeys(name for index in row.versions for name in flows[index].get(activity_id, []))
            )
            rows.append(
                {
                    "applet_id": history.applet_id,
                    "applet_name": history.applet_name,
                    "activity_id": activity_id,
                    "activity_name": row.activity.get("name"),
                    "activity_flows": " | ".join(flow_names),
                    **cells,
                    "first_version": names[row.versions[0]],
                    "last_version": names[row.versions[-1]],
                    "applet_versions": ", ".join(names[index] for index in row.versions),
                    "status": row.status,
                    "change": row.change,
                }
            )

        keys = [row.entry.key for row in item_rows]
        notes = []
        if latest.get("isReviewable"):
            notes.append("reviewer assessment")
        if latest.get("isHidden"):
            notes.append("hidden from participants")
        dictionaries.append(
            ActivityDictionary(
                activity_id=activity_id,
                activity_name=latest.get("name") or activity_id,
                rows=rows,
                items=len(set(keys)),
                items_changed=sum(1 for key in set(keys) if keys.count(key) > 1),
                first_version=names[present[0]],
                last_version=names[present[-1]],
                status="current" if present[-1] == len(names) - 1 else f"removed in {names[present[-1] + 1]}",
                notes=notes,
            )
        )
    return dictionaries


# -- writing ------------------------------------------------------------------------


def write_dictionaries(
    folder: Path, history: AppletHistory, dictionaries: list[ActivityDictionary], used_names: set[str]
) -> list[dict]:
    """One CSV per activity, named <applet>__<activity>__data_dictionary.csv. Returns index rows."""
    index_rows = []
    for dictionary in dictionaries:
        # File names are limited to 255 bytes.
        stem = f"{file_slug(history.applet_name)[:80]}__{file_slug(dictionary.activity_name)[:80]}"
        name = f"{stem}__data_dictionary.csv"
        if name.lower() in used_names:
            # Same names (e.g. a deleted activity and a new one, or two applets): add the IDs.
            name = f"{stem}_{history.applet_id[:8]}_{dictionary.activity_id[:8]}__data_dictionary.csv"
        used_names.add(name.lower())
        write_csv(folder / name, dictionary.rows, DICTIONARY_COLUMNS)
        index_rows.append(
            {
                "applet_id": history.applet_id,
                "applet_name": history.applet_name,
                "activity_id": dictionary.activity_id,
                "activity_name": dictionary.activity_name,
                "file": name,
                "items": dictionary.items,
                "items_changed": dictionary.items_changed,
                "first_version": dictionary.first_version,
                "last_version": dictionary.last_version,
                "status": dictionary.status,
                "note": "; ".join(dictionary.notes),
            }
        )
    return index_rows
