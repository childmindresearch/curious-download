"""`curious-download --upload` and `--restore` against a fake server that saves applets like the backend."""

from __future__ import annotations

import copy
import csv
import io
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from curious_download import cli
from curious_download.client import CuriousClient
from curious_download.csvout import write_csv
from curious_download.dictionary import DICTIONARY_COLUMNS, item_cells, score_rows
from curious_download.upload import update_body

from .full_applet import APPLET_ID, DAILY, ENCRYPTION, OTHER, FakeBuilderServer, applet

SERVER = "https://api.curious.test"
EMAIL = "researcher@example.org"


def dictionary_rows(source: dict, activity_name: str) -> list[dict]:
    """What `--dictionary` writes for the current version of one activity."""
    activity = next(a for a in source["activities"] if a["name"] == activity_name)
    context = {"activity_id": activity["id"], "activity_name": activity["name"]}
    items = [{**context, **item_cells(item, activity, n)} for n, item in enumerate(activity["items"], start=1)]
    return items + [{**context, **row} for row in score_rows(activity)]


@pytest.fixture
def run(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SETTINGS_PATH", tmp_path / "settings.json")
    app = typer.Typer()
    app.command()(cli.download)
    out = tmp_path / "out"

    def invoke(server: FakeBuilderServer, *args: str, rows: list[dict] | None = None) -> tuple:
        monkeypatch.setattr(cli, "CuriousClient", lambda url: CuriousClient(url, transport=server.transport()))
        extra = list(args)
        if rows is not None:
            path = tmp_path / "upload.csv"
            write_csv(path, rows, DICTIONARY_COLUMNS)
            extra = ["--upload", str(path), "--applet", "EMA Study", *extra]
        base = ["--server", SERVER, "--email", EMAIL, "--output", str(out), "--no-input"]
        result = CliRunner().invoke(app, [*base, *extra], env={"CURIOUS_PASSWORD": "pw"})
        result.text = " ".join(result.output.split())  # the console wraps long lines
        folders = sorted(out.glob("upload_*")) if out.exists() else []
        return result, (folders[-1] if folders else None)

    return invoke


def _plan(folder: Path) -> list[dict]:
    return list(csv.DictReader(io.StringIO((folder / "upload_plan.csv").read_bytes().decode("utf-8-sig"))))


def test_unedited_download_changes_nothing(run):
    server = FakeBuilderServer()
    result, folder = run(server, "--yes", rows=dictionary_rows(server.applet, "Daily"))
    assert result.exit_code == 0, result.output
    assert "Nothing to change" in result.text
    assert server.puts == []
    assert {row["action"] for row in _plan(folder)} == {"unchanged"}


def test_history_rows_are_ignored(run):
    server = FakeBuilderServer()
    rows = dictionary_rows(server.applet, "Daily")
    old = {**rows[0], "value_labels": "0=Awful | 1=Good", "status": "changed in 1.2.0"}
    result, _ = run(server, "--yes", rows=[old, *rows])
    assert result.exit_code == 0, result.output
    assert server.puts == []


def test_edits_are_saved_and_everything_else_is_sent_back(run):
    server = FakeBuilderServer()
    original = copy.deepcopy(server.applet)
    rows = dictionary_rows(server.applet, "Daily")
    by_name = {row["item_name"]: row for row in rows}
    by_name["mood"]["value_labels"] = "0=Bad | 1=Great"
    by_name["notes"]["response_rules"] = "max 200 characters; numbers only; response required"
    by_name["feelings"]["shown_if"] = 'mood = "Bad" (0)'
    new = {
        "activity_name": "Daily",
        "item_name": "why",
        "item_type": "text",
        "item_prompt": "Why?",
        "shown_if": "mood = 1",
    }
    rows.insert(2, new)

    result, folder = run(server, rows=rows)
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.text and server.puts == []

    result, folder = run(server, "--yes", rows=rows)
    assert result.exit_code == 0, result.output
    assert len(server.puts) == 1
    body = server.puts[0]

    # Everything the dictionary doesn't touch is echoed exactly.
    assert body["encryption"] == ENCRYPTION
    for key in ("displayName", "description", "about", "themeId", "streamEnabled", "image", "watermark"):
        assert body[key] == original[key]
    assert body["activities"][1] == {**original["activities"][1], "key": OTHER}
    assert body["activityFlows"][0]["items"] == [
        {"id": original["activityFlows"][0]["items"][0]["id"], "activityKey": DAILY}
    ]
    daily = body["activities"][0]
    assert daily["key"] == DAILY and daily["subscaleSetting"] == original["activities"][0]["subscaleSetting"]

    items = {item["name"]: item for item in daily["items"]}
    assert [item["name"] for item in daily["items"]][:4] == ["mood", "feelings", "why", "energy"]
    assert [o["text"] for o in items["mood"]["responseValues"]["options"]] == ["Bad", "Great"]
    assert items["mood"]["id"] == original["activities"][0]["items"][0]["id"]
    assert items["notes"]["config"]["numericalResponseRequired"] is True
    assert items["feelings"]["conditionalLogic"]["conditions"][0]["payload"] == {"optionValue": "0"}
    assert "id" not in items["why"], "new items get their id from the server"
    assert items["why"]["conditionalLogic"]["conditions"][0] == {"itemName": "mood", "type": "EQUAL_TO_OPTION", "payload": {"optionValue": "1"}}  # fmt: skip
    for name in ("energy", "hours", "grid", "pain", "task", "voice"):
        assert items[name] == next(i for i in original["activities"][0]["items"] if i["name"] == name)

    plan = {(row["item"], row["action"]) for row in _plan(folder)}
    assert {("mood", "changed"), ("notes", "changed"), ("feelings", "changed"), ("why", "added")} <= plan
    assert ("energy", "unchanged") in plan
    info = json.loads((folder / "export_info.json").read_text())
    assert info["saved"] and info["applet"]["version_before"] == "1.2.0" and info["applet"]["version_after"] == "1.2.1"
    assert json.loads((folder / "applet_before.json").read_text()) == original
    assert "--restore" in result.text


def test_items_missing_from_the_file(run):
    rows = [r for r in dictionary_rows(applet(), "Daily") if r["item_name"] not in ("voice", "task")]
    rows[0]["item_prompt"] = "Mood today?"

    server = FakeBuilderServer()
    result, _ = run(server, "--yes", rows=rows)
    assert result.exit_code == 0, result.output
    names = [i["name"] for i in server.puts[0]["activities"][0]["items"]]
    assert names[-2:] == ["voice", "task"], "kept items stay where they were"

    server = FakeBuilderServer()
    result, folder = run(server, "--yes", "--remove-missing", rows=rows)
    assert result.exit_code == 0, result.output
    names = [i["name"] for i in server.puts[0]["activities"][0]["items"]]
    assert "voice" not in names and "task" not in names
    assert ("voice", "removed") in {(row["item"], row["action"]) for row in _plan(folder)}


def test_removing_an_item_reports_still_use_is_refused(run):
    server = FakeBuilderServer()
    rows = [r for r in dictionary_rows(server.applet, "Daily") if r["item_name"] != "notes"]
    result, folder = run(server, "--yes", "--remove-missing", rows=rows)
    assert result.exit_code == 2
    assert "prints 'notes'" in result.text and "Nothing was saved" in result.text
    assert server.puts == []
    assert (folder / "upload_problems.csv").is_file()


def test_new_activity_is_created(run):
    server = FakeBuilderServer()
    rows = [
        {"activity_name": "Baseline", "item_name": "age", "item_type": "numberSelect", "item_prompt": "Age?", "min_value": "10", "max_value": "99"},
        {"activity_name": "Baseline", "item_name": "school", "item_type": "singleSelect", "item_prompt": "School?", "value_labels": "1=Yes | 0=No", "shown_if": "age > 17"},
    ]  # fmt: skip
    result, _ = run(server, "--yes", rows=rows)
    assert result.exit_code == 0, result.output
    activities = server.puts[0]["activities"]
    assert [a["name"] for a in activities] == ["Daily", "Weekly", "Baseline"]
    baseline = activities[2]
    assert "id" not in baseline and len(baseline["key"]) == 36
    assert [i["name"] for i in baseline["items"]] == ["age", "school"]
    assert baseline["items"][1]["conditionalLogic"]["conditions"][0] == {"itemName": "age", "type": "GREATER_THAN", "payload": {"value": 17}}  # fmt: skip
    assert activities[0] == update_body(applet())["activities"][0]
    assert [a["name"] for a in server.applet["activities"]] == ["Daily", "Weekly", "Baseline"]


def test_problems_block_the_upload(run):
    server = FakeBuilderServer()
    rows = dictionary_rows(server.applet, "Daily")
    rows[0]["shown_if"] = "energy > 2"  # energy comes later
    rows[1]["value_labels"] = "Sad | None"
    result, _ = run(server, "--yes", rows=rows)
    assert result.exit_code == 2
    assert "not an earlier item" in result.text and "value=label" in result.text
    assert server.puts == []


def test_role_stale_applet_and_server_refusal(run):
    rows = dictionary_rows(applet(), "Daily")
    rows[0]["item_prompt"] = "Mood now?"

    result, _ = run(FakeBuilderServer(role="reviewer"), "--yes", rows=rows)
    assert result.exit_code == 2 and "needs owner, manager, or editor" in result.text

    server = FakeBuilderServer()
    server.bump_before_put = True
    result, _ = run(server, "--yes", rows=rows)
    assert result.exit_code == 2 and "changed on the server" in result.text
    assert server.puts == []

    server = FakeBuilderServer()
    server._check = lambda body: "Something the backend rejects"
    result, folder = run(server, "--yes", rows=rows)
    assert result.exit_code == 2 and "nothing was saved" in result.text
    assert json.loads((folder / "export_info.json").read_text())["saved"] is False


def test_restore_puts_the_applet_back(run, tmp_path):
    server = FakeBuilderServer()
    rows = dictionary_rows(server.applet, "Daily")
    rows[0]["item_prompt"] = "Changed prompt"
    result, folder = run(server, "--yes", rows=rows)
    assert result.exit_code == 0, result.output
    assert server.applet["activities"][0]["items"][0]["question"] == {"en": "Changed prompt"}

    backup = folder / "applet_before.json"
    result, _ = run(server, "--restore", str(backup))
    assert result.exit_code == 0 and "Dry run" in result.text and len(server.puts) == 1
    result, _ = run(server, "--restore", str(backup), "--yes")
    assert result.exit_code == 0, result.output
    assert len(server.puts) == 2
    assert server.puts[1] == update_body(json.loads(backup.read_text()))
    assert server.applet["activities"][0]["items"][0]["question"] == {"en": "How is your mood?"}
    assert server.applet["version"] == "1.2.2"


def test_upload_needs_an_applet_and_valid_files(run, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("name,type\nx,text\n")
    result, _ = run(FakeBuilderServer(), "--upload", str(bad), "--applet", "EMA Study")
    assert result.exit_code == 2 and "isn't a data dictionary" in result.text
    result, _ = run(FakeBuilderServer(), "--upload", str(bad))
    assert result.exit_code == 2
    result, _ = run(FakeBuilderServer(), "--upload", str(bad), "--dictionary")
    assert result.exit_code == 2 and "at a time" in result.text
    assert APPLET_ID


def _score_row(rows: list[dict], name: str) -> dict:
    return next(row for row in rows if row["item_name"] == name)


def test_subscales_are_edited_and_added(run):
    server = FakeBuilderServer()
    rows = dictionary_rows(server.applet, "Daily")
    assert [r["item_name"] for r in rows[-3:]] == ["activity_score", "activity_score_lookup_text", "subscale_name_Core"]
    core = _score_row(rows, "subscale_name_Core")
    assert core["item_prompt"] == "Subscale Core: Sum of mood, energy"
    core["item_prompt"] = "Subscale Core: Average of mood, energy"
    core["value_labels"] = "raw 0~2 -> 50 (sex F, age 8~12, Mild): Low | raw 2~6 -> 60: High"
    _score_row(rows, "activity_score")["item_prompt"] = "Total score: Sum of mood as a percentage of its maximum"
    rows.append({**core, "item_name": "subscale_name_Extra", "item_prompt": "Subscale Extra: Sum of mood, Core (subscale)", "value_labels": ""})  # fmt: skip

    result, folder = run(server, "--yes", rows=rows)
    assert result.exit_code == 0, result.output
    setting = server.puts[0]["activities"][0]["subscaleSetting"]
    assert setting["calculateTotalScore"] == "percentage"
    assert setting["subscales"] == [
        {
            "name": "Core",
            "scoring": "average",
            "items": [{"name": "mood", "type": "item"}, {"name": "energy", "type": "item"}],
            "subscaleTableData": [
                {
                    "rawScore": "0~2",
                    "score": "50",
                    "sex": "F",
                    "age": "8~12",
                    "severity": "Mild",
                    "optionalText": "Low",
                },
                {"rawScore": "2~6", "score": "60", "optionalText": "High"},
            ],
        },
        {
            "name": "Extra",
            "scoring": "sum",
            "items": [{"name": "mood", "type": "item"}, {"name": "Core", "type": "subscale"}],
            "subscaleTableData": None,
        },
    ]
    plan = {(row["item"], row["action"]) for row in _plan(folder)}
    assert {("subscale Core", "changed"), ("subscale Extra", "added"), ("total score", "changed")} <= plan

    # Downloading what was saved and uploading it again changes nothing.
    again = FakeBuilderServer()
    again.applet = server.applet
    result, _ = run(again, "--yes", rows=dictionary_rows(again.applet, "Daily"))
    assert result.exit_code == 0 and again.puts == [], result.output


def test_subscales_missing_from_the_file(run):
    rows = [r for r in dictionary_rows(applet(), "Daily") if r["item_name"] != "subscale_name_Core"]
    rows[0]["item_prompt"] = "Mood today?"
    server = FakeBuilderServer()
    result, _ = run(server, "--yes", rows=rows)
    assert result.exit_code == 0, result.output
    assert [s["name"] for s in server.puts[0]["activities"][0]["subscaleSetting"]["subscales"]] == ["Core"]

    server = FakeBuilderServer()
    result, folder = run(server, "--yes", "--remove-missing", rows=rows)
    assert result.exit_code == 0, result.output
    assert server.puts[0]["activities"][0]["subscaleSetting"]["subscales"] == []
    assert ("subscale Core", "removed") in {(row["item"], row["action"]) for row in _plan(folder)}


def test_subscale_problems_block_the_upload(run):
    server = FakeBuilderServer()
    rows = dictionary_rows(server.applet, "Daily")
    rows.append({**_score_row(rows, "subscale_name_Core"), "item_name": "subscale_name_Bad", "item_prompt": "Subscale Bad: Sum of feelings"})  # fmt: skip
    rows.append({**_score_row(rows, "subscale_name_Core"), "item_name": "subscale_name_Odd", "item_prompt": "Subscale Odd: Product of mood"})  # fmt: skip
    result, _ = run(server, "--yes", rows=rows)
    assert result.exit_code == 2
    assert "uses 'feelings', which is gone or no longer scored" in result.text
    assert "'Subscale NAME: Sum of a, b, c'" in result.text
    assert server.puts == []
