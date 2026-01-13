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
from typing import Dict, List, Optional


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


# VTIMEZONE definitions for common timezones
# These follow RFC 5545 VTIMEZONE component structure
_VTIMEZONE_DEFS: Dict[str, Dict] = {
    "America/Los_Angeles": {
        "tzid": "America/Los_Angeles",
        "standard": {
            "tzoffsetfrom": "-0700",
            "tzoffsetto": "-0800",
            "tzname": "PST",
            "dtstart": "19701101T020000",
            "rrule": "FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        },
        "daylight": {
            "tzoffsetfrom": "-0800",
            "tzoffsetto": "-0700",
            "tzname": "PDT",
            "dtstart": "19700308T020000",
            "rrule": "FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        },
    },
    "America/Denver": {
        "tzid": "America/Denver",
        "standard": {
            "tzoffsetfrom": "-0600",
            "tzoffsetto": "-0700",
            "tzname": "MST",
            "dtstart": "19701101T020000",
            "rrule": "FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        },
        "daylight": {
            "tzoffsetfrom": "-0700",
            "tzoffsetto": "-0600",
            "tzname": "MDT",
            "dtstart": "19700308T020000",
            "rrule": "FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        },
    },
    "America/Chicago": {
        "tzid": "America/Chicago",
        "standard": {
            "tzoffsetfrom": "-0500",
            "tzoffsetto": "-0600",
            "tzname": "CST",
            "dtstart": "19701101T020000",
            "rrule": "FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        },
        "daylight": {
            "tzoffsetfrom": "-0600",
            "tzoffsetto": "-0500",
            "tzname": "CDT",
            "dtstart": "19700308T020000",
            "rrule": "FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        },
    },
    "America/New_York": {
        "tzid": "America/New_York",
        "standard": {
            "tzoffsetfrom": "-0400",
            "tzoffsetto": "-0500",
            "tzname": "EST",
            "dtstart": "19701101T020000",
            "rrule": "FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        },
        "daylight": {
            "tzoffsetfrom": "-0500",
            "tzoffsetto": "-0400",
            "tzname": "EDT",
            "dtstart": "19700308T020000",
            "rrule": "FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        },
    },
    "Europe/London": {
        "tzid": "Europe/London",
        "standard": {
            "tzoffsetfrom": "+0100",
            "tzoffsetto": "+0000",
            "tzname": "GMT",
            "dtstart": "19701025T020000",
            "rrule": "FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        },
        "daylight": {
            "tzoffsetfrom": "+0000",
            "tzoffsetto": "+0100",
            "tzname": "BST",
            "dtstart": "19700329T010000",
            "rrule": "FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        },
    },
    "Europe/Paris": {
        "tzid": "Europe/Paris",
        "standard": {
            "tzoffsetfrom": "+0200",
            "tzoffsetto": "+0100",
            "tzname": "CET",
            "dtstart": "19701025T030000",
            "rrule": "FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        },
        "daylight": {
            "tzoffsetfrom": "+0100",
            "tzoffsetto": "+0200",
            "tzname": "CEST",
            "dtstart": "19700329T020000",
            "rrule": "FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        },
    },
    "Australia/Sydney": {
        "tzid": "Australia/Sydney",
        "standard": {
            "tzoffsetfrom": "+1100",
            "tzoffsetto": "+1000",
            "tzname": "AEST",
            "dtstart": "19700405T030000",
            "rrule": "FREQ=YEARLY;BYMONTH=4;BYDAY=1SU",
        },
        "daylight": {
            "tzoffsetfrom": "+1000",
            "tzoffsetto": "+1100",
            "tzname": "AEDT",
            "dtstart": "19701004T020000",
            "rrule": "FREQ=YEARLY;BYMONTH=10;BYDAY=1SU",
        },
    },
}


def _generate_vtimezone(tz_name: str) -> List[str]:
    """
    Generate VTIMEZONE component for iCalendar.

    Args:
        tz_name: IANA timezone name

    Returns:
        List of iCalendar lines for VTIMEZONE component.
        Empty list for UTC or unsupported timezones.
    """
    if not tz_name or tz_name == "UTC":
        return []  # UTC doesn't need VTIMEZONE

    if tz_name not in _VTIMEZONE_DEFS:
        return []  # Fall back to UTC times for unsupported TZs

    tz = _VTIMEZONE_DEFS[tz_name]
    lines = [
        "BEGIN:VTIMEZONE",
        f"TZID:{tz['tzid']}",
    ]

    if "standard" in tz:
        s = tz["standard"]
        lines.extend([
            "BEGIN:STANDARD",
            f"TZOFFSETFROM:{s['tzoffsetfrom']}",
            f"TZOFFSETTO:{s['tzoffsetto']}",
            f"TZNAME:{s['tzname']}",
            f"DTSTART:{s['dtstart']}",
            f"RRULE:{s['rrule']}",
            "END:STANDARD",
        ])

    if "daylight" in tz:
        d = tz["daylight"]
        lines.extend([
            "BEGIN:DAYLIGHT",
            f"TZOFFSETFROM:{d['tzoffsetfrom']}",
            f"TZOFFSETTO:{d['tzoffsetto']}",
            f"TZNAME:{d['tzname']}",
            f"DTSTART:{d['dtstart']}",
            f"RRULE:{d['rrule']}",
            "END:DAYLIGHT",
        ])

    lines.append("END:VTIMEZONE")
    return lines


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
    display_timezone: str = "UTC"  # IANA timezone for VTIMEZONE component

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

        If display_timezone is set to a supported timezone, includes a VTIMEZONE
        component for proper DST handling by calendar applications.
        """
        try:
            now = datetime.now(timezone.utc)
            lines = [
                "BEGIN:VCALENDAR",
                "PRODID:-//PowerDash HR//Interview Scheduler//EN",
                "VERSION:2.0",
                "CALSCALE:GREGORIAN",
                "METHOD:REQUEST",
            ]

            # Add VTIMEZONE component if timezone is supported (not UTC)
            vtimezone_lines = _generate_vtimezone(self.display_timezone)
            lines.extend(vtimezone_lines)

            lines.extend([
                "BEGIN:VEVENT",
                f"UID:{self.uid}",
                f"DTSTAMP:{_fmt_dt_utc(now)}",
                f"DTSTART:{_fmt_dt_utc(self.dtstart_utc)}",
                f"DTEND:{_fmt_dt_utc(self.dtend_utc)}",
                f"SUMMARY:{_escape_text(self.summary)}",
                f"DESCRIPTION:{_escape_text(self.description + (('\\n' + self.url) if self.url else ''))}",
                "STATUS:CONFIRMED",
                "SEQUENCE:0",
            ])

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
