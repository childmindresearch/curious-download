"""The three downloads as job runners. Same steps and files as the CLI commands, without the prompts."""

from __future__ import annotations

import json
import shlex
from collections import Counter
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..cli import _equivalent_command, dictionary_command
from ..client import ApiError, CuriousClient, Participant
from ..crypto import AppletDecryptor
from ..csvout import file_slug, write_csv
from ..dashboard.excel import write_workbook
from ..dashboard.views import GridResult, cells_csv, grid_csv, long_csv, rows_csv
from ..dictionary import INDEX_COLUMNS, build_dictionaries, fetch_history, write_dictionaries
from ..export import DEFAULT_PAGE_SIZE, ExportFilters, build_export, download_answers, write_jsonl
from ..journey import JOURNEY_COLUMNS, JOURNEY_FILE_NAME, journey_info
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
    journey: bool = False,
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
        include_journey=journey,
    )

    export_dir = output / f"{file_slug(applet_name)}_{_timestamp()}"
    export_dir.mkdir(parents=True, exist_ok=True)
    write_csv(export_dir / "responses.csv", result.rows, REPORT_COLUMNS)
    if journey:
        write_csv(export_dir / JOURNEY_FILE_NAME, result.journey, JOURNEY_COLUMNS)
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
    command = _equivalent_command(
        server,
        email,
        details["id"],
        participants,
        filters.from_utc,
        filters.to_utc,
        activity_labels,
        output,
        media,
        journey,
    )
    info = {
        "tool": f"curious-download {__version__}",
        "created": _created(),
        "command": command,
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
            **{
                status: media_counts.get(status, 0) for status in (STATUS_DOWNLOADED, STATUS_FROM_ANSWER, STATUS_FAILED)
            },
        },
        "journey": journey_info(journey, result.journey, result.journey_failures),
    }
    (export_dir / "export_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))

    if result.failures:
        job.warn(f"{len(result.failures)} submission(s) could not be decrypted; see export_info.json.")
    if media_counts[STATUS_FAILED]:
        job.warn(f"{media_counts[STATUS_FAILED]} media file(s) could not be downloaded; see media_files.csv.")
    if result.journey_failures:
        job.warn(
            f"The actions of {len(result.journey_failures)} submission(s) could not be decrypted; see export_info.json."
        )

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
        "journey": {"rows": len(result.journey), "failed": len(result.journey_failures)} if journey else None,
        "command": command,
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
    current_only: bool = False,
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
        what = "the current version" if current_only else "versions"
        job.progress(stage=f"Reading {what} of {name}{counter}")
        try:
            history = fetch_history(
                client,
                row["id"],
                name,
                on_progress=lambda done, total: job.progress(done=done, total=total),
                current_only=current_only,
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
    command = dictionary_command(server, email, rows, workspace, output, current_only)
    info = {
        "tool": f"curious-download {__version__}",
        "created": _created(),
        "command": command,
        "server": server,
        "kind": "data dictionary",
        "versions": "current" if current_only else "all",
        "workspace": (workspace.get("workspaceName") or workspace["ownerId"]) if workspace else None,
        "applets": applet_info,
        "files": files,
    }
    (folder / "export_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))

    failed = sum(1 for applet in applet_info if applet.get("error"))
    return {
        "folder": str(folder),
        "files": files,
        "applets": len(rows),
        "failed": failed,
        "command": command,
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


DASHBOARD_FILES = {
    "info": "export_info.json",
    "cells": "dashboard_cells.csv",
    "grid": "dashboard_grid.csv",
    "rows": "participants.csv",
    "occurrences": "occurrences.csv",
}
OPTIONAL_DASHBOARD_FILES = {"gridJson": "dashboard_grid.json", "workbook": "dashboard.xlsx"}


def _write_text(path: Path, text: str) -> None:
    # UTF-8 with a BOM and LF line endings, like the other CSVs this tool writes.
    path.write_text("\ufeff" + text, encoding="utf-8", newline="")


def run_dashboard_export(
    job: Job,
    *,
    server: str,
    email: str,
    study: dict,
    data_info: dict,
    result: GridResult,
    output: Path,
    grid_json: bool = True,
    workbook: bool = True,
) -> dict:
    """Write a computed dashboard view as a folder of CSVs plus a JSON describing them.

    With `grid_json`, the exact payload the page renders is saved too; with `workbook`, an Excel
    file with one sheet per CSV and a coloured copy of the grid.
    """
    grid = result.grid
    params = grid["params"]
    focused = bool(params.get("focus"))
    job.progress(stage="Writing dashboard files")
    folder = output / f"dashboard_{file_slug(study['name'])}_{grid['view']}_{_timestamp()}"
    folder.mkdir(parents=True, exist_ok=True)
    files = dict(DASHBOARD_FILES)
    if focused:
        files["rows"] = "activities.csv"
    if grid_json:
        files["gridJson"] = OPTIONAL_DASHBOARD_FILES["gridJson"]
    if workbook:
        files["workbook"] = OPTIONAL_DASHBOARD_FILES["workbook"]

    cells = cells_csv(grid)
    rows = rows_csv(grid)
    occurrences = long_csv(result)
    _write_text(folder / files["cells"], cells)
    _write_text(folder / files["grid"], grid_csv(grid))
    _write_text(folder / files["rows"], rows)
    _write_text(folder / files["occurrences"], occurrences)

    overall = grid.get("overall") or {}
    counts = {
        "rows": len(grid["rows"]),
        "columns": len(grid["columns"]),
        "cells": sum(1 for row in grid["rows"] for cell in row["cells"] if cell is not None),
        "occurrenceRows": max(0, occurrences.count("\n") - 1),
    }
    info = {
        "tool": f"curious-download {__version__}",
        "created": _created(),
        "server": server,
        "exportedBy": email,
        "study": {key: value for key, value in study.items() if key not in ("server",)},
        "view": {
            "kind": grid["view"],
            **{key: value for key, value in params.items() if key != "focusLabel"},
            "focusLabel": params.get("focusLabel"),
        },
        "data": data_info,
        "summary": {
            **counts,
            "participants": overall.get("participants"),
            "expected": overall.get("e"),
            "done": overall.get("d"),
            "late": overall.get("l"),
            "partlyDone": overall.get("p"),
            "missed": overall.get("m"),
            "openOrUpcoming": overall.get("u"),
            "unscheduled": overall.get("x"),
            "adherence": overall.get("pct"),
        },
        "statuses": grid.get("legend") or [],
        "notes": grid.get("notes") or [],
        "files": files,
        "columns": {
            files["cells"]: "one row per participant and column: counts of expected occurrences by outcome, the "
            "status the cell is coloured with, and the first completion day",
            files["grid"]: "the grid as shown in the app, one column per cell",
            files["rows"]: "totals per row; adherence is done divided by occurrences whose window has closed",
            files["occurrences"]: "one row per expected occurrence and per completion of it, with the time window",
        },
    }
    if grid_json:
        info["columns"][files["gridJson"]] = (
            "the grid exactly as the app renders it: columns, rows with cells {e, d, l, p, m, u, x, s, t}, "
            "totals, legend, notes, and the view parameters"
        )
        (folder / files["gridJson"]).write_text(json.dumps(grid, indent=2, ensure_ascii=False))
    if workbook:
        info["columns"][files["workbook"]] = (
            "the same content as one Excel workbook: Info, Grid (coloured as in the app), and one sheet per CSV"
        )
    (folder / files["info"]).write_text(json.dumps(info, indent=2, ensure_ascii=False))
    if workbook:
        job.progress(stage="Writing the Excel workbook")
        try:
            write_workbook(
                folder / files["workbook"],
                info=info,
                grid=grid,
                tables={"Cells": cells, "Activities" if focused else "Participants": rows, "Occurrences": occurrences},
            )
        except ImportError:
            files.pop("workbook")
            info["columns"].pop(OPTIONAL_DASHBOARD_FILES["workbook"], None)
            info["files"] = files
            (folder / files["info"]).write_text(json.dumps(info, indent=2, ensure_ascii=False))
            job.warn(
                "The Excel workbook was skipped: the openpyxl package is not installed. "
                "Run `pip install -e <path to curious-data-download>` again to add it."
            )
    for note in grid.get("notes") or []:
        job.warn(note)
    return {
        "folder": str(folder),
        "files": sorted(files.values()),
        "study": study["name"],
        "view": grid["view"],
        "counts": counts,
    }
