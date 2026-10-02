"""Presigning and downloading media files against a fake API and fake storage hosts."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from curious_download.client import CuriousClient
from curious_download.export import prepare_records
from curious_download.media import (
    STATUS_DOWNLOADED,
    STATUS_FAILED,
    STATUS_FROM_ANSWER,
    MediaFile,
    collect_media,
    download_media,
    write_manifest,
)

from . import corpus

API = "https://api.test"


class FakeStorage:
    """Presign endpoint plus the storage hosts the signed URLs point to."""

    def __init__(self, *, flaky: set[str] = frozenset(), presign_status: int = 200):
        self.flaky = set(flaky)
        self.presign_status = presign_status
        self.presign_calls: list[list[str]] = []
        self.storage_requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.test":
            assert request.url.path == f"/file/{corpus.APPLET_ID}/presign"
            urls = json.loads(request.content)["privateUrls"]
            self.presign_calls.append(urls)
            if self.presign_status != 200:
                return httpx.Response(self.presign_status, json={"result": [{"message": "Presign failed"}]})
            return httpx.Response(200, json={"result": [self._sign(url) for url in urls], "count": len(urls)})

        self.storage_requests.append(request)
        path = request.url.path
        if request.url.host == "legacy.example" and path.endswith("voice.mp3"):
            return httpx.Response(404)
        if path in self.flaky:
            self.flaky.discard(path)
            return httpx.Response(503)
        if request.url.params.get("sig") != "ok" and request.url.host == "storage.test":
            return httpx.Response(403)
        return httpx.Response(200, content=f"bytes of {path}".encode())

    @staticmethod
    def _sign(url: str) -> str:
        if url.startswith(("s3://", "gs://")) and "denied" not in url:
            key = url.split("://", 1)[1].split("?")[0]
            return f"https://storage.test/{key}?sig=ok"
        return url  # the backend returns what it won't sign unchanged


@pytest.fixture
def make_client():
    def make(storage: FakeStorage) -> CuriousClient:
        client = CuriousClient(API, transport=httpx.MockTransport(storage.handle))
        client._access_token = "api-token"
        client.retry_delay = 0
        return client

    return make


def _files(answer_ids: list[str]) -> list[MediaFile]:
    files = []
    for case in corpus.submissions():
        if case["answer"]["id"] in answer_ids:
            files += collect_media(prepare_records(case["answer"], case["activity"], case["answersDecrypted"]))
    return files


def _by_path(outcomes):
    return {outcome.path: outcome for outcome in outcomes}


def test_downloads_every_kind_of_media(make_client, tmp_path):
    storage = FakeStorage()
    outcomes = download_media(make_client(storage), corpus.APPLET_ID, _files(["a1", "a2", "a3", "m1"]), tmp_path)
    by_path = _by_path(outcomes)

    expected = {
        "media/P001-a1-selfie.jpg": STATUS_DOWNLOADED,
        "media/P001-a1-clip.MOV": STATUS_DOWNLOADED,
        "media/P001-a1-voice.m4a": STATUS_DOWNLOADED,
        "media/P001-a1-sketch.svg": STATUS_DOWNLOADED,
        "unity/a1/run_1.json": STATUS_DOWNLOADED,
        "unity/a1/run_2.json": STATUS_DOWNLOADED,
        "media/P001-a2-clip.webm": STATUS_FAILED,  # never uploaded from the device
        "media/P001-a2-sketch.svg": STATUS_FROM_ANSWER,  # no upload, SVG kept in the answer
        "media/P001-a3-selfie.png": STATUS_DOWNLOADED,  # public legacy URL, fetched as is
        "media/P001-a3-sketch.svg": STATUS_DOWNLOADED,
        "media/P_002-m1-selfie.png": STATUS_DOWNLOADED,  # "/" in the secret ID is made safe
        "media/P_002-m1-clip.mp4": STATUS_DOWNLOADED,
        "media/P_002-m1-voice.mp3": STATUS_FAILED,  # storage answered 404
        "media/P_002-m1-sketch.svg": STATUS_FROM_ANSWER,
        "media/P_002-m1-game.json": STATUS_DOWNLOADED,
    }
    assert {path: outcome.status for path, outcome in by_path.items()} == expected

    assert (
        tmp_path / "media/P001-a1-selfie.jpg"
    ).read_bytes() == b"bytes of /bucket/mindlogger/answer/u/a/1749138592012/IMG_1.jpg"
    assert (tmp_path / "media/P001-a2-sketch.svg").read_text() == "<svg/>"
    assert "never uploaded" in by_path["media/P001-a2-clip.webm"].error
    assert "404" in by_path["media/P_002-m1-voice.mp3"].error
    assert not list(tmp_path.rglob("*.part"))

    # Only fetchable URLs are presigned, and the API token never goes to storage hosts.
    sent = [url for call in storage.presign_calls for url in call]
    assert sent and all(url.startswith(("s3://", "https://")) for url in sent)
    assert all("authorization" not in request.headers for request in storage.storage_requests)


def test_refused_presign_is_reported_and_drawings_fall_back(make_client, tmp_path):
    files = [
        MediaFile("media", "photo.jpg", "s3://bucket/denied/photo.jpg", None, "a", "P1", "photo", "photo"),
        MediaFile("media", "draw.svg", "s3://bucket/denied/draw.svg", "<svg>kept</svg>", "a", "P1", "draw", "drawing"),
    ]
    outcomes = download_media(make_client(FakeStorage()), corpus.APPLET_ID, files, tmp_path)
    assert [o.status for o in outcomes] == [STATUS_FAILED, STATUS_FROM_ANSWER]
    assert "refused" in outcomes[0].error
    assert (tmp_path / "media/draw.svg").read_text() == "<svg>kept</svg>"


def test_retries_transient_storage_errors(make_client, tmp_path):
    storage = FakeStorage(flaky={"/bucket/flaky.jpg"})
    files = [MediaFile("media", "flaky.jpg", "s3://bucket/flaky.jpg", None, "a", "P1", "photo", "photo")]
    outcomes = download_media(make_client(storage), corpus.APPLET_ID, files, tmp_path)
    assert outcomes[0].status == STATUS_DOWNLOADED
    assert len(storage.storage_requests) == 2


def test_presigns_in_batches(make_client, tmp_path):
    storage = FakeStorage()
    files = [
        MediaFile("media", f"{i}.jpg", f"s3://bucket/{i}.jpg", None, "a", "P1", "photo", "photo") for i in range(5)
    ]
    outcomes = download_media(make_client(storage), corpus.APPLET_ID, files, tmp_path, batch_size=2)
    assert [len(call) for call in storage.presign_calls] == [2, 2, 1]
    assert all(o.status == STATUS_DOWNLOADED for o in outcomes)


def test_presign_failure_marks_the_batch_failed(make_client, tmp_path):
    files = [MediaFile("media", "x.jpg", "s3://bucket/x.jpg", None, "a", "P1", "photo", "photo")]
    outcomes = download_media(make_client(FakeStorage(presign_status=500)), corpus.APPLET_ID, files, tmp_path)
    assert outcomes[0].status == STATUS_FAILED
    assert "download link" in outcomes[0].error


def test_duplicate_names_get_unique_paths(make_client, tmp_path):
    files = [
        MediaFile("media", "same.jpg", f"s3://bucket/{i}.jpg", None, "a", "P1", "photo", "photo") for i in range(3)
    ]
    outcomes = download_media(make_client(FakeStorage()), corpus.APPLET_ID, files, tmp_path)
    assert [o.path for o in outcomes] == ["media/same.jpg", "media/same (2).jpg", "media/same (3).jpg"]


def test_unsafe_names_stay_inside_the_export_folder(make_client, tmp_path):
    files = [
        MediaFile("media", "../../escape.jpg", "s3://bucket/e.jpg", None, "a", "P1", "photo", "photo"),
        MediaFile("unity", "../../x/..", "s3://bucket/u.json", None, "a", "P1", "game", "unity"),
    ]
    outcomes = download_media(make_client(FakeStorage()), corpus.APPLET_ID, files, tmp_path / "export")
    for outcome in outcomes:
        assert outcome.status == STATUS_DOWNLOADED
        assert (tmp_path / "export" / outcome.path).resolve().is_relative_to((tmp_path / "export").resolve())


def test_manifest_lists_every_file(make_client, tmp_path):
    outcomes = download_media(make_client(FakeStorage()), corpus.APPLET_ID, _files(["a2"]), tmp_path)
    write_manifest(tmp_path / "media_files.csv", outcomes)
    text = Path(tmp_path / "media_files.csv").read_text(encoding="utf-8-sig")
    assert text.splitlines()[0] == "activity_submission_id,target_secret_id,item_name,item_type,file,status,error"
    assert "media/P001-a2-sketch.svg,saved_from_answer" in text
