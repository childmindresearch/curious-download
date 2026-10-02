"""The Python port must produce the same output as the admin panel's own code.

tests/js_oracle/admin_export.js holds the admin functions copied verbatim (types removed);
this test runs both implementations on the same synthetic submissions and compares
every row, cell, and column order of responses.csv, and the files (names and URLs)
the admin would put in its media and unity zips.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from curious_download.csvout import sanitize_value
from curious_download.export import prepare_records, rows_for_submission
from curious_download.media import MEDIA_FOLDER, UNITY_FOLDER, collect_media

from .corpus import submissions

ORACLE = Path(__file__).parent / "js_oracle" / "admin_export.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed for the admin reference")


def _admin_output(cases: list[dict], null_when_skipped: bool) -> list[dict]:
    payload = json.dumps({"submissions": cases, "nullWhenSkipped": null_when_skipped})
    result = subprocess.run(["node", str(ORACLE)], input=payload, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _python_rows(case: dict, null_when_skipped: bool) -> list[dict]:
    records = prepare_records(case["answer"], case["activity"], case["answersDecrypted"])
    rows = rows_for_submission(records, null_when_skipped=null_when_skipped)
    return [{key: sanitize_value(value) for key, value in row.items()} for row in rows]


def _python_media(case: dict, folder: str) -> list[dict]:
    records = prepare_records(case["answer"], case["activity"], case["answersDecrypted"])
    return [
        {"fileName": file.name, "url": file.url if file.url is not None else f"inline:{file.inline}"}
        for file in collect_media(records)
        if file.folder == folder
    ]


@pytest.mark.parametrize("null_when_skipped", [False, True])
def test_rows_match_admin_export(null_when_skipped):
    cases = submissions()
    expected = _admin_output(cases, null_when_skipped)
    assert len(expected) == len(cases)

    for case, admin in zip(cases, expected, strict=True):
        python_rows = _python_rows(case, null_when_skipped)
        label = case["answer"]["id"]
        assert len(python_rows) == len(admin["rows"]), f"{label}: row count"
        for index, (mine, theirs) in enumerate(zip(python_rows, admin["rows"], strict=True)):
            assert list(mine) == list(theirs), f"{label} row {index}: column order"
            for column in theirs:
                assert mine[column] == theirs[column], f"{label} row {index} column {column}"


def test_media_files_match_admin_export():
    cases = submissions()
    expected = _admin_output(cases, False)
    total = 0
    for case, admin in zip(cases, expected, strict=True):
        label = case["answer"]["id"]
        assert _python_media(case, MEDIA_FOLDER) == admin["media"], f"{label}: media zip"
        assert _python_media(case, UNITY_FOLDER) == admin["unity"], f"{label}: unity zip"
        total += len(admin["media"]) + len(admin["unity"])
    assert total >= 10, "the corpus should exercise media"


def test_corpus_exercises_every_response_type():
    seen = {
        record["activityItem"]["responseType"]
        for case in submissions()
        for record in prepare_records(case["answer"], case["activity"], case["answersDecrypted"])
    }
    expected = {
        "singleSelect", "multiSelect", "slider", "numberSelect", "text", "paragraphText", "date", "time",
        "timeRange", "geolocation", "photo", "video", "audio", "audioPlayer", "drawing", "singleSelectRows",
        "multiSelectRows", "sliderRows", "ABTrails", "stabilityTracker", "flanker", "unity",
        "requestHealthRecordData", "message",
    }  # fmt: skip
    assert expected <= seen
