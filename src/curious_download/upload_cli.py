"""Terminal side of `curious-download --convert-redcap`, `--upload`, and `--restore`."""

from __future__ import annotations

import json
import shlex
from collections import Counter
from datetime import datetime
from pathlib import Path

import questionary
from rich.markup import escape
from rich.table import Table

from . import __version__
from .cli import UsageError, _ask, _choose_workspace, _find_applet, console
from .client import ApiError, CuriousClient
from .csvout import file_slug, write_csv
from .itembuilder import ActivityRows, DictionaryError, Problem, dictionary_files, read_dictionaries
from .redcap import Decisions, RedcapError, convert_redcap, write_conversion
from .upload import (
    EDIT_ROLES,
    PLAN_COLUMNS,
    StaleAppletError,
    UploadPlan,
    apply_plan,
    plan_upload,
    restore_body,
    verify,
)

PROBLEM_COLUMNS = ["severity", "activity", "item", "message", "where"]
_MAX_SHOWN = 25


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def _ask_path(question: str) -> Path:
    text = _ask(questionary.path(question, validate=lambda v: bool(v.strip()) or "Enter a path."))
    # Paths dragged into a terminal come quoted or with escaped spaces.
    text = text.strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return Path(text.replace("\\ ", " ")).expanduser()


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# -- REDCap conversion ------------------------------------------------------------------------------


def _ask_choice(question: str, options: list[tuple[str, str]], context: str) -> str:
    if context:
        console.print(f"[dim]{escape(context)}[/dim]")
    choices = [
        questionary.Choice(f"{label} (Recommended)" if index == 0 else label, value)
        for index, (value, label) in enumerate(options)
    ]
    return _ask(questionary.select(question, choices=choices))


def _ask_many(question: str, options: list[tuple[str, str]]) -> list[str]:
    return _ask(
        questionary.checkbox(
            question + " (space to toggle, enter to confirm)",
            choices=[questionary.Choice(label, value, checked=True) for value, label in options],
            validate=lambda picked: bool(picked) or "Choose at least one.",
        )
    )


def _ask_text(question: str, default: str) -> str:
    return _ask(questionary.text(question, default=default))


def run_convert(source: Path | None, decisions_file: Path | None, output: Path, interactive: bool) -> Path:
    """Convert a REDCap data dictionary; returns the folder with the Curious dictionaries."""
    if source is None:
        if not interactive:
            raise UsageError("No REDCap file given. Pass --convert-redcap PATH.")
        source = _ask_path("REDCap data dictionary (CSV file):")
    if not source.is_file():
        raise UsageError(f"{source} is not a file.")
    preset = {}
    if decisions_file is not None:
        try:
            preset = json.loads(decisions_file.read_text())
        except (OSError, ValueError) as e:
            raise UsageError(f"Can't read the decisions file {decisions_file}: {e}") from None
    decisions = Decisions(preset, _ask_choice, _ask_text, _ask_many) if interactive else Decisions(preset)
    try:
        conversion = convert_redcap(source, decisions)
    except RedcapError as e:
        raise UsageError(str(e)) from None

    folder = output / f"redcap_{file_slug(source.stem)}_{_stamp()}"
    files = write_conversion(conversion, folder, source)
    kinds = Counter(note.kind for note in conversion.notes)
    _write_json(
        folder / "export_info.json",
        {
            "tool": f"curious-download {__version__}",
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "kind": "redcap conversion",
            "source": str(source),
            "activities": {
                name: {"redcap_form": conversion.forms[name], "items": len(rows)}
                for name, rows in conversion.activities.items()
            },
            "item_types": dict(conversion.counts),
            "notes": dict(kinds),
            "decisions": conversion.decisions,
        },
    )

    table = Table(title="Converted", title_justify="left")
    table.add_column("Activity")
    table.add_column("REDCap form")
    table.add_column("Items", justify="right")
    table.add_column("File")
    for name, path in zip(conversion.activities, files):
        table.add_row(escape(name), escape(conversion.forms[name]), str(len(conversion.activities[name])), path.name)
    console.print(table)
    console.print("Item types: " + ", ".join(f"{kind} {count}" for kind, count in conversion.counts.most_common()))
    warnings = [n for n in conversion.notes if n.kind == "warning"]
    for note in warnings[:_MAX_SHOWN]:
        where = note.variable or (f"line {note.line}" if note.line else "")
        console.print(f"[yellow]{escape(where + ': ' if where else '')}{escape(note.message)}[/yellow]")
    if len(warnings) > _MAX_SHOWN:
        console.print(f"[yellow]… and {len(warnings) - _MAX_SHOWN} more warnings.[/yellow]")
    console.print(
        f"[green]Done.[/green] {sum(len(r) for r in conversion.activities.values())} items in [bold]{folder}[/bold]. "
        f"conversion_report.csv lists all {len(conversion.notes)} notes and decisions."
    )
    console.print(
        "Check the CSV files before uploading; you can edit them (for example shown_if or value_labels).",
    )
    if interactive:
        parts = ["curious-download", "--convert-redcap", str(source)]
        parts += ["--decisions", str(folder / "redcap_decisions.json"), "--output", str(output)]
        console.print("\nTo convert again with the same answers (also for a similar file):")
        console.print(" ".join(shlex.quote(p) for p in parts), soft_wrap=True, highlight=False)
    return folder


# -- upload -------------------------------------------------------------------------------------------


def _choose_editable_applet(
    client: CuriousClient, applet: str | None, workspace: str | None, interactive: bool
) -> tuple[dict, str]:
    if applet:
        row, owner_id = _find_applet(client, applet, workspace)
    elif interactive:
        chosen = _choose_workspace(client, workspace, interactive)
        owner_id = chosen["ownerId"]
        rows = [
            r for r in client.workspace_applets(owner_id) if r.get("type") != "folder" and r.get("role") in EDIT_ROLES
        ]
        if not rows:
            raise UsageError("You aren't an owner, manager, or editor of any applet in this workspace.")
        rows.sort(key=lambda r: (r.get("displayName") or "").lower())
        row = _ask(
            questionary.select(
                "Upload into which applet?",
                choices=[questionary.Choice(f"{r.get('displayName')}  ({r.get('role')})", r) for r in rows],
                use_search_filter=len(rows) > 10,
                use_jk_keys=False,
            )
        )
    else:
        raise UsageError("No applet given. Pass --applet (ID or name) for --upload.")
    if row.get("role") not in EDIT_ROLES:
        raise UsageError(
            f"Your role on {row.get('displayName')!r} is {row.get('role') or 'unknown'}; "
            "saving an applet needs owner, manager, or editor."
        )
    return row, owner_id


def _print_problems(problems: list[Problem]) -> None:
    for problem in problems[:_MAX_SHOWN]:
        color = "red" if problem.severity == "error" else "yellow"
        console.print(f"[{color}]{escape(str(problem))}[/{color}]")
    if len(problems) > _MAX_SHOWN:
        console.print(f"… and {len(problems) - _MAX_SHOWN} more (see upload_problems.csv).")


def _print_plan(plan: UploadPlan) -> None:
    table = Table(title="Changes", title_justify="left")
    for column in ("Activity", "Added", "Changed", "Removed", "Kept (not in file)", "Unchanged"):
        table.add_column(column, justify="left" if column == "Activity" else "right")
    activities = list(dict.fromkeys(c.activity for c in plan.changes))
    for activity in activities:
        counts = Counter(c.action for c in plan.changes if c.activity == activity)
        label = activity + (" (new)" if activity in plan.new_activities else "")
        if counts["renamed"]:
            label += " (renamed)"
        if counts["reordered"]:
            label += " (reordered)"
        table.add_row(
            escape(label),
            *(str(counts[a]) for a in ("added", "changed", "removed", "kept", "unchanged")),
        )
    console.print(table)
    detailed = [c for c in plan.changes if c.action in ("changed", "removed") and c.item and c.details]
    for change in detailed[:_MAX_SHOWN]:
        console.print(
            f"  {escape(change.activity)} / {escape(change.item)}: {change.action} — {escape(change.details)}"
        )
    if len(detailed) > _MAX_SHOWN:
        console.print(f"  … and {len(detailed) - _MAX_SHOWN} more (see upload_plan.csv).")


def load_upload(source: Path | None, interactive: bool) -> tuple[Path, list[ActivityRows]]:
    """Read the dictionary to upload (before logging in, so file problems show up first)."""
    if source is None:
        if not interactive:
            raise UsageError("No dictionary given. Pass --upload FILE or FOLDER.")
        source = _ask_path("Data dictionary to upload (a CSV file or a folder of them):")
    try:
        activities = read_dictionaries(dictionary_files(source))
    except DictionaryError as e:
        raise UsageError(str(e)) from None
    total = sum(len(a.items) for a in activities)
    console.print(
        f"Read {total} items in {len(activities)} activit{'y' if len(activities) == 1 else 'ies'} from {source}"
    )
    return source, activities


def run_upload(
    client: CuriousClient,
    server: str,
    email: str,
    source: Path,
    activities: list[ActivityRows],
    applet: str | None,
    workspace: str | None,
    output: Path,
    interactive: bool,
    yes: bool,
    remove_missing: bool | None,
) -> None:
    row, owner_id = _choose_editable_applet(client, applet, workspace, interactive)
    applet_name = row.get("displayName") or row["id"]
    current = client.applet_full(owner_id, row["id"])

    def extras(activity: str, names: list[str]) -> str:
        if remove_missing is not None:
            return "remove" if remove_missing else "keep"
        if not interactive:
            return "keep"
        shown = ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")
        console.print(
            f"[bold]{escape(activity)}[/bold] has {len(names)} item(s) or subscale(s) the dictionary doesn't list: {escape(shown)}"
        )
        return _ask(
            questionary.select(
                "What should happen to them?",
                choices=[
                    questionary.Choice("Keep them (Recommended)", "keep"),
                    questionary.Choice("Remove them from the activity", "remove"),
                ],
            )
        )

    plan = plan_upload(current, activities, extras)
    folder = output / f"upload_{file_slug(applet_name)}_{_stamp()}"
    folder.mkdir(parents=True, exist_ok=True)
    _write_json(folder / "applet_before.json", current)
    _write_json(folder / "upload_body.json", plan.body)
    write_csv(folder / "upload_plan.csv", [vars(c) for c in plan.changes], PLAN_COLUMNS)
    write_csv(folder / "upload_problems.csv", [vars(p) for p in plan.problems], PROBLEM_COLUMNS)
    info = {
        "tool": f"curious-download {__version__}",
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": "upload",
        "server": server,
        "source": str(source),
        "applet": {"id": row["id"], "name": applet_name, "version_before": current.get("version")},
        "changes": plan.counts(),
        "new_activities": plan.new_activities,
        "errors": len(plan.errors),
        "warnings": len(plan.warnings),
        "saved": False,
    }

    def finish() -> None:
        _write_json(folder / "export_info.json", info)

    _print_problems(plan.problems)
    if plan.errors:
        finish()
        raise UsageError(
            f"{len(plan.errors)} problem(s) must be fixed in the dictionary first. Nothing was saved. "
            f"All problems are in {folder / 'upload_problems.csv'}."
        )
    if not plan.has_changes:
        finish()
        console.print(f"[green]Nothing to change:[/green] {escape(applet_name)} already matches the dictionary.")
        return
    _print_plan(plan)
    console.print(
        f"Saving creates a new version of {escape(applet_name)} (now {current.get('version')}) "
        "and sends its participants an 'Applet is updated' notification."
    )
    if interactive:
        if not _ask(questionary.confirm("Save these changes?", default=False)):
            finish()
            console.print(f"Not saved. The plan is in [bold]{folder}[/bold].")
            return
    elif not yes:
        finish()
        console.print(
            f"[yellow]Dry run:[/yellow] nothing was saved. Add --yes to save. The plan is in [bold]{folder}[/bold]."
        )
        return

    try:
        after = apply_plan(client, owner_id, plan)
    except StaleAppletError as e:
        finish()
        raise UsageError(str(e)) from None
    except ApiError as e:
        finish()
        raise UsageError(f"The server refused the changes, so nothing was saved: {e}") from None
    _write_json(folder / "applet_after.json", after)
    mismatches = verify(plan, after)
    info |= {"saved": True, "applet": {**info["applet"], "version_after": after.get("version")}, "verify": mismatches}
    finish()
    for mismatch in mismatches:
        console.print(f"[yellow]{escape(mismatch)}[/yellow]")
    console.print(
        f"[green]Saved.[/green] {escape(applet_name)} is now version {after.get('version')}. "
        f"Details in [bold]{folder}[/bold]."
    )
    undo = ["curious-download", "--restore", str(folder / "applet_before.json"), "--server", server, "--email", email]
    console.print("To undo it:")
    console.print(" ".join(shlex.quote(p) for p in undo), soft_wrap=True, highlight=False)


# -- restore ------------------------------------------------------------------------------------------


def load_backup(backup_path: Path) -> dict:
    try:
        backup = json.loads(backup_path.read_text())
    except (OSError, ValueError) as e:
        raise UsageError(f"Can't read {backup_path}: {e}") from None
    if not isinstance(backup, dict) or not backup.get("id") or "activities" not in backup:
        raise UsageError(f"{backup_path} is not an applet saved by an upload (applet_before.json).")
    return backup


def run_restore(client: CuriousClient, backup: dict, output: Path, interactive: bool, yes: bool) -> None:
    row, owner_id = _find_applet(client, backup["id"], None)
    if row.get("role") not in EDIT_ROLES:
        raise UsageError("Restoring an applet needs owner, manager, or editor.")
    current = client.applet_full(owner_id, backup["id"])
    try:
        body, notes = restore_body(backup, current)
    except ValueError as e:
        raise UsageError(str(e)) from None

    table = Table(title=f"Restore {row.get('displayName')}", title_justify="left")
    table.add_column("Activity")
    table.add_column(f"Items now ({current.get('version')})", justify="right")
    table.add_column(f"Items after restore (as in {backup.get('version')})", justify="right")
    now = {a.get("name"): len(a.get("items") or []) for a in current.get("activities") or []}
    then = {a.get("name"): len(a.get("items") or []) for a in backup.get("activities") or []}
    for name in dict.fromkeys([*then, *now]):
        table.add_row(escape(name or ""), str(now.get(name, "—")), str(then.get(name, "—")))
    console.print(table)
    for note in notes:
        console.print(f"[yellow]{escape(note)}[/yellow]")
    console.print("Restoring saves a new version with the old content and notifies participants.")
    if interactive:
        if not _ask(questionary.confirm("Restore it?", default=False)):
            console.print("Not restored.")
            return
    elif not yes:
        console.print("[yellow]Dry run:[/yellow] nothing was changed. Add --yes to restore.")
        return
    folder = output / f"restore_{file_slug(row.get('displayName') or backup['id'])}_{_stamp()}"
    folder.mkdir(parents=True, exist_ok=True)
    _write_json(folder / "applet_before.json", current)
    try:
        after = apply_plan(client, owner_id, UploadPlan(applet=current, body=body))
    except (StaleAppletError, ApiError) as e:
        raise UsageError(f"Nothing was restored: {e}") from None
    _write_json(folder / "applet_after.json", after)
    console.print(f"[green]Restored.[/green] Now version {after.get('version')}. Details in [bold]{folder}[/bold].")
