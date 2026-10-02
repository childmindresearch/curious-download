"""Media files referenced by answers: photos, video, audio, drawings, and Unity task files.

Mirrors the admin export (mindlogger-admin/src/shared/utils/getParsedAnswers.ts and
exportData/getReportAndMediaData.ts): file URLs are taken from the decrypted answers,
presigned with POST /file/{appletId}/presign, downloaded, and saved under the same
names the admin uses in its media zips. Those names are also what responses.csv shows
in `item_response`, so rows and files can be matched.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .client import ApiError, CuriousClient
from .csvout import write_csv
from .jscompat import coalesce, get, truthy
from .report import MEDIA_TYPES, file_extension, media_file_name, unity_media_urls

MEDIA_FOLDER = "media"
UNITY_FOLDER = "unity"
PRESIGN_BATCH_SIZE = 50
DOWNLOAD_WORKERS = 4

# Item types whose answers can point at uploaded files (admin ItemsWithFileResponses).
_FILE_RESPONSE_TYPES = (*MEDIA_TYPES, "unity")
_FETCHABLE_SCHEMES = ("s3://", "gs://", "https://", "http://")
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

STATUS_DOWNLOADED = "downloaded"
STATUS_FROM_ANSWER = "saved_from_answer"
STATUS_FAILED = "failed"

MANIFEST_COLUMNS = [
    "activity_submission_id",
    "target_secret_id",
    "item_name",
    "item_type",
    "file",
    "status",
    "error",
]


def media_url(answer: Any) -> Any:
    """getUrls.ts getMediaUrl: the stored URL of a photo, video, or audio answer."""
    if not truthy(answer):
        return ""
    value = get(answer, "value")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return get(value, 0)
    if isinstance(value, dict) and "uri" in value:
        return value["uri"] or ""
    return ""


def with_public_urls(records: list[dict]) -> list[dict]:
    """getAnswersWithPublicUrls: put each file's URL where the admin's formatting expects it.

    The admin swaps stored URLs for presigned ones before building rows. Presigning keeps the
    file path, so the CSV only depends on where the URL ends up; the actual presigning happens
    in download_media.
    """
    updated = []
    for record in records:
        answer = record["answer"]
        response_type = record["activityItem"].get("responseType")
        if not truthy(answer) or response_type not in _FILE_RESPONSE_TYPES:
            updated.append(record)
        elif response_type == "unity":
            urls = unity_media_urls(record)
            value = {"taskData": urls} if urls else coalesce(media_url(answer), "")
            updated.append({**record, "answer": {**answer, "value": value}})
        else:
            updated.append({**record, "answer": {**answer, "value": coalesce(media_url(answer), "")}})
    return updated


@dataclass
class MediaFile:
    """One file to save. `name` is the admin's file name inside `folder`."""

    folder: str
    name: str
    url: str | None
    inline: str | None
    answer_id: str
    target_secret_id: str
    item_name: str
    item_type: str


def _media_file(record: dict, folder: str, name: str, url: str | None, inline: str | None = None) -> MediaFile:
    return MediaFile(
        folder=folder,
        name=name,
        url=url,
        inline=inline,
        answer_id=str(record.get("id") or ""),
        target_secret_id=str(record.get("targetSecretId") or ""),
        item_name=str(record["activityItem"].get("name") or ""),
        item_type=str(record["activityItem"].get("responseType") or ""),
    )


def collect_media(records: list[dict]) -> list[MediaFile]:
    """getMediaData + getUnityData for one submission (records after with_public_urls)."""
    files = []
    for record in records:
        answer = record["answer"]
        response_type = record["activityItem"].get("responseType")
        if response_type == "drawing" and truthy(answer):
            value = get(answer, "value")
            if isinstance(value, dict):
                uri = value.get("uri")
                svg = value.get("svgString")
                files.append(
                    _media_file(
                        record,
                        MEDIA_FOLDER,
                        media_file_name(record, "svg"),
                        uri if truthy(uri) else None,
                        svg if isinstance(svg, str) else None,
                    )
                )
            continue
        url = media_url(answer)
        if response_type in _FILE_RESPONSE_TYPES and isinstance(url, str) and url:
            files.append(_media_file(record, MEDIA_FOLDER, media_file_name(record, file_extension(url)), url))

    for record in records:
        if record["activityItem"].get("responseType") != "unity":
            continue
        for index, url in enumerate(unity_media_urls(record)):
            url_file_name = url.split("?")[0].split("/")[-1]
            name = f"{record.get('id')}/{url_file_name or f'{index}.{file_extension(url)}'}"
            files.append(_media_file(record, UNITY_FOLDER, name, url))
    return files


@dataclass
class MediaOutcome:
    file: MediaFile
    path: str
    status: str
    error: str = ""


def _safe_part(text: str) -> str:
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", text).strip()
    return "_" if cleaned in ("", ".", "..") else cleaned


def _target_path(out_dir: Path, file: MediaFile, used: set[Path]) -> Path:
    """Where to save a file: the admin name, made safe for the file system and unique."""
    if file.folder == UNITY_FOLDER and "/" in file.name:
        folder_name, file_name = file.name.split("/", 1)
        path = out_dir / UNITY_FOLDER / _safe_part(folder_name) / _safe_part(file_name)
    else:
        path = out_dir / file.folder / _safe_part(file.name)
    candidate, counter = path, 2
    while candidate in used:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        counter += 1
    used.add(candidate)
    return candidate


def download_media(
    client: CuriousClient,
    applet_id: str,
    files: list[MediaFile],
    out_dir: Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    batch_size: int = PRESIGN_BATCH_SIZE,
    workers: int = DOWNLOAD_WORKERS,
) -> list[MediaOutcome]:
    """Presign and download every file. Never raises for a single file; failures are reported."""
    used: set[Path] = set()
    targets = [(file, _target_path(out_dir, file, used)) for file in files]
    outcomes: dict[int, MediaOutcome] = {}
    lock = threading.Lock()
    done = 0

    def finish(index: int, file: MediaFile, path: Path, status: str, error: str = "") -> None:
        nonlocal done
        with lock:
            outcomes[index] = MediaOutcome(file, path.relative_to(out_dir).as_posix(), status, error)
            done += 1
            if on_progress:
                on_progress(done, len(targets))

    def save_inline_or_fail(index: int, file: MediaFile, path: Path, error: str) -> None:
        if file.inline is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file.inline, encoding="utf-8")
            finish(index, file, path, STATUS_FROM_ANSWER)
        else:
            finish(index, file, path, STATUS_FAILED, error)

    def fetch(index: int, file: MediaFile, path: Path, signed_url: str | None) -> None:
        if not signed_url or not signed_url.startswith(("https://", "http://")):
            save_inline_or_fail(index, file, path, "the server refused to share this file (no access or unknown file)")
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(signed_url, path)
        except httpx.HTTPStatusError as e:
            save_inline_or_fail(index, file, path, f"storage returned HTTP {e.response.status_code}")
        except httpx.HTTPError as e:
            save_inline_or_fail(index, file, path, f"download failed: {e}")
        else:
            finish(index, file, path, STATUS_DOWNLOADED)

    pending = []
    for index, (file, path) in enumerate(targets):
        if file.url is None:
            save_inline_or_fail(index, file, path, "the answer has no file")
        elif not file.url.startswith(_FETCHABLE_SCHEMES):
            save_inline_or_fail(index, file, path, f"the file was never uploaded from the device ({file.url[:40]})")
        else:
            pending.append((index, file, path))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            # Presign right before downloading: presigned URLs expire after an hour.
            try:
                signed = client.presign(applet_id, [file.url for _, file, _ in batch])
            except (ApiError, httpx.HTTPError) as e:
                for index, file, path in batch:
                    save_inline_or_fail(index, file, path, f"could not get a download link: {e}")
                continue
            if len(signed) != len(batch):
                for index, file, path in batch:
                    save_inline_or_fail(index, file, path, "the server returned an unexpected number of links")
                continue
            list(pool.map(lambda job: fetch(*job), [(*entry, url) for entry, url in zip(batch, signed, strict=True)]))

    return [outcomes[index] for index in range(len(targets))]


def write_manifest(path: Path, outcomes: list[MediaOutcome]) -> None:
    rows = [
        {
            "activity_submission_id": outcome.file.answer_id,
            "target_secret_id": outcome.file.target_secret_id,
            "item_name": outcome.file.item_name,
            "item_type": outcome.file.item_type,
            "file": outcome.path,
            "status": outcome.status,
            "error": outcome.error,
        }
        for outcome in outcomes
    ]
    write_csv(path, rows, MANIFEST_COLUMNS)
