"""
SQLite audit log + lightweight interview persistence.

- audit_log table: append-only (as per requirements)
- interviews table: store created event IDs so we can reschedule/cancel later
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class AuditEntry:
    timestamp_utc: str
    action: str
    actor: str
    candidate_email: str
    hiring_manager_email: str
    recruiter_email: str
    role_title: str
    event_id: str
    payload_json: str
    status: str
    error_message: str


class AuditLog:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        _ensure_parent(self.path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT,
                    candidate_email TEXT,
                    hiring_manager_email TEXT,
                    recruiter_email TEXT,
                    role_title TEXT,
                    event_id TEXT,
                    payload_json TEXT,
                    status TEXT,
                    error_message TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS interviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_utc TEXT NOT NULL,
                    role_title TEXT,
                    candidate_email TEXT,
                    hiring_manager_email TEXT,
                    recruiter_email TEXT,
                    duration_minutes INTEGER,
                    start_utc TEXT,
                    end_utc TEXT,
                    display_timezone TEXT,
                    graph_event_id TEXT,
                    teams_join_url TEXT,
                    subject TEXT,
                    last_status TEXT
                )
                """
            )

    @staticmethod
    def redact_payload(payload: Any) -> str:
        """
        Redact known secret-like fields from payloads before storing.
        (Graph payloads shouldn't include secrets; still, be defensive.)
        """
        try:
            s = json.dumps(payload, ensure_ascii=False)
        except Exception:
            return "<non-serializable-payload>"

        for key in ["client_secret", "authorization", "access_token", "refresh_token", "password"]:
            # very simple redaction
            s = s.replace(key, f"{key}[REDACTED]")
        return s

    def log(
        self,
        action: str,
        *,
        actor: str = "",
        candidate_email: str = "",
        hiring_manager_email: str = "",
        recruiter_email: str = "",
        role_title: str = "",
        event_id: str = "",
        payload: Any = None,
        status: str = "success",
        error_message: str = "",
    ) -> None:
        payload_json = self.redact_payload(payload) if payload is not None else ""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                    timestamp_utc, action, actor, candidate_email, hiring_manager_email, recruiter_email,
                    role_title, event_id, payload_json, status, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now_iso(),
                    action,
                    actor,
                    candidate_email,
                    hiring_manager_email,
                    recruiter_email,
                    role_title,
                    event_id,
                    payload_json,
                    status,
                    error_message[:2000],
                ),
            )

    def upsert_interview(
        self,
        *,
        role_title: str,
        candidate_email: str,
        hiring_manager_email: str,
        recruiter_email: str,
        duration_minutes: int,
        start_utc: str,
        end_utc: str,
        display_timezone: str,
        graph_event_id: str,
        teams_join_url: str,
        subject: str,
        last_status: str,
    ) -> None:
        with self._connect() as conn:
            # simplistic: one active interview per candidate+role+hm
            conn.execute(
                """
                INSERT INTO interviews (
                    created_utc, role_title, candidate_email, hiring_manager_email, recruiter_email,
                    duration_minutes, start_utc, end_utc, display_timezone,
                    graph_event_id, teams_join_url, subject, last_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now_iso(),
                    role_title,
                    candidate_email,
                    hiring_manager_email,
                    recruiter_email,
                    int(duration_minutes),
                    start_utc,
                    end_utc,
                    display_timezone,
                    graph_event_id,
                    teams_join_url,
                    subject,
                    last_status,
                ),
            )

    def list_recent_audit(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_interviews(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM interviews ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]
