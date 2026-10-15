"""Fetch everything a dashboard needs from the Curious API: participants, activities, schedules,
and completion metadata. Answers are never decrypted; the encrypted content is dropped as it arrives."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..client import ApiError, CuriousClient
from .config import StudyConfig
from .model import Completion, Entity, Participant, StudyData
from .schedule import bound_current_rules, entity_key, rule_from_event, rules_from_history

EXPORT_PAGE_SIZE = 10000  # the backend's maximum; rows are small once the ciphertext is dropped
HISTORY_PAGE_SIZE = 10000

# Called like Job.progress: progress(stage=None, done=None, total=None, detail=None).
Progress = Callable[..., None]


def _noop(**_: Any) -> None:
    pass


def _utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC).replace(tzinfo=None)
    text = str(value).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment


def _local(value: Any, tz_offset: int) -> datetime | None:
    moment = _utc(value)
    return moment + timedelta(minutes=tz_offset) if moment else None


def _api_day(text: str | None) -> str | None:
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d").astimezone().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


class StudyLoader:
    def __init__(self, client: CuriousClient, config: StudyConfig, progress: Progress | None = None):
        self.client = client
        self.config = config
        self.progress = progress or _noop
        self.data = StudyData(
            study_id=config.id, server=config.server, loaded_at=datetime.now(UTC).replace(tzinfo=None)
        )
        self._subject_to_participant: dict[str, str] = {}

    # -- identity -------------------------------------------------------------------

    def _key(self, secret_id: str) -> str:
        secret_id = (secret_id or "").strip()
        return self.config.aliases.get(secret_id, secret_id)

    def _participant(self, secret_id: str, *, nickname: str = "", tag: str = "") -> Participant:
        key = self._key(secret_id) or "(no secret ID)"
        participant = self.data.participants.get(key)
        if participant is None:
            participant = Participant(key=key, secret_id=key, nickname=nickname, tag=tag)
            self.data.participants[key] = participant
        else:
            participant.nickname = participant.nickname or nickname
            participant.tag = participant.tag or tag
        return participant

    # -- steps --------------------------------------------------------------------------

    def load(self) -> StudyData:
        applets = self.config.applets
        for number, applet in enumerate(applets, start=1):
            name = applet.get("name") or applet["appletId"]
            prefix = f"Applet {number} of {len(applets)}: " if len(applets) > 1 else ""
            self.progress(stage=f"{prefix}reading {name}", detail="activities and participants")
            details = self._load_applet(applet["ownerId"], applet["appletId"])
            applet_name = details.get("displayName") or name
            self._load_participants(applet["ownerId"], applet["appletId"], applet_name)
            self.progress(stage=f"{prefix}reading the schedule of {applet_name}", detail="")
            self._load_schedule(applet["appletId"], applet_name)
            self.progress(stage=f"{prefix}reading completions of {applet_name}", done=0, total=None)
            self._load_completions(applet["appletId"], applet_name)
        return self.data

    def _load_applet(self, owner_id: str, applet_id: str) -> dict:
        details = self.client.applet(applet_id)
        applet_name = details.get("displayName") or applet_id
        self.data.applets[applet_id] = {
            "id": applet_id,
            "name": applet_name,
            "ownerId": owner_id,
            "version": details.get("version"),
        }
        for order, activity in enumerate(details.get("activities") or []):
            key = entity_key(applet_id, "activity", activity["id"])
            self.data.entities[key] = Entity(
                key=key,
                applet_id=applet_id,
                kind="activity",
                id=activity["id"],
                name=activity.get("name") or activity["id"],
                applet_name=applet_name,
                hidden=bool(activity.get("isHidden")),
                reviewable=bool(activity.get("isReviewable")),
                order=activity.get("order") if isinstance(activity.get("order"), int) else order,
            )
        for order, flow in enumerate(details.get("activityFlows") or []):
            key = entity_key(applet_id, "flow", flow["id"])
            activity_ids = flow.get("activityIds")
            if not activity_ids and flow.get("items"):
                activity_ids = [item.get("activityId") for item in flow["items"] if item.get("activityId")]
            self.data.entities[key] = Entity(
                key=key,
                applet_id=applet_id,
                kind="flow",
                id=flow["id"],
                name=flow.get("name") or flow["id"],
                applet_name=applet_name,
                hidden=bool(flow.get("isHidden")),
                activity_ids=tuple(str(a) for a in activity_ids or ()),
                order=flow.get("order") if isinstance(flow.get("order"), int) else order,
            )
        return details

    def _load_participants(self, owner_id: str, applet_id: str, applet_name: str) -> None:
        try:
            rows = self.client.applet_respondents(owner_id, applet_id)
        except ApiError as e:
            if e.status_code != 403:
                raise
            self.data.warnings.append(f"{applet_name}: no access to the participant list ({e.message}).")
            return
        for row in rows:
            if row.get("isAnonymousRespondent"):
                continue
            for detail in row.get("details") or []:
                if detail.get("appletId") not in (None, applet_id) or not detail.get("subjectId"):
                    continue
                secret_id = detail.get("respondentSecretId") or ""
                participant = self._participant(
                    secret_id, nickname=detail.get("respondentNickname") or "", tag=detail.get("subjectTag") or ""
                )
                subject_id = str(detail["subjectId"])
                participant.subject_ids[applet_id] = subject_id
                participant.user_ids[applet_id] = str(row["id"]) if row.get("id") else None
                created = _utc(detail.get("subjectCreatedAt"))
                participant.joined[applet_id] = created.date() if created else None
                participant.individual[applet_id] = bool(detail.get("hasIndividualSchedule"))
                participant.removed[applet_id] = bool(detail.get("subjectIsDeleted"))
                self._subject_to_participant[subject_id] = participant.key

    def _load_schedule(self, applet_id: str, applet_name: str) -> None:
        rules = []
        try:
            for event in self.client.applet_events(applet_id):
                rule = rule_from_event(applet_id, event)
                if rule:
                    rules.append(rule)
            self.data.schedule_available[applet_id] = True
        except ApiError as e:
            if e.status_code not in (403, 404):
                raise
            self.data.schedule_available[applet_id] = False
            self.data.warnings.append(
                f"{applet_name}: the schedule could not be read ({e.message}); every activity is treated as "
                "always available. Reading schedules needs an owner, manager, or coordinator role."
            )
            return

        with_individual = [
            (p, p.user_ids.get(applet_id))
            for p in self.data.participants.values()
            if p.individual.get(applet_id) and p.user_ids.get(applet_id)
        ]
        for number, (participant, user_id) in enumerate(with_individual, start=1):
            self.progress(detail=f"individual schedule {number} of {len(with_individual)}: {participant.secret_id}")
            try:
                events = self.client.applet_events(applet_id, respondent_id=user_id)
            except ApiError as e:
                if e.status_code not in (400, 403, 404):
                    raise
                self.data.warnings.append(
                    f"{applet_name}: individual schedule of {participant.secret_id} not readable ({e.message})."
                )
                continue
            for event in events:
                rule = rule_from_event(applet_id, event)
                if rule:
                    rules.append(rule)

        history = self._load_history(applet_id, applet_name)
        self.data.rules[applet_id] = bound_current_rules(rules, history) if history is not None else rules

    def _load_history(self, applet_id: str, applet_name: str) -> list | None:
        rows: list[dict] = []
        page = 1
        try:
            while True:
                self.progress(detail=f"schedule history, page {page}")
                body = self.client.applet_events_history_page(applet_id, page=page, limit=HISTORY_PAGE_SIZE)
                chunk = body.get("result") or []
                rows.extend(chunk)
                total = int(body.get("count") or 0)
                if not chunk or page * HISTORY_PAGE_SIZE >= total:
                    break
                page += 1
        except ApiError as e:
            if e.status_code not in (403, 404):
                raise
            self.data.history_available[applet_id] = False
            return None
        self.data.history_available[applet_id] = True
        for row in rows:
            entity_id = row.get("activityOrFlowId")
            if not entity_id:
                continue
            kind = "flow" if str(row.get("eventType") or "").lower() == "flow" else "activity"
            key = entity_key(applet_id, kind, str(entity_id))
            if key not in self.data.entities:
                self.data.entities[key] = Entity(
                    key=key,
                    applet_id=applet_id,
                    kind=kind,
                    id=str(entity_id),
                    name=row.get("activityOrFlowName") or str(entity_id),
                    applet_name=applet_name,
                    hidden=bool(row.get("activityOrFlowHidden")),
                    deleted=True,
                    order=10_000,
                )
        return rules_from_history(applet_id, rows)

    def _load_completions(self, applet_id: str, applet_name: str) -> None:
        page = 1
        fetched = 0
        seen: set[str] = set()
        from_date = _api_day(self.config.data_from)
        while True:
            body = self.client.export_page(applet_id, page=page, limit=EXPORT_PAGE_SIZE, from_date=from_date)
            result = body.get("result") or {}
            total = int(body.get("count") or 0)
            answers = result.get("answers") or []
            for definition in result.get("activities") or []:
                self._register_deleted_activity(applet_id, applet_name, definition)
            for answer in answers:
                if answer.get("id") in seen:
                    continue
                seen.add(answer.get("id"))
                completion = self._completion(applet_id, answer)
                if completion:
                    self.data.completions.append(completion)
            fetched += len(answers)
            self.progress(done=min(fetched, total) if total else fetched, total=total or None)
            if not answers or fetched >= total:
                break
            page += 1

    def _register_deleted_activity(self, applet_id: str, applet_name: str, definition: dict) -> None:
        activity_id = definition.get("id") or str(definition.get("idVersion") or "")[:36]
        if not activity_id:
            return
        key = entity_key(applet_id, "activity", str(activity_id))
        if key not in self.data.entities:
            self.data.entities[key] = Entity(
                key=key,
                applet_id=applet_id,
                kind="activity",
                id=str(activity_id),
                name=definition.get("name") or str(activity_id),
                applet_name=applet_name,
                reviewable=bool(definition.get("isReviewable")),
                deleted=True,
                order=10_000,
            )

    def _completion(self, applet_id: str, answer: dict) -> Completion | None:
        if answer.get("reviewedAnswerId") or answer.get("reviewedFlowSubmitId"):
            return None  # a reviewer's assessment, not participant data
        activity_id = answer.get("activityId") or str(answer.get("activityHistoryId") or "")[:36]
        if not activity_id:
            return None
        subject_id = str(answer.get("targetSubjectId") or "")
        key = self._subject_to_participant.get(subject_id)
        if key is None:
            secret_id = answer.get("targetSecretId") or answer.get("respondentSecretId") or subject_id or "(unknown)"
            participant = self._participant(
                secret_id, nickname=answer.get("targetUserNickname") or "", tag=answer.get("targetUserTag") or ""
            )
            if applet_id not in participant.subject_ids:
                # Known from answers only: removed from the applet with their data kept, or a reviewer-only view.
                participant.subject_ids[applet_id] = subject_id or participant.key
                participant.removed[applet_id] = True
            if subject_id:
                self._subject_to_participant[subject_id] = participant.key
            key = participant.key
        participant = self.data.participants[key]

        tz_offset = answer.get("tzOffset")
        tz_offset = int(tz_offset) if isinstance(tz_offset, (int, float)) else 0
        created = _utc(answer.get("createdAt")) or datetime.now(UTC).replace(tzinfo=None)
        end_local = _local(answer.get("endDatetime"), tz_offset) or created + timedelta(minutes=tz_offset)
        start_local = _local(answer.get("startDatetime"), tz_offset) or end_local
        flow_id = answer.get("flowId") or (str(answer["flowHistoryId"])[:36] if answer.get("flowHistoryId") else None)
        completion = Completion(
            answer_id=str(answer.get("id")),
            submit_id=str(answer.get("submitId") or answer.get("id")),
            participant=key,
            applet_id=applet_id,
            activity_id=str(activity_id),
            flow_id=str(flow_id) if flow_id else None,
            scheduled_event_id=str(answer["scheduledEventId"]) if answer.get("scheduledEventId") else None,
            scheduled_local=_local(answer.get("scheduledDatetime"), tz_offset),
            start_local=start_local,
            end_local=end_local,
            created_utc=created,
            tz_offset=tz_offset,
            version=str(answer.get("version") or ""),
        )
        if participant.last_completion is None or created > participant.last_completion:
            participant.last_completion = created
            participant.tz_offset = tz_offset
        return completion


def load_study(client: CuriousClient, config: StudyConfig, progress: Progress | None = None) -> StudyData:
    return StudyLoader(client, config, progress).load()


def host_tz_offset_minutes(now: datetime | None = None) -> int:
    """This computer's offset from UTC in minutes, used for participants who have no completion yet."""
    moment = (now or datetime.now(UTC)).astimezone()
    offset = moment.utcoffset() or timedelta()
    return int(offset.total_seconds() // 60)


def today_local(offset_minutes: int) -> date:
    return (datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=offset_minutes)).date()
