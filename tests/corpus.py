"""Synthetic export data covering every item type and the edge cases of the admin export.

Answer shapes follow the mobile app's DTOs (mindlogger-app-refactor/src/shared/api/services/IAnswerService.ts),
item definitions follow the export endpoint's `activities[].items[]`.
"""

from __future__ import annotations

import copy

APPLET_ID = "11111111-1111-4111-8111-111111111111"
ACTIVITY_ID = "22222222-2222-4222-8222-222222222222"
FLOW_ID = "33333333-3333-4333-8333-333333333333"
SUBJECT_ID = "44444444-4444-4444-8444-444444444444"
USER_ID = "55555555-5555-4555-8555-555555555555"


def _options(kind: str, scored: bool = True) -> dict:
    texts = ["never", "Sometimes $&", "always"]
    return {
        "type": kind,
        "paletteName": None,
        "options": [
            {"id": f"o{i}", "text": text, "value": i, "score": (i + 1 if scored else None), "isHidden": False}
            for i, text in enumerate(texts)
        ],
    }


def item(name: str, response_type: str, response_values=None, *, question=None, hidden=False, allow_edit=True):
    return {
        "id": f"item-{name}",
        "name": name,
        "question": question if question is not None else f"Question for {name}?",
        "responseType": response_type,
        "responseValues": response_values,
        "config": {},
        "order": 0,
        "isHidden": hidden,
        "allowEdit": allow_edit,
        "conditionalLogic": None,
    }


ROWS = [{"id": "r1", "rowName": "Row one"}, {"id": "r2", "rowName": "Row two"}, {"id": "r3", "rowName": "Row three"}]

ALL_TYPES_ITEMS = [
    item("mood", "singleSelect", _options("singleSelect"), question="Today [[name]] feels...\n=SUM(1)"),
    item("feelings", "multiSelect", _options("multiSelect")),
    item("energy", "slider", {"minValue": 0, "maxValue": 5, "scores": [0, 1, 2, 3, 4, 5], "minLabel": "low"}),
    item("energy_unscored", "slider", {"minValue": 1, "maxValue": 3, "scores": None}),
    item("count", "numberSelect", {"minValue": 0, "maxValue": 10}),
    item("name", "text", None, question="Name? You picked [[mood]] and [[feelings]]"),
    item("story", "paragraphText", None),
    item("birthday", "date", None),
    item("bedtime", "time", None),
    item("sleep", "timeRange", None, question="Slept [[sleep]] on [[birthday]] at [[energy]]"),
    item("where", "geolocation", None),
    item("selfie", "photo", None),
    item("clip", "video", None),
    item("voice", "audio", None),
    item("listen", "audioPlayer", None),
    item("sketch", "drawing", None),
    item("grid_single", "singleSelectRows", {"rows": ROWS, "options": [{"id": "x", "text": "Yes"}]}),
    item(
        "grid_multi", "multiSelectRows", {"rows": ROWS, "options": [{"id": "x", "text": "A"}, {"id": "y", "text": "B"}]}
    ),
    item("grid_slider", "sliderRows", {"rows": [{"id": "s1", "label": "Pain", "minValue": 0, "maxValue": 10}]}),
    item("trails", "ABTrails", None),
    item("balance", "stabilityTracker", None),
    item("flank", "flanker", None),
    item("game", "unity", None),
    item("ehr", "requestHealthRecordData", None),
    item("notice", "message", None),
    item("secret", "text", None, hidden=True),
]

ALL_TYPES_ANSWERS = [
    {"value": 1, "text": "extra note"},
    {"value": [2, 0]},
    {"value": 3},
    {"value": 2},
    {"value": "7"},
    "+1 (555) 123",
    "line one\n-line two\r\nline three",
    {"value": {"day": 5, "month": 9, "year": 2026}},
    {"value": {"hours": 22, "minutes": 5}},
    {"value": {"from": {"hour": 23, "minute": 0}, "to": {"hour": 7, "minute": 30}}},
    {"value": {"latitude": 40.7128, "longitude": -74.006}},
    {"value": "s3://bucket/mindlogger/answer/u/a/1749138592012/IMG_1.jpg"},
    {"value": "s3://bucket/mindlogger/answer/u/a/1749138592012/clip.quicktime"},
    {"value": "s3://bucket/mindlogger/answer/u/a/1749138592012/voice.m4a"},
    {"value": True},
    {"value": {"svgString": "<svg/>", "width": 300, "uri": "s3://b/x.svg", "lines": []}},
    {"value": ["Yes", None, "Yes"]},
    {"value": [["A", "B"], None, ["B"]]},
    {"value": [7]},
    {"value": {"width": 300, "startTime": 1, "updated": True, "currentIndex": 5, "maximumIndex": 8, "lines": []}},
    {"value": [{"timestamp": 1, "lambda": 0.1}], "maxLambda": 0.4, "phaseType": "focus-phase"},
    {"value": [{"trial_index": 1, "button_pressed": "0"}]},
    {"value": ["s3://bucket/unity/run_1.json?x=1", "s3://bucket/unity/run_2.json"]},
    {"value": "opt_in"},
    None,
    "hidden answer",
]


def activity(items, *, name="Daily check-in", version="2.2.4", subscale_setting=None) -> dict:
    return {
        "id": ACTIVITY_ID,
        "idVersion": f"{ACTIVITY_ID}_{version}",
        "version": version,
        "name": name,
        "description": "",
        "items": items,
        "subscaleSetting": subscale_setting,
    }


def answer_record(answer_id: str, **overrides) -> dict:
    record = {
        "id": answer_id,
        "submitId": f"submit-{answer_id}",
        "version": "2.2.4",
        "appletHistoryId": f"{APPLET_ID}_2.2.4",
        "appletId": APPLET_ID,
        "activityHistoryId": f"{ACTIVITY_ID}_2.2.4",
        "activityId": ACTIVITY_ID,
        "flowHistoryId": None,
        "flowId": None,
        "flowName": None,
        "reviewedAnswerId": None,
        "reviewedFlowSubmitId": None,
        "respondentId": USER_ID,
        "respondentSecretId": "P001",
        "sourceSubjectId": SUBJECT_ID,
        "sourceSecretId": "P001",
        "sourceUserNickname": "Pat",
        "sourceUserTag": "Child",
        "targetSubjectId": SUBJECT_ID,
        "targetSecretId": "P001",
        "targetUserNickname": "Pat",
        "targetUserTag": "Child",
        "inputSubjectId": SUBJECT_ID,
        "inputSecretId": "P001",
        "inputUserNickname": "Pat",
        "relation": "self",
        "legacyProfileId": None,
        "userPublicKey": "[1,2,3]",
        "answer": "encrypted",
        "itemIds": [],
        "events": "encrypted",
        "scheduledDatetime": None,
        "startDatetime": 1749138592.012,
        "endDatetime": 1749138602.936,
        "migratedDate": None,
        "tzOffset": -240,
        "scheduledEventId": "91d44b6a-ed7c-4d6c-9db7-2c5ddc5e96f2",
        "scheduledEventHistoryId": "91d44b6a-ed7c-4d6c-9db7-2c5ddc5e96f2_20250506-1",
        "createdAt": "2025-06-05T15:50:02.936000",
        "migratedData": None,
        "client": {"appId": "mindlogger-mobile", "appVersion": "2.0.0", "width": 390, "height": 844},
        "ehrDataFile": None,
    }
    record.update(overrides)
    return record


SUBSCALE_ITEMS = [
    item("age_screen", "text", None, allow_edit=False),
    item("gender_screen", "singleSelect", _options("singleSelect", scored=False), allow_edit=False),
    item("q1", "singleSelect", _options("singleSelect")),
    item("q2", "multiSelect", _options("multiSelect")),
    item("q3", "slider", {"minValue": 0, "maxValue": 4, "scores": [0, 2, 4, 6, 8]}),
    item("q4", "singleSelect", _options("singleSelect"), hidden=True),
]

SUBSCALE_SETTING = {
    "calculateTotalScore": "sum",
    "totalScoresTableData": [{"rawScore": "0 ~ 100", "optionalText": "Total band"}],
    "subscales": [
        {
            "name": "Sub A (core)",
            "scoring": "average",
            "items": [{"name": "q1", "type": "item"}, {"name": "q2", "type": "item"}],
            "subscaleTableData": [
                {"score": "10", "rawScore": "0~2", "age": "8~12", "sex": "M", "optionalText": "Low (boy)"},
                {"score": "0", "rawScore": "0~100", "optionalText": "Fallback"},
            ],
        },
        {
            "name": "Nested",
            "scoring": "sum",
            "items": [{"name": "Sub A (core)", "type": "subscale"}, {"name": "q3", "type": "item"}],
        },
        {
            "name": "Pct",
            "scoring": "percentage",
            "items": [{"name": "q1", "type": "item"}, {"name": "q3", "type": "item"}],
        },
        {"name": "Empty", "scoring": "average", "items": [{"name": "q4", "type": "item"}]},
        {
            # 8 items averaging to x.xx5 exercises Math.round's tie rule.
            "name": "Tie",
            "scoring": "average",
            "items": [{"name": name, "type": "item"} for name in ("q1", "q3", "q3", "q3", "q3", "q3", "q3", "q3")],
        },
    ],
}


def submissions() -> list[dict]:
    """(answer, activity, answersDecrypted) triples."""
    full = activity(ALL_TYPES_ITEMS)
    cases = [
        # Everything answered.
        (answer_record("a1"), full, ALL_TYPES_ANSWERS),
        # Skips, nulls, quirky shapes, and answers shorter than the item list.
        (
            answer_record("a2", scheduledDatetime=1749130000.5, startDatetime=None, tzOffset=330),
            full,
            [
                {"text": "only text"},
                {"value": []},
                {"value": 0},
                {"value": None, "text": "why"},
                {},
                "",
                "=cmd|' /C calc'!A0",
                {"value": {"day": 1, "month": 0, "year": 2020}},
                {"hour": 9, "minute": 15},
                {"value": {"from": None, "to": {"hour": 5, "minute": 0}}},
                {"value": None},
                {"value": None},
                {"value": {"uri": "file:///local/clip.webm", "type": "video/webm"}},
                None,
                {"value": False, "edited": 1749138592012},
                {"value": {"svgString": "<svg/>", "width": 100, "lines": []}},
                {"value": "null"},
                {"value": "null"},
                {"value": [None]},
                {"value": None},
            ],
        ),
        # Migrated legacy answer with media URLs in migratedData and a 0-based month.
        (
            answer_record(
                "a3",
                migratedDate="2023-01-01T00:00:00",
                migratedData={
                    "decryptedFileAnswers": [
                        {"answerItemId": "item-selfie", "fileUrl": "https://legacy.example/photo.png"},
                        {"answerItemId": "item-sketch", "fileUrl": "https://legacy.example/drawing.svg"},
                    ]
                },
                flowId=FLOW_ID,
                flowName="Morning flow",
                reviewedAnswerId="rev-1",
            ),
            full,
            [
                {"value": 0},
                {"value": [1]},
                {"value": 0},
                {"value": 1},
                {"value": "0"},
                "ID",
                'He said "hi", then left',
                {"value": {"day": None, "month": 11, "year": 1999}},
                {"value": {"hours": 0, "minutes": 0}},
                {"value": {"from": {"hour": 1, "minute": 2}, "to": None}},
                {"value": {"latitude": None, "longitude": 0}},
                {"value": "old"},
                None,
                None,
                None,
                {"value": {"svgString": "<svg/>", "width": 100, "uri": "old", "lines": []}},
            ],
        ),
        # Failed decryption placeholder from the admin's decryptData.
        (answer_record("a4"), full, [{"type": "", "time": "", "screen": ""}]),
        # Reviewer assessment inside a flow.
        (
            answer_record("a5", flowId=FLOW_ID, flowName="Flow", reviewedFlowSubmitId="flow-sub-9"),
            activity(ALL_TYPES_ITEMS[:3]),
            [{"value": 2}, {"value": [0, 1, 2]}, {"value": 5}],
        ),
    ]

    def media_answers(**by_name) -> list:
        names = [entry["name"] for entry in ALL_TYPES_ITEMS]
        return [by_name.get(name) for name in names]

    cases += [
        # Media stored in every shape the app or legacy data can produce.
        (
            answer_record("m1", targetSecretId="P/002"),
            full,
            media_answers(
                selfie={"value": ["s3://bucket/mindlogger/answer/u/a/1/list-photo.png"]},
                clip={"value": {"uri": "s3://bucket/mindlogger/answer/u/a/1/movie.mp4", "type": "video/mp4"}},
                voice={"value": "https://legacy.example/voice.mp3?token=1", "text": "noisy"},
                sketch={"value": {"svgString": "<svg>m1</svg>", "width": 100, "lines": []}},
                game={"value": "s3://bucket/unity/single.json"},
            ),
        ),
        (
            answer_record("m2"),
            full,
            media_answers(
                selfie={"value": []},
                clip={"value": ""},
                sketch={"value": {"svgString": "<svg/>", "width": 100, "uri": "s3://b/m2.svg", "lines": []}},
                game={"value": ["s3://bucket/unity/", 5, "s3://bucket/unity/b.json?sig=1"]},
            ),
        ),
    ]

    scored = activity(SUBSCALE_ITEMS, name="Scored", subscale_setting=SUBSCALE_SETTING)
    cases += [
        # Lookup table hit on sex + age interval.
        (answer_record("s1"), scored, ["10", {"value": 0}, {"value": 0}, {"value": [0]}, {"value": 2}, {"value": 2}]),
        # Lookup falls through to the second row (score "0" falls back to the computed score).
        (answer_record("s2"), scored, ["30", {"value": 1}, {"value": 2}, {"value": [1, 2]}, {"value": 4}, None]),
        # Everything skipped: the admin still emits a row holding only the scores.
        (answer_record("s3"), scored, [None, None, None, None, None, None]),
        # Unknown option values give NaN totals.
        (answer_record("s4"), scored, [{"value": 9}, None, {"value": 7}, {"value": [5]}, {"value": 9}, None]),
    ]

    return [
        {"answer": copy.deepcopy(a), "activity": copy.deepcopy(act), "answersDecrypted": copy.deepcopy(ans)}
        for a, act, ans in cases
    ]
