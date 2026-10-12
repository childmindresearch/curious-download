"""Upload bodies checked by the backend's own request model, when its code and dependencies are available.

Set CURIOUS_BACKEND_PYTHON to a Python with the backend's packages (pydantic, pydantic-extra-types, nh3,
requests, starlette, fastapi, pydantic-settings). The backend source is looked up next to this repo in
mindlogger-backend-refactor-develop/src, or in CURIOUS_BACKEND_SRC. Skipped otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from curious_download.itembuilder import ActivityRows
from curious_download.upload import plan_upload

from .full_applet import applet
from .test_itembuilder import _row
from .test_upload import dictionary_rows

BACKEND_PYTHON = os.environ.get("CURIOUS_BACKEND_PYTHON")
BACKEND_SRC = Path(
    os.environ.get("CURIOUS_BACKEND_SRC") or Path(__file__).parents[2] / "mindlogger-backend-refactor-develop" / "src"
)

pytestmark = pytest.mark.skipif(
    not BACKEND_PYTHON or not (BACKEND_SRC / "apps").is_dir(),
    reason="set CURIOUS_BACKEND_PYTHON to a Python with the backend's dependencies",
)

EVERY_TYPE = [
    {"item_name": "a", "item_type": "singleSelect", "value_labels": "1=Yes | 0=No", "scores": "1=1 | 0=0"},
    {"item_name": "b", "item_type": "multiSelect", "value_labels": "1=A | 9=None (none of the above)", "shown_if": 'a = "Yes" (1)'},
    {"item_name": "c", "item_type": "slider", "min_value": 0, "max_value": 4, "scores": "0=0 | 1=1 | 2=2 | 3=3 | 4=4", "shown_if": "b includes 1"},
    {"item_name": "d", "item_type": "numberSelect", "min_value": 0, "max_value": 100, "shown_if": "c between 2 and 3"},
    {"item_name": "e", "item_type": "text", "response_rules": "max 50 characters; numbers only; correct answer: 42", "shown_if": "d > 3"},
    {"item_name": "f", "item_type": "paragraphText", "response_rules": "response required"},
    {"item_name": "g", "item_type": "date"},
    {"item_name": "h", "item_type": "time", "shown_if": "g > 2026-01-01"},
    {"item_name": "i", "item_type": "timeRange", "shown_if": "h between 08:00 and 09:30"},
    {"item_name": "j", "item_type": "message", "shown_if": "i (start) > 21:00"},
    {"item_name": "k", "item_type": "sliderRows", "rows": "Head (0=none to 2=worst) | Back (1 to 2)", "scores": "Head: 0=0, 1=1, 2=2 | Back: 1=0, 2=1", "shown_if": "i (end) not between 06:00 and 07:00"},
    {"item_name": "l", "item_type": "singleSelectRows", "rows": "Morning | Evening", "value_labels": "Calm | Tense", "scores": "Morning: Calm=0, Tense=1 | Evening: Calm=0, Tense=2", "shown_if": "k [Back] > 1"},
    {"item_name": "m", "item_type": "multiSelectRows", "rows": "Mon | Tue", "value_labels": "Run | Swim", "shown_if": 'l [Evening] = "Tense"'},
    {"item_name": "n", "item_type": "audio", "response_rules": "max 30 seconds", "shown_if": 'm [Tue] includes "Swim"'},
    {"item_name": "o", "item_type": "photo", "other_text_option": "required"},
    {"item_name": "p", "item_type": "video"},
    {"item_name": "q", "item_type": "drawing"},
    {"item_name": "r", "item_type": "geolocation", "hidden": "yes"},
]  # fmt: skip


def _redcap_activities(tmp_path: Path) -> list[ActivityRows]:
    """The synthetic REDCap codebook of test_redcap.py, converted with the recommended answers."""
    import csv

    from curious_download.itembuilder import dictionary_files, read_dictionaries
    from curious_download.redcap import Decisions, convert_redcap, write_conversion

    from .test_redcap import HEADER, ROWS

    source = tmp_path / "codebook.csv"
    with source.open("w", newline="") as fh:
        csv.writer(fh).writerows([HEADER, *ROWS])
    folder = tmp_path / "converted"
    write_conversion(convert_redcap(source, Decisions()), folder, source)
    return read_dictionaries(dictionary_files(folder))


def _bodies(tmp_path: Path) -> list[dict]:
    from curious_download.itembuilder import SCORE_ROW_TYPES, ItemRow

    rows = dictionary_rows(applet(), "Daily")
    rows[0]["value_labels"] = "0=Bad | 1=Great"
    core = next(r for r in rows if r["item_name"] == "subscale_name_Core")
    core["item_prompt"] = "Subscale Core: Average of mood, energy"
    core["value_labels"] = "raw 0~2 -> 50 (sex F, age 8~12, Mild): Low"
    rows.insert(2, {"activity_name": "Daily", "item_name": "why", "item_type": "text", "shown_if": "mood = 1"})
    as_rows = [ItemRow({k: str(v) for k, v in r.items()}, "") for r in rows]
    edits = ActivityRows(
        "Daily",
        rows[0]["activity_id"],
        [r for r in as_rows if r.cells["item_type"] not in SCORE_ROW_TYPES],
        [r for r in as_rows if r.cells["item_type"] in SCORE_ROW_TYPES],
    )
    new = ActivityRows("Every type", "", [_row(activity_name="Every type", **cells) for cells in EVERY_TYPE])
    bodies = []
    for activities in ([edits], [new], _redcap_activities(tmp_path)):
        plan = plan_upload(applet(), activities, lambda activity, names: "keep")
        assert not plan.errors, plan.errors
        bodies.append(plan.body)
    return bodies


def test_backend_accepts_upload_bodies(tmp_path):
    env = {
        **os.environ,
        # Placeholder secrets from the backend's .env.default; its settings module needs them to import.
        "AUTHENTICATION__ACCESS_TOKEN__SECRET_KEY": "secret1",
        "AUTHENTICATION__REFRESH_TOKEN__SECRET_KEY": "secret2",
        "AUTHENTICATION__MFA_TOKEN__SECRET_KEY": "secret3",
    }
    result = subprocess.run(
        [BACKEND_PYTHON, "-I", str(Path(__file__).with_name("backend_validate.py")), str(BACKEND_SRC)],
        input=json.dumps(_bodies(tmp_path)),
        capture_output=True,
        text=True,
        env=env,
        cwd=BACKEND_SRC,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\n")[:3] == ["0 OK", "1 OK", "2 OK"], result.stdout
