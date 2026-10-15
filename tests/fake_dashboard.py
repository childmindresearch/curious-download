"""A fake Curious backend with a scheduled study, for the dashboard tests.

Dates are relative to today so that "missed", "open now", and "upcoming" come out the same
whenever the tests run. Times are in this computer's local time zone, which is also what the
fake phones report as their offset.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import parse_qs

import httpx

from curious_download.dashboard.loader import host_tz_offset_minutes

EMAIL = "researcher@example.org"
PASSWORD = "account-pw"
OWNER_ID = "66666666-6666-4666-8666-666666666666"

EMA = "a1a1a1a1-0000-4000-8000-000000000001"
INTAKE = "a2a2a2a2-0000-4000-8000-000000000002"

MORNING, EVENING, SLEEP, MOOD, RATING, HIDDEN, BASELINE = (
    f"ac{n}00000-0000-4000-8000-00000000000{n}" for n in range(1, 8)
)
WEEKLY_FLOW = "f1f1f1f1-0000-4000-8000-000000000001"
CONSENT, DEMOGRAPHICS = "ac800000-0000-4000-8000-000000000008", "ac900000-0000-4000-8000-000000000009"

EV_MORNING, EV_EVENING, EV_FLOW, EV_RATING, EV_SLEEP_OLD, EV_MORNING_P2, EV_BASELINE = (
    f"e{n}e{n}e{n}e{n}-0000-4000-8000-00000000000{n}" for n in range(1, 8)
)

U1, U2, U5, MANAGER = (f"u{n}u{n}u{n}u{n}-0000-4000-8000-00000000000{n}" for n in (1, 2, 5, 9))
S1, S2, S3, S4, S9, S1B, S2B = (f"s{n}s{n}s{n}s{n}-0000-4000-8000-00000000000{n}" for n in (1, 2, 3, 4, 9, 6, 7))

TODAY = datetime.now().astimezone().date()
TZ = host_tz_offset_minutes()


def day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


def first_weekday_from(start: date) -> date:
    while start.weekday() >= 5:
        start += timedelta(days=1)
    return start


def _utc_seconds(local: datetime) -> float:
    return (local - timedelta(minutes=TZ)).replace(tzinfo=UTC).timestamp()


def _utc_iso(local: datetime) -> str:
    return (local - timedelta(minutes=TZ)).strftime("%Y-%m-%dT%H:%M:%S")


def _event(
    event_id: str,
    *,
    activity=None,
    flow=None,
    periodicity="DAILY",
    start=None,
    end=None,
    selected=None,
    start_time="08:00:00",
    end_time="12:00:00",
    respondent=None,
    one_time=False,
    access_before=False,
    version="20260901-1",
):
    return {
        "id": event_id,
        "startTime": start_time if periodicity != "ALWAYS" else None,
        "endTime": end_time if periodicity != "ALWAYS" else None,
        "accessBeforeSchedule": access_before,
        "oneTimeCompletion": one_time,
        "timer": 0,
        "timerType": "NOT_SET",
        "periodicity": {
            "type": periodicity,
            "startDate": start.isoformat() if start else None,
            "endDate": end.isoformat() if end else None,
            "selectedDate": selected.isoformat() if selected else None,
        },
        "respondentId": respondent,
        "activityId": activity,
        "flowId": flow,
        "notification": None,
        "version": version,
    }  # fmt: skip


def _history_row(
    event: dict,
    *,
    created: datetime,
    deleted_at: datetime | None = None,
    applet_version="1.0.0",
    name="",
    event_type="activity",
):
    periodicity = event["periodicity"]
    return {
        "appletId": EMA,
        "appletVersion": applet_version,
        "appletName": "EMA Study",
        "userId": event["respondentId"],
        "subjectId": None,
        "eventId": event["id"],
        "eventType": event_type,
        "eventVersion": event["version"],
        "eventVersionCreatedAt": _utc_iso(created),
        "eventVersionUpdatedAt": _utc_iso(deleted_at or created),
        "eventVersionIsDeleted": deleted_at is not None,
        "linkedWithAppletAt": _utc_iso(created),
        "eventUpdatedBy": MANAGER,
        "activityOrFlowId": event["flowId"] or event["activityId"],
        "activityOrFlowName": name,
        "activityOrFlowHidden": False,
        "accessBeforeSchedule": event["accessBeforeSchedule"],
        "oneTimeCompletion": event["oneTimeCompletion"],
        "periodicity": periodicity["type"],
        "startDate": periodicity["startDate"],
        "startTime": event["startTime"] or "00:00:00",
        "endDate": periodicity["endDate"],
        "endTime": event["endTime"] or "23:59:00",
        "selectedDate": periodicity["selectedDate"],
    }


class FakeStudyServer:
    def __init__(self, *, intake_events_forbidden: bool = True, max_page: int | None = None, fail_events: bool = False):
        self.requests: list[httpx.Request] = []
        self._tokens: set[str] = set()
        self._counter = 0
        self.intake_events_forbidden = intake_events_forbidden
        # Like a server that chokes on big pages: list endpoints answer 500 when limit exceeds this.
        self.max_page = max_page
        # Simulate the default-schedule endpoint failing.
        self.fail_events = fail_events

        start, end = day(-10), day(5)
        self.events = {
            EMA: [
                _event(EV_MORNING, activity=MORNING, start=start, end=end),
                _event(EV_EVENING, activity=EVENING, start=start, end=end, start_time="20:00:00", end_time="02:00:00"),
                _event(EV_FLOW, flow=WEEKLY_FLOW, periodicity="WEEKLY", start=start, end=end, selected=day(-7), start_time="00:00:00", end_time="23:59:00"),
                _event(EV_RATING, activity=RATING, periodicity="ALWAYS"),
                _event(EV_BASELINE, activity=BASELINE, periodicity="ALWAYS", one_time=True),
            ],
            INTAKE: [
                _event("e8e8e8e8-0000-4000-8000-000000000008", activity=CONSENT, periodicity="ALWAYS", one_time=True),
                _event("e9e9e9e9-0000-4000-8000-000000000009", activity=DEMOGRAPHICS, periodicity="ALWAYS", one_time=True),
            ],
        }  # fmt: skip
        self.individual_events = {
            (EMA, U2): [
                _event(EV_MORNING_P2, activity=MORNING, periodicity="WEEKDAYS", start=day(-6), end=end,
                       start_time="09:00:00", end_time="11:00:00", respondent=U2, version="20260905-1"),
            ],
        }  # fmt: skip
        # History: current versions plus a deleted daily "Sleep" event that was only in force for two days,
        # and a duplicate row (the same version linked with a second applet version).
        old_sleep = _event(
            EV_SLEEP_OLD,
            activity=SLEEP,
            start=start,
            end=end,
            start_time="21:00:00",
            end_time="23:00:00",
            version="20260901-1",
        )
        created = datetime.combine(start, time(9, 0))
        self.history = [
            _history_row(self.events[EMA][0], created=created, name="Morning"),
            _history_row(self.events[EMA][0], created=created, name="Morning", applet_version="1.0.1"),
            _history_row(self.events[EMA][1], created=created, name="Evening"),
            _history_row(self.events[EMA][2], created=created, name="Weekly flow", event_type="flow"),
            _history_row(self.events[EMA][3], created=created, name="Clinician rating"),
            _history_row(self.events[EMA][4], created=created, name="Baseline survey"),
            _history_row(
                self.individual_events[(EMA, U2)][0], created=datetime.combine(day(-6), time(9, 0)), name="Morning"
            ),
            _history_row(old_sleep, created=created, deleted_at=datetime.combine(day(-8), time(10, 0)), name="Sleep"),
        ]
        self.answers = {EMA: self._ema_answers(), INTAKE: self._intake_answers()}

    # -- data ------------------------------------------------------------------------

    def _row(
        self,
        applet,
        answer_id,
        activity,
        subject,
        secret,
        *,
        end: datetime,
        event=None,
        scheduled: datetime | None = None,
        flow=None,
        submit=None,
        nickname="",
        tag="",
        reviewed=None,
        respondent=None,
        start=None,
        applet_version="1.0.0",
    ):
        return {
            "id": answer_id,
            "submitId": submit or f"submit-{answer_id}",
            "version": applet_version,
            "appletHistoryId": f"{applet}_{applet_version}",
            "appletId": applet,
            "activityHistoryId": f"{activity}_{applet_version}",
            "activityId": activity,
            "flowHistoryId": f"{flow}_{applet_version}" if flow else None,
            "flowId": flow,
            "flowName": "Weekly flow" if flow else None,
            "reviewedAnswerId": reviewed,
            "reviewedFlowSubmitId": None,
            "respondentId": respondent or subject,
            "respondentSecretId": secret,
            "sourceSubjectId": subject,
            "sourceSecretId": secret,
            "targetSubjectId": subject,
            "targetSecretId": secret,
            "targetUserNickname": nickname,
            "targetUserTag": tag,
            "inputSubjectId": subject,
            "relation": "self",
            "userPublicKey": "[1,2,3]",
            "answer": "ciphertext",
            "itemIds": [],
            "events": "ciphertext",
            "scheduledDatetime": _utc_seconds(scheduled) if scheduled else None,
            "startDatetime": _utc_seconds(start or end - timedelta(minutes=5)),
            "endDatetime": _utc_seconds(end),
            "migratedDate": None,
            "tzOffset": TZ,
            "scheduledEventId": event,
            "scheduledEventHistoryId": f"{event}_20260901-1" if event else None,
            "createdAt": _utc_iso(end + timedelta(seconds=2)),
            "client": {"appId": "mindlogger-mobile", "appVersion": "1.0", "width": 1, "height": 1},
        }  # fmt: skip

    def _ema_answers(self) -> list[dict]:
        at = lambda offset, hh, mm=0: datetime.combine(day(offset), time(hh, mm))
        p2_weekday = first_weekday_from(day(-6))
        rows = [
            # P001: in window, late, spread-to-next-day evening, window-matched legacy, a repeat, a complete flow
            self._row(EMA, "r01", MORNING, S1, "P001", end=at(-10, 9), event=EV_MORNING, scheduled=at(-10, 8), nickname="Pat", tag="Child"),
            self._row(EMA, "r02", MORNING, S1, "P001", end=at(-9, 13), event=EV_MORNING, scheduled=at(-9, 8)),
            self._row(EMA, "r03", EVENING, S1, "P001", end=at(-9, 1, 30), event=EV_EVENING, scheduled=at(-10, 20)),
            self._row(EMA, "r04", MORNING, S1, "P001", end=at(-8, 10)),
            self._row(EMA, "r05", MORNING, S1, "P001", end=at(-8, 11)),
            self._row(EMA, "r06", SLEEP, S1, "P001", end=at(-7, 10), flow=WEEKLY_FLOW, submit="flow-1", event=EV_FLOW, scheduled=at(-7, 0)),
            self._row(EMA, "r07", MOOD, S1, "P001", end=at(-7, 10, 10), flow=WEEKLY_FLOW, submit="flow-1", event=EV_FLOW, scheduled=at(-7, 0)),
            self._row(EMA, "r08", BASELINE, S1, "P001", end=at(-10, 15), event=EV_BASELINE, scheduled=at(-10, 0)),
            self._row(EMA, "r09", SLEEP, S1, "P001", end=at(-10, 21, 30), event=EV_SLEEP_OLD, scheduled=at(-10, 21)),
            # P002: individual weekday mornings, and a flow started but not finished
            self._row(EMA, "r10", MORNING, S2, "P002", end=datetime.combine(p2_weekday, time(9, 30)), event=EV_MORNING_P2,
                      scheduled=datetime.combine(p2_weekday, time(9, 0))),
            self._row(EMA, "r11", SLEEP, S2, "P002", end=at(-7, 18), flow=WEEKLY_FLOW, submit="flow-2", event=EV_FLOW, scheduled=at(-7, 0)),
            # P003 is a shell account (no login of their own); a manager answered on the phone for them
            self._row(EMA, "r12", EVENING, S3, "P003", end=at(-2, 21), event=EV_EVENING, scheduled=at(-2, 20), respondent=MANAGER),
            # A reviewer's assessment of r01, and a participant removed from the applet with data kept
            self._row(EMA, "r13", RATING, S1, "P001", end=at(-9, 16), reviewed="r01", respondent=MANAGER),
            self._row(EMA, "r14", MORNING, S9, "P009", end=at(-6, 9), event=EV_MORNING, scheduled=at(-6, 8)),
        ]  # fmt: skip
        return sorted(rows, key=lambda r: r["createdAt"], reverse=True)

    def _intake_answers(self) -> list[dict]:
        at = lambda offset, hh: datetime.combine(day(offset), time(hh, 0))
        return [
            self._row(INTAKE, "i01", CONSENT, S1B, "P001", end=at(-11, 10), nickname="Pat"),
            self._row(INTAKE, "i02", DEMOGRAPHICS, S1B, "P001", end=at(-11, 10)),
            self._row(INTAKE, "i03", CONSENT, S2B, "p002x", end=at(-9, 15)),
        ]

    def applet_detail(self, applet_id: str) -> dict:
        if applet_id == EMA:
            return {
                "id": EMA, "displayName": "EMA Study", "version": "1.0.0", "ownerId": OWNER_ID, "encryption": {},
                "activities": [
                    {"id": MORNING, "name": "Morning", "isHidden": False, "isReviewable": False, "order": 0},
                    {"id": EVENING, "name": "Evening", "isHidden": False, "isReviewable": False, "order": 1},
                    {"id": SLEEP, "name": "Sleep", "isHidden": False, "isReviewable": False, "order": 2},
                    {"id": MOOD, "name": "Mood", "isHidden": False, "isReviewable": False, "order": 3},
                    {"id": RATING, "name": "Clinician rating", "isHidden": False, "isReviewable": True, "order": 4},
                    {"id": HIDDEN, "name": "Hidden one", "isHidden": True, "isReviewable": False, "order": 5},
                    {"id": BASELINE, "name": "Baseline survey", "isHidden": False, "isReviewable": False, "order": 6},
                ],
                "activityFlows": [{"id": WEEKLY_FLOW, "name": "Weekly flow", "isHidden": False, "activityIds": [SLEEP, MOOD], "order": 0}],
            }  # fmt: skip
        return {
            "id": INTAKE, "displayName": "Intake", "version": "2.0.0", "ownerId": OWNER_ID, "encryption": {},
            "activities": [
                {"id": CONSENT, "name": "Consent", "isHidden": False, "isReviewable": False, "order": 0},
                {"id": DEMOGRAPHICS, "name": "Demographics", "isHidden": False, "isReviewable": False, "order": 1},
            ],
            "activityFlows": [],
        }  # fmt: skip

    def respondents(self, applet_id: str) -> list[dict]:
        def detail(subject, secret, nickname, created_offset, *, individual=False, deleted=False, applet=applet_id):
            created = datetime.combine(day(created_offset), time(9, 0))
            return {
                "appletId": applet, "appletDisplayName": "x", "subjectId": subject, "respondentSecretId": secret,
                "respondentNickname": nickname, "subjectTag": "Child", "subjectCreatedAt": _utc_iso(created),
                "subjectUpdatedAt": _utc_iso(created), "subjectIsDeleted": deleted, "hasIndividualSchedule": individual,
                "invitation": None, "roles": ["respondent"], "subjectFirstName": "", "subjectLastName": "",
            }  # fmt: skip

        if applet_id == EMA:
            return [
                {
                    "id": U1,
                    "status": "approved",
                    "isAnonymousRespondent": False,
                    "details": [detail(S1, "P001", "Pat", -10)],
                },
                {
                    "id": U2,
                    "status": "approved",
                    "isAnonymousRespondent": False,
                    "details": [detail(S2, "P002", "", -8, individual=True)],
                },
                {
                    "id": None,
                    "status": "not_invited",
                    "isAnonymousRespondent": False,
                    "details": [detail(S3, "P003", "", -3)],
                },
                {
                    "id": U5,
                    "status": "approved",
                    "isAnonymousRespondent": False,
                    "details": [detail(S4, "P004", "", -9, deleted=True)],
                },
                {
                    "id": "anon",
                    "status": "approved",
                    "isAnonymousRespondent": True,
                    "details": [detail("anon-s", "anon", "", -9)],
                },
            ]
        return [
            {
                "id": U1,
                "status": "approved",
                "isAnonymousRespondent": False,
                "details": [detail(S1B, "P001", "Pat", -11)],
            },
            {"id": U2, "status": "approved", "isAnonymousRespondent": False, "details": [detail(S2B, "p002x", "", -9)]},
        ]

    # -- transport ------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _token(self) -> dict:
        self._counter += 1
        token = f"access-{self._counter}"
        self._tokens.add(token)
        return {"accessToken": token, "refreshToken": "refresh-1", "tokenType": "Bearer"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = parse_qs(request.url.query.decode())
        body = json.loads(request.content) if request.content else {}
        if path == "/auth/login":
            if body.get("email") != EMAIL or body.get("password") != PASSWORD:
                return httpx.Response(401, json={"result": [{"message": "Incorrect email or password"}]})
            return httpx.Response(200, json={"result": {"token": self._token(), "user": {"email": EMAIL}}})
        if path == "/auth/token/refresh":
            return httpx.Response(200, json={"result": self._token()})
        if request.headers.get("Authorization", "").removeprefix("Bearer ") not in self._tokens:
            return httpx.Response(401, json={"result": [{"message": "Not authenticated"}]})

        too_big = self.max_page and int(params.get("limit", ["10000"])[0]) > self.max_page
        if too_big and path.endswith(("/respondents", "/events/history", "/data")):
            return httpx.Response(500, json={"detail": "Internal Server Error"})

        if path == "/workspaces":
            return httpx.Response(200, json={"result": [{"ownerId": OWNER_ID, "workspaceName": "Lab"}], "count": 1})
        if path == f"/workspaces/{OWNER_ID}/applets":
            rows = [
                {"id": EMA, "displayName": "EMA Study", "role": "owner", "type": "applet"},
                {"id": INTAKE, "displayName": "Intake", "role": "manager", "type": "applet"},
                {"id": "folder-1", "displayName": "Archive", "type": "folder"},
            ]
            return httpx.Response(200, json={"result": rows, "count": len(rows)})
        for applet_id in (EMA, INTAKE):
            if path == f"/applets/{applet_id}":
                return httpx.Response(200, json={"result": self.applet_detail(applet_id)})
            if path == f"/workspaces/{OWNER_ID}/applets/{applet_id}/respondents":
                rows = self.respondents(applet_id)
                page, limit = int(params.get("page", ["1"])[0]), int(params.get("limit", ["10000"])[0])
                chunk = rows[(page - 1) * limit : page * limit]
                return httpx.Response(200, json={"result": chunk, "count": len(rows)})
            if path == f"/applets/{applet_id}/events/history":
                if applet_id == INTAKE:
                    return httpx.Response(404, json={"detail": "Not Found"})
                page, limit = int(params.get("page", ["1"])[0]), int(params.get("limit", ["10000"])[0])
                chunk = self.history[(page - 1) * limit : page * limit]
                return httpx.Response(200, json={"result": copy.deepcopy(chunk), "count": len(self.history)})
            if path == f"/applets/{applet_id}/events":
                if self.fail_events and not params.get("respondentId"):
                    return httpx.Response(500, json={"detail": "Internal Server Error"})
                if applet_id == INTAKE and self.intake_events_forbidden:
                    return httpx.Response(403, json={"result": [{"message": "Access denied"}]})
                respondent = params.get("respondentId", [None])[0]
                rows = self.individual_events.get((applet_id, respondent), []) if respondent else self.events[applet_id]
                return httpx.Response(200, json={"result": copy.deepcopy(rows), "count": len(rows)})
            if path == f"/answers/applet/{applet_id}/data":
                rows = self.answers[applet_id]
                if start := params.get("fromDate"):
                    rows = [r for r in rows if r["createdAt"] >= start[0]]
                page, limit = int(params.get("page", ["1"])[0]), int(params.get("limit", ["10000"])[0])
                chunk = rows[(page - 1) * limit : page * limit]
                activities = [
                    {
                        "id": a["id"],
                        "idVersion": f"{a['id']}_1.0.0",
                        "name": a["name"],
                        "isReviewable": a["isReviewable"],
                        "items": [],
                    }
                    for a in self.applet_detail(applet_id)["activities"]
                    if any(r["activityId"] == a["id"] for r in chunk)
                ]
                return httpx.Response(
                    200,
                    json={"result": {"answers": copy.deepcopy(chunk), "activities": activities}, "count": len(rows)},
                )
        return httpx.Response(404, json={"result": [{"message": f"No route {path}"}]})
