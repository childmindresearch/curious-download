"""Saved dashboard studies: which applets, which kind of dashboard, and its rules.

Stored as one JSON file per study next to the app's settings, never with any password.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path

from .. import cli

VIEWS = ("fixed", "rolling", "cross", "individual")
START_RULES = ("first_completion", "individual_schedule", "joined", "manual")
GRANULARITIES = ("day", "week")

_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass
class StudyConfig:
    id: str
    name: str
    server: str
    view: str = "fixed"
    applets: list[dict] = field(default_factory=list)  # [{ownerId, appletId, name}]
    entities: list[str] | None = None  # entity keys to show; None means all visible activities and flows
    participants: list[str] | None = None  # participant keys (secret IDs) to show; None means everyone
    only_with_data: bool = False  # leave out participants who have not completed anything
    use_history: bool = True  # read the schedule history (slow on very large studies)
    hide_empty_columns: bool = True  # drop columns where nothing was scheduled or done for anyone
    start_rule: str = "first_completion"  # rolling view: how each participant's day 1 is found
    start_entity: str | None = None  # rolling view: with first_completion, only this activity or flow counts
    study_days: int = 28  # rolling view: how many study days to show
    manual_starts: dict[str, str] = field(default_factory=dict)  # secret id -> YYYY-MM-DD
    aliases: dict[str, str] = field(default_factory=dict)  # secret id -> the secret id it should count as
    late_counts_as_done: bool = True
    count_from_joined: bool = True  # ignore occurrences before the participant was added to the applet
    include_removed: bool = False  # show participants removed from every applet (their data is still there)
    data_from: str | None = None  # YYYY-MM-DD: only fetch completions received from this day on
    granularity: str = "day"
    created: str = ""
    updated: str = ""

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("Give the study a name.")
        if self.view not in VIEWS:
            raise ValueError(f"Unknown dashboard type {self.view!r}.")
        if not self.applets:
            raise ValueError("Choose at least one applet.")
        for applet in self.applets:
            if not applet.get("ownerId") or not applet.get("appletId"):
                raise ValueError("Each applet needs an ownerId and an appletId.")
        if self.start_rule not in START_RULES:
            raise ValueError(f"Unknown start rule {self.start_rule!r}.")
        if self.granularity not in GRANULARITIES:
            raise ValueError(f"Unknown granularity {self.granularity!r}.")
        if not 1 <= int(self.study_days) <= 730:
            raise ValueError("Study days must be between 1 and 730.")
        for secret_id, day in self.manual_starts.items():
            try:
                date.fromisoformat(day)
            except ValueError as e:
                raise ValueError(f"Start date for {secret_id!r} is not YYYY-MM-DD: {day!r}") from e
        if self.data_from:
            try:
                date.fromisoformat(self.data_from)
            except ValueError as e:
                raise ValueError(f"'Data from' is not YYYY-MM-DD: {self.data_from!r}") from e

    @property
    def applet_ids(self) -> list[str]:
        return [applet["appletId"] for applet in self.applets]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> StudyConfig:
        known = {f.name for f in fields(cls)}
        values = {key: value for key, value in payload.items() if key in known}
        values.setdefault("id", new_id())
        values.setdefault("name", "")
        values.setdefault("server", "")
        config = cls(**values)
        config.study_days = int(config.study_days)
        config.manual_starts = {
            str(k).strip(): str(v).strip() for k, v in (config.manual_starts or {}).items() if str(k).strip()
        }
        config.aliases = {str(k).strip(): str(v).strip() for k, v in (config.aliases or {}).items() if str(k).strip()}
        if config.participants is not None:
            config.participants = [str(key).strip() for key in config.participants if str(key).strip()]
        if config.entities is not None:
            config.entities = [str(key).strip() for key in config.entities if str(key).strip()]
        return config


def new_id() -> str:
    return secrets.token_hex(6)


def studies_dir() -> Path:
    # Read at call time so tests that redirect the settings file redirect the studies too.
    return cli.SETTINGS_PATH.parent / "dashboards"


def _path(study_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", study_id):
        raise ValueError("Not a valid study id.")
    return studies_dir() / f"{study_id}.json"


def list_studies() -> list[StudyConfig]:
    folder = studies_dir()
    if not folder.is_dir():
        return []
    studies = []
    for path in folder.glob("*.json"):
        try:
            studies.append(StudyConfig.from_dict(json.loads(path.read_text())))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(studies, key=lambda s: (s.name.lower(), s.created))


def get_study(study_id: str) -> StudyConfig | None:
    try:
        return StudyConfig.from_dict(json.loads(_path(study_id).read_text()))
    except (OSError, ValueError, TypeError):
        return None


def save_study(config: StudyConfig) -> StudyConfig:
    config.validate()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    config.created = config.created or now
    config.updated = now
    path = _path(config.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2))
    return config


def delete_study(study_id: str) -> bool:
    path = _path(study_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def slug(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-") or "study"
