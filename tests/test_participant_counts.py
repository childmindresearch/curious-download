"""`curious-participant-counts --no-input` against a small fake Curious server."""

from __future__ import annotations

import csv
import io
from urllib.parse import parse_qs

import httpx
import pytest
import typer
from typer.testing import CliRunner

from curious_download import cli, participant_counts
from curious_download.client import CuriousClient

SERVER = "https://api.curious.test"
EMAIL = "researcher@example.org"
PASSWORD = "account-pw"
OWNER_ID = "66666666-6666-4666-8666-666666666666"
OTHER_OWNER_ID = "99999999-9999-4999-8999-999999999999"

EMA = "a0000000-0000-4000-8000-000000000001"
FAMILY = "a0000000-0000-4000-8000-000000000002"
REVIEWED = "a0000000-0000-4000-8000-000000000003"
EDITED = "a0000000-0000-4000-8000-000000000004"
EMPTY = "a0000000-0000-4000-8000-000000000005"

DAILY = "c0000000-0000-4000-8000-000000000001"
ASSESSMENT = "c0000000-0000-4000-8000-000000000002"


def _submission(respondent: str, target: str, created: str, activity: str = DAILY) -> dict:
    return {
        "respondentSubjectId": respondent,
        "targetSubjectId": target,
        "sourceSubjectId": respondent,
        "activityId": activity,
        "activityName": "Daily",
        "createdAt": created,
        "updatedAt": created,
    }


class FakeServer:
    def __init__(self, *, has_submissions_endpoint: bool = True):
        self.has_submissions_endpoint = has_submissions_endpoint
        self.requests: list[httpx.Request] = []
        self.applets = [
            {"id": EMA, "displayName": "EMA Study", "role": "owner", "type": "applet"},
            {"id": FAMILY, "displayName": "Family Study", "role": "manager", "type": "applet"},
            {"id": REVIEWED, "displayName": "Clinic", "role": "reviewer", "type": "applet"},
            {"id": EDITED, "displayName": "Draft", "role": "editor", "type": "applet"},
            {"id": EMPTY, "displayName": "Brand New", "role": "owner", "type": "applet"},
        ]
        self.submissions = {
            # Three people, one of whom submitted three times.
            EMA: [
                _submission("s1", "s1", "2026-09-03T08:00:00.123456"),
                _submission("s2", "s2", "2026-09-02T08:00:00"),
                _submission("s1", "s1", "2026-09-01T08:00:00"),
                _submission("s3", "s3", "2026-08-15T08:00:00"),
                _submission("s1", "s1", "2026-08-01T08:00:00"),
            ],
            # Two parents answer about two children; one child also answers about themself.
            FAMILY: [
                _submission("parent1", "child1", "2026-09-05T08:00:00"),
                _submission("parent2", "child2", "2026-09-04T08:00:00"),
                _submission("child1", "child1", "2026-09-03T08:00:00"),
            ],
            # One participant, plus an assessment by a reviewer (counted against the reviewer's own subject).
            REVIEWED: [
                _submission("reviewer", "reviewer", "2026-09-07T08:00:00", activity=ASSESSMENT),
                _submission("p1", "p1", "2026-09-06T08:00:00"),
            ],
            EMPTY: [],
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = parse_qs(request.url.query.decode())
        if path == "/auth/login":
            return httpx.Response(200, json={"result": {"token": {"accessToken": "t", "refreshToken": "r"}}})
        if path == "/workspaces":
            workspaces = [
                {"ownerId": OWNER_ID, "workspaceName": "Lab"},
                {"ownerId": OTHER_OWNER_ID, "workspaceName": "Other Lab"},
            ]
            return httpx.Response(200, json={"result": workspaces, "count": 2})
        if path == f"/workspaces/{OWNER_ID}/applets":
            return httpx.Response(200, json={"result": self.applets, "count": len(self.applets)})
        for applet_id in self.submissions:
            if path == f"/applets/{applet_id}":
                activities = [{"id": DAILY, "isReviewable": False}, {"id": ASSESSMENT, "isReviewable": True}]
                return httpx.Response(200, json={"result": {"id": applet_id, "activities": activities}})
        applet_id = path.split("/")[3] if path.startswith("/answers/applet/") else None
        if applet_id == EDITED:
            return httpx.Response(403, json={"result": [{"message": "Access denied to view answers."}]})
        if path.endswith("/submissions") and applet_id in self.submissions:
            if not self.has_submissions_endpoint:
                return httpx.Response(404, json={"detail": "Not Found"})
            rows = self._filtered(applet_id, params)
            page, limit = int(params["page"][0]), int(params["limit"][0])
            body = {"submissions": rows[(page - 1) * limit : page * limit], "submissionsCount": len(rows)}
            return httpx.Response(200, json={**body, "participantsCount": 99})
        if path.endswith("/data") and applet_id in self.submissions:
            # The export endpoint: respondents by user ID, assessments flagged with reviewedAnswerId.
            rows = self._filtered(applet_id, params)
            answers = [
                {
                    "respondentId": f"user-{row['respondentSubjectId']}",
                    "targetSubjectId": row["targetSubjectId"],
                    "reviewedAnswerId": "x" if row["activityId"] == ASSESSMENT else None,
                    "createdAt": row["createdAt"],
                }
                for row in rows
            ]
            page, limit = int(params["page"][0]), int(params["limit"][0])
            result = {"answers": answers[(page - 1) * limit : page * limit], "activities": []}
            return httpx.Response(200, json={"result": result, "count": len(answers)})
        return httpx.Response(404, json={"result": [{"message": f"No route {path}"}]})

    def _filtered(self, applet_id: str, params: dict) -> list[dict]:
        rows = self.submissions[applet_id]
        if start := params.get("fromDate"):
            rows = [row for row in rows if row["createdAt"] >= start[0]]
        if end := params.get("toDate"):
            rows = [row for row in rows if row["createdAt"] <= end[0]]
        return rows


@pytest.fixture
def run(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SETTINGS_PATH", tmp_path / "settings.json")
    app = typer.Typer()
    app.command()(participant_counts.participant_counts)

    def invoke(server: FakeServer, *args: str) -> tuple:
        monkeypatch.setattr(
            participant_counts, "CuriousClient", lambda url: CuriousClient(url, transport=server.transport())
        )
        base = ["--server", SERVER, "--email", EMAIL, "--output", str(tmp_path / "out"), "--no-input"]
        result = CliRunner().invoke(app, [*base, *args], env={"CURIOUS_PASSWORD": PASSWORD})
        files = sorted((tmp_path / "out").glob("participant_counts_*.csv"))
        rows = {}
        if files:
            text = files[-1].read_bytes().decode("utf-8-sig")
            rows = {row["applet_name"]: row for row in csv.DictReader(io.StringIO(text))}
        return result, rows

    return invoke


def test_counts_every_applet(run):
    result, rows = run(FakeServer(), "--workspace", "Lab")
    assert result.exit_code == 0, result.output

    assert list(rows) == ["Brand New", "Clinic", "Draft", "EMA Study", "Family Study"]
    ema = rows["EMA Study"]
    assert (ema["participants_submitted"], ema["participants_with_data"], ema["submissions"]) == ("3", "3", "5")
    assert ema["first_submission_utc"] == "2026-08-01 08:00:00"
    assert ema["last_submission_utc"] == "2026-09-03 08:00:00"
    assert ema["your_role"] == "owner" and ema["note"] == ""

    family = rows["Family Study"]
    assert (family["participants_submitted"], family["participants_with_data"]) == ("3", "2")

    clinic = rows["Clinic"]
    assert (clinic["participants_submitted"], clinic["submissions"]) == ("1", "1"), "assessment not counted"
    assert clinic["note"] == participant_counts.NOTE_REVIEWER

    assert rows["Draft"]["participants_submitted"] == ""
    assert rows["Draft"]["note"] == participant_counts.NOTE_NO_ACCESS

    empty = rows["Brand New"]
    assert (empty["participants_submitted"], empty["submissions"], empty["last_submission_utc"]) == ("0", "0", "")


def test_never_downloads_answers(run):
    server = FakeServer()
    result, _ = run(server, "--workspace", OWNER_ID)
    assert result.exit_code == 0, result.output
    assert not any(r.url.path.endswith("/data") for r in server.requests)
    # Applet details are only fetched for applets with submissions.
    assert f"/applets/{EMPTY}" not in {r.url.path for r in server.requests}


def test_date_range_is_sent_and_applied(run):
    server = FakeServer()
    result, rows = run(server, "--workspace", "Lab", "--from", "2026-09-01T00:00:00Z", "--to", "2026-09-30T23:59:59Z")
    assert result.exit_code == 0, result.output
    assert (rows["EMA Study"]["participants_submitted"], rows["EMA Study"]["submissions"]) == ("2", "3")
    sent = next(r for r in server.requests if r.url.path == f"/answers/applet/{EMA}/submissions")
    assert sent.url.params["fromDate"] == "2026-09-01T00:00:00"
    assert sent.url.params["toDate"] == "2026-09-30T23:59:59"


def test_pages_through_large_applets(run, monkeypatch):
    monkeypatch.setattr(participant_counts, "SUBMISSIONS_PAGE_SIZE", 2)
    server = FakeServer()
    result, rows = run(server, "--workspace", "Lab")
    assert result.exit_code == 0, result.output
    assert rows["EMA Study"]["submissions"] == "5"
    pages = [r.url.params["page"] for r in server.requests if r.url.path == f"/answers/applet/{EMA}/submissions"]
    assert pages == ["1", "2", "3"]


def test_falls_back_to_export_endpoint_on_older_servers(run):
    server = FakeServer(has_submissions_endpoint=False)
    result, rows = run(server, "--workspace", "Lab")
    assert result.exit_code == 0, result.output
    assert (rows["EMA Study"]["participants_submitted"], rows["EMA Study"]["submissions"]) == ("3", "5")
    assert (rows["Family Study"]["participants_submitted"], rows["Family Study"]["participants_with_data"]) == (
        "3",
        "2",
    )
    assert (rows["Clinic"]["participants_submitted"], rows["Clinic"]["submissions"]) == ("1", "1")
    # The missing route is detected once, not retried for every applet.
    probes = [r for r in server.requests if r.url.path.endswith("/submissions")]
    assert len(probes) == 1


def test_several_workspaces_need_a_choice(run):
    result, _ = run(FakeServer())
    assert result.exit_code == 2
    assert "several workspaces" in result.output and "Other Lab" in result.output


def test_unknown_workspace(run):
    result, _ = run(FakeServer(), "--workspace", "Nope")
    assert result.exit_code == 2
    assert "No workspace called 'Nope'" in result.output
