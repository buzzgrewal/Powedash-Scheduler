"""
SQLite audit log + lightweight interview persistence.

- audit_log table: append-only (as per requirements)
- interviews table: store created event IDs so we can reschedule/cancel later
- Structured logging for production diagnostics
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


# ----------------------------
# Structured Logging
# ----------------------------
class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def _setup_logger() -> logging.Logger:
    """Configure structured JSON-style logger for production diagnostics."""
    logger = logging.getLogger("powerdash")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            '{"time":"%(asctime)s","level":"%(levelname)s","module":"%(name)s","message":"%(message)s"}'
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


_logger = _setup_logger()


def log_structured(
    level: LogLevel,
    message: str,
    *,
    action: str = "",
    error_type: str = "",
    details: Optional[Dict[str, Any]] = None,
    exc_info: bool = False,
) -> None:
    """Log with structured context for aggregation and diagnostics."""
    extra = {"action": action, "error_type": error_type}
    if details:
        extra.update(details)
    log_msg = f"{message} | {extra}"

    if exc_info:
        log_msg += f" | traceback={traceback.format_exc()}"

    getattr(_logger, level.value.lower())(log_msg)


# ----------------------------
# Utilities
# ----------------------------
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


# ----------------------------
# AuditLog Class
# ----------------------------
class AuditLog:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        _ensure_parent(self.path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Create a thread-safe connection with WAL mode for Streamlit concurrency."""
        try:
            conn = sqlite3.connect(
                str(self.path),
                timeout=30.0,  # Wait up to 30s for locks
                check_same_thread=False,  # Streamlit runs in multiple threads
            )
            conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrent read/write
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")  # 30s busy timeout
            return conn
        except sqlite3.Error as e:
            log_structured(
                LogLevel.ERROR,
                f"Database connection failed: {e}",
                action="db_connect",
                error_type="sqlite_error",
                exc_info=True,
            )
            raise

    def _init_db(self) -> None:
        """Initialize database tables."""
        try:
            conn = self._connect()
            try:
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
                conn.commit()

                # Migration: Add candidate_timezone column if missing
                try:
                    conn.execute("SELECT candidate_timezone FROM interviews LIMIT 1")
                except sqlite3.OperationalError:
                    conn.execute("ALTER TABLE interviews ADD COLUMN candidate_timezone TEXT")
                    conn.commit()
                    log_structured(
                        LogLevel.INFO,
                        "Added candidate_timezone column to interviews table",
                        action="db_migration",
                    )
            finally:
                conn.close()
        except sqlite3.Error as e:
            log_structured(
                LogLevel.ERROR,
                f"Database initialization failed: {e}",
                action="db_init",
                error_type="sqlite_error",
                exc_info=True,
            )
            raise

    @staticmethod
    def redact_payload(payload: Any) -> str:
        """
        Redact known secret-like fields from payloads before storing.
        Returns safe JSON string, never fails.
        """
        try:
            # Handle non-JSON-serializable objects
            if hasattr(payload, '__dict__') and not isinstance(payload, dict):
                payload = payload.__dict__
            s = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError, RecursionError) as e:
            log_structured(
                LogLevel.WARNING,
                f"Payload serialization failed: {e}",
                action="redact_payload",
                error_type="serialization_error",
            )
            return f"<serialization-failed: {type(payload).__name__}>"

        # Redact sensitive keys with proper regex (JSON-aware)
        for key in ["client_secret", "authorization", "access_token", "refresh_token", "password", "api_key"]:
            s = re.sub(rf'"{key}":\s*"[^"]*"', f'"{key}": "[REDACTED]"', s, flags=re.IGNORECASE)
            s = re.sub(rf'{key}=[^\s&"]+', f'{key}=[REDACTED]', s, flags=re.IGNORECASE)
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
    ) -> bool:
        """
        Append audit entry. Returns True on success, False on failure.
        Never raises - audit logging should not crash the app.
        """
        payload_json = self.redact_payload(payload) if payload is not None else ""

        try:
            conn = self._connect()
            try:
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
                        error_message[:2000] if error_message else "",
                    ),
                )
                conn.commit()  # Explicit commit - critical for durability
                return True
            finally:
                conn.close()
        except sqlite3.Error as e:
            log_structured(
                LogLevel.ERROR,
                f"Audit log write failed: {e}",
                action="audit_write",
                error_type="sqlite_error",
                details={"attempted_action": action},
                exc_info=True,
            )
            return False

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
        candidate_timezone: str,
        graph_event_id: str,
        teams_join_url: str,
        subject: str,
        last_status: str,
    ) -> bool:
        """
        Insert interview record. Returns True on success, False on failure.

        Args:
            candidate_timezone: IANA timezone for the candidate (used for invitation display)
        """
        try:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO interviews (
                        created_utc, role_title, candidate_email, hiring_manager_email, recruiter_email,
                        duration_minutes, start_utc, end_utc, display_timezone, candidate_timezone,
                        graph_event_id, teams_join_url, subject, last_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        candidate_timezone,
                        graph_event_id,
                        teams_join_url,
                        subject,
                        last_status,
                    ),
                )
                conn.commit()  # Explicit commit
                return True
            finally:
                conn.close()
        except sqlite3.Error as e:
            log_structured(
                LogLevel.ERROR,
                f"Interview upsert failed: {e}",
                action="interview_upsert",
                error_type="sqlite_error",
                exc_info=True,
            )
            return False

    def interview_exists(
        self,
        *,
        candidate_email: str,
        hiring_manager_email: str,
        role_title: str,
        start_utc: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Check if an interview already exists for this combination (idempotency check).
        Returns the existing interview dict if found, None otherwise.
        """
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT * FROM interviews
                    WHERE LOWER(candidate_email) = LOWER(?)
                    AND LOWER(hiring_manager_email) = LOWER(?)
                    AND role_title = ?
                    AND start_utc = ?
                    AND last_status NOT IN ('cancelled', 'deleted')
                    LIMIT 1
                    """,
                    (candidate_email, hiring_manager_email, role_title, start_utc),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        except sqlite3.Error as e:
            log_structured(
                LogLevel.WARNING,
                f"Interview exists check failed: {e}",
                action="interview_exists_check",
                error_type="sqlite_error",
                exc_info=True,
            )
            return None  # On error, allow creation (fail open)

    def list_recent_audit(self, limit: int = 200) -> List[Dict[str, Any]]:
        """List recent audit log entries. Returns empty list on error."""
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        except sqlite3.Error as e:
            log_structured(
                LogLevel.ERROR,
                f"Audit log read failed: {e}",
                action="audit_read",
                error_type="sqlite_error",
                exc_info=True,
            )
            return []

    def list_interviews(self, limit: int = 200) -> List[Dict[str, Any]]:
        """List recent interviews. Returns empty list on error."""
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM interviews ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        except sqlite3.Error as e:
            log_structured(
                LogLevel.ERROR,
                f"Interview list read failed: {e}",
                action="interview_read",
                error_type="sqlite_error",
                exc_info=True,
            )
            return []
