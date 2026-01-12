"""
RFC 5545-ish .ics generator with validation.

- DTSTART/DTEND in UTC (Z)
- Stable UID (caller can supply, else derived)
- ORGANIZER + ATTENDEE lines
- Input validation to prevent malformed calendar files
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List


def _fold_ical_line(line: str, limit: int = 75) -> str:
    """
    Fold long lines per iCalendar rules: CRLF + space continuation.
    Keep it simple: operate on UTF-8 characters (not byte-perfect, but works well in practice).
    """
    if len(line) <= limit:
        return line
    out = []
    while len(line) > limit:
        out.append(line[:limit])
        line = " " + line[limit:]
    out.append(line)
    return "\r\n".join(out)


def _escape_text(s: str) -> str:
    """
    Escape special characters per RFC 5545 section 3.3.11.
    """
    return (
        (s or "")
        .replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\r\n", r"\n")  # Handle CRLF first
        .replace("\r", r"\n")    # Handle standalone CR
        .replace("\n", r"\n")    # Handle standalone LF
    )


def _fmt_dt_utc(dt: datetime) -> str:
    """Format datetime as UTC iCalendar format (YYYYMMDDTHHMMSSZ)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%Y%m%dT%H%M%SZ")


def stable_uid(*parts: str) -> str:
    """Generate a stable UID from parts for idempotent event creation."""
    raw = "|".join([p.strip().lower() for p in parts if p])
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{h}@powerdashhr.com"


class ICSValidationError(ValueError):
    """Raised when ICS invite data is invalid."""
    pass


@dataclass
class ICSInvite:
    uid: str
    dtstart_utc: datetime
    dtend_utc: datetime
    summary: str
    description: str
    organizer_email: str
    organizer_name: str
    attendee_emails: List[str]
    location: str = ""
    url: str = ""  # e.g. Teams join URL

    def __post_init__(self):
        """Validate ICS invite data on construction."""
        errors = []

        if not self.uid or not self.uid.strip():
            errors.append("UID is required")

        if not self.summary or not self.summary.strip():
            errors.append("Summary is required")

        if not self.organizer_email or not self.organizer_email.strip():
            errors.append("Organizer email is required")

        if self.dtstart_utc >= self.dtend_utc:
            errors.append("Start time must be before end time")

        if errors:
            raise ICSValidationError(f"Invalid ICS invite: {'; '.join(errors)}")

    def to_ics(self) -> bytes:
        """
        Generate ICS file content as bytes.
        Raises ICSValidationError if generation fails.
        """
        try:
            now = datetime.now(timezone.utc)
            lines = [
                "BEGIN:VCALENDAR",
                "PRODID:-//PowerDash HR//Interview Scheduler//EN",
                "VERSION:2.0",
                "CALSCALE:GREGORIAN",
                "METHOD:REQUEST",
                "BEGIN:VEVENT",
                f"UID:{self.uid}",
                f"DTSTAMP:{_fmt_dt_utc(now)}",
                f"DTSTART:{_fmt_dt_utc(self.dtstart_utc)}",
                f"DTEND:{_fmt_dt_utc(self.dtend_utc)}",
                f"SUMMARY:{_escape_text(self.summary)}",
                f"DESCRIPTION:{_escape_text(self.description + (('\\n' + self.url) if self.url else ''))}",
                "STATUS:CONFIRMED",
                "SEQUENCE:0",
            ]

            if self.location:
                lines.append(f"LOCATION:{_escape_text(self.location)}")

            org_cn = _escape_text(self.organizer_name or "Scheduler")
            organizer = f"ORGANIZER;CN={org_cn}:mailto:{self.organizer_email}"
            lines.append(organizer)

            # Attendees
            for e in [x.strip() for x in self.attendee_emails if x and x.strip()]:
                lines.append(f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{e}")

            lines.extend(["END:VEVENT", "END:VCALENDAR"])

            folded = "\r\n".join([_fold_ical_line(l) for l in lines]) + "\r\n"
            return folded.encode("utf-8")
        except Exception as e:
            raise ICSValidationError(f"Failed to generate ICS: {e}") from e
