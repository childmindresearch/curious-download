"""Background jobs with progress, run in threads and polled by the browser."""

from __future__ import annotations

import threading
import traceback
import uuid
from collections.abc import Callable
from datetime import datetime

import httpx

from ..client import ApiError


class JobCancelled(Exception):
    """Raised inside a runner (via Job.progress) after the user cancelled the job."""


class Job:
    """One background run. Runners report through `progress` and `warn`; the server reads `to_dict`."""

    def __init__(self, kind: str, title: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind  # "answers" | "dictionary" | "counts"
        self.title = title
        self.created = datetime.now().astimezone().isoformat(timespec="seconds")
        self.status = "running"  # running | done | failed | cancelled
        self.stage = "Starting"
        self.detail = ""  # a finer-grained line under the stage, e.g. the applet being counted
        self.done = 0
        self.total: int | None = None
        self.warnings: list[str] = []
        self.result: dict | None = None
        self.error: str | None = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()

    def progress(
        self,
        stage: str | None = None,
        done: int | None = None,
        total: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Update what the browser shows. Raises JobCancelled once a cancel was requested."""
        if self._cancel.is_set():
            raise JobCancelled()
        with self._lock:
            if stage is not None and stage != self.stage:
                self.stage, self.done, self.total, self.detail = stage, 0, None, ""
            if done is not None:
                self.done = done
            if total is not None:
                self.total = total
            if detail is not None:
                self.detail = detail

    def warn(self, message: str) -> None:
        with self._lock:
            self.warnings.append(message)

    def cancel(self) -> None:
        self._cancel.set()

    def _end(self, status: str, *, stage: str | None = None, result: dict | None = None, error: str | None = None):
        with self._lock:
            self.status = status
            if stage is not None:
                self.stage = stage
            self.result = result
            self.error = error

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "title": self.title,
                "created": self.created,
                "status": self.status,
                "stage": self.stage,
                "detail": self.detail,
                "cancelRequested": self._cancel.is_set(),
                "done": self.done,
                "total": self.total,
                "warnings": list(self.warnings),
                "result": self.result,
                "error": self.error,
            }


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, title: str, runner: Callable[[Job], dict]) -> Job:
        job = Job(kind, title)
        with self._lock:
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, runner), daemon=True).start()
        return job

    @staticmethod
    def _run(job: Job, runner: Callable[[Job], dict]) -> None:
        try:
            result = runner(job)
        except JobCancelled:
            job._end("cancelled", stage="Cancelled")
        except ApiError as e:
            job._end("failed", error=f"The Curious server refused the request: {e}")
        except httpx.HTTPError as e:
            job._end("failed", error=f"Could not reach the Curious server: {e}")
        except Exception as e:  # noqa: BLE001 -- keep the app alive; the browser shows the message
            traceback.print_exc()
            job._end("failed", error=f"{type(e).__name__}: {e}")
        else:
            job._end("done", stage="Done", result=result)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda job: job.created, reverse=True)

    def any_running(self) -> bool:
        return any(job.to_dict()["status"] == "running" for job in self.all())
