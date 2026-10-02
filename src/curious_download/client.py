"""Minimal client for the Curious backend REST API."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Self

import httpx

# Roles that may call the answer export endpoint (backend check_answers_export_access).
EXPORT_ROLES = {"super_admin", "owner", "manager", "reviewer"}

_RETRY_STATUS = {502, 503, 504}
_MAX_ATTEMPTS = 4


class ApiError(Exception):
    """An error response from the Curious API."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"{message} (HTTP {status_code})")
        self.status_code = status_code
        self.message = message


class MfaRequiredError(Exception):
    """Login needs a TOTP or recovery code but none was provided."""


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200] or response.reason_phrase
    result = body.get("result") if isinstance(body, dict) else None
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return "; ".join(str(err.get("message", err)) for err in result)
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return response.reason_phrase


@dataclass
class Participant:
    """A subject of an applet, as listed on the admin panel's Participants page."""

    subject_id: str
    secret_id: str
    nickname: str
    tag: str
    user_id: str | None

    @property
    def label(self) -> str:
        extra = ", ".join(part for part in (self.nickname, self.tag) if part)
        return f"{self.secret_id} ({extra})" if extra else self.secret_id


class CuriousClient:
    """Talks to one Curious backend. Call `login` before anything else."""

    def __init__(self, base_url: str, *, timeout: float = 120.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self.user: dict[str, Any] = {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- auth -------------------------------------------------------------

    def login(self, email: str, password: str, mfa_code: str | Callable[[], str] | None = None) -> dict:
        """Log in. `mfa_code` may be a code or a callable that asks the user for one."""
        result = self._post_json("/auth/login", {"email": email, "password": password}, auth=False)
        if result.get("mfaRequired"):
            if mfa_code is None:
                raise MfaRequiredError("This account uses two-factor authentication; an MFA code is required.")
            code = mfa_code() if callable(mfa_code) else mfa_code
            code = code.strip()
            if code.isdigit() and len(code) == 6:
                result = self._post_json(
                    "/auth/mfa/totp/verify", {"mfaToken": result["mfaToken"], "totpCode": code}, auth=False
                )
            else:
                result = self._post_json(
                    "/auth/mfa/recovery-codes/verify", {"mfaToken": result["mfaToken"], "code": code}, auth=False
                )
        self._store_tokens(result["token"])
        self.user = result.get("user", {})
        return self.user

    def _store_tokens(self, token: dict) -> None:
        self._access_token = token["accessToken"]
        self._refresh_token = token.get("refreshToken", self._refresh_token)

    def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        try:
            result = self._post_json("/auth/token/refresh", {"refreshToken": self._refresh_token}, auth=False)
        except ApiError:
            return False
        self._store_tokens(result)
        return True

    # -- transport --------------------------------------------------------

    def _post_json(self, path: str, body: dict, *, auth: bool = True) -> Any:
        return self._request("POST", path, json=body, auth=auth)["result"]

    def _request(self, method: str, path: str, *, auth: bool = True, **kwargs) -> Any:
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            headers = {"Authorization": f"Bearer {self._access_token}"} if auth and self._access_token else {}
            try:
                response = self._http.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError:
                if attempt >= _MAX_ATTEMPTS:
                    raise
                time.sleep(2**attempt)
                continue

            if response.status_code == 401 and auth and not refreshed:
                refreshed = True
                if self._refresh():
                    continue
            if response.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS:
                time.sleep(2**attempt)
                continue
            if response.is_error:
                raise ApiError(response.status_code, _error_message(response))
            return response.json()

    def _get(self, path: str, params: Any = None) -> Any:
        return self._request("GET", path, params=params)

    # -- endpoints --------------------------------------------------------

    def workspaces(self) -> list[dict]:
        """[{ownerId, workspaceName, ...}]"""
        return self._get("/workspaces")["result"]

    def workspace_applets(self, owner_id: str) -> list[dict]:
        """Applets in a workspace, including those inside folders."""
        params = {"flatList": "true", "limit": 10000}
        return self._get(f"/workspaces/{owner_id}/applets", params)["result"]

    def applet(self, applet_id: str) -> dict:
        """Applet details: encryption, activities and activityFlows (without items)."""
        return self._get(f"/applets/{applet_id}")["result"]

    def participants(self, owner_id: str, applet_id: str) -> list[Participant]:
        rows = self._get(f"/workspaces/{owner_id}/applets/{applet_id}/respondents", {"limit": 10000})["result"]
        found: dict[str, Participant] = {}
        for row in rows:
            for detail in row.get("details") or []:
                if detail.get("appletId") not in (None, applet_id) or not detail.get("subjectId"):
                    continue
                subject_id = detail["subjectId"]
                found[subject_id] = Participant(
                    subject_id=subject_id,
                    secret_id=detail.get("respondentSecretId") or "",
                    nickname=detail.get("respondentNickname") or "",
                    tag=detail.get("subjectTag") or "",
                    user_id=row.get("id"),
                )
        return sorted(found.values(), key=lambda p: p.secret_id.lower())

    def export_page(
        self,
        applet_id: str,
        *,
        page: int,
        limit: int,
        target_subject_ids: Sequence[str] = (),
        respondent_ids: Sequence[str] = (),
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict:
        """One page of encrypted answers: {"result": {"answers": [...], "activities": [...]}, "count": n}."""
        params: list[tuple[str, str | int]] = [("page", page), ("limit", limit)]
        params += [("targetSubjectIds", subject_id) for subject_id in target_subject_ids]
        params += [("respondentIds", user_id) for user_id in respondent_ids]
        if from_date:
            params.append(("fromDate", from_date))
        if to_date:
            params.append(("toDate", to_date))
        return self._get(f"/answers/applet/{applet_id}/data", params)
