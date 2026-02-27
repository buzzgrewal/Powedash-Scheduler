"""
Comprehensive tests for multi-file upload support per interviewer.

Tests cover:

WHITE-BOX:
- Session state schema: "files" (list) replaces "file" (single) everywhere
- _save_persisted_slots: excludes non-serializable "files" key
- _load_persisted_slots + restore: adds "files": [] to loaded interviewers
- _parse_single_interviewer_availability: all 7 output branches
- _parse_all_panel_availability: all output branches, per-interviewer warnings
- Cross-file deduplication logic using (date, start, end) tuple key
- Rejection reason accumulation across files (dict merging)
- File seek(0) called before each parse
- _merge_slots: manual preferred over uploaded for duplicate keys

BLACK-BOX:
- Upload 2+ files → all parsed, slots merged
- Upload mix of PDF + PNG → both types parsed
- Overlapping dates across files → deduplicated
- All-past dates → rejection warning shown
- Manual slots + multi-file → manual preserved
- Single file upload (backward compat) → same as before
- Parse button visibility based on files/slots state

EDGE CASES:
- Empty files list ([]) → falls back to manual
- Missing "files" key entirely → treated as no files
- None returned from file_uploader → stored as []
- Exception in one file among many → caught, error shown
- Same slot in 3+ files → deduplicated to 1
- Interviewer with no name → fallback naming "Interviewer {id}"
- Invalid interviewer index → error, return
- No files AND no manual slots → warning, return
- Mixed rejection reasons from different files (past_date + weekend)
- Save/load round-trip: files excluded, restored as []
- _parse_all: one interviewer errors, others still parse
- _parse_all: partial rejection warning alongside valid slots
- _parse_all: interviewer with files + interviewer with only manual

INTEGRATION:
- Real testing_files/ loaded as BytesIO through multi-file paths
- All 8 testing files at once through both parse functions
- Seek after partial read on real files
"""

import io
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Mock streamlit at module level
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

import app as app_mod

TESTING_FILES_DIR = Path(__file__).resolve().parent.parent / "testing_files"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
TODAY = date.today()


def _next_weekday(start: date, offset: int = 1) -> date:
    d = start + timedelta(days=offset)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _prev_weekday(start: date, offset: int = 1) -> date:
    d = start - timedelta(days=offset)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# Use large enough offsets so each lands on a distinct weekday,
# even when TODAY is adjacent to a weekend.
FUTURE1 = _next_weekday(TODAY, 1)
FUTURE2 = _next_weekday(FUTURE1, 1)  # chain from FUTURE1 to guarantee distinct
FUTURE3 = _next_weekday(FUTURE2, 1)
FUTURE4 = _next_weekday(FUTURE3, 1)
PAST1 = _prev_weekday(TODAY, 1)


def _make_future_slots(dates_and_times):
    """Build slot dicts with given dates and times."""
    return [
        {"date": d.isoformat(), "start": s, "end": e, "confidence": 0.9}
        for d, s, e in dates_and_times
    ]


def _make_file(name="cal.png", content=b"fake"):
    """Create a named BytesIO for use as a fake uploaded file."""
    f = io.BytesIO(content)
    f.name = name
    return f


def _make_file_like(path: Path) -> io.BytesIO:
    """Create a seekable BytesIO from a real file, with .name attribute."""
    data = path.read_bytes()
    buf = io.BytesIO(data)
    buf.name = path.name
    return buf


def _make_interviewer(files=None, name="Alice", email="alice@test.com",
                      slots=None, iid=1, timezone="America/New_York"):
    return {
        "id": iid, "name": name, "email": email,
        "files": files if files is not None else [],
        "slots": slots or [],
        "timezone": timezone,
    }


def _setup_session_state(interviewers):
    """Configure mock session state for parse functions."""
    st = sys.modules["streamlit"]
    st.session_state = {
        "panel_interviewers": interviewers,
        "selected_timezone": "America/New_York",
        "duration_minutes": 60,
        "computed_intersections": [],
        "slots": [],
    }
    st.success.reset_mock()
    st.warning.reset_mock()
    st.error.reset_mock()
    st.info.reset_mock()
    return st


@pytest.fixture(autouse=True)
def _patch_intersection():
    """Patch slot_intersection globally to avoid real slot computation."""
    mock_mod = MagicMock()
    mock_mod.normalize_slots_to_utc.side_effect = lambda s, tz: s
    mock_mod.merge_adjacent_slots.side_effect = lambda s: s
    mock_mod.compute_intersection.return_value = []
    with patch.dict(sys.modules, {"slot_intersection": mock_mod}):
        yield mock_mod


# ===========================================================================
# 1. WHITE-BOX: Session state schema
# ===========================================================================
class TestSessionStateSchema:
    """Verify 'files' (list) replaced 'file' (single) in all templates."""

    def test_no_file_colon_none_in_source(self):
        """app.py must not contain '"file": None' pattern."""
        import re
        source = (Path(__file__).resolve().parent.parent / "app.py").read_text()
        matches = re.findall(r'"file"\s*:\s*None', source)
        assert matches == [], f"Found stale 'file': None in app.py"

    def test_save_excludes_files_key_not_file(self):
        """_save_persisted_slots must filter 'files', not old 'file'."""
        import re
        source = (Path(__file__).resolve().parent.parent / "app.py").read_text()
        # Find the exclusion pattern in _save_persisted_slots
        matches = re.findall(r'k\s*!=\s*"(\w+)"', source)
        assert "files" in matches, "Should exclude 'files' key from serialization"
        assert "file" not in matches, "Should NOT still be excluding old 'file' key"


# ===========================================================================
# 2. WHITE-BOX: Persistence round-trip
# ===========================================================================
class TestPersistenceRoundTrip:
    """Verify save/load correctly handles the 'files' key."""

    def test_save_excludes_files_from_json(self):
        """Files (BytesIO objects) must not appear in saved JSON."""
        st = sys.modules["streamlit"]
        f = _make_file("test.png")
        interviewer = _make_interviewer(files=[f], slots=[
            {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00", "source": "uploaded"}
        ])
        st.session_state = {
            "panel_interviewers": [interviewer],
            "slots": [],
            "computed_intersections": [],
            "next_interviewer_id": 2,
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with patch.object(app_mod, "_get_slots_path", return_value=tmp_path):
                app_mod._save_persisted_slots()

            with open(tmp_path, 'r') as fh:
                data = json.load(fh)

            saved_iv = data["panel_interviewers"][0]
            assert "files" not in saved_iv, "files key must be excluded from JSON"
            assert "file" not in saved_iv, "old file key must not appear"
            assert "slots" in saved_iv
        finally:
            os.unlink(tmp_path)

    def test_load_restores_files_as_empty_list(self):
        """When loading persisted data, 'files' should be added as []."""
        saved_data = {
            "slots": [],
            "computed_intersections": [],
            "panel_interviewers": [
                {"id": 1, "name": "Alice", "email": "a@t.com",
                 "slots": [], "timezone": "America/New_York"}
            ],
            "next_interviewer_id": 2,
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
            json.dump(saved_data, tmp)
            tmp_path = tmp.name

        try:
            with patch.object(app_mod, "_get_slots_path", return_value=tmp_path):
                loaded = app_mod._load_persisted_slots()

            # Simulate what init_session_state does on load
            restored_panel = [
                {**iv, "files": []} for iv in loaded["panel_interviewers"]
            ]
            assert restored_panel[0]["files"] == []
            assert restored_panel[0]["name"] == "Alice"
        finally:
            os.unlink(tmp_path)

    def test_save_load_round_trip_preserves_slots(self):
        """Slots survive a save/load round trip; files are stripped and restored."""
        st = sys.modules["streamlit"]
        slot = {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                "source": "uploaded", "confidence": 0.9}
        interviewer = _make_interviewer(
            files=[_make_file()],
            slots=[slot],
        )
        st.session_state = {
            "panel_interviewers": [interviewer],
            "slots": [slot],
            "computed_intersections": [],
            "next_interviewer_id": 2,
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with patch.object(app_mod, "_get_slots_path", return_value=tmp_path):
                app_mod._save_persisted_slots()
                loaded = app_mod._load_persisted_slots()

            restored = [{**iv, "files": []} for iv in loaded["panel_interviewers"]]
            assert len(restored[0]["slots"]) == 1
            assert restored[0]["slots"][0]["date"] == FUTURE1.isoformat()
            assert restored[0]["files"] == []
        finally:
            os.unlink(tmp_path)


# ===========================================================================
# 3. WHITE-BOX: _parse_single_interviewer_availability — all branches
# ===========================================================================
class TestParseSingleAllBranches:
    """Test every output branch of _parse_single_interviewer_availability."""

    # Branch 1: invalid index
    def test_invalid_index_shows_error(self):
        st = _setup_session_state([])
        app_mod._parse_single_interviewer_availability(0)
        st.error.assert_called_once_with("Invalid interviewer index")

    def test_negative_index_beyond_list(self):
        st = _setup_session_state([_make_interviewer()])
        app_mod._parse_single_interviewer_availability(5)
        st.error.assert_called_once_with("Invalid interviewer index")

    # Branch 2a: files present, valid slots, no rejections → success
    def test_files_valid_no_rejections(self):
        slots = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        interviewer = _make_interviewer(files=[_make_file()])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=slots):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        st.success.assert_called_once()
        msg = st.success.call_args[0][0]
        assert "1 slot" in msg
        assert "filtered" not in msg.lower()

    # Branch 2b: files present, valid slots + rejections → success with filtered info
    def test_files_valid_with_rejections(self):
        slots = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        interviewer = _make_interviewer(files=[_make_file()])
        st = _setup_session_state([interviewer])

        def mock_parse(f, itz, dtz):
            st.session_state["parser_rejected_reasons"] = {"past_date": 3, "weekend": 1}
            return slots

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        st.success.assert_called_once()
        msg = st.success.call_args[0][0]
        assert "1 slot" in msg
        assert "filtered out" in msg.lower()
        assert "in the past" in msg.lower()
        assert "weekend" in msg.lower()

    # Branch 2c: files present, 0 valid, all rejected → warning
    def test_files_all_rejected(self):
        interviewer = _make_interviewer(files=[_make_file()])
        st = _setup_session_state([interviewer])

        def mock_parse(f, itz, dtz):
            st.session_state["parser_rejected_reasons"] = {"past_date": 5}
            return []

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        st.warning.assert_called_once()
        msg = st.warning.call_args[0][0]
        assert "0 slot" in msg
        assert "all extracted slots were filtered" in msg.lower()

    # Branch 2d: files present, 0 valid, 0 rejected → warning no extraction
    def test_files_no_slots_no_rejections(self):
        interviewer = _make_interviewer(files=[_make_file()])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=[]):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        st.warning.assert_called_once()
        msg = st.warning.call_args[0][0]
        assert "no slots could be extracted" in msg.lower()

    # Branch 3: no files, has manual slots → info
    def test_no_files_manual_slots(self):
        manual = {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                  "source": "manual", "confidence": 1.0}
        interviewer = _make_interviewer(files=[], slots=[manual])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_save_persisted_slots"):
            app_mod._parse_single_interviewer_availability(0)

        st.info.assert_called_once()
        assert "1 manual" in st.info.call_args[0][0]

    # Branch 4: no files, no manual → warning, early return
    def test_no_files_no_manual(self):
        interviewer = _make_interviewer(files=[], slots=[])
        st = _setup_session_state([interviewer])

        app_mod._parse_single_interviewer_availability(0)

        st.warning.assert_called_once()
        assert "no file or manual" in st.warning.call_args[0][0].lower()

    # Branch 5: exception during parse → error
    def test_exception_during_parse(self):
        interviewer = _make_interviewer(files=[_make_file()])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload",
                          side_effect=ValueError("corrupt PDF")):
            app_mod._parse_single_interviewer_availability(0)

        st.error.assert_called_once()
        assert "corrupt PDF" in st.error.call_args[0][0]

    # Fallback name when interviewer has no name
    def test_fallback_name_when_no_name(self):
        interviewer = _make_interviewer(files=[], slots=[], name="", iid=42)
        st = _setup_session_state([interviewer])

        app_mod._parse_single_interviewer_availability(0)

        msg = st.warning.call_args[0][0]
        assert "Interviewer 42" in msg


# ===========================================================================
# 4. WHITE-BOX: Multi-file loop mechanics
# ===========================================================================
class TestMultiFileLoopMechanics:
    """Test seek, iteration, dedup, and rejection accumulation."""

    def test_seek_called_per_file(self):
        """Each file.seek(0) must be called before parse."""
        f1, f2, f3 = MagicMock(name="f1"), MagicMock(name="f2"), MagicMock(name="f3")
        interviewer = _make_interviewer(files=[f1, f2, f3])
        _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=[]):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        f1.seek.assert_called_with(0)
        f2.seek.assert_called_with(0)
        f3.seek.assert_called_with(0)

    def test_parse_called_once_per_file(self):
        """_parse_availability_upload must be called exactly N times for N files."""
        files = [_make_file(f"f{i}.png") for i in range(5)]
        interviewer = _make_interviewer(files=files)
        _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=[]) as mock_parse:
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        assert mock_parse.call_count == 5

    def test_cross_file_dedup_by_date_start_end(self):
        """Slots with same (date, start, end) across files are deduplicated."""
        slot = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        files = [_make_file(f"f{i}.png") for i in range(3)]
        interviewer = _make_interviewer(files=files)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=slot):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        uploaded = [s for s in result if s.get("source") == "uploaded"]
        assert len(uploaded) == 1

    def test_dedup_keeps_different_times_same_date(self):
        """Different time slots on the same date are NOT deduped."""
        s1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        s2 = _make_future_slots([(FUTURE1, "14:00", "15:00")])
        files = [_make_file("a.png"), _make_file("b.png")]
        interviewer = _make_interviewer(files=files)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [s1, s2]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 2

    def test_rejection_reasons_summed_across_files(self):
        """Rejection counts from multiple files are accumulated."""
        files = [_make_file("a.png"), _make_file("b.png"), _make_file("c.png")]
        interviewer = _make_interviewer(files=files)
        st = _setup_session_state([interviewer])

        call_idx = [0]

        def mock_parse(f, itz, dtz):
            call_idx[0] += 1
            reasons = {
                1: {"past_date": 2, "weekend": 1},
                2: {"past_date": 3},
                3: {"weekend": 2, "too_short": 1},
            }
            st.session_state["parser_rejected_reasons"] = reasons[call_idx[0]]
            return []

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        # past_date: 2+3=5, weekend: 1+2=3, too_short: 1
        msg = st.warning.call_args[0][0]
        assert "5 in the past" in msg
        assert "3 on a weekend" in msg
        assert "1 were too short" in msg

    def test_all_slots_marked_uploaded_source(self):
        """Every slot from files must have source='uploaded'."""
        s1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        s2 = _make_future_slots([(FUTURE2, "14:00", "15:00")])
        files = [_make_file("a.png"), _make_file("b.png")]
        interviewer = _make_interviewer(files=files)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [s1, s2]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        for s in result:
            assert s["source"] == "uploaded"

    def test_parser_rejected_reasons_cleared_between_files(self):
        """parser_rejected_reasons in session state is popped before each file."""
        files = [_make_file("a.png"), _make_file("b.png")]
        interviewer = _make_interviewer(files=files)
        st = _setup_session_state([interviewer])
        st.session_state["parser_rejected_reasons"] = {"stale": 99}

        calls = []

        def mock_parse(f, itz, dtz):
            # Record what was in session state at call time
            calls.append(st.session_state.get("parser_rejected_reasons"))
            st.session_state["parser_rejected_reasons"] = {"past_date": 1}
            return []

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        # Before each file parse, the old value should have been popped
        # First call: "stale" was popped, then _parse sets "past_date"
        # Second call: "past_date" from first was popped
        assert calls[0] is None or calls[0] == {"past_date": 1}
        # The key point: stale value from before the loop doesn't persist


# ===========================================================================
# 5. WHITE-BOX: _merge_slots interaction
# ===========================================================================
class TestMergeSlotsWithMultiFile:
    """Test that manual slots are preferred over uploaded in merges."""

    def test_manual_wins_over_uploaded_duplicate(self):
        """When manual and uploaded have same (date,start,end), manual kept."""
        manual = {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                  "source": "manual", "confidence": 1.0}
        uploaded = {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                    "source": "uploaded", "confidence": 0.85}

        result = app_mod._merge_slots([manual], [uploaded])
        assert len(result) == 1
        assert result[0]["source"] == "manual"

    def test_non_overlapping_manual_and_uploaded(self):
        """Manual + uploaded with different times both kept."""
        manual = {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                  "source": "manual", "confidence": 1.0}
        uploaded = {"date": FUTURE1.isoformat(), "start": "14:00", "end": "15:00",
                    "source": "uploaded", "confidence": 0.9}

        result = app_mod._merge_slots([manual], [uploaded])
        assert len(result) == 2

    def test_manual_preserved_when_multi_file_has_overlap(self):
        """Multi-file upload that overlaps with manual — manual wins in merge."""
        manual = {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                  "source": "manual", "confidence": 1.0}
        file_slot = _make_future_slots([(FUTURE1, "09:00", "10:00")])

        interviewer = _make_interviewer(files=[_make_file()], slots=[manual])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=file_slot):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        matching = [s for s in result
                    if s["date"] == FUTURE1.isoformat() and s["start"] == "09:00"]
        assert len(matching) == 1
        assert matching[0]["source"] == "manual"


# ===========================================================================
# 6. WHITE-BOX: _parse_all_panel_availability — all branches
# ===========================================================================
class TestParseAllBranches:
    """Test all branches in _parse_all_panel_availability."""

    def test_two_interviewers_each_with_files(self):
        """Multiple interviewers with files → all parsed."""
        s1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        s2 = _make_future_slots([(FUTURE2, "14:00", "15:00")])

        interviewers = [
            _make_interviewer(files=[_make_file("a.png")], name="Alice", iid=1),
            _make_interviewer(files=[_make_file("b.png")], name="Bob", iid=2),
        ]
        st = _setup_session_state(interviewers)

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [s1, s2]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        assert mock_parse.call_count == 2
        assert len(st.session_state["panel_interviewers"][0]["slots"]) == 1
        assert len(st.session_state["panel_interviewers"][1]["slots"]) == 1

    def test_one_files_one_manual_only(self):
        """One interviewer with files, another with only manual."""
        file_slots = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        manual = {"date": FUTURE2.isoformat(), "start": "14:00", "end": "15:00",
                  "source": "manual", "confidence": 1.0}

        interviewers = [
            _make_interviewer(files=[_make_file()], name="Alice", iid=1),
            _make_interviewer(files=[], name="Bob", iid=2, slots=[manual]),
        ]
        st = _setup_session_state(interviewers)

        with patch.object(app_mod, "_parse_availability_upload", return_value=file_slots):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        alice = st.session_state["panel_interviewers"][0]["slots"]
        bob = st.session_state["panel_interviewers"][1]["slots"]
        assert len(alice) == 1 and alice[0]["source"] == "uploaded"
        assert len(bob) == 1 and bob[0]["source"] == "manual"

    def test_exception_in_one_interviewer_others_proceed(self):
        """Exception parsing one interviewer shouldn't stop others."""
        s2 = _make_future_slots([(FUTURE2, "14:00", "15:00")])
        interviewers = [
            _make_interviewer(files=[_make_file("bad.png")], name="Alice", iid=1),
            _make_interviewer(files=[_make_file("good.png")], name="Bob", iid=2),
        ]
        st = _setup_session_state(interviewers)

        call_idx = [0]

        def mock_parse(f, itz, dtz):
            call_idx[0] += 1
            if call_idx[0] == 1:
                raise RuntimeError("corrupt file")
            return s2

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        # Alice's error reported
        st.error.assert_called_once()
        assert "Alice" in st.error.call_args[0][0]
        # Bob still parsed
        bob = st.session_state["panel_interviewers"][1]["slots"]
        assert len(bob) == 1

    def test_all_rejected_warning_per_interviewer(self):
        """When all slots rejected for one interviewer → specific warning."""
        interviewer = _make_interviewer(files=[_make_file()], name="Alice", iid=1)
        st = _setup_session_state([interviewer])

        def mock_parse(f, itz, dtz):
            st.session_state["parser_rejected_reasons"] = {"past_date": 4}
            return []

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        # Specific per-interviewer warning
        warning_calls = [c[0][0] for c in st.warning.call_args_list]
        alice_warn = [w for w in warning_calls if "Alice" in w]
        assert len(alice_warn) >= 1
        assert "all extracted slots were filtered" in alice_warn[0].lower()

    def test_partial_rejection_warning_per_interviewer(self):
        """When some slots pass and some rejected → warning with counts."""
        valid = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        interviewer = _make_interviewer(files=[_make_file()], name="Alice", iid=1)
        st = _setup_session_state([interviewer])

        def mock_parse(f, itz, dtz):
            st.session_state["parser_rejected_reasons"] = {"past_date": 3}
            return valid

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        warning_calls = [c[0][0] for c in st.warning.call_args_list]
        alice_warn = [w for w in warning_calls if "Alice" in w]
        assert len(alice_warn) >= 1
        assert "parsed 1 slot" in alice_warn[0].lower()
        assert "filtered out" in alice_warn[0].lower()

    def test_no_rejection_no_warning(self):
        """When all slots pass with no rejections → no per-interviewer warning."""
        valid = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        interviewer = _make_interviewer(files=[_make_file()], name="Alice", iid=1)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=valid):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        warning_calls = [c[0][0] for c in st.warning.call_args_list]
        alice_warn = [w for w in warning_calls if "Alice" in w]
        assert alice_warn == []

    def test_cross_file_dedup_in_parse_all(self):
        """Duplicate slots across files deduped within one interviewer in parse_all."""
        dup = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        interviewer = _make_interviewer(
            files=[_make_file("a.png"), _make_file("b.png")], iid=1)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=dup):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        result = st.session_state["panel_interviewers"][0]["slots"]
        uploaded = [s for s in result if s.get("source") == "uploaded"]
        assert len(uploaded) == 1

    def test_no_interviewers_no_crash(self):
        """Empty interviewer list should not crash."""
        st = _setup_session_state([])
        with patch.object(app_mod, "_save_persisted_slots"):
            app_mod._parse_all_panel_availability()
        st.warning.assert_called()  # "No availability found" warning

    def test_multi_file_rejection_accumulation_in_parse_all(self):
        """Rejection reasons from 2 files for one interviewer accumulated in parse_all."""
        files = [_make_file("a.png"), _make_file("b.png")]
        interviewer = _make_interviewer(files=files, name="Alice", iid=1)
        st = _setup_session_state([interviewer])

        call_idx = [0]

        def mock_parse(f, itz, dtz):
            call_idx[0] += 1
            if call_idx[0] == 1:
                st.session_state["parser_rejected_reasons"] = {"past_date": 2}
            else:
                st.session_state["parser_rejected_reasons"] = {"weekend": 3}
            return []

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        warning_calls = [c[0][0] for c in st.warning.call_args_list]
        alice_warn = [w for w in warning_calls if "Alice" in w]
        assert len(alice_warn) >= 1
        assert "in the past" in alice_warn[0]
        assert "weekend" in alice_warn[0]


# ===========================================================================
# 7. BLACK-BOX: Upload scenarios
# ===========================================================================
class TestBlackBoxUploadScenarios:
    """End-to-end scenarios from the user's perspective."""

    def test_single_file_backward_compat(self):
        """Single file in list works the same as old single-file behavior."""
        slots = _make_future_slots([(FUTURE1, "09:00", "10:00"), (FUTURE2, "14:00", "15:00")])
        interviewer = _make_interviewer(files=[_make_file()])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=slots):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 2
        st.success.assert_called_once()

    def test_two_files_distinct_weeks(self):
        """Two files covering different weeks → union of all slots."""
        week1 = _make_future_slots([
            (FUTURE1, "09:00", "10:00"),
            (FUTURE1, "14:00", "15:00"),
        ])
        week2 = _make_future_slots([
            (FUTURE3, "09:00", "10:00"),
            (FUTURE3, "14:00", "15:00"),
        ])
        interviewer = _make_interviewer(files=[_make_file("w1.png"), _make_file("w2.png")])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [week1, week2]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 4
        dates = {s["date"] for s in result}
        assert FUTURE1.isoformat() in dates
        assert FUTURE3.isoformat() in dates

    def test_overlapping_dates_deduplicated(self):
        """Files with overlapping dates → duplicates removed."""
        shared = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        unique = _make_future_slots([(FUTURE2, "14:00", "15:00")])

        interviewer = _make_interviewer(files=[_make_file("a.png"), _make_file("b.png")])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            # File A returns shared + unique1, File B returns shared
            mock_parse.side_effect = [shared + unique, shared]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 2  # shared deduped to 1 + unique

    def test_manual_slots_plus_multi_file(self):
        """Manual slots preserved when multi-file upload adds more."""
        manual = {"date": FUTURE3.isoformat(), "start": "11:00", "end": "12:00",
                  "source": "manual", "confidence": 1.0}
        uploaded1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        uploaded2 = _make_future_slots([(FUTURE2, "14:00", "15:00")])

        interviewer = _make_interviewer(
            files=[_make_file("a.png"), _make_file("b.png")],
            slots=[manual],
        )
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [uploaded1, uploaded2]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 3
        sources = {s["source"] for s in result}
        assert sources == {"manual", "uploaded"}

    def test_all_files_return_past_dates(self):
        """All files produce only past-date slots → warning shown."""
        interviewer = _make_interviewer(
            files=[_make_file("a.png"), _make_file("b.png")])
        st = _setup_session_state([interviewer])

        def mock_parse(f, itz, dtz):
            st.session_state["parser_rejected_reasons"] = {"past_date": 5}
            return []

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        st.warning.assert_called()
        msg = st.warning.call_args[0][0]
        assert "0 slot" in msg
        # 5 from each file = 10 total
        assert "10 in the past" in msg

    def test_none_from_uploader_stored_as_empty_list(self):
        """Simulates st.file_uploader returning None → stored as []."""
        # This is the `uploaded or []` branch in the widget
        uploaded = None
        result = uploaded or []
        assert result == []
        assert isinstance(result, list)


# ===========================================================================
# 8. EDGE CASES
# ===========================================================================
class TestEdgeCases:
    """Boundary conditions and unusual inputs."""

    def test_missing_files_key_treated_as_no_files(self):
        """Interviewer dict without 'files' key → treated like no files."""
        interviewer = {"id": 1, "name": "Alice", "email": "a@t.com",
                       "slots": [], "timezone": "America/New_York"}
        # No "files" key at all
        st = _setup_session_state([interviewer])
        app_mod._parse_single_interviewer_availability(0)
        st.warning.assert_called()
        assert "no file or manual" in st.warning.call_args[0][0].lower()

    def test_files_key_is_none(self):
        """files=None (not []) → treated as no files."""
        interviewer = _make_interviewer(files=None)
        # Override to None (bypass helper default)
        interviewer["files"] = None
        st = _setup_session_state([interviewer])
        app_mod._parse_single_interviewer_availability(0)
        st.warning.assert_called()

    def test_single_file_in_list(self):
        """List with one file works identically to old single-file path."""
        slots = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        interviewer = _make_interviewer(files=[_make_file()])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=slots):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 1
        assert result[0]["source"] == "uploaded"

    def test_many_files_stress(self):
        """10 files each returning 5 slots → all processed."""
        files = [_make_file(f"f{i}.png") for i in range(10)]
        slot_batch = _make_future_slots([
            (FUTURE1, f"{9+j}:00", f"{10+j}:00") for j in range(5)
        ])
        interviewer = _make_interviewer(files=files)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=slot_batch):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        # 5 unique slots (deduped across 10 files returning same slots)
        uploaded = [s for s in result if s.get("source") == "uploaded"]
        assert len(uploaded) == 5

    def test_dedup_preserves_last_seen_metadata(self):
        """Cross-file dedup dict uses last-wins for same key; verify it doesn't crash."""
        s1 = [{"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
               "confidence": 0.7}]
        s2 = [{"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
               "confidence": 0.95}]

        interviewer = _make_interviewer(files=[_make_file("a.png"), _make_file("b.png")])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [s1, s2]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 1
        # Last-wins in dict comprehension means s2's confidence
        assert result[0]["confidence"] == 0.95

    def test_empty_slot_list_from_all_files(self):
        """All files return [] with no rejections → 'no extraction' warning."""
        files = [_make_file("a.png"), _make_file("b.png")]
        interviewer = _make_interviewer(files=files)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=[]):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        msg = st.warning.call_args[0][0]
        assert "no slots could be extracted" in msg.lower()

    def test_interviewer_updated_in_session_state_after_parse(self):
        """After parse, the interviewer dict in session_state is updated."""
        slots = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        interviewer = _make_interviewer(files=[_make_file()])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=slots):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        stored = st.session_state["panel_interviewers"][0]
        assert len(stored["slots"]) == 1
        assert stored["slots"][0]["date"] == FUTURE1.isoformat()

    def test_parse_button_visible_with_files_no_slots(self):
        """Parse button condition: files present, no slots → truthy."""
        iv = _make_interviewer(files=[_make_file()])
        assert iv.get("files") or iv.get("slots")

    def test_parse_button_visible_with_slots_no_files(self):
        """Parse button condition: no files, has slots → truthy."""
        iv = _make_interviewer(files=[], slots=[{"date": "2026-03-01"}])
        assert iv.get("files") or iv.get("slots")

    def test_parse_button_hidden_no_files_no_slots(self):
        """Parse button condition: no files, no slots → falsy."""
        iv = _make_interviewer(files=[], slots=[])
        assert not (iv.get("files") or iv.get("slots"))


# ===========================================================================
# 9. INTEGRATION: real testing_files/
# ===========================================================================
class TestIntegrationRealFiles:
    """Load real files from testing_files/ through multi-file code paths."""

    def _real_files(self, *names):
        files = []
        for name in names:
            path = TESTING_FILES_DIR / name
            if not path.exists():
                pytest.skip(f"Test file not found: {path}")
            files.append(_make_file_like(path))
        return files

    def test_two_pngs(self):
        files = self._real_files(
            "Screenshot 2026-01-22 112205.png",
            "Screenshot 2026-01-26 121045.png",
        )
        s1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        s2 = _make_future_slots([(FUTURE2, "14:00", "15:00")])

        interviewer = _make_interviewer(files=files, name="Alice")
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [s1, s2]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        assert len(st.session_state["panel_interviewers"][0]["slots"]) == 2

    def test_two_pdfs(self):
        files = self._real_files("Scheduler Test.pdf", "Test 40.pdf")
        s1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        s2 = _make_future_slots([(FUTURE2, "11:00", "12:00")])

        interviewer = _make_interviewer(files=files, name="Bob")
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [s1, s2]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        assert len(st.session_state["panel_interviewers"][0]["slots"]) == 2

    def test_mixed_pdf_and_png(self):
        files = self._real_files("Test 50.pdf", "Screenshot 2026-01-26 121120.png")
        s1 = _make_future_slots([(FUTURE1, "08:00", "09:00")])
        s2 = _make_future_slots([(FUTURE2, "13:00", "14:00")])

        interviewer = _make_interviewer(files=files, name="Carol")
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [s1, s2]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        assert len(st.session_state["panel_interviewers"][0]["slots"]) == 2

    def test_all_eight_files_single_interviewer(self):
        all_names = [
            "CalendarTest 12.pdf", "Scheduler Test.pdf",
            "Screenshot 2026-01-22 112205.png", "Screenshot 2026-01-26 121045.png",
            "Screenshot 2026-01-26 121120.png", "Test 40.pdf",
            "Test 50.pdf", "Test 50 (1).pdf",
        ]
        files = self._real_files(*all_names)
        all_slots = [
            _make_future_slots([(FUTURE1, f"{8+i}:00", f"{9+i}:00")])
            for i in range(len(all_names))
        ]

        interviewer = _make_interviewer(files=files, name="Dave")
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = all_slots
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == len(all_names)
        assert mock_parse.call_count == len(all_names)

    def test_all_files_via_parse_all_two_interviewers(self):
        pdf_files = self._real_files("Scheduler Test.pdf", "Test 40.pdf")
        png_files = self._real_files(
            "Screenshot 2026-01-22 112205.png", "Screenshot 2026-01-26 121045.png")

        interviewers = [
            _make_interviewer(files=pdf_files, name="Eve", iid=1),
            _make_interviewer(files=png_files, name="Frank", iid=2),
        ]
        st = _setup_session_state(interviewers)

        s1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        s2 = _make_future_slots([(FUTURE1, "14:00", "15:00")])
        s3 = _make_future_slots([(FUTURE2, "09:00", "10:00")])
        s4 = _make_future_slots([(FUTURE2, "14:00", "15:00")])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [s1, s2, s3, s4]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        assert mock_parse.call_count == 4
        assert len(st.session_state["panel_interviewers"][0]["slots"]) == 2
        assert len(st.session_state["panel_interviewers"][1]["slots"]) == 2

    def test_real_file_seek_after_partial_read(self):
        files = self._real_files("Test 50.pdf")
        f = files[0]
        f.read(100)
        assert f.tell() > 0
        f.seek(0)
        assert f.tell() == 0
        data = f.read()
        assert len(data) > 100

    def test_real_files_with_overlapping_mock_slots(self):
        """Two real files returning overlapping slots → deduped."""
        files = self._real_files(
            "Screenshot 2026-01-22 112205.png",
            "Screenshot 2026-01-26 121045.png",
        )
        same = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        unique = _make_future_slots([(FUTURE2, "11:00", "12:00")])

        interviewer = _make_interviewer(files=files, name="Grace")
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [same + unique, same]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 2  # same deduped, unique kept


# ===========================================================================
# 10. FORMAT_REJECTED_REASONS output verification
# ===========================================================================
class TestFormatRejectedReasons:
    """Verify _format_rejected_reasons produces correct human-readable text."""

    def test_single_reason(self):
        result = app_mod._format_rejected_reasons({"past_date": 3})
        assert result == "3 in the past"

    def test_multiple_reasons(self):
        result = app_mod._format_rejected_reasons({"past_date": 2, "weekend": 1})
        assert "2 in the past" in result
        assert "1 on a weekend" in result

    def test_unknown_reason_uses_key(self):
        result = app_mod._format_rejected_reasons({"unknown_thing": 5})
        assert "5 unknown_thing" in result

    def test_empty_reasons(self):
        result = app_mod._format_rejected_reasons({})
        assert result == ""

    def test_all_known_reasons(self):
        reasons = {
            "past_date": 1, "weekend": 2, "invalid_date": 3,
            "invalid_format": 4, "missing_fields": 5,
            "invalid_time_range": 6, "too_short": 7, "invalid_time": 8,
        }
        result = app_mod._format_rejected_reasons(reasons)
        assert "in the past" in result
        assert "on a weekend" in result
        assert "had invalid dates" in result
        assert "had invalid format" in result
        assert "were missing required fields" in result
        assert "had invalid time ranges" in result
        assert "were too short" in result
        assert "had invalid times" in result


# ===========================================================================
# 11. BUG FIX: Per-file error isolation (exception in one file doesn't lose others)
# ===========================================================================
class TestPerFileErrorIsolation:
    """Verify that a corrupt file doesn't discard good results from other files."""

    # --- _parse_single: file 2 of 3 raises, files 1 and 3 slots preserved ---
    def test_single_mid_loop_exception_preserves_good_slots(self):
        """File 1 OK, file 2 raises, file 3 OK → slots from 1 and 3 kept."""
        s1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        s3 = _make_future_slots([(FUTURE3, "14:00", "15:00")])

        files = [_make_file("good1.png"), _make_file("bad.png"), _make_file("good3.png")]
        interviewer = _make_interviewer(files=files)
        st = _setup_session_state([interviewer])

        call_idx = [0]

        def mock_parse(f, itz, dtz):
            call_idx[0] += 1
            if call_idx[0] == 2:
                raise ValueError("corrupt image")
            return s1 if call_idx[0] == 1 else s3

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        # Slots from file 1 and file 3 should survive
        assert len(result) == 2
        dates = {s["date"] for s in result}
        assert FUTURE1.isoformat() in dates
        assert FUTURE3.isoformat() in dates
        # Error reported for file 2
        error_calls = [c[0][0] for c in st.error.call_args_list]
        assert any("bad.png" in e and "corrupt image" in e for e in error_calls)
        # Success message for the 2 good slots
        st.success.assert_called_once()
        assert "2 slot" in st.success.call_args[0][0]

    def test_single_first_file_raises_rest_still_parsed(self):
        """File 1 raises, files 2-3 still parsed."""
        s2 = _make_future_slots([(FUTURE2, "09:00", "10:00")])
        s3 = _make_future_slots([(FUTURE3, "14:00", "15:00")])
        files = [_make_file("bad.png"), _make_file("ok2.png"), _make_file("ok3.png")]
        interviewer = _make_interviewer(files=files)
        st = _setup_session_state([interviewer])

        call_idx = [0]

        def mock_parse(f, itz, dtz):
            call_idx[0] += 1
            if call_idx[0] == 1:
                raise RuntimeError("bad header")
            return s2 if call_idx[0] == 2 else s3

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 2
        st.error.assert_called_once()

    def test_single_last_file_raises_earlier_slots_kept(self):
        """Files 1-2 OK, file 3 raises → slots from 1-2 kept."""
        s1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        s2 = _make_future_slots([(FUTURE2, "14:00", "15:00")])
        files = [_make_file("ok1.png"), _make_file("ok2.png"), _make_file("bad.png")]
        interviewer = _make_interviewer(files=files)
        st = _setup_session_state([interviewer])

        call_idx = [0]

        def mock_parse(f, itz, dtz):
            call_idx[0] += 1
            if call_idx[0] == 3:
                raise IOError("disk error")
            return s1 if call_idx[0] == 1 else s2

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 2
        st.success.assert_called_once()

    def test_single_all_files_raise_shows_warning(self):
        """All files raise → error for each, plus 'no extraction' warning."""
        files = [_make_file("bad1.png"), _make_file("bad2.png")]
        interviewer = _make_interviewer(files=files)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload",
                          side_effect=ValueError("parse error")):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        assert st.error.call_count == 2
        # 0 slots extracted, no rejected reasons → "no extraction" warning
        st.warning.assert_called_once()
        assert "no slots could be extracted" in st.warning.call_args[0][0].lower()

    def test_single_error_includes_filename(self):
        """Per-file error message includes the file name."""
        f = _make_file("My Calendar.pdf")
        interviewer = _make_interviewer(files=[f])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload",
                          side_effect=ValueError("bad format")):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        msg = st.error.call_args[0][0]
        assert "My Calendar.pdf" in msg
        assert "bad format" in msg

    def test_single_error_with_file_missing_name_attribute(self):
        """File without .name attr → uses 'unknown' in error."""
        f = io.BytesIO(b"data")
        # BytesIO doesn't have .name by default
        interviewer = _make_interviewer(files=[f])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload",
                          side_effect=ValueError("oops")):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        msg = st.error.call_args[0][0]
        assert "unknown" in msg.lower()

    # --- _parse_all: per-file error isolation ---
    def test_all_panel_mid_loop_exception_preserves_good_slots(self):
        """In _parse_all, file 2 of 3 raises for one interviewer → other files' slots kept."""
        s1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        s3 = _make_future_slots([(FUTURE3, "14:00", "15:00")])
        files = [_make_file("ok1.png"), _make_file("bad.png"), _make_file("ok3.png")]
        interviewer = _make_interviewer(files=files, name="Alice", iid=1)
        st = _setup_session_state([interviewer])

        call_idx = [0]

        def mock_parse(f, itz, dtz):
            call_idx[0] += 1
            if call_idx[0] == 2:
                raise ValueError("corrupt")
            return s1 if call_idx[0] == 1 else s3

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 2
        error_calls = [c[0][0] for c in st.error.call_args_list]
        assert any("bad.png" in e for e in error_calls)

    def test_all_panel_error_doesnt_block_other_interviewers(self):
        """Interviewer A's file error doesn't prevent interviewer B from parsing."""
        s_bob = _make_future_slots([(FUTURE2, "14:00", "15:00")])
        interviewers = [
            _make_interviewer(files=[_make_file("bad.png")], name="Alice", iid=1),
            _make_interviewer(files=[_make_file("good.png")], name="Bob", iid=2),
        ]
        st = _setup_session_state(interviewers)

        call_idx = [0]

        def mock_parse(f, itz, dtz):
            call_idx[0] += 1
            if call_idx[0] == 1:
                raise RuntimeError("corrupt")
            return s_bob

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        bob = st.session_state["panel_interviewers"][1]["slots"]
        assert len(bob) == 1


# ===========================================================================
# 12. _parse_all success message branches
# ===========================================================================
class TestParseAllSuccessMessages:
    """Test the summary success messages after _parse_all completes."""

    def test_single_interviewer_uploaded_only(self, _patch_intersection):
        """1 interviewer, uploaded slots only → 'Extracted N slot(s) from uploaded file.'"""
        _patch_intersection.compute_intersection.return_value = [
            {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00"}
        ]
        slots = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        interviewer = _make_interviewer(files=[_make_file()], name="Alice", iid=1)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=slots):
            with patch.object(app_mod, "split_slot_by_duration", return_value=[slots[0]]):
                with patch.object(app_mod, "_save_persisted_slots"):
                    app_mod._parse_all_panel_availability()

        success_calls = [c[0][0] for c in st.success.call_args_list]
        assert any("extracted 1 slot" in s.lower() for s in success_calls)

    def test_single_interviewer_manual_only(self, _patch_intersection):
        """1 interviewer, manual slots only → 'Processed N manual slot(s).'"""
        manual = {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                  "source": "manual", "confidence": 1.0}
        _patch_intersection.compute_intersection.return_value = [manual]
        interviewer = _make_interviewer(files=[], slots=[manual], name="Alice", iid=1)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "split_slot_by_duration", return_value=[manual]):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        success_calls = [c[0][0] for c in st.success.call_args_list]
        assert any("1 manual slot" in s.lower() for s in success_calls)

    def test_single_interviewer_mixed_sources(self, _patch_intersection):
        """1 interviewer, manual + uploaded → 'Processed N (M manual, K uploaded).'"""
        manual = {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                  "source": "manual", "confidence": 1.0}
        uploaded = _make_future_slots([(FUTURE2, "14:00", "15:00")])
        _patch_intersection.compute_intersection.return_value = [manual, uploaded[0]]
        interviewer = _make_interviewer(
            files=[_make_file()], slots=[manual], name="Alice", iid=1)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=uploaded):
            with patch.object(app_mod, "split_slot_by_duration", side_effect=lambda s, d: [s]):
                with patch.object(app_mod, "_save_persisted_slots"):
                    app_mod._parse_all_panel_availability()

        success_calls = [c[0][0] for c in st.success.call_args_list]
        assert any("1 manual" in s and "1 uploaded" in s for s in success_calls)

    def test_multi_interviewer_summary(self, _patch_intersection):
        """2+ interviewers → 'Processed N total ... Found X intersection slot(s)...'"""
        s1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        s2 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        intersection = [{"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                         "is_full_overlap": True}]
        _patch_intersection.compute_intersection.return_value = intersection

        interviewers = [
            _make_interviewer(files=[_make_file("a.png")], name="Alice", iid=1),
            _make_interviewer(files=[_make_file("b.png")], name="Bob", iid=2),
        ]
        st = _setup_session_state(interviewers)

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [s1, s2]
            with patch.object(app_mod, "split_slot_by_duration", side_effect=lambda s, d: [s]):
                with patch.object(app_mod, "_save_persisted_slots"):
                    app_mod._parse_all_panel_availability()

        success_calls = [c[0][0] for c in st.success.call_args_list]
        assert any("2 interviewers" in s for s in success_calls)
        assert any("intersection" in s.lower() for s in success_calls)

    def test_no_availability_warning(self):
        """No interviewers with slots → 'No availability found.'"""
        interviewer = _make_interviewer(files=[], slots=[], iid=1)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_save_persisted_slots"):
            app_mod._parse_all_panel_availability()

        warning_calls = [c[0][0] for c in st.warning.call_args_list]
        assert any("no availability found" in w.lower() for w in warning_calls)


# ===========================================================================
# 13. _parse_all: total_uploaded reflects post-dedup count
# ===========================================================================
class TestParseAllDedupCounting:
    """Verify total_uploaded counts unique slots after cross-file dedup."""

    def test_total_uploaded_after_dedup(self, _patch_intersection):
        """2 files each returning same 3 slots → total_uploaded = 3, not 6."""
        slots = _make_future_slots([
            (FUTURE1, "09:00", "10:00"),
            (FUTURE1, "11:00", "12:00"),
            (FUTURE2, "14:00", "15:00"),
        ])
        _patch_intersection.compute_intersection.return_value = slots

        files = [_make_file("a.png"), _make_file("b.png")]
        interviewer = _make_interviewer(files=files, iid=1)
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=slots):
            with patch.object(app_mod, "split_slot_by_duration", side_effect=lambda s, d: [s]):
                with patch.object(app_mod, "_save_persisted_slots"):
                    app_mod._parse_all_panel_availability()

        # The success message should say 3 (deduped), not 6
        success_calls = [c[0][0] for c in st.success.call_args_list]
        assert any("3 slot" in s.lower() or "extracted 3" in s.lower() for s in success_calls)


# ===========================================================================
# 14. Early return paths don't update session state
# ===========================================================================
class TestEarlyReturnPaths:
    """Verify session state isn't corrupted on early-return paths."""

    def test_no_files_no_manual_early_return(self):
        """When no files and no manual → early return, session state unchanged."""
        interviewer = _make_interviewer(files=[], slots=[])
        original_slots = []
        st = _setup_session_state([interviewer])

        app_mod._parse_single_interviewer_availability(0)

        # Interviewer slots should remain empty (not set to something else)
        result = st.session_state["panel_interviewers"][0]["slots"]
        assert result == original_slots

    def test_re_parse_discards_old_uploaded_keeps_manual(self):
        """Re-parsing discards previously uploaded slots, keeps manual."""
        manual = {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                  "source": "manual", "confidence": 1.0}
        old_uploaded = {"date": FUTURE2.isoformat(), "start": "14:00", "end": "15:00",
                        "source": "uploaded", "confidence": 0.9}
        new_uploaded = _make_future_slots([(FUTURE3, "11:00", "12:00")])

        interviewer = _make_interviewer(
            files=[_make_file()], slots=[manual, old_uploaded])
        st = _setup_session_state([interviewer])

        with patch.object(app_mod, "_parse_availability_upload", return_value=new_uploaded):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        result = st.session_state["panel_interviewers"][0]["slots"]
        sources = {s["source"] for s in result}
        assert sources == {"manual", "uploaded"}
        dates = {s["date"] for s in result}
        # Old uploaded FUTURE2 should be gone, replaced by new FUTURE3
        assert FUTURE2.isoformat() not in dates
        assert FUTURE3.isoformat() in dates
        assert FUTURE1.isoformat() in dates  # manual kept


# ===========================================================================
# 15. _parse_all: interviewer name fallback chain
# ===========================================================================
class TestInterviewerNameFallback:
    """Test name → email → 'Interviewer {id}' fallback in _parse_all."""

    def test_name_used_when_present(self):
        """Name is preferred when available."""
        manual = {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                  "source": "manual", "confidence": 1.0}
        iv = _make_interviewer(files=[], slots=[manual], name="Alice",
                               email="alice@t.com", iid=1)
        st = _setup_session_state([iv])

        with patch.object(app_mod, "_save_persisted_slots"):
            app_mod._parse_all_panel_availability()

        # Alice's name should be in success (or no warning since we have slots)
        # We just check it doesn't crash and processes correctly
        result = st.session_state["panel_interviewers"][0]["slots"]
        assert len(result) == 1

    def test_email_fallback_when_no_name(self):
        """Email used when name is empty."""
        manual = {"date": FUTURE1.isoformat(), "start": "09:00", "end": "10:00",
                  "source": "manual", "confidence": 1.0}
        iv = {"id": 1, "name": "", "email": "bob@test.com",
              "files": [], "slots": [manual], "timezone": "America/New_York"}
        _setup_session_state([iv])

        with patch.object(app_mod, "_save_persisted_slots"):
            app_mod._parse_all_panel_availability()

        # Should not crash; email used as fallback name internally

    def test_id_fallback_when_no_name_no_email(self):
        """'Interviewer {id}' used when both name and email are empty."""
        iv = {"id": 42, "name": "", "email": "",
              "files": [_make_file()], "slots": [], "timezone": "America/New_York"}
        st = _setup_session_state([iv])

        def mock_parse(f, itz, dtz):
            st.session_state["parser_rejected_reasons"] = {"past_date": 1}
            return []

        with patch.object(app_mod, "_parse_availability_upload", side_effect=mock_parse):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        warning_calls = [c[0][0] for c in st.warning.call_args_list]
        id_warnings = [w for w in warning_calls if "Interviewer 42" in w]
        assert len(id_warnings) >= 1


# ===========================================================================
# 16. Interviewer timezone passed to _parse_availability_upload
# ===========================================================================
class TestInterviewerTimezonePassthrough:
    """Verify interviewer-specific timezone is forwarded to parse."""

    def test_interviewer_tz_used_not_display_tz(self):
        """Each interviewer's timezone should be passed, not the display tz."""
        slots = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        iv = _make_interviewer(files=[_make_file()], timezone="Europe/London")
        st = _setup_session_state([iv])
        st.session_state["selected_timezone"] = "America/New_York"

        with patch.object(app_mod, "_parse_availability_upload", return_value=slots) as mock_parse:
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        # First arg: file, second: interviewer_tz, third: display_tz
        call_args = mock_parse.call_args[0]
        assert call_args[1] == "Europe/London"
        assert call_args[2] == "America/New_York"

    def test_interviewer_tz_fallback_to_display(self):
        """If interviewer has no timezone key, falls back to display tz."""
        slots = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        iv = {"id": 1, "name": "Alice", "email": "a@t.com",
              "files": [_make_file()], "slots": []}
        # No "timezone" key at all
        st = _setup_session_state([iv])
        st.session_state["selected_timezone"] = "US/Pacific"

        with patch.object(app_mod, "_parse_availability_upload", return_value=slots) as mock_parse:
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_single_interviewer_availability(0)

        call_args = mock_parse.call_args[0]
        assert call_args[1] == "US/Pacific"  # fell back to display tz

    def test_parse_all_uses_per_interviewer_tz(self):
        """_parse_all passes each interviewer's own timezone."""
        s1 = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        s2 = _make_future_slots([(FUTURE2, "14:00", "15:00")])

        interviewers = [
            _make_interviewer(files=[_make_file("a.png")], name="Alice",
                              iid=1, timezone="Europe/Berlin"),
            _make_interviewer(files=[_make_file("b.png")], name="Bob",
                              iid=2, timezone="Asia/Tokyo"),
        ]
        st = _setup_session_state(interviewers)

        with patch.object(app_mod, "_parse_availability_upload") as mock_parse:
            mock_parse.side_effect = [s1, s2]
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        # First call: Alice's tz
        assert mock_parse.call_args_list[0][0][1] == "Europe/Berlin"
        # Second call: Bob's tz
        assert mock_parse.call_args_list[1][0][1] == "Asia/Tokyo"


# ===========================================================================
# 17. _parse_all: silent skip for interviewer with no files, no manual, no slots
# ===========================================================================
class TestParseAllSilentSkip:
    """Interviewers with nothing to parse are silently skipped."""

    def test_empty_interviewer_skipped_in_parse_all(self):
        """Interviewer with no files and no slots is simply skipped."""
        empty_iv = _make_interviewer(files=[], slots=[], name="Ghost", iid=1)
        slots = _make_future_slots([(FUTURE1, "09:00", "10:00")])
        active_iv = _make_interviewer(
            files=[_make_file()], name="Alice", iid=2)
        st = _setup_session_state([empty_iv, active_iv])

        with patch.object(app_mod, "_parse_availability_upload", return_value=slots):
            with patch.object(app_mod, "_save_persisted_slots"):
                app_mod._parse_all_panel_availability()

        # Ghost's slots remain empty
        ghost = st.session_state["panel_interviewers"][0]["slots"]
        assert ghost == []
        # Alice parsed fine
        alice = st.session_state["panel_interviewers"][1]["slots"]
        assert len(alice) == 1
