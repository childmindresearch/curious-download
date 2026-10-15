"""`curious-app`: serve the local web app and its JSON API on 127.0.0.1.

Security model: the server binds to localhost only, and every /api request must carry the
random token that `main` puts in the launch URL. The custom header forces a CORS preflight,
so web pages from other sites cannot call the API, and the Host check stops DNS rebinding.
Passwords and decryptors live only in memory, never on disk.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..cli import UsageError, _load_settings, _parse_day, _save_settings
from ..client import EXPORT_ROLES, ApiError, CuriousClient, MfaRequiredError, Participant
from ..crypto import AppletDecryptor, AppletPasswordError
from ..dashboard import config as studies
from ..dashboard.loader import host_tz_offset_minutes, load_study, today_local
from ..dashboard.views import GridResult, build, grid_csv, long_csv, view_params
from ..export import DEFAULT_PAGE_SIZE, ExportFilters
from . import runners
from .jobs import JobManager

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
STATIC_DIR = Path(__file__).parent / "static"


class Session:
    """The one logged-in Curious connection, plus the applets unlocked with their password."""

    def __init__(self, client: CuriousClient, server: str, email: str):
        self.client = client
        self.server = server
        self.email = email
        self.decryptors: dict[str, AppletDecryptor] = {}


class AppState:
    def __init__(self, token: str, client_factory):
        self.token = token
        self.client_factory = client_factory
        self.session: Session | None = None
        self.jobs = JobManager()
        # Dashboard data lives only in memory, per login: study id -> StudyData.
        self.dashboards: dict = {}
        self.dashboard_views: dict = {}  # (study id, request signature) -> GridResult
        self.dashboard_jobs: dict[str, str] = {}  # study id -> job id of its latest load


# -- request bodies ----------------------------------------------------------


class LoginBody(BaseModel):
    server: str
    email: str
    password: str
    mfaCode: str | None = None


class UnlockBody(BaseModel):
    password: str


class ChosenParticipant(BaseModel):
    subjectId: str
    secretId: str = ""


class AnswersJobBody(BaseModel):
    appletId: str
    participants: list[ChosenParticipant] = Field(default_factory=list)  # empty means all
    fromDate: str | None = None  # YYYY-MM-DD (or with time), local time
    toDate: str | None = None
    activityIds: list[str] = Field(default_factory=list)  # empty means all
    flowIds: list[str] = Field(default_factory=list)
    media: bool = False
    journey: bool = False
    outputDir: str
    pageSize: int = DEFAULT_PAGE_SIZE
    saveDecrypted: bool = False
    subscaleNullWhenSkipped: bool = False


class DictionaryJobBody(BaseModel):
    ownerId: str
    appletIds: list[str] = Field(default_factory=list)
    allApplets: bool = False
    currentOnly: bool = False
    outputDir: str


class CountsJobBody(BaseModel):
    ownerId: str
    fromDate: str | None = None
    toDate: str | None = None
    outputDir: str


class RevealBody(BaseModel):
    path: str


# -- app ----------------------------------------------------------------------


def create_app(
    *,
    token: str,
    client_factory=CuriousClient,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    state = AppState(token, client_factory)
    hosts = {host.lower() for host in (allowed_hosts or LOCAL_HOSTS)}
    app = FastAPI(title="Curious app", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    def session() -> Session:
        if state.session is None:
            raise HTTPException(401, "Not logged in.")
        return state.session

    def default_output() -> str:
        return _load_settings().get("output_dir") or str(Path.home() / "curious_exports")

    def output_dir(text: str) -> Path:
        text = (text or "").strip()
        if not text:
            raise HTTPException(400, "Choose an output folder.")
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path.home() / path
        _save_settings(output_dir=str(path))
        return path

    def parse_dates(from_text: str | None, to_text: str | None):
        start = _parse_day(from_text, end_of_day=False) if from_text else None
        end = _parse_day(to_text, end_of_day=True) if to_text else None
        if start and end and start > end:
            raise UsageError("The start date is after the end date.")
        return start, end

    # -- guards and error shapes ----------------------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host not in hosts:
            return JSONResponse({"error": "Forbidden host."}, status_code=403)
        if request.url.path.startswith("/api/") and request.headers.get("x-curious-token") != state.token:
            return JSONResponse(
                {
                    "error": "Missing or wrong app token. Open the app with the link shown in the terminal.",
                    "badToken": True,
                },
                status_code=401,
            )
        return await call_next(request)

    @app.exception_handler(UsageError)
    async def usage_error(request: Request, exc: UsageError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        if exc.status_code == 401:
            return JSONResponse(
                {"error": f"The Curious login expired: {exc.message}. Log in again.", "sessionExpired": True},
                status_code=401,
            )
        return JSONResponse({"error": str(exc)}, status_code=502)

    @app.exception_handler(httpx.HTTPError)
    async def transport_error(request: Request, exc: httpx.HTTPError):
        return JSONResponse({"error": f"Could not reach the Curious server: {exc}"}, status_code=502)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    # -- session ----------------------------------------------------------------

    @app.get("/api/state")
    def get_state():
        settings = _load_settings()
        return {
            "version": __version__,
            "loggedIn": state.session is not None,
            "email": state.session.email if state.session else None,
            "server": state.session.server if state.session else None,
            "settings": {
                "server": settings.get("server", ""),
                "email": settings.get("email", ""),
                "outputDir": default_output(),
            },
        }

    @app.post("/api/login")
    def login(body: LoginBody):
        server = body.server.strip().rstrip("/")
        email = body.email.strip()
        if not server.startswith(("http://", "https://")):
            raise HTTPException(
                400, "The server address must start with https:// (the backend API, not the admin site)."
            )
        if not email:
            raise HTTPException(400, "Enter your email.")
        client = state.client_factory(server)
        try:
            client.login(email, body.password, body.mfaCode.strip() if body.mfaCode else None)
        except MfaRequiredError:
            client.close()
            return {"mfaRequired": True}
        except ApiError as e:
            client.close()
            if e.status_code in (400, 401, 403):
                raise HTTPException(401, e.message) from e
            raise
        except httpx.HTTPError:
            client.close()
            raise
        if state.session and not state.jobs.any_running():
            state.session.client.close()
        state.session = Session(client, server, email)
        state.dashboards.clear()
        state.dashboard_views.clear()
        _save_settings(server=server, email=email)
        return {"ok": True, "email": email, "server": server}

    @app.post("/api/logout")
    def logout():
        if state.jobs.any_running():
            raise HTTPException(400, "A download is still running. Cancel it or wait for it to finish first.")
        if state.session:
            state.session.client.close()
        state.session = None
        state.dashboards.clear()
        state.dashboard_views.clear()
        return {"ok": True}

    # -- browsing the account ----------------------------------------------------

    @app.get("/api/workspaces")
    def workspaces():
        rows = session().client.workspaces()
        return [{"ownerId": w["ownerId"], "name": w.get("workspaceName") or w["ownerId"]} for w in rows]

    @app.get("/api/workspaces/{owner_id}/applets")
    def workspace_applets(owner_id: str):
        rows = [row for row in session().client.workspace_applets(owner_id) if row.get("type") != "folder"]
        rows.sort(key=lambda row: (row.get("displayName") or "").lower())
        return [
            {
                "id": row["id"],
                "name": row.get("displayName") or row["id"],
                "role": row.get("role") or "",
                "canExport": row.get("role") in EXPORT_ROLES,
            }
            for row in rows
        ]

    @app.get("/api/applets/{applet_id}")
    def applet(applet_id: str):
        current = session()
        details = current.client.applet(applet_id)
        return {
            "id": details["id"],
            "name": details.get("displayName") or details["id"],
            "version": details.get("version"),
            "hasEncryption": bool(details.get("encryption")),
            "unlocked": applet_id in current.decryptors,
            "activities": [{"id": a["id"], "name": a.get("name")} for a in details.get("activities") or []],
            "flows": [{"id": f["id"], "name": f.get("name")} for f in details.get("activityFlows") or []],
        }

    @app.get("/api/workspaces/{owner_id}/applets/{applet_id}/participants")
    def applet_participants(owner_id: str, applet_id: str):
        rows = session().client.participants(owner_id, applet_id)
        return [{"subjectId": p.subject_id, "secretId": p.secret_id, "label": p.label} for p in rows]

    @app.post("/api/applets/{applet_id}/unlock")
    def unlock(applet_id: str, body: UnlockBody):
        current = session()
        encryption = current.client.applet(applet_id).get("encryption")
        if not encryption:
            raise HTTPException(400, "This applet has no encryption settings, so its answers cannot be decrypted.")
        try:
            decryptor = AppletDecryptor.from_password(body.password, encryption)
        except AppletPasswordError as e:
            raise HTTPException(400, "That applet password is not correct.") from e
        current.decryptors[applet_id] = decryptor
        return {"ok": True, "keyVariant": decryptor.key_variant}

    # -- jobs ---------------------------------------------------------------------

    @app.post("/api/jobs/answers")
    def start_answers(body: AnswersJobBody):
        current = session()
        decryptor = current.decryptors.get(body.appletId)
        if decryptor is None:
            raise HTTPException(400, "Unlock the applet with its password first.")
        details = current.client.applet(body.appletId)
        applet_name = details.get("displayName") or details["id"]
        start, end = parse_dates(body.fromDate, body.toDate)
        activity_ids, flow_ids = set(body.activityIds), set(body.flowIds)
        labels = [a.get("name") for a in details.get("activities") or [] if a["id"] in activity_ids]
        labels += [f"Flow: {f.get('name')}" for f in details.get("activityFlows") or [] if f["id"] in flow_ids]
        participants = [Participant(p.subjectId, p.secretId, "", "", None) for p in body.participants]
        filters = ExportFilters(
            target_subject_ids=[p.subject_id for p in participants],
            from_utc=start,
            to_utc=end,
            activity_ids=activity_ids,
            flow_ids=flow_ids,
        )
        output = output_dir(body.outputDir)
        page_size = max(1, min(body.pageSize, 10000))
        client, server, email = current.client, current.server, current.email
        job = state.jobs.start(
            "answers",
            f"Answers — {applet_name}",
            lambda job: runners.run_answers(
                job,
                client,
                server=server,
                email=email,
                details=details,
                decryptor=decryptor,
                participants=participants,
                filters=filters,
                activity_labels=labels,
                media=body.media,
                journey=body.journey,
                output=output,
                page_size=page_size,
                save_decrypted=body.saveDecrypted,
                null_when_skipped=body.subscaleNullWhenSkipped,
            ),
        )
        return {"jobId": job.id}

    @app.post("/api/jobs/dictionary")
    def start_dictionary(body: DictionaryJobBody):
        current = session()
        all_rows = [row for row in current.client.workspace_applets(body.ownerId) if row.get("type") != "folder"]
        all_rows.sort(key=lambda row: (row.get("displayName") or "").lower())
        workspace = next((w for w in current.client.workspaces() if w["ownerId"] == body.ownerId), None)
        workspace_name = (workspace or {}).get("workspaceName") or body.ownerId
        if body.allApplets:
            rows, scope, whole = all_rows, workspace_name, workspace or {"ownerId": body.ownerId}
        else:
            by_id = {row["id"]: row for row in all_rows}
            missing = [applet_id for applet_id in body.appletIds if applet_id not in by_id]
            if missing:
                raise HTTPException(400, f"Not in this workspace: {', '.join(missing)}")
            rows = [by_id[applet_id] for applet_id in body.appletIds]
            scope = (rows[0].get("displayName") or rows[0]["id"]) if len(rows) == 1 else workspace_name
            whole = None
        if not rows:
            raise HTTPException(400, "Select at least one applet.")
        output = output_dir(body.outputDir)
        client, server, email = current.client, current.server, current.email
        details = ([f"{len(rows)} applets"] if len(rows) > 1 else []) + (
            ["current version"] if body.currentOnly else []
        )
        title = f"Data dictionary — {scope}" + (f" ({', '.join(details)})" if details else "")
        job = state.jobs.start(
            "dictionary",
            title,
            lambda job: runners.run_dictionary(
                job,
                client,
                server=server,
                email=email,
                rows=rows,
                scope=scope,
                workspace=whole,
                output=output,
                current_only=body.currentOnly,
            ),
        )
        return {"jobId": job.id}

    @app.post("/api/jobs/counts")
    def start_counts(body: CountsJobBody):
        current = session()
        workspace = next((w for w in current.client.workspaces() if w["ownerId"] == body.ownerId), None)
        if workspace is None:
            raise HTTPException(400, "No such workspace.")
        applets = [row for row in current.client.workspace_applets(body.ownerId) if row.get("type") != "folder"]
        applets.sort(key=lambda row: (row.get("displayName") or "").lower())
        if not applets:
            raise HTTPException(400, "This workspace has no applets.")
        start, end = parse_dates(body.fromDate, body.toDate)
        filters = ExportFilters(from_utc=start, to_utc=end)
        output = output_dir(body.outputDir)
        client, server, email = current.client, current.server, current.email
        workspace_name = workspace.get("workspaceName") or body.ownerId
        job = state.jobs.start(
            "counts",
            f"Participant counts — {workspace_name}",
            lambda job: runners.run_counts(
                job,
                client,
                server=server,
                email=email,
                workspace=workspace,
                applets=applets,
                filters=filters,
                from_text=body.fromDate,
                to_text=body.toDate,
                output=output,
            ),
        )
        return {"jobId": job.id}

    @app.get("/api/jobs")
    def jobs():
        return [job.to_dict() for job in state.jobs.all()]

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job.")
        return job.to_dict()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job.")
        job.cancel()
        return {"ok": True}

    # -- dashboard --------------------------------------------------------------------

    def study_or_404(study_id: str) -> studies.StudyConfig:
        try:
            config = studies.get_study(study_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if config is None:
            raise HTTPException(404, "No such study.")
        return config

    def study_payload(config: studies.StudyConfig) -> dict:
        current = session()
        data = state.dashboards.get(config.id)
        job_id = state.dashboard_jobs.get(config.id)
        job = state.jobs.get(job_id) if job_id else None
        payload = {
            **config.to_dict(),
            "sameServer": config.server.rstrip("/") == current.server.rstrip("/"),
            "loaded": data is not None,
            "loadingJobId": job.id if job and job.to_dict()["status"] == "running" else None,
            "lastJobId": job.id if job else None,
        }
        if data is not None:
            first, last = data.day_span()
            payload["data"] = {
                "loadedAt": data.loaded_at.replace(tzinfo=UTC).astimezone().isoformat(timespec="seconds"),
                "participants": len(data.participants),
                "completions": len(data.completions),
                "applets": [
                    {
                        **applet,
                        "scheduleAvailable": data.schedule_available.get(applet_id, False),
                        "historyAvailable": data.history_available.get(applet_id, False),
                    }
                    for applet_id, applet in data.applets.items()
                ],
                "entities": [
                    {"key": e.key, "label": e.label, "applet": e.applet_name, "kind": e.kind, "deleted": e.deleted}
                    for e in data.visible_entities()
                ],
                "warnings": data.warnings,
                "span": [first.isoformat() if first else None, last.isoformat() if last else None],
            }
        return payload

    @app.get("/api/dashboard/studies")
    def dashboard_studies():
        session()
        return [study_payload(config) for config in studies.list_studies()]

    @app.post("/api/dashboard/studies")
    def dashboard_save(body: dict):
        current = session()
        try:
            config = studies.StudyConfig.from_dict(body)
            config.server = current.server
            previous = studies.get_study(config.id)
            config = studies.save_study(config)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        # The loaded data depends on the applets, the identity aliases, and the data range; anything
        # else (the dashboard type, its rules) is applied when a view is computed.
        if previous is None or (previous.applets, previous.aliases, previous.data_from) != (
            config.applets,
            config.aliases,
            config.data_from,
        ):
            state.dashboards.pop(config.id, None)
        for key in [key for key in state.dashboard_views if key[0] == config.id]:
            state.dashboard_views.pop(key, None)
        return study_payload(config)

    @app.delete("/api/dashboard/studies/{study_id}")
    def dashboard_delete(study_id: str):
        session()
        try:
            found = studies.delete_study(study_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if not found:
            raise HTTPException(404, "No such study.")
        state.dashboards.pop(study_id, None)
        for key in [key for key in state.dashboard_views if key[0] == study_id]:
            state.dashboard_views.pop(key, None)
        return {"ok": True}

    @app.get("/api/dashboard/studies/{study_id}")
    def dashboard_study(study_id: str):
        return study_payload(study_or_404(study_id))

    @app.post("/api/dashboard/studies/{study_id}/load")
    def dashboard_load(study_id: str):
        current = session()
        config = study_or_404(study_id)
        running = state.dashboard_jobs.get(study_id)
        if running and (job := state.jobs.get(running)) and job.to_dict()["status"] == "running":
            return {"jobId": job.id}
        client = current.client

        def run(job):
            data = load_study(client, config, progress=job.progress)
            state.dashboards[config.id] = data
            for key in [key for key in state.dashboard_views if key[0] == config.id]:
                state.dashboard_views.pop(key, None)
            for warning in data.warnings:
                job.warn(warning)
            return {
                "studyId": config.id,
                "participants": len(data.participants),
                "completions": len(data.completions),
                "applets": len(data.applets),
                "activities": len(data.visible_entities()),
            }

        job = state.jobs.start("dashboard", f"Dashboard data — {config.name}", run)
        state.dashboard_jobs[study_id] = job.id
        return {"jobId": job.id}

    def dashboard_result(study_id: str, query: dict) -> tuple[studies.StudyConfig, GridResult]:
        config = study_or_404(study_id)
        data = state.dashboards.get(study_id)
        if data is None:
            raise HTTPException(409, "Load the study's data first.")
        host_tz = host_tz_offset_minutes()
        today = today_local(host_tz)
        signature = json.dumps(
            {**query, "_loaded": data.loaded_at.isoformat(), "_today": today.isoformat()}, sort_keys=True
        )
        cached = state.dashboard_views.get((study_id, signature))
        if cached is not None:
            return config, cached
        try:
            params = view_params(
                config, query, data, today=today, now_utc=datetime.now(UTC).replace(tzinfo=None), host_tz=host_tz
            )
            result = build(data, config, params)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        mine = [key for key in state.dashboard_views if key[0] == study_id]
        for key in mine[:-5]:  # keep a few recent views for cell look-ups
            state.dashboard_views.pop(key, None)
        state.dashboard_views[(study_id, signature)] = result
        return config, result

    @app.get("/api/dashboard/studies/{study_id}/view")
    def dashboard_view(study_id: str, request: Request):
        _, result = dashboard_result(study_id, dict(request.query_params))
        return result.grid

    @app.get("/api/dashboard/studies/{study_id}/cell")
    def dashboard_cell(study_id: str, row: str, col: str, request: Request):
        query = {key: value for key, value in request.query_params.items() if key not in ("row", "col")}
        _, result = dashboard_result(study_id, query)
        return {"items": result.details.get((row, col), [])}

    @app.get("/api/dashboard/studies/{study_id}/export")
    def dashboard_export(study_id: str, request: Request):
        query = dict(request.query_params)
        layout = query.pop("format", "grid")
        config, result = dashboard_result(study_id, query)
        text = long_csv(result) if layout == "long" else grid_csv(result.grid)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        name = f"dashboard_{studies.slug(config.name)}_{result.grid['view']}_{layout}_{stamp}.csv"
        return Response(
            content="\ufeff" + text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # -- local conveniences ---------------------------------------------------------

    @app.get("/api/browse")
    def browse(path: str | None = None):
        """Subfolders of a folder, for the output-folder picker. Only used locally, token-protected."""
        base = Path(path).expanduser() if path else Path.home()
        try:
            base = base.resolve()
            if not base.is_dir():
                raise HTTPException(400, "Not a folder.")
            dirs = sorted(
                (entry.name for entry in base.iterdir() if entry.is_dir() and not entry.name.startswith(".")),
                key=str.lower,
            )
        except (OSError, PermissionError) as e:
            raise HTTPException(400, f"Cannot open that folder: {e}") from e
        return {"path": str(base), "parent": str(base.parent) if base.parent != base else None, "dirs": dirs}

    @app.post("/api/reveal")
    def reveal(body: RevealBody):
        """Open a finished job's folder in the file manager. Only paths a job produced are allowed."""
        session()
        allowed = False
        for job in state.jobs.all():
            result = job.to_dict().get("result") or {}
            folder = result.get("folder")
            for candidate in (folder, result.get("mainFile")):
                if candidate and body.path == candidate:
                    allowed = True
        if not allowed:
            raise HTTPException(400, "Not a folder this app created.")
        target = Path(body.path)
        if not target.exists():
            raise HTTPException(400, "That folder no longer exists.")
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(target)] if target.is_file() else ["open", str(target)], check=False)
        elif os.name == "nt":
            os.startfile(str(target if target.is_dir() else target.parent))
        else:
            subprocess.run(["xdg-open", str(target if target.is_dir() else target.parent)], check=False)
        return {"ok": True}

    # -- static UI -------------------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


# -- launcher -------------------------------------------------------------------------


def _free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit(f"No free port between {preferred} and {preferred + 49}.")


def serve(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1 (the next free one is used if taken).")] = 8456,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Don't open the browser automatically.")] = False,
) -> None:
    """Run the Curious app: a local web page for downloading answers, data dictionaries, and participant counts.

    Everything runs on this computer. Passwords are never written to disk, and the page only
    works with the tokened link printed below.
    """
    token = secrets.token_urlsafe(24)
    port = _free_port(port)
    url = f"http://127.0.0.1:{port}/?token={token}"
    app = create_app(token=token)
    print(f"\nCurious app {__version__} — started {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}")
    print(f"\n  Open:  {url}\n")
    print("Keep this window open while you use the app. Press Ctrl+C to stop it.\n", flush=True)
    if not no_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    typer.run(serve)
