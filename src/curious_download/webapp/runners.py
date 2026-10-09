"""The three downloads as job runners. Same steps and files as the CLI commands, without the prompts."""

from __future__ import annotations

import json
import shlex
from collections import Counter
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..cli import _equivalent_command
from ..client import ApiError, CuriousClient, Participant
from ..crypto import AppletDecryptor
from ..csvout import file_slug, write_csv
from ..dictionary import INDEX_COLUMNS, build_dictionaries, fetch_history, write_dictionaries
from ..export import DEFAULT_PAGE_SIZE, ExportFilters, build_export, download_answers, write_jsonl
from ..media import STATUS_DOWNLOADED, STATUS_FAILED, STATUS_FROM_ANSWER, download_media, write_manifest
from ..participant_counts import COLUMNS as COUNT_COLUMNS
from ..participant_counts import count_workspace
from ..report import REPORT_COLUMNS
from .jobs import Job


def _timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def _created() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run_answers(
    job: Job,
    client: CuriousClient,
    *,
    server: str,
    email: str,
    details: dict,
    decryptor: AppletDecryptor,
    participants: list[Participant],
    filters: ExportFilters,
    activity_labels: list[str],
    media: bool,
    output: Path,
    page_size: int = DEFAULT_PAGE_SIZE,
    save_decrypted: bool = False,
    null_when_skipped: bool = False,
) -> dict:
    applet_name = details.get("displayName") or details["id"]

    job.progress(stage="Downloading answers")
    answers, activity_defs = download_answers(
        client,
        details["id"],
        filters,
        page_size=page_size,
        on_progress=lambda done, total: job.progress(done=done, total=total),
    )

    job.progress(stage="Decrypting and formatting")
    result = build_export(
        answers,
        activity_defs,
        decryptor,
        filters,
        null_when_skipped=null_when_skipped,
        keep_decrypted=save_decrypted,
        include_media=media,
    )

    export_dir = output / f"{file_slug(applet_name)}_{_timestamp()}"
    export_dir.mkdir(parents=True, exist_ok=True)
    write_csv(export_dir / "responses.csv", result.rows, REPORT_COLUMNS)
    if save_decrypted:
        write_jsonl(export_dir / "decrypted_answers.jsonl", result.decrypted)

    media_outcomes = []
    if media:
        if result.media:
            job.progress(stage="Downloading media", total=len(result.media))
            media_outcomes = download_media(
                client,
                details["id"],
                result.media,
                export_dir,
                on_progress=lambda done, total: job.progress(done=done, total=total),
            )
        write_manifest(export_dir / "media_files.csv", media_outcomes)

    media_counts = Counter(outcome.status for outcome in media_outcomes)
    info = {
        "tool": f"curious-download {__version__}",
        "created": _created(),
        "server": server,
        "applet": {"id": details["id"], "name": applet_name, "version": details.get("version")},
        "filters": {
            "participants": [p.secret_id for p in participants] or "all",
            "from_utc": filters.api_from,
            "to_utc": filters.api_to,
            "activities": activity_labels or "all",
        },
        "answers_downloaded": result.answers_downloaded,
        "answers_after_activity_filter": result.answers_kept,
        "rows_written": len(result.rows),
        "key_variant": decryptor.key_variant,
        "decryption_failures": [vars(f) for f in result.failures],
        "media": {
            "requested": media,
            "files": len(media_outcomes),
            **{status: media_counts.get(status, 0) for status in (STATUS_DOWNLOADED, STATUS_FROM_ANSWER, STATUS_FAILED)},
        },
    }
    (export_dir / "export_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))

    if result.failures:
        job.warn(f"{len(result.failures)} submission(s) could not be decrypted; see export_info.json.")
    if media_counts[STATUS_FAILED]:
        job.warn(f"{media_counts[STATUS_FAILED]} media file(s) could not be downloaded; see media_files.csv.")

    return {
        "folder": str(export_dir),
        "mainFile": str(export_dir / "responses.csv"),
        "submissions": result.answers_kept,
        "rows": len(result.rows),
        "decryptionFailures": len(result.failures),
        "media": {
            "files": len(media_outcomes),
            "saved": media_counts[STATUS_DOWNLOADED] + media_counts[STATUS_FROM_ANSWER],
            "failed": media_counts[STATUS_FAILED],
        }
        if media
        else None,
        "command": _equivalent_command(
            server, email, details["id"], participants, filters.from_utc, filters.to_utc, activity_labels, output, media
        ),
    }


def run_dictionary(
    job: Job,
    client: CuriousClient,
    *,
    server: str,
    email: str,
    rows: list[dict],
    scope: str,
    workspace: dict | None,
    output: Path,
) -> dict:
    """`workspace` is set when every applet of that workspace was chosen (mirrors --all-applets)."""
    folder = output / f"data_dictionary_{file_slug(scope)}_{_timestamp()}"
    folder.mkdir(parents=True, exist_ok=True)

    index_rows: list[dict] = []
    applet_info: list[dict] = []
    used_names: set[str] = set()
    for number, row in enumerate(rows, start=1):
        name = row.get("displayName") or row["id"]
        counter = f" ({number} of {len(rows)})" if len(rows) > 1 else ""
        job.progress(stage=f"Reading versions of {name}{counter}")
        try:
            history = fetch_history(
                client,
                row["id"],
                name,
                on_progress=lambda done, total: job.progress(done=done, total=total),
            )
        except ApiError as e:
            note = "no access to this applet" if e.status_code == 403 else f"error: {e}"
            index_rows.append({"applet_id": row["id"], "applet_name": name, "note": note})
            applet_info.append({"id": row["id"], "name": name, "error": note})
            job.warn(f"{name}: {note}")
            continue
        dictionaries = build_dictionaries(history)
        written = write_dictionaries(folder, history, dictionaries, used_names)
        index_rows += written or [{"applet_id": row["id"], "applet_name": name, "note": "no activities"}]
        applet_info.append(
            {
                "id": row["id"],
                "name": name,
                "versions": len(history.versions),
                "current_version": history.versions[-1] if history.versions else None,
                "activities": len(dictionaries),
            }
        )
    write_csv(folder / "index.csv", index_rows, INDEX_COLUMNS)

    files = sum(1 for index_row in index_rows if index_row.get("file"))
    info = {
        "tool": f"curious-download {__version__}",
        "created": _created(),
        "server": server,
        "kind": "data dictionary",
        "workspace": (workspace.get("workspaceName") or workspace["ownerId"]) if workspace else None,
        "applets": applet_info,
        "files": files,
    }
    (folder / "export_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))

    parts = ["curious-download", "--dictionary", "--server", server, "--email", email]
    if workspace:
        parts += ["--all-applets", "--workspace", workspace["ownerId"]]
    else:
        for row in rows:
            parts += ["--applet", row["id"]]
    parts += ["--output", str(output)]

    failed = sum(1 for applet in applet_info if applet.get("error"))
    return {
        "folder": str(folder),
        "files": files,
        "applets": len(rows),
        "failed": failed,
        "command": " ".join(shlex.quote(part) for part in parts),
    }


def run_counts(
    job: Job,
    client: CuriousClient,
    *,
    server: str,
    email: str,
    workspace: dict,
    applets: list[dict],
    filters: ExportFilters,
    from_text: str | None,
    to_text: str | None,
    output: Path,
) -> dict:
    workspace_name = workspace.get("workspaceName") or workspace["ownerId"]
    job.progress(stage=f"Counting {len(applets)} applet(s)", total=len(applets))

    # on_page runs after every page request, so a cancel takes effect mid-applet
    # (job.progress raises JobCancelled) and large applets show movement.
    current = {"name": ""}

    def on_applet(number: int, total: int, applet: dict) -> None:
        current["name"] = applet.get("displayName") or applet["id"]
        job.progress(detail=current["name"])

    def on_page(fetched: int, total_rows: int) -> None:
        if total_rows:
            job.progress(detail=f"{current['name']}: {fetched:,} of {total_rows:,} submissions read")
        else:
            job.progress(detail=current["name"])

    rows = count_workspace(
        client,
        applets,
        filters,
        on_progress=lambda done, total: job.progress(done=done, total=total),
        on_applet=on_applet,
        on_page=on_page,
    )

    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / f"participant_counts_{file_slug(workspace_name)}_{_timestamp()}.csv"
    write_csv(csv_path, rows, COUNT_COLUMNS)

    parts = ["curious-participant-counts", "--server", server, "--email", email, "--workspace", workspace["ownerId"]]
    if from_text:
        parts += ["--from", from_text]
    if to_text:
        parts += ["--to", to_text]
    parts += ["--output", str(output)]

    return {
        "folder": str(output),
        "mainFile": str(csv_path),
        "workspace": workspace_name,
        "fromUtc": filters.api_from,
        "toUtc": filters.api_to,
        "rows": rows,
        "command": " ".join(shlex.quote(part) for part in parts),
    }
