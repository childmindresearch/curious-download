"""The interactive questions of --convert-redcap and --upload, answered by a script."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from curious_download import cli, upload_cli
from curious_download.client import CuriousClient
from curious_download.csvout import write_csv
from curious_download.dictionary import DICTIONARY_COLUMNS

from .full_applet import FakeBuilderServer
from .test_redcap import HEADER, ROWS
from .test_upload import dictionary_rows


class Script:
    """Stands in for questionary: each prompt is answered by the first rule whose text appears in it."""

    def __init__(self, rules: list[tuple[str, object]]):
        self.rules = rules
        self.asked: list[str] = []

    def _question(self, message: str, choices=None, default=None):
        self.asked.append(message)
        for text, answer in self.rules:
            if text in message:
                value = answer(choices) if callable(answer) else answer
                return SimpleNamespace(unsafe_ask=lambda value=value: value)
        if choices:  # the recommended (first) choice
            return SimpleNamespace(unsafe_ask=lambda: choices[0].value)
        return SimpleNamespace(unsafe_ask=lambda: default)

    def select(self, message, choices, **_):
        return self._question(message, choices)

    def checkbox(self, message, choices, **_):
        self.asked.append(message)
        for text, answer in self.rules:
            if text in message:
                return SimpleNamespace(unsafe_ask=lambda answer=answer: answer)
        return SimpleNamespace(unsafe_ask=lambda: [c.value for c in choices if c.checked])

    def text(self, message, default="", **_):
        return self._question(message, default=default)

    def confirm(self, message, default=False, **_):
        return self._question(message, default=default)

    def path(self, message, **_):
        return self._question(message)

    Choice = staticmethod(
        lambda title, value=None, checked=False: SimpleNamespace(title=title, value=value, checked=checked)
    )


@pytest.fixture
def quiet(monkeypatch):
    printed = io.StringIO()
    monkeypatch.setattr(upload_cli, "console", cli.Console(file=printed, width=200))
    return printed


def test_conversion_questions(monkeypatch, tmp_path, quiet):
    import csv

    path = tmp_path / "Study.csv"
    with path.open("w", newline="") as fh:
        csv.writer(fh).writerows([HEADER, *ROWS])
    script = Script([("matrix groups", "grid"), ("activity name for the REDCap form 'baseline'", "Intake")])
    monkeypatch.setattr(upload_cli, "questionary", script)

    folder = upload_cli.run_convert(path, None, tmp_path / "out", interactive=True)
    decisions = json.loads((folder / "redcap_decisions.json").read_text())
    assert decisions["matrix"] == "grid" and decisions["activity_name:baseline"] == "Intake"
    assert (folder / "Study__Intake__data_dictionary.csv").is_file()
    # Every decision the file needs was asked once, with the recommended option marked.
    assert len(script.asked) == len(decisions)
    assert "--decisions" in quiet.getvalue()


def test_upload_questions(monkeypatch, tmp_path, quiet):
    server = FakeBuilderServer()
    rows = [r for r in dictionary_rows(server.applet, "Daily") if r["item_name"] != "voice"]
    rows[0]["item_prompt"] = "Mood right now?"
    source = tmp_path / "daily.csv"
    write_csv(source, rows, DICTIONARY_COLUMNS)

    def pick_applet(choices):
        return choices[0].value

    script = Script([("Upload into which applet", pick_applet), ("What should happen to them", "remove"), ("Save these changes", True)])  # fmt: skip
    monkeypatch.setattr(upload_cli, "questionary", script)
    monkeypatch.setattr(cli, "questionary", script)
    with CuriousClient("https://api.curious.test", transport=server.transport()) as client:
        client.login("r@example.org", "pw")
        _, activities = upload_cli.load_upload(source, interactive=True)
        upload_cli.run_upload(
            client, "https://api.curious.test", "r@example.org", source, activities,
            None, None, tmp_path / "out", interactive=True, yes=False, remove_missing=None,
        )  # fmt: skip
    assert script.asked == [
        "Upload into which applet?",
        "What should happen to them?",
        "Save these changes?",
    ]
    names = [i["name"] for i in server.puts[0]["activities"][0]["items"]]
    assert "voice" not in names
    assert "Saved." in quiet.getvalue() and "voice" in quiet.getvalue()


def test_upload_not_confirmed_saves_nothing(monkeypatch, tmp_path, quiet):
    server = FakeBuilderServer()
    rows = dictionary_rows(server.applet, "Daily")
    rows[0]["item_prompt"] = "Mood right now?"
    source = tmp_path / "daily.csv"
    write_csv(source, rows, DICTIONARY_COLUMNS)
    monkeypatch.setattr(upload_cli, "questionary", Script([("Save these changes", False)]))
    with CuriousClient("https://api.curious.test", transport=server.transport()) as client:
        client.login("r@example.org", "pw")
        _, activities = upload_cli.load_upload(source, interactive=True)
        upload_cli.run_upload(
            client, "https://api.curious.test", "r@example.org", source, activities,
            "EMA Study", None, tmp_path / "out", interactive=True, yes=False, remove_missing=None,
        )  # fmt: skip
    assert server.puts == []
    assert "Not saved" in quiet.getvalue()
