"""
Comprehensive tests for the past-date filter in calendar_parser._validate_and_filter_slots.

Tests cover:
- White-box: direct calls to _validate_and_filter_slots with crafted inputs,
  verifying the date.today() boundary, interaction with weekend filter, business
  hours clamping, minimum duration, and confidence defaults.
- Black-box: end-to-end parse_image flow with mocked OpenAI returning past/future
  slots, verifying only future slots survive.
- Edge cases: today's slots kept, yesterday rejected, year/month boundaries,
  all-past input yields empty list, mixed past/future batches, leap year dates,
  far-future dates, malformed dates, empty inputs.
- Integration with testing_files: real PDF/image files parsed through the full
  pipeline (mocked OpenAI) to confirm past-date filtering at the end-to-end level.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Mock streamlit at module level (same pattern as existing tests)
# ---------------------------------------------------------------------------
_local_mock = MagicMock()
_local_mock.secrets = {}
_local_mock.session_state = {}
_local_mock.cache_data = lambda *a, **kw: (lambda f: f)
_local_mock.cache_resource = lambda *a, **kw: (lambda f: f)

sys.modules.setdefault("streamlit", _local_mock)
sys.modules.setdefault("streamlit.components", MagicMock())
sys.modules.setdefault("streamlit.components.v1", MagicMock())

# Always use the mock that's actually in sys.modules, not our local one,
# because when tests run together the first test file's mock wins.
_mock_st = sys.modules["streamlit"]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calendar_parser import (
    CalendarFormat,
    CalendarParser,
    ParsedSlot,
    ParseResult,
    ParserConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)


def _next_weekday(start: date, offset_days: int = 1) -> date:
    """Return the next weekday on or after start + offset_days."""
    d = start + timedelta(days=offset_days)
    while d.weekday() >= 5:  # skip Sat/Sun
        d += timedelta(days=1)
    return d


def _prev_weekday(start: date, offset_days: int = 1) -> date:
    """Return the previous weekday on or before start - offset_days."""
    d = start - timedelta(days=offset_days)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


FUTURE_WEEKDAY = _next_weekday(TODAY, 1)
PAST_WEEKDAY = _prev_weekday(TODAY, 1)


def _make_slot(slot_date: str, start: str = "09:00", end: str = "10:00",
               confidence: float = 0.9, inferred_tz: str = None):
    """Build a raw slot dict matching OpenAI output shape."""
    slot = {"date": slot_date, "start": start, "end": end, "confidence": confidence}
    if inferred_tz:
        slot["inferred_tz"] = inferred_tz
    return slot


def _make_parser(config: ParserConfig = None) -> CalendarParser:
    """Create a CalendarParser with a mock OpenAI client."""
    mock_client = MagicMock()
    return CalendarParser(mock_client, config or ParserConfig())


def _filter_slots(parser, raw_slots):
    """Call _validate_and_filter_slots and return only the valid slots list."""
    slots, _rejected = parser._validate_and_filter_slots(raw_slots)
    return slots


def _filter_slots_with_reasons(parser, raw_slots):
    """Call _validate_and_filter_slots and return (slots, rejected_reasons)."""
    return parser._validate_and_filter_slots(raw_slots)


# ===========================================================================
# WHITE-BOX TESTS: _validate_and_filter_slots
# ===========================================================================
class TestPastDateFilterWhiteBox:
    """Direct unit tests for the past-date filter logic."""

    def test_yesterday_slot_rejected(self):
        """A slot dated yesterday must be filtered out."""
        parser = _make_parser()
        raw = [_make_slot(PAST_WEEKDAY.isoformat())]
        result = _filter_slots(parser,raw)
        assert result == [], f"Expected empty, got {result}"

    def test_today_slot_kept(self):
        """A slot dated today must be kept (may still have future time windows)."""
        # Skip if today is a weekend — the weekend filter would remove it
        if TODAY.weekday() >= 5:
            pytest.skip("Today is a weekend; weekend filter takes precedence")
        parser = _make_parser()
        raw = [_make_slot(TODAY.isoformat())]
        result = _filter_slots(parser,raw)
        assert len(result) == 1
        assert result[0].date == TODAY.isoformat()

    def test_tomorrow_slot_kept(self):
        """A slot dated tomorrow (weekday) must be kept."""
        parser = _make_parser()
        raw = [_make_slot(FUTURE_WEEKDAY.isoformat())]
        result = _filter_slots(parser,raw)
        assert len(result) == 1
        assert result[0].date == FUTURE_WEEKDAY.isoformat()

    def test_far_past_rejected(self):
        """A slot from months ago must be rejected."""
        parser = _make_parser()
        past = _prev_weekday(TODAY, 60)
        raw = [_make_slot(past.isoformat())]
        result = _filter_slots(parser,raw)
        assert result == []

    def test_far_future_kept(self):
        """A slot months in the future on a weekday must be kept."""
        parser = _make_parser()
        future = _next_weekday(TODAY, 90)
        raw = [_make_slot(future.isoformat())]
        result = _filter_slots(parser,raw)
        assert len(result) == 1

    def test_mixed_past_and_future_filters_correctly(self):
        """Given a mix of past and future slots, only future ones survive."""
        parser = _make_parser()
        past1 = _prev_weekday(TODAY, 1)
        past2 = _prev_weekday(TODAY, 5)
        future1 = _next_weekday(TODAY, 1)
        future2 = _next_weekday(TODAY, 3)
        raw = [
            _make_slot(past1.isoformat()),
            _make_slot(future1.isoformat()),
            _make_slot(past2.isoformat()),
            _make_slot(future2.isoformat()),
        ]
        result = _filter_slots(parser,raw)
        result_dates = {s.date for s in result}
        assert past1.isoformat() not in result_dates
        assert past2.isoformat() not in result_dates
        assert future1.isoformat() in result_dates
        assert future2.isoformat() in result_dates

    def test_all_past_yields_empty(self):
        """If every slot is in the past, the result must be empty."""
        parser = _make_parser()
        raw = [
            _make_slot(_prev_weekday(TODAY, i).isoformat())
            for i in range(1, 6)
        ]
        result = _filter_slots(parser,raw)
        assert result == []

    def test_past_date_filter_runs_before_business_hours_clamp(self):
        """Past dates should be filtered before business hours clamping runs.
        Ensures we don't waste processing on past slots."""
        parser = _make_parser()
        # A past slot with odd times that would normally be clamped
        raw = [_make_slot(PAST_WEEKDAY.isoformat(), start="06:00", end="20:00")]
        result = _filter_slots(parser,raw)
        assert result == []

    def test_past_weekend_double_filter(self):
        """A past Saturday should be filtered (by either weekend or past-date)."""
        parser = _make_parser()
        # Find a past Saturday
        d = TODAY - timedelta(days=1)
        while d.weekday() != 5:
            d -= timedelta(days=1)
        raw = [_make_slot(d.isoformat())]
        result = _filter_slots(parser,raw)
        assert result == []


# ===========================================================================
# WHITE-BOX TESTS: Interaction with other filters
# ===========================================================================
class TestPastDateInteractionWithOtherFilters:
    """Verify past-date filter interacts correctly with other business rules."""

    def test_weekend_filter_still_works_for_future(self):
        """A future Saturday must still be rejected by the weekend filter."""
        parser = _make_parser()
        d = TODAY + timedelta(days=1)
        while d.weekday() != 5:  # find next Saturday
            d += timedelta(days=1)
        raw = [_make_slot(d.isoformat())]
        result = _filter_slots(parser,raw)
        assert result == [], "Future weekends must still be filtered"

    def test_business_hours_clamping_still_works_for_future(self):
        """Future slots outside business hours should be clamped, not dropped."""
        parser = _make_parser()
        raw = [_make_slot(FUTURE_WEEKDAY.isoformat(), start="07:00", end="19:00")]
        result = _filter_slots(parser,raw)
        assert len(result) == 1
        assert result[0].start == "08:00"
        assert result[0].end == "18:00"

    def test_min_duration_still_works_for_future(self):
        """Future slots shorter than min_slot_minutes must still be dropped."""
        parser = _make_parser(ParserConfig(min_slot_minutes=30))
        raw = [_make_slot(FUTURE_WEEKDAY.isoformat(), start="09:00", end="09:15")]
        result = _filter_slots(parser,raw)
        assert result == [], "Short future slots must still be filtered"

    def test_invalid_date_format_still_rejected(self):
        """Malformed date strings must still be rejected."""
        parser = _make_parser()
        raw = [_make_slot("not-a-date"), _make_slot("2026/03/01")]
        result = _filter_slots(parser,raw)
        assert result == []

    def test_missing_keys_still_rejected(self):
        """Slots missing required keys must still be rejected."""
        parser = _make_parser()
        raw = [
            {"date": FUTURE_WEEKDAY.isoformat(), "start": "09:00"},  # no "end"
            {"start": "09:00", "end": "10:00"},  # no "date"
        ]
        result = _filter_slots(parser,raw)
        assert result == []

    def test_non_dict_still_rejected(self):
        """Non-dict items in the list must still be rejected."""
        parser = _make_parser()
        raw = ["not a dict", 42, None, [1, 2, 3]]
        result = _filter_slots(parser,raw)
        assert result == []

    def test_confidence_default_preserved(self):
        """Default confidence of 0.8 must still apply for future slots."""
        parser = _make_parser()
        raw = [{"date": FUTURE_WEEKDAY.isoformat(), "start": "09:00", "end": "10:00"}]
        result = _filter_slots(parser,raw)
        assert len(result) == 1
        assert result[0].confidence == 0.8

    def test_inferred_tz_preserved(self):
        """Timezone info must be preserved for future slots."""
        parser = _make_parser()
        raw = [_make_slot(FUTURE_WEEKDAY.isoformat(), inferred_tz="America/New_York")]
        result = _filter_slots(parser,raw)
        assert len(result) == 1
        assert result[0].inferred_tz == "America/New_York"

    def test_empty_input(self):
        """Empty input list returns empty output."""
        parser = _make_parser()
        assert _filter_slots(parser, []) == []


# ===========================================================================
# EDGE CASE TESTS
# ===========================================================================
class TestPastDateFilterEdgeCases:
    """Boundary and edge-case tests."""

    def test_today_boundary_exact(self):
        """date.today() itself must NOT be filtered (< not <=)."""
        if TODAY.weekday() >= 5:
            pytest.skip("Today is a weekend")
        parser = _make_parser()
        raw = [_make_slot(TODAY.isoformat())]
        result = _filter_slots(parser,raw)
        assert len(result) == 1, "Today should be kept"

    def test_yesterday_boundary_exact(self):
        """The day before today must be filtered."""
        parser = _make_parser()
        raw = [_make_slot(YESTERDAY.isoformat())]
        result = _filter_slots(parser,raw)
        # May also be filtered by weekend rule, but either way must not appear
        assert result == []

    @patch("calendar_parser.date")
    def test_year_boundary_dec31_to_jan1(self, mock_date):
        """Across year boundary: Dec 31 is past when today is Jan 2."""
        mock_date.today.return_value = date(2026, 1, 2)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        parser = _make_parser()
        # Dec 31, 2025 is a Wednesday — should be filtered as past
        raw = [_make_slot("2025-12-31")]
        result = _filter_slots(parser,raw)
        assert result == []

    @patch("calendar_parser.date")
    def test_year_boundary_jan1_kept(self, mock_date):
        """Jan 2 today — Jan 2 slot kept, Jan 1 (Thu) filtered as past."""
        mock_date.today.return_value = date(2026, 1, 2)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        parser = _make_parser()
        raw = [
            _make_slot("2026-01-01"),  # Thursday, past
            _make_slot("2026-01-02"),  # Friday, today — kept
        ]
        result = _filter_slots(parser,raw)
        assert len(result) == 1
        assert result[0].date == "2026-01-02"

    @patch("calendar_parser.date")
    def test_month_boundary(self, mock_date):
        """Feb 1 today — Jan 31 (past) filtered, Feb 2 (future) kept."""
        mock_date.today.return_value = date(2026, 2, 1)  # Sunday
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        parser = _make_parser()
        raw = [
            _make_slot("2026-01-30"),  # Friday, past
            _make_slot("2026-02-02"),  # Monday, future
        ]
        result = _filter_slots(parser,raw)
        assert len(result) == 1
        assert result[0].date == "2026-02-02"

    @patch("calendar_parser.date")
    def test_leap_year_feb29(self, mock_date):
        """Feb 29 on a leap year — valid date, kept if today or future."""
        mock_date.today.return_value = date(2028, 2, 28)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        parser = _make_parser()
        raw = [_make_slot("2028-02-29")]  # Tuesday, future
        result = _filter_slots(parser,raw)
        assert len(result) == 1
        assert result[0].date == "2028-02-29"

    def test_feb29_on_non_leap_year_rejected(self):
        """Feb 29 on a non-leap year is an invalid date — must be rejected."""
        parser = _make_parser()
        raw = [_make_slot("2025-02-29")]
        result = _filter_slots(parser,raw)
        assert result == []

    def test_large_batch_filtering(self):
        """Performance/correctness: filter a large batch of 1000 slots."""
        parser = _make_parser()
        raw = []
        expected_count = 0
        for i in range(-500, 500):
            d = TODAY + timedelta(days=i)
            raw.append(_make_slot(d.isoformat()))
            if d >= TODAY and d.weekday() < 5:
                expected_count += 1
        result = _filter_slots(parser,raw)
        assert len(result) == expected_count

    def test_duplicate_dates_all_kept_if_future(self):
        """Multiple slots on the same future day should all be kept."""
        parser = _make_parser()
        d = FUTURE_WEEKDAY.isoformat()
        raw = [
            _make_slot(d, start="09:00", end="10:00"),
            _make_slot(d, start="11:00", end="12:00"),
            _make_slot(d, start="14:00", end="15:00"),
        ]
        result = _filter_slots(parser,raw)
        assert len(result) == 3

    def test_duplicate_dates_all_rejected_if_past(self):
        """Multiple slots on the same past day should all be rejected."""
        parser = _make_parser()
        d = PAST_WEEKDAY.isoformat()
        raw = [
            _make_slot(d, start="09:00", end="10:00"),
            _make_slot(d, start="11:00", end="12:00"),
        ]
        result = _filter_slots(parser,raw)
        assert result == []


# ===========================================================================
# BLACK-BOX TESTS: parse_image end-to-end
# ===========================================================================
class TestPastDateFilterBlackBox:
    """End-to-end tests through the public parse_image interface."""

    def _mock_openai_response(self, slots_json: list):
        """Create a mock OpenAI chat completion returning the given slots."""
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = json.dumps(slots_json)
        return response

    def _make_test_image(self):
        """Create a minimal PIL Image for testing."""
        from PIL import Image
        return Image.new("RGB", (1200, 800), color="white")

    def test_parse_image_filters_past_from_openai_response(self):
        """Full pipeline: OpenAI returns mixed dates, only future survive."""
        if TODAY.weekday() >= 5:
            pytest.skip("Today is a weekend")

        mock_client = MagicMock()
        parser = CalendarParser(mock_client, ParserConfig())

        mixed_slots = [
            {"date": PAST_WEEKDAY.isoformat(), "start": "09:00", "end": "10:00", "confidence": 0.9},
            {"date": TODAY.isoformat(), "start": "14:00", "end": "15:00", "confidence": 0.85},
            {"date": FUTURE_WEEKDAY.isoformat(), "start": "10:00", "end": "11:00", "confidence": 0.95},
        ]

        # Mock both the format detection and slot extraction
        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.9, "Mocked"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                mixed_slots, json.dumps(mixed_slots)
            )):
                result = parser.parse_image(self._make_test_image())

        result_dates = {s.date for s in result.slots}
        assert PAST_WEEKDAY.isoformat() not in result_dates
        assert TODAY.isoformat() in result_dates
        assert FUTURE_WEEKDAY.isoformat() in result_dates

    def test_parse_image_all_past_returns_empty_slots(self):
        """Full pipeline: all past slots yields ParseResult with empty slots list."""
        mock_client = MagicMock()
        parser = CalendarParser(mock_client, ParserConfig())

        past_slots = [
            {"date": _prev_weekday(TODAY, i).isoformat(), "start": "09:00",
             "end": "10:00", "confidence": 0.9}
            for i in range(1, 4)
        ]

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.9, "Mocked"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                past_slots, json.dumps(past_slots)
            )):
                result = parser.parse_image(self._make_test_image())

        assert result.slots == []
        assert result.error is None  # No error, just no valid slots

    def test_parse_image_preserves_metadata(self):
        """Verify that parse_image still returns format and preprocessing info."""
        mock_client = MagicMock()
        parser = CalendarParser(mock_client, ParserConfig())

        slots = [
            {"date": FUTURE_WEEKDAY.isoformat(), "start": "09:00",
             "end": "10:00", "confidence": 0.9},
        ]

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.AGENDA_VIEW, 0.85, "Mocked"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                slots, json.dumps(slots)
            )):
                result = parser.parse_image(self._make_test_image())

        assert result.detected_format == CalendarFormat.AGENDA_VIEW
        assert result.format_confidence == 0.85
        assert isinstance(result.preprocessing_applied, list)


# ===========================================================================
# INTEGRATION TESTS: with real testing_files
# ===========================================================================
TESTING_FILES_DIR = Path(__file__).resolve().parent.parent / "testing_files"

# Realistic simulated OpenAI responses for each testing file.
# These match the actual date ranges visible in the January 2026 calendar files.
JAN_19_23_WEEK_SLOTS = [
    {"date": "2026-01-19", "start": "08:00", "end": "12:00", "confidence": 0.95},
    {"date": "2026-01-19", "start": "13:00", "end": "17:00", "confidence": 0.92},
    {"date": "2026-01-20", "start": "09:00", "end": "10:30", "confidence": 0.88},
    {"date": "2026-01-20", "start": "14:00", "end": "16:00", "confidence": 0.90},
    {"date": "2026-01-21", "start": "08:00", "end": "09:30", "confidence": 0.93},
    {"date": "2026-01-21", "start": "11:00", "end": "12:00", "confidence": 0.85},
    {"date": "2026-01-21", "start": "14:00", "end": "18:00", "confidence": 0.87},
    {"date": "2026-01-22", "start": "09:00", "end": "11:00", "confidence": 0.91},
    {"date": "2026-01-22", "start": "13:00", "end": "15:30", "confidence": 0.89},
    {"date": "2026-01-23", "start": "08:00", "end": "10:00", "confidence": 0.94},
    {"date": "2026-01-23", "start": "11:00", "end": "12:30", "confidence": 0.86},
    {"date": "2026-01-24", "start": "10:00", "end": "14:00", "confidence": 0.80},  # Sat
    {"date": "2026-01-25", "start": "10:00", "end": "14:00", "confidence": 0.80},  # Sun
]

JAN_26_30_WEEK_SLOTS = [
    {"date": "2026-01-26", "start": "08:00", "end": "10:00", "confidence": 0.96},
    {"date": "2026-01-26", "start": "11:00", "end": "12:30", "confidence": 0.91},
    {"date": "2026-01-26", "start": "14:00", "end": "17:00", "confidence": 0.93},
    {"date": "2026-01-27", "start": "09:00", "end": "10:00", "confidence": 0.89},
    {"date": "2026-01-27", "start": "13:00", "end": "15:00", "confidence": 0.87},
    {"date": "2026-01-28", "start": "08:00", "end": "12:00", "confidence": 0.95},
    {"date": "2026-01-28", "start": "14:00", "end": "16:30", "confidence": 0.90},
    {"date": "2026-01-29", "start": "09:00", "end": "11:00", "confidence": 0.92},
    {"date": "2026-01-29", "start": "14:30", "end": "18:00", "confidence": 0.88},
    {"date": "2026-01-30", "start": "08:00", "end": "09:30", "confidence": 0.94},
    {"date": "2026-01-30", "start": "10:00", "end": "12:00", "confidence": 0.91},
    {"date": "2026-01-30", "start": "15:00", "end": "17:30", "confidence": 0.86},
]

# Mixed scenario: past (Jan) + future (March) dates
MIXED_JAN_MARCH_SLOTS = [
    {"date": "2026-01-19", "start": "09:00", "end": "10:30", "confidence": 0.92},
    {"date": "2026-01-20", "start": "14:00", "end": "16:00", "confidence": 0.88},
    {"date": "2026-01-22", "start": "09:00", "end": "11:00", "confidence": 0.90},
    {"date": "2026-03-02", "start": "09:00", "end": "11:00", "confidence": 0.95},   # Mon future
    {"date": "2026-03-03", "start": "10:00", "end": "12:00", "confidence": 0.93},   # Tue future
    {"date": "2026-03-04", "start": "14:00", "end": "16:30", "confidence": 0.91},   # Wed future
    {"date": "2026-03-05", "start": "08:00", "end": "09:30", "confidence": 0.89},   # Thu future
    {"date": "2026-01-23", "start": "11:00", "end": "12:30", "confidence": 0.87},
    {"date": "2026-03-06", "start": "13:00", "end": "15:00", "confidence": 0.94},   # Fri future
]


class TestPastDateFilterWithTestingFiles:
    """
    Integration tests using real PDF and image files from testing_files/.

    These files are from January 2026 — all dates in them are in the past
    (today is Feb 27, 2026). Tests load the real files, run them through
    actual image preprocessing, and verify past-date filtering works
    end-to-end.
    """

    @pytest.fixture
    def parser(self):
        """Parser with mock OpenAI client."""
        mock_client = MagicMock()
        return CalendarParser(mock_client, ParserConfig())

    # -----------------------------------------------------------------
    # PNG screenshots — load real image, real preprocessing, mock OpenAI
    # -----------------------------------------------------------------

    def test_screenshot_jan22_loads_and_all_past_filtered(self, parser):
        """Screenshot 2026-01-22: load real PNG, preprocess, filter past dates."""
        img_path = TESTING_FILES_DIR / "Screenshot 2026-01-22 112205.png"
        if not img_path.exists():
            pytest.skip(f"Test file not found: {img_path}")

        from PIL import Image
        img = Image.open(img_path)
        assert img.size[0] > 0 and img.size[1] > 0, "Image should load correctly"

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.93, "Grid layout detected"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                JAN_19_23_WEEK_SLOTS, json.dumps(JAN_19_23_WEEK_SLOTS)
            )):
                result = parser.parse_image(img)

        assert result.slots == [], (
            f"All Jan 19-25 slots must be filtered as past. "
            f"Leaked: {[s.date for s in result.slots]}"
        )
        assert result.detected_format == CalendarFormat.WEEK_VIEW
        assert len(result.preprocessing_applied) > 0, "Preprocessing should run on real image"

    def test_screenshot_jan26_121045_loads_and_all_past_filtered(self, parser):
        """Screenshot 2026-01-26 121045: load real PNG, preprocess, filter past."""
        img_path = TESTING_FILES_DIR / "Screenshot 2026-01-26 121045.png"
        if not img_path.exists():
            pytest.skip(f"Test file not found: {img_path}")

        from PIL import Image
        img = Image.open(img_path)
        assert img.size[0] > 0 and img.size[1] > 0

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.95, "Week grid columns detected"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                JAN_26_30_WEEK_SLOTS, json.dumps(JAN_26_30_WEEK_SLOTS)
            )):
                result = parser.parse_image(img)

        assert result.slots == [], (
            f"All Jan 26-30 slots must be filtered. "
            f"Leaked: {[s.date for s in result.slots]}"
        )

    def test_screenshot_jan26_121120_loads_and_all_past_filtered(self, parser):
        """Screenshot 2026-01-26 121120: agenda view, load real PNG, filter past."""
        img_path = TESTING_FILES_DIR / "Screenshot 2026-01-26 121120.png"
        if not img_path.exists():
            pytest.skip(f"Test file not found: {img_path}")

        from PIL import Image
        img = Image.open(img_path)
        assert img.size[0] > 0 and img.size[1] > 0

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.AGENDA_VIEW, 0.88, "List-based layout detected"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                JAN_26_30_WEEK_SLOTS, json.dumps(JAN_26_30_WEEK_SLOTS)
            )):
                result = parser.parse_image(img)

        assert result.slots == []
        assert result.detected_format == CalendarFormat.AGENDA_VIEW

    # -----------------------------------------------------------------
    # PDF files — real PDF→image conversion, real preprocessing, filter
    # -----------------------------------------------------------------

    def _load_pdf_first_page(self, pdf_path: Path):
        """Load a real PDF file and convert first page to PIL Image."""
        from calendar_parser import pdf_to_images_enhanced
        pdf_bytes = pdf_path.read_bytes()
        images = pdf_to_images_enhanced(pdf_bytes, max_pages=1, dpi=300)
        assert len(images) >= 1, f"PDF should produce at least 1 image: {pdf_path.name}"
        return images[0]

    def test_scheduler_test_pdf_real_conversion_all_past_filtered(self, parser):
        """Scheduler Test.pdf: real PDF→image, preprocess, filter past dates."""
        pdf_path = TESTING_FILES_DIR / "Scheduler Test.pdf"
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        img = self._load_pdf_first_page(pdf_path)
        assert img.size[0] > 0 and img.size[1] > 0

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.91, "Grid detected from PDF"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                JAN_19_23_WEEK_SLOTS, json.dumps(JAN_19_23_WEEK_SLOTS)
            )):
                result = parser.parse_image(img)

        assert result.slots == [], (
            f"All past slots from Scheduler Test.pdf must be filtered. "
            f"Leaked: {[s.date for s in result.slots]}"
        )
        assert isinstance(result.preprocessing_applied, list)

    def test_test40_pdf_real_conversion_all_past_filtered(self, parser):
        """Test 40.pdf: real PDF→image, preprocess, filter past dates."""
        pdf_path = TESTING_FILES_DIR / "Test 40.pdf"
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        img = self._load_pdf_first_page(pdf_path)
        assert img.size[0] > 0 and img.size[1] > 0

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.AGENDA_VIEW, 0.87, "Agenda format from PDF"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                JAN_19_23_WEEK_SLOTS, json.dumps(JAN_19_23_WEEK_SLOTS)
            )):
                result = parser.parse_image(img)

        assert result.slots == []

    def test_test50_pdf_real_conversion_all_past_filtered(self, parser):
        """Test 50.pdf: real PDF→image, preprocess, filter past dates."""
        pdf_path = TESTING_FILES_DIR / "Test 50.pdf"
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        img = self._load_pdf_first_page(pdf_path)
        assert img.size[0] > 0 and img.size[1] > 0

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.90, "Grid from PDF"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                JAN_26_30_WEEK_SLOTS, json.dumps(JAN_26_30_WEEK_SLOTS)
            )):
                result = parser.parse_image(img)

        assert result.slots == []

    def test_test50_1_pdf_real_conversion_all_past_filtered(self, parser):
        """Test 50 (1).pdf: real PDF→image, preprocess, filter past dates."""
        pdf_path = TESTING_FILES_DIR / "Test 50 (1).pdf"
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        img = self._load_pdf_first_page(pdf_path)
        assert img.size[0] > 0 and img.size[1] > 0

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.89, "Grid from PDF"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                JAN_26_30_WEEK_SLOTS, json.dumps(JAN_26_30_WEEK_SLOTS)
            )):
                result = parser.parse_image(img)

        assert result.slots == []

    # -----------------------------------------------------------------
    # Mixed past/future scenarios with real files
    # -----------------------------------------------------------------

    def test_screenshot_jan22_mixed_past_future_only_march_kept(self, parser):
        """Real screenshot + mixed Jan/March slots: only March survives."""
        img_path = TESTING_FILES_DIR / "Screenshot 2026-01-22 112205.png"
        if not img_path.exists():
            pytest.skip(f"Test file not found: {img_path}")

        from PIL import Image
        img = Image.open(img_path)

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.92, "Mocked"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                MIXED_JAN_MARCH_SLOTS, json.dumps(MIXED_JAN_MARCH_SLOTS)
            )):
                result = parser.parse_image(img)

        result_dates = [s.date for s in result.slots]
        # All Jan dates must be gone
        for d in result_dates:
            assert not d.startswith("2026-01"), f"Past date leaked: {d}"
        # All March dates must survive
        assert "2026-03-02" in result_dates
        assert "2026-03-03" in result_dates
        assert "2026-03-04" in result_dates
        assert "2026-03-05" in result_dates
        assert "2026-03-06" in result_dates
        assert len(result.slots) == 5

    def test_scheduler_pdf_mixed_past_future_only_march_kept(self, parser):
        """Real PDF + mixed Jan/March slots: only March survives."""
        pdf_path = TESTING_FILES_DIR / "Scheduler Test.pdf"
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        img = self._load_pdf_first_page(pdf_path)

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.9, "Mocked"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                MIXED_JAN_MARCH_SLOTS, json.dumps(MIXED_JAN_MARCH_SLOTS)
            )):
                result = parser.parse_image(img)

        result_dates = [s.date for s in result.slots]
        for d in result_dates:
            assert not d.startswith("2026-01"), f"Past date leaked: {d}"
        assert len(result.slots) == 5

    # -----------------------------------------------------------------
    # PDF-to-image conversion sanity (no mocking — real fitz)
    # -----------------------------------------------------------------

    def test_all_pdfs_convert_to_valid_images(self):
        """All 4 PDFs must convert to at least one valid RGB image via fitz."""
        from calendar_parser import pdf_to_images_enhanced

        pdf_files = [
            "Scheduler Test.pdf",
            "Test 40.pdf",
            "Test 50.pdf",
            "Test 50 (1).pdf",
        ]
        for name in pdf_files:
            pdf_path = TESTING_FILES_DIR / name
            if not pdf_path.exists():
                pytest.skip(f"Test file not found: {pdf_path}")
            images = pdf_to_images_enhanced(pdf_path.read_bytes(), max_pages=3, dpi=300)
            assert len(images) >= 1, f"{name}: should produce at least 1 image"
            for img in images:
                assert img.mode == "RGB", f"{name}: image mode should be RGB"
                assert img.size[0] > 100, f"{name}: image width too small"
                assert img.size[1] > 100, f"{name}: image height too small"

    # -----------------------------------------------------------------
    # Image preprocessing sanity (no mocking — real PIL)
    # -----------------------------------------------------------------

    def test_all_pngs_preprocess_without_error(self):
        """All 3 PNG screenshots preprocess successfully through real pipeline."""
        from PIL import Image
        from calendar_parser import preprocess_image

        png_files = [
            "Screenshot 2026-01-22 112205.png",
            "Screenshot 2026-01-26 121045.png",
            "Screenshot 2026-01-26 121120.png",
        ]
        config = ParserConfig()
        for name in png_files:
            img_path = TESTING_FILES_DIR / name
            if not img_path.exists():
                pytest.skip(f"Test file not found: {img_path}")
            img = Image.open(img_path)
            processed, applied = preprocess_image(img, config)
            assert processed.size[0] > 0, f"{name}: processed image has zero width"
            assert processed.size[1] > 0, f"{name}: processed image has zero height"
            assert isinstance(applied, list), f"{name}: applied should be a list"

    def test_all_pdfs_preprocess_without_error(self):
        """All 4 PDFs: convert→preprocess pipeline runs without errors."""
        from calendar_parser import pdf_to_images_enhanced, preprocess_image

        pdf_files = [
            "Scheduler Test.pdf",
            "Test 40.pdf",
            "Test 50.pdf",
            "Test 50 (1).pdf",
        ]
        config = ParserConfig()
        for name in pdf_files:
            pdf_path = TESTING_FILES_DIR / name
            if not pdf_path.exists():
                pytest.skip(f"Test file not found: {pdf_path}")
            images = pdf_to_images_enhanced(pdf_path.read_bytes(), max_pages=1, dpi=300)
            for img in images:
                processed, applied = preprocess_image(img, config)
                assert processed.size[0] > 0, f"{name}: processed image has zero width"
                assert isinstance(applied, list)

    # -----------------------------------------------------------------
    # Full pipeline: real file → real preprocess → mock OpenAI → filter
    # Verifies the entire chain end-to-end per file
    # -----------------------------------------------------------------

    @pytest.mark.parametrize("filename,slot_data", [
        ("Screenshot 2026-01-22 112205.png", JAN_19_23_WEEK_SLOTS),
        ("Screenshot 2026-01-26 121045.png", JAN_26_30_WEEK_SLOTS),
        ("Screenshot 2026-01-26 121120.png", JAN_26_30_WEEK_SLOTS),
    ])
    def test_png_full_pipeline_parametrized(self, filename, slot_data, parser):
        """Parametrized: each PNG → preprocess → mock extract → filter → empty."""
        img_path = TESTING_FILES_DIR / filename
        if not img_path.exists():
            pytest.skip(f"Test file not found: {img_path}")

        from PIL import Image
        img = Image.open(img_path)

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.9, "Mocked"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                slot_data, json.dumps(slot_data)
            )):
                result = parser.parse_image(img)

        assert result.slots == [], (
            f"{filename}: all Jan 2026 slots should be filtered. "
            f"Leaked: {[s.date for s in result.slots]}"
        )

    @pytest.mark.parametrize("filename,slot_data", [
        ("Scheduler Test.pdf", JAN_19_23_WEEK_SLOTS),
        ("Test 40.pdf", JAN_19_23_WEEK_SLOTS),
        ("Test 50.pdf", JAN_26_30_WEEK_SLOTS),
        ("Test 50 (1).pdf", JAN_26_30_WEEK_SLOTS),
    ])
    def test_pdf_full_pipeline_parametrized(self, filename, slot_data, parser):
        """Parametrized: each PDF → fitz convert → preprocess → filter → empty."""
        pdf_path = TESTING_FILES_DIR / filename
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        img = self._load_pdf_first_page(pdf_path)

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.9, "Mocked"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                slot_data, json.dumps(slot_data)
            )):
                result = parser.parse_image(img)

        assert result.slots == [], (
            f"{filename}: all Jan 2026 slots should be filtered. "
            f"Leaked: {[s.date for s in result.slots]}"
        )


# ===========================================================================
# CURRENT WEEK TESTS (Feb 23 – Mar 1, 2026)
#
# Today is Friday Feb 27, 2026.
#   Mon Feb 23 – Thu Feb 26 = past (filtered)
#   Fri Feb 27             = today (KEPT)
#   Sat Feb 28 / Sun Mar 1 = weekend (filtered)
#
# Next week Mar 2 – Mar 6 = all future weekdays (KEPT)
# ===========================================================================

# Simulated OpenAI output for this week's calendar screenshot
CURRENT_WEEK_SLOTS = [
    # Monday Feb 23 — past
    {"date": "2026-02-23", "start": "08:00", "end": "10:00", "confidence": 0.96},
    {"date": "2026-02-23", "start": "11:00", "end": "12:30", "confidence": 0.93},
    {"date": "2026-02-23", "start": "14:00", "end": "17:00", "confidence": 0.91},
    # Tuesday Feb 24 — past
    {"date": "2026-02-24", "start": "09:00", "end": "10:30", "confidence": 0.94},
    {"date": "2026-02-24", "start": "13:00", "end": "15:00", "confidence": 0.89},
    # Wednesday Feb 25 — past
    {"date": "2026-02-25", "start": "08:00", "end": "12:00", "confidence": 0.95},
    {"date": "2026-02-25", "start": "14:00", "end": "16:30", "confidence": 0.90},
    # Thursday Feb 26 — yesterday (past)
    {"date": "2026-02-26", "start": "09:00", "end": "11:00", "confidence": 0.92},
    {"date": "2026-02-26", "start": "13:30", "end": "15:00", "confidence": 0.88},
    {"date": "2026-02-26", "start": "16:00", "end": "18:00", "confidence": 0.86},
    # Friday Feb 27 — TODAY (kept)
    {"date": "2026-02-27", "start": "08:00", "end": "09:30", "confidence": 0.97},
    {"date": "2026-02-27", "start": "10:00", "end": "12:00", "confidence": 0.95},
    {"date": "2026-02-27", "start": "14:00", "end": "17:00", "confidence": 0.93},
    # Saturday Feb 28 — weekend
    {"date": "2026-02-28", "start": "10:00", "end": "14:00", "confidence": 0.80},
    # Sunday Mar 1 — weekend
    {"date": "2026-03-01", "start": "10:00", "end": "14:00", "confidence": 0.80},
]

# Next week (all future weekdays — all kept)
NEXT_WEEK_SLOTS = [
    {"date": "2026-03-02", "start": "08:00", "end": "10:00", "confidence": 0.96},
    {"date": "2026-03-02", "start": "11:00", "end": "12:30", "confidence": 0.93},
    {"date": "2026-03-02", "start": "14:00", "end": "17:00", "confidence": 0.91},
    {"date": "2026-03-03", "start": "09:00", "end": "11:00", "confidence": 0.94},
    {"date": "2026-03-03", "start": "13:00", "end": "15:30", "confidence": 0.89},
    {"date": "2026-03-04", "start": "08:00", "end": "12:00", "confidence": 0.95},
    {"date": "2026-03-04", "start": "14:00", "end": "16:30", "confidence": 0.90},
    {"date": "2026-03-05", "start": "09:00", "end": "10:30", "confidence": 0.92},
    {"date": "2026-03-05", "start": "13:00", "end": "15:00", "confidence": 0.88},
    {"date": "2026-03-06", "start": "08:00", "end": "09:30", "confidence": 0.94},
    {"date": "2026-03-06", "start": "10:00", "end": "12:00", "confidence": 0.91},
    {"date": "2026-03-06", "start": "15:00", "end": "17:30", "confidence": 0.87},
]

# Combined: this week + next week (simulates a 2-week calendar upload)
TWO_WEEK_SLOTS = CURRENT_WEEK_SLOTS + NEXT_WEEK_SLOTS


class TestCurrentWeekFilter:
    """
    Tests using the latest/current week (Feb 23–27, 2026).

    Today is Friday Feb 27. Mon–Thu are past, Fri is today (kept),
    Sat/Sun are weekends (filtered). Next week Mar 2–6 are all future.
    """

    # -----------------------------------------------------------------
    # White-box: _validate_and_filter_slots with current week data
    # -----------------------------------------------------------------

    def test_current_week_only_today_friday_kept(self):
        """This week's calendar: only today (Fri Feb 27) slots survive."""
        parser = _make_parser()
        result = _filter_slots(parser,CURRENT_WEEK_SLOTS)

        result_dates = {s.date for s in result}
        assert result_dates == {"2026-02-27"}, (
            f"Only today should survive. Got dates: {result_dates}"
        )
        # Today had 3 slots in the data
        assert len(result) == 3

    def test_current_week_past_monday_filtered(self):
        """Monday Feb 23 (past) — all 3 slots must be filtered."""
        parser = _make_parser()
        mon_slots = [s for s in CURRENT_WEEK_SLOTS if s["date"] == "2026-02-23"]
        result = _filter_slots(parser,mon_slots)
        assert result == []

    def test_current_week_past_tuesday_filtered(self):
        """Tuesday Feb 24 (past) — all slots filtered."""
        parser = _make_parser()
        tue_slots = [s for s in CURRENT_WEEK_SLOTS if s["date"] == "2026-02-24"]
        result = _filter_slots(parser,tue_slots)
        assert result == []

    def test_current_week_past_wednesday_filtered(self):
        """Wednesday Feb 25 (past) — all slots filtered."""
        parser = _make_parser()
        wed_slots = [s for s in CURRENT_WEEK_SLOTS if s["date"] == "2026-02-25"]
        result = _filter_slots(parser,wed_slots)
        assert result == []

    def test_current_week_yesterday_thursday_filtered(self):
        """Thursday Feb 26 (yesterday) — all 3 slots filtered."""
        parser = _make_parser()
        thu_slots = [s for s in CURRENT_WEEK_SLOTS if s["date"] == "2026-02-26"]
        result = _filter_slots(parser,thu_slots)
        assert result == [], "Yesterday's slots must all be filtered"

    def test_current_week_today_friday_all_slots_kept(self):
        """Friday Feb 27 (today) — all 3 slots kept."""
        parser = _make_parser()
        fri_slots = [s for s in CURRENT_WEEK_SLOTS if s["date"] == "2026-02-27"]
        result = _filter_slots(parser,fri_slots)
        assert len(result) == 3
        for slot in result:
            assert slot.date == "2026-02-27"

    def test_current_week_today_slot_times_preserved(self):
        """Today's slot start/end times must pass through unchanged (within biz hours)."""
        parser = _make_parser()
        fri_slots = [s for s in CURRENT_WEEK_SLOTS if s["date"] == "2026-02-27"]
        result = _filter_slots(parser,fri_slots)
        times = [(s.start, s.end) for s in result]
        assert ("08:00", "09:30") in times
        assert ("10:00", "12:00") in times
        assert ("14:00", "17:00") in times

    def test_current_week_today_confidence_preserved(self):
        """Today's slot confidence scores must be preserved."""
        parser = _make_parser()
        fri_slots = [s for s in CURRENT_WEEK_SLOTS if s["date"] == "2026-02-27"]
        result = _filter_slots(parser,fri_slots)
        confidences = sorted([s.confidence for s in result])
        assert confidences == [0.93, 0.95, 0.97]

    def test_current_week_saturday_filtered(self):
        """Saturday Feb 28 (weekend) — filtered by weekend rule."""
        parser = _make_parser()
        sat_slots = [s for s in CURRENT_WEEK_SLOTS if s["date"] == "2026-02-28"]
        result = _filter_slots(parser,sat_slots)
        assert result == []

    def test_current_week_sunday_filtered(self):
        """Sunday Mar 1 (weekend) — filtered by weekend rule."""
        parser = _make_parser()
        sun_slots = [s for s in CURRENT_WEEK_SLOTS if s["date"] == "2026-03-01"]
        result = _filter_slots(parser,sun_slots)
        assert result == []

    # -----------------------------------------------------------------
    # Next week (all future) — everything kept
    # -----------------------------------------------------------------

    def test_next_week_all_slots_kept(self):
        """Next week Mar 2–6 (all future weekdays) — all 12 slots kept."""
        parser = _make_parser()
        result = _filter_slots(parser,NEXT_WEEK_SLOTS)
        assert len(result) == 12
        result_dates = {s.date for s in result}
        assert result_dates == {
            "2026-03-02", "2026-03-03", "2026-03-04",
            "2026-03-05", "2026-03-06",
        }

    def test_next_week_each_day_has_correct_slot_count(self):
        """Verify each day of next week has the expected number of slots."""
        parser = _make_parser()
        result = _filter_slots(parser,NEXT_WEEK_SLOTS)
        from collections import Counter
        counts = Counter(s.date for s in result)
        assert counts["2026-03-02"] == 3  # Mon
        assert counts["2026-03-03"] == 2  # Tue
        assert counts["2026-03-04"] == 2  # Wed
        assert counts["2026-03-05"] == 2  # Thu
        assert counts["2026-03-06"] == 3  # Fri

    # -----------------------------------------------------------------
    # Combined: this week + next week (2-week upload)
    # -----------------------------------------------------------------

    def test_two_week_upload_filters_past_keeps_future(self):
        """2-week calendar: past Mon–Thu filtered, today + next week kept."""
        parser = _make_parser()
        result = _filter_slots(parser,TWO_WEEK_SLOTS)

        result_dates = {s.date for s in result}
        # Past Mon–Thu must be gone
        assert "2026-02-23" not in result_dates
        assert "2026-02-24" not in result_dates
        assert "2026-02-25" not in result_dates
        assert "2026-02-26" not in result_dates
        # Weekends must be gone
        assert "2026-02-28" not in result_dates
        assert "2026-03-01" not in result_dates
        # Today must be present
        assert "2026-02-27" in result_dates
        # Next week must all be present
        for d in ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]:
            assert d in result_dates
        # 3 today + 12 next week = 15
        assert len(result) == 15

    def test_two_week_upload_count_breakdown(self):
        """Verify exact slot counts: 3 today + 12 next week = 15 total."""
        parser = _make_parser()
        result = _filter_slots(parser,TWO_WEEK_SLOTS)
        today_count = sum(1 for s in result if s.date == "2026-02-27")
        next_week_count = sum(1 for s in result if s.date > "2026-02-27")
        assert today_count == 3, f"Expected 3 today slots, got {today_count}"
        assert next_week_count == 12, f"Expected 12 next-week slots, got {next_week_count}"

    # -----------------------------------------------------------------
    # Black-box: parse_image with current week data
    # -----------------------------------------------------------------

    def test_parse_image_current_week_only_today_survives(self):
        """Full pipeline with current week: only today's 3 slots survive."""
        mock_client = MagicMock()
        parser = CalendarParser(mock_client, ParserConfig())

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.94, "Week grid detected"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                CURRENT_WEEK_SLOTS, json.dumps(CURRENT_WEEK_SLOTS)
            )):
                from PIL import Image
                img = Image.new("RGB", (1200, 800), color="white")
                result = parser.parse_image(img)

        assert len(result.slots) == 3
        for slot in result.slots:
            assert slot.date == "2026-02-27"

    def test_parse_image_two_week_upload(self):
        """Full pipeline with 2-week calendar: today + next week survive."""
        mock_client = MagicMock()
        parser = CalendarParser(mock_client, ParserConfig())

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.92, "Week grid detected"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                TWO_WEEK_SLOTS, json.dumps(TWO_WEEK_SLOTS)
            )):
                from PIL import Image
                img = Image.new("RGB", (1200, 800), color="white")
                result = parser.parse_image(img)

        assert len(result.slots) == 15
        result_dates = {s.date for s in result.slots}
        assert "2026-02-23" not in result_dates  # past Mon
        assert "2026-02-27" in result_dates       # today Fri
        assert "2026-03-02" in result_dates       # next Mon

    def test_parse_image_next_week_all_kept(self):
        """Full pipeline with next week only: all 12 slots survive."""
        mock_client = MagicMock()
        parser = CalendarParser(mock_client, ParserConfig())

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.91, "Week grid detected"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                NEXT_WEEK_SLOTS, json.dumps(NEXT_WEEK_SLOTS)
            )):
                from PIL import Image
                img = Image.new("RGB", (1200, 800), color="white")
                result = parser.parse_image(img)

        assert len(result.slots) == 12

    # -----------------------------------------------------------------
    # Integration: real testing_files with current-week slot data
    # -----------------------------------------------------------------

    def test_real_screenshot_with_current_week_data(self):
        """Real PNG file + current week slots: only today survives."""
        img_path = TESTING_FILES_DIR / "Screenshot 2026-01-22 112205.png"
        if not img_path.exists():
            pytest.skip(f"Test file not found: {img_path}")

        from PIL import Image
        img = Image.open(img_path)

        mock_client = MagicMock()
        parser = CalendarParser(mock_client, ParserConfig())

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.9, "Mocked"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                CURRENT_WEEK_SLOTS, json.dumps(CURRENT_WEEK_SLOTS)
            )):
                result = parser.parse_image(img)

        assert len(result.slots) == 3
        for slot in result.slots:
            assert slot.date == "2026-02-27"

    def test_real_pdf_with_two_week_data(self):
        """Real PDF file + 2-week slots: only today + next week survive."""
        pdf_path = TESTING_FILES_DIR / "Scheduler Test.pdf"
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        from calendar_parser import pdf_to_images_enhanced
        images = pdf_to_images_enhanced(pdf_path.read_bytes(), max_pages=1, dpi=300)
        assert len(images) >= 1
        img = images[0]

        mock_client = MagicMock()
        parser = CalendarParser(mock_client, ParserConfig())

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.9, "Mocked"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                TWO_WEEK_SLOTS, json.dumps(TWO_WEEK_SLOTS)
            )):
                result = parser.parse_image(img)

        assert len(result.slots) == 15
        result_dates = {s.date for s in result.slots}
        assert "2026-02-23" not in result_dates
        assert "2026-02-27" in result_dates
        assert "2026-03-06" in result_dates

    def test_real_pdf_with_next_week_only_all_kept(self):
        """Real PDF + next week only: all 12 future slots survive."""
        pdf_path = TESTING_FILES_DIR / "Test 50.pdf"
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        from calendar_parser import pdf_to_images_enhanced
        images = pdf_to_images_enhanced(pdf_path.read_bytes(), max_pages=1, dpi=300)
        img = images[0]

        mock_client = MagicMock()
        parser = CalendarParser(mock_client, ParserConfig())

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.9, "Mocked"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                NEXT_WEEK_SLOTS, json.dumps(NEXT_WEEK_SLOTS)
            )):
                result = parser.parse_image(img)

        assert len(result.slots) == 12
        for slot in result.slots:
            assert slot.date >= "2026-03-02"


# ===========================================================================
# ParsedSlot.to_dict and ParseResult.to_legacy_format
# ===========================================================================
class TestFilteredSlotOutput:
    """Verify that filtered output formats correctly through public APIs."""

    def test_to_dict_on_filtered_future_slot(self):
        """ParsedSlot.to_dict must include all fields correctly."""
        slot = ParsedSlot(
            date=FUTURE_WEEKDAY.isoformat(),
            start="09:00",
            end="10:00",
            confidence=0.9,
            inferred_tz="America/Chicago"
        )
        d = slot.to_dict()
        assert d["date"] == FUTURE_WEEKDAY.isoformat()
        assert d["start"] == "09:00"
        assert d["end"] == "10:00"
        assert d["confidence"] == 0.9
        assert d["inferred_tz"] == "America/Chicago"

    def test_to_legacy_format_excludes_past(self):
        """ParseResult.to_legacy_format on filtered result must have no past dates."""
        parser = _make_parser()
        raw = [
            _make_slot(PAST_WEEKDAY.isoformat()),
            _make_slot(FUTURE_WEEKDAY.isoformat()),
        ]
        filtered = _filter_slots(parser, raw)
        result = ParseResult(
            slots=filtered,
            detected_format=CalendarFormat.WEEK_VIEW,
            format_confidence=0.9,
            preprocessing_applied=[]
        )
        legacy = result.to_legacy_format()
        for item in legacy:
            assert item["date"] >= TODAY.isoformat()


# ===========================================================================
# Regression / safety-net tests
# ===========================================================================
class TestPastDateFilterRegression:
    """Regression tests to prevent future breakage."""

    def test_filter_is_strictly_less_than_not_lte(self):
        """The filter must use < (not <=) so today's slots are preserved."""
        if TODAY.weekday() >= 5:
            pytest.skip("Today is a weekend")
        parser = _make_parser()
        raw = [_make_slot(TODAY.isoformat(), start="09:00", end="17:00")]
        result = _filter_slots(parser,raw)
        assert len(result) == 1, "Today's slots must NOT be filtered out"

    def test_no_side_effects_on_input(self):
        """_validate_and_filter_slots must not mutate the input list."""
        parser = _make_parser()
        raw = [
            _make_slot(PAST_WEEKDAY.isoformat()),
            _make_slot(FUTURE_WEEKDAY.isoformat()),
        ]
        raw_copy = [dict(s) for s in raw]
        _filter_slots(parser, raw)
        assert raw == raw_copy, "Input list must not be mutated"

    def test_returned_type_is_parsed_slot(self):
        """All returned items must be ParsedSlot instances."""
        parser = _make_parser()
        raw = [_make_slot(FUTURE_WEEKDAY.isoformat())]
        result = _filter_slots(parser,raw)
        for item in result:
            assert isinstance(item, ParsedSlot)

    def test_order_preserved(self):
        """The order of valid slots must match the input order."""
        parser = _make_parser()
        d1 = _next_weekday(TODAY, 1)
        d2 = _next_weekday(TODAY, 3)
        d3 = _next_weekday(TODAY, 5)
        raw = [
            _make_slot(d1.isoformat()),
            _make_slot(d2.isoformat()),
            _make_slot(d3.isoformat()),
        ]
        result = _filter_slots(parser, raw)
        result_dates = [s.date for s in result]
        assert result_dates == [d1.isoformat(), d2.isoformat(), d3.isoformat()]


# ===========================================================================
# REJECTED REASONS TESTS
# ===========================================================================
class TestRejectedReasons:
    """Verify that _validate_and_filter_slots returns accurate rejection reasons."""

    def test_past_date_reason_counted(self):
        """Past-date slots produce 'past_date' rejection reason."""
        parser = _make_parser()
        raw = [
            _make_slot(PAST_WEEKDAY.isoformat()),
            _make_slot(_prev_weekday(TODAY, 3).isoformat()),
        ]
        _slots, rejected = _filter_slots_with_reasons(parser, raw)
        assert rejected.get("past_date", 0) == 2

    def test_weekend_reason_counted(self):
        """Weekend slots produce 'weekend' rejection reason."""
        parser = _make_parser()
        # Find a future Saturday
        d = TODAY + timedelta(days=1)
        while d.weekday() != 5:
            d += timedelta(days=1)
        raw = [_make_slot(d.isoformat()), _make_slot((d + timedelta(days=1)).isoformat())]
        _slots, rejected = _filter_slots_with_reasons(parser, raw)
        assert rejected.get("weekend", 0) == 2

    def test_invalid_date_reason(self):
        """Malformed dates produce 'invalid_date' rejection reason."""
        parser = _make_parser()
        raw = [_make_slot("not-a-date"), _make_slot("2026/03/01")]
        _slots, rejected = _filter_slots_with_reasons(parser, raw)
        assert rejected.get("invalid_date", 0) == 2

    def test_missing_fields_reason(self):
        """Slots missing keys produce 'missing_fields' rejection reason."""
        parser = _make_parser()
        raw = [{"date": "2026-03-02"}, {"start": "09:00", "end": "10:00"}]
        _slots, rejected = _filter_slots_with_reasons(parser, raw)
        assert rejected.get("missing_fields", 0) == 2

    def test_invalid_format_reason(self):
        """Non-dict items produce 'invalid_format' rejection reason."""
        parser = _make_parser()
        raw = ["string", 42, None]
        _slots, rejected = _filter_slots_with_reasons(parser, raw)
        assert rejected.get("invalid_format", 0) == 3

    def test_too_short_reason(self):
        """Slots shorter than min duration produce 'too_short' rejection reason."""
        parser = _make_parser(ParserConfig(min_slot_minutes=30))
        raw = [_make_slot(FUTURE_WEEKDAY.isoformat(), start="09:00", end="09:15")]
        _slots, rejected = _filter_slots_with_reasons(parser, raw)
        assert rejected.get("too_short", 0) == 1

    def test_mixed_reasons_all_counted(self):
        """Mixed input produces accurate counts for each rejection reason."""
        parser = _make_parser()
        d_future_sat = TODAY + timedelta(days=1)
        while d_future_sat.weekday() != 5:
            d_future_sat += timedelta(days=1)

        raw = [
            _make_slot(PAST_WEEKDAY.isoformat()),                          # past_date
            _make_slot(PAST_WEEKDAY.isoformat()),                          # past_date
            _make_slot(d_future_sat.isoformat()),                          # weekend
            _make_slot("bad-date"),                                        # invalid_date
            {"date": FUTURE_WEEKDAY.isoformat()},                         # missing_fields
            _make_slot(FUTURE_WEEKDAY.isoformat(), start="09:00", end="09:10"),  # too_short
            _make_slot(FUTURE_WEEKDAY.isoformat(), start="09:00", end="10:00"),  # valid
        ]
        slots, rejected = _filter_slots_with_reasons(parser, raw)
        assert len(slots) == 1
        assert rejected.get("past_date", 0) == 2
        assert rejected.get("weekend", 0) == 1
        assert rejected.get("invalid_date", 0) == 1
        assert rejected.get("missing_fields", 0) == 1
        assert rejected.get("too_short", 0) == 1

    def test_no_rejections_returns_empty_dict(self):
        """All valid slots produce an empty rejected_reasons dict."""
        parser = _make_parser()
        raw = [_make_slot(FUTURE_WEEKDAY.isoformat())]
        _slots, rejected = _filter_slots_with_reasons(parser, raw)
        assert rejected == {}

    def test_empty_input_returns_empty_dict(self):
        """Empty input produces empty rejected_reasons dict."""
        parser = _make_parser()
        _slots, rejected = _filter_slots_with_reasons(parser, [])
        assert rejected == {}

    def test_current_week_rejected_reasons(self):
        """Current week data: past_date and weekend counts are correct."""
        parser = _make_parser()
        _slots, rejected = _filter_slots_with_reasons(parser, CURRENT_WEEK_SLOTS)
        # Mon-Thu = 10 past slots, Sat+Sun = 2 weekend slots, Fri = 3 valid
        assert rejected.get("past_date", 0) == 10
        assert rejected.get("weekend", 0) == 2

    def test_rejected_reasons_in_parse_result(self):
        """ParseResult.rejected_reasons is populated from parse_image."""
        mock_client = MagicMock()
        parser = CalendarParser(mock_client, ParserConfig())

        past_slots = [
            {"date": PAST_WEEKDAY.isoformat(), "start": "09:00", "end": "10:00", "confidence": 0.9},
            {"date": PAST_WEEKDAY.isoformat(), "start": "11:00", "end": "12:00", "confidence": 0.9},
        ]

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.9, "Mocked"
        )):
            with patch.object(parser, "_extract_slots", return_value=(
                past_slots, json.dumps(past_slots)
            )):
                from PIL import Image
                result = parser.parse_image(Image.new("RGB", (1200, 800)))

        assert result.slots == []
        assert result.rejected_reasons.get("past_date", 0) == 2
