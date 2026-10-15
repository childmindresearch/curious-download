"""Keep a study's loaded data between runs of the app.

One gzip-compressed JSON file per study next to the saved studies. It holds what the loader
fetched (participants, activities, schedules, and completion times), never any answer content
and never a password. A refresh then only asks the server for what changed.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict
from datetime import date, datetime, time
from pathlib import Path

from . import config as studies
from .model import Completion, Entity, Participant, StudyData
from .schedule import EventRule

CACHE_VERSION = 1


def cache_dir() -> Path:
    return studies.studies_dir() / "cache"


def cache_path(study_id: str) -> Path:
    return cache_dir() / f"{study_id}.json.gz"


def _iso(value: datetime | date | time | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _d(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


def _t(text: str) -> time:
    return time.fromisoformat(text)


def encode(data: StudyData) -> dict:
    return {
        "studyId": data.study_id,
        "server": data.server,
        "loadedAt": _iso(data.loaded_at),
        "applets": data.applets,
        "entities": [asdict(entity) | {"activity_ids": list(entity.activity_ids)} for entity in data.entities.values()],
        "participants": [
            asdict(p)
            | {"joined": {a: _iso(day) for a, day in p.joined.items()}, "last_completion": _iso(p.last_completion)}
            for p in data.participants.values()
        ],
        "completions": [
            asdict(c)
            | {
                "scheduled_local": _iso(c.scheduled_local),
                "start_local": _iso(c.start_local),
                "end_local": _iso(c.end_local),
                "created_utc": _iso(c.created_utc),
            }
            for c in data.completions
        ],
        "rules": {
            applet_id: [
                asdict(rule)
                | {
                    "start_date": _iso(rule.start_date),
                    "end_date": _iso(rule.end_date),
                    "selected_date": _iso(rule.selected_date),
                    "start_time": _iso(rule.start_time),
                    "end_time": _iso(rule.end_time),
                    "active_from": _iso(rule.active_from),
                    "active_to": _iso(rule.active_to),
                }
                for rule in rules
            ]
            for applet_id, rules in data.rules.items()
        },
        "scheduleAvailable": data.schedule_available,
        "historyAvailable": data.history_available,
        "warnings": data.warnings,
        "individualEvents": data.individual_events,
    }


def decode(payload: dict) -> StudyData:
    data = StudyData(study_id=payload["studyId"], server=payload["server"], loaded_at=_dt(payload["loadedAt"]))  # type: ignore[arg-type]
    data.applets = payload.get("applets") or {}
    for entry in payload.get("entities") or []:
        data.entities[entry["key"]] = Entity(**(entry | {"activity_ids": tuple(entry.get("activity_ids") or ())}))
    for entry in payload.get("participants") or []:
        participant = Participant(
            **(
                entry
                | {
                    "joined": {a: _d(day) for a, day in (entry.get("joined") or {}).items()},
                    "last_completion": _dt(entry.get("last_completion")),
                }
            )
        )
        data.participants[participant.key] = participant
    for entry in payload.get("completions") or []:
        data.completions.append(
            Completion(
                **(
                    entry
                    | {
                        "scheduled_local": _dt(entry.get("scheduled_local")),
                        "start_local": _dt(entry["start_local"]),
                        "end_local": _dt(entry["end_local"]),
                        "created_utc": _dt(entry["created_utc"]),
                    }
                )
            )
        )
    for applet_id, rules in (payload.get("rules") or {}).items():
        data.rules[applet_id] = [
            EventRule(
                **(
                    entry
                    | {
                        "start_date": _d(entry.get("start_date")),
                        "end_date": _d(entry.get("end_date")),
                        "selected_date": _d(entry.get("selected_date")),
                        "start_time": _t(entry["start_time"]),
                        "end_time": _t(entry["end_time"]),
                        "active_from": _d(entry.get("active_from")),
                        "active_to": _d(entry.get("active_to")),
                    }
                )
            )
            for entry in rules
        ]
    data.schedule_available = payload.get("scheduleAvailable") or {}
    data.history_available = payload.get("historyAvailable") or {}
    data.warnings = payload.get("warnings") or []
    data.individual_events = payload.get("individualEvents") or {}
    return data


def save_cache(data: StudyData, *, email: str) -> Path:
    """Write the study's data for the next run. The file is tied to the server and the account."""
    path = cache_path(data.study_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "version": CACHE_VERSION,
        "server": data.server.rstrip("/"),
        "email": email.strip().lower(),
        "savedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": encode(data),
    }
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(envelope, fh)
    return path


def load_cache(study_id: str, *, server: str, email: str) -> StudyData | None:
    """The study's data from the last run, if it was saved for this server and account."""
    path = cache_path(study_id)
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            envelope = json.load(fh)
        if envelope.get("version") != CACHE_VERSION:
            return None
        if envelope.get("server") != server.rstrip("/") or envelope.get("email") != email.strip().lower():
            return None
        return decode(envelope["data"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def delete_cache(study_id: str) -> None:
    try:
        cache_path(study_id).unlink()
    except OSError:
        pass
