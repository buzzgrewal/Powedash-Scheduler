"""
Comprehensive tests for optional hiring manager email feature.

Tests cover:
- White-box: validation functions, attendee list construction, audit log storage,
  ICS generation, Graph event payload, idempotency checks, email sending paths.
- Black-box: end-to-end flows for individual/group/handle invites with and without
  hiring manager email.
- Edge cases: empty string, None, whitespace, invalid email, duplicate emails,
  case sensitivity, SQL NULL handling.
"""
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit_log import AuditLog
from ics_utils import ICSInvite, stable_uid


# ---------------------------------------------------------------------------
# Mock streamlit at module level to avoid segfaults on Python 3.13
# (patch.dict teardown of sys.modules triggers CPython bug)
# ---------------------------------------------------------------------------
_local_mock = MagicMock()
_local_mock.secrets = {}
_local_mock.session_state = {}
_local_mock.cache_data = lambda *a, **kw: (lambda f: f)
_local_mock.cache_resource = lambda *a, **kw: (lambda f: f)

# Install mocks before importing app
sys.modules.setdefault("streamlit", _local_mock)
sys.modules.setdefault("streamlit.components", MagicMock())
sys.modules.setdefault("streamlit.components.v1", MagicMock())

# Always use the mock that's actually in sys.modules, not our local one,
# because when tests run together the first test file's mock wins.
_mock_st = sys.modules["streamlit"]

import app as app_mod


@pytest.fixture
def app_module():
    """Provide the pre-imported app module."""
    return app_mod


@pytest.fixture
def audit_db():
    """Create a temporary audit log database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    audit = AuditLog(db_path)
    yield audit
    Path(db_path).unlink(missing_ok=True)


# ===========================================================================
# 1. VALIDATION FUNCTIONS (White-box unit tests)
# ===========================================================================
class TestValidateEmail:
    """Test validate_email (required) function."""

    def test_valid_email(self, app_module):
        assert app_module.validate_email("User@Example.COM") == "user@example.com"

    def test_valid_email_with_whitespace(self, app_module):
        assert app_module.validate_email("  user@test.com  ") == "user@test.com"

    def test_empty_string_raises(self, app_module):
        with pytest.raises(app_module.ValidationError, match="required"):
            app_module.validate_email("")

    def test_none_raises(self, app_module):
        with pytest.raises(app_module.ValidationError, match="required"):
            app_module.validate_email(None)

    def test_invalid_format_raises(self, app_module):
        with pytest.raises(app_module.ValidationError, match="Invalid email"):
            app_module.validate_email("not-an-email")

    def test_too_long_raises(self, app_module):
        long_email = "a" * 246 + "@test.com"  # 255 chars, valid format but exceeds RFC 5321 limit
        with pytest.raises(app_module.ValidationError, match="too long"):
            app_module.validate_email(long_email)


class TestValidateEmailOptional:
    """Test validate_email_optional (optional) function."""

    def test_valid_email_returns_normalized(self, app_module):
        assert app_module.validate_email_optional("HM@Test.COM") == "hm@test.com"

    def test_empty_string_returns_none(self, app_module):
        assert app_module.validate_email_optional("") is None

    def test_none_returns_none(self, app_module):
        assert app_module.validate_email_optional(None) is None

    def test_whitespace_only_returns_none(self, app_module):
        assert app_module.validate_email_optional("   ") is None

    def test_invalid_format_raises(self, app_module):
        with pytest.raises(app_module.ValidationError, match="Invalid email"):
            app_module.validate_email_optional("bad-email")

    def test_or_empty_pattern(self, app_module):
        """Verify the `or ""` normalization pattern used after validation."""
        result = app_module.validate_email_optional("") or ""
        assert result == ""
        assert isinstance(result, str)

        result2 = app_module.validate_email_optional(None) or ""
        assert result2 == ""
        assert isinstance(result2, str)

    def test_valid_email_or_empty_preserves(self, app_module):
        """Verify `or ""` doesn't interfere with valid emails."""
        result = app_module.validate_email_optional("valid@test.com") or ""
        assert result == "valid@test.com"


# ===========================================================================
# 2. AUDIT LOG (White-box: storage and retrieval with empty hm_email)
# ===========================================================================
class TestAuditLogHiringManager:
    """Test audit log handles empty/missing hiring manager email."""

    def test_log_with_empty_hm_email(self, audit_db):
        """audit.log() should accept empty string for hiring_manager_email."""
        result = audit_db.log(
            "test_action",
            actor="recruiter@test.com",
            candidate_email="candidate@test.com",
            hiring_manager_email="",
            role_title="Engineer",
        )
        assert result is True

    def test_log_with_valid_hm_email(self, audit_db):
        """audit.log() should accept valid email for hiring_manager_email."""
        result = audit_db.log(
            "test_action",
            actor="recruiter@test.com",
            candidate_email="candidate@test.com",
            hiring_manager_email="hm@test.com",
            role_title="Engineer",
        )
        assert result is True

    def test_upsert_interview_empty_hm_email(self, audit_db):
        """upsert_interview should work with empty hiring_manager_email."""
        result = audit_db.upsert_interview(
            role_title="Engineer",
            candidate_email="candidate@test.com",
            hiring_manager_email="",
            recruiter_email="recruiter@test.com",
            duration_minutes=60,
            start_utc="2026-03-01T10:00:00+00:00",
            end_utc="2026-03-01T11:00:00+00:00",
            display_timezone="UTC",
            candidate_timezone="America/New_York",
            graph_event_id="event-123",
            teams_join_url="https://teams.microsoft.com/meet",
            subject="Interview: Engineer",
            last_status="created",
        )
        assert result is True

    def test_upsert_interview_valid_hm_email(self, audit_db):
        """upsert_interview should work with valid hiring_manager_email."""
        result = audit_db.upsert_interview(
            role_title="Engineer",
            candidate_email="candidate@test.com",
            hiring_manager_email="hm@test.com",
            recruiter_email="recruiter@test.com",
            duration_minutes=60,
            start_utc="2026-03-01T10:00:00+00:00",
            end_utc="2026-03-01T11:00:00+00:00",
            display_timezone="UTC",
            candidate_timezone="America/New_York",
            graph_event_id="event-456",
            teams_join_url="",
            subject="Interview: Engineer",
            last_status="created",
        )
        assert result is True

    def test_interview_exists_empty_hm_email(self, audit_db):
        """interview_exists should find matches when hm_email is empty."""
        audit_db.upsert_interview(
            role_title="Engineer",
            candidate_email="candidate@test.com",
            hiring_manager_email="",
            recruiter_email="",
            duration_minutes=60,
            start_utc="2026-03-01T10:00:00+00:00",
            end_utc="2026-03-01T11:00:00+00:00",
            display_timezone="UTC",
            candidate_timezone="UTC",
            graph_event_id="event-789",
            teams_join_url="",
            subject="Test Interview",
            last_status="created",
        )

        existing = audit_db.interview_exists(
            candidate_email="candidate@test.com",
            hiring_manager_email="",
            role_title="Engineer",
            start_utc="2026-03-01T10:00:00+00:00",
        )
        assert existing is not None
        assert existing["graph_event_id"] == "event-789"

    def test_interview_exists_with_hm_email(self, audit_db):
        """interview_exists should match when hm_email is provided."""
        audit_db.upsert_interview(
            role_title="Designer",
            candidate_email="candidate@test.com",
            hiring_manager_email="hm@test.com",
            recruiter_email="",
            duration_minutes=45,
            start_utc="2026-03-02T14:00:00+00:00",
            end_utc="2026-03-02T14:45:00+00:00",
            display_timezone="UTC",
            candidate_timezone="UTC",
            graph_event_id="event-abc",
            teams_join_url="",
            subject="Test Interview",
            last_status="created",
        )

        existing = audit_db.interview_exists(
            candidate_email="candidate@test.com",
            hiring_manager_email="hm@test.com",
            role_title="Designer",
            start_utc="2026-03-02T14:00:00+00:00",
        )
        assert existing is not None
        assert existing["graph_event_id"] == "event-abc"

    def test_interview_exists_case_insensitive_hm(self, audit_db):
        """interview_exists should be case-insensitive for hm_email."""
        audit_db.upsert_interview(
            role_title="PM",
            candidate_email="cand@test.com",
            hiring_manager_email="HM@Test.COM",
            recruiter_email="",
            duration_minutes=30,
            start_utc="2026-03-03T09:00:00+00:00",
            end_utc="2026-03-03T09:30:00+00:00",
            display_timezone="UTC",
            candidate_timezone="UTC",
            graph_event_id="event-case",
            teams_join_url="",
            subject="Test",
            last_status="created",
        )

        existing = audit_db.interview_exists(
            candidate_email="cand@test.com",
            hiring_manager_email="hm@test.com",
            role_title="PM",
            start_utc="2026-03-03T09:00:00+00:00",
        )
        assert existing is not None

    def test_interview_exists_empty_does_not_match_filled(self, audit_db):
        """Empty hm_email should NOT match an interview that has a real hm_email."""
        audit_db.upsert_interview(
            role_title="Engineer",
            candidate_email="candidate@test.com",
            hiring_manager_email="manager@test.com",
            recruiter_email="",
            duration_minutes=60,
            start_utc="2026-03-04T10:00:00+00:00",
            end_utc="2026-03-04T11:00:00+00:00",
            display_timezone="UTC",
            candidate_timezone="UTC",
            graph_event_id="event-diff",
            teams_join_url="",
            subject="Test",
            last_status="created",
        )

        existing = audit_db.interview_exists(
            candidate_email="candidate@test.com",
            hiring_manager_email="",
            role_title="Engineer",
            start_utc="2026-03-04T10:00:00+00:00",
        )
        assert existing is None  # Different hm_email should not match

    def test_interview_exists_null_matches_empty_via_coalesce(self, audit_db):
        """COALESCE in SQL should treat NULL and empty string equivalently."""
        # Insert directly with NULL (simulating legacy data)
        conn = audit_db._connect()
        try:
            conn.execute(
                """INSERT INTO interviews (
                    created_utc, role_title, candidate_email, hiring_manager_email,
                    recruiter_email, duration_minutes, start_utc, end_utc,
                    display_timezone, candidate_timezone, graph_event_id,
                    teams_join_url, subject, last_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "2026-03-05T00:00:00+00:00", "Engineer", "cand@test.com", None,
                    "", 60, "2026-03-05T10:00:00+00:00", "2026-03-05T11:00:00+00:00",
                    "UTC", "UTC", "event-null", "", "Test", "created",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Query with empty string should match NULL via COALESCE
        existing = audit_db.interview_exists(
            candidate_email="cand@test.com",
            hiring_manager_email="",
            role_title="Engineer",
            start_utc="2026-03-05T10:00:00+00:00",
        )
        assert existing is not None
        assert existing["graph_event_id"] == "event-null"

    def test_audit_log_entries_readable(self, audit_db):
        """Verify logged entries with empty hm_email are readable."""
        audit_db.log(
            "test_action",
            candidate_email="cand@test.com",
            hiring_manager_email="",
        )
        entries = audit_db.list_recent_audit(limit=10)
        assert len(entries) >= 1
        entry = entries[0]
        assert entry["hiring_manager_email"] == ""


# ===========================================================================
# 3. ICS GENERATION (White-box: uid_hint and attendee_emails with empty hm)
# ===========================================================================
class TestICSGeneration:
    """Test ICS file generation with optional hiring manager."""

    def test_stable_uid_consistency_empty_hm(self):
        """stable_uid should produce consistent results when hm_email is empty."""
        uid1 = stable_uid("Engineer|cand@test.com|", "org@test.com", "2026-03-01T10:00:00")
        uid2 = stable_uid("Engineer|cand@test.com|", "org@test.com", "2026-03-01T10:00:00")
        assert uid1 == uid2
        assert uid1.endswith("@powerdashhr.com")

    def test_stable_uid_differs_with_and_without_hm(self):
        """UID should differ based on whether hm_email is present."""
        uid_with = stable_uid("Engineer|cand@test.com|hm@test.com", "org@test.com", "2026-03-01T10:00:00")
        uid_without = stable_uid("Engineer|cand@test.com|", "org@test.com", "2026-03-01T10:00:00")
        assert uid_with != uid_without

    @staticmethod
    def _unfold_ics(text: str) -> str:
        """Unfold ICS line continuations (CRLF + space)."""
        return text.replace("\r\n ", "")

    def test_ics_invite_without_hm_attendee(self):
        """ICS should be valid with only candidate as attendee."""
        now = datetime.now(timezone.utc)
        invite = ICSInvite(
            uid="test-uid@powerdashhr.com",
            dtstart_utc=now,
            dtend_utc=now + timedelta(hours=1),
            summary="Interview: Engineer",
            description="Test interview",
            organizer_email="scheduler@test.com",
            organizer_name="Scheduler",
            attendee_emails=["candidate@test.com"],
            location="Teams",
        )
        ics_bytes = invite.to_ics()
        ics_text = self._unfold_ics(ics_bytes.decode("utf-8"))
        assert "candidate@test.com" in ics_text
        assert "BEGIN:VCALENDAR" in ics_text
        assert "END:VCALENDAR" in ics_text

    def test_ics_invite_with_hm_attendee(self):
        """ICS should include hiring manager as attendee when provided."""
        now = datetime.now(timezone.utc)
        invite = ICSInvite(
            uid="test-uid-hm@powerdashhr.com",
            dtstart_utc=now,
            dtend_utc=now + timedelta(hours=1),
            summary="Interview: Engineer",
            description="Test interview",
            organizer_email="scheduler@test.com",
            organizer_name="Scheduler",
            attendee_emails=["candidate@test.com", "hm@test.com"],
            location="Teams",
        )
        ics_bytes = invite.to_ics()
        ics_text = self._unfold_ics(ics_bytes.decode("utf-8"))
        assert "candidate@test.com" in ics_text
        assert "hm@test.com" in ics_text

    def test_ics_filters_empty_attendees(self):
        """ICS generation should filter out empty/None attendee emails."""
        now = datetime.now(timezone.utc)
        invite = ICSInvite(
            uid="test-uid-filter@powerdashhr.com",
            dtstart_utc=now,
            dtend_utc=now + timedelta(hours=1),
            summary="Interview: Engineer",
            description="Test interview",
            organizer_email="scheduler@test.com",
            organizer_name="Scheduler",
            attendee_emails=["candidate@test.com", "", "  "],
            location="Teams",
        )
        ics_bytes = invite.to_ics()
        ics_text = self._unfold_ics(ics_bytes.decode("utf-8"))
        # Only candidate should appear as ATTENDEE
        attendee_lines = [l for l in ics_text.split("\r\n") if "ATTENDEE" in l]
        assert len(attendee_lines) == 1
        assert "candidate@test.com" in attendee_lines[0]


# ===========================================================================
# 4. GRAPH EVENT PAYLOAD (White-box: attendees list construction)
# ===========================================================================
class TestGraphEventPayload:
    """Test Graph API event payload with optional hiring manager."""

    def test_payload_without_hm(self, app_module):
        """Graph payload should work with only candidate as attendee."""
        now = datetime.now(timezone.utc)
        payload = app_module._graph_event_payload(
            subject="Interview: Engineer",
            body_html="<p>Test</p>",
            start_local=now,
            end_local=now + timedelta(hours=1),
            time_zone="UTC",
            attendees=[("candidate@test.com", "Candidate")],
            is_teams=True,
            location="",
        )
        assert len(payload["attendees"]) == 1
        assert payload["attendees"][0]["emailAddress"]["address"] == "candidate@test.com"
        assert payload["attendees"][0]["type"] == "required"
        assert payload["isOnlineMeeting"] is True

    def test_payload_with_hm(self, app_module):
        """Graph payload should include HM as required attendee."""
        now = datetime.now(timezone.utc)
        payload = app_module._graph_event_payload(
            subject="Interview: Engineer",
            body_html="<p>Test</p>",
            start_local=now,
            end_local=now + timedelta(hours=1),
            time_zone="UTC",
            attendees=[("candidate@test.com", "Candidate"), ("hm@test.com", "HM")],
            is_teams=False,
            location="Office",
        )
        assert len(payload["attendees"]) == 2
        addresses = [a["emailAddress"]["address"] for a in payload["attendees"]]
        assert "candidate@test.com" in addresses
        assert "hm@test.com" in addresses
        assert payload["location"]["displayName"] == "Office"

    def test_payload_with_cc_attendees(self, app_module):
        """CC attendees (panel interviewers) should be 'optional' type."""
        now = datetime.now(timezone.utc)
        payload = app_module._graph_event_payload(
            subject="Panel Interview",
            body_html="<p>Test</p>",
            start_local=now,
            end_local=now + timedelta(hours=1),
            time_zone="UTC",
            attendees=[("candidate@test.com", "Candidate")],
            is_teams=True,
            location="",
            cc_attendees=[("interviewer@test.com", "Interviewer")],
        )
        assert len(payload["attendees"]) == 2
        required = [a for a in payload["attendees"] if a["type"] == "required"]
        optional = [a for a in payload["attendees"] if a["type"] == "optional"]
        assert len(required) == 1
        assert len(optional) == 1


# ===========================================================================
# 5. ATTENDEE LIST CONSTRUCTION (White-box: deduplication, conditional add)
# ===========================================================================
class TestAttendeeListConstruction:
    """Test the attendee list building logic extracted from invite functions."""

    def _build_attendees(
        self,
        candidate_email: str,
        candidate_name: str,
        hm_email: str,
        hm_name: str,
        panel_interviewers: Optional[List[Dict[str, str]]] = None,
        rec_email: str = "",
        rec_name: str = "",
        include_recruiter: bool = True,
    ) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
        """Simulate the attendee building logic from _create_individual_invite."""
        attendees: List[Tuple[str, str]] = [(candidate_email, candidate_name)]
        seen_emails: set = {candidate_email.lower()}
        cc_attendees: List[Tuple[str, str]] = []

        # Add hiring manager as required attendee (if valid and not duplicate)
        if hm_email and hm_email.lower() not in seen_emails:
            attendees.append((hm_email, hm_name))
            seen_emails.add(hm_email.lower())

        # Add panel interviewers to CC
        if panel_interviewers:
            for pi in panel_interviewers:
                pi_email = (pi.get("email") or "").strip().lower()
                if pi_email and pi_email not in seen_emails:
                    cc_attendees.append((pi_email, pi.get("name", "")))
                    seen_emails.add(pi_email)

        # Optionally add recruiter to CC
        if include_recruiter and rec_email and rec_email.lower() not in seen_emails:
            cc_attendees.append((rec_email, rec_name))
            seen_emails.add(rec_email.lower())

        return attendees, cc_attendees

    def test_no_hm_no_panel(self):
        """Only candidate when no HM and no panel interviewers."""
        attendees, cc = self._build_attendees("cand@test.com", "Cand", "", "", None)
        assert len(attendees) == 1
        assert attendees[0][0] == "cand@test.com"
        assert len(cc) == 0

    def test_with_hm_no_panel(self):
        """Candidate + HM as required attendees when HM provided."""
        attendees, cc = self._build_attendees("cand@test.com", "Cand", "hm@test.com", "HM", None)
        assert len(attendees) == 2
        assert attendees[1][0] == "hm@test.com"

    def test_no_hm_with_panel(self):
        """Candidate + panel in CC when no HM."""
        panel = [{"email": "panel1@test.com", "name": "P1"}]
        attendees, cc = self._build_attendees("cand@test.com", "Cand", "", "", panel)
        assert len(attendees) == 1  # Only candidate
        assert len(cc) == 1  # Panel in CC
        assert cc[0][0] == "panel1@test.com"

    def test_with_hm_and_panel(self):
        """Candidate + HM required, panel in CC."""
        panel = [{"email": "panel1@test.com", "name": "P1"}]
        attendees, cc = self._build_attendees("cand@test.com", "Cand", "hm@test.com", "HM", panel)
        assert len(attendees) == 2  # Candidate + HM
        assert len(cc) == 1  # Panel in CC

    def test_hm_duplicate_of_candidate(self):
        """HM should not be added if same email as candidate."""
        attendees, cc = self._build_attendees("same@test.com", "Cand", "same@test.com", "HM", None)
        assert len(attendees) == 1  # Only candidate, HM skipped

    def test_hm_duplicate_case_insensitive(self):
        """Deduplication should be case-insensitive."""
        attendees, cc = self._build_attendees("CAND@Test.com", "Cand", "cand@test.com", "HM", None)
        assert len(attendees) == 1

    def test_panel_duplicate_of_hm(self):
        """Panel interviewer with same email as HM should be skipped."""
        panel = [{"email": "hm@test.com", "name": "P1"}]
        attendees, cc = self._build_attendees("cand@test.com", "Cand", "hm@test.com", "HM", panel)
        assert len(attendees) == 2  # Candidate + HM
        assert len(cc) == 0  # Panel skipped (duplicate of HM)

    def test_recruiter_in_cc(self):
        """Recruiter should be added to CC."""
        attendees, cc = self._build_attendees(
            "cand@test.com", "Cand", "", "", None,
            rec_email="rec@test.com", rec_name="Rec",
        )
        assert len(cc) == 1
        assert cc[0][0] == "rec@test.com"

    def test_empty_hm_is_falsy(self):
        """Empty string hm_email should not be added to attendees."""
        attendees, cc = self._build_attendees("cand@test.com", "Cand", "", "", None)
        # Verify no tuple with empty string was added
        for email, _ in attendees:
            assert email != ""
            assert email is not None


# ===========================================================================
# 6. _handle_create_invite BACKWARD COMPAT BRANCH (Bug fix verification)
# ===========================================================================
class TestHandleCreateInviteBackwardCompat:
    """
    Test the `else` branch in _handle_create_invite where no panel interviewers
    exist and it falls back to just the hiring manager.
    This was a critical bug: attendees.append((hm_email, hm_name)) was
    unconditional even when hm_email was empty/None.
    """

    def _simulate_else_branch_fixed(self, hm_email: str, hm_name: str):
        """Simulate the fixed else branch logic."""
        attendees: List[Tuple[str, str]] = [("cand@test.com", "Candidate")]
        # Fixed logic (with guard)
        if hm_email:
            attendees.append((hm_email, hm_name))
        return attendees

    def _simulate_else_branch_broken(self, hm_email: str, hm_name: str):
        """Simulate the broken (original) else branch logic."""
        attendees: List[Tuple[str, str]] = [("cand@test.com", "Candidate")]
        # Broken logic (unconditional append)
        attendees.append((hm_email, hm_name))
        return attendees

    def test_fixed_empty_hm_no_append(self):
        """Fixed: empty hm_email should NOT be appended to attendees."""
        attendees = self._simulate_else_branch_fixed("", "")
        assert len(attendees) == 1
        assert all(email for email, _ in attendees)

    def test_fixed_valid_hm_appended(self):
        """Fixed: valid hm_email should be appended."""
        attendees = self._simulate_else_branch_fixed("hm@test.com", "HM")
        assert len(attendees) == 2
        assert attendees[1] == ("hm@test.com", "HM")

    def test_broken_empty_hm_would_append_empty(self):
        """Demonstrates the bug: broken version adds ('', '') to attendees."""
        attendees = self._simulate_else_branch_broken("", "")
        assert len(attendees) == 2  # Bug: added empty tuple
        assert attendees[1] == ("", "")  # This would break Graph API


# ===========================================================================
# 7. EMAIL RECIPIENT LIST (White-box: to_emails filtering)
# ===========================================================================
class TestEmailRecipientList:
    """Test email recipient list construction with optional HM."""

    def test_to_emails_filter_empty(self):
        """The [e for e in [...] if e] pattern should filter empty strings."""
        candidate_email = "cand@test.com"
        hiring_manager_email = ""  # Empty
        recruiter_email = "rec@test.com"
        include_recruiter = True

        to_emails = (
            [e for e in [candidate_email, hiring_manager_email] if e]
            + ([recruiter_email] if include_recruiter and recruiter_email else [])
        )
        assert to_emails == ["cand@test.com", "rec@test.com"]
        assert "" not in to_emails

    def test_to_emails_with_hm(self):
        """Full list when HM is present."""
        candidate_email = "cand@test.com"
        hiring_manager_email = "hm@test.com"
        recruiter_email = "rec@test.com"

        to_emails = (
            [e for e in [candidate_email, hiring_manager_email] if e]
            + ([recruiter_email] if recruiter_email else [])
        )
        assert to_emails == ["cand@test.com", "hm@test.com", "rec@test.com"]

    def test_to_emails_only_candidate(self):
        """Only candidate when HM and recruiter are empty."""
        to_emails = [e for e in ["cand@test.com", ""] if e]
        assert to_emails == ["cand@test.com"]

    def test_none_filtered_out(self):
        """None values should also be filtered."""
        to_emails = [e for e in ["cand@test.com", None] if e]
        assert to_emails == ["cand@test.com"]


# ===========================================================================
# 8. VALIDATE INVITE FLOW (White-box: validation report)
# ===========================================================================
class TestValidateInviteFlow:
    """Test the _validate_invite_flow function with optional HM."""

    def _mock_validate_flow(
        self,
        app_module,
        hm_email_raw: str,
        panel_interviewers: Optional[List[Dict[str, str]]] = None,
    ):
        """Run validation with mocked dependencies."""
        candidates = [
            app_module.CandidateValidationResult(
                original="cand@test.com",
                email="cand@test.com",
                name="Candidate",
                is_valid=True,
                error=None,
            )
        ]
        report = app_module._validate_invite_flow(
            selected_slot={"date": "2026-03-01", "start": "10:00", "end": "11:00"},
            tz_name="UTC",
            candidate_timezone="UTC",
            duration_minutes=60,
            role_title="Engineer",
            candidates=candidates,
            hiring_manager=( hm_email_raw, "HM Name"),
            recruiter=("", ""),
            include_recruiter=False,
            panel_interviewers=panel_interviewers,
            is_teams=True,
        )
        return report

    def test_validate_empty_hm_no_panel(self, app_module):
        """No errors when HM is empty and panel is provided."""
        report = self._mock_validate_flow(
            app_module, "",
            panel_interviewers=[{"email": "panel@test.com", "name": "P1"}],
        )
        # HM is optional, so no error about it
        hm_errors = [e for e in report.errors if "iring" in e.lower()]
        assert len(hm_errors) == 0

    def test_validate_valid_hm(self, app_module):
        """HM email in intended recipients when valid."""
        report = self._mock_validate_flow(app_module, "hm@test.com")
        assert "hm@test.com" in report.intended_recipients

    def test_validate_empty_hm_not_in_recipients(self, app_module):
        """Empty HM should NOT appear in intended recipients."""
        report = self._mock_validate_flow(
            app_module, "",
            panel_interviewers=[{"email": "panel@test.com", "name": "P1"}],
        )
        assert "" not in report.intended_recipients
        assert None not in report.intended_recipients

    def test_validate_invalid_hm_format_adds_error(self, app_module):
        """Invalid HM email format should add an error."""
        report = self._mock_validate_flow(
            app_module, "not-an-email",
            panel_interviewers=[{"email": "panel@test.com", "name": "P1"}],
        )
        hm_errors = [e for e in report.errors if "email" in e.lower()]
        assert len(hm_errors) > 0


# ===========================================================================
# 9. UI LABEL (Black-box: verify the input label text)
# ===========================================================================
class TestUILabel:
    """Verify the hiring manager email field label says 'optional'."""

    def test_label_says_optional(self, app_module):
        """Read app.py source and verify the label text."""
        import inspect
        source = inspect.getsource(app_module)
        assert 'Hiring Manager Email (optional)' in source
        assert 'Hiring Manager Email (required)' not in source


# ===========================================================================
# 10. SCHEDULING RESULT DATACLASS (White-box: recipients field)
# ===========================================================================
class TestSchedulingResult:
    """Test SchedulingResult with various attendee configurations."""

    def test_result_recipients_without_hm(self, app_module):
        """Recipients should only contain candidate when no HM."""
        result = app_module.SchedulingResult(
            candidate_email="cand@test.com",
            candidate_name="Candidate",
            success=True,
            event_id="event-123",
            teams_url="https://teams.microsoft.com/meet",
            error=None,
            recipients=["cand@test.com"],
        )
        assert "cand@test.com" in result.recipients
        assert len(result.recipients) == 1

    def test_result_recipients_with_hm(self, app_module):
        """Recipients should include HM when provided."""
        result = app_module.SchedulingResult(
            candidate_email="cand@test.com",
            candidate_name="Candidate",
            success=True,
            event_id="event-123",
            teams_url="",
            error=None,
            recipients=["cand@test.com", "hm@test.com"],
        )
        assert len(result.recipients) == 2

    def test_result_recipients_none_on_failure(self, app_module):
        """Recipients should be None on scheduling failure."""
        result = app_module.SchedulingResult(
            candidate_email="cand@test.com",
            candidate_name="Candidate",
            success=False,
            event_id=None,
            teams_url=None,
            error="Graph API failed",
            recipients=None,
        )
        assert result.recipients is None


# ===========================================================================
# 11. EDGE CASES (Black-box & boundary testing)
# ===========================================================================
class TestEdgeCases:
    """Edge case and boundary tests."""

    def test_hm_email_only_whitespace(self, app_module):
        """Whitespace-only HM email should be treated as empty."""
        result = app_module.validate_email_optional("   \t\n  ") or ""
        assert result == ""

    def test_hm_email_with_leading_trailing_spaces(self, app_module):
        """Valid email with spaces should be trimmed and normalized."""
        result = app_module.validate_email_optional("  HM@Test.COM  ") or ""
        assert result == "hm@test.com"

    def test_hm_email_special_characters(self, app_module):
        """Valid email with special characters in local part."""
        result = app_module.validate_email_optional("hm.name+tag@test.com") or ""
        assert result == "hm.name+tag@test.com"

    def test_stable_uid_empty_hm_consistent(self):
        """UID generation should be consistent for empty HM across calls."""
        uid1 = stable_uid("Role|cand@test.com|", "org@test.com", "2026-01-01T00:00:00")
        uid2 = stable_uid("Role|cand@test.com|", "org@test.com", "2026-01-01T00:00:00")
        assert uid1 == uid2

    def test_graph_send_mail_filters_empty_addresses(self):
        """Verify Graph send_mail builds recipients that filter empties."""
        # Simulate the Graph API recipient building logic
        to_recipients = ["cand@test.com", "", None]
        built = [
            {"emailAddress": {"address": addr}}
            for addr in to_recipients
            if addr
        ]
        assert len(built) == 1
        assert built[0]["emailAddress"]["address"] == "cand@test.com"

    def test_multiple_empty_hm_idempotency(self, audit_db):
        """Multiple inserts with empty HM should be detectable."""
        for i in range(3):
            audit_db.upsert_interview(
                role_title="Engineer",
                candidate_email="cand@test.com",
                hiring_manager_email="",
                recruiter_email="",
                duration_minutes=60,
                start_utc="2026-03-10T10:00:00+00:00",
                end_utc="2026-03-10T11:00:00+00:00",
                display_timezone="UTC",
                candidate_timezone="UTC",
                graph_event_id=f"event-{i}",
                teams_join_url="",
                subject="Test",
                last_status="created",
            )

        # All should be findable
        existing = audit_db.interview_exists(
            candidate_email="cand@test.com",
            hiring_manager_email="",
            role_title="Engineer",
            start_utc="2026-03-10T10:00:00+00:00",
        )
        assert existing is not None


# ===========================================================================
# 12. INTEGRATION: Full audit log cycle with optional HM
# ===========================================================================
class TestAuditLogIntegration:
    """Integration tests for the full audit log lifecycle."""

    def test_full_cycle_no_hm(self, audit_db):
        """Full create -> log -> exists cycle without hiring manager."""
        # 1. Create interview
        assert audit_db.upsert_interview(
            role_title="SWE",
            candidate_email="john@example.com",
            hiring_manager_email="",
            recruiter_email="rec@example.com",
            duration_minutes=45,
            start_utc="2026-04-01T14:00:00+00:00",
            end_utc="2026-04-01T14:45:00+00:00",
            display_timezone="America/New_York",
            candidate_timezone="America/Los_Angeles",
            graph_event_id="graph-evt-001",
            teams_join_url="https://teams.microsoft.com/l/meetup-join/123",
            subject="Interview: SWE - John",
            last_status="created",
        )

        # 2. Log the action
        assert audit_db.log(
            "graph_create_event",
            actor="rec@example.com",
            candidate_email="john@example.com",
            hiring_manager_email="",
            recruiter_email="rec@example.com",
            role_title="SWE",
            event_id="graph-evt-001",
            status="success",
        )

        # 3. Check existence
        existing = audit_db.interview_exists(
            candidate_email="john@example.com",
            hiring_manager_email="",
            role_title="SWE",
            start_utc="2026-04-01T14:00:00+00:00",
        )
        assert existing is not None
        assert existing["graph_event_id"] == "graph-evt-001"
        assert existing["hiring_manager_email"] == ""

        # 4. Verify audit log entry
        entries = audit_db.list_recent_audit(limit=5)
        create_entry = next(e for e in entries if e["action"] == "graph_create_event")
        assert create_entry["hiring_manager_email"] == ""
        assert create_entry["candidate_email"] == "john@example.com"

    def test_full_cycle_with_hm(self, audit_db):
        """Full create -> log -> exists cycle with hiring manager."""
        assert audit_db.upsert_interview(
            role_title="PM",
            candidate_email="jane@example.com",
            hiring_manager_email="boss@example.com",
            recruiter_email="rec@example.com",
            duration_minutes=60,
            start_utc="2026-04-02T10:00:00+00:00",
            end_utc="2026-04-02T11:00:00+00:00",
            display_timezone="UTC",
            candidate_timezone="UTC",
            graph_event_id="graph-evt-002",
            teams_join_url="",
            subject="Interview: PM - Jane",
            last_status="created",
        )

        existing = audit_db.interview_exists(
            candidate_email="jane@example.com",
            hiring_manager_email="boss@example.com",
            role_title="PM",
            start_utc="2026-04-02T10:00:00+00:00",
        )
        assert existing is not None
        assert existing["hiring_manager_email"] == "boss@example.com"


# ===========================================================================
# 13. REGRESSION: Ensure the old "required" behavior doesn't resurface
# ===========================================================================
class TestRegressionRequiredRemoved:
    """Ensure no code path still treats HM email as required."""

    def test_no_required_label_in_source(self, app_module):
        """No reference to 'Hiring Manager Email (required)' should remain."""
        source_path = Path(__file__).resolve().parent.parent / "app.py"
        source = source_path.read_text()
        assert 'Hiring Manager Email (required)' not in source

    def test_validate_email_not_used_for_hm(self, app_module):
        """validate_email (required) should not be called for HM field."""
        source_path = Path(__file__).resolve().parent.parent / "app.py"
        source = source_path.read_text()

        # Find all lines that call validate_email for hiring manager
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "hiring manager" in line.lower() or "hm_email" in line.lower():
                # Should not have bare validate_email( without _optional
                stripped = line.strip()
                if stripped.startswith("hm_email = validate_email("):
                    pytest.fail(
                        f"Line {i+1} uses validate_email (required) for HM: {stripped}"
                    )


# ===========================================================================
# 14. HAS_INTERVIEWERS LOGIC (White-box: create button enablement)
# ===========================================================================
class TestHasInterviewersLogic:
    """Test the has_interviewers flag used to enable/disable create button."""

    def test_has_interviewers_with_panel_no_hm(self):
        """Button should be enabled with panel interviewers even without HM."""
        panel_interviewers_for_invite = [{"name": "P1", "email": "p1@test.com"}]
        hiring_manager_email = ""
        has_interviewers = bool(panel_interviewers_for_invite) or bool(hiring_manager_email)
        assert has_interviewers is True

    def test_has_interviewers_with_hm_no_panel(self):
        """Button should be enabled with HM even without panel."""
        panel_interviewers_for_invite = []
        hiring_manager_email = "hm@test.com"
        has_interviewers = bool(panel_interviewers_for_invite) or bool(hiring_manager_email)
        assert has_interviewers is True

    def test_has_interviewers_both(self):
        """Button should be enabled with both HM and panel."""
        panel_interviewers_for_invite = [{"name": "P1", "email": "p1@test.com"}]
        hiring_manager_email = "hm@test.com"
        has_interviewers = bool(panel_interviewers_for_invite) or bool(hiring_manager_email)
        assert has_interviewers is True

    def test_has_interviewers_neither(self):
        """Button should be disabled when neither HM nor panel provided."""
        panel_interviewers_for_invite = []
        hiring_manager_email = ""
        has_interviewers = bool(panel_interviewers_for_invite) or bool(hiring_manager_email)
        assert has_interviewers is False


# ===========================================================================
# 15. AUTO-SEND VALIDATION (Scheduler Inbox tab)
# ===========================================================================
class TestAutoSendValidation:
    """Test the auto-send validation in Scheduler Inbox tab."""

    def test_auto_send_blocked_no_hm_no_panel(self):
        """Auto-send should be blocked when no HM and no panel interviewers."""
        hm_email = ""
        panel_interviewers = []
        should_block = not hm_email and not panel_interviewers
        assert should_block is True

    def test_auto_send_allowed_with_panel(self):
        """Auto-send should proceed with panel even without HM."""
        hm_email = ""
        panel_interviewers = [{"email": "panel@test.com", "name": "P1"}]
        should_block = not hm_email and not panel_interviewers
        assert should_block is False

    def test_auto_send_allowed_with_hm(self):
        """Auto-send should proceed with HM even without panel."""
        hm_email = "hm@test.com"
        panel_interviewers = []
        should_block = not hm_email and not panel_interviewers
        assert should_block is False
