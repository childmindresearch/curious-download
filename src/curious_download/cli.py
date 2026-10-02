"""`curious-download`: interactive (or scripted) download of Curious answer data."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
import questionary
import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .client import EXPORT_ROLES, ApiError, CuriousClient, MfaRequiredError, Participant
from .crypto import AppletDecryptor, AppletPasswordError
from .csvout import write_csv
from .export import DEFAULT_PAGE_SIZE, ExportFilters, build_export, download_answers, write_jsonl
from .report import REPORT_COLUMNS

console = Console(stderr=True)
SETTINGS_PATH = Path.home() / ".config" / "curious-download" / "settings.json"
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class UsageError(Exception):
    """A problem with the user's input; printed without a traceback."""


# -- small helpers ----------------------------------------------------------


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_settings(**values: str) -> None:
    settings = _load_settings() | values
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    except OSError:
        pass


def _ask(question: questionary.Question) -> Any:
    """Ask, turning Ctrl-C into a clean exit."""
    return question.unsafe_ask()


def _parse_day(text: str, *, end_of_day: bool) -> datetime:
    """Parse YYYY-MM-DD (or YYYY-MM-DDTHH:MM[:SS]) in local time and convert to naive UTC."""
    text = text.strip()
    try:
        if "T" in text or " " in text:
            moment = datetime.fromisoformat(text)
        else:
            day = date.fromisoformat(text)
            moment = datetime.combine(day, time(23, 59, 59) if end_of_day else time(0, 0, 0))
    except ValueError as e:
        raise UsageError(f"Not a valid date: {text!r} (use YYYY-MM-DD)") from e
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(UTC).replace(tzinfo=None)


def _valid_day(text: str) -> bool | str:
    if not text.strip():
        return True
    try:
        _parse_day(text, end_of_day=False)
    except UsageError as e:
        return str(e)
    return True


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "applet"


# -- steps ------------------------------------------------------------------


def _login(client: CuriousClient, email: str, password: str | None, mfa_code: str | None, interactive: bool) -> None:
    attempts = 3 if interactive else 1
    for attempt in range(1, attempts + 1):
        if password is None:
            if not interactive:
                raise UsageError("No password given. Set CURIOUS_PASSWORD or run interactively.")
            password = _ask(questionary.password(f"Curious password for {email}:"))
        code_source: Any = mfa_code
        if code_source is None and interactive:
            code_source = lambda: _ask(questionary.text("Two-factor code (6 digits, or a recovery code):"))
        try:
            client.login(email, password, code_source)
            return
        except MfaRequiredError as e:
            raise UsageError(f"{e} Pass --mfa-code or run interactively.") from e
        except ApiError as e:
            if e.status_code in (400, 401) and attempt < attempts:
                console.print(f"[red]Login failed:[/red] {e.message}")
                password = None
                continue
            raise


def _resolve_applet(client: CuriousClient, applet: str | None, interactive: bool) -> tuple[dict, str]:
    """Return (applet details, workspace owner id)."""
    if applet and _UUID.match(applet):
        details = client.applet(applet)
        owner_id = details.get("ownerId")
        if owner_id:
            return details, owner_id

    workspaces = client.workspaces()
    if not workspaces:
        raise UsageError("Your account has no workspaces with applets you can export.")

    if applet:
        for workspace in workspaces:
            for row in client.workspace_applets(workspace["ownerId"]):
                if row.get("id") == applet or (row.get("displayName") or "").lower() == applet.lower():
                    return client.applet(row["id"]), workspace["ownerId"]
        raise UsageError(f"No applet called {applet!r} in your workspaces.")

    if not interactive:
        raise UsageError("No applet given. Pass --applet (ID or name).")

    if len(workspaces) == 1:
        workspace = workspaces[0]
    else:
        workspace = _ask(
            questionary.select(
                "Workspace:",
                choices=[questionary.Choice(w.get("workspaceName") or w["ownerId"], w) for w in workspaces],
            )
        )
    rows = [row for row in client.workspace_applets(workspace["ownerId"]) if row.get("role") in EXPORT_ROLES]
    if not rows:
        raise UsageError("You don't have an owner, manager, or reviewer role on any applet in this workspace.")
    rows.sort(key=lambda row: (row.get("displayName") or "").lower())
    choice = _ask(
        questionary.select(
            "Applet:",
            choices=[questionary.Choice(f"{row.get('displayName')}  ({row.get('role')})", row) for row in rows],
            use_search_filter=len(rows) > 10,
            use_jk_keys=False,
        )
    )
    return client.applet(choice["id"]), workspace["ownerId"]


def _unlock_applet(details: dict, applet_password: str | None, interactive: bool) -> AppletDecryptor:
    encryption = details.get("encryption")
    if not encryption:
        raise UsageError("This applet has no encryption settings, so its answers cannot be decrypted.")
    attempts = 3 if interactive else 1
    for attempt in range(1, attempts + 1):
        if applet_password is None:
            if not interactive:
                raise UsageError("No applet password given. Set CURIOUS_APPLET_PASSWORD or run interactively.")
            applet_password = _ask(questionary.password(f"Applet password for {details.get('displayName')}:"))
        try:
            return AppletDecryptor.from_password(applet_password, encryption)
        except AppletPasswordError:
            if attempt == attempts:
                raise
            console.print("[red]That applet password is not correct.[/red]")
            applet_password = None
    raise AssertionError("unreachable")


def _choose_participants(available: list[Participant], wanted: list[str], ask: bool) -> list[Participant]:
    if wanted:
        by_key: dict[str, Participant] = {}
        for participant in available:
            by_key[participant.secret_id] = participant
            by_key[participant.subject_id] = participant
        lowered = {key.lower(): value for key, value in by_key.items()}
        chosen, missing = [], []
        for key in wanted:
            match = by_key.get(key) or lowered.get(key.lower())
            (chosen if match else missing).append(match or key)
        if missing:
            raise UsageError(f"Unknown participant(s): {', '.join(missing)}")
        return chosen
    if not ask or not available:
        return []
    mode = _ask(questionary.select("Participants:", choices=["All participants", "Choose participants…"]))
    if mode == "All participants":
        return []
    return _ask(
        questionary.checkbox(
            "Select participants (space to toggle, type to search, enter to confirm):",
            choices=[questionary.Choice(p.label, p) for p in available],
            validate=lambda picked: bool(picked) or "Select at least one participant.",
            use_search_filter=True,
            use_jk_keys=False,
        )
    )


def _choose_dates(from_text: str | None, to_text: str | None, ask: bool) -> tuple[datetime | None, datetime | None]:
    if from_text or to_text or not ask:
        start = _parse_day(from_text, end_of_day=False) if from_text else None
        end = _parse_day(to_text, end_of_day=True) if to_text else None
        if start and end and start > end:
            raise UsageError("--from is after --to.")
        return start, end
    mode = _ask(questionary.select("Date range:", choices=["All time", "Last 7 days", "Last 30 days", "Custom range…"]))
    today = datetime.now().astimezone().date()
    if mode == "All time":
        return None, None
    if mode in ("Last 7 days", "Last 30 days"):
        days = 7 if mode == "Last 7 days" else 30
        return (
            _parse_day((today - timedelta(days=days)).isoformat(), end_of_day=False),
            _parse_day(today.isoformat(), end_of_day=True),
        )
    start_text = _ask(questionary.text("From (YYYY-MM-DD, blank for no limit):", validate=_valid_day))
    end_text = _ask(questionary.text("To (YYYY-MM-DD, blank for no limit):", validate=_valid_day))
    return _choose_dates(start_text or None, end_text or None, ask=False) if (start_text or end_text) else (None, None)


def _choose_activities(details: dict, activities: list[str], flows: list[str], ask: bool) -> tuple[set, set, list]:
    """Return (activity ids, flow ids, labels)."""
    applet_activities = details.get("activities") or []
    applet_flows = details.get("activityFlows") or []

    def resolve(wanted: list[str], pool: list[dict], kind: str) -> list[dict]:
        found = []
        for key in wanted:
            match = next((e for e in pool if e.get("id") == key or (e.get("name") or "").lower() == key.lower()), None)
            if match is None:
                names = ", ".join(repr(e.get("name")) for e in pool) or "none"
                raise UsageError(f"Unknown {kind} {key!r}. Available: {names}")
            found.append(match)
        return found

    if activities or flows:
        picked_activities = resolve(activities, applet_activities, "activity")
        picked_flows = resolve(flows, applet_flows, "flow")
    elif ask and (len(applet_activities) + len(applet_flows)) > 1:
        mode = _ask(questionary.select("Activities:", choices=["All activities", "Choose activities…"]))
        if mode == "All activities":
            return set(), set(), []
        choices = [questionary.Choice(a.get("name"), ("activity", a)) for a in applet_activities]
        choices += [questionary.Choice(f"Flow: {f.get('name')}", ("flow", f)) for f in applet_flows]
        picked = _ask(
            questionary.checkbox(
                "Select activities (space to toggle, enter to confirm):",
                choices=choices,
                validate=lambda chosen: bool(chosen) or "Select at least one activity.",
            )
        )
        picked_activities = [entry for kind, entry in picked if kind == "activity"]
        picked_flows = [entry for kind, entry in picked if kind == "flow"]
    else:
        return set(), set(), []

    labels = [a.get("name") for a in picked_activities] + [f"Flow: {f.get('name')}" for f in picked_flows]
    return {a["id"] for a in picked_activities}, {f["id"] for f in picked_flows}, labels


def _equivalent_command(server: str, email: str, applet_id: str, participants, start, end, activity_labels, output):
    parts = ["curious-download", "--server", server, "--email", email, "--applet", applet_id]
    for participant in participants:
        parts += ["--participant", participant.secret_id or participant.subject_id]
    if start:
        parts += ["--from", start.replace(tzinfo=UTC).astimezone().strftime("%Y-%m-%dT%H:%M:%S")]
    if end:
        parts += ["--to", end.replace(tzinfo=UTC).astimezone().strftime("%Y-%m-%dT%H:%M:%S")]
    for label in activity_labels:
        if label.startswith("Flow: "):
            parts += ["--flow", label.removeprefix("Flow: ")]
        else:
            parts += ["--activity", label]
    parts += ["--output", str(output)]
    return " ".join(shlex.quote(part) for part in parts)


# -- command ----------------------------------------------------------------


def download(
    server: Annotated[
        str | None,
        typer.Option(envvar="CURIOUS_API_URL", help="Curious API address (the backend, not the admin site)."),
    ] = None,
    email: Annotated[str | None, typer.Option(envvar="CURIOUS_EMAIL", help="Your Curious login email.")] = None,
    applet: Annotated[str | None, typer.Option(help="Applet ID or exact name.")] = None,
    participant: Annotated[
        list[str] | None, typer.Option("--participant", "-p", help="Participant secret ID (repeat for several).")
    ] = None,
    from_date: Annotated[str | None, typer.Option("--from", help="First day, YYYY-MM-DD in local time.")] = None,
    to_date: Annotated[str | None, typer.Option("--to", help="Last day, YYYY-MM-DD in local time.")] = None,
    activity: Annotated[
        list[str] | None, typer.Option("--activity", "-a", help="Activity name or ID (repeat for several).")
    ] = None,
    flow: Annotated[list[str] | None, typer.Option("--flow", help="Activity flow name or ID (repeat).")] = None,
    output: Annotated[Path, typer.Option("--output", "-o", help="Folder to write exports into.")] = Path(
        "curious_exports"
    ),
    page_size: Annotated[int, typer.Option(min=1, max=10000, help="Answers per API request.")] = DEFAULT_PAGE_SIZE,
    save_decrypted: Annotated[
        bool, typer.Option(help="Also save the decrypted answers as JSON lines (for debugging).")
    ] = False,
    subscale_null_when_skipped: Annotated[
        bool, typer.Option(help="Score skipped items as missing instead of 0 (admin feature flag).")
    ] = False,
    mfa_code: Annotated[str | None, typer.Option(help="Two-factor code, if your account uses MFA.")] = None,
    no_input: Annotated[bool, typer.Option(help="Never prompt; anything not given means 'all'.")] = False,
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    """Download and decrypt Curious answers into an admin-compatible responses.csv.

    Run it without options for a step-by-step wizard. Every answer can also be
    given as an option, so the same download can be scripted and re-run.
    Passwords are never saved; for scripts use CURIOUS_PASSWORD and
    CURIOUS_APPLET_PASSWORD.
    """
    if version:
        console.print(f"curious-download {__version__}")
        raise typer.Exit()

    interactive = not no_input and sys.stdin.isatty()
    # Ask about filters only when the user didn't give any on the command line.
    ask_filters = interactive and not (participant or from_date or to_date or activity or flow)
    settings = _load_settings()

    try:
        if not server:
            if not interactive:
                raise UsageError("No server given. Pass --server or set CURIOUS_API_URL.")
            server = _ask(
                questionary.text(
                    "Curious API address:",
                    default=settings.get("server", ""),
                    validate=lambda v: v.startswith(("http://", "https://")) or "Start with https://",
                )
            )
        if not email:
            if not interactive:
                raise UsageError("No email given. Pass --email or set CURIOUS_EMAIL.")
            email = _ask(questionary.text("Email:", default=settings.get("email", "")))

        with CuriousClient(server) as client:
            _login(client, email, os.environ.get("CURIOUS_PASSWORD"), mfa_code, interactive)
            _save_settings(server=server, email=email)
            console.print(f"[green]Logged in[/green] as {email}")

            details, owner_id = _resolve_applet(client, applet, interactive)
            applet_name = details.get("displayName") or details["id"]
            decryptor = _unlock_applet(details, os.environ.get("CURIOUS_APPLET_PASSWORD"), interactive)
            console.print(f"[green]Applet unlocked:[/green] {applet_name}")

            participants = _choose_participants(
                client.participants(owner_id, details["id"]) if (participant or ask_filters) else [],
                participant or [],
                ask_filters,
            )
            start, end = _choose_dates(from_date, to_date, ask_filters)
            activity_ids, flow_ids, activity_labels = _choose_activities(
                details, activity or [], flow or [], ask_filters
            )
            filters = ExportFilters(
                target_subject_ids=[p.subject_id for p in participants],
                from_utc=start,
                to_utc=end,
                activity_ids=activity_ids,
                flow_ids=flow_ids,
            )

            summary = Table(show_header=False, box=None)
            summary.add_row("Applet", applet_name)
            summary.add_row("Participants", ", ".join(p.label for p in participants) or "all")
            summary.add_row("From (UTC)", filters.api_from or "beginning")
            summary.add_row("To (UTC)", filters.api_to or "now")
            summary.add_row("Activities", ", ".join(activity_labels) or "all")
            console.print(summary)
            if interactive and not _ask(questionary.confirm("Download now?", default=True)):
                raise typer.Exit(1)

            with Progress(
                TextColumn("Downloading answers"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("download", total=None)
                answers, activity_defs = download_answers(
                    client,
                    details["id"],
                    filters,
                    page_size=page_size,
                    on_progress=lambda done, total: progress.update(task, completed=done, total=total),
                )

        result = build_export(
            answers,
            activity_defs,
            decryptor,
            filters,
            null_when_skipped=subscale_null_when_skipped,
            keep_decrypted=save_decrypted,
        )

        export_dir = output / f"{_slug(applet_name)}_{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"
        export_dir.mkdir(parents=True, exist_ok=True)
        write_csv(export_dir / "responses.csv", result.rows, REPORT_COLUMNS)
        if save_decrypted:
            write_jsonl(export_dir / "decrypted_answers.jsonl", result.decrypted)
        info = {
            "tool": f"curious-download {__version__}",
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
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
        }
        (export_dir / "export_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))

        console.print(
            f"[green]Done.[/green] {result.answers_kept} submissions → {len(result.rows)} rows in "
            f"[bold]{export_dir / 'responses.csv'}[/bold]"
        )
        if result.failures:
            console.print(
                f"[yellow]{len(result.failures)} submission(s) could not be decrypted; see export_info.json.[/yellow]"
            )
        if interactive:
            console.print("\nTo repeat this download without the questions:")
            console.print(
                _equivalent_command(server, email, details["id"], participants, start, end, activity_labels, output),
                soft_wrap=True,
                highlight=False,
            )
    except KeyboardInterrupt:
        console.print("\nCancelled.")
        raise typer.Exit(130) from None
    except (UsageError, AppletPasswordError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None
    except ApiError as e:
        console.print(f"[red]The Curious server refused the request:[/red] {e}")
        raise typer.Exit(1) from None
    except httpx.HTTPError as e:
        console.print(f"[red]Could not reach {server}:[/red] {e}")
        raise typer.Exit(1) from None


def main() -> None:
    typer.run(download)
