"""An applet as GET /workspaces/{owner}/applets/{id} returns it (typed, camelCase, with `type` inside
config and responseValues), plus a fake server that saves it the way PUT /applets/{id} does."""

from __future__ import annotations

import copy
import json
import uuid

import httpx

OWNER_ID = "66666666-6666-4666-8666-666666666666"
APPLET_ID = "a1000000-0000-4000-8000-000000000001"
DAILY = "d1000000-0000-4000-8000-000000000001"
OTHER = "d1000000-0000-4000-8000-000000000002"
FLOW = "f1000000-0000-4000-8000-000000000001"
ENCRYPTION = {"publicKey": "[1,2,3]", "prime": "[7,7]", "base": "[2]", "accountId": OWNER_ID}

_NO_TEXT = {"textInputOption": False, "textInputRequired": False}


def _config(kind: str, **extra) -> dict:
    return {"type": kind, "removeBackButton": False, "skippableItem": False, **extra}


def _select_config(kind: str, **extra) -> dict:
    defaults = {
        "randomizeOptions": False,
        "timer": None,
        "addScores": False,
        "setAlerts": False,
        "addTooltip": False,
        "setPalette": False,
        "addTokens": None,
        "additionalResponseOption": _NO_TEXT,
        "portraitLayout": None,
    }
    return _config(kind, **(defaults | extra))


def _option(i: int, text: str, score=None, **extra) -> dict:
    return {
        "id": f"0000000{i}-0000-4000-8000-00000000000{i}",
        "text": text,
        "image": None,
        "score": score,
        "tooltip": None,
        "isHidden": False,
        "color": None,
        "alert": None,
        "value": i,
        **extra,
    }


def _item(item_id: str, name: str, kind: str, question: str, values, config, order: int, **extra) -> dict:
    return {
        "id": item_id,
        "name": name,
        "question": {"en": question},
        "responseType": kind,
        "responseValues": values,
        "config": config,
        "order": order,
        "isHidden": False,
        "conditionalLogic": None,
        "allowEdit": True,
        **extra,
    }


def daily_items() -> list[dict]:
    rows = [{"id": "e0000000-0000-4000-8000-000000000001", "rowName": "Morning", "rowImage": None, "tooltip": None},
            {"id": "e0000000-0000-4000-8000-000000000002", "rowName": "Evening", "rowImage": None, "tooltip": None}]  # fmt: skip
    grid_options = [{"id": "c0000000-0000-4000-8000-000000000001", "text": "Calm", "image": None, "tooltip": None},
                    {"id": "c0000000-0000-4000-8000-000000000002", "text": "Tense", "image": None, "tooltip": None}]  # fmt: skip
    matrix = [
        {"rowId": row["id"], "options": [{"optionId": o["id"], "score": s, "alert": None, "value": None} for o, s in zip(grid_options, (0, 2))]}
        for row in rows
    ]  # fmt: skip
    items = [
        _item(
            "a2000000-0000-4000-8000-000000000001",
            "mood",
            "singleSelect",
            "How is your mood?",
            {"type": "singleSelect", "paletteName": None, "options": [_option(0, "Bad", 0.0), _option(1, "Good", 2.0, color="#ff0000", tooltip="tip")]},
            _select_config("singleSelect", addScores=True, autoAdvance=False, responseDataIdentifier=False),
            1,
        ),
        _item(
            "a2000000-0000-4000-8000-000000000002",
            "feelings",
            "multiSelect",
            "Which feelings?",
            {"type": "multiSelect", "paletteName": None, "options": [_option(0, "Sad", isNoneAbove=False), _option(1, "None", isNoneAbove=True)]},
            _select_config("multiSelect", randomizeOptions=True),
            2,
            conditionalLogic={"match": "all", "conditions": [{"itemName": "mood", "type": "EQUAL_TO_OPTION", "payload": {"optionValue": "1"}}]},
        ),
        _item(
            "a2000000-0000-4000-8000-000000000003",
            "energy",
            "slider",
            "Energy?",
            {"type": "slider", "minLabel": "low", "maxLabel": "high", "minValue": 0, "maxValue": 4, "minImage": None, "maxImage": None, "scores": [0.0, 1.0, 2.0, 3.0, 4.0], "alerts": None},
            _config("slider", addScores=True, setAlerts=False, additionalResponseOption=_NO_TEXT, showTickMarks=False, showTickLabels=False, continuousSlider=False, timer=30),
            3,
        ),
        _item(
            "a2000000-0000-4000-8000-000000000004",
            "hours",
            "numberSelect",
            "Hours slept?",
            {"type": "numberSelect", "minValue": 0, "maxValue": 12},
            _config("numberSelect", additionalResponseOption={"textInputOption": True, "textInputRequired": False}),
            4,
            conditionalLogic={"match": "any", "conditions": [{"itemName": "energy", "type": "BETWEEN", "payload": {"minValue": 1.0, "maxValue": 3.0}}]},
        ),
        _item(
            "a2000000-0000-4000-8000-000000000005",
            "notes",
            "text",
            "Anything else?",
            None,
            _config("text", skippableItem=True, maxResponseLength=200, correctAnswerRequired=False, correctAnswer=None, numericalResponseRequired=False, responseDataIdentifier=False, responseRequired=True, isIdentifier=None),
            5,
        ),
        _item("a2000000-0000-4000-8000-000000000006", "day", "date", "Which day?", None, _config("date", additionalResponseOption=_NO_TEXT, timer=None), 6),
        _item(
            "a2000000-0000-4000-8000-000000000007",
            "bedtime",
            "time",
            "Bedtime?",
            None,
            _config("time", additionalResponseOption=_NO_TEXT, timer=None),
            7,
            conditionalLogic={"match": "all", "conditions": [{"itemName": "day", "type": "GREATER_THAN_DATE", "payload": {"date": "2026-01-01"}}]},
        ),
        _item(
            "a2000000-0000-4000-8000-000000000008",
            "sleep",
            "timeRange",
            "Sleep times?",
            None,
            _config("timeRange", additionalResponseOption=_NO_TEXT, timer=None),
            8,
            conditionalLogic={"match": "all", "conditions": [{"itemName": "bedtime", "type": "BETWEEN_TIMES", "payload": {"minTime": {"hours": 21, "minutes": 0}, "maxTime": {"hours": 23, "minutes": 30}, "fieldName": None}}]},
        ),
        _item(
            "a2000000-0000-4000-8000-000000000009",
            "grid",
            "singleSelectRows",
            "How did you feel?",
            {"type": "singleSelectRows", "rows": rows, "options": grid_options, "dataMatrix": matrix},
            _config("singleSelectRows", timer=None, addScores=True, setAlerts=False, addTooltip=False, addTokens=None),
            9,
            conditionalLogic={"match": "all", "conditions": [{"itemName": "sleep", "type": "GREATER_THAN_TIME_RANGE", "payload": {"time": "06:00", "maxValue": None, "minValue": None, "fieldName": "to"}}]},
        ),
        _item(
            "a2000000-0000-4000-8000-000000000010",
            "pain",
            "sliderRows",
            "Pain?",
            {"type": "sliderRows", "rows": [{"id": "b0000000-0000-4000-8000-000000000001", "label": "Head", "minLabel": "none", "maxLabel": "worst", "minValue": 0, "maxValue": 3, "minImage": None, "maxImage": None, "scores": None, "alerts": None}]},
            _config("sliderRows", addScores=False, setAlerts=False, timer=None),
            10,
            conditionalLogic={"match": "all", "conditions": [{"itemName": "grid", "type": "EQUAL_TO_ROW_OPTION", "payload": {"optionValue": grid_options[1]["id"], "rowIndex": "1"}}]},
        ),
        _item(
            "a2000000-0000-4000-8000-000000000011",
            "after_pain",
            "message",
            "Thanks.",
            None,
            {"type": "message", "removeBackButton": False, "timer": None},
            11,
            conditionalLogic={"match": "all", "conditions": [{"itemName": "pain", "type": "GREATER_THAN_SLIDER_ROWS", "payload": {"value": 1.0, "rowIndex": "0"}}]},
        ),
        _item("a2000000-0000-4000-8000-000000000012", "voice", "audio", "Say something", {"type": "audio", "maxDuration": 60}, _config("audio", additionalResponseOption=_NO_TEXT, timer=None), 12),
        _item("a2000000-0000-4000-8000-000000000013", "task", "flanker", "Flanker task", None, {"type": "flanker", "stimulusTrials": [], "blocks": [], "buttons": [], "samplingMethod": "randomize-order", "showFeedback": True, "showFixation": True, "showResults": True, "trialDuration": 1500, "isLastPractice": False, "isFirstPractice": True, "isLastTest": False, "blockType": "practice"}, 13),
    ]  # fmt: skip
    return items


def applet() -> dict:
    daily = {
        "id": DAILY,
        "name": "Daily",
        "description": {"en": "Every day", "fr": "Chaque jour"},
        "splashScreen": "",
        "image": "",
        "showAllAtOnce": False,
        "isSkippable": False,
        "isReviewable": False,
        "responseIsEditable": True,
        "isHidden": False,
        "scoresAndReports": {
            "generateReport": True,
            "showScoreSummary": True,
            "reports": [{"type": "score", "name": "Mood score", "id": "sumScore_mood", "calculationType": "sum", "itemsScore": ["mood"], "itemsPrint": ["notes"], "conditionalLogic": []}],
        },
        "subscaleSetting": {"calculateTotalScore": "sum", "subscales": [{"name": "Core", "scoring": "sum", "items": [{"name": "mood", "type": "item"}, {"name": "energy", "type": "item"}], "subscaleTableData": None}], "totalScoresTableData": None},
        "reportIncludedItemName": "notes",
        "performanceTaskType": "flanker",
        "autoAssign": True,
        "items": daily_items(),
        "createdAt": "2026-01-01T00:00:00",
    }  # fmt: skip
    other = {
        **{k: v for k, v in daily.items() if k not in ("items", "subscaleSetting", "scoresAndReports")},
        "id": OTHER,
        "name": "Weekly",
        "description": {"en": ""},
        "reportIncludedItemName": None,
        "performanceTaskType": None,
        "subscaleSetting": None,
        "scoresAndReports": None,
        "items": [_item("a2000000-0000-4000-8000-000000000099", "week", "text", "This week?", None, _config("text", maxResponseLength=300, correctAnswerRequired=False, correctAnswer=None, numericalResponseRequired=False, responseDataIdentifier=False, responseRequired=False), 1)],
    }  # fmt: skip
    return {
        "id": APPLET_ID,
        "displayName": "EMA Study",
        "version": "1.2.0",
        "description": {"en": "An EMA study"},
        "about": {"en": "About it"},
        "image": "",
        "watermark": "",
        "themeId": "77777777-7777-4777-8777-777777777777",
        "streamEnabled": False,
        "streamIpAddress": None,
        "streamPort": None,
        "encryption": dict(ENCRYPTION),
        "ownerId": OWNER_ID,
        "activities": [daily, other],
        "activityFlows": [
            {"id": FLOW, "name": "Morning", "description": {"en": ""}, "isSingleReport": False, "hideBadge": False, "order": 1, "isHidden": False, "autoAssign": True, "items": [{"id": "f2000000-0000-4000-8000-000000000001", "activityId": DAILY, "order": 1}]}
        ],
    }  # fmt: skip


class FakeBuilderServer:
    """GET and PUT of a whole applet, checked and saved like the backend does."""

    def __init__(self, role: str = "editor"):
        self.applet = applet()
        self.role = role
        self.requests: list[httpx.Request] = []
        self.puts: list[dict] = []
        self.bump_before_put = False  # simulate someone else saving in between

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/auth/login":
            return httpx.Response(200, json={"result": {"token": {"accessToken": "t", "refreshToken": "r"}}})
        if path == "/workspaces":
            return httpx.Response(200, json={"result": [{"ownerId": OWNER_ID, "workspaceName": "Lab"}]})
        if path == f"/workspaces/{OWNER_ID}/applets":
            row = {"id": APPLET_ID, "displayName": self.applet["displayName"], "role": self.role, "type": "applet"}
            return httpx.Response(200, json={"result": [row]})
        if path == f"/applets/{APPLET_ID}" and request.method == "GET":
            return httpx.Response(
                200, json={"result": {"id": APPLET_ID, "displayName": "EMA Study", "ownerId": OWNER_ID}}
            )
        if path == f"/workspaces/{OWNER_ID}/applets/{APPLET_ID}":
            if self.bump_before_put and sum(r.url.path == path for r in self.requests) == 2:
                self.applet["version"] = "9.9.9"  # someone saved between the preview and the save
            return httpx.Response(200, json={"result": copy.deepcopy(self.applet)})
        if path == f"/applets/{APPLET_ID}" and request.method == "PUT":
            if self.role not in ("owner", "manager", "editor"):
                return httpx.Response(403, json={"result": [{"message": "Access denied to edit applet."}]})
            body = json.loads(request.content)
            error = self._check(body)
            if error:
                return httpx.Response(422, json={"result": [{"message": error}]})
            self.puts.append(copy.deepcopy(body))
            self._save(body)
            return httpx.Response(200, json={"result": {"id": APPLET_ID, "version": self.applet["version"]}})
        return httpx.Response(404, json={"detail": "Not Found"})

    @staticmethod
    def _check(body: dict) -> str | None:
        for key in ("displayName", "encryption", "activities", "activityFlows"):
            if key not in body:
                return f"{key} is required"
        if set(body["encryption"]) != {"publicKey", "prime", "base", "accountId"}:
            return "encryption has extra fields"
        keys = set()
        for activity in body["activities"]:
            if not activity.get("name") or not activity.get("key"):
                return "activities need a name and a key"
            keys.add(activity["key"])
            names = [item.get("name") for item in activity.get("items") or []]
            if len(names) != len(set(names)):
                return "Activity item names must be unique"
            for item in activity.get("items") or []:
                if not item.get("responseType") or not isinstance(item.get("config"), dict):
                    return "items need a responseType and config"
                if item["responseType"] in ("singleSelect", "multiSelect"):
                    options = (item.get("responseValues") or {}).get("options")
                    if not options or any(not o.get("text") for o in options):
                        return "options need text"
                    if item["config"].get("addScores") and any(o.get("score") is None for o in options):
                        return "Scores must be provided in each option"
                if item.get("isHidden") and item.get("conditionalLogic"):
                    return "hidden items can't have conditional logic"
        for flow in body["activityFlows"]:
            for flow_item in flow.get("items") or []:
                if flow_item.get("activityKey") not in keys:
                    return "flow item activityKey does not match an activity"
        return None

    def _save(self, body: dict) -> None:
        major, middle, minor = (int(p) for p in self.applet["version"].split("."))
        saved = copy.deepcopy(body)
        key_to_id = {}
        for order, activity in enumerate(saved["activities"], start=1):
            activity["id"] = activity.get("id") or str(uuid.uuid4())
            key_to_id[activity.pop("key")] = activity["id"]
            activity["order"] = order
            for position, item in enumerate(activity.get("items") or [], start=1):
                item["id"] = item.get("id") or str(uuid.uuid4())
                item["order"] = position
        for flow in saved["activityFlows"]:
            flow["items"] = [
                {"id": i.get("id") or str(uuid.uuid4()), "activityId": key_to_id[i["activityKey"]], "order": n}
                for n, i in enumerate(flow["items"], start=1)
            ]
        saved["version"] = f"{major}.{middle}.{minor + 1}"
        self.applet = saved
