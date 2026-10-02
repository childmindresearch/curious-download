"""End-to-end runs of `curious-download --no-input` against a fake Curious server."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from curious_download import cli
from curious_download.client import CuriousClient
from curious_download.export import prepare_records, rows_for_submission
from curious_download.report import REPORT_COLUMNS

from . import corpus
from .fake_server import APPLET_PASSWORD, EMAIL, PASSWORD, FakeCurious

SERVER = "https://api.curious.test"


@pytest.fixture
def run(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SETTINGS_PATH", tmp_path / "settings.json")
    app = typer.Typer()
    app.command()(cli.download)

    def invoke(server: FakeCurious, *args: str, applet_password: str = APPLET_PASSWORD):
        monkeypatch.setattr(cli, "CuriousClient", lambda url: CuriousClient(url, transport=server.transport()))
        env = {"CURIOUS_PASSWORD": PASSWORD, "CURIOUS_APPLET_PASSWORD": applet_password}
        base = [
            "--server",
            SERVER,
            "--email",
            EMAIL,
            "--applet",
            "EMA Study",
            "--output",
            str(tmp_path / "out"),
            "--no-input",
        ]
        result = CliRunner().invoke(app, [*base, *args], env=env)
        exports = sorted((tmp_path / "out").glob("*/responses.csv")) if result.exit_code == 0 else []
        return result, (exports[-1].parent if exports else None)

    return invoke


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "admin CSVs start with a UTF-8 BOM"
    assert b"\r\n" not in raw
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    return list(reader.fieldnames or []), list(reader)


def _expected_rows(server: FakeCurious, answer_ids: list[str]) -> list[dict]:
    """What the admin would write for these answers, using the parity-tested row builder."""
    from curious_download.csvout import sanitize_value

    rows = []
    for answer in server.answers:
        if answer["id"] in answer_ids:
            activity = server.activities[answer["activityHistoryId"]]
            records = prepare_records(answer, activity, server.plain_answers[answer["id"]])
            rows += [{k: sanitize_value(v) for k, v in row.items()} for row in rows_for_submission(records)]
    return rows


def test_full_download(run):
    server = FakeCurious()
    result, export_dir = run(server)
    assert result.exit_code == 0, result.output

    header, rows = _read_csv(export_dir / "responses.csv")
    assert header[: len(REPORT_COLUMNS)] == REPORT_COLUMNS
    assert {r["activity_submission_id"] for r in rows} == {"e1", "e2", "e3", "e4", "e5"}
    expected = _expected_rows(server, ["e1", "e2", "e3", "e4", "e5"])
    assert [{k: r.get(k, "") for k in header} for r in expected] == rows

    info = json.loads((export_dir / "export_info.json").read_text())
    assert info["answers_downloaded"] == 5
    assert info["rows_written"] == len(rows)
    assert info["decryption_failures"] == []
    assert APPLET_PASSWORD not in json.dumps(info) and PASSWORD not in json.dumps(info)


def test_participant_date_and_activity_filters(run):
    server = FakeCurious()
    result, export_dir = run(
        server, "--participant", "P001", "--activity", "scored", "--from", "2026-09-01", "--to", "2026-09-30"
    )
    assert result.exit_code == 0, result.output
    _, rows = _read_csv(export_dir / "responses.csv")
    # P001 + Scored + September leaves e2 and e4 (e3 is P002, e1 is another activity, e5 is in August).
    assert sorted({r["activity_submission_id"] for r in rows}) == ["e2", "e4"]
    assert {r["target_secret_id"] for r in rows} == {"P001"}

    data_requests = [r for r in server.requests if r.url.path.endswith("/data")]
    query = str(data_requests[0].url)
    assert f"targetSubjectIds={corpus.SUBJECT_ID}" in query
    assert "fromDate=" in query and "toDate=" in query

    info = json.loads((export_dir / "export_info.json").read_text())
    assert info["filters"]["participants"] == ["P001"]
    assert info["answers_downloaded"] == 3  # e1, e2, e4 match on the server; e1 is dropped client-side
    assert info["answers_after_activity_filter"] == 2


def test_pagination_and_token_refresh(run):
    server = FakeCurious(expire_first_data_token=True)
    result, export_dir = run(server, "--page-size", "2")
    assert result.exit_code == 0, result.output
    pages = [r for r in server.requests if r.url.path.endswith("/data")]
    assert len(pages) == 1 + 3  # one rejected with 401, then 3 pages of 2
    assert any(r.url.path == "/auth/token/refresh" for r in server.requests)
    _, rows = _read_csv(export_dir / "responses.csv")
    assert {r["activity_submission_id"] for r in rows} == {"e1", "e2", "e3", "e4", "e5"}


def test_mfa_login(run):
    server = FakeCurious(mfa=True)
    result, _ = run(server, "--mfa-code", "123456")
    assert result.exit_code == 0, result.output

    result, _ = run(FakeCurious(mfa=True))
    assert result.exit_code == 2
    assert "MFA code" in result.output or "mfa-code" in result.output


def test_wrong_applet_password(run):
    result, export_dir = run(FakeCurious(), applet_password="nope")
    assert result.exit_code == 2
    assert "applet password is incorrect" in result.output
    assert export_dir is None


def test_unknown_participant_and_activity(run):
    result, _ = run(FakeCurious(), "--participant", "P999")
    assert result.exit_code == 2 and "P999" in result.output
    result, _ = run(FakeCurious(), "--activity", "Nope")
    assert result.exit_code == 2 and "Daily check-in" in result.output


def test_output_feeds_post_processing_tool(run):
    """The existing post-processing tool reads *responses*.csv; check its required columns are present."""
    result, export_dir = run(FakeCurious())
    assert result.exit_code == 0, result.output
    sample = (
        Path(__file__).parents[2] / "mindlogger-data-export-post-processing-main" / "tests" / "data" / "responses.csv"
    )
    if not sample.exists():
        pytest.skip("post-processing sample not available")
    sample_header = next(csv.reader(io.StringIO(sample.read_text(encoding="utf-8-sig"))))
    header, _ = _read_csv(export_dir / "responses.csv")
    assert header[: len(sample_header)] == sample_header


def test_media_download(run):
    server = FakeCurious()
    result, export_dir = run(server, "--media")
    assert result.exit_code == 0, result.output

    # e1 answers every item type; its media goes where responses.csv says it is.
    _, rows = _read_csv(export_dir / "responses.csv")
    cells = {r["item_name"]: r["item_response"] for r in rows if r["activity_submission_id"] == "e1"}
    for item in ("selfie", "clip", "voice", "sketch"):
        assert (export_dir / "media" / cells[item]).read_bytes().startswith(b"bytes of /")
    for name in cells["game"].split(", "):
        assert (export_dir / "unity" / name).is_file()

    _, manifest = _read_csv(export_dir / "media_files.csv")
    assert len(manifest) == 6
    assert {m["status"] for m in manifest} == {"downloaded"}
    assert all("authorization" not in r.headers for r in server.storage_requests)

    info = json.loads((export_dir / "export_info.json").read_text())
    assert info["media"] == {"requested": True, "files": 6, "downloaded": 6, "saved_from_answer": 0, "failed": 0}


def test_media_requested_but_none_found(run):
    server = FakeCurious()
    result, export_dir = run(server, "--media", "--activity", "Scored")
    assert result.exit_code == 0, result.output
    _, manifest = _read_csv(export_dir / "media_files.csv")
    assert manifest == []
    assert not (export_dir / "media").exists()


def test_no_media_by_default(run):
    server = FakeCurious()
    result, export_dir = run(server)
    assert result.exit_code == 0, result.output
    assert not (export_dir / "media").exists() and not (export_dir / "media_files.csv").exists()
    assert not any(r.url.path.endswith("/presign") for r in server.requests)
    assert json.loads((export_dir / "export_info.json").read_text())["media"]["requested"] is False
