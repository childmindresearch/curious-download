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

# Page sizes for the bulk reads. Each export row carries its encrypted answer, so very large pages
# have made the server fail on big applets; when a page is refused with a server error the loader
# halves the page size and tries again, down to MIN_PAGE_SIZE, and then skips the rows it cannot get.
EXPORT_PAGE_SIZE = 1000
HISTORY_PAGE_SIZE = 2000
RESPONDENTS_PAGE_SIZE = 1000
SUBMISSIONS_PAGE_SIZE = 10000
MIN_PAGE_SIZE = 10
MAX_SKIPPED_PAGES = 20
SERVER_ERRORS = {500, 502, 503, 504}
# Ids per filtered request (they travel in the URL).
ID_CHUNK = 40
# Below this share of participants a chosen subset is fetched with per-participant filters;
# above it, reading everything is fewer requests.
FILTER_BELOW = 0.8
# A respondent id that matches nobody: with the history endpoint it yields the default events alone.
NOBODY = "00000000-0000-0000-0000-000000000000"

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


def _with_context(error: ApiError, doing: str) -> ApiError:
    """The same error, saying which request failed (the job shows only the message)."""
    return ApiError(error.status_code, f"{error.message} — while {doing}")


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
        # Page sizes the server has accepted so far, per kind of request; a refused page halves it.
        self._page_sizes: dict[str, int] = {}

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
        many = len(applets) > 1
        for number, applet in enumerate(applets, start=1):
            name = applet.get("name") or applet["appletId"]
            prefix = f"Applet {number} of {len(applets)}: " if many else ""
            self.progress(stage=f"{prefix}reading {name}", detail="activities and participants")
            details = self._load_applet(applet["ownerId"], applet["appletId"])
            self._load_participants(applet["ownerId"], applet["appletId"], details.get("displayName") or name)
        selected = self._select_participants()
        for number, applet in enumerate(applets, start=1):
            applet_id = applet["appletId"]
            applet_name = self.data.applets[applet_id]["name"]
            prefix = f"Applet {number} of {len(applets)}: " if many else ""
            self.progress(stage=f"{prefix}reading the schedule of {applet_name}", detail="")
            self._load_schedule(applet_id, applet_name, selected)
            self.progress(stage=f"{prefix}reading completions of {applet_name}", done=0, total=None)
            self._load_completions(applet_id, applet_name, selected)
        self._drop_unselected(selected)
        return self.data

    # -- which participants ---------------------------------------------------------------

    def _select_participants(self) -> set[str] | None:
        """Keys of the participants to load, or None for everyone.

        A study can name its participants, and/or ask for only those who have submitted data; the
        latter is found from the cheap submission metadata before any answer is read.
        """
        selected: set[str] | None = set(self.config.participants) if self.config.participants else None
        if self.config.only_with_data:
            with_data: set[str] = set()
            known = True
            for applet_id, applet in self.data.applets.items():
                counts = self._submission_counts(applet_id, applet["name"])
                if counts is None:
                    known = False
                    break
                for subject_id in counts:
                    key = self._subject_to_participant.get(subject_id)
                    if key:
                        with_data.add(key)
            if known:
                selected = with_data if selected is None else selected & with_data
        return selected

    def _subjects_for(self, applet_id: str, keys: set[str] | None) -> set[str] | None:
        if keys is None:
            return None
        return {
            p.subject_ids[applet_id]
            for p in self.data.participants.values()
            if p.key in keys and applet_id in p.subject_ids
        }

    def _accounts_for(self, applet_id: str, keys: set[str] | None) -> list[str]:
        return sorted(
            {
                p.user_ids[applet_id]
                for p in self.data.participants.values()
                if (keys is None or p.key in keys) and p.user_ids.get(applet_id)
            }
        )

    def _submission_counts(self, applet_id: str, applet_name: str) -> dict[str, int] | None:
        """Completed activities per target subject from the submission metadata; None if unavailable."""
        reviewer = {e.id for e in self.data.entities.values() if e.applet_id == applet_id and e.reviewable}
        from_date = _api_day(self.config.data_from)
        counts: dict[str, int] = {}

        def fetch(page: int, limit: int) -> dict:
            body = self.client.submissions_page(applet_id, page=page, limit=limit, from_date=from_date)
            return {"result": body.get("submissions") or [], "count": body.get("submissionsCount") or 0}

        try:
            for chunk, fetched, total in self._paged(
                f"scanning the submissions of {applet_name}", fetch, kind="submissions", page_size=SUBMISSIONS_PAGE_SIZE
            ):
                for row in chunk:
                    if row.get("activityId") in reviewer or not row.get("targetSubjectId"):
                        continue
                    subject_id = str(row["targetSubjectId"])
                    counts[subject_id] = counts.get(subject_id, 0) + 1
                self.progress(detail=f"submissions: {fetched:,} of {total:,}")
        except ApiError as e:
            if e.status_code not in (403, 404):
                raise
            self.data.warnings.append(
                f"{applet_name}: who has data could not be checked before loading ({e.message}); everyone is loaded."
            )
            return None
        return counts

    def _drop_unselected(self, selected: set[str] | None) -> None:
        if selected is None:
            return
        for key in [key for key in self.data.participants if key not in selected]:
            participant = self.data.participants.pop(key)
            for subject_id in participant.subject_ids.values():
                self._subject_to_participant.pop(subject_id, None)

    def _chunks(self, ids: list[str]) -> list[list[str]]:
        return [ids[i : i + ID_CHUNK] for i in range(0, len(ids), ID_CHUNK)]

    def _load_applet(self, owner_id: str, applet_id: str) -> dict:
        try:
            details = self.client.applet(applet_id)
        except ApiError as e:
            raise _with_context(e, f"reading applet {applet_id}") from e
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

    def _paged(self, doing: str, fetch, *, kind: str, page_size: int, result_key: str = "result"):
        """Read every page of a list endpoint, halving the page size when the server refuses one.

        `fetch(page, limit)` returns the response body. Rows come back in order; a page refused with
        a server error is retried with a smaller page (pages stay aligned because each new size
        divides the old one), and the size that worked is kept for the rest of the load. Rows the
        server cannot return even in the smallest page are skipped with a warning, so one bad row
        does not stop the whole load.
        """
        offset = 0
        total: int | None = None
        skipped_in_a_row = 0
        limit = self._page_sizes.setdefault(kind, page_size)
        while True:
            page = offset // limit + 1
            try:
                body = fetch(page, limit)
            except ApiError as e:
                if e.status_code not in SERVER_ERRORS:
                    raise _with_context(e, f"{doing} (page {page}, {limit} per page)") from e
                if limit > MIN_PAGE_SIZE:
                    limit = self._page_sizes[kind] = max(MIN_PAGE_SIZE, limit // 2)
                    self.progress(detail=f"{doing}: the server refused a page; retrying with {limit} per page")
                    continue
                skipped_in_a_row += 1
                self.data.warnings.append(
                    f"{doing}: rows {offset + 1:,}–{offset + limit:,} could not be read "
                    f"(the server answered HTTP {e.status_code}) and were skipped."
                )
                self.progress(detail=f"{doing}: skipping rows {offset + 1:,}–{offset + limit:,}")
                offset += limit
                if total is not None and offset >= total:
                    return
                if skipped_in_a_row >= MAX_SKIPPED_PAGES:
                    self.data.warnings.append(f"{doing}: gave up after {MAX_SKIPPED_PAGES} unreadable pages in a row.")
                    return
                continue
            skipped_in_a_row = 0
            chunk = body.get(result_key) or []
            total = int(body.get("count") or 0)
            offset += len(chunk)
            yield chunk, offset, total
            if not chunk or offset >= total:
                return

    def _load_participants(self, owner_id: str, applet_id: str, applet_name: str) -> None:
        rows: list[dict] = []
        try:
            for chunk, fetched, total in self._paged(
                f"reading the participants of {applet_name}",
                lambda page, limit: self.client.applet_respondents_page(owner_id, applet_id, page=page, limit=limit),
                kind="respondents",
                page_size=RESPONDENTS_PAGE_SIZE,
            ):
                rows.extend(chunk)
                self.progress(detail=f"participants: {fetched:,} of {total:,}")
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

    def _load_schedule(self, applet_id: str, applet_name: str, selected: set[str] | None) -> None:
        rules = []
        try:
            for event in self.client.applet_events(applet_id):
                rule = rule_from_event(applet_id, event)
                if rule:
                    rules.append(rule)
            self.data.schedule_available[applet_id] = True
        except ApiError as e:
            if e.status_code not in (403, 404):
                raise _with_context(e, f"reading the default schedule of {applet_name}") from e
            self.data.schedule_available[applet_id] = False
            self.data.warnings.append(
                f"{applet_name}: the schedule could not be read ({e.message}); every activity is treated as "
                "always available. Reading schedules needs an owner, manager, or coordinator role."
            )
            return

        with_individual = [
            (p, p.user_ids.get(applet_id))
            for p in self.data.participants.values()
            if p.individual.get(applet_id) and p.user_ids.get(applet_id) and (selected is None or p.key in selected)
        ]
        for number, (participant, user_id) in enumerate(with_individual, start=1):
            self.progress(detail=f"individual schedule {number} of {len(with_individual)}: {participant.secret_id}")
            try:
                events = self.client.applet_events(applet_id, respondent_id=user_id)
            except ApiError as e:
                if e.status_code not in (400, 403, 404, *SERVER_ERRORS):
                    raise _with_context(e, f"reading the individual schedule of {participant.secret_id}") from e
                self.data.warnings.append(
                    f"{applet_name}: individual schedule of {participant.secret_id} not readable ({e.message}); "
                    "the default schedule is assumed for them."
                )
                continue
            for event in events:
                rule = rule_from_event(applet_id, event)
                if rule:
                    rules.append(rule)

        if not self.config.use_history:
            self.data.history_available[applet_id] = False
            self.data.rules[applet_id] = rules
            return
        history = self._load_history(applet_id, applet_name, selected)
        self.data.rules[applet_id] = bound_current_rules(rules, history) if history is not None else rules

    def _load_history(self, applet_id: str, applet_name: str, selected: set[str] | None) -> list | None:
        """Every event version, or with a selection only the chosen people's (plus the default events)."""
        rows: list[dict] = []
        seen: set[tuple[str, str]] = set()
        if selected is None:
            batches: list[list[str]] = [[]]
        else:
            accounts = self._accounts_for(applet_id, selected)
            batches = self._chunks(accounts) or [[NOBODY]]
        try:
            for number, batch in enumerate(batches, start=1):
                for chunk, fetched, total in self._paged(
                    f"reading the schedule history of {applet_name}",
                    lambda page, limit, batch=batch: self.client.applet_events_history_page(
                        applet_id, page=page, limit=limit, respondent_ids=batch
                    ),
                    kind="history",
                    page_size=HISTORY_PAGE_SIZE,
                ):
                    for row in chunk:
                        key = (str(row.get("eventId")), str(row.get("eventVersion")))
                        if key not in seen:
                            seen.add(key)
                            rows.append(row)
                    where = f"batch {number} of {len(batches)}, " if len(batches) > 1 else ""
                    self.progress(detail=f"schedule history: {where}{fetched:,} of {total:,} event versions")
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

    def _load_completions(self, applet_id: str, applet_name: str, selected: set[str] | None) -> None:
        seen: set[str] = set()
        from_date = _api_day(self.config.data_from)
        subjects = self._subjects_for(applet_id, selected)
        in_applet = sum(1 for p in self.data.participants.values() if applet_id in p.subject_ids)
        if subjects is not None and in_applet and len(subjects) < FILTER_BELOW * in_applet:
            batches = self._chunks(sorted(subjects))
        else:
            batches = [[]]  # everyone, unfiltered
        if not batches:
            return

        def make_fetch(batch: list[str]):
            def fetch(page: int, limit: int) -> dict:
                body = self.client.export_page(
                    applet_id, page=page, limit=limit, target_subject_ids=batch, from_date=from_date
                )
                # The page's activity definitions ride along; keep them for activities deleted since.
                for definition in (body.get("result") or {}).get("activities") or []:
                    self._register_deleted_activity(applet_id, applet_name, definition)
                return {"result": (body.get("result") or {}).get("answers") or [], "count": body.get("count") or 0}

            return fetch

        for number, batch in enumerate(batches, start=1):
            for chunk, fetched, total in self._paged(
                f"reading the completions of {applet_name}",
                make_fetch(batch),
                kind="completions",
                page_size=EXPORT_PAGE_SIZE,
            ):
                for answer in chunk:
                    if answer.get("id") in seen:
                        continue
                    seen.add(answer.get("id"))
                    completion = self._completion(applet_id, answer)
                    if completion:
                        self.data.completions.append(completion)
                if len(batches) > 1:
                    self.progress(
                        done=number,
                        total=len(batches),
                        detail=f"participants {(number - 1) * ID_CHUNK + 1}–{min(number * ID_CHUNK, len(subjects or []))} of {len(subjects or [])}: {fetched:,} completions",
                    )
                else:
                    self.progress(done=min(fetched, total) if total else fetched, total=total or None)

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


def scan_participants(
    client: CuriousClient, applets: list[dict], *, aliases: dict[str, str] | None = None, data_from: str | None = None
) -> dict:
    """Who is in these applets and how much each has submitted, without reading any answer.

    Used by the study editor so participants can be chosen before the first load.
    """
    config = StudyConfig(id="scan", name="scan", server="", applets=applets, aliases=aliases or {}, data_from=data_from)
    scanner = StudyLoader(client, config)
    submissions_known = True
    counts_by_key: dict[str, int] = {}
    for applet in applets:
        details = scanner._load_applet(applet["ownerId"], applet["appletId"])
        name = details.get("displayName") or applet.get("name") or applet["appletId"]
        scanner._load_participants(applet["ownerId"], applet["appletId"], name)
        counts = scanner._submission_counts(applet["appletId"], name)
        if counts is None:
            submissions_known = False
            continue
        for subject_id, count in counts.items():
            key = scanner._subject_to_participant.get(subject_id)
            if key:
                counts_by_key[key] = counts_by_key.get(key, 0) + count
    data = scanner.data
    return {
        "participants": [
            {
                "key": p.key,
                "label": p.label,
                "nickname": p.nickname,
                "tag": p.tag,
                "applets": [data.applets[a]["name"] for a in sorted(p.applet_ids) if a in data.applets],
                "submissions": counts_by_key.get(p.key, 0) if submissions_known else None,
                "hasData": (counts_by_key.get(p.key, 0) > 0) if submissions_known else None,
                "removed": p.is_removed_everywhere(),
            }
            for p in sorted(data.participants.values(), key=lambda p: p.secret_id.lower())
        ],
        "submissionsKnown": submissions_known,
        "warnings": data.warnings,
    }
