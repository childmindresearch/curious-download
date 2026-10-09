"""`activity_user_journey.csv`: one row per user action (next, back, undo, skip, answer, done).

Ported from the admin export: getActivityJourneyData in
mindlogger-admin/src/shared/utils/exportData/getReportAndMediaData.ts and getJourneyCSVObject.ts
(renamed columns). The actions come from each answer's encrypted `events`, recorded by the app as
{type, screen: "<activityId>/<itemId>", time (ms epoch), response (SET_ANSWER only)}.
"""

from __future__ import annotations

import re
from typing import Any

from .jscompat import UNDEFINED, coalesce, nullish, to_string, truthy
from .report import (
    NOT_SCHEDULED,
    date_stamp_to_ms,
    dictionary_text,
    parse_options,
    parse_response_value,
    replace_item_variables,
)

# Column order of the admin's renamed journey export (consts.tsx activityJourneyHeader).
JOURNEY_COLUMNS = [
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
    "user_id",
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
    "activity_schedule_start_time",
    "utc_timezone_offset",
    "item_id",
    "item_name",
    "item_prompt",
    "item_response_options",
    "item_response",
    "item_type",
    "press_next_time",
    "press_popup_skip_time",
    "press_popup_keep_time",
    "press_back_time",
    "press_undo_time",
    "press_skip_time",
    "press_done_time",
    "response_option_selection_time",
]

# Which action fills each time column.
_ACTION_COLUMNS = {
    "press_next_time": "NEXT",
    "press_popup_skip_time": "SKIP_POPUP_CONFIRM",
    "press_popup_keep_time": "SKIP_POPUP_CANCEL",
    "press_back_time": "PREV",
    "press_undo_time": "UNDO",
    "press_skip_time": "SKIP",
    "press_done_time": "DONE",
    "response_option_selection_time": "SET_ANSWER",
}

SPLASH_SCREEN_ITEM_NAME = "Splash Screen"
_MIGRATED_ID_SUFFIX_LENGTH = len("00000000")
_URL_SCREEN = re.compile(r"^https?://")


class _SkipRow(Exception):
    """The admin code throws here and drops the row."""


def is_success_event(event: Any) -> bool:
    """isSuccessEvent: placeholders from failed decryption have empty type, screen and time."""
    return (
        isinstance(event, dict)
        and event.get("type", UNDEFINED) != ""
        and event.get("screen", UNDEFINED) != ""
        and event.get("time", UNDEFINED) != ""
    )


def _id_before_migration(value: Any) -> str:
    """getIdBeforeMigration: 64c23b53-8819-c178-d236-685e00000000 -> 64c23b538819c178d236685e."""
    text = value if isinstance(value, str) else ""
    return text[:-_MIGRATED_ID_SUFFIX_LENGTH].replace("-", "")


def _screen_key(screen: Any, has_url_screen: bool) -> str:
    """getEventScreenWrapper; without URL screens the value is used as an object key as is."""
    if not has_url_screen:
        return screen if isinstance(screen, str) else to_string(screen)
    if not isinstance(screen, str):
        raise _SkipRow("screen")  # `screen.split` throws
    return screen.split("/")[-1]


def _answers_by_screen(records: list[dict], has_migrated: bool, has_url_screen: bool) -> dict[str, dict]:
    """getDecryptedAnswersObject: records keyed the way event screens refer to items."""
    by_screen = {}
    for record in records:
        item = record["activityItem"]
        if has_url_screen:
            key = to_string(item.get("name", UNDEFINED))
        elif has_migrated:
            key = f"{_id_before_migration(record.get('activityId'))}/{_id_before_migration(item.get('id'))}"
        else:
            key = f"{to_string(record.get('activityId', UNDEFINED))}/{to_string(item.get('id', UNDEFINED))}"
        by_screen[key] = record
    return by_screen


def _action_times(event: dict) -> dict[str, str]:
    time = event.get("time")
    if nullish(time):
        raise _SkipRow("time")  # `event.time.toString()` throws
    return {
        column: to_string(time) if event.get("type") == action else "" for column, action in _ACTION_COLUMNS.items()
    }


def _journey_row(merged: dict, times: dict[str, str], item: dict) -> dict[str, Any]:
    """getJourneyCSVReturn (renamed columns), without the item-specific cells."""
    scheduled = merged.get("scheduledDatetime")
    return {
        "target_id": merged.get("targetSubjectId"),
        "target_secret_id": merged.get("targetSecretId"),
        "target_nickname": merged.get("targetUserNickname"),
        "target_tag": merged.get("targetUserTag"),
        "source_id": merged.get("sourceSubjectId"),
        "source_secret_id": merged.get("sourceSecretId"),
        "source_nickname": merged.get("sourceUserNickname"),
        "source_tag": merged.get("sourceUserTag"),
        "source_relation": merged.get("relation"),
        "input_id": merged.get("inputSubjectId"),
        "input_secret_id": merged.get("inputSecretId"),
        "input_nickname": merged.get("inputUserNickname"),
        "user_id": merged.get("respondentId"),
        "secret_user_id": merged.get("respondentSecretId"),
        "legacy_user_id": merged.get("legacyProfileId"),
        "applet_version": merged.get("version"),
        "activity_flow_id": merged.get("flowId"),
        "activity_flow_name": merged.get("flowName"),
        "activity_flow_submission_id": merged.get("submitId") if truthy(merged.get("flowId")) else "",
        "activity_id": merged.get("activityId"),
        "activity_name": merged.get("activityName"),
        "activity_submission_id": merged.get("id"),
        "activity_start_time": date_stamp_to_ms(merged.get("startDatetime")),
        "activity_end_time": date_stamp_to_ms(merged.get("endDatetime")),
        **item,
        "activity_schedule_start_time": date_stamp_to_ms(scheduled) if truthy(scheduled) else NOT_SCHEDULED,
        **times,
    }


def _order(row: dict) -> dict:
    return {column: row.get(column) for column in JOURNEY_COLUMNS}


def splash_screen_row(event: dict, next_merged: dict) -> dict | None:
    """getSplashScreen: the first action happened on a screen before the first item."""
    activity_item = next_merged.get("activityItem")
    if not truthy(activity_item):
        return None
    item = {
        "activity_schedule_id": None,
        "utc_timezone_offset": None,
        "item_id": "",
        "item_name": SPLASH_SCREEN_ITEM_NAME,
        "item_prompt": "",
        "item_response_options": "",
        "item_response": "",
        "item_type": activity_item.get("responseType"),
    }
    return _order(_journey_row(next_merged, _action_times(event), item))


def journey_row(merged: dict, raw_answers: dict, index: int) -> dict | None:
    """getJourneyCSVObject: one action merged with its item's record."""
    activity_item = merged.get("activityItem")
    if not truthy(activity_item):
        return None
    times = _action_times(merged)
    items = merged.get("items") or []
    try:
        response = parse_response_value(merged, index, is_event=True)
        options = replace_item_variables(
            to_string(
                coalesce(parse_options(activity_item.get("responseValues"), activity_item.get("responseType")), "")
            ),
            items,
            raw_answers,
        )
    except Exception:  # noqa: BLE001 -- the admin catches parse errors and leaves both cells empty
        response, options = "", ""
    item = {
        "activity_schedule_id": merged.get("scheduledEventId"),
        "utc_timezone_offset": merged.get("tzOffset"),
        "item_id": activity_item.get("id"),
        "item_name": activity_item.get("name"),
        "item_prompt": replace_item_variables(dictionary_text(activity_item.get("question")), items, raw_answers),
        "item_response_options": options,
        "item_response": response,
        "item_type": activity_item.get("responseType"),
    }
    return _order(_journey_row(merged, times, item))


JOURNEY_FILE_NAME = "activity_user_journey.csv"


def journey_info(requested: bool, rows: list[dict], failures: list) -> dict:
    """The `journey` section of export_info.json."""
    return {
        "requested": requested,
        "rows": len(rows),
        "actions_decryption_failures": [vars(failure) for failure in failures],
    }


def journey_rows(records: list[dict], events: Any) -> list[dict]:
    """getActivityJourneyData for one submission (records after with_public_urls)."""
    if not isinstance(events, list):
        return []
    events = [event for event in events if is_success_event(event)]
    has_migrated = bool(records) and truthy(records[0].get("migratedDate"))
    first_screen = events[0].get("screen") if events else None
    has_url_screen = isinstance(first_screen, str) and bool(_URL_SCREEN.match(first_screen))
    by_screen = _answers_by_screen(records, has_migrated, has_url_screen)
    raw_answers = {record["activityItem"].get("name"): record for record in records}

    rows = []
    # Numbers AB Trails files in journey cells; it counts answer actions, not items.
    answer_counter = 0
    for index, event in enumerate(events):
        try:
            key = _screen_key(event.get("screen", UNDEFINED), has_url_screen)
            if index == 0 and key not in by_screen and index + 1 < len(events):
                following = events[index + 1]
                next_key = _screen_key(following.get("screen", UNDEFINED), has_url_screen)
                row = splash_screen_row(event, {**following, **by_screen.get(next_key, {})})
            else:
                row_index = answer_counter
                if event.get("type") == "SET_ANSWER":
                    answer_counter += 1
                row = journey_row({**event, **by_screen.get(key, {})}, raw_answers, row_index)
        except _SkipRow:
            row = None
        if row is not None:
            rows.append(row)
    return rows
