"""Data dictionaries (`curious-download --dictionary`) against a fake server with several applet versions."""

from __future__ import annotations

import copy
import csv
import io
import json
from pathlib import Path

import httpx
import pytest
import typer
from typer.testing import CliRunner

from curious_download import cli
from curious_download.client import CuriousClient
from curious_download.dictionary import (
    AppletHistory,
    _merged_order,
    build_dictionaries,
    camelize,
    describe_condition,
    describe_logic,
    normalize_version,
)
from curious_download.report import parse_options

SERVER = "https://api.curious.test"
EMAIL = "researcher@example.org"
PASSWORD = "account-pw"
OWNER_ID = "66666666-6666-4666-8666-666666666666"

EMA = "a0000000-0000-4000-8000-000000000001"
LOCKED = "a0000000-0000-4000-8000-000000000002"
DAILY = "d0000000-0000-4000-8000-000000000001"
OLD = "d0000000-0000-4000-8000-000000000002"
WEEKLY = "d0000000-0000-4000-8000-000000000003"
FLOW = "f0000000-0000-4000-8000-000000000001"


# -- a four-version applet, stored JSON in snake_case as the version endpoint returns it -----------


def _item(item_id: str, name: str, response_type: str, question: str, values=None, config=None, **extra) -> dict:
    return {
        "id": item_id,
        "name": name,
        "question": {"en": question, "fr": f"FR {question}"},
        "responseType": response_type,
        "responseValues": values,
        "config": config or {},
        "order": 0,
        "isHidden": False,
        "conditionalLogic": None,
        "allowEdit": True,
        **extra,
    }


def _select(texts: list[str], scores: list[float | None]) -> dict:
    options = [
        {"id": f"opt{i}", "text": text, "value": i, "score": score, "is_hidden": False, "color": None, "alert": None}
        for i, (text, score) in enumerate(zip(texts, scores))
    ]
    return {"type": "singleSelect", "palette_name": None, "options": options}


SELECT_CONFIG = {
    "type": "singleSelect",
    "remove_back_button": False,
    "skippable_item": False,
    "randomize_options": False,
    "timer": None,
    "add_scores": True,
    "set_alerts": False,
    "add_tooltip": False,
    "set_palette": False,
    "additional_response_option": {"text_input_option": True, "text_input_required": False},
}
MOOD_V1 = _item("i-mood", "mood", "singleSelect", "How is your mood?", _select(["never", "sometimes", "always"], [1.0, 2.0, 3.0]), SELECT_CONFIG)  # fmt: skip
MOOD_V2 = _item("i-mood", "mood", "singleSelect", "How is your mood?", _select(["never", "often", "always"], [1.0, 2.0, 4.0]), SELECT_CONFIG)  # fmt: skip
SLEEP = _item(
    "i-sleep",
    "sleep_hours",
    "numberSelect",
    "Hours slept?",
    {"type": "numberSelect", "min_value": 0, "max_value": 12},
    {"skippable_item": True, "additional_response_option": {"text_input_option": False, "text_input_required": False}},
)
ENERGY = _item(
    "i-energy",
    "energy",
    "slider",
    "Energy level",
    {
        "type": "slider",
        "min_value": 0,
        "max_value": 4,
        "min_label": "none",
        "max_label": "lots",
        "scores": [0, 1, 2, 3, 4],
    },
    {"skippable_item": False, "continuous_slider": True, "timer": 30},
)
NOTES_CONFIG = {
    "skippable_item": True,
    "max_response_length": 300,
    "correct_answer_required": False,
    "numerical_response_required": True,
    "response_required": False,
    "response_data_identifier": False,
}
NOTES_V1 = _item("i-notes", "notes", "text", "Anything else?", None, NOTES_CONFIG)
NOTES_V2 = {
    **NOTES_V1,
    "conditionalLogic": {
        "match": "all",
        "conditions": [{"item_name": "mood", "type": "EQUAL_TO_OPTION", "payload": {"option_value": "1"}}],
    },
}
OLD_ITEM = _item("i-old", "q_old", "text", "Old question", None, {"skippable_item": False})

WEEKLY_ITEMS = [
    _item("i-w1", "w1", "singleSelect", "Week item 1", _select(["no", "yes"], [0.0, 1.0]), SELECT_CONFIG),
    _item("i-w2", "w2", "singleSelect", "Week item 2", _select(["no", "yes"], [0.0, 2.0]), SELECT_CONFIG),
    _item(
        "i-w3", "w3", "singleSelect", "Hidden item", _select(["no", "yes"], [0.0, 5.0]), SELECT_CONFIG, isHidden=True
    ),
]
WEEKLY_SUBSCALES = {
    "calculate_total_score": "sum",
    "total_scores_table_data": [{"raw_score": "0~4", "optional_text": "Low"}],
    "subscales": [
        {
            "name": "Core (A)",
            "scoring": "average",
            "items": [{"name": "w1", "type": "item"}, {"name": "w2", "type": "item"}],
            "subscale_table_data": [
                {
                    "score": "50",
                    "raw_score": "0~2",
                    "age": "8~12",
                    "sex": "M",
                    "optional_text": "Typical",
                    "severity": "Mild",
                }
            ],
        }
    ],
}


def _activity(activity_id: str, name: str, items: list[dict], **extra) -> dict:
    return {
        "id": activity_id,
        "name": name,
        "description": {"en": ""},
        "isSkippable": False,
        "isReviewable": False,
        "isHidden": False,
        "order": 1,
        "subscaleSetting": None,
        "items": items,
        **extra,
    }


def _versions() -> dict[str, dict]:
    daily_v1 = _activity(DAILY, "Daily", [MOOD_V1, SLEEP, NOTES_V1])
    daily_v3 = _activity(DAILY, "Daily", [MOOD_V2, ENERGY, SLEEP, NOTES_V2])
    daily_v4 = _activity(DAILY, "Daily", [MOOD_V2, ENERGY, NOTES_V2])
    old = _activity(OLD, "Old survey", [OLD_ITEM], isReviewable=True)
    weekly = _activity(WEEKLY, "Weekly", WEEKLY_ITEMS, subscaleSetting=WEEKLY_SUBSCALES)
    flow = {"id": FLOW, "name": "Morning", "description": {}, "order": 1, "items": [{"id": "fi", "order": 1, "activity": daily_v4}]}  # fmt: skip
    plan = {
        "1.0.0": ([daily_v1, old], []),
        "1.0.1": ([daily_v1, old], []),
        "1.1.0": ([daily_v3, old], []),
        "2.0.0": ([daily_v4, weekly], [flow]),
    }
    return {
        version: {
            "id": EMA,
            "displayName": "EMA Study",
            "version": version,
            "activities": copy.deepcopy(activities),
            "activityFlows": copy.deepcopy(flows),
        }
        for version, (activities, flows) in plan.items()
    }


class FakeServer:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.versions = _versions()
        self.applets = [
            {"id": EMA, "displayName": "EMA Study", "role": "owner", "type": "applet"},
            {"id": LOCKED, "displayName": "Locked [pilot]", "role": "respondent", "type": "applet"},
            {"id": "folder-1", "displayName": "A folder", "type": "folder"},
        ]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/auth/login":
            return httpx.Response(200, json={"result": {"token": {"accessToken": "t", "refreshToken": "r"}}})
        if path == "/workspaces":
            workspaces = [{"ownerId": OWNER_ID, "workspaceName": "Lab"}]
            return httpx.Response(200, json={"result": workspaces, "count": 1})
        if path == f"/workspaces/{OWNER_ID}/applets":
            return httpx.Response(200, json={"result": self.applets, "count": len(self.applets)})
        if path.startswith(f"/applets/{LOCKED}/"):
            return httpx.Response(403, json={"result": [{"message": "Access denied to applet."}]})
        if path == f"/applets/{EMA}/versions":
            created = {"1.0.0": "2026-01-01T10:00:00", "1.0.1": "2026-01-02T10:00:00", "1.1.0": "2026-02-01T10:00:00"}
            rows = [
                {"version": v, "createdAt": created.get(v, "2026-03-01T10:00:00"), "creator": {"id": "u"}}
                for v in reversed(self.versions)
            ]
            return httpx.Response(200, json={"result": rows, "count": len(rows)})
        if path.startswith(f"/applets/{EMA}/versions/"):
            return httpx.Response(200, json={"result": self.versions[path.rsplit("/", 1)[1]]})
        return httpx.Response(404, json={"result": [{"message": f"No route {path}"}]})


@pytest.fixture
def run(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SETTINGS_PATH", tmp_path / "settings.json")
    app = typer.Typer()
    app.command()(cli.download)

    def invoke(server: FakeServer, *args: str) -> tuple:
        monkeypatch.setattr(cli, "CuriousClient", lambda url: CuriousClient(url, transport=server.transport()))
        base = ["--server", SERVER, "--email", EMAIL, "--output", str(tmp_path / "out"), "--no-input", "--dictionary"]
        # No applet password: dictionaries never decrypt anything.
        result = CliRunner().invoke(app, [*base, *args], env={"CURIOUS_PASSWORD": PASSWORD})
        folders = sorted((tmp_path / "out").glob("data_dictionary_*"))
        return result, (folders[-1] if folders else None)

    return invoke


def _read(path: Path) -> list[dict]:
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


# -- end to end ------------------------------------------------------------------------------------


def test_one_file_per_activity_with_version_changes(run):
    server = FakeServer()
    result, folder = run(server, "--applet", "EMA Study")
    assert result.exit_code == 0, result.output
    assert folder.name.startswith("data_dictionary_EMA_Study_")
    assert sorted(p.name for p in folder.glob("*.csv")) == [
        "EMA_Study__Daily__data_dictionary.csv",
        "EMA_Study__Old_survey__data_dictionary.csv",
        "EMA_Study__Weekly__data_dictionary.csv",
        "index.csv",
    ]

    rows = _read(folder / "EMA_Study__Daily__data_dictionary.csv")
    summary = [(r["item_name"], r["first_version"], r["last_version"], r["status"], r["change"]) for r in rows]
    assert summary == [
        ("mood", "1.0.0", "1.0.1", "changed in 1.1.0", ""),
        ("mood", "1.1.0", "2.0.0", "current", "response options; scores"),
        ("energy", "1.1.0", "2.0.0", "current", "added"),
        # Removed items stay where they were, after the item they followed.
        ("sleep_hours", "1.0.0", "1.1.0", "removed in 2.0.0", ""),
        ("notes", "1.0.0", "1.0.1", "changed in 1.1.0", ""),
        ("notes", "1.1.0", "2.0.0", "current", "skip logic"),
    ]
    assert rows[0]["applet_versions"] == "1.0.0, 1.0.1"
    assert rows[1]["applet_versions"] == "1.1.0, 2.0.0"

    old_mood, mood = rows[0], rows[1]
    assert mood["item_id"] == "i-mood" and mood["item_type"] == "singleSelect"
    assert mood["item_prompt"] == "How is your mood?"
    assert old_mood["value_labels"] == "0=never | 1=sometimes | 2=always"
    assert mood["value_labels"] == "0=never | 1=often | 2=always"
    assert mood["scores"] == "0=1 | 1=2 | 2=4"
    assert mood["other_text_option"] == "optional" and mood["skippable"] == "no" and mood["hidden"] == "no"
    # The same cell responses.csv has for this item.
    camel_values = camelize(MOOD_V2["responseValues"])
    assert mood["item_response_options"] == parse_options(camel_values, "singleSelect")
    assert mood["item_response_options"] == "Never: 0 (score: 1), Often: 1 (score: 2), Always: 2 (score: 4)"

    energy = rows[2]
    assert (energy["min_value"], energy["max_value"], energy["min_label"], energy["max_label"]) == (
        "0",
        "4",
        "none",
        "lots",
    )
    assert energy["value_labels"] == "0=none | 4=lots"
    assert energy["scores"] == "0=0 | 1=1 | 2=2 | 3=3 | 4=4"
    assert energy["response_rules"] == "continuous slider; time limit 30 seconds"
    assert energy["item_order"] == "2"

    sleep = rows[3]
    assert (sleep["min_value"], sleep["max_value"], sleep["skippable"]) == ("0", "12", "yes")
    assert sleep["activity_flows"] == ""  # never in a flow while it existed

    notes = rows[5]
    assert notes["shown_if"] == 'mood = "often" (1)'
    assert rows[4]["shown_if"] == ""
    assert notes["response_rules"] == "max 300 characters; numbers only"
    # Only version 2.0.0 has the flow.
    assert notes["activity_flows"] == "Morning" and rows[4]["activity_flows"] == ""
    assert {r["applet_id"] for r in rows} == {EMA} and {r["applet_name"] for r in rows} == {"EMA Study"}


def test_score_and_subscale_rows(run):
    result, folder = run(FakeServer(), "--applet", EMA)
    assert result.exit_code == 0, result.output
    rows = {r["item_name"]: r for r in _read(folder / "EMA_Study__Weekly__data_dictionary.csv")}
    assert list(rows) == [
        "w1",
        "w2",
        "w3",
        "activity_score",
        "activity_score_lookup_text",
        "subscale_name_Core__A_",
        "subscale_lookup_text_Core__A_",
    ]
    assert rows["w3"]["hidden"] == "yes"
    total = rows["activity_score"]
    assert total["item_type"] == "total_score"
    assert total["item_prompt"] == "Total score: Sum of w1, w2"  # hidden items are left out, as in the admin
    assert total["value_labels"] == "raw 0~4: Low"
    assert rows["activity_score_lookup_text"]["item_prompt"] == "Text from the lookup table of the total score"
    subscale = rows["subscale_name_Core__A_"]
    assert subscale["item_type"] == "subscale_score"
    assert subscale["item_prompt"] == "Subscale Core (A): Average of w1, w2"
    assert subscale["value_labels"] == "raw 0~2 -> 50 (sex M, age 8~12, Mild): Typical"
    assert {r["change"] for r in rows.values()} == {""}  # all appeared with the activity in 2.0.0
    assert {r["status"] for r in rows.values()} == {"current"}


def test_index_and_info(run):
    server = FakeServer()
    result, folder = run(server, "--all-applets", "--workspace", "Lab")
    assert result.exit_code == 0, result.output
    assert folder.name.startswith("data_dictionary_Lab_")

    index = _read(folder / "index.csv")
    assert [(r["applet_name"], r["activity_name"], r["status"]) for r in index] == [
        ("EMA Study", "Daily", "current"),
        ("EMA Study", "Old survey", "removed in 2.0.0"),
        ("EMA Study", "Weekly", "current"),
        ("Locked [pilot]", "", ""),
    ]
    daily, old, _, locked = index
    assert (daily["items"], daily["items_changed"], daily["first_version"]) == ("4", "2", "1.0.0")
    assert old["note"] == "reviewer assessment" and old["last_version"] == "1.1.0"
    assert locked["note"] == "no access to this applet" and locked["file"] == ""
    assert "Locked [pilot]: no access" in result.output

    info = json.loads((folder / "export_info.json").read_text())
    assert info["kind"] == "data dictionary" and info["workspace"] == "Lab" and info["files"] == 3
    assert info["applets"][0] == {
        "id": EMA,
        "name": "EMA Study",
        "versions": 4,
        "current_version": "2.0.0",
        "activities": 3,
    }

    paths = [r.url.path for r in server.requests]
    assert not any(p.startswith("/answers") for p in paths), "no answer data is downloaded"
    assert "/applets/folder-1/versions" not in paths
    assert paths.count(f"/applets/{EMA}/versions") == 1
    assert sum(p.startswith(f"/applets/{EMA}/versions/") for p in paths) == 4


def test_several_applets_and_bad_combinations(run):
    result, folder = run(FakeServer(), "--applet", "EMA Study", "--applet", EMA)
    assert result.exit_code == 0, result.output
    assert folder.name.startswith("data_dictionary_EMA_Study_"), "the same applet twice is one applet"

    result, _ = run(FakeServer(), "--applet", "Nope")
    assert result.exit_code == 2 and "No applet called 'Nope'" in result.output
    result, _ = run(FakeServer(), "--participant", "P001", "--applet", "EMA Study")
    assert result.exit_code == 2 and "--participant only apply to answer downloads" in result.output
    result, _ = run(FakeServer(), "--applet", "EMA Study", "--all-applets")
    assert result.exit_code == 2 and "not both" in result.output
    result, _ = run(FakeServer())
    assert result.exit_code == 2 and "--all-applets" in result.output


def test_answer_mode_rejects_dictionary_options(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SETTINGS_PATH", tmp_path / "settings.json")
    app = typer.Typer()
    app.command()(cli.download)
    base = ["--server", SERVER, "--email", EMAIL, "--no-input"]
    result = CliRunner().invoke(app, [*base, "--all-applets"])
    assert result.exit_code == 2 and "--all-applets only applies to --dictionary" in result.output
    result = CliRunner().invoke(app, [*base, "--applet", "A", "--applet", "B"])
    assert result.exit_code == 2 and "one applet at a time" in result.output


def test_wizard_choose_applets(monkeypatch, tmp_path):
    server = FakeServer()
    answers = iter(["Choose applets…", [server.applets[0]]])
    monkeypatch.setattr(cli, "_ask", lambda question: next(answers))
    printed = io.StringIO()
    monkeypatch.setattr(cli, "console", cli.Console(file=printed, width=300))
    with CuriousClient(SERVER, transport=server.transport()) as client:
        client.login(EMAIL, PASSWORD)
        cli._download_dictionaries(client, SERVER, EMAIL, [], False, None, tmp_path, interactive=True)
    folder = next(tmp_path.glob("data_dictionary_EMA_Study_*"))
    assert (folder / "EMA_Study__Daily__data_dictionary.csv").is_file()
    assert f"curious-download --dictionary --server {SERVER} --email {EMAIL} --applet {EMA}" in printed.getvalue()


# -- units -----------------------------------------------------------------------------------------


def test_camelize_matches_the_backend():
    stored = {"min_value": 1, "options": [{"is_none_above": True, "text": "a_b"}], "alreadyCamel": {"row_index": "0"}}
    assert camelize(stored) == {
        "minValue": 1,
        "options": [{"isNoneAbove": True, "text": "a_b"}],
        "alreadyCamel": {"rowIndex": "0"},
    }
    # Translations are left alone.
    version = normalize_version({"activities": [_activity(DAILY, "Daily", [MOOD_V1])]})
    item = version["activities"][0]["items"][0]
    assert item["question"] == {"en": "How is your mood?", "fr": "FR How is your mood?"}
    assert item["responseValues"]["paletteName"] is None and "palette_name" not in item["responseValues"]
    assert item["config"]["additionalResponseOption"] == {"textInputOption": True, "textInputRequired": False}


def test_merged_order_keeps_removed_items_in_place():
    assert _merged_order([["a", "b", "c"], ["a", "c"], ["a", "d", "c"]]) == {"a": 0, "b": 1, "d": 2, "c": 3}
    assert _merged_order([["x", "a"], ["a"]]) == {"x": 0, "a": 1}


def test_conditions_are_readable():
    items = {
        "feelings": {"responseType": "multiSelect", "responseValues": camelize(_select(["never", "often"], [0, 1]))},
        "grid": {"responseType": "sliderRows", "responseValues": {"rows": [{"label": "Pain"}, {"label": "Itch"}]}},
        "grid_single": {
            "responseType": "singleSelectRows",
            "responseValues": {
                "rows": [{"rowName": "Row one"}, {"rowName": "Row two"}],
                "options": [{"id": "x", "text": "Yes"}],
            },
        },
    }

    def text(kind: str, name: str, **payload) -> str:
        return describe_condition({"itemName": name, "type": kind, "payload": payload}, items)

    assert text("INCLUDES_OPTION", "feelings", optionValue="0") == 'feelings includes "never" (0)'
    assert text("NOT_INCLUDES_OPTION", "feelings", optionValue="9") == 'feelings does not include "9"'
    assert text("EQUAL_TO_ROW_OPTION", "grid_single", optionValue="x", rowIndex="1") == 'grid_single [Row two] = "Yes"'
    assert text("GREATER_THAN_SLIDER_ROWS", "grid", value=3.0, rowIndex="0") == "grid [Pain] > 3"
    assert text("BETWEEN_SLIDER_ROWS", "grid", minValue=1, maxValue=4, rowIndex="1") == "grid [Itch] between 1 and 4"
    assert text("OUTSIDE_OF", "energy", minValue=1.0, maxValue=3.0) == "energy not between 1 and 3"
    assert text("NOT_EQUAL", "count", value=2.5) == "count != 2.5"
    assert (
        text("BETWEEN_DATES", "day", minDate="2020-01-01", maxDate="2020-12-31")
        == "day between 2020-01-01 and 2020-12-31"
    )
    assert text("LESS_THAN_DATE", "day", date="2020-01-01") == "day < 2020-01-01"
    assert text("GREATER_THAN_TIME_RANGE", "sleep", time="08:30", fieldName="from") == "sleep (start) > 08:30"
    assert text("BETWEEN_TIMES", "bed", minTime={"hours": 8, "minutes": 0}, maxTime={"hours": 9, "minutes": 5}) == (
        "bed between 08:00 and 09:05"
    )
    assert text("EQUAL", "bed", type="endTime", value="07:00") == "bed (end) = 07:00"
    assert text("SOMETHING_NEW", "x", value=1) == 'x SOMETHING_NEW {"value": 1}'

    logic = {
        "match": "any",
        "conditions": [
            {"itemName": "feelings", "type": "INCLUDES_OPTION", "payload": {"optionValue": "1"}},
            {"itemName": "count", "type": "GREATER_THAN", "payload": {"value": 5}},
        ],
    }
    assert describe_logic(logic, items) == 'feelings includes "often" (1) OR count > 5'
    assert describe_logic({**logic, "match": "all"}, items).count(" AND ") == 1
    assert describe_logic(None, items) == ""


def _history(*activity_versions: list[dict]) -> AppletHistory:
    names = [f"1.{i}.0" for i in range(len(activity_versions))]
    snapshots = [{"activities": [_activity(DAILY, "Daily", items)], "activityFlows": []} for items in activity_versions]
    return AppletHistory(EMA, "EMA Study", names, [normalize_version(s) for s in snapshots])


def test_item_followed_when_its_id_is_replaced():
    replaced = {**OLD_ITEM, "id": "i-new"}
    [dictionary] = build_dictionaries(_history([OLD_ITEM], [replaced]))
    assert [(r["item_id"], r["status"], r["change"]) for r in dictionary.rows] == [
        ("i-old", "changed in 1.1.0", ""),
        ("i-new", "current", "item ID changed (was i-old)"),
    ]
    assert (dictionary.items, dictionary.items_changed) == (1, 1)


def test_change_labels():
    renamed = {**OLD_ITEM, "name": "q_new", "question": {"en": "New question"}}
    hidden = {**renamed, "isHidden": True, "config": {"skippable_item": True, "timer": 10}}
    readded = {**OLD_ITEM}
    [dictionary] = build_dictionaries(_history([OLD_ITEM], [renamed], [hidden], [], [readded]))
    assert [(r["item_name"], r["status"], r["change"]) for r in dictionary.rows] == [
        ("q_old", "changed in 1.1.0", ""),
        ("q_new", "changed in 1.2.0", "renamed (was q_old); prompt"),
        ("q_new", "removed in 1.3.0", "now hidden; settings: skippable item, timer"),
        ("q_old", "current", "added again"),
    ]


def test_appearance_and_alert_changes_are_labelled():
    values = camelize(_select(["a", "b"], [0, 1]))
    recolored = copy.deepcopy(values)
    recolored["options"][0]["color"] = "#ff0000"
    recolored["options"][1]["alert"] = "Call the participant"
    first = _item("i-x", "x", "singleSelect", "X?", values)
    second = {**first, "responseValues": recolored}
    [dictionary] = build_dictionaries(_history([first], [second]))
    assert dictionary.rows[1]["change"] == "alerts; appearance"
