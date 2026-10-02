"""Download, decrypt, filter, and turn answers into `responses.csv` rows."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .client import CuriousClient
from .crypto import AppletDecryptor, DecryptionError
from .jscompat import UNDEFINED, get, nullish
from .report import report_row
from .subscales import subscale_columns

DEFAULT_PAGE_SIZE = 500

# Fields of an export answer that hold ciphertext; they are dropped from decrypted records.
_ENCRYPTED_FIELDS = ("answer", "events", "userPublicKey", "itemIds", "migratedData")


@dataclass
class ExportFilters:
    """What to download. Empty collections mean "no filter"."""

    target_subject_ids: list[str] = field(default_factory=list)
    respondent_ids: list[str] = field(default_factory=list)
    from_utc: datetime | None = None
    to_utc: datetime | None = None
    activity_ids: set[str] = field(default_factory=set)
    flow_ids: set[str] = field(default_factory=set)

    @staticmethod
    def _api_datetime(value: datetime | None) -> str | None:
        # The backend compares against naive UTC timestamps.
        return value.strftime("%Y-%m-%dT%H:%M:%S") if value else None

    @property
    def api_from(self) -> str | None:
        return self._api_datetime(self.from_utc)

    @property
    def api_to(self) -> str | None:
        return self._api_datetime(self.to_utc)

    def keeps(self, answer: dict) -> bool:
        """Activity/flow filtering, done client-side because the export endpoint can't."""
        if not self.activity_ids and not self.flow_ids:
            return True
        return answer.get("activityId") in self.activity_ids or answer.get("flowId") in self.flow_ids


@dataclass
class DecryptionFailure:
    answer_id: str
    activity_name: str
    target_secret_id: str
    reason: str


@dataclass
class ExportResult:
    rows: list[dict]
    answers_downloaded: int
    answers_kept: int
    failures: list[DecryptionFailure]
    decrypted: list[dict]


ProgressCallback = Callable[[int, int], None]


def download_answers(
    client: CuriousClient,
    applet_id: str,
    filters: ExportFilters,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    on_progress: ProgressCallback | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Fetch every page. Returns (answers in API order, activity definitions by idVersion)."""
    answers: list[dict] = []
    seen_ids: set[str] = set()
    activities: dict[str, dict] = {}
    page = 1
    total_pages = 1
    total = 0
    while page <= total_pages:
        response = client.export_page(
            applet_id,
            page=page,
            limit=page_size,
            target_subject_ids=filters.target_subject_ids,
            respondent_ids=filters.respondent_ids,
            from_date=filters.api_from,
            to_date=filters.api_to,
        )
        result = response.get("result") or {}
        if page == 1:
            total = int(response.get("count") or 0)
            total_pages = max(1, math.ceil(total / page_size))
        # Each page only carries the activity versions its own answers use.
        for activity in result.get("activities") or []:
            activities.setdefault(activity["idVersion"], activity)
        for answer in result.get("answers") or []:
            # Offset paging can repeat rows if answers arrive mid-download.
            if answer["id"] not in seen_ids:
                seen_ids.add(answer["id"])
                answers.append(answer)
        if on_progress:
            on_progress(len(answers), total)
        page += 1
    return answers, activities


def _apply_migrated_url(answer: Any, activity_item: dict, migrated_urls: dict) -> Any:
    """getDecryptedAnswers.ts getAnswer: legacy answers keep media URLs in migratedData."""
    migrated = migrated_urls.get(activity_item.get("id"))
    if not migrated or not isinstance(answer, dict):
        return answer
    if activity_item.get("responseType") == "drawing":
        return {**answer, "value": {**(answer.get("value") or {}), "uri": migrated.get("fileUrl")}}
    return {**answer, "value": migrated.get("fileUrl")}


def _is_failed_decryption(answer: Any) -> bool:
    return isinstance(answer, dict) and {"type", "screen", "time"} <= answer.keys()


def decrypt_records(answer: dict, activity: dict, answers_decrypted: Any) -> list[dict]:
    """One record per visible item, matched to answers by position like the admin does."""
    shared = {key: value for key, value in answer.items() if key not in _ENCRYPTED_FIELDS}
    items = activity.get("items") or []
    shared.update(items=items, activityName=activity.get("name"), subscaleSetting=activity.get("subscaleSetting"))

    migrated_files = (answer.get("migratedData") or {}).get("decryptedFileAnswers") or []
    migrated_urls = {entry.get("answerItemId"): entry for entry in migrated_files if isinstance(entry, dict)}

    records = []
    for index, activity_item in enumerate(items):
        if activity_item.get("isHidden"):
            continue
        item_answer = get(answers_decrypted, index) if isinstance(answers_decrypted, list) else UNDEFINED
        item_answer = _apply_migrated_url(item_answer, activity_item, migrated_urls)
        if _is_failed_decryption(item_answer):
            item_answer = None
        records.append({**shared, "activityItem": activity_item, "answer": item_answer})
    return records


def rows_for_submission(records: list[dict], *, null_when_skipped: bool = False) -> list[dict]:
    """getReportData: rows for answered items, plus subscale columns on the first row."""
    raw_answers = {record["activityItem"].get("name"): record for record in records}
    rows = [
        report_row(record, raw_answers, index) for index, record in enumerate(records) if not nullish(record["answer"])
    ]
    setting = records[0].get("subscaleSetting") if records else None
    if isinstance(setting, dict) and setting.get("subscales"):
        extra = subscale_columns(setting, raw_answers, null_when_skipped=null_when_skipped)
        if rows:
            rows[0] = {**rows[0], **extra}
        else:
            # The admin's splice on an empty list inserts a row holding only the scores.
            rows.append(extra)
    return rows


def _failure(answer: dict, activity_name: str, reason: str) -> DecryptionFailure:
    return DecryptionFailure(answer.get("id", ""), activity_name, answer.get("targetSecretId") or "", reason)


def build_export(
    answers: Iterable[dict],
    activities: dict[str, dict],
    decryptor: AppletDecryptor,
    filters: ExportFilters,
    *,
    null_when_skipped: bool = False,
    keep_decrypted: bool = False,
) -> ExportResult:
    rows: list[dict] = []
    failures: list[DecryptionFailure] = []
    decrypted: list[dict] = []
    downloaded = kept = 0

    for answer in answers:
        downloaded += 1
        if not filters.keeps(answer):
            continue
        kept += 1
        activity = activities.get(answer.get("activityHistoryId"))
        activity_name = activity.get("name", "") if activity else ""

        if activity is None:
            reason = f"activity definition {answer.get('activityHistoryId')} missing from export"
            failures.append(_failure(answer, activity_name, reason))
            continue

        answers_decrypted: Any = []
        events: Any = []
        if answer.get("userPublicKey") and answer.get("answer"):
            try:
                answers_decrypted = decryptor.decrypt_json(answer["userPublicKey"], answer["answer"])
            except DecryptionError as e:
                failures.append(_failure(answer, activity_name, str(e)))
                continue
            if keep_decrypted and answer.get("events"):
                try:
                    events = decryptor.decrypt_json(answer["userPublicKey"], answer["events"])
                except DecryptionError:
                    events = None

        records = decrypt_records(answer, activity, answers_decrypted)
        rows.extend(rows_for_submission(records, null_when_skipped=null_when_skipped))

        if keep_decrypted:
            decrypted.append(
                {
                    "id": answer.get("id"),
                    "submitId": answer.get("submitId"),
                    "activityId": answer.get("activityId"),
                    "activityName": activity_name,
                    "flowId": answer.get("flowId"),
                    "targetSecretId": answer.get("targetSecretId"),
                    "itemIds": answer.get("itemIds"),
                    "itemNames": [item.get("name") for item in activity.get("items") or []],
                    "answers": answers_decrypted,
                    "events": events,
                }
            )

    return ExportResult(rows, downloaded, kept, failures, decrypted)


def write_jsonl(path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
