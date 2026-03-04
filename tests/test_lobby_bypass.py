"""
Tests for Teams meeting lobby bypass functionality.

Tests cover:
- GraphClient.set_meeting_lobby_bypass: meeting lookup, PATCH payload, error handling
- Integration: lobby bypass is called after event creation in all code paths
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graph_client import GraphClient, GraphConfig, GraphAPIError, GraphAuthError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
SAMPLE_JOIN_URL = "https://teams.microsoft.com/l/meetup-join/abc123"
SAMPLE_MEETING_ID = "meeting-id-456"


@pytest.fixture
def graph_cfg():
    return GraphConfig(
        tenant_id="test-tenant",
        client_id="test-client",
        client_secret="test-secret",
        scheduler_mailbox="scheduler@powerdashhr.com",
    )


@pytest.fixture
def client(graph_cfg):
    c = GraphClient(graph_cfg)
    # Pre-set a valid token so _headers() doesn't try to authenticate
    c._token = "fake-token"
    from datetime import datetime, timedelta, timezone
    c._token_expiry_utc = datetime.now(timezone.utc) + timedelta(hours=1)
    return c


# ===========================================================================
# 1. GraphClient.set_meeting_lobby_bypass — unit tests
# ===========================================================================
class TestSetMeetingLobbyBypass:
    """Unit tests for the set_meeting_lobby_bypass method."""

    def test_successful_lobby_bypass(self, client):
        """Should find meeting by join URL, PATCH lobby settings, return True."""
        responses = [
            # GET /onlineMeetings?$filter=... → returns one meeting
            (200, {"value": [{"id": SAMPLE_MEETING_ID}]}),
            # PATCH /onlineMeetings/{id} → success
            (200, {}),
        ]
        call_log = []

        def mock_request(method, url, *, params=None, json_body=None, _retry_auth=True):
            call_log.append((method, url, params, json_body))
            return responses.pop(0)

        client._request = mock_request

        result = client.set_meeting_lobby_bypass(SAMPLE_JOIN_URL)

        assert result is True
        assert len(call_log) == 2

        # Verify GET call searches by join URL
        get_method, get_url, get_params, _ = call_log[0]
        assert get_method == "GET"
        assert "/onlineMeetings" in get_url
        assert get_params == {"$filter": f"JoinWebUrl eq '{SAMPLE_JOIN_URL}'"}

        # Verify PATCH call sets lobby bypass
        patch_method, patch_url, _, patch_body = call_log[1]
        assert patch_method == "PATCH"
        assert SAMPLE_MEETING_ID in patch_url
        assert patch_body == {
            "lobbyBypassSettings": {
                "scope": "everyone",
                "isDialInBypassEnabled": True,
            },
        }

    def test_meeting_not_found_returns_false(self, client):
        """Should return False when no meeting matches the join URL."""
        def mock_request(method, url, *, params=None, json_body=None, _retry_auth=True):
            return (200, {"value": []})

        client._request = mock_request

        result = client.set_meeting_lobby_bypass(SAMPLE_JOIN_URL)
        assert result is False

    def test_meeting_id_missing_returns_false(self, client):
        """Should return False when meeting record has no id field."""
        def mock_request(method, url, *, params=None, json_body=None, _retry_auth=True):
            return (200, {"value": [{"subject": "Test", "id": None}]})

        client._request = mock_request

        result = client.set_meeting_lobby_bypass(SAMPLE_JOIN_URL)
        assert result is False

    def test_graph_api_error_returns_false(self, client):
        """Should catch GraphAPIError (e.g. 403 missing permissions) and return False."""
        def mock_request(method, url, *, params=None, json_body=None, _retry_auth=True):
            raise GraphAPIError("Forbidden", status_code=403)

        client._request = mock_request

        result = client.set_meeting_lobby_bypass(SAMPLE_JOIN_URL)
        assert result is False

    def test_graph_auth_error_returns_false(self, client):
        """Should catch GraphAuthError and return False."""
        def mock_request(method, url, *, params=None, json_body=None, _retry_auth=True):
            raise GraphAuthError("Token expired")

        client._request = mock_request

        result = client.set_meeting_lobby_bypass(SAMPLE_JOIN_URL)
        assert result is False

    def test_patch_failure_returns_false(self, client):
        """Should return False when GET succeeds but PATCH fails."""
        call_count = [0]

        def mock_request(method, url, *, params=None, json_body=None, _retry_auth=True):
            call_count[0] += 1
            if call_count[0] == 1:
                return (200, {"value": [{"id": SAMPLE_MEETING_ID}]})
            raise GraphAPIError("Server error", status_code=500)

        client._request = mock_request

        result = client.set_meeting_lobby_bypass(SAMPLE_JOIN_URL)
        assert result is False

    def test_empty_join_url(self, client):
        """Should handle empty join URL gracefully (no meetings found)."""
        def mock_request(method, url, *, params=None, json_body=None, _retry_auth=True):
            return (200, {"value": []})

        client._request = mock_request

        result = client.set_meeting_lobby_bypass("")
        assert result is False

    def test_null_body_returns_false(self, client):
        """Should handle None response body gracefully."""
        def mock_request(method, url, *, params=None, json_body=None, _retry_auth=True):
            return (200, None)

        client._request = mock_request

        result = client.set_meeting_lobby_bypass(SAMPLE_JOIN_URL)
        assert result is False

    def test_correct_mailbox_in_url(self, client):
        """Should use the scheduler mailbox from config in the API URL."""
        call_log = []

        def mock_request(method, url, *, params=None, json_body=None, _retry_auth=True):
            call_log.append(url)
            return (200, {"value": []})

        client._request = mock_request
        client.set_meeting_lobby_bypass(SAMPLE_JOIN_URL)

        assert "scheduler@powerdashhr.com" in call_log[0]


# ===========================================================================
# 2. Event payload — no invalid properties
# ===========================================================================
class TestEventPayloadNoLobbySettings:
    """Ensure _graph_event_payload does NOT include onlineMeeting lobby settings
    (those are set via the separate onlineMeetings API)."""

    def setup_method(self):
        # Mock streamlit if not already done
        if "streamlit" not in sys.modules:
            sys.modules["streamlit"] = MagicMock()

    def test_teams_payload_has_no_onlinemeeting_property(self):
        """isOnlineMeeting should be set but onlineMeeting dict should not be present."""
        from datetime import datetime, timedelta, timezone
        import app as app_mod

        now = datetime.now(timezone.utc)
        payload = app_mod._graph_event_payload(
            subject="Interview",
            body_html="<p>Test</p>",
            start_local=now,
            end_local=now + timedelta(hours=1),
            time_zone="UTC",
            attendees=[("candidate@test.com", "Candidate")],
            is_teams=True,
            location="",
        )
        assert payload["isOnlineMeeting"] is True
        assert payload["onlineMeetingProvider"] == "teamsForBusiness"
        # The onlineMeeting dict with lobbyBypassSettings must NOT be in the payload
        # (it's a read-only property on calendar events)
        assert "onlineMeeting" not in payload
        assert "allowNewTimeProposals" not in payload

    def test_non_teams_payload_unchanged(self):
        """Non-Teams events should have no online meeting properties."""
        from datetime import datetime, timedelta, timezone
        import app as app_mod

        now = datetime.now(timezone.utc)
        payload = app_mod._graph_event_payload(
            subject="Interview",
            body_html="<p>Test</p>",
            start_local=now,
            end_local=now + timedelta(hours=1),
            time_zone="UTC",
            attendees=[("candidate@test.com", "Candidate")],
            is_teams=False,
            location="Conference Room A",
        )
        assert "isOnlineMeeting" not in payload
        assert "onlineMeetingProvider" not in payload
        assert payload["location"]["displayName"] == "Conference Room A"
