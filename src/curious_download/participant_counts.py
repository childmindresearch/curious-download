"""`curious-participant-counts`: how many participants have submitted data to each applet in a workspace."""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import questionary
import typer
from rich.table import Table

from . import __version__
from .cli import UsageError, _ask, _load_settings, _login, _parse_day, _progress, _save_settings, _slug, console
from .client import ApiError, CuriousClient
from .csvout import write_csv
from .export import DEFAULT_PAGE_SIZE, ExportFilters

# Submission rows carry no answer content, so a large page is cheap (the backend's maximum).
SUBMISSIONS_PAGE_SIZE = 10000

COLUMNS = [
    "applet_id",
    "applet_name",
    "your_role",
    "participants_submitted",
    "participants_with_data",
    "submissions",
    "first_submission_utc",
    "last_submission_utc",
    "note",
]

NOTE_REVIEWER = "reviewer: only your assigned participants"
NOTE_NO_ACCESS = "no data access (needs owner, manager, or reviewer)"


def _utc(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment


@dataclass
class Tally:
    """Counts for one applet. A submission is one completed activity."""

    respondents: set[str] = field(default_factory=set)
    subjects: set[str] = field(default_factory=set)
    submissions: int = 0
    first: datetime | None = None
    last: datetime | None = None

    def add(self, respondent: str, subject: str, created_at: str | None) -> None:
        self.respondents.add(respondent)
        self.subjects.add(subject)
        self.submissions += 1
        if created_at:
            moment = _utc(created_at)
            self.first = moment if self.first is None else min(self.first, moment)
            self.last = moment if self.last is None else max(self.last, moment)


class SubmissionCounter:
    """Counts submitters per applet from submission metadata; never downloads or decrypts answers."""

    def __init__(self, client: CuriousClient, filters: ExportFilters):
        self.client = client
        self.filters = filters
        # Older backends lack /submissions; then the (heavier) export endpoint gives the same metadata.
        self.has_submissions_endpoint = True

    def count(self, applet_id: str) -> Tally:
        if self.has_submissions_endpoint:
            try:
                return self._from_submissions(applet_id)
            except ApiError as e:
                # FastAPI's own reply for an unknown route; the backend's errors have other messages.
                if e.status_code != 404 or e.message != "Not Found":
                    raise
                self.has_submissions_endpoint = False
        return self._from_answer_export(applet_id)

    def _from_submissions(self, applet_id: str) -> Tally:
        tally = Tally()
        reviewer_activities: set[str] | None = None
        page = 1
        while True:
            body = self.client.submissions_page(
                applet_id,
                page=page,
                limit=SUBMISSIONS_PAGE_SIZE,
                from_date=self.filters.api_from,
                to_date=self.filters.api_to,
            )
            rows = body.get("submissions") or []
            if rows and reviewer_activities is None:
                # For owners and managers the list includes reviewers' assessments, which aren't
                # participant data. They are answers to reviewer-only (reviewable) activities.
                activities = self.client.applet(applet_id).get("activities") or []
                reviewer_activities = {a["id"] for a in activities if a.get("isReviewable")}
            for row in rows:
                if row.get("activityId") in (reviewer_activities or ()):
                    continue
                tally.add(row["respondentSubjectId"], row["targetSubjectId"], row.get("createdAt"))
            if not rows or page * SUBMISSIONS_PAGE_SIZE >= (body.get("submissionsCount") or 0):
                return tally
            page += 1

    def _from_answer_export(self, applet_id: str) -> Tally:
        tally = Tally()
        page = 1
        while True:
            body = self.client.export_page(
                applet_id,
                page=page,
                limit=DEFAULT_PAGE_SIZE,
                from_date=self.filters.api_from,
                to_date=self.filters.api_to,
            )
            answers = (body.get("result") or {}).get("answers") or []
            for answer in answers:
                if answer.get("reviewedAnswerId"):
                    continue  # a reviewer's assessment
                respondent = answer.get("respondentId") or ""
                tally.add(respondent, answer.get("targetSubjectId") or respondent, answer.get("createdAt"))
            if not answers or page * DEFAULT_PAGE_SIZE >= (body.get("count") or 0):
                return tally
            page += 1


def _format(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else ""


def count_workspace(
    client: CuriousClient,
    applets: list[dict],
    filters: ExportFilters,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """One CSV row per applet. Applets that can't be counted get a note instead of numbers."""
    counter = SubmissionCounter(client, filters)
    rows = []
    for done, applet in enumerate(applets, start=1):
        role = applet.get("role") or ""
        row = {"applet_id": applet["id"], "applet_name": applet.get("displayName") or "", "your_role": role}
        try:
            tally = counter.count(applet["id"])
        except ApiError as e:
            row["note"] = NOTE_NO_ACCESS if e.status_code == 403 else f"error: {e}"
        else:
            row |= {
                "participants_submitted": len(tally.respondents),
                "participants_with_data": len(tally.subjects),
                "submissions": tally.submissions,
                "first_submission_utc": _format(tally.first),
                "last_submission_utc": _format(tally.last),
                "note": NOTE_REVIEWER if role == "reviewer" else "",
            }
        rows.append(row)
        if on_progress:
            on_progress(done, len(applets))
    return rows


def _choose_workspace(client: CuriousClient, workspace: str | None, interactive: bool) -> dict:
    workspaces = client.workspaces()
    if not workspaces:
        raise UsageError("Your account has no workspaces.")
    names = ", ".join(repr(w.get("workspaceName") or w["ownerId"]) for w in workspaces)
    if workspace:
        for candidate in workspaces:
            if candidate["ownerId"] == workspace or (candidate.get("workspaceName") or "").lower() == workspace.lower():
                return candidate
        raise UsageError(f"No workspace called {workspace!r}. Available: {names}")
    if len(workspaces) == 1:
        return workspaces[0]
    if not interactive:
        raise UsageError(f"You have several workspaces; pass --workspace (name or owner ID). Available: {names}")
    return _ask(
        questionary.select(
            "Workspace:",
            choices=[questionary.Choice(w.get("workspaceName") or w["ownerId"], w) for w in workspaces],
        )
    )


def _print_table(rows: list[dict], workspace_name: str) -> None:
    table = Table(title=f"Participants who submitted data — {workspace_name}", title_justify="left")
    table.add_column("Applet")
    table.add_column("Your role")
    table.add_column("Submitted", justify="right")
    table.add_column("With data", justify="right")
    table.add_column("Submissions", justify="right")
    table.add_column("Last submission (UTC)")
    table.add_column("Note", style="yellow")
    for row in rows:
        table.add_row(
            row["applet_name"],
            row["your_role"],
            str(row.get("participants_submitted", "")),
            str(row.get("participants_with_data", "")),
            str(row.get("submissions", "")),
            row.get("last_submission_utc", "")[:10],
            row.get("note", ""),
        )
    console.print(table)
    console.print(
        "[dim]Submitted: people who completed at least one activity. With data: participants the answers are "
        "about. They differ only when someone answers about another person (e.g. a parent about a child). "
        "Reviewer assessments are not counted.[/dim]"
    )


def participant_counts(
    server: Annotated[
        str | None,
        typer.Option(envvar="CURIOUS_API_URL", help="Curious API address (the backend, not the admin site)."),
    ] = None,
    email: Annotated[str | None, typer.Option(envvar="CURIOUS_EMAIL", help="Your Curious login email.")] = None,
    workspace: Annotated[str | None, typer.Option(help="Workspace name or owner ID.")] = None,
    from_date: Annotated[
        str | None, typer.Option("--from", help="Only count submissions from this day, YYYY-MM-DD in local time.")
    ] = None,
    to_date: Annotated[
        str | None, typer.Option("--to", help="Only count submissions up to this day, YYYY-MM-DD in local time.")
    ] = None,
    output: Annotated[Path, typer.Option("--output", "-o", help="Folder to write the CSV into.")] = Path(
        "curious_exports"
    ),
    mfa_code: Annotated[str | None, typer.Option(help="Two-factor code, if your account uses MFA.")] = None,
    no_input: Annotated[bool, typer.Option(help="Never prompt.")] = False,
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    """Count the participants who have submitted data to each applet in a workspace.

    Only submission metadata is read, so no applet passwords are needed and no answers are downloaded.
    The counts are printed and saved as a CSV.
    """
    if version:
        console.print(f"curious-participant-counts {__version__}")
        raise typer.Exit()

    interactive = not no_input and sys.stdin.isatty()
    settings = _load_settings()

    try:
        start = _parse_day(from_date, end_of_day=False) if from_date else None
        end = _parse_day(to_date, end_of_day=True) if to_date else None
        if start and end and start > end:
            raise UsageError("--from is after --to.")
        filters = ExportFilters(from_utc=start, to_utc=end)

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

            chosen = _choose_workspace(client, workspace, interactive)
            workspace_name = chosen.get("workspaceName") or chosen["ownerId"]
            applets = [row for row in client.workspace_applets(chosen["ownerId"]) if row.get("type") != "folder"]
            applets.sort(key=lambda row: (row.get("displayName") or "").lower())
            if not applets:
                raise UsageError(f"The workspace {workspace_name!r} has no applets.")

            with _progress(f"Counting {len(applets)} applets") as progress:
                task = progress.add_task("count", total=len(applets))
                rows = count_workspace(
                    client,
                    applets,
                    filters,
                    on_progress=lambda done, total: progress.update(task, completed=done, total=total),
                )

        output.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        csv_path = output / f"participant_counts_{_slug(workspace_name)}_{stamp}.csv"
        write_csv(csv_path, rows, COLUMNS)

        _print_table(rows, workspace_name)
        if start or end:
            console.print(f"Date range (UTC): {filters.api_from or 'beginning'} to {filters.api_to or 'now'}")
        console.print(f"[green]Saved[/green] [bold]{csv_path}[/bold]")

        if interactive:
            parts = ["curious-participant-counts", "--server", server, "--email", email]
            parts += ["--workspace", chosen["ownerId"]]
            if from_date:
                parts += ["--from", from_date]
            if to_date:
                parts += ["--to", to_date]
            parts += ["--output", str(output)]
            console.print("\nTo repeat this without the questions:")
            console.print(" ".join(shlex.quote(part) for part in parts), soft_wrap=True, highlight=False)
    except KeyboardInterrupt:
        console.print("\nCancelled.")
        raise typer.Exit(130) from None
    except UsageError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None
    except ApiError as e:
        console.print(f"[red]The Curious server refused the request:[/red] {e}")
        raise typer.Exit(1) from None
    except httpx.HTTPError as e:
        console.print(f"[red]Could not reach {server}:[/red] {e}")
        raise typer.Exit(1) from None


def main() -> None:
    typer.run(participant_counts)
