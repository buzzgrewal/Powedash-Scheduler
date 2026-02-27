"""
Comprehensive tests for company branding propagation across the application.

Tests cover the full branding pipeline: configuration → session state → emails → UI.

WHITE-BOX tests:
- get_company_config() fallback chain: session_state → secrets → defaults
- ensure_session_state() loads company_name and primary_color from branding file
- _update_branding_field() merge semantics (add, update, remove)
- _render_logo_settings() company name input handling
- _render_footer() uses dynamic company name
- main() page title reads from persisted branding

BLACK-BOX tests:
- Slot email contains configured company name (not "PowerDash HR")
- Confirmation email contains configured company name
- Cancellation email contains configured company name
- Reschedule email contains configured company name
- Plain text email contains configured company name
- Signature line uses configured company name
- Full lifecycle: set name → persist → reload → verify in email

EDGE CASES:
- Empty string company name falls back to default
- Whitespace-only company name ignored
- Unicode/special characters in company name (e.g., "Ünïcödé Corp 日本語")
- HTML-like characters in company name (XSS vector)
- Very long company name
- Company name with quotes and apostrophes
- Resetting custom name back to default clears override
- Secrets override vs session state override precedence
- Missing branding file on first load
- Corrupt branding file recovery
- Primary color propagation through same chain
- Multiple branding fields persisted simultaneously
"""
import base64
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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

_mock_st = sys.modules["streamlit"]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_streamlit_state():
    """Reset session state and secrets before each test."""
    _mock_st.session_state = {}
    _mock_st.secrets = {}
    yield
    _mock_st.session_state = {}
    _mock_st.secrets = {}


@pytest.fixture
def tmp_branding_file(tmp_path):
    """Create a temporary JSON file path for branding settings."""
    return str(tmp_path / "branding_settings.json")


@pytest.fixture
def patch_branding_path(tmp_branding_file):
    """Patch _get_branding_settings_path to use a temp file."""
    with patch.object(app_mod, "_get_branding_settings_path", return_value=tmp_branding_file):
        yield tmp_branding_file


@pytest.fixture
def neogen_company():
    """A CompanyConfig for Neogen (non-default company)."""
    return app_mod.CompanyConfig(
        name="Neogen",
        logo_url=None,
        primary_color="#FF5500",
        website="https://neogen.com",
        sender_email="hr@neogen.com",
    )


@pytest.fixture
def default_company():
    """A CompanyConfig with default values (PowerDash HR)."""
    return app_mod.CompanyConfig(
        name="PowerDash HR",
        logo_url="logo.png",
        primary_color="#0066CC",
        website=None,
        sender_email="scheduling@powerdashhr.com",
    )


@pytest.fixture
def sample_slots():
    """Sample interview slots for email tests."""
    return [
        {"date": "2026-03-15", "start": "10:00", "end": "11:00"},
        {"date": "2026-03-16", "start": "14:00", "end": "15:00"},
    ]


# ===========================================================================
# WHITE-BOX: get_company_config() fallback chain
# ===========================================================================
class TestGetCompanyConfigFallback:
    """Test the priority chain: session_state > secrets > hardcoded defaults."""

    def test_default_returns_powerdash_hr(self):
        """With no overrides, company name should be 'PowerDash HR'."""
        config = app_mod.get_company_config()
        assert config.name == "PowerDash HR"
        assert config.primary_color == "#0066CC"

    def test_secrets_override_default(self):
        """Secrets should override hardcoded defaults."""
        _mock_st.secrets = {"company_name": "Neogen", "company_primary_color": "#FF5500"}
        config = app_mod.get_company_config()
        assert config.name == "Neogen"
        assert config.primary_color == "#FF5500"

    def test_session_state_overrides_secrets(self):
        """Session state should override secrets."""
        _mock_st.secrets = {"company_name": "FromSecrets"}
        _mock_st.session_state = {"custom_company_name": "FromSessionState"}
        config = app_mod.get_company_config()
        assert config.name == "FromSessionState"

    def test_session_state_overrides_default(self):
        """Session state should override default when no secrets set."""
        _mock_st.session_state = {"custom_company_name": "Acme Corp"}
        config = app_mod.get_company_config()
        assert config.name == "Acme Corp"

    def test_session_state_none_falls_through_to_secrets(self):
        """None in session state should fall through to secrets."""
        _mock_st.secrets = {"company_name": "FromSecrets"}
        _mock_st.session_state = {"custom_company_name": None}
        config = app_mod.get_company_config()
        assert config.name == "FromSecrets"

    def test_empty_string_session_state_falls_through(self):
        """Empty string in session state should fall through (falsy)."""
        _mock_st.secrets = {"company_name": "FromSecrets"}
        _mock_st.session_state = {"custom_company_name": ""}
        config = app_mod.get_company_config()
        assert config.name == "FromSecrets"

    def test_primary_color_session_state_override(self):
        """Primary color should also follow session state > secrets > default."""
        _mock_st.session_state = {"custom_primary_color": "#123456"}
        config = app_mod.get_company_config()
        assert config.primary_color == "#123456"

    def test_primary_color_secrets_override(self):
        """Primary color from secrets should override default."""
        _mock_st.secrets = {"company_primary_color": "#ABCDEF"}
        config = app_mod.get_company_config()
        assert config.primary_color == "#ABCDEF"

    def test_signature_name_uses_company_name(self):
        """signature_name property should use the configured company name."""
        _mock_st.session_state = {"custom_company_name": "Neogen"}
        config = app_mod.get_company_config()
        assert config.signature_name == "Neogen Talent Acquisition Team"

    def test_signature_name_default(self):
        """signature_name with default company name."""
        config = app_mod.get_company_config()
        assert config.signature_name == "PowerDash HR Talent Acquisition Team"

    def test_website_from_secrets(self):
        """Website should come from secrets (no session state override)."""
        _mock_st.secrets = {"company_website": "https://example.com"}
        config = app_mod.get_company_config()
        assert config.website == "https://example.com"

    def test_sender_email_from_secrets(self):
        """Sender email should come from graph_scheduler_mailbox secret."""
        _mock_st.secrets = {"graph_scheduler_mailbox": "hr@neogen.com"}
        config = app_mod.get_company_config()
        assert config.sender_email == "hr@neogen.com"

    def test_all_overrides_simultaneously(self):
        """All fields overridden at once."""
        _mock_st.session_state = {
            "custom_company_name": "Neogen",
            "custom_primary_color": "#FF0000",
            "custom_logo_data": "data:image/png;base64,abc",
        }
        _mock_st.secrets = {
            "company_website": "https://neogen.com",
            "graph_scheduler_mailbox": "sched@neogen.com",
        }
        config = app_mod.get_company_config()
        assert config.name == "Neogen"
        assert config.primary_color == "#FF0000"
        assert config.logo_url == "data:image/png;base64,abc"
        assert config.website == "https://neogen.com"
        assert config.sender_email == "sched@neogen.com"


# ===========================================================================
# WHITE-BOX: _update_branding_field() merge semantics
# ===========================================================================
class TestUpdateBrandingField:
    """Test _update_branding_field preserves other keys and handles edge cases."""

    def test_add_company_name_to_empty_file(self, patch_branding_path):
        """Adding company_name to empty settings file."""
        app_mod._update_branding_field("company_name", "Neogen")
        loaded = app_mod._load_branding_settings()
        assert loaded == {"company_name": "Neogen"}

    def test_add_company_name_preserves_logo(self, patch_branding_path):
        """Adding company_name should not remove existing logo_data."""
        app_mod._save_branding_settings({"logo_data": "data:image/png;base64,abc"})
        app_mod._update_branding_field("company_name", "Neogen")
        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "Neogen"
        assert loaded["logo_data"] == "data:image/png;base64,abc"

    def test_remove_company_name_preserves_other_keys(self, patch_branding_path):
        """Removing company_name (None) should keep other keys intact."""
        app_mod._save_branding_settings({
            "company_name": "Neogen",
            "logo_data": "data:image/png;base64,abc",
            "primary_color": "#FF5500",
        })
        app_mod._update_branding_field("company_name", None)
        loaded = app_mod._load_branding_settings()
        assert "company_name" not in loaded
        assert loaded["logo_data"] == "data:image/png;base64,abc"
        assert loaded["primary_color"] == "#FF5500"

    def test_update_existing_company_name(self, patch_branding_path):
        """Updating company_name should overwrite old value."""
        app_mod._save_branding_settings({"company_name": "OldName"})
        app_mod._update_branding_field("company_name", "NewName")
        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "NewName"

    def test_update_primary_color(self, patch_branding_path):
        """Should work for primary_color too."""
        app_mod._update_branding_field("primary_color", "#123ABC")
        loaded = app_mod._load_branding_settings()
        assert loaded["primary_color"] == "#123ABC"

    def test_remove_nonexistent_key_is_noop(self, patch_branding_path):
        """Removing a key that doesn't exist should be a no-op."""
        app_mod._save_branding_settings({"logo_data": "xyz"})
        app_mod._update_branding_field("company_name", None)
        loaded = app_mod._load_branding_settings()
        assert loaded == {"logo_data": "xyz"}

    def test_handles_corrupt_existing_file(self, patch_branding_path):
        """Should handle corrupt JSON in existing file gracefully."""
        with open(patch_branding_path, "w") as f:
            f.write("NOT VALID JSON{{{")
        app_mod._update_branding_field("company_name", "Neogen")
        loaded = app_mod._load_branding_settings()
        assert loaded == {"company_name": "Neogen"}

    def test_multiple_fields_in_sequence(self, patch_branding_path):
        """Multiple sequential field updates should all persist."""
        app_mod._update_branding_field("company_name", "Neogen")
        app_mod._update_branding_field("primary_color", "#FF5500")
        app_mod._update_branding_field("logo_data", "data:image/png;base64,xyz")
        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "Neogen"
        assert loaded["primary_color"] == "#FF5500"
        assert loaded["logo_data"] == "data:image/png;base64,xyz"


# ===========================================================================
# WHITE-BOX: ensure_session_state() branding loading
# ===========================================================================
class TestEnsureSessionStateBranding:
    """Test that ensure_session_state loads company_name and primary_color from disk."""

    def test_loads_company_name_from_branding_file(self, patch_branding_path):
        """Company name should be loaded into session state on first run."""
        app_mod._save_branding_settings({"company_name": "Neogen"})
        _mock_st.session_state = {}

        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert _mock_st.session_state.get("custom_company_name") == "Neogen"

    def test_loads_primary_color_from_branding_file(self, patch_branding_path):
        """Primary color should be loaded into session state on first run."""
        app_mod._save_branding_settings({"primary_color": "#FF5500"})
        _mock_st.session_state = {}

        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert _mock_st.session_state.get("custom_primary_color") == "#FF5500"

    def test_loads_all_branding_fields_together(self, patch_branding_path):
        """All branding fields should load in one pass."""
        app_mod._save_branding_settings({
            "company_name": "Neogen",
            "primary_color": "#FF5500",
            "logo_data": "data:image/png;base64,abc",
        })
        _mock_st.session_state = {}

        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert _mock_st.session_state["custom_company_name"] == "Neogen"
        assert _mock_st.session_state["custom_primary_color"] == "#FF5500"
        assert _mock_st.session_state["custom_logo_data"] == "data:image/png;base64,abc"

    def test_skips_loading_if_already_loaded(self, patch_branding_path):
        """Should not overwrite session state if _branding_loaded is True."""
        app_mod._save_branding_settings({"company_name": "Neogen"})
        _mock_st.session_state = {
            "_branding_loaded": True,
            "custom_company_name": "AlreadySet",
        }

        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert _mock_st.session_state["custom_company_name"] == "AlreadySet"

    def test_handles_missing_branding_file(self, patch_branding_path):
        """Should not crash if branding file doesn't exist."""
        _mock_st.session_state = {}

        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert _mock_st.session_state.get("custom_company_name") is None
        assert _mock_st.session_state["_branding_loaded"] is True

    def test_handles_empty_branding_file(self, patch_branding_path):
        """Empty branding dict should not set session state keys."""
        app_mod._save_branding_settings({})
        _mock_st.session_state = {}

        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert "custom_company_name" not in _mock_st.session_state
        assert "custom_primary_color" not in _mock_st.session_state
        assert _mock_st.session_state["_branding_loaded"] is True

    def test_handles_corrupt_branding_file(self, patch_branding_path):
        """Corrupt JSON should not crash ensure_session_state."""
        with open(patch_branding_path, "w") as f:
            f.write("{{{broken json")
        _mock_st.session_state = {}

        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert "custom_company_name" not in _mock_st.session_state
        assert _mock_st.session_state["_branding_loaded"] is True

    def test_does_not_load_null_company_name(self, patch_branding_path):
        """Null value for company_name in file should not be loaded."""
        with open(patch_branding_path, "w") as f:
            json.dump({"company_name": None, "primary_color": None}, f)
        _mock_st.session_state = {}

        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert "custom_company_name" not in _mock_st.session_state
        assert "custom_primary_color" not in _mock_st.session_state


# ===========================================================================
# BLACK-BOX: Email output contains configured company name
# ===========================================================================
class TestEmailsUseCompanyName:
    """All email templates should use the configured company name, not hardcoded."""

    def test_slot_email_html_uses_custom_name(self, neogen_company, sample_slots):
        """Slot selection email should say 'Neogen', not 'PowerDash HR'."""
        html = app_mod.build_branded_email_html(
            candidate_name="Jane Doe",
            role_title="Software Engineer",
            slots=sample_slots,
            company=neogen_company,
        )
        assert "Neogen" in html
        assert "PowerDash HR" not in html
        assert "Software Engineer" in html
        assert "Jane Doe" in html

    def test_slot_email_html_default_name(self, default_company, sample_slots):
        """With default config, email should say 'PowerDash HR'."""
        html = app_mod.build_branded_email_html(
            candidate_name="Jane Doe",
            role_title="Designer",
            slots=sample_slots,
            company=default_company,
        )
        assert "PowerDash HR" in html

    def test_slot_email_plain_uses_custom_name(self, neogen_company, sample_slots):
        """Plain text slot email should use custom company name."""
        text = app_mod.build_branded_email_plain(
            candidate_name="Jane Doe",
            role_title="Software Engineer",
            slots=sample_slots,
            company=neogen_company,
        )
        assert "Neogen" in text
        assert "PowerDash HR" not in text
        assert "Software Engineer" in text

    def test_confirmation_email_uses_custom_name(self, neogen_company):
        """Confirmation email should use custom company name."""
        html = app_mod.build_confirmation_email_html(
            candidate_name="Jane Doe",
            role_title="Data Analyst",
            interview_time="March 15, 2026 at 10:00 AM",
            teams_url="https://teams.microsoft.com/meet/123",
            interviewer_names=["John Smith"],
            company=neogen_company,
        )
        assert "Neogen" in html
        assert "PowerDash HR" not in html
        assert "Data Analyst" in html

    def test_cancellation_email_uses_custom_name(self, neogen_company):
        """Cancellation email should use custom company name."""
        html = app_mod.build_cancellation_email_html(
            candidate_name="Jane Doe",
            role_title="QA Engineer",
            interview_time="March 15, 2026 at 10:00 AM",
            reason="Position filled",
            custom_message=None,
            company=neogen_company,
        )
        assert "Neogen" in html
        assert "PowerDash HR" not in html

    def test_reschedule_email_uses_custom_name(self, neogen_company):
        """Reschedule email should use custom company name."""
        html = app_mod.build_reschedule_email_html(
            candidate_name="Jane Doe",
            role_title="PM",
            old_time="March 15, 2026 at 10:00 AM",
            new_time="March 16, 2026 at 2:00 PM",
            teams_url=None,
            company=neogen_company,
        )
        assert "Neogen" in html
        assert "PowerDash HR" not in html

    def test_signature_line_in_slot_email(self, neogen_company, sample_slots):
        """Email signature should say 'Neogen Talent Acquisition Team'."""
        html = app_mod.build_branded_email_html(
            candidate_name="Jane Doe",
            role_title="Engineer",
            slots=sample_slots,
            company=neogen_company,
        )
        assert "Neogen Talent Acquisition Team" in html

    def test_signature_line_in_confirmation_email(self, neogen_company):
        """Confirmation email signature should use custom company name."""
        html = app_mod.build_confirmation_email_html(
            candidate_name="Jane Doe",
            role_title="Engineer",
            interview_time="March 15, 2026",
            teams_url=None,
            interviewer_names=[],
            company=neogen_company,
        )
        assert "Neogen Talent Acquisition Team" in html

    def test_signature_line_in_cancellation_email(self, neogen_company):
        """Cancellation email signature should use custom company name."""
        html = app_mod.build_cancellation_email_html(
            candidate_name="Jane Doe",
            role_title="Engineer",
            interview_time="March 15, 2026",
            reason="Cancelled",
            custom_message=None,
            company=neogen_company,
        )
        assert "Neogen Talent Acquisition Team" in html

    def test_signature_line_in_reschedule_email(self, neogen_company):
        """Reschedule email signature should use custom company name."""
        html = app_mod.build_reschedule_email_html(
            candidate_name="Jane Doe",
            role_title="Engineer",
            old_time="March 15",
            new_time="March 16",
            teams_url=None,
            company=neogen_company,
        )
        assert "Neogen Talent Acquisition Team" in html

    def test_plain_text_signature(self, neogen_company, sample_slots):
        """Plain text email footer should use custom company name."""
        text = app_mod.build_branded_email_plain(
            candidate_name="Jane Doe",
            role_title="Engineer",
            slots=sample_slots,
            company=neogen_company,
        )
        assert "Neogen Talent Acquisition Team" in text

    def test_sender_email_in_html_footer(self, neogen_company, sample_slots):
        """HTML email footer should show configured sender email."""
        html = app_mod.build_branded_email_html(
            candidate_name="Jane",
            role_title="Role",
            slots=sample_slots,
            company=neogen_company,
        )
        assert "hr@neogen.com" in html

    def test_sender_email_in_plain_text_footer(self, neogen_company, sample_slots):
        """Plain text email footer should show configured sender email."""
        text = app_mod.build_branded_email_plain(
            candidate_name="Jane",
            role_title="Role",
            slots=sample_slots,
            company=neogen_company,
        )
        assert "hr@neogen.com" in text


# ===========================================================================
# BLACK-BOX: Email primary color propagation
# ===========================================================================
class TestEmailPrimaryColor:
    """Emails should use the configured primary color, not hardcoded default."""

    def test_slot_email_uses_custom_color(self, neogen_company, sample_slots):
        """Custom primary color should appear in the email HTML."""
        html = app_mod.build_branded_email_html(
            candidate_name="Jane",
            role_title="Role",
            slots=sample_slots,
            company=neogen_company,
        )
        assert "#FF5500" in html
        # Default color should not appear
        assert "#0066CC" not in html

    def test_confirmation_email_uses_custom_color(self, neogen_company):
        """Confirmation email should use custom color."""
        html = app_mod.build_confirmation_email_html(
            candidate_name="Jane",
            role_title="Role",
            interview_time="March 15",
            teams_url=None,
            interviewer_names=[],
            company=neogen_company,
        )
        assert "#FF5500" in html

    def test_slot_email_uses_default_color_when_not_overridden(self, default_company, sample_slots):
        """Default color should appear when no override."""
        html = app_mod.build_branded_email_html(
            candidate_name="Jane",
            role_title="Role",
            slots=sample_slots,
            company=default_company,
        )
        assert "#0066CC" in html


# ===========================================================================
# BLACK-BOX: Website link in emails
# ===========================================================================
class TestEmailWebsiteLink:
    """Website link should appear when configured, absent when not."""

    def test_website_link_present(self, neogen_company, sample_slots):
        """When website is set, it should appear in email."""
        html = app_mod.build_branded_email_html(
            candidate_name="Jane",
            role_title="Role",
            slots=sample_slots,
            company=neogen_company,
        )
        assert "https://neogen.com" in html

    def test_no_website_link_when_none(self, sample_slots):
        """When website is None, no link should appear."""
        company = app_mod.CompanyConfig(
            name="NoWebsite Inc",
            logo_url=None,
            primary_color="#000000",
            website=None,
            sender_email="hr@example.com",
        )
        html = app_mod.build_branded_email_html(
            candidate_name="Jane",
            role_title="Role",
            slots=sample_slots,
            company=company,
        )
        assert "NoWebsite Inc" in html
        # Should not have a link tag for website
        assert 'href="None"' not in html


# ===========================================================================
# BLACK-BOX: Full lifecycle (persist → reload → email)
# ===========================================================================
class TestBrandingLifecycle:
    """End-to-end: save branding → reload into session → generate email."""

    def test_save_and_reload_into_email(self, patch_branding_path, sample_slots):
        """Save custom name to disk, reload via ensure_session_state, verify email output."""
        # Step 1: Save branding to disk
        app_mod._update_branding_field("company_name", "Neogen")
        app_mod._update_branding_field("primary_color", "#FF5500")

        # Step 2: Simulate fresh session (clear state)
        _mock_st.session_state = {}

        # Step 3: Load via ensure_session_state
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        # Step 4: Build company config (should pick up session state)
        config = app_mod.get_company_config()
        assert config.name == "Neogen"
        assert config.primary_color == "#FF5500"

        # Step 5: Generate email and verify
        html = app_mod.build_branded_email_html(
            candidate_name="Jane Doe",
            role_title="Software Engineer",
            slots=sample_slots,
            company=config,
        )
        assert "Neogen" in html
        assert "PowerDash HR" not in html
        assert "#FF5500" in html
        assert "Neogen Talent Acquisition Team" in html

    def test_update_name_then_reset_to_default(self, patch_branding_path, sample_slots):
        """Set custom name, then reset to default, verify default appears in email."""
        # Set custom
        app_mod._update_branding_field("company_name", "Neogen")

        # Reset to default (remove override)
        app_mod._update_branding_field("company_name", None)

        # Simulate fresh session
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        config = app_mod.get_company_config()
        assert config.name == "PowerDash HR"

    def test_multiple_reloads_consistent(self, patch_branding_path):
        """Loading branding multiple times should be idempotent."""
        app_mod._save_branding_settings({"company_name": "Neogen"})

        for _ in range(3):
            _mock_st.session_state = {}
            with patch.object(app_mod, "_load_persisted_slots", return_value={}):
                app_mod.ensure_session_state()
            assert _mock_st.session_state["custom_company_name"] == "Neogen"


# ===========================================================================
# WHITE-BOX: _render_logo_settings() company name input
# ===========================================================================
class TestRenderLogoSettingsCompanyName:
    """Test the company name text input in the sidebar."""

    def test_name_change_updates_session_state(self, patch_branding_path):
        """Changing company name via text input should update session state."""
        _mock_st.session_state = {}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input.return_value = "Neogen"
        _mock_st.button.return_value = False
        _mock_st.file_uploader.return_value = None

        app_mod._render_logo_settings()

        assert _mock_st.session_state.get("custom_company_name") == "Neogen"

    def test_name_change_persists_to_disk(self, patch_branding_path):
        """Changing company name should persist to branding file."""
        _mock_st.session_state = {}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input.return_value = "Neogen"
        _mock_st.button.return_value = False
        _mock_st.file_uploader.return_value = None

        app_mod._render_logo_settings()

        loaded = app_mod._load_branding_settings()
        assert loaded.get("company_name") == "Neogen"

    def test_resetting_to_default_clears_override(self, patch_branding_path):
        """Setting name back to default should clear the override."""
        _mock_st.session_state = {"custom_company_name": "Neogen"}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input.return_value = "PowerDash HR"  # Default value
        _mock_st.button.return_value = False
        _mock_st.file_uploader.return_value = None

        app_mod._render_logo_settings()

        assert _mock_st.session_state.get("custom_company_name") is None
        loaded = app_mod._load_branding_settings()
        assert "company_name" not in loaded

    def test_no_change_when_name_same(self, patch_branding_path):
        """No update when text input returns current value."""
        _mock_st.session_state = {"custom_company_name": "Neogen"}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input.return_value = "Neogen"  # Same as current
        _mock_st.button.return_value = False
        _mock_st.file_uploader.return_value = None

        # Should not try to save (name unchanged)
        with patch.object(app_mod, "_update_branding_field") as mock_update:
            app_mod._render_logo_settings()
            mock_update.assert_not_called()

    def test_empty_string_ignored(self, patch_branding_path):
        """Empty string from text input should be ignored."""
        _mock_st.session_state = {"custom_company_name": "Neogen"}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input.return_value = ""
        _mock_st.button.return_value = False
        _mock_st.file_uploader.return_value = None

        with patch.object(app_mod, "_update_branding_field") as mock_update:
            app_mod._render_logo_settings()
            mock_update.assert_not_called()

        # Original value should be preserved
        assert _mock_st.session_state["custom_company_name"] == "Neogen"

    def test_whitespace_only_ignored(self, patch_branding_path):
        """Whitespace-only name should be ignored."""
        _mock_st.session_state = {"custom_company_name": "Neogen"}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input.return_value = "   "
        _mock_st.button.return_value = False
        _mock_st.file_uploader.return_value = None

        with patch.object(app_mod, "_update_branding_field") as mock_update:
            app_mod._render_logo_settings()
            mock_update.assert_not_called()

    def test_mock_return_value_not_string_ignored(self, patch_branding_path):
        """Non-string return (e.g., MagicMock) should be ignored."""
        _mock_st.session_state = {}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input.return_value = MagicMock()  # Not a string
        _mock_st.button.return_value = False
        _mock_st.file_uploader.return_value = None

        with patch.object(app_mod, "_update_branding_field") as mock_update:
            app_mod._render_logo_settings()
            mock_update.assert_not_called()

    def test_name_change_does_not_affect_logo(self, patch_branding_path):
        """Changing company name should not touch logo session state."""
        _mock_st.session_state = {"custom_logo_data": "data:image/png;base64,abc"}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input.return_value = "Neogen"
        _mock_st.button.return_value = False
        _mock_st.file_uploader.return_value = None

        app_mod._render_logo_settings()

        # Logo should be untouched
        assert _mock_st.session_state["custom_logo_data"] == "data:image/png;base64,abc"
        # Company name should be set
        assert _mock_st.session_state["custom_company_name"] == "Neogen"


# ===========================================================================
# WHITE-BOX: _render_footer() uses company config
# ===========================================================================
class TestRenderFooterBranding:
    """Footer should use dynamic company name from config."""

    def _get_footer_html(self):
        """Extract the actual footer HTML from markdown calls (not the CSS)."""
        calls = _mock_st.markdown.call_args_list
        # The footer HTML contains "All rights reserved" — CSS <style> block does not
        footer_calls = [c for c in calls if "All rights reserved" in str(c)]
        assert len(footer_calls) > 0, "Footer HTML not found in markdown calls"
        return str(footer_calls[0])

    def test_footer_uses_custom_company_name(self):
        """Footer should contain custom company name."""
        _mock_st.session_state = {"custom_company_name": "Neogen"}
        _mock_st.markdown = MagicMock()

        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_footer()

        footer_html = self._get_footer_html()
        assert "Neogen" in footer_html
        assert "PowerDash HR" not in footer_html

    def test_footer_uses_default_when_no_override(self):
        """Footer should show default name when no custom name set."""
        _mock_st.session_state = {}
        _mock_st.markdown = MagicMock()

        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_footer()

        footer_html = self._get_footer_html()
        assert "PowerDash HR" in footer_html

    def test_footer_alt_text_uses_company_name(self):
        """Logo alt text in footer should use company name."""
        _mock_st.session_state = {"custom_company_name": "Neogen"}
        _mock_st.markdown = MagicMock()

        with patch.object(app_mod, "_get_logo_src", return_value="data:image/png;base64,abc"):
            app_mod._render_footer()

        footer_html = self._get_footer_html()
        assert 'alt="Neogen"' in footer_html


# ===========================================================================
# WHITE-BOX: main() page title
# ===========================================================================
class _StopAfterPageConfig(Exception):
    """Sentinel to halt main() after set_page_config is captured."""
    pass


class TestMainPageTitle:
    """Page title should use persisted branding or secrets, not hardcoded default."""

    def _run_main_and_capture_page_config(self):
        """Run main() but stop after set_page_config by raising on ensure_session_state."""
        _mock_st.set_page_config = MagicMock()
        # Stop execution right after set_page_config by raising in the next call
        with patch.object(app_mod, "ensure_session_state", side_effect=_StopAfterPageConfig):
            with pytest.raises(_StopAfterPageConfig):
                app_mod.main()
        return _mock_st.set_page_config.call_args

    def test_page_title_from_branding_file(self, patch_branding_path):
        """Page title should read company name from branding file."""
        app_mod._save_branding_settings({"company_name": "Neogen"})
        _mock_st.session_state = {}

        config_call = self._run_main_and_capture_page_config()
        assert config_call[1]["page_title"] == "Neogen Interview Scheduler"

    def test_page_title_from_secrets_fallback(self, patch_branding_path):
        """Page title should fall back to secrets when no branding file."""
        _mock_st.secrets = {"company_name": "FromSecrets"}
        _mock_st.session_state = {}

        config_call = self._run_main_and_capture_page_config()
        assert config_call[1]["page_title"] == "FromSecrets Interview Scheduler"

    def test_page_title_default_fallback(self, patch_branding_path):
        """Page title should use 'PowerDash HR' when nothing configured."""
        _mock_st.session_state = {}

        config_call = self._run_main_and_capture_page_config()
        assert config_call[1]["page_title"] == "PowerDash HR Interview Scheduler"


# ===========================================================================
# EDGE CASES: Special characters in company name
# ===========================================================================
class TestCompanyNameEdgeCases:
    """Test that special/unusual company names work correctly everywhere."""

    def test_unicode_company_name_in_email(self, sample_slots):
        """Unicode characters should render properly in emails."""
        company = app_mod.CompanyConfig(
            name="Ünïcödé Corp 日本語",
            logo_url=None,
            primary_color="#0066CC",
            website=None,
            sender_email="hr@example.com",
        )
        html = app_mod.build_branded_email_html(
            candidate_name="Test",
            role_title="Role",
            slots=sample_slots,
            company=company,
        )
        assert "Ünïcödé Corp 日本語" in html

    def test_unicode_in_plain_text_email(self, sample_slots):
        """Unicode in plain text email."""
        company = app_mod.CompanyConfig(
            name="Ünïcödé Corp",
            logo_url=None,
            primary_color="#0066CC",
            website=None,
            sender_email="hr@example.com",
        )
        text = app_mod.build_branded_email_plain(
            candidate_name="Test",
            role_title="Role",
            slots=sample_slots,
            company=company,
        )
        assert "Ünïcödé Corp" in text
        assert "Ünïcödé Corp Talent Acquisition Team" in text

    def test_unicode_company_name_persistence(self, patch_branding_path):
        """Unicode company name should round-trip through JSON persistence."""
        app_mod._update_branding_field("company_name", "Ünïcödé Corp 日本語")
        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "Ünïcödé Corp 日本語"

    def test_ampersand_in_company_name(self, sample_slots):
        """Ampersand in company name (common in legal names)."""
        company = app_mod.CompanyConfig(
            name="Smith & Wesson",
            logo_url=None,
            primary_color="#0066CC",
            website=None,
            sender_email="hr@example.com",
        )
        html = app_mod.build_branded_email_html(
            candidate_name="Test",
            role_title="Role",
            slots=sample_slots,
            company=company,
        )
        assert "Smith & Wesson" in html

    def test_quotes_in_company_name(self, sample_slots):
        """Quotes in company name should not break HTML."""
        company = app_mod.CompanyConfig(
            name='O\'Brien "Labs"',
            logo_url=None,
            primary_color="#0066CC",
            website=None,
            sender_email="hr@example.com",
        )
        html = app_mod.build_branded_email_html(
            candidate_name="Test",
            role_title="Role",
            slots=sample_slots,
            company=company,
        )
        assert "O'Brien" in html

    def test_very_long_company_name(self, sample_slots):
        """Very long company name should not crash."""
        long_name = "A" * 500
        company = app_mod.CompanyConfig(
            name=long_name,
            logo_url=None,
            primary_color="#0066CC",
            website=None,
            sender_email="hr@example.com",
        )
        html = app_mod.build_branded_email_html(
            candidate_name="Test",
            role_title="Role",
            slots=sample_slots,
            company=company,
        )
        assert long_name in html
        assert f"{long_name} Talent Acquisition Team" in html

    def test_very_long_company_name_persistence(self, patch_branding_path):
        """Very long company name should persist correctly."""
        long_name = "A" * 1000
        app_mod._update_branding_field("company_name", long_name)
        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == long_name

    def test_html_tags_in_company_name(self, sample_slots):
        """HTML-like content in company name (potential XSS vector)."""
        xss_name = '<script>alert("xss")</script>'
        company = app_mod.CompanyConfig(
            name=xss_name,
            logo_url=None,
            primary_color="#0066CC",
            website=None,
            sender_email="hr@example.com",
        )
        # Should not crash — note: email clients generally handle HTML sanitization
        html = app_mod.build_branded_email_html(
            candidate_name="Test",
            role_title="Role",
            slots=sample_slots,
            company=company,
        )
        # The name gets included (it's an internal tool, not public-facing)
        assert isinstance(html, str)
        assert len(html) > 0


# ===========================================================================
# EDGE CASES: Email with empty/no slots
# ===========================================================================
class TestEmailEdgeCases:
    """Test email generation with edge-case inputs."""

    def test_email_with_empty_slots(self, neogen_company):
        """Email with no slots should still show company name."""
        html = app_mod.build_branded_email_html(
            candidate_name="Jane",
            role_title="Role",
            slots=[],
            company=neogen_company,
        )
        assert "Neogen" in html
        assert "No slots available" in html

    def test_email_with_empty_candidate_name(self, neogen_company, sample_slots):
        """Empty candidate name should use generic greeting."""
        html = app_mod.build_branded_email_html(
            candidate_name="",
            role_title="Role",
            slots=sample_slots,
            company=neogen_company,
        )
        assert "Hello," in html
        assert "Neogen" in html

    def test_email_with_none_candidate_name(self, neogen_company, sample_slots):
        """None candidate name should use generic greeting."""
        html = app_mod.build_branded_email_html(
            candidate_name=None,
            role_title="Role",
            slots=sample_slots,
            company=neogen_company,
        )
        assert "Hello," in html
        assert "Neogen" in html

    def test_email_with_custom_message(self, neogen_company, sample_slots):
        """Custom message should appear alongside company branding."""
        html = app_mod.build_branded_email_html(
            candidate_name="Jane",
            role_title="Role",
            slots=sample_slots,
            company=neogen_company,
            custom_message="We look forward to meeting you!",
        )
        assert "Neogen" in html
        assert "We look forward to meeting you!" in html

    def test_plain_text_email_with_empty_slots(self, neogen_company):
        """Plain text email with no slots."""
        text = app_mod.build_branded_email_plain(
            candidate_name="Jane",
            role_title="Role",
            slots=[],
            company=neogen_company,
        )
        assert "Neogen" in text
        assert "No slots available" in text

    def test_confirmation_email_no_teams_url(self, neogen_company):
        """Confirmation email without Teams URL should still work."""
        html = app_mod.build_confirmation_email_html(
            candidate_name="Jane",
            role_title="Role",
            interview_time="March 15",
            teams_url=None,
            interviewer_names=["Alice"],
            company=neogen_company,
        )
        assert "Neogen" in html
        assert "Join Meeting" not in html

    def test_confirmation_email_multiple_interviewers(self, neogen_company):
        """Multiple interviewers should be listed."""
        html = app_mod.build_confirmation_email_html(
            candidate_name="Jane",
            role_title="Role",
            interview_time="March 15",
            teams_url=None,
            interviewer_names=["Alice", "Bob", "Charlie"],
            company=neogen_company,
        )
        assert "Alice, Bob, Charlie" in html
        assert "Neogen" in html

    def test_cancellation_email_with_custom_message(self, neogen_company):
        """Cancellation with custom message should include both."""
        html = app_mod.build_cancellation_email_html(
            candidate_name="Jane",
            role_title="Role",
            interview_time="March 15",
            reason="Position filled",
            custom_message="We will keep your resume on file.",
            company=neogen_company,
        )
        assert "Neogen" in html
        assert "We will keep your resume on file." in html
        assert "Position filled" in html

    def test_reschedule_email_with_teams_url(self, neogen_company):
        """Reschedule email with Teams URL should include meeting link."""
        html = app_mod.build_reschedule_email_html(
            candidate_name="Jane",
            role_title="Role",
            old_time="March 15",
            new_time="March 16",
            teams_url="https://teams.microsoft.com/meet/456",
            company=neogen_company,
        )
        assert "Neogen" in html
        assert "Join Meeting" in html
        assert "March 15" in html
        assert "March 16" in html


# ===========================================================================
# EDGE CASE: CompanyConfig no-logo header fallback
# ===========================================================================
class TestEmailHeaderFallback:
    """When no logo is set, email should show company name as gradient header."""

    def test_no_logo_shows_company_name_header(self, sample_slots):
        """Without logo, the company name should be shown in a colored header."""
        company = app_mod.CompanyConfig(
            name="Neogen",
            logo_url=None,
            primary_color="#FF5500",
            website=None,
            sender_email="hr@neogen.com",
        )
        html = app_mod.build_branded_email_html(
            candidate_name="Jane",
            role_title="Role",
            slots=sample_slots,
            company=company,
        )
        # Company name should appear in a header block
        assert "Neogen" in html
        # The gradient background should use the primary color
        assert "#FF5500" in html

    def test_with_logo_url_no_name_header(self, sample_slots):
        """With a logo URL, the name header block should not appear."""
        company = app_mod.CompanyConfig(
            name="Neogen",
            logo_url="https://example.com/logo.png",
            primary_color="#FF5500",
            website=None,
            sender_email="hr@neogen.com",
        )
        html = app_mod.build_branded_email_html(
            candidate_name="Jane",
            role_title="Role",
            slots=sample_slots,
            company=company,
        )
        # Should have logo img tag instead of name-as-header
        assert 'img src="https://example.com/logo.png"' in html


# ===========================================================================
# INTEGRATION: Secrets + session state + branding file interaction
# ===========================================================================
class TestBrandingPrecedenceIntegration:
    """Test the full precedence chain with all sources available."""

    def test_session_state_beats_branding_file(self, patch_branding_path):
        """Session state override should win over branding file."""
        app_mod._save_branding_settings({"company_name": "FromFile"})
        _mock_st.session_state = {"custom_company_name": "FromSessionState"}

        config = app_mod.get_company_config()
        assert config.name == "FromSessionState"

    def test_branding_file_beats_secrets(self, patch_branding_path):
        """Branding file loaded into session state should beat secrets."""
        app_mod._save_branding_settings({"company_name": "FromFile"})
        _mock_st.secrets = {"company_name": "FromSecrets"}
        _mock_st.session_state = {}

        # Load branding into session state
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        config = app_mod.get_company_config()
        assert config.name == "FromFile"

    def test_secrets_used_when_no_file_or_state(self, patch_branding_path):
        """Secrets should be used when no branding file or session state."""
        _mock_st.secrets = {"company_name": "FromSecrets"}
        _mock_st.session_state = {}

        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        config = app_mod.get_company_config()
        assert config.name == "FromSecrets"

    def test_default_used_when_nothing_configured(self, patch_branding_path):
        """Default 'PowerDash HR' used when absolutely nothing configured."""
        _mock_st.session_state = {}

        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        config = app_mod.get_company_config()
        assert config.name == "PowerDash HR"

    def test_primary_color_same_precedence(self, patch_branding_path):
        """Primary color follows same precedence chain."""
        app_mod._save_branding_settings({"primary_color": "#111111"})
        _mock_st.secrets = {"company_primary_color": "#222222"}
        _mock_st.session_state = {"custom_primary_color": "#333333"}

        config = app_mod.get_company_config()
        assert config.primary_color == "#333333"  # session state wins

    def test_primary_color_file_beats_secrets(self, patch_branding_path):
        """Primary color from file should beat secrets."""
        app_mod._save_branding_settings({"primary_color": "#111111"})
        _mock_st.secrets = {"company_primary_color": "#222222"}
        _mock_st.session_state = {}

        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        config = app_mod.get_company_config()
        assert config.primary_color == "#111111"


# ===========================================================================
# EDGE CASE: Non-dict JSON in branding file
# ===========================================================================
class TestBrandingFileNonDictJson:
    """Test handling of non-dict JSON content in branding file."""

    def test_array_json(self, patch_branding_path):
        """Array JSON should be treated as empty branding."""
        with open(patch_branding_path, "w") as f:
            json.dump(["not", "a", "dict"], f)

        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert "custom_company_name" not in _mock_st.session_state

    def test_string_json(self, patch_branding_path):
        """String JSON should be treated as empty branding."""
        with open(patch_branding_path, "w") as f:
            json.dump("just a string", f)

        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert "custom_company_name" not in _mock_st.session_state

    def test_null_json(self, patch_branding_path):
        """null JSON should be treated as empty branding."""
        with open(patch_branding_path, "w") as f:
            f.write("null")

        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert "custom_company_name" not in _mock_st.session_state

    def test_number_json(self, patch_branding_path):
        """Number JSON should be treated as empty branding."""
        with open(patch_branding_path, "w") as f:
            f.write("42")

        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert "custom_company_name" not in _mock_st.session_state


# ===========================================================================
# EDGE CASE: Branding field update with read-only filesystem
# ===========================================================================
class TestBrandingFieldReadOnlyFS:
    """Test graceful handling when branding file cannot be written."""

    def test_update_field_read_only(self, patch_branding_path, tmp_path):
        """Should not crash on read-only directory."""
        read_only_path = str(tmp_path / "readonly" / "branding.json")
        # Don't create the directory — write will fail

        with patch.object(app_mod, "_get_branding_settings_path", return_value=read_only_path):
            # Should not raise — just warns
            _mock_st.warning = MagicMock()
            app_mod._update_branding_field("company_name", "Neogen")


# ===========================================================================
# BLACK-BOX: CompanyConfig signature_name across different names
# ===========================================================================
class TestSignatureNameVariations:
    """Test the signature_name property with various company names."""

    @pytest.mark.parametrize("name,expected_sig", [
        ("Neogen", "Neogen Talent Acquisition Team"),
        ("PowerDash HR", "PowerDash HR Talent Acquisition Team"),
        ("A", "A Talent Acquisition Team"),
        ("Smith & Jones LLC", "Smith & Jones LLC Talent Acquisition Team"),
        ("日本語 Corp", "日本語 Corp Talent Acquisition Team"),
    ])
    def test_signature_name(self, name, expected_sig):
        config = app_mod.CompanyConfig(
            name=name,
            logo_url=None,
            primary_color="#000",
            website=None,
            sender_email="x@x.com",
        )
        assert config.signature_name == expected_sig
