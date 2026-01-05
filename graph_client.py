"""
Microsoft Graph client for app-only (client credentials) access to a scheduler mailbox.
Designed for Streamlit apps: no secrets are hard-coded; everything is injected via st.secrets.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import requests


@dataclass(frozen=True)
class GraphConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    scheduler_mailbox: str
    base_url: str = "https://graph.microsoft.com/v1.0"


class GraphAuthError(RuntimeError):
    pass


class GraphAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, response_json: Any | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_json = response_json


class GraphClient:
    """
    Minimal Graph wrapper with:
      - client credentials token caching
      - create / patch / delete event under a scheduler mailbox
      - basic "diagnostics" helpers
    """

    def __init__(self, cfg: GraphConfig, timeout_s: int = 30):
        self.cfg = cfg
        self.timeout_s = timeout_s
        self._token: Optional[str] = None
        self._token_expiry_utc: Optional[datetime] = None

    # ---------------- Auth ----------------
    def _token_valid(self) -> bool:
        if not self._token or not self._token_expiry_utc:
            return False
        # refresh a little early
        return datetime.now(timezone.utc) < (self._token_expiry_utc - timedelta(minutes=2))

    def get_token(self, force_refresh: bool = False) -> str:
        if (not force_refresh) and self._token_valid():
            return self._token  # type: ignore[return-value]

        token_url = f"https://login.microsoftonline.com/{self.cfg.tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
        resp = requests.post(token_url, data=data, timeout=self.timeout_s)
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": resp.text}

        if resp.status_code >= 400:
            raise GraphAuthError(f"Token request failed ({resp.status_code}): {payload}")

        access_token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 3599))
        if not access_token:
            raise GraphAuthError(f"Token response missing access_token: {payload}")

        self._token = access_token
        self._token_expiry_utc = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        return access_token

    def _headers(self) -> Dict[str, str]:
        token = self.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ---------------- Core HTTP ----------------
    def _request(self, method: str, url: str, *, params: Dict[str, Any] | None = None, json_body: Any | None = None) -> Tuple[int, Any]:
        resp = requests.request(
            method,
            url,
            headers=self._headers(),
            params=params,
            json=json_body,
            timeout=self.timeout_s,
        )
        try:
            body = resp.json() if resp.text else None
        except Exception:
            body = {"raw": resp.text}

        if resp.status_code >= 400:
            # Surface Graph error payload (safe: no secrets, but may include IDs)
            raise GraphAPIError(
                f"Graph {method} failed ({resp.status_code})",
                status_code=resp.status_code,
                response_json=body,
            )
        return resp.status_code, body

    # ---------------- Events ----------------
    def create_event(self, event_payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.cfg.base_url}/users/{self.cfg.scheduler_mailbox}/events"
        _, body = self._request("POST", url, json_body=event_payload)
        return body or {}

    def patch_event(self, event_id: str, patch_payload: Dict[str, Any], send_updates: str = "all") -> Dict[str, Any]:
        url = f"{self.cfg.base_url}/users/{self.cfg.scheduler_mailbox}/events/{event_id}"
        params = {"sendUpdates": send_updates} if send_updates else None
        _, body = self._request("PATCH", url, params=params, json_body=patch_payload)
        return body or {}

    def delete_event(self, event_id: str) -> None:
        url = f"{self.cfg.base_url}/users/{self.cfg.scheduler_mailbox}/events/{event_id}"
        self._request("DELETE", url)

    # ---------------- Diagnostics ----------------
    def me(self) -> Dict[str, Any]:
        # app-only tokens usually cannot call /me; keep for completeness
        url = f"{self.cfg.base_url}/me"
        _, body = self._request("GET", url)
        return body or {}

    def test_calendar_read(self, top: int = 5) -> Dict[str, Any]:
        url = f"{self.cfg.base_url}/users/{self.cfg.scheduler_mailbox}/calendar/events"
        params = {"$top": str(top), "$orderby": "start/dateTime desc"}
        _, body = self._request("GET", url, params=params)
        return body or {}

    def create_dummy_event(self, subject: str, start_dt: Dict[str, str], end_dt: Dict[str, str], dry_run: bool = True) -> Dict[str, Any]:
        payload = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": "PowerDash Graph diagnostics dummy event."},
            "start": start_dt,
            "end": end_dt,
            "location": {"displayName": "Diagnostics"},
            "attendees": [],
        }
        if dry_run:
            return {"dry_run": True, "payload": payload}
        return self.create_event(payload)

    # ---------------- Mail ----------------
    def send_mail(
        self,
        subject: str,
        body: str,
        to_recipients: list[str],
        cc_recipients: list[str] | None = None,
        content_type: str = "Text",
        attachment: Dict[str, Any] | None = None,
        save_to_sent: bool = True,
    ) -> Dict[str, Any]:
        """
        Send an email via Microsoft Graph API.

        Args:
            subject: Email subject
            body: Email body content
            to_recipients: List of recipient email addresses
            cc_recipients: Optional list of CC email addresses
            content_type: "Text" or "HTML"
            attachment: Optional dict with keys: name, contentBytes (base64), contentType
            save_to_sent: Whether to save the email to Sent Items
        """
        url = f"{self.cfg.base_url}/users/{self.cfg.scheduler_mailbox}/sendMail"

        message: Dict[str, Any] = {
            "subject": subject,
            "body": {
                "contentType": content_type,
                "content": body,
            },
            "toRecipients": [
                {"emailAddress": {"address": addr}} for addr in to_recipients if addr
            ],
        }

        if cc_recipients:
            message["ccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in cc_recipients if addr
            ]

        if attachment:
            import base64
            # Ensure content is base64 encoded
            content_bytes = attachment.get("contentBytes")
            if isinstance(content_bytes, bytes):
                content_bytes = base64.b64encode(content_bytes).decode("utf-8")

            message["attachments"] = [{
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": attachment.get("name", "attachment.bin"),
                "contentType": attachment.get("contentType", "application/octet-stream"),
                "contentBytes": content_bytes,
            }]

        payload = {
            "message": message,
            "saveToSentItems": save_to_sent,
        }

        _, response_body = self._request("POST", url, json_body=payload)
        return response_body or {"status": "sent"}
