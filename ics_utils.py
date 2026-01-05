"""
RFC 5545-ish .ics generator.

- DTSTART/DTEND in UTC (Z)
- Stable UID (caller can supply, else derived)
- ORGANIZER + ATTENDEE lines
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional


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
    # https://www.rfc-editor.org/rfc/rfc5545#section-3.3.11
    return (
        (s or "")
        .replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\n", r"\n")
    )


def _fmt_dt_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%Y%m%dT%H%M%SZ")


def stable_uid(*parts: str) -> str:
    raw = "|".join([p.strip().lower() for p in parts if p])
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{h}@powerdashhr.com"


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

    def to_ics(self) -> bytes:
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
        ]

        if self.location:
            lines.append(f"LOCATION:{_escape_text(self.location)}")

        org_cn = _escape_text(self.organizer_name or "Scheduler")
        organizer = f"ORGANIZER;CN={org_cn}:mailto:{self.organizer_email}"
        lines.append(organizer)

        # Attendees
        for e in [x.strip() for x in self.attendee_emails if x and x.strip()]:
            # Basic attendee line (can add RSVP=TRUE etc if needed)
            lines.append(f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{e}")

        lines.extend(["END:VEVENT", "END:VCALENDAR"])

        folded = "\r\n".join([_fold_ical_line(l) for l in lines]) + "\r\n"
        return folded.encode("utf-8")
