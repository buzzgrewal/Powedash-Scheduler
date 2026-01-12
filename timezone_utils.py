"""
Timezone helpers with validation.
Store everything internally as UTC ISO8601, display in a selected TZ.

Uses standard library zoneinfo on Python 3.11+.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Known valid timezones (fast lookup, matches _common_timezones() in app.py)
_COMMON_TIMEZONES = frozenset([
    "UTC", "Europe/London", "Europe/Dublin", "Europe/Paris", "Europe/Rome",
    "Europe/Berlin", "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Toronto", "America/Sao_Paulo",
    "Asia/Dubai", "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney",
])


def is_valid_timezone(tz_name: str) -> bool:
    """
    Check if timezone name is valid without throwing.
    Returns False for None, empty string, or invalid timezone names.
    """
    if not tz_name or not isinstance(tz_name, str):
        return False
    # Fast path for common timezones
    if tz_name in _COMMON_TIMEZONES:
        return True
    # Try to construct it for less common timezones
    try:
        ZoneInfo(tz_name)
        return True
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return False


def safe_zoneinfo(tz_name: str, fallback: str = "UTC") -> Tuple[ZoneInfo, bool]:
    """
    Get ZoneInfo safely with fallback.
    Returns (ZoneInfo, was_valid) tuple.

    If tz_name is invalid, returns (ZoneInfo(fallback), False).
    """
    if is_valid_timezone(tz_name):
        return ZoneInfo(tz_name), True
    return ZoneInfo(fallback), False


def to_utc(dt_local: datetime) -> datetime:
    """
    Convert timezone-aware datetime to UTC.
    Raises ValueError if datetime is naive (no timezone info).
    """
    if dt_local.tzinfo is None:
        raise ValueError("dt_local must be timezone-aware")
    return dt_local.astimezone(timezone.utc)


def from_utc(dt_utc: datetime, tz_name: str) -> datetime:
    """
    Convert UTC datetime to local timezone.
    Raises ValueError if timezone is invalid.
    """
    if not is_valid_timezone(tz_name):
        raise ValueError(f"Invalid timezone: {tz_name}")
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(ZoneInfo(tz_name))


def iso_utc(dt_utc: datetime) -> str:
    """
    Format datetime as ISO8601 UTC string.
    Assumes naive datetime is UTC (for backwards compatibility).
    """
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(dt_str: str) -> datetime:
    """
    Parse ISO datetime string.
    Raises ValueError if format is invalid.
    """
    # Handle 'Z' suffix (ISO standard for UTC) which fromisoformat doesn't support
    if dt_str.endswith('Z'):
        dt_str = dt_str[:-1] + '+00:00'
    return datetime.fromisoformat(dt_str)
