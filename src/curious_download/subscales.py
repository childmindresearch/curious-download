"""Subscale and total scores, ported from mindlogger-admin/src/shared/utils/exportData/getSubscales.ts.

Scores are added to the first row of each submission, as the admin export does.
Column names follow the renamed export: activity_score, activity_score_lookup_text,
subscale_name_<name>, subscale_lookup_text_<name>.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .jscompat import UNDEFINED, coalesce, get, is_number, to_number, to_string, truthy

FINAL_SUBSCALE_KEY = "finalSubScale"
FINAL_SCORE_COLUMN = "activity_score"
FINAL_TEXT_COLUMN = "activity_score_lookup_text"
AGE_ITEM = "age_screen"
GENDER_ITEM = "gender_screen"
INTERVAL_SYMBOL = "~"
_TOTAL_SCORE_TYPES = ("singleSelect", "multiSelect", "slider")


def _js_round(value: float) -> float:
    """Math.round: nearest integer, ties toward +infinity."""
    if math.isnan(value) or math.isinf(value):
        return value
    floor = math.floor(value)
    return floor + 1 if value - floor >= 0.5 else floor


def round_to_2(value: Any) -> Any:
    if not is_number(value) or math.isnan(value):
        return value
    return _js_round((value + 2.220446049250313e-16) * 100) / 100


def _add(acc: Any, value: Any) -> Any:
    """JS `(acc ?? 0) + value` for numeric operands (undefined gives NaN)."""
    left = coalesce(acc, 0)
    if value is UNDEFINED:
        return math.nan
    return left + (0 if value is None else value)


def _js_max(values: list) -> float:
    if not values:
        return -math.inf
    numbers = [0 if v is None else to_number(v) for v in values]
    return math.nan if any(math.isnan(n) for n in numbers) else max(numbers)


def subscale_score(total: Any, scoring: Any, length: int, max_score: float) -> Any:
    if total is None:
        return None
    if scoring == "sum":
        return total
    if scoring == "average":
        return 0 if length == 0 else round_to_2(total / length)
    if scoring == "percentage":
        return 0 if max_score == 0 else round_to_2(total * 100 / max_score)
    return 0


def _is_system_item(item: dict) -> bool:
    return not truthy(item.get("allowEdit")) and item.get("name") in (AGE_ITEM, GENDER_ITEM)


def _matches_lookup_row(row: dict, score: float, activity_items: dict) -> bool:
    sex = row.get("sex")
    gender_answer = get(activity_items.get(GENDER_ITEM, UNDEFINED), "answer")
    with_sex = not truthy(sex) or ("0" if sex == "M" else "1") == to_string(get(gender_answer, "value"))

    age_answer = get(activity_items.get(AGE_ITEM, UNDEFINED), "answer")
    reported_age: Any = UNDEFINED
    if truthy(age_answer):
        if isinstance(age_answer, str):
            reported_age = age_answer
        elif isinstance(age_answer, dict) and "value" in age_answer:
            value = age_answer["value"]
            if is_number(value) or isinstance(value, str):
                reported_age = to_string(value)

    age = row.get("age")
    with_age = True
    if truthy(age):
        if isinstance(age, str) and INTERVAL_SYMBOL in age:
            parts = re.sub(r"\s", "", age).split(INTERVAL_SYMBOL)
            reported = to_number(reported_age)
            with_age = to_number(parts[0]) <= reported <= to_number(parts[1] if len(parts) > 1 else UNDEFINED)
        else:
            with_age = to_string(age) == reported_age

    if not with_sex or not with_age:
        return False

    raw_score = to_string(row.get("rawScore"))
    if INTERVAL_SYMBOL not in raw_score:
        return raw_score == to_string(score)
    parts = re.sub(r"\s", "", raw_score).split(INTERVAL_SYMBOL)
    return to_number(parts[0]) <= score <= to_number(parts[1] if len(parts) > 1 else UNDEFINED)


def _item_value(answer: Any, response_values: dict, counts: dict) -> Any:
    """Score of one item's answer, or None when unanswered or unscored."""
    options = response_values.get("options") if "options" in response_values else None
    if isinstance(options, list) and options:
        scores_by_value: dict[str, Any] = {}
        for option in options:
            value, score = get(option, "value"), get(option, "score")
            if value is not UNDEFINED and score is not UNDEFINED:
                scores_by_value[to_string(value)] = score
        if response_values.get("type") == "singleSelect":
            counts["max_score"] += _js_max(list(scores_by_value.values()))
        else:
            for score in scores_by_value.values():
                counts["max_score"] = counts["max_score"] + (0 if score is None else score)

        answer_value = get(answer, "value")
        if isinstance(answer_value, list):
            result: Any = None
            for value in answer_value:
                score = scores_by_value.get(to_string(value), UNDEFINED)
                if score is None:
                    continue
                result = _add(result, score)
            return result
        if not truthy(answer):
            return None
        return coalesce(scores_by_value.get(to_string(answer_value), UNDEFINED), None)

    scores = response_values.get("scores") if "scores" in response_values else None
    if isinstance(scores, list) and scores:
        minimum = to_number(response_values.get("minValue", UNDEFINED))
        maximum = to_number(response_values.get("maxValue", UNDEFINED))
        counts["max_score"] += _js_max(scores)
        answer_value = get(answer, "value")
        if is_number(minimum) and is_number(maximum) and not (math.isnan(minimum) or math.isnan(maximum)):
            options_range = [i + minimum for i in range(max(int(maximum - minimum + 1), 0))]
        else:
            options_range = []
        index = next(
            (i for i, option in enumerate(options_range) if is_number(answer_value) and option == answer_value), -1
        )
        return coalesce(get(scores, index), None)
    return None


def calc_scores(
    subscale: dict,
    activity_items: dict,
    subscales_by_name: dict,
    null_when_skipped: bool,
    result: dict | None = None,
) -> dict:
    """calcScores: {subscale name: {score, optionText, severity}} for this subscale (and nested ones)."""
    result = {} if result is None else result
    counts = {"max_score": 0, "item_count": 0}
    total: Any = None if null_when_skipped else 0

    for item in subscale.get("items") or []:
        if not truthy(item.get("type")):
            continue
        if item["type"] == "subscale":
            counts["item_count"] += 1
            nested_subscale = subscales_by_name.get(item["name"])
            if nested_subscale is None:
                continue
            nested = calc_scores(nested_subscale, activity_items, subscales_by_name, null_when_skipped, result).get(
                item["name"]
            )
            if nested is not None and is_number(nested["score"]):
                result[item["name"]] = nested
                total = _add(total, nested["score"])
            continue

        record = activity_items.get(item.get("name"))
        if _is_system_item(item) or (record is not None and truthy(record["activityItem"].get("isHidden"))):
            continue

        answer = record["answer"] if record is not None else UNDEFINED
        response_values = record["activityItem"].get("responseValues") if record is not None else None
        value = _item_value(answer, response_values, counts) if isinstance(response_values, dict) else None

        if value is None:
            if null_when_skipped:
                continue
            value = 0
        counts["item_count"] += 1
        total = _add(total, value)

    score = subscale_score(total, subscale.get("scoring"), counts["item_count"], counts["max_score"])
    name = subscale.get("name")

    table = subscale.get("subscaleTableData")
    if score is not None and truthy(table):
        found = next((row for row in table if _matches_lookup_row(row, score, activity_items)), None)
        lookup_score = to_number(found.get("score", UNDEFINED)) if found else math.nan
        return {
            **result,
            name: {
                "score": lookup_score if truthy(lookup_score) else round_to_2(score),
                "optionText": (found.get("optionalText") if found and truthy(found.get("optionalText")) else ""),
                "severity": (found.get("severity") if found and truthy(found.get("severity")) else None),
            },
        }

    if is_number(score):
        return {**result, name: {"score": round_to_2(score), "optionText": "", "severity": None}}
    return {**result}


def _total_score(setting: dict, activity_items: dict, null_when_skipped: bool) -> dict:
    if not truthy(setting.get("calculateTotalScore")):
        return {}
    items = [
        {"name": name, "type": "item", "allowEdit": record["activityItem"].get("allowEdit")}
        for name, record in activity_items.items()
        if record["activityItem"].get("responseType") in _TOTAL_SCORE_TYPES
    ]
    subscale = {
        "name": FINAL_SUBSCALE_KEY,
        "items": items,
        "scoring": setting["calculateTotalScore"],
        "subscaleTableData": setting.get("totalScoresTableData"),
    }
    return calc_scores(subscale, activity_items, {}, null_when_skipped)


def _clean_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]", "_", name)


def subscale_columns(setting: Any, activity_items: dict, *, null_when_skipped: bool = False) -> dict[str, Any]:
    """getSubscales: extra columns for the first row of a submission ({} when the activity has no subscales)."""
    subscales = get(setting, "subscales")
    if not isinstance(subscales, list) or not subscales or not activity_items:
        return {}
    subscales_by_name = {subscale.get("name"): subscale for subscale in subscales}

    parsed: dict[str, Any] = {}
    for subscale in subscales:
        calculated = calc_scores(subscale, activity_items, subscales_by_name, null_when_skipped).get(
            subscale.get("name")
        )
        if not calculated:
            continue
        cleaned = _clean_name(to_string(subscale.get("name")))
        parsed[f"subscale_name_{cleaned}"] = calculated["score"]
        if truthy(calculated["optionText"]):
            parsed[f"subscale_lookup_text_{cleaned}"] = calculated["optionText"]

    columns: dict[str, Any] = {}
    if truthy(setting.get("calculateTotalScore")):
        total = _total_score(setting, activity_items, null_when_skipped).get(FINAL_SUBSCALE_KEY)
        if total is not None and is_number(total["score"]):
            columns[FINAL_SCORE_COLUMN] = total["score"]
            columns[FINAL_TEXT_COLUMN] = total["optionText"]
    columns.update(parsed)
    return columns
