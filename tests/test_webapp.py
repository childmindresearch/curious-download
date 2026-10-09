"""The local web app (`curious-app`) against the same fake servers as the CLI tests."""

from __future__ import annotations

import csv
import io
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from curious_download import cli
from curious_download.client import CuriousClient
from curious_download.webapp.server import create_app

from . import corpus, fake_server
from .test_dictionary import FakeServer as DictionaryFake
from .test_participant_counts import OWNER_ID
from .test_participant_counts import FakeServer as CountsFake

TOKEN = "test-token"


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    """A TestClient over an app whose CuriousClient talks to the given fake server."""
    monkeypatch.setattr(cli, "SETTINGS_PATH", tmp_path / "settings.json")

    def build(fake) -> TestClient:
        app = create_app(
            token=TOKEN,
            client_factory=lambda url: CuriousClient(url, transport=fake.transport()),
            allowed_hosts={"testserver"},
        )
        client = TestClient(app)
        client.headers["X-Curious-Token"] = TOKEN
        return client

    return build


def login(client: TestClient, email: str = fake_server.EMAIL, password: str = fake_server.PASSWORD) -> None:
    response = client.post(
        "/api/login", json={"server": "https://api.curious.test", "email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    assert response.json().get("ok")


def wait_for(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def read_csv(path: Path) -> list[dict]:
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "responses CSVs carry a BOM"
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


# -- access control ---------------------------------------------------------------


def test_api_requires_the_app_token(make_client):
    client = make_client(fake_server.FakeCurious())
    bare = client.get("/api/state", headers={"X-Curious-Token": "wrong"})
    assert bare.status_code == 401
    assert bare.json()["badToken"]
    # The static page itself needs no token.
    assert client.get("/").status_code == 200
    assert client.get("/api/state").status_code == 200


def test_endpoints_need_a_login(make_client):
    client = make_client(fake_server.FakeCurious())
    assert client.get("/api/workspaces").status_code == 401


# -- login -------------------------------------------------------------------------


def test_login_saves_server_and_email_for_next_time(make_client):
    client = make_client(fake_server.FakeCurious())
    login(client)
    state = client.get("/api/state").json()
    assert state["loggedIn"] and state["email"] == fake_server.EMAIL
    assert state["settings"]["server"] == "https://api.curious.test"
    assert state["settings"]["email"] == fake_server.EMAIL


def test_wrong_password(make_client):
    client = make_client(fake_server.FakeCurious())
    response = client.post(
        "/api/login", json={"server": "https://api.curious.test", "email": fake_server.EMAIL, "password": "nope"}
    )
    assert response.status_code == 401
    assert "Incorrect email or password" in response.json()["error"]


def test_mfa_flow(make_client):
    client = make_client(fake_server.FakeCurious(mfa=True))
    body = {"server": "https://api.curious.test", "email": fake_server.EMAIL, "password": fake_server.PASSWORD}
    first = client.post("/api/login", json=body)
    assert first.status_code == 200 and first.json() == {"mfaRequired": True}
    second = client.post("/api/login", json={**body, "mfaCode": "123456"})
    assert second.status_code == 200 and second.json()["ok"]


# -- answers ---------------------------------------------------------------------------


def test_answers_end_to_end(make_client, tmp_path):
    client = make_client(fake_server.FakeCurious())
    login(client)

    workspaces = client.get("/api/workspaces").json()
    assert [w["name"] for w in workspaces] == ["Lab"]
    owner_id = workspaces[0]["ownerId"]

    applets = client.get(f"/api/workspaces/{owner_id}/applets").json()
    assert applets[0]["name"] == "EMA Study" and applets[0]["canExport"]
    applet_id = applets[0]["id"]

    details = client.get(f"/api/applets/{applet_id}").json()
    assert details["hasEncryption"] and not details["unlocked"]
    assert {activity["name"] for activity in details["activities"]} == {"Daily check-in", "Scored"}

    participants = client.get(f"/api/workspaces/{owner_id}/applets/{applet_id}/participants").json()
    assert [p["secretId"] for p in participants] == ["P001", "P002"]

    wrong = client.post(f"/api/applets/{applet_id}/unlock", json={"password": "nope"})
    assert wrong.status_code == 400 and "not correct" in wrong.json()["error"]

    unlocked = client.post(f"/api/applets/{applet_id}/unlock", json={"password": fake_server.APPLET_PASSWORD})
    assert unlocked.status_code == 200 and unlocked.json()["keyVariant"] == "browser"
    assert client.get(f"/api/applets/{applet_id}").json()["unlocked"]

    started = client.post("/api/jobs/answers", json={"appletId": applet_id, "outputDir": str(tmp_path / "out")})
    assert started.status_code == 200, started.text
    job = wait_for(client, started.json()["jobId"])
    assert job["status"] == "done", job["error"]
    assert job["result"]["submissions"] == 5 and job["warnings"] == []

    folder = Path(job["result"]["folder"])
    rows = read_csv(folder / "responses.csv")
    assert len(rows) == int(job["result"]["rows"]) and (folder / "export_info.json").exists()
    assert "--applet" in job["result"]["command"]
    assert json.loads((folder / "export_info.json").read_text())["command"] == job["result"]["command"]


def test_answers_requires_unlock_first(make_client, tmp_path):
    client = make_client(fake_server.FakeCurious())
    login(client)
    response = client.post("/api/jobs/answers", json={"appletId": corpus.APPLET_ID, "outputDir": str(tmp_path / "out")})
    assert response.status_code == 400 and "Unlock" in response.json()["error"]


def test_answers_with_participant_filter_and_media(make_client, tmp_path):
    client = make_client(fake_server.FakeCurious())
    login(client)
    client.post(f"/api/applets/{corpus.APPLET_ID}/unlock", json={"password": fake_server.APPLET_PASSWORD})
    participants = client.get(f"/api/workspaces/{fake_server.OWNER_ID}/applets/{corpus.APPLET_ID}/participants").json()
    chosen = next(p for p in participants if p["secretId"] == "P001")

    started = client.post(
        "/api/jobs/answers",
        json={
            "appletId": corpus.APPLET_ID,
            "participants": [{"subjectId": chosen["subjectId"], "secretId": chosen["secretId"]}],
            "media": True,
            "outputDir": str(tmp_path / "out"),
        },
    )
    job = wait_for(client, started.json()["jobId"])
    assert job["status"] == "done", job["error"]

    folder = Path(job["result"]["folder"])
    assert (folder / "media_files.csv").exists()
    assert job["result"]["media"]["files"] > 0
    assert job["result"]["media"]["saved"] == job["result"]["media"]["files"] - job["result"]["media"]["failed"]
    rows = read_csv(folder / "responses.csv")
    assert {row["userId"] for row in rows if row.get("userId")}, "filtered export still has rows"


def test_answers_with_journey(make_client, tmp_path):
    client = make_client(fake_server.FakeCurious())
    login(client)
    client.post(f"/api/applets/{corpus.APPLET_ID}/unlock", json={"password": fake_server.APPLET_PASSWORD})

    started = client.post(
        "/api/jobs/answers",
        json={"appletId": corpus.APPLET_ID, "journey": True, "outputDir": str(tmp_path / "out")},
    )
    job = wait_for(client, started.json()["jobId"])
    assert job["status"] == "done", job["error"]

    folder = Path(job["result"]["folder"])
    journey = read_csv(folder / "activity_user_journey.csv")
    assert journey and job["result"]["journey"] == {"rows": len(journey), "failed": 1}
    assert any("actions" in warning for warning in job["warnings"])
    assert "--journey" in job["result"]["command"]


# -- data dictionary ----------------------------------------------------------------------


def test_dictionary_job_all_applets(make_client, tmp_path):
    client = make_client(DictionaryFake())
    login(client, email="any@example.org", password="x")
    workspaces = client.get("/api/workspaces").json()
    started = client.post(
        "/api/jobs/dictionary",
        json={"ownerId": workspaces[0]["ownerId"], "allApplets": True, "outputDir": str(tmp_path / "out")},
    )
    assert started.status_code == 200, started.text
    job = wait_for(client, started.json()["jobId"])
    assert job["status"] == "done", job["error"]
    assert job["result"]["files"] > 0 and job["result"]["failed"] == 1  # the respondent-role applet
    assert any("no access" in warning for warning in job["warnings"])

    folder = Path(job["result"]["folder"])
    assert folder.name.startswith("data_dictionary_Lab_")
    assert (folder / "index.csv").exists() and (folder / "export_info.json").exists()
    assert "--all-applets" in job["result"]["command"]
    assert json.loads((folder / "export_info.json").read_text())["command"] == job["result"]["command"]


# -- participant counts ----------------------------------------------------------------------


def test_counts_cancel_takes_effect_mid_applet(make_client, tmp_path):
    """A cancel during a large applet must not wait for the whole workspace (238-applet report)."""
    fake = CountsFake()
    in_request = threading.Event()
    release = threading.Event()
    original_handle = fake.handle

    def slow_handle(request):
        if request.url.path.endswith("/submissions") and not in_request.is_set():
            in_request.set()
            release.wait(timeout=10)
        return original_handle(request)

    fake.handle = slow_handle
    client = make_client(fake)
    login(client, email="any@example.org", password="x")
    started = client.post("/api/jobs/counts", json={"ownerId": OWNER_ID, "outputDir": str(tmp_path / "out")})
    job_id = started.json()["jobId"]

    assert in_request.wait(5), "the job never reached the submissions endpoint"
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "running" and job["detail"], "the applet being counted is shown"

    client.post(f"/api/jobs/{job_id}/cancel")
    assert client.get(f"/api/jobs/{job_id}").json()["cancelRequested"]
    release.set()

    job = wait_for(client, job_id)
    assert job["status"] == "cancelled"
    assert job["done"] == 0, "cancelled inside the first applet, not after all of them"


def test_counts_job(make_client, tmp_path):
    client = make_client(CountsFake())
    login(client, email="any@example.org", password="x")
    started = client.post("/api/jobs/counts", json={"ownerId": OWNER_ID, "outputDir": str(tmp_path / "out")})
    assert started.status_code == 200, started.text
    job = wait_for(client, started.json()["jobId"])
    assert job["status"] == "done", job["error"]

    rows = {row["applet_name"]: row for row in job["result"]["rows"]}
    ema = rows["EMA Study"]
    assert (ema["participants_submitted"], ema["participants_with_data"], ema["submissions"]) == (3, 3, 5)
    assert rows["Draft"]["note"].startswith("no data access")

    csv_path = Path(job["result"]["mainFile"])
    assert csv_path.exists() and csv_path.name.startswith("participant_counts_Lab_")
    assert "curious-participant-counts" in job["result"]["command"]


# -- local conveniences --------------------------------------------------------------------------


def test_browse_lists_subfolders(make_client, tmp_path):
    client = make_client(fake_server.FakeCurious())
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / ".hidden").mkdir()
    listing = client.get("/api/browse", params={"path": str(tmp_path)}).json()
    assert listing["dirs"] == ["alpha", "beta"]
    assert listing["parent"] == str(tmp_path.parent)


def test_reveal_only_allows_job_output(make_client, tmp_path):
    client = make_client(fake_server.FakeCurious())
    login(client)
    response = client.post("/api/reveal", json={"path": str(tmp_path)})
    assert response.status_code == 400
