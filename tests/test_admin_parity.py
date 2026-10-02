"""The Python port must produce the same responses.csv cells as the admin panel's own code.

tests/js_oracle/admin_export.js holds the admin functions copied verbatim (types removed);
this test runs both implementations on the same synthetic submissions and compares
every row, cell, and column order.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from curious_download.csvout import sanitize_value
from curious_download.export import decrypt_records, rows_for_submission

from .corpus import submissions

ORACLE = Path(__file__).parent / "js_oracle" / "admin_export.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed for the admin reference")


def _admin_rows(cases: list[dict], null_when_skipped: bool) -> list[list[dict]]:
    payload = json.dumps({"submissions": cases, "nullWhenSkipped": null_when_skipped})
    result = subprocess.run(["node", str(ORACLE)], input=payload, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _python_rows(case: dict, null_when_skipped: bool) -> list[dict]:
    records = decrypt_records(case["answer"], case["activity"], case["answersDecrypted"])
    rows = rows_for_submission(records, null_when_skipped=null_when_skipped)
    return [{key: sanitize_value(value) for key, value in row.items()} for row in rows]


@pytest.mark.parametrize("null_when_skipped", [False, True])
def test_rows_match_admin_export(null_when_skipped):
    cases = submissions()
    expected = _admin_rows(cases, null_when_skipped)
    assert len(expected) == len(cases)

    for case, admin_rows in zip(cases, expected, strict=True):
        python_rows = _python_rows(case, null_when_skipped)
        label = case["answer"]["id"]
        assert len(python_rows) == len(admin_rows), f"{label}: row count"
        for index, (mine, theirs) in enumerate(zip(python_rows, admin_rows, strict=True)):
            assert list(mine) == list(theirs), f"{label} row {index}: column order"
            for column in theirs:
                assert mine[column] == theirs[column], f"{label} row {index} column {column}"


def test_corpus_exercises_every_response_type():
    seen = {
        record["activityItem"]["responseType"]
        for case in submissions()
        for record in decrypt_records(case["answer"], case["activity"], case["answersDecrypted"])
    }
    expected = {
        "singleSelect", "multiSelect", "slider", "numberSelect", "text", "paragraphText", "date", "time",
        "timeRange", "geolocation", "photo", "video", "audio", "audioPlayer", "drawing", "singleSelectRows",
        "multiSelectRows", "sliderRows", "ABTrails", "stabilityTracker", "flanker", "unity",
        "requestHealthRecordData", "message",
    }  # fmt: skip
    assert expected <= seen
