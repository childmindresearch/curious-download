"""An in-memory stand-in for the Curious backend, served through httpx.MockTransport."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from curious_download.crypto import (
    KEY_VARIANT_BROWSER,
    _aes_key_candidates,
    _int_to_bytes,
    applet_private_key,
    encrypt_with_key,
)

from . import corpus

OWNER_ID = "66666666-6666-4666-8666-666666666666"
SCORED_ACTIVITY_ID = "77777777-7777-4777-8777-777777777777"
SECOND_SUBJECT = "88888888-8888-4888-8888-888888888888"
EMAIL = "researcher@example.org"
PASSWORD = "account-pw"
APPLET_PASSWORD = "applet-pw"
ACCOUNT_ID = OWNER_ID
PRIME = json.loads((Path(__file__).parent / "fixtures" / "prime.json").read_text())


class FakeCurious:
    def __init__(self, *, mfa: bool = False, expire_first_data_token: bool = False):
        self.mfa = mfa
        self.expire_first_data_token = expire_first_data_token
        self.requests: list[httpx.Request] = []
        self.storage_requests: list[httpx.Request] = []
        self._token_counter = 0
        self._valid_tokens: set[str] = set()

        prime = int.from_bytes(bytes(PRIME), "big")
        applet_private = applet_private_key(APPLET_PASSWORD, ACCOUNT_ID, KEY_VARIANT_BROWSER)
        self.encryption = {
            "publicKey": json.dumps(list(_int_to_bytes(pow(2, applet_private, prime)))),
            "prime": json.dumps(PRIME),
            "base": json.dumps([2]),
            "accountId": ACCOUNT_ID,
        }
        self.activities = {
            a["idVersion"]: a
            for a in (
                corpus.activity(corpus.ALL_TYPES_ITEMS),
                {
                    **corpus.activity(corpus.SUBSCALE_ITEMS, name="Scored", subscale_setting=corpus.SUBSCALE_SETTING),
                    "id": SCORED_ACTIVITY_ID,
                    "idVersion": f"{SCORED_ACTIVITY_ID}_2.2.4",
                },
            )
        }
        self.plain_answers: dict[str, list] = {}
        self.answers = self._make_answers(prime, applet_private)

    def _encrypt_for(self, respondent: str, plaintext: str, prime: int, applet_private: int) -> tuple[str, str]:
        user_private = int.from_bytes(hashlib.sha512(respondent.encode()).digest(), "big")
        user_public = pow(2, user_private, prime)
        shared = pow(user_public, applet_private, prime)
        key = _aes_key_candidates(shared, len(PRIME))[0]
        return json.dumps(list(_int_to_bytes(user_public))), encrypt_with_key(key, plaintext, os.urandom(16))

    def _make_answers(self, prime: int, applet_private: int) -> list[dict]:
        plan = [
            # (id, subject, secret id, activity history id, decrypted answers, created at)
            ("e1", corpus.SUBJECT_ID, "P001", f"{corpus.ACTIVITY_ID}_2.2.4", corpus.ALL_TYPES_ANSWERS, "2026-09-01T12:00:00"),
            ("e2", corpus.SUBJECT_ID, "P001", f"{SCORED_ACTIVITY_ID}_2.2.4", ["10", {"value": 0}, {"value": 0}, {"value": [0]}, {"value": 2}, None], "2026-09-10T12:00:00"),
            ("e3", SECOND_SUBJECT, "P002", f"{SCORED_ACTIVITY_ID}_2.2.4", ["30", {"value": 1}, {"value": 2}, {"value": [1, 2]}, {"value": 4}, None], "2026-09-15T12:00:00"),
            ("e4", corpus.SUBJECT_ID, "P001", f"{SCORED_ACTIVITY_ID}_2.2.4", [None, {"value": 1}, {"value": 1}, {"value": [2]}, {"value": 1}, None], "2026-09-20T12:00:00"),
            ("e5", corpus.SUBJECT_ID, "P001", f"{SCORED_ACTIVITY_ID}_2.2.4", ["11", None, {"value": 2}, None, {"value": 3}, None], "2026-08-01T12:00:00"),
        ]  # fmt: skip
        answers = []
        for answer_id, subject, secret, history_id, plain, created in plan:
            activity = self.activities[history_id]
            public_key, ciphertext = self._encrypt_for(subject, json.dumps(plain), prime, applet_private)
            _, events = self._encrypt_for(subject, json.dumps([]), prime, applet_private)
            record = corpus.answer_record(
                answer_id,
                activityHistoryId=history_id,
                activityId=activity["id"],
                targetSubjectId=subject,
                targetSecretId=secret,
                sourceSubjectId=subject,
                sourceSecretId=secret,
                inputSubjectId=subject,
                inputSecretId=secret,
                respondentSecretId=secret,
                userPublicKey=public_key,
                answer=ciphertext,
                events=events,
                createdAt=created,
            )
            answers.append(record)
            self.plain_answers[answer_id] = plain
        # The API returns newest first.
        return sorted(answers, key=lambda a: a["createdAt"], reverse=True)

    # -- transport ---------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _token(self) -> dict:
        self._token_counter += 1
        access = f"access-{self._token_counter}"
        self._valid_tokens.add(access)
        return {"accessToken": access, "refreshToken": "refresh-1", "tokenType": "Bearer"}

    def _authorized(self, request: httpx.Request) -> bool:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return token in self._valid_tokens

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.test":
            self.storage_requests.append(request)
            if request.url.params.get("sig") != "ok":
                return httpx.Response(403)
            return httpx.Response(200, content=f"bytes of {request.url.path}".encode())

        self.requests.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else {}

        if path == "/auth/login":
            if body.get("email") != EMAIL or body.get("password") != PASSWORD:
                return httpx.Response(401, json={"result": [{"message": "Incorrect email or password"}]})
            if self.mfa:
                return httpx.Response(200, json={"result": {"mfaRequired": True, "mfaToken": "mfa-1"}})
            return httpx.Response(200, json={"result": {"token": self._token(), "user": {"email": EMAIL}}})
        if path == "/auth/mfa/totp/verify":
            if body.get("mfaToken") != "mfa-1" or body.get("totpCode") != "123456":
                return httpx.Response(401, json={"result": [{"message": "Invalid code"}]})
            return httpx.Response(200, json={"result": {"token": self._token(), "user": {"email": EMAIL}}})
        if path == "/auth/token/refresh":
            if body.get("refreshToken") != "refresh-1":
                return httpx.Response(400, json={"result": [{"message": "Bad refresh token"}]})
            token = self._token()
            return httpx.Response(200, json={"result": token})

        if not self._authorized(request):
            return httpx.Response(401, json={"result": [{"message": "Not authenticated"}]})

        if path == "/workspaces":
            return httpx.Response(200, json={"result": [{"ownerId": OWNER_ID, "workspaceName": "Lab"}], "count": 1})
        if path == f"/workspaces/{OWNER_ID}/applets":
            row = {"id": corpus.APPLET_ID, "displayName": "EMA Study", "role": "owner", "encryption": self.encryption}
            return httpx.Response(200, json={"result": [row], "count": 1})
        if path == f"/applets/{corpus.APPLET_ID}":
            result = {
                "id": corpus.APPLET_ID,
                "displayName": "EMA Study",
                "ownerId": OWNER_ID,
                "version": "2.2.4",
                "encryption": self.encryption,
                "activities": [{"id": a["id"], "name": a["name"]} for a in self.activities.values()],
                "activityFlows": [],
            }
            return httpx.Response(200, json={"result": result})
        if path == f"/workspaces/{OWNER_ID}/applets/{corpus.APPLET_ID}/respondents":
            rows = [
                {"id": corpus.USER_ID, "details": [self._detail(corpus.SUBJECT_ID, "P001", "Pat")]},
                {"id": None, "details": [self._detail(SECOND_SUBJECT, "P002", "")]},
            ]
            return httpx.Response(200, json={"result": rows, "count": 2})
        if path == f"/answers/applet/{corpus.APPLET_ID}/data":
            if self.expire_first_data_token:
                self.expire_first_data_token = False
                self._valid_tokens.clear()
                return httpx.Response(401, json={"result": [{"message": "Token expired"}]})
            return self._export(request)
        if path == f"/file/{corpus.APPLET_ID}/presign":
            # Like the backend: sign storage URLs, return anything else unchanged.
            signed = [
                f"https://storage.test/{url.split('://', 1)[1].split('?')[0]}?sig=ok"
                if url.startswith("s3://")
                else url
                for url in body["privateUrls"]
            ]
            return httpx.Response(200, json={"result": signed, "count": len(signed)})
        return httpx.Response(404, json={"result": [{"message": f"No route {path}"}]})

    @staticmethod
    def _detail(subject_id: str, secret: str, nickname: str) -> dict:
        return {
            "appletId": corpus.APPLET_ID,
            "subjectId": subject_id,
            "respondentSecretId": secret,
            "respondentNickname": nickname,
            "subjectTag": "Child",
        }

    def _export(self, request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        page = int(params.get("page", ["1"])[0])
        limit = int(params.get("limit", ["10000"])[0])
        rows = self.answers
        if subjects := params.get("targetSubjectIds"):
            rows = [a for a in rows if a["targetSubjectId"] in subjects]
        if start := params.get("fromDate"):
            rows = [a for a in rows if a["createdAt"] >= start[0]]
        if end := params.get("toDate"):
            rows = [a for a in rows if a["createdAt"] <= end[0]]
        chunk = rows[(page - 1) * limit : page * limit]
        activities = [self.activities[h] for h in dict.fromkeys(a["activityHistoryId"] for a in chunk)]
        return httpx.Response(
            200,
            json={
                "result": {"answers": copy.deepcopy(chunk), "activities": copy.deepcopy(activities)},
                "count": len(rows),
            },
        )
