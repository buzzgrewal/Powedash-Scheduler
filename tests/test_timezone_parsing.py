"""Tests for timezone handling in calendar parsing and slot normalization.

Validates that:
1. The parser prompt never asks OpenAI to convert timezones
2. normalize_slots_to_utc uses the correct source timezone
3. End-to-end: slots parsed in one TZ and displayed in another produce correct UTC times
4. Integration: real testing_files parsed with non-UTC timezones produce consistent results
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
from PIL import Image

from calendar_parser import CalendarFormat, CalendarParser, ParserConfig, preprocess_image
from slot_intersection import normalize_slots_to_utc, merge_adjacent_slots, compute_intersection
from timezone_utils import from_utc, to_utc, ZoneInfo


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

TESTING_FILES_DIR = Path(__file__).resolve().parent.parent / "testing_files"

# Future-dated slots that won't be filtered by past-date validation.
# Simulates what OpenAI would return from a week-view calendar.
FUTURE_WEEK_SLOTS = [
    {"date": "2026-03-09", "start": "08:00", "end": "10:00", "confidence": 0.96},
    {"date": "2026-03-09", "start": "11:00", "end": "12:30", "confidence": 0.91},
    {"date": "2026-03-09", "start": "14:00", "end": "17:00", "confidence": 0.93},
    {"date": "2026-03-10", "start": "09:00", "end": "10:30", "confidence": 0.89},
    {"date": "2026-03-10", "start": "13:00", "end": "15:00", "confidence": 0.87},
    {"date": "2026-03-11", "start": "08:00", "end": "12:00", "confidence": 0.95},
    {"date": "2026-03-11", "start": "14:00", "end": "16:30", "confidence": 0.90},
    {"date": "2026-03-12", "start": "09:00", "end": "11:00", "confidence": 0.92},
    {"date": "2026-03-12", "start": "14:30", "end": "18:00", "confidence": 0.88},
    {"date": "2026-03-13", "start": "08:00", "end": "09:30", "confidence": 0.94},
    {"date": "2026-03-13", "start": "10:00", "end": "12:00", "confidence": 0.91},
    {"date": "2026-03-13", "start": "15:00", "end": "17:30", "confidence": 0.86},
]

FUTURE_AGENDA_SLOTS = [
    {"date": "2026-03-09", "start": "09:00", "end": "11:00", "confidence": 0.95},
    {"date": "2026-03-10", "start": "10:00", "end": "12:00", "confidence": 0.93},
    {"date": "2026-03-11", "start": "14:00", "end": "16:30", "confidence": 0.91},
    {"date": "2026-03-12", "start": "08:00", "end": "09:30", "confidence": 0.89},
    {"date": "2026-03-13", "start": "13:00", "end": "15:00", "confidence": 0.94},
]

# Timezones to test (non-UTC)
NON_UTC_TIMEZONES = [
    "America/New_York",
    "America/Los_Angeles",
    "Europe/London",
    "Asia/Tokyo",
    "Australia/Sydney",
    "America/Chicago",
    "Europe/Berlin",
    "Asia/Kolkata",
]


# ===========================================================================
# UNIT TESTS: prompt construction
# ===========================================================================

class TestParserPromptNoConversion:
    """Verify the extraction prompt never asks the model to convert timezones."""

    def setup_method(self):
        self.parser = CalendarParser(openai_client=None, config=ParserConfig())

    def test_same_tz_no_conversion_instruction(self):
        prompt = self.parser._build_extraction_prompt(
            CalendarFormat.WEEK_VIEW,
            interviewer_tz="America/New_York",
            display_tz="America/New_York",
        )
        assert "Do NOT convert" in prompt
        assert "Convert all times to" not in prompt

    def test_different_tz_no_conversion_instruction(self):
        """The critical case: different TZs must NOT produce a conversion instruction."""
        prompt = self.parser._build_extraction_prompt(
            CalendarFormat.WEEK_VIEW,
            interviewer_tz="America/New_York",
            display_tz="Europe/London",
        )
        assert "Do NOT convert" in prompt
        assert "Convert all times to" not in prompt
        assert "America/New_York" in prompt
        # display_tz should NOT appear in the prompt at all
        assert "Europe/London" not in prompt

    def test_utc_interviewer_non_utc_display(self):
        prompt = self.parser._build_extraction_prompt(
            CalendarFormat.AGENDA_VIEW,
            interviewer_tz="UTC",
            display_tz="Asia/Tokyo",
        )
        assert "Do NOT convert" in prompt
        assert "Convert all times to" not in prompt
        assert "Asia/Tokyo" not in prompt

    def test_no_interviewer_tz(self):
        """When no interviewer TZ is set, no TZ instruction at all."""
        prompt = self.parser._build_extraction_prompt(
            CalendarFormat.WEEK_VIEW,
            interviewer_tz=None,
            display_tz="America/Chicago",
        )
        assert "TIMEZONE" not in prompt
        assert "Convert" not in prompt

    @pytest.mark.parametrize("interviewer_tz,display_tz", [
        ("America/New_York", "Europe/London"),
        ("Asia/Tokyo", "America/Los_Angeles"),
        ("UTC", "Australia/Sydney"),
        ("Europe/Berlin", "America/Chicago"),
        ("Asia/Kolkata", "UTC"),
    ])
    def test_no_conversion_for_any_tz_pair(self, interviewer_tz, display_tz):
        """Parametrized: no conversion instruction for any timezone pair."""
        for fmt in [CalendarFormat.WEEK_VIEW, CalendarFormat.AGENDA_VIEW]:
            prompt = self.parser._build_extraction_prompt(fmt, interviewer_tz, display_tz)
            assert "Convert all times to" not in prompt, (
                f"Prompt should not ask for conversion: {interviewer_tz} -> {display_tz} ({fmt})"
            )
            assert display_tz not in prompt or display_tz == interviewer_tz, (
                f"Display TZ '{display_tz}' should not appear in prompt"
            )


# ===========================================================================
# UNIT TESTS: normalization
# ===========================================================================

class TestNormalizeUsesInterviewerTz:
    """Verify normalize_slots_to_utc correctly uses each interviewer's timezone."""

    def test_new_york_slot_to_utc(self):
        """9:00 AM in New York (EDT in March) = 13:00 UTC."""
        slots = [{"date": "2026-03-10", "start": "09:00", "end": "10:00"}]
        result = normalize_slots_to_utc(slots, "America/New_York")
        assert len(result) == 1
        start_utc, end_utc = result[0]
        # March 10 2026 — EDT is active (DST starts March 8 2026), EDT = UTC-4
        assert start_utc.hour == 13
        assert end_utc.hour == 14

    def test_los_angeles_slot_to_utc(self):
        """9:00 AM in LA (PDT in March) = 16:00 UTC."""
        slots = [{"date": "2026-03-10", "start": "09:00", "end": "10:00"}]
        result = normalize_slots_to_utc(slots, "America/Los_Angeles")
        assert len(result) == 1
        start_utc, _ = result[0]
        # PDT = UTC-7
        assert start_utc.hour == 16

    def test_tokyo_slot_to_utc(self):
        """9:00 AM in Tokyo = 00:00 UTC (JST = UTC+9)."""
        slots = [{"date": "2026-03-10", "start": "09:00", "end": "10:00"}]
        result = normalize_slots_to_utc(slots, "Asia/Tokyo")
        assert len(result) == 1
        start_utc, end_utc = result[0]
        assert start_utc.hour == 0
        assert end_utc.hour == 1

    def test_sydney_slot_to_utc(self):
        """9:00 AM in Sydney (AEDT in March) = 22:00 UTC previous day (AEDT = UTC+11)."""
        slots = [{"date": "2026-03-10", "start": "09:00", "end": "10:00"}]
        result = normalize_slots_to_utc(slots, "Australia/Sydney")
        assert len(result) == 1
        start_utc, _ = result[0]
        # AEDT = UTC+11, so 9:00 AEDT = 22:00 UTC previous day
        assert start_utc.hour == 22
        assert start_utc.day == 9

    def test_utc_slot_unchanged(self):
        """UTC slots should remain at the same hour."""
        slots = [{"date": "2026-03-10", "start": "09:00", "end": "10:00"}]
        result = normalize_slots_to_utc(slots, "UTC")
        assert len(result) == 1
        start_utc, end_utc = result[0]
        assert start_utc.hour == 9
        assert end_utc.hour == 10

    def test_wrong_tz_gives_wrong_utc(self):
        """Using display_tz instead of interviewer_tz gives wrong UTC — the original bug."""
        slots = [{"date": "2026-03-10", "start": "09:00", "end": "10:00"}]

        correct = normalize_slots_to_utc(slots, "America/New_York")
        wrong = normalize_slots_to_utc(slots, "Europe/London")

        assert correct[0][0] != wrong[0][0], (
            "Using the wrong timezone should produce different UTC times"
        )

    @pytest.mark.parametrize("tz", [
        # Exclude Europe/London — it's UTC+0 (GMT) in March, same offset as UTC
        "America/New_York",
        "America/Los_Angeles",
        "Asia/Tokyo",
        "Australia/Sydney",
        "America/Chicago",
        "Europe/Berlin",
        "Asia/Kolkata",
    ])
    def test_non_utc_differs_from_utc(self, tz):
        """Non-UTC-offset timezone should produce different UTC times for the same local time."""
        slots = [{"date": "2026-03-10", "start": "12:00", "end": "13:00"}]
        utc_result = normalize_slots_to_utc(slots, "UTC")
        tz_result = normalize_slots_to_utc(slots, tz)
        assert utc_result[0][0] != tz_result[0][0], (
            f"Slots in {tz} should produce different UTC than slots in UTC"
        )


# ===========================================================================
# UNIT TESTS: end-to-end timezone flow
# ===========================================================================

class TestEndToEndTimezoneFlow:
    """Test the full flow: parse → normalize → display in a different TZ."""

    def test_parse_in_ny_display_in_london(self):
        """Slots parsed as New York time, displayed in London."""
        parsed_slots = [
            {"date": "2026-03-10", "start": "09:00", "end": "10:00"},
            {"date": "2026-03-10", "start": "14:00", "end": "15:00"},
        ]

        utc_tuples = normalize_slots_to_utc(parsed_slots, "America/New_York")

        for start_utc, end_utc in utc_tuples:
            start_london = from_utc(start_utc, "Europe/London")
            # London (GMT) = UTC in March. EDT = UTC-4.
            # 9:00 AM EDT = 13:00 UTC = 13:00 GMT
            assert start_london.hour == start_utc.hour

    def test_parse_in_tokyo_display_in_ny(self):
        """Slots parsed as Tokyo time, displayed in New York."""
        parsed_slots = [{"date": "2026-03-10", "start": "09:00", "end": "10:00"}]
        utc_tuples = normalize_slots_to_utc(parsed_slots, "Asia/Tokyo")
        start_utc, _ = utc_tuples[0]

        assert start_utc.hour == 0  # JST = UTC+9
        start_ny = from_utc(start_utc, "America/New_York")
        assert start_ny.hour == 20  # EDT = UTC-4
        assert start_ny.day == 9    # Previous day

    def test_utc_round_trip_is_identity(self):
        parsed_slots = [{"date": "2026-03-10", "start": "09:00", "end": "10:00"}]
        utc_tuples = normalize_slots_to_utc(parsed_slots, "UTC")
        start_utc, _ = utc_tuples[0]
        displayed = from_utc(start_utc, "UTC")
        assert displayed.hour == 9

    @pytest.mark.parametrize("interviewer_tz,display_tz", [
        ("America/New_York", "Asia/Tokyo"),
        ("Europe/London", "America/Los_Angeles"),
        ("Australia/Sydney", "Europe/Berlin"),
        ("Asia/Kolkata", "America/Chicago"),
    ])
    def test_cross_tz_round_trip_consistent(self, interviewer_tz, display_tz):
        """Parse in interviewer TZ, normalize to UTC, display in display TZ — consistent."""
        parsed_slots = [
            {"date": "2026-03-10", "start": "10:00", "end": "11:00"},
            {"date": "2026-03-11", "start": "14:00", "end": "15:00"},
        ]

        utc_tuples = normalize_slots_to_utc(parsed_slots, interviewer_tz)

        for start_utc, end_utc in utc_tuples:
            # Convert to display TZ
            start_display = from_utc(start_utc, display_tz)
            end_display = from_utc(end_utc, display_tz)

            # Convert display time back to UTC to verify round-trip
            start_back = to_utc(start_display)
            end_back = to_utc(end_display)

            assert start_back == start_utc, f"Round-trip failed for {interviewer_tz} → {display_tz}"
            assert end_back == end_utc


# ===========================================================================
# INTEGRATION: full intersection pipeline with non-UTC timezones
# ===========================================================================

class TestIntersectionWithTimezones:
    """Test that multi-interviewer intersection works with different timezones."""

    def test_two_interviewers_different_tz_overlap(self):
        """Two interviewers in different TZs: slots that overlap in UTC appear in intersection."""
        # Interviewer 1 (New York): free 9:00-12:00 EDT = 13:00-16:00 UTC
        iv1_slots = [{"date": "2026-03-10", "start": "09:00", "end": "12:00"}]
        # Interviewer 2 (London): free 14:00-17:00 GMT = 14:00-17:00 UTC
        iv2_slots = [{"date": "2026-03-10", "start": "14:00", "end": "17:00"}]

        iv1_utc = normalize_slots_to_utc(iv1_slots, "America/New_York")
        iv2_utc = normalize_slots_to_utc(iv2_slots, "Europe/London")

        # iv1: 13-16 UTC, iv2: 14-17 UTC → full overlap 14-16 UTC
        intersection = compute_intersection(
            {1: merge_adjacent_slots(iv1_utc), 2: merge_adjacent_slots(iv2_utc)},
            min_duration_minutes=30,
            display_timezone="UTC",
        )

        full_overlaps = [s for s in intersection if s["is_full_overlap"]]
        assert len(full_overlaps) == 1
        slot = full_overlaps[0]
        assert slot["start"] == "14:00"
        assert slot["end"] == "16:00"

    def test_two_interviewers_same_local_time_different_tz_no_overlap(self):
        """Same local time in different TZs may NOT overlap in UTC."""
        # Interviewer 1 (New York): free 09:00-10:00 EDT = 13:00-14:00 UTC
        iv1_slots = [{"date": "2026-03-10", "start": "09:00", "end": "10:00"}]
        # Interviewer 2 (Tokyo): free 09:00-10:00 JST = 00:00-01:00 UTC
        iv2_slots = [{"date": "2026-03-10", "start": "09:00", "end": "10:00"}]

        iv1_utc = normalize_slots_to_utc(iv1_slots, "America/New_York")
        iv2_utc = normalize_slots_to_utc(iv2_slots, "Asia/Tokyo")

        intersection = compute_intersection(
            {1: merge_adjacent_slots(iv1_utc), 2: merge_adjacent_slots(iv2_utc)},
            min_duration_minutes=30,
            display_timezone="UTC",
        )

        # No overlap — 13-14 UTC vs 0-1 UTC
        full_overlap = [s for s in intersection if s["is_full_overlap"]]
        assert len(full_overlap) == 0

    def test_intersection_displayed_in_nonutc(self):
        """Intersection results should correctly display in non-UTC timezone."""
        # Both in New York: iv1 free 09:00-12:00 EDT, iv2 free 10:00-14:00 EDT
        iv1_slots = [{"date": "2026-03-10", "start": "09:00", "end": "12:00"}]
        iv2_slots = [{"date": "2026-03-10", "start": "10:00", "end": "14:00"}]

        iv1_utc = normalize_slots_to_utc(iv1_slots, "America/New_York")
        iv2_utc = normalize_slots_to_utc(iv2_slots, "America/New_York")

        # Display in New York time
        intersection = compute_intersection(
            {1: merge_adjacent_slots(iv1_utc), 2: merge_adjacent_slots(iv2_utc)},
            min_duration_minutes=30,
            display_timezone="America/New_York",
        )

        # Filter to full overlap only (both available)
        full_overlaps = [s for s in intersection if s["is_full_overlap"]]
        assert len(full_overlaps) == 1
        slot = full_overlaps[0]
        # Full overlap is 10:00-12:00 EDT
        assert slot["start"] == "10:00"
        assert slot["end"] == "12:00"


# ===========================================================================
# INTEGRATION: real testing_files with non-UTC timezones
# ===========================================================================

class TestRealFilesWithTimezones:
    """
    Integration tests using real PDF/image files from testing_files/.

    Loads real files, runs real preprocessing, mocks OpenAI to return
    future-dated slots, then verifies timezone handling across the full pipeline.
    """

    @pytest.fixture
    def parser(self):
        mock_client = MagicMock()
        return CalendarParser(mock_client, ParserConfig())

    # -----------------------------------------------------------------
    # Helper
    # -----------------------------------------------------------------
    def _parse_with_tz(self, parser, image, interviewer_tz, display_tz, mock_slots):
        """Parse image with given TZ pair and return (result, prompt_used)."""
        captured_prompt = {}

        original_extract = parser._extract_slots

        def capture_extract(img, prompt):
            captured_prompt["prompt"] = prompt
            return mock_slots, json.dumps(mock_slots)

        with patch.object(parser, "detect_format", return_value=(
            CalendarFormat.WEEK_VIEW, 0.95, "Grid layout"
        )):
            with patch.object(parser, "_extract_slots", side_effect=capture_extract):
                result = parser.parse_image(
                    image,
                    interviewer_timezone=interviewer_tz,
                    display_timezone=display_tz,
                )

        return result, captured_prompt.get("prompt", "")

    # -----------------------------------------------------------------
    # PNG files
    # -----------------------------------------------------------------
    @pytest.mark.parametrize("tz", NON_UTC_TIMEZONES)
    def test_screenshot_jan22_nonutc_prompt_no_conversion(self, parser, tz):
        """Real PNG: prompt never asks for conversion regardless of TZ."""
        img_path = TESTING_FILES_DIR / "Screenshot 2026-01-22 112205.png"
        if not img_path.exists():
            pytest.skip(f"Test file not found: {img_path}")

        img = Image.open(img_path)
        _, prompt = self._parse_with_tz(parser, img, tz, "UTC", FUTURE_WEEK_SLOTS)

        assert "Convert all times to" not in prompt
        assert "Do NOT convert" in prompt
        assert tz in prompt

    @pytest.mark.parametrize("tz", NON_UTC_TIMEZONES)
    def test_screenshot_jan26_nonutc_prompt_no_conversion(self, parser, tz):
        """Real PNG: prompt never asks for conversion regardless of TZ."""
        img_path = TESTING_FILES_DIR / "Screenshot 2026-01-26 121045.png"
        if not img_path.exists():
            pytest.skip(f"Test file not found: {img_path}")

        img = Image.open(img_path)
        display_tz = "Europe/London"
        _, prompt = self._parse_with_tz(parser, img, tz, display_tz, FUTURE_WEEK_SLOTS)

        assert "Convert all times to" not in prompt
        assert "Do NOT convert" in prompt
        # display_tz should not appear as a conversion target
        if tz != display_tz:
            assert display_tz not in prompt

    @pytest.mark.parametrize("tz", NON_UTC_TIMEZONES)
    def test_png_slots_identical_regardless_of_display_tz(self, parser, tz):
        """Parsed slots must be identical whether display_tz is UTC or non-UTC."""
        img_path = TESTING_FILES_DIR / "Screenshot 2026-01-26 121120.png"
        if not img_path.exists():
            pytest.skip(f"Test file not found: {img_path}")

        img = Image.open(img_path)

        result_utc, _ = self._parse_with_tz(parser, img, tz, "UTC", FUTURE_WEEK_SLOTS)
        result_other, _ = self._parse_with_tz(parser, img, tz, "Asia/Tokyo", FUTURE_WEEK_SLOTS)

        # Raw parsed slots should be identical — display_tz doesn't affect parsing
        slots_utc = [(s.date, s.start, s.end) for s in result_utc.slots]
        slots_other = [(s.date, s.start, s.end) for s in result_other.slots]
        assert slots_utc == slots_other, (
            "Changing display_tz should NOT change parsed slot times"
        )

    # -----------------------------------------------------------------
    # PDF files
    # -----------------------------------------------------------------
    @pytest.mark.parametrize("tz", NON_UTC_TIMEZONES)
    def test_pdf_scheduler_test_nonutc(self, parser, tz):
        """Real PDF: prompt never asks for conversion."""
        pdf_path = TESTING_FILES_DIR / "Scheduler Test.pdf"
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        from app import pdf_to_images
        images = pdf_to_images(pdf_path.read_bytes(), max_pages=1)
        if not images:
            pytest.skip("Could not extract images from PDF")

        _, prompt = self._parse_with_tz(parser, images[0], tz, "UTC", FUTURE_AGENDA_SLOTS)

        assert "Convert all times to" not in prompt
        assert "Do NOT convert" in prompt

    @pytest.mark.parametrize("tz", NON_UTC_TIMEZONES)
    def test_pdf_test40_nonutc(self, parser, tz):
        """Real PDF: prompt never asks for conversion."""
        pdf_path = TESTING_FILES_DIR / "Test 40.pdf"
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        from app import pdf_to_images
        images = pdf_to_images(pdf_path.read_bytes(), max_pages=1)
        if not images:
            pytest.skip("Could not extract images from PDF")

        display_tz = "Europe/Berlin"
        _, prompt = self._parse_with_tz(parser, images[0], tz, display_tz, FUTURE_WEEK_SLOTS)

        assert "Convert all times to" not in prompt
        if tz != display_tz:
            assert display_tz not in prompt

    @pytest.mark.parametrize("tz", NON_UTC_TIMEZONES)
    def test_pdf_test50_nonutc(self, parser, tz):
        """Real PDF: prompt never asks for conversion."""
        pdf_path = TESTING_FILES_DIR / "Test 50.pdf"
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        from app import pdf_to_images
        images = pdf_to_images(pdf_path.read_bytes(), max_pages=1)
        if not images:
            pytest.skip("Could not extract images from PDF")

        display_tz = "America/Los_Angeles"
        _, prompt = self._parse_with_tz(parser, images[0], tz, display_tz, FUTURE_WEEK_SLOTS)

        assert "Convert all times to" not in prompt
        if tz != display_tz:
            assert display_tz not in prompt

    @pytest.mark.parametrize("tz", NON_UTC_TIMEZONES)
    def test_pdf_calendar_test12_nonutc(self, parser, tz):
        """Real PDF: prompt never asks for conversion."""
        pdf_path = TESTING_FILES_DIR / "CalendarTest 12.pdf"
        if not pdf_path.exists():
            pytest.skip(f"Test file not found: {pdf_path}")

        from app import pdf_to_images
        images = pdf_to_images(pdf_path.read_bytes(), max_pages=1)
        if not images:
            pytest.skip("Could not extract images from PDF")

        display_tz = "Asia/Kolkata"
        _, prompt = self._parse_with_tz(parser, images[0], tz, display_tz, FUTURE_WEEK_SLOTS)

        assert "Convert all times to" not in prompt
        if tz != display_tz:
            assert display_tz not in prompt

    # -----------------------------------------------------------------
    # Full pipeline: file → parse → normalize → intersection → display
    # -----------------------------------------------------------------
    def test_full_pipeline_two_interviewers_different_tz(self, parser):
        """
        End-to-end pipeline with two interviewers in different timezones:
        parse files → normalize each to UTC → compute intersection → display.
        """
        img_path = TESTING_FILES_DIR / "Screenshot 2026-01-26 121045.png"
        if not img_path.exists():
            pytest.skip(f"Test file not found: {img_path}")

        img = Image.open(img_path)

        # Interviewer 1 is in New York — their calendar shows NY times
        iv1_mock = [
            {"date": "2026-03-10", "start": "09:00", "end": "12:00", "confidence": 0.95},
            {"date": "2026-03-10", "start": "14:00", "end": "17:00", "confidence": 0.93},
        ]
        result1, _ = self._parse_with_tz(parser, img, "America/New_York", "UTC", iv1_mock)

        # Interviewer 2 is in London — their calendar shows London times
        iv2_mock = [
            {"date": "2026-03-10", "start": "13:00", "end": "17:00", "confidence": 0.95},
            {"date": "2026-03-10", "start": "18:00", "end": "20:00", "confidence": 0.90},
        ]
        result2, _ = self._parse_with_tz(parser, img, "Europe/London", "UTC", iv2_mock)

        # Normalize each using their respective timezone (THE FIX)
        iv1_slots = [s.to_dict() for s in result1.slots]
        iv2_slots = [s.to_dict() for s in result2.slots]

        iv1_utc = normalize_slots_to_utc(iv1_slots, "America/New_York")
        iv2_utc = normalize_slots_to_utc(iv2_slots, "Europe/London")

        # iv1: 09-12 EDT = 13-16 UTC, 14-17 EDT = 18-21 UTC
        # iv2: 13-17 GMT = 13-17 UTC, 18-20 GMT = 18-20 UTC
        # Overlap: 13-16 UTC (from iv1 morning + iv2 afternoon)
        #          and 18-20 UTC (from iv1 afternoon + iv2 evening)

        intersection = compute_intersection(
            {1: merge_adjacent_slots(iv1_utc), 2: merge_adjacent_slots(iv2_utc)},
            min_duration_minutes=30,
            display_timezone="UTC",
        )

        full_overlaps = [s for s in intersection if s["is_full_overlap"]]
        assert len(full_overlaps) >= 1, "Should find at least one overlapping slot"

        # Verify the first overlap is in the expected range
        first_overlap = full_overlaps[0]
        assert first_overlap["start"] == "13:00"
        assert first_overlap["end"] == "16:00"

    def test_full_pipeline_display_in_nonutc(self, parser):
        """
        Parsed slots normalized correctly and displayed in a non-UTC timezone.
        Verifies the whole chain doesn't double-convert.
        """
        img_path = TESTING_FILES_DIR / "Screenshot 2026-01-22 112205.png"
        if not img_path.exists():
            pytest.skip(f"Test file not found: {img_path}")

        img = Image.open(img_path)

        # Parse as if interviewer is in Chicago
        mock_slots = [
            {"date": "2026-03-10", "start": "10:00", "end": "11:00", "confidence": 0.95},
        ]
        result, _ = self._parse_with_tz(parser, img, "America/Chicago", "Asia/Tokyo", mock_slots)

        slots = [s.to_dict() for s in result.slots]
        # Slots are in Chicago time (CDT = UTC-5)
        utc_tuples = normalize_slots_to_utc(slots, "America/Chicago")
        start_utc, end_utc = utc_tuples[0]

        # 10:00 CDT = 15:00 UTC
        assert start_utc.hour == 15
        assert end_utc.hour == 16

        # Display in Tokyo: 15:00 UTC = 00:00 JST (next day)
        start_tokyo = from_utc(start_utc, "Asia/Tokyo")
        assert start_tokyo.hour == 0
        assert start_tokyo.day == 11  # Next day
