"""Turn data dictionary rows back into Curious items, and check them the way the backend will.

The CSV format is the one `--dictionary` writes (and the REDCap converter produces). For an item
that already exists, only the aspects whose cells differ from what a download would show are
changed, so downloading a dictionary and uploading it unedited changes nothing.
"""

from __future__ import annotations

import copy
import csv
import io
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .dictionary import describe_logic, display_text, item_cells, score_rows
from .jscompat import coalesce, to_string
from .report import parse_options

SELECT_TYPES = ("singleSelect", "multiSelect")
GRID_TYPES = ("singleSelectRows", "multiSelectRows")
SCORE_ROW_TYPES = ("total_score", "subscale_score", "lookup_text")
# Types skip logic may refer to (backend ResponseType.conditional_logic_types).
CONDITION_TYPES = (
    "date",
    "numberSelect",
    "time",
    "timeRange",
    "singleSelectRows",
    "multiSelectRows",
    "sliderRows",
    "singleSelect",
    "multiSelect",
    "slider",
)
_SCORED_TYPES = ("singleSelect", "multiSelect", "slider")

_NO_ANSWER = {"textInputOption": False, "textInputRequired": False}
_SCREEN = {"removeBackButton": False, "skippableItem": False}
# What the admin Builder sends for a new item of each type (ItemConfiguration defaults).
_DEFAULT_CONFIG: dict[str, dict] = {
    "singleSelect": {
        **_SCREEN,
        "randomizeOptions": False,
        "addScores": False,
        "setAlerts": False,
        "addTooltip": False,
        "setPalette": False,
        "timer": 0,
        "additionalResponseOption": _NO_ANSWER,
        "portraitLayout": False,
        "autoAdvance": False,
        "responseDataIdentifier": False,
    },
    "multiSelect": {
        **_SCREEN,
        "randomizeOptions": False,
        "addScores": False,
        "setAlerts": False,
        "addTooltip": False,
        "setPalette": False,
        "timer": 0,
        "additionalResponseOption": _NO_ANSWER,
        "portraitLayout": False,
    },
    "slider": {
        **_SCREEN,
        "addScores": False,
        "setAlerts": False,
        "showTickMarks": False,
        "showTickLabels": False,
        "continuousSlider": False,
        "timer": 0,
        "additionalResponseOption": _NO_ANSWER,
    },
    "numberSelect": {**_SCREEN, "additionalResponseOption": _NO_ANSWER},
    "text": {
        **_SCREEN,
        "maxResponseLength": 300,
        "correctAnswerRequired": False,
        "correctAnswer": "",
        "numericalResponseRequired": False,
        "responseDataIdentifier": False,
        "responseRequired": False,
    },
    "paragraphText": {**_SCREEN, "maxResponseLength": 1000, "responseRequired": False},
    "message": {"removeBackButton": False, "timer": 0},
    "sliderRows": {**_SCREEN, "addScores": False, "setAlerts": False, "timer": 0},
    "singleSelectRows": {**_SCREEN, "timer": 0, "addScores": False, "setAlerts": False, "addTooltip": False},
    "multiSelectRows": {**_SCREEN, "timer": 0, "addScores": False, "setAlerts": False, "addTooltip": False},
    "drawing": {
        **_SCREEN,
        "additionalResponseOption": _NO_ANSWER,
        "timer": 0,
        "removeUndoButton": False,
        "navigationToTop": False,
    },
}
for _type in ("date", "time", "timeRange", "photo", "video", "geolocation", "audio"):
    _DEFAULT_CONFIG[_type] = {**_SCREEN, "additionalResponseOption": _NO_ANSWER, "timer": 0}
# Types a dictionary can create. Others (flanker, A/B trails, Unity, audio player, …) need files or
# task settings, so they can only be kept or have their prompt edited.
CREATABLE_TYPES = tuple(_DEFAULT_CONFIG)
_NO_RESPONSE_VALUES = ("text", "paragraphText", "message", "timeRange", "geolocation", "photo", "video", "date", "time")


class DictionaryError(Exception):
    """The dictionary files can't be read at all."""


@dataclass
class Problem:
    """Something wrong with one item. Errors block the upload; warnings don't."""

    activity: str
    item: str
    message: str
    severity: str = "error"
    where: str = ""  # file and line

    def __str__(self) -> str:
        place = f"{self.activity} / {self.item}" if self.item else self.activity
        return f"{place}: {self.message}" + (f" ({self.where})" if self.where else "")


@dataclass
class ItemRow:
    cells: dict[str, str]
    where: str

    @property
    def name(self) -> str:
        return self.cells.get("item_name", "")


@dataclass
class ActivityRows:
    name: str
    activity_id: str
    items: list[ItemRow] = field(default_factory=list)
    score_rows: list[ItemRow] = field(default_factory=list)  # total score, subscales, and lookup text
    columns: set[str] = field(default_factory=set)


# -- reading CSVs -----------------------------------------------------------------------------------


def _unsanitize(text: str) -> str:
    """Undo csvSanitization: a quote the writer put before a line starting with = + - @ or a tab."""
    return "\n".join(
        line[1:] if line.startswith("'") and line[1:2] in ("=", "+", "-", "@", "\t", "\r") else line
        for line in text.split("\n")
    )


def _decode(raw: bytes, name: str) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Excel's plain "CSV" format is Windows-1252.
        return raw.decode("cp1252")


def dictionary_files(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(p for p in path.glob("*.csv") if p.name.lower() not in ("index.csv", "conversion_report.csv"))
        if not files:
            raise DictionaryError(f"No data dictionary CSV files in {path}.")
        return files
    if not path.is_file():
        raise DictionaryError(f"{path} does not exist.")
    return [path]


def read_dictionaries(paths: Iterable[Path]) -> list[ActivityRows]:
    """Group rows by activity, in file order. History rows (status 'changed in …'/'removed in …') are skipped."""
    activities: dict[str, ActivityRows] = {}
    for path in paths:
        reader = csv.DictReader(io.StringIO(_decode(path.read_bytes(), path.name)))
        columns = {(c or "").strip() for c in reader.fieldnames or []}
        missing = {"activity_name", "item_name", "item_type"} - columns
        if missing:
            raise DictionaryError(
                f"{path.name} has no {', '.join(sorted(missing))} column, so it isn't a data dictionary."
            )
        for line, raw_row in enumerate(reader, start=2):
            cells = {(k or "").strip(): _unsanitize((v or "").strip()) for k, v in raw_row.items() if k}
            if not any(cells.values()):
                continue
            status = cells.get("status", "")
            if status.startswith(("changed in", "removed in")):
                continue
            name = cells.get("activity_name", "")
            if not name:
                raise DictionaryError(f"{path.name} line {line}: activity_name is empty.")
            activity = activities.setdefault(name, ActivityRows(name, cells.get("activity_id", "")))
            activity.columns |= columns
            if cells.get("item_type") in SCORE_ROW_TYPES:
                activity.score_rows.append(ItemRow(cells, f"{path.name} line {line}"))
                continue
            activity.items.append(ItemRow(cells, f"{path.name} line {line}"))
    return list(activities.values())


# -- parsing cells ----------------------------------------------------------------------------------


class CellError(ValueError):
    pass


def _parts(cell: str) -> list[str]:
    return [part.strip() for part in cell.split("|") if part.strip()] if cell.strip() else []


def _int(text: str, what: str) -> int:
    try:
        number = float(text.strip())
    except ValueError:
        raise CellError(f"{what} {text!r} is not a whole number") from None
    if not number.is_integer():
        raise CellError(f"{what} {text!r} is not a whole number")
    return int(number)


def _number(text: str, what: str) -> int | float:
    try:
        number = float(text.strip())
    except ValueError:
        raise CellError(f"{what} {text!r} is not a number") from None
    return int(number) if number.is_integer() else number


_OPTION_FLAGS = ("none of the above", "hidden")
_FLAG_SUFFIX = re.compile(r"^(?P<label>.*?) \((?P<flags>[^()]*)\)$")


def parse_value_labels(cell: str) -> list[tuple[int, str, set[str]]]:
    """'0=Never | 1=Often (none of the above)' -> [(0, 'Never', set()), (1, 'Often', {'none of the above'})]."""
    options = []
    for part in _parts(cell):
        value, sep, label = part.partition("=")
        if not sep:
            raise CellError(f"option {part!r} should look like value=label")
        flags: set[str] = set()
        match = _FLAG_SUFFIX.match(label.strip())
        if match and all(flag.strip() in _OPTION_FLAGS for flag in match["flags"].split(",")):
            label, flags = match["label"], {flag.strip() for flag in match["flags"].split(",")}
        options.append((_int(value, "option value"), label.strip(), flags))
    return options


def parse_scores(cell: str) -> dict[int, int | float]:
    scores = {}
    for part in _parts(cell):
        value, sep, score = part.partition("=")
        if not sep:
            raise CellError(f"score {part!r} should look like value=score")
        scores[_int(value, "scored value")] = _number(score, "score")
    return scores


def _prefixed(cell: str, names: list[str], what: str) -> dict[str, str]:
    """'Row one: a=1, b=2 | Row two: …' -> {'Row one': 'a=1, b=2', …}, matching the known row names."""
    found = {}
    for part in _parts(cell):
        name = next((n for n in sorted(names, key=len, reverse=True) if part.startswith(f"{n}:")), None)
        if name is None:
            raise CellError(f"{what} {part!r} doesn't start with one of the rows ({', '.join(names)})")
        found[name] = part[len(name) + 1 :].strip()
    return found


_SLIDER_ROW = re.compile(r"^(?P<label>.*) \((?P<min>-?\d+)(?:=(?P<minl>.*?))? to (?P<max>-?\d+)(?:=(?P<maxl>.*))?\)$")


def parse_slider_rows(cell: str) -> list[dict]:
    rows = []
    for part in _parts(cell):
        match = _SLIDER_ROW.match(part)
        if not match:
            raise CellError(f"slider row {part!r} should look like 'Label (0=low label to 10=high label)'")
        rows.append(
            {
                "label": match["label"].strip(),
                "minValue": int(match["min"]),
                "maxValue": int(match["max"]),
                "minLabel": (match["minl"] or "").strip(),
                "maxLabel": (match["maxl"] or "").strip(),
            }
        )
    return rows


_RULES = [
    (re.compile(r"max (\d+) characters"), "maxResponseLength"),
    (re.compile(r"numbers only"), "numericalResponseRequired"),
    (re.compile(r"correct answer: (.*)"), "correctAnswer"),
    (re.compile(r"response required"), "responseRequired"),
    (re.compile(r"used as response identifier"), "responseDataIdentifier"),
    (re.compile(r"max (\d+) seconds"), "maxDuration"),
    (re.compile(r"options in random order"), "randomizeOptions"),
    (re.compile(r"continuous slider"), "continuousSlider"),
    (re.compile(r"plays once"), "playOnce"),
    (re.compile(r"time limit (\d+) seconds"), "timer"),
]


def parse_rules(cell: str) -> dict[str, Any]:
    rules: dict[str, Any] = {}
    for part in (p.strip() for p in cell.split(";")):
        if not part:
            continue
        for pattern, key in _RULES:
            match = pattern.fullmatch(part)
            if match:
                if not match.groups():
                    rules[key] = True
                else:
                    rules[key] = match[1] if key == "correctAnswer" else int(match[1])
                break
        else:
            raise CellError(f"unknown rule {part!r}")
    return rules


# -- skip logic ---------------------------------------------------------------------------------------

_JOINER = re.compile(r"\s+(AND|OR)\s+")
_SUBJECT = re.compile(r'(?P<name>[^\s\[\]"]+)(?: \[(?P<row>[^\]]*)\])?(?: \((?P<side>start|end)\))?\s+')
_OPERATOR = re.compile(r"(does not include|includes|not between|between|!=|=|>|<)\s+")
_QUOTED = re.compile(r'"(?P<label>.*?)"(?: \((?P<value>-?\d+(?:\.\d+)?)\))?')
_BARE = re.compile(r'(?P<value>[^\s"]+)')
_RANGE = re.compile(r'(?P<low>[^\s"]+)\s+and\s+(?P<high>[^\s"]+)')

_SUFFIXES = {
    "slider": "",
    "numberSelect": "",
    "date": "_DATE",
    "time": "_TIME",
    "timeRange": "_TIME_RANGE",
    "sliderRows": "_SLIDER_ROWS",
}
_BASE_OPERATORS = {">": "GREATER_THAN", "<": "LESS_THAN", "=": "EQUAL", "!=": "NOT_EQUAL"}


def _condition_type(response_type: str, operator: str) -> str:
    suffix = _SUFFIXES[response_type]
    if operator in ("between", "not between"):
        base = "BETWEEN" if operator == "between" else "OUTSIDE_OF"
        plural = {"_DATE": "_DATES", "_TIME": "_TIMES", "_TIME_RANGE": "_TIMES_RANGE"}.get(suffix, suffix)
        return base + plural
    base = _BASE_OPERATORS[operator]
    if suffix and base in ("EQUAL", "NOT_EQUAL"):
        base += "_TO"  # EQUAL_TO_DATE, NOT_EQUAL_TO_TIME, …
    return base + suffix


def _time(text: str) -> dict:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise CellError(f"{text!r} is not a time (HH:MM)")
    return {"hours": int(match[1]), "minutes": int(match[2])}


def _date(text: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise CellError(f"{text!r} is not a date (YYYY-MM-DD)")
    return text


def _scalar(response_type: str, text: str) -> Any:
    if response_type == "date":
        return _date(text)
    if response_type in ("time", "timeRange"):
        return _time(text)
    return _number(text, "value")


def _row_index(item: dict, row: str | None) -> str:
    rows = (item.get("responseValues") or {}).get("rows") or []
    names = [r.get("rowName") or r.get("label") or "" for r in rows]
    if row is None:
        raise CellError(f"{item['name']} has rows; say which one, like {item['name']} [{names[0] if names else 'row'}]")
    for index, name in enumerate(names):
        if name == row:
            return str(index)
    match = re.fullmatch(r"row (\d+)", row)
    if match and 0 < int(match[1]) <= len(rows):
        return str(int(match[1]) - 1)
    raise CellError(f"{item['name']} has no row {row!r}")


def _option(item: dict, label: str | None, value: str | None) -> dict:
    options = (item.get("responseValues") or {}).get("options") or []
    if value is not None:
        found = [
            o
            for o in options
            if o.get("value") is not None and to_string(o.get("value")) == to_string(_number(value, "option value"))
        ]
    else:
        found = [o for o in options if (o.get("text") or "").strip().lower() == (label or "").strip().lower()]
    if not found:
        shown = f"({value})" if value is not None else f'"{label}"'
        raise CellError(f"{item['name']} has no option {shown}")
    return found[0]


def _parse_condition(text: str, items_by_name: dict[str, dict]) -> dict:
    subject = _SUBJECT.match(text)
    if not subject:
        raise CellError(f"can't read the condition {text!r}")
    name, row, side = subject["name"], subject["row"], subject["side"]
    rest = text[subject.end() :]
    operator_match = _OPERATOR.match(rest)
    if not operator_match:
        raise CellError(f"no operator (=, !=, >, <, includes, between, …) in {text!r}")
    operator, operand = operator_match[1], rest[operator_match.end() :].strip()

    item = items_by_name.get(name)
    if item is None:
        raise CellError(f"the condition refers to {name!r}, which is not an earlier item in this activity")
    response_type = item.get("responseType")
    if response_type not in CONDITION_TYPES:
        raise CellError(f"{name} is a {response_type} item, which skip logic can't use")

    def need(*allowed: str) -> None:
        if operator not in allowed:
            raise CellError(f"{name} is a {response_type} item; use {' or '.join(allowed)}, not {operator!r}")

    if response_type in SELECT_TYPES or response_type in GRID_TYPES:
        quoted = _QUOTED.fullmatch(operand)
        if quoted:
            label, value = quoted["label"], quoted["value"]
        elif re.fullmatch(r"-?\d+", operand):
            label, value = None, operand
        else:
            label, value = operand.strip('"'), None  # an unquoted label
        multiple = response_type in ("multiSelect", "multiSelectRows")
        if multiple:
            need("includes", "does not include")
            kind = "INCLUDES" if operator == "includes" else "NOT_INCLUDES"
        else:
            need("=", "!=")
            kind = "EQUAL_TO" if operator == "=" else "NOT_EQUAL_TO"
        if response_type in GRID_TYPES:
            option = _option(item, label if label is not None else value, None)
            payload = {"optionValue": option.get("id"), "rowIndex": _row_index(item, row)}
            return {"itemName": name, "type": f"{kind}_ROW_OPTION", "payload": payload}
        option = _option(item, label, value)
        return {"itemName": name, "type": f"{kind}_OPTION", "payload": {"optionValue": to_string(option.get("value"))}}

    need(">", "<", "=", "!=", "between", "not between")
    if response_type == "timeRange" and side is None:
        raise CellError(f"{name} is a time range; say {name} (start) or {name} (end)")
    payload: dict[str, Any] = {}
    if operator in ("between", "not between"):
        bounds = _RANGE.fullmatch(operand)
        if not bounds:
            raise CellError(f"write a range as 'between A and B' in {text!r}")
        low, high = _scalar(response_type, bounds["low"]), _scalar(response_type, bounds["high"])
        keys = {"date": ("minDate", "maxDate"), "time": ("minTime", "maxTime"), "timeRange": ("minTime", "maxTime")}
        low_key, high_key = keys.get(response_type, ("minValue", "maxValue"))
        payload |= {low_key: low, high_key: high}
    else:
        bare = _BARE.fullmatch(operand)
        if not bare:
            raise CellError(f"can't read the value in {text!r}")
        value = _scalar(response_type, bare["value"])
        key = {"date": "date", "time": "time", "timeRange": "time"}.get(response_type, "value")
        payload[key] = value
    if response_type == "timeRange":
        payload["fieldName"] = "from" if side == "start" else "to"
    if response_type == "sliderRows":
        payload["rowIndex"] = _row_index(item, row)
    return {"itemName": name, "type": _condition_type(response_type, operator), "payload": payload}


def parse_shown_if(text: str, items_by_name: dict[str, dict]) -> dict | None:
    """'mood = "Often" (1) AND sleep > 6' -> conditionalLogic. Only earlier items may be referenced."""
    text = text.strip()
    if not text:
        return None
    joiners = set(_JOINER.findall(text))
    if len(joiners) > 1:
        raise CellError("Curious can't mix AND and OR in one item's skip logic")
    parts = _JOINER.split(text)[::2]
    conditions = [_parse_condition(part.strip(), items_by_name) for part in parts]
    return {"match": "any" if joiners == {"OR"} else "all", "conditions": conditions}


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


# -- building items ---------------------------------------------------------------------------------


def new_item(response_type: str, name: str) -> dict:
    item: dict[str, Any] = {
        "name": name,
        "question": {"en": ""},
        "responseType": response_type,
        "config": copy.deepcopy(_DEFAULT_CONFIG[response_type]),
        "responseValues": None,
        "isHidden": False,
        "allowEdit": True,
        "conditionalLogic": None,
    }
    if response_type in SELECT_TYPES:
        item["responseValues"] = {"paletteName": None, "options": []}
    elif response_type == "slider":
        item["responseValues"] = {"minValue": 0, "maxValue": 12, "minLabel": "", "maxLabel": ""}
    elif response_type == "numberSelect":
        item["responseValues"] = {"minValue": 0, "maxValue": 1}
    elif response_type in GRID_TYPES:
        item["responseValues"] = {"rows": [], "options": []}
    elif response_type == "sliderRows":
        item["responseValues"] = {"rows": []}
    elif response_type == "audio":
        item["responseValues"] = {"maxDuration": 300}
    elif response_type == "drawing":
        item["responseValues"] = {"proportion": {"enabled": True}}
    return item


def _set_prompt(question: Any, text: str) -> dict:
    question = dict(question) if isinstance(question, dict) else {}
    language = "en" if question.get("en") or not question else next((k for k, v in question.items() if v), "en")
    question[language] = text
    return question


def _apply_select(item: dict, cells: dict, change: set[str]) -> None:
    values = item["responseValues"]
    config = item["config"]
    old_options = {o.get("value"): o for o in values.get("options") or []}
    if "value_labels" in change:
        parsed = parse_value_labels(cells["value_labels"])
        if not parsed:
            raise CellError("a choice item needs options in value_labels, like 0=No | 1=Yes")
    else:
        parsed = [
            (
                o.get("value"),
                o.get("text") or "",
                {f for f, on in (("none of the above", o.get("isNoneAbove")), ("hidden", o.get("isHidden"))) if on},
            )
            for o in values.get("options") or []
        ]
    if "scores" in change:
        scores = parse_scores(cells.get("scores", ""))
    else:
        scores = {o.get("value"): o.get("score") for o in old_options.values() if config.get("addScores")}

    seen = set()
    options = []
    for value, text, flags in parsed:
        if value in seen:
            raise CellError(f"option value {value} is used twice")
        seen.add(value)
        option = copy.deepcopy(old_options.get(value)) or {"id": str(uuid.uuid4()), "isHidden": False}
        option.update(text=text, value=value, isHidden="hidden" in flags)
        if "none of the above" in flags and item["responseType"] != "multiSelect":
            raise CellError("only multiple-choice options can be 'none of the above'")
        if item["responseType"] == "multiSelect":
            option["isNoneAbove"] = "none of the above" in flags
        option["score"] = scores.get(value) if scores else None
        options.append(option)
    unknown = sorted(set(scores) - seen)
    if unknown:
        raise CellError(f"scores given for values that aren't options: {', '.join(map(str, unknown))}")
    if scores and len(scores) < len(options):
        missing = [str(o["value"]) for o in options if o["value"] not in scores]
        raise CellError(f"scores are missing for values {', '.join(missing)} (score every option or none)")
    values["options"] = options
    config["addScores"] = bool(scores)


def _apply_slider(item: dict, cells: dict, change: set[str]) -> None:
    values = item["responseValues"]
    if "min_value" in change:
        values["minValue"] = _int(cells["min_value"], "min_value")
    if "max_value" in change:
        values["maxValue"] = _int(cells["max_value"], "max_value")
    if "min_label" in change and item["responseType"] == "slider":
        values["minLabel"] = cells["min_label"]
    if "max_label" in change and item["responseType"] == "slider":
        values["maxLabel"] = cells["max_label"]
    if item["responseType"] == "slider" and change & {"scores", "min_value", "max_value"}:
        if "scores" in change:
            scores = parse_scores(cells.get("scores", ""))
        else:
            old = values.get("scores") or []
            scores = {values.get("minValue", 0) + i: s for i, s in enumerate(old)} if old else {}
        span = list(range(values["minValue"], values["maxValue"] + 1))
        if scores and sorted(scores) != span:
            raise CellError(f"slider scores must cover every value from {span[0]} to {span[-1]}")
        values["scores"] = [scores[v] for v in span] if scores else None
        item["config"]["addScores"] = bool(scores)


def _apply_slider_rows(item: dict, cells: dict, change: set[str]) -> None:
    values = item["responseValues"]
    old_rows = {r.get("label"): r for r in values.get("rows") or []}
    if "rows" in change:
        parsed = parse_slider_rows(cells["rows"])
        if not parsed:
            raise CellError("a slider grid needs rows, like 'Pain (0=none to 10=worst)'")
        rows = []
        for spec in parsed:
            row = copy.deepcopy(old_rows.get(spec["label"])) or {"id": str(uuid.uuid4())}
            row.update(spec)
            rows.append(row)
    else:
        rows = values.get("rows") or []
    if "scores" in change:
        by_row = _prefixed(cells.get("scores", ""), [r["label"] for r in rows], "slider row scores")
        for row in rows:
            scores = parse_scores(by_row[row["label"]].replace(",", "|")) if row["label"] in by_row else {}
            span = list(range(row["minValue"], row["maxValue"] + 1))
            if scores and sorted(scores) != span:
                raise CellError(f"scores for row {row['label']!r} must cover {span[0]} to {span[-1]}")
            row["scores"] = [scores[v] for v in span] if scores else None
    values["rows"] = rows
    scored = [bool(row.get("scores")) for row in rows]
    if any(scored) and not all(scored):
        raise CellError("score every slider row or none (Curious needs scores on all rows once scoring is on)")
    item["config"]["addScores"] = any(scored)


def _apply_grid(item: dict, cells: dict, change: set[str]) -> None:
    values = item["responseValues"]
    old_rows = {r.get("rowName"): r for r in values.get("rows") or []}
    old_options = {o.get("text"): o for o in values.get("options") or []}
    if "rows" in change:
        names = _parts(cells["rows"])
        if not names:
            raise CellError("a grid needs rows, like 'Row one | Row two'")
        values["rows"] = [copy.deepcopy(old_rows.get(n)) or {"id": str(uuid.uuid4()), "rowName": n} for n in names]
    if "value_labels" in change:
        texts = _parts(cells["value_labels"])
        if not texts:
            raise CellError("a grid needs options in value_labels, like 'Yes | No'")
        values["options"] = [copy.deepcopy(old_options.get(t)) or {"id": str(uuid.uuid4()), "text": t} for t in texts]
    if change & {"scores", "rows", "value_labels"}:
        old_matrix = {
            (row.get("rowId"), option.get("optionId")): option
            for row in values.get("dataMatrix") or []
            for option in row.get("options") or []
        }
        if "scores" in change:
            by_row = _prefixed(cells.get("scores", ""), [r["rowName"] for r in values["rows"]], "grid scores")
            scores = {}
            for row in values["rows"]:
                text = by_row.get(row["rowName"], "")
                for option in values["options"]:
                    match = re.search(rf"(?:^|,\s*){re.escape(option['text'])}=(-?\d+(?:\.\d+)?)", text)
                    if match:
                        scores[(row["id"], option["id"])] = _number(match[1], "score")
        else:
            scores = {key: o.get("score") for key, o in old_matrix.items() if o.get("score") is not None}
        if scores or item["config"].get("setAlerts"):
            values["dataMatrix"] = [
                {
                    "rowId": row["id"],
                    "options": [
                        {
                            **old_matrix.get((row["id"], o["id"]), {}),
                            "optionId": o["id"],
                            "score": scores.get((row["id"], o["id"])),
                        }
                        for o in values["options"]
                    ],
                }
                for row in values["rows"]
            ]
            if scores and len(scores) < len(values["rows"]) * len(values["options"]):
                raise CellError("grid scores are missing for some row and option pairs (score all or none)")
        else:
            values["dataMatrix"] = None
        item["config"]["addScores"] = bool(scores)


def _apply_rules(item: dict, cells: dict, problems: list[str]) -> None:
    rules = parse_rules(cells.get("response_rules", ""))
    config, values, response_type = item["config"], item.get("responseValues"), item["responseType"]
    text_like = response_type in ("text", "paragraphText")
    applicable = {
        "maxResponseLength": text_like,
        "responseRequired": text_like,
        "numericalResponseRequired": response_type == "text",
        "correctAnswer": response_type == "text",
        "responseDataIdentifier": response_type == "text",
        "maxDuration": response_type == "audio",
        "randomizeOptions": response_type in SELECT_TYPES,
        "continuousSlider": response_type == "slider",
        "playOnce": response_type == "audioPlayer",
        "timer": "timer" in config,
    }
    for key in rules:
        if not applicable.get(key):
            problems.append(f"the rule for {_RULE_WORDS[key]} doesn't apply to {response_type} items and was ignored")
    for key, ok in applicable.items():
        if not ok:
            continue
        if key == "maxResponseLength":
            if key in rules:
                config[key] = rules[key]
        elif key == "maxDuration":
            if key in rules:
                values[key] = rules[key]
        elif key == "timer":
            config[key] = rules.get(key, 0 if config.get(key) else config.get(key))
        elif key == "correctAnswer":
            config["correctAnswerRequired"] = key in rules
            config["correctAnswer"] = rules.get(key, "")
        else:
            config[key] = bool(rules.get(key))


_RULE_WORDS = {
    "maxResponseLength": "maximum characters",
    "numericalResponseRequired": "numbers only",
    "correctAnswer": "a correct answer",
    "responseRequired": "a required response",
    "responseDataIdentifier": "a response identifier",
    "maxDuration": "maximum seconds",
    "randomizeOptions": "random option order",
    "continuousSlider": "a continuous slider",
    "playOnce": "playing once",
    "timer": "a time limit",
}


def build_item(row: ItemRow, existing: dict | None, activity: dict) -> tuple[dict, list[str]]:
    """The item to save for one row, plus warnings. `existing` is the applet's current item, if any.

    Skip logic is resolved later by `resolve_logic`, once every item of the activity is known.
    """
    cells = row.cells
    response_type = cells.get("item_type", "")
    warnings: list[str] = []
    if existing is not None and existing.get("responseType") == response_type:
        item = copy.deepcopy(existing)
        before = {k: str(coalesce(v, "")).strip() for k, v in item_cells(existing, activity).items()}
        change = {col for col, value in cells.items() if col in before and value != before[col]}
    else:
        if response_type not in CREATABLE_TYPES:
            known = ", ".join(CREATABLE_TYPES)
            raise CellError(f"item_type {response_type!r} can't be created from a dictionary (use one of: {known})")
        item = new_item(response_type, cells["item_name"])
        if existing is not None:
            item["id"] = existing["id"]
            warnings.append(
                f"type changes from {existing.get('responseType')} to {response_type}; its settings start over"
            )
        change = {col for col, value in cells.items() if value}
    item["name"] = cells["item_name"]

    if "item_prompt" in change:
        item["question"] = _set_prompt(item.get("question"), cells["item_prompt"])
    if response_type in SELECT_TYPES and change & {"value_labels", "scores"}:
        _apply_select(item, cells, change)
    elif response_type in ("slider", "numberSelect") and change & {
        "min_value",
        "max_value",
        "min_label",
        "max_label",
        "scores",
    }:
        _apply_slider(item, cells, change)
    elif response_type == "sliderRows" and change & {"rows", "scores"}:
        _apply_slider_rows(item, cells, change)
    elif response_type in GRID_TYPES and change & {"rows", "value_labels", "scores"}:
        _apply_grid(item, cells, change)
    elif response_type not in CREATABLE_TYPES and change & {"value_labels", "scores", "rows", "min_value", "max_value"}:
        warnings.append(f"options of {response_type} items can't be edited from a dictionary; those cells were ignored")

    config = item["config"]
    if "response_rules" in change:
        _apply_rules(item, cells, warnings)
    if "other_text_option" in change and "additionalResponseOption" in config:
        choice = cells["other_text_option"].lower()
        if choice not in ("", "optional", "required"):
            raise CellError("other_text_option must be empty, optional, or required")
        config["additionalResponseOption"] = {
            "textInputOption": bool(choice),
            "textInputRequired": choice == "required",
        }
    if "skippable" in change and "skippableItem" in config:
        choice = cells["skippable"].lower()
        if choice not in ("", "yes", "no"):
            raise CellError("skippable must be yes or no")
        if activity.get("isSkippable") and choice == "no":
            warnings.append("the whole activity is skippable, so this item stays skippable")
        elif choice and not activity.get("isSkippable"):
            config["skippableItem"] = choice == "yes"
    if "hidden" in change:
        choice = cells["hidden"].lower()
        if choice not in ("", "yes", "no"):
            raise CellError("hidden must be yes or no")
        item["isHidden"] = choice == "yes"
    return item, warnings


def resolve_logic(items: list[dict], rows: dict[int, ItemRow], old_items: list[dict], activity: dict) -> list[Problem]:
    """Set conditionalLogic from each row's shown_if, against the items before it."""
    problems: list[Problem] = []
    old_by_name = {item.get("name"): item for item in old_items}
    old_by_id = {item.get("id"): item for item in old_items if item.get("id")}
    for index, item in enumerate(items):
        row = rows.get(index)
        if row is None or "shown_if" not in row.cells:
            continue
        text = row.cells["shown_if"]
        old = old_by_id.get(item.get("id"))
        if old is not None and _same_text(text, describe_logic(old.get("conditionalLogic"), old_by_name)):
            item["conditionalLogic"] = copy.deepcopy(old.get("conditionalLogic"))
            continue
        try:
            item["conditionalLogic"] = parse_shown_if(text, {i.get("name"): i for i in items[:index]})
        except CellError as e:
            problems.append(Problem(activity.get("name", ""), item.get("name", ""), f"shown_if: {e}", where=row.where))
    return problems


# -- scores and subscales ---------------------------------------------------------------------------

_SUBSCALE_PROMPT = re.compile(
    r"^Subscale (?P<name>.+?): (?P<kind>Sum|Average) of (?P<items>.+?)(?P<percent> as a percentage of its maximum)?$",
    re.DOTALL,
)
_TOTAL_PROMPT = re.compile(
    r"^Total score: (?P<kind>Sum|Average) of ?(?P<items>.*?)(?P<percent> as a percentage of its maximum)?$", re.DOTALL
)
_LOOKUP_ROW = re.compile(
    r"^raw (?P<raw>\S+)(?: -> (?P<score>\S+))?(?: \((?P<quals>[^()]*)\))?(?:: (?P<text>.*))?$", re.DOTALL
)
_SEVERITIES = ("Minimal", "Mild", "Moderate", "Severe")


def _scoring(match: re.Match) -> str:
    if match["percent"]:
        return "percentage"
    return "sum" if match["kind"] == "Sum" else "average"


def parse_lookup_table(cell: str, *, total: bool = False) -> list[dict] | None:
    """'raw 0~2 -> 50 (sex M, age 8~12, Mild): Typical | …' -> lookup rows (None when empty)."""
    rows = []
    for part in _parts(cell):
        match = _LOOKUP_ROW.match(part)
        if not match:
            raise CellError(f"lookup row {part!r} should look like 'raw 0~4 -> 50 (sex F, age 8~12, Mild): text'")
        row: dict[str, Any] = {"rawScore": match["raw"]}
        if total:
            if match["score"] or match["quals"]:
                raise CellError(f"total score lookup row {part!r} can only have a raw score and a text")
        else:
            if not match["score"]:
                raise CellError(f"subscale lookup row {part!r} needs a score, like 'raw 0~4 -> 50'")
            row["score"] = match["score"]
        for qualifier in (q.strip() for q in (match["quals"] or "").split(",") if q.strip()):
            if qualifier.startswith("sex "):
                row["sex"] = qualifier[4:].strip()
            elif qualifier.startswith("age "):
                age = qualifier[4:].strip()
                row["age"] = int(age) if age.isdigit() else age
            elif qualifier in _SEVERITIES:
                row["severity"] = qualifier
            else:
                raise CellError(f"unknown lookup qualifier {qualifier!r} (use sex M/F, age N or N~M, or a severity)")
        if match["text"]:
            row["optionalText"] = match["text"]
        rows.append(row)
    return rows or None


def parse_subscale_row(cells: dict[str, str]) -> dict:
    """A 'subscale_score' row -> a Curious subscale."""
    match = _SUBSCALE_PROMPT.match(cells.get("item_prompt", ""))
    if not match:
        raise CellError("item_prompt should look like 'Subscale NAME: Sum of a, b, c' (or 'Average of …')")
    items = []
    for part in (p.strip() for p in match["items"].split(",")):
        if part.endswith(" (subscale)"):
            items.append({"name": part.removesuffix(" (subscale)").strip(), "type": "subscale"})
        elif part:
            items.append({"name": part, "type": "item"})
    return {
        "name": match["name"].strip(),
        "scoring": _scoring(match),
        "items": items,
        "subscaleTableData": parse_lookup_table(cells.get("value_labels", "")),
    }


@dataclass
class ScorePlan:
    """The subscale setting a file asks for, merged with the activity's current one."""

    subscales: list[dict]
    total: tuple[str, list[dict] | None] | None  # (scoring, lookup table), when the file has a total score row
    leftovers: list[str]  # current subscales the file doesn't list ("total score" for the total)
    changes: list[tuple[str, str, str]]  # (name, action, details)
    problems: list[Problem]


def plan_scores(rows: list[ItemRow], activity: dict) -> ScorePlan:
    """Read the score rows against the activity as it is now (`activity` with its current items).

    A subscale whose row matches what a download shows for it is kept exactly as it is.
    """
    setting = activity.get("subscaleSetting") or {}
    current = {s.get("name"): s for s in setting.get("subscales") or []}
    shown = {row["item_name"]: row for row in score_rows(activity)}
    plan = ScorePlan([], None, [], [], [])
    seen: set[str] = set()
    for row in rows:
        cells, kind = row.cells, row.cells.get("item_type")
        try:
            if kind == "total_score":
                match = _TOTAL_PROMPT.match(cells.get("item_prompt", ""))
                if not match:
                    raise CellError("item_prompt should look like 'Total score: Sum of …'")
                plan.total = (_scoring(match), parse_lookup_table(cells.get("value_labels", ""), total=True))
                before = shown.get(cells.get("item_name"))
                same = (
                    before is not None
                    and setting.get("calculateTotalScore") == plan.total[0]
                    and _same_text(before.get("value_labels", ""), cells.get("value_labels", ""))
                )
                if same:
                    plan.total = (setting["calculateTotalScore"], setting.get("totalScoresTableData"))
                plan.changes.append(("total score", "unchanged" if same else "changed" if before else "added", ""))
            elif kind == "subscale_score":
                subscale = parse_subscale_row(cells)
                name = subscale["name"]
                if name in seen:
                    raise CellError(f"subscale {name!r} is listed twice")
                seen.add(name)
                before = shown.get(cells.get("item_name"))
                old = current.get(name)
                same = (
                    old is not None
                    and before is not None
                    and all(_same_text(before.get(c, ""), cells.get(c, "")) for c in ("item_prompt", "value_labels"))
                )
                plan.subscales.append(copy.deepcopy(old) if same else subscale)
                action = "unchanged" if same else "changed" if old is not None else "added"
                plan.changes.append((f"subscale {name}", action, ""))
        except CellError as e:
            plan.problems.append(Problem(activity.get("name", ""), cells.get("item_name", ""), str(e), where=row.where))
    plan.leftovers = [f"subscale {name}" for name in current if name not in seen]
    if setting.get("calculateTotalScore") and plan.total is None:
        plan.leftovers.append("total score")
    return plan


def apply_scores(plan: ScorePlan, activity: dict, keep: bool) -> list[tuple[str, str, str]]:
    """Set the activity's subscaleSetting from the plan; returns the changes, including leftovers."""
    setting = activity.get("subscaleSetting") or {}
    current = {s.get("name"): s for s in setting.get("subscales") or []}
    subscales = list(plan.subscales)
    changes = list(plan.changes)
    for leftover in plan.leftovers:
        changes.append((leftover, "kept" if keep else "removed", "not in the dictionary"))
        if keep and leftover.startswith("subscale "):
            subscales.append(current[leftover.removeprefix("subscale ")])
    total = plan.total or ((setting.get("calculateTotalScore"), setting.get("totalScoresTableData")) if keep else None)
    if subscales or (total and total[0]):
        activity["subscaleSetting"] = {
            "calculateTotalScore": total[0] if total else None,
            "subscales": subscales,
            "totalScoresTableData": total[1] if total else None,
        }
    else:
        activity["subscaleSetting"] = None
    return changes


# -- checking an activity the way the backend will ---------------------------------------------------

_NAME = re.compile(r"^[\w-]+$")


def _check_item(item: dict) -> list[str]:
    errors = []
    response_type = item.get("responseType")
    values = item.get("responseValues") if isinstance(item.get("responseValues"), dict) else {}
    config = item.get("config") or {}
    if response_type in SELECT_TYPES:
        options = values.get("options") or []
        if not options:
            errors.append("has no options")
        if any(not (o.get("text") or "").strip() for o in options):
            errors.append("has an option without text")
        if response_type == "multiSelect" and sum(bool(o.get("isNoneAbove")) for o in options) > 1:
            errors.append("has more than one 'none of the above' option")
    if response_type == "slider":
        low, high = values.get("minValue"), values.get("maxValue")
        if not (isinstance(low, int) and isinstance(high, int) and 0 <= low < high <= 12 and low <= 11):
            errors.append(f"slider range {low}–{high} is not allowed (0 ≤ min < max ≤ 12)")
        for key in ("minLabel", "maxLabel"):
            if len(values.get(key) or "") > 100:
                errors.append(f"{key} is longer than 100 characters")
    if response_type == "numberSelect":
        low, high = values.get("minValue"), values.get("maxValue")
        if not (isinstance(low, int) and isinstance(high, int) and 0 <= low < high):
            errors.append(f"number range {low}–{high} is not allowed (0 ≤ min < max)")
    if response_type in GRID_TYPES:
        if not values.get("rows") or not values.get("options"):
            errors.append("needs rows and options")
        for text in [r.get("rowName") or "" for r in values.get("rows") or []] + [
            o.get("text") or "" for o in values.get("options") or []
        ]:
            if not text.strip() or len(text) > 100:
                errors.append(f"row or option {text!r} must have 1 to 100 characters")
    if response_type == "sliderRows":
        for row in values.get("rows") or []:
            if not (0 <= row.get("minValue", -1) < row.get("maxValue", -1) <= 12):
                errors.append(f"slider row {row.get('label')!r} range is not allowed (0 ≤ min < max ≤ 12)")
            if not (row.get("label") or "").strip() or len(row.get("label") or "") > 100:
                errors.append("slider row labels must have 1 to 100 characters")
    if response_type in ("text", "paragraphText") and not (config.get("maxResponseLength") or 0) >= 1:
        errors.append("maximum characters must be at least 1")
    if response_type == "text" and config.get("correctAnswerRequired") and not config.get("correctAnswer"):
        errors.append("needs a correct answer")
    if item.get("isHidden") and item.get("conditionalLogic"):
        errors.append("can't be hidden and have skip logic at the same time")
    return errors


def check_activity(activity: dict) -> list[Problem]:
    """What the backend (or the admin Builder) would reject in this activity, plus a few warnings."""
    name = activity.get("name") or ""
    problems: list[Problem] = []
    items = activity.get("items") or []

    def add(item: str, message: str, severity: str = "error") -> None:
        problems.append(Problem(name, item, message, severity))

    if not items:
        add("", "has no items")
    seen: set[str] = set()
    for index, item in enumerate(items):
        item_name = item.get("name") or ""
        if not _NAME.match(item_name):
            add(item_name, "item names may only use letters, digits, _ and -")
        if item_name in seen:
            add(item_name, "two items have this name")
        seen.add(item_name)
        for message in _check_item(item):
            add(item_name, message)
        earlier = {i.get("name"): i for i in items[:index]}
        for condition in (item.get("conditionalLogic") or {}).get("conditions") or []:
            target = earlier.get(condition.get("itemName"))
            if target is None:
                add(item_name, f"skip logic refers to {condition.get('itemName')!r}, which is not an earlier item")
            elif target.get("responseType") in SELECT_TYPES:
                values = {to_string(o.get("value")) for o in (target.get("responseValues") or {}).get("options") or []}
                if to_string((condition.get("payload") or {}).get("optionValue")) not in values:
                    add(item_name, f"skip logic uses an option {target['name']} no longer has")
        for variable in re.findall(r"\[\[(.*?)]]", display_text(item.get("question"))):
            if variable not in earlier:
                add(item_name, f"the prompt shows [[{variable}]], which is not an earlier item", "warning")

    by_name = {item.get("name"): item for item in items}

    def scored(item_name: str) -> bool:
        item = by_name.get(item_name)
        return bool(item and item.get("responseType") in _SCORED_TYPES and (item.get("config") or {}).get("addScores"))

    setting = activity.get("subscaleSetting") or {}
    subscale_names = {s.get("name") for s in setting.get("subscales") or []}
    for subscale in setting.get("subscales") or []:
        for part in subscale.get("items") or []:
            if part.get("type") == "subscale":
                if part.get("name") not in subscale_names:
                    add("", f"subscale {subscale.get('name')!r} uses a subscale that doesn't exist")
            elif not scored(part.get("name")):
                add(
                    "",
                    f"subscale {subscale.get('name')!r} uses {part.get('name')!r}, which is gone or no longer scored",
                )
    for report in (activity.get("scoresAndReports") or {}).get("reports") or []:
        if report.get("scoringType") == "score" and report.get("subscaleName") not in subscale_names:
            add("", f"score report {report.get('name')!r} uses subscale {report.get('subscaleName')!r}, which is gone")
        for item_name in report.get("itemsScore") or []:
            if not scored(item_name):
                add("", f"score report {report.get('name')!r} uses {item_name!r}, which is gone or no longer scored")
        for item_name in report.get("itemsPrint") or []:
            if item_name not in by_name:
                add("", f"report {report.get('name')!r} prints {item_name!r}, which is gone")
    included = activity.get("reportIncludedItemName")
    if included and included not in by_name:
        add("", f"the report setting 'include item' uses {included!r}, which is gone")
    return problems


def response_options_cell(item: dict) -> str:
    """The item_response_options cell (as in responses.csv) for a built item."""
    return to_string(coalesce(parse_options(item.get("responseValues"), item.get("responseType")), ""))
