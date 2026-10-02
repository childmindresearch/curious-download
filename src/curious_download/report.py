"""Build `responses.csv` rows exactly like the admin panel's "Export data".

Each function ports one admin helper from mindlogger-admin/src/shared/utils/exportData
(file names are noted in the docstrings). A "record" is a decrypted answer for
one item: the export answer fields plus `activityItem`, `answer`, `items`,
`activityName` and `subscaleSetting`, the same shape the admin code works on.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .jscompat import (
    UNDEFINED,
    coalesce,
    get,
    is_number,
    join,
    join_with_comma,
    nullish,
    replace_all,
    to_number,
    to_string,
    truthy,
)

NULL_ANSWER = "value: null"
NOT_SCHEDULED = "not scheduled"

MEDIA_TYPES = ("photo", "video", "audio")
ROW_TYPES = ("singleSelectRows", "multiSelectRows", "sliderRows")

# Column order of the admin's renamed export (getReportCSVObject.ts, enableDataExportRenaming).
REPORT_COLUMNS = [
    "target_id",
    "target_secret_id",
    "target_nickname",
    "target_tag",
    "source_id",
    "source_secret_id",
    "source_nickname",
    "source_tag",
    "source_relation",
    "input_id",
    "input_secret_id",
    "input_nickname",
    "userId",
    "secret_user_id",
    "legacy_user_id",
    "applet_version",
    "activity_flow_id",
    "activity_flow_name",
    "activity_flow_submission_id",
    "activity_id",
    "activity_name",
    "activity_submission_id",
    "activity_start_time",
    "activity_end_time",
    "activity_schedule_id",
    "activity_schedule_history_id",
    "activity_schedule_start_time",
    "utc_timezone_offset",
    "activity_submission_review_id",
    "item_id",
    "item_name",
    "item_prompt",
    "item_response_options",
    "item_response",
    "item_response_status",
    "item_type",
    "rawScore",
]


class _JsTypeError(Exception):
    """Where the admin code would throw a TypeError (and usually catch it)."""


def _prop(obj: Any, key: Any) -> Any:
    """JS `obj.key` without optional chaining: throws on null/undefined."""
    if nullish(obj):
        raise _JsTypeError(key)
    return get(obj, key)


def _plus_one(value: Any) -> Any:
    """JS `value + 1` for the values a date month can hold."""
    if is_number(value):
        return value + 1
    if isinstance(value, str):
        return value + "1"
    if value is None:
        return 1
    return math.nan


def date_stamp_to_ms(value: Any) -> str:
    """convertDateStampToMs.ts: `${+date * 1000}` (the API sends epoch seconds)."""
    return to_string(to_number(value) * 1000)


# -- file names (getReportName.ts) ------------------------------------------


def file_extension(file_url: str) -> str:
    extension = file_url.split("/")[-1].split(".")[-1].split("?")[0]
    return "MOV" if extension == "quicktime" else extension


def media_file_name(record: dict, extension: str) -> str:
    return (
        f"{to_string(record.get('targetSecretId'))}-{to_string(record.get('id'))}-"
        f"{to_string(record['activityItem'].get('name'))}.{extension}"
    )


def abtrails_csv_name(index: int, record: dict) -> str:
    record_id = record.get("id") if truthy(record.get("id")) else ""
    return f"{to_string(record.get('targetSecretId'))}-{to_string(record_id)}-trail{index + 1}.csv"


def stability_tracker_csv_name(record: dict, phase_type: Any) -> str:
    return f"{to_string(record.get('targetSecretId'))}_{to_string(record.get('id'))}_{to_string(phase_type)}.csv"


def flanker_csv_name(record: dict) -> str:
    return media_file_name(record, "csv")


def unity_media_urls(record: dict) -> list[str]:
    """getUrls.ts getUnityMediaUrls."""
    answer = record.get("answer")
    value = get(answer, "value")
    if not truthy(answer) or not truthy(value):
        return []
    if isinstance(value, list):
        return [url for url in value if isinstance(url, str)]
    if isinstance(value, str):
        return []
    task_data = get(value, "taskData")
    if isinstance(value, dict) and "taskData" in value and truthy(get(task_data, "length")):
        return task_data
    return []


# -- response value (parseResponseValue.ts, getAnswerValue.ts) --------------


def _parse_value(value: Any) -> Any:
    if is_number(value) and value == 0:
        return "0"
    if (isinstance(value, list) and not value) or not truthy(value):
        return "null"
    return value


def answer_value(answer: Any) -> Any:
    """getAnswerValue: the `value` of an answer, with JS fallbacks."""
    if answer is None or isinstance(answer, (dict, list)):
        if truthy(get(answer, "phaseType")):
            return answer
        return _parse_value(coalesce(get(answer, "value"), answer))
    return _parse_value(answer)


def _time_range_part(data: Any, has_fallback: bool = False) -> str:
    hour = get(data, "hour")
    minute = get(data, "minute")
    if has_fallback:
        hour, minute = coalesce(hour, 0), coalesce(minute, 0)
    return f"hr {to_string(hour)}, min {to_string(minute)}"


def _rows(record: dict) -> list:
    rows = get(record["activityItem"].get("responseValues"), "rows")
    return rows if isinstance(rows, list) else []


def parse_response_value_raw(record: dict, index: int, answer: Any) -> Any:
    if isinstance(answer, str):
        return answer
    if answer is None or (isinstance(answer, (dict, list)) and len(answer) == 0):
        return NULL_ANSWER

    activity_item = record["activityItem"]
    input_type = activity_item.get("responseType")
    if input_type == "requestHealthRecordData":
        return coalesce(record.get("ehrDataFile", UNDEFINED), "")

    if isinstance(answer, dict):
        keys = list(answer.keys())
    elif isinstance(answer, list):
        keys = [str(i) for i in range(len(answer))]
    else:
        keys = []
    key = keys[0] if keys else UNDEFINED
    value = answer_value(answer)

    if not truthy(key) or (key == "text" and len(keys) < 2):
        return ""

    if input_type in MEDIA_TYPES:
        media_value = get(record.get("answer"), "value")
        if not truthy(media_value):
            return ""
        return media_file_name(record, file_extension(media_value if isinstance(media_value, str) else ""))

    if input_type == "timeRange":
        prefix = "time_range: "
        start, end = get(value, "from"), get(value, "to")
        if start is None and end is None:
            return f"{prefix}from (empty) / to (empty)"
        if start is None:
            return f"{prefix}from (empty) / to ({_time_range_part(end)})"
        if end is None:
            return f"{prefix}from ({_time_range_part(start)}) / to (empty)"
        return f"{prefix}from ({_time_range_part(start)}) / to ({_time_range_part(end, True)})"
    if input_type == "date":
        day, month, year = get(value, "day"), get(value, "month"), get(value, "year")
        if truthy(record.get("migratedDate")):
            month = _plus_one(month)
        return f"date: {to_string(day)}/{to_string(month)}/{to_string(year)}"
    if input_type == "time":
        hours = coalesce(get(value, "hours"), get(answer, "hour"))
        minutes = coalesce(get(value, "minutes"), get(answer, "minute"))
        return f"time: hr {to_string(hours)}, min {to_string(minutes)}"
    if input_type == "geolocation":
        return f"geo: lat ({to_string(get(value, 'latitude'))}) / long ({to_string(get(value, 'longitude'))})"
    if input_type == "drawing":
        return media_file_name(record, "svg")
    if input_type == "ABTrails":
        return abtrails_csv_name(index, record)
    if input_type == "singleSelectRows":
        return "\n".join(
            f"{to_string(get(row, 'rowName'))}: {to_string(coalesce(get(value, i), ''))}"
            for i, row in enumerate(_rows(record))
        )
    if input_type == "multiSelectRows":
        lines = []
        for i, row in enumerate(_rows(record)):
            selected = "" if value == "null" else get(value, i)
            text = join(selected, ", ") if isinstance(selected, list) else to_string(coalesce(selected, ""))
            lines.append(f"{to_string(get(row, 'rowName'))}: {text}")
        return "\n".join(lines)
    if input_type == "sliderRows":
        return "\n".join(
            f"{to_string(get(row, 'label'))}: {to_string(coalesce(get(value, i), ''))}"
            for i, row in enumerate(_rows(record))
        )
    if input_type == "stabilityTracker":
        return stability_tracker_csv_name(record, get(value, "phaseType"))
    if input_type == "flanker":
        return flanker_csv_name(record)
    if input_type == "unity":
        folder = to_string(record.get("id"))
        names = [url.split("?")[0].split("/")[-1] for url in unity_media_urls(record)]
        paths = [f"{folder}/{name}" for name in names if name]
        return ", ".join(paths) if paths else folder

    # singleSelect, multiSelect, slider, numberSelect, audioPlayer, and anything unknown.
    corrected_key = "value" if key == "text" and isinstance(answer, dict) and "value" in answer else key
    rendered = join_with_comma(value) if isinstance(value, list) else to_string(value)
    return f"{to_string(corrected_key)}: {rendered}"


def parse_response_value(record: dict, index: int) -> str:
    """parseResponseValue: the `item_response` cell."""
    answer = record.get("answer")
    edited = get(answer, "edited") if isinstance(answer, dict) else UNDEFINED
    edited_label = f" | edited: {to_string(edited)}" if truthy(edited) else ""
    response_value = parse_response_value_raw(record, index, answer)

    text = get(answer, "text") if isinstance(answer, dict) else UNDEFINED
    if isinstance(answer, dict) and truthy(get(text, "length")):
        prefix = f"{to_string(response_value)} | " if response_value != "" else ""
        return f"{prefix}text: {to_string(text)}{edited_label}"
    return f"{to_string(response_value)}{edited_label}"


# -- options, scores, status ------------------------------------------------


def _range_min_max(minimum: Any, maximum: Any) -> list:
    """createArrayFromMinToMax with JS Array.from length coercion."""
    if not is_number(minimum) or not is_number(maximum) or math.isnan(minimum) or math.isnan(maximum):
        return []
    length = maximum - minimum + 1
    if math.isinf(length) or length <= 0:
        return []
    return [i + minimum for i in range(int(length))]


def parse_options(response_values: Any, response_type: Any) -> Any:
    """parseOptions.ts: the `item_response_options` cell (before variable replacement)."""
    if response_type in ROW_TYPES:
        return ""
    if response_type == "slider":
        minimum = to_number(get(response_values, "minValue"))
        maximum = to_number(get(response_values, "maxValue"))
        scores = get(response_values, "scores")
        has_scores = truthy(get(scores, "length"))
        return join_with_comma(
            [
                f"{to_string(option)}: {to_string(option)}"
                + (f" (score: {to_string(get(scores, i))})" if has_scores else "")
                for i, option in enumerate(_range_min_max(minimum, maximum))
            ]
        )
    if response_type == "numberSelect":
        minimum = to_number(get(response_values, "minValue"))
        maximum = to_number(get(response_values, "maxValue"))
        return f"Min: {to_string(minimum)}, Max: {to_string(maximum)}"

    options = get(response_values, "options")
    if not truthy(get(options, "length")):
        return UNDEFINED
    parts = []
    for option in options:
        value = to_string(coalesce(get(option, "value"), ""))
        score = get(option, "score")
        parts.append(
            to_string(get(option, "text"))
            + (f": {value}" if value else "")
            + (f" (score: {to_string(score)})" if is_number(score) else "")
        )
    return join_with_comma(parts)


def raw_scores(response_values: Any) -> Any:
    """getRawScores.ts: the sum of all scores defined on the item (not the respondent's score)."""
    scores = get(response_values, "scores")
    if truthy(get(scores, "length")):
        total: Any = 0
        for score in scores:
            total = total + (score if truthy(score) else 0)
        return total
    options = get(response_values, "options")
    if isinstance(options, list):
        total = 0
        for option in options:
            score = get(option, "score")
            total = total + (score if truthy(score) else 0)
        return total
    return UNDEFINED


def response_status(record: dict) -> str:
    """getFlag.ts."""
    if truthy(record.get("scheduledDatetime")) and not truthy(record.get("startDatetime")):
        return "missed"
    if record["activityItem"].get("responseType") == "ABTrails":
        value = get(record.get("answer"), "value")
        maximum = get(value, "maximumIndex")
        if not truthy(value) or (truthy(maximum) and get(value, "currentIndex") != maximum):
            return "incomplete"
    return "completed"


# -- [[variable]] replacement (replaceItemVariableWithName.ts) --------------

_DOUBLE_BRACKETS = re.compile(r"\[\[(.*?)]]")
_ESCAPE_DOLLAR_AMP = re.compile(r"(?=[$&])")


def _pad2(value: Any) -> str:
    return to_string(value).rjust(2, "0")


def _method_to_string(value: Any) -> str:
    """JS `value.toString()`: throws on null/undefined."""
    if nullish(value):
        raise _JsTypeError("toString")
    return to_string(value)


def _time_string(obj: Any) -> str:
    if not truthy(obj):
        return ""
    hour = _method_to_string(_prop(obj, "hour"))
    minute = _method_to_string(_prop(obj, "minute"))
    return f"{_pad2(hour)}:{_pad2(minute)}"


def _date_string(obj: Any) -> str:
    if not truthy(obj):
        return ""
    month = to_number(_prop(obj, "month"))
    day = to_number(_prop(obj, "day"))
    return f"{to_string(_prop(obj, 'year'))}-{_pad2(month)}-{_pad2(day)}"


def _find_option(item_value: Any, wanted: Any) -> Any:
    options = _prop(_prop(item_value, "responseValues"), "options")
    if not isinstance(options, list):
        raise _JsTypeError("options")
    for option in options:
        if to_string(get(option, "value")) == to_string(wanted):
            return option
    return UNDEFINED


def replace_item_variables(markdown: str, items: list, raw_answers: dict) -> str:
    """Replace [[itemName]] in prompts and options with the respondent's answer."""
    names = _DOUBLE_BRACKETS.findall(markdown)
    if not names:
        return markdown
    items_by_name = {item.get("name"): item for item in items or []}
    try:
        for name in names:
            pattern = re.compile(r"\[\[" + name + r"\]\]", re.IGNORECASE)
            item_value = items_by_name.get(name, UNDEFINED)
            raw_answer = get(raw_answers.get(name, UNDEFINED), "answer")

            if truthy(raw_answer) and isinstance(get(raw_answer, "value"), list):
                options = _prop(_prop(item_value, "responseValues"), "options")
                labels = []
                for value in raw_answer["value"]:
                    option = None
                    if isinstance(options, list):
                        option = next((o for o in options if to_string(get(o, "value")) == to_string(value)), None)
                    if option is not None:
                        labels.append(get(option, "text"))
                markdown = replace_all(markdown, pattern, f"{join(labels, ', ')} ")
            elif truthy(raw_answer) and isinstance(raw_answer, (dict, list)):
                response_type = _prop(item_value, "responseType")
                value = get(raw_answer, "value")
                if response_type == "singleSelect":
                    option = _find_option(item_value, value)
                    if option is not UNDEFINED:
                        markdown = replace_all(markdown, pattern, f"{to_string(get(option, 'text'))} ")
                elif response_type in ("slider", "numberSelect"):
                    markdown = replace_all(markdown, pattern, f"{to_string(value)} ")
                elif response_type == "timeRange":
                    start = _time_string(_prop(value, "from"))
                    end = _time_string(_prop(value, "to"))
                    markdown = replace_all(markdown, pattern, f"{start} - {end} ")
                elif response_type == "date":
                    markdown = replace_all(markdown, pattern, f"{_date_string(value)} ")
            elif truthy(raw_answer):
                escaped = _ESCAPE_DOLLAR_AMP.sub(lambda _: "\\", to_string(raw_answer))
                markdown = replace_all(markdown, pattern, escaped)

            markdown = replace_all(markdown, pattern, " ")
    except (_JsTypeError, re.error):
        pass
    return markdown


def dictionary_text(value: Any, language: str = "en") -> str:
    """getDictionaryText: the export already sends translated strings."""
    if not truthy(value):
        return ""
    if isinstance(value, dict):
        return to_string(coalesce(value.get(language, UNDEFINED), ""))
    return to_string(value)


# -- the row ----------------------------------------------------------------


def _or_empty(value: Any) -> Any:
    return coalesce(value, "")


def report_row(record: dict, raw_answers: dict, index: int) -> dict[str, Any]:
    """getReportCSVObject.ts (renamed columns). Values are sanitized when written."""
    activity_item = record["activityItem"]
    response_values = activity_item.get("responseValues")
    flow_id = record.get("flowId")
    reviewed_flow_submit_id = record.get("reviewedFlowSubmitId")
    scheduled = record.get("scheduledDatetime")
    raw_score = raw_scores(response_values)
    options = parse_options(response_values, activity_item.get("responseType"))

    return {
        "target_id": _or_empty(record.get("targetSubjectId")),
        "target_secret_id": _or_empty(record.get("targetSecretId")),
        "target_nickname": _or_empty(record.get("targetUserNickname")),
        "target_tag": _or_empty(record.get("targetUserTag")),
        "source_id": _or_empty(record.get("sourceSubjectId")),
        "source_secret_id": _or_empty(record.get("sourceSecretId")),
        "source_nickname": _or_empty(record.get("sourceUserNickname")),
        "source_tag": _or_empty(record.get("sourceUserTag")),
        "source_relation": _or_empty(record.get("relation")),
        "input_id": _or_empty(record.get("inputSubjectId")),
        "input_secret_id": _or_empty(record.get("inputSecretId")),
        "input_nickname": _or_empty(record.get("inputUserNickname")),
        "userId": _or_empty(record.get("respondentId")),
        "secret_user_id": _or_empty(record.get("respondentSecretId")),
        "legacy_user_id": _or_empty(record.get("legacyProfileId")),
        "applet_version": _or_empty(record.get("version")),
        "activity_flow_id": _or_empty(flow_id),
        "activity_flow_name": _or_empty(record.get("flowName")),
        "activity_flow_submission_id": record.get("submitId")
        if truthy(flow_id) and not truthy(reviewed_flow_submit_id)
        else "",
        "activity_id": record.get("activityId"),
        "activity_name": record.get("activityName"),
        "activity_submission_id": record.get("id"),
        "activity_start_time": date_stamp_to_ms(record.get("startDatetime")),
        "activity_end_time": date_stamp_to_ms(record.get("endDatetime")),
        "activity_schedule_id": _or_empty(record.get("scheduledEventId")),
        "activity_schedule_history_id": _or_empty(record.get("scheduledEventHistoryId")),
        "activity_schedule_start_time": date_stamp_to_ms(scheduled) if truthy(scheduled) else NOT_SCHEDULED,
        "utc_timezone_offset": _or_empty(record.get("tzOffset")),
        "activity_submission_review_id": coalesce(
            reviewed_flow_submit_id, coalesce(record.get("reviewedAnswerId"), "")
        ),
        "item_id": _or_empty(activity_item.get("id")),
        "item_name": activity_item.get("name"),
        "item_prompt": replace_item_variables(
            dictionary_text(activity_item.get("question")), record.get("items") or [], raw_answers
        ),
        "item_response_options": replace_item_variables(
            to_string(coalesce(options, "")), record.get("items") or [], raw_answers
        ),
        "item_response": parse_response_value(record, index),
        "item_response_status": response_status(record),
        "item_type": activity_item.get("responseType"),
        "rawScore": raw_score if truthy(raw_score) else "",
    }
