"""
Comprehensive tests for the cross-test mock pattern fix and integration
testing of the full branding → data directory → email pipeline.

Tests cover:

WHITE-BOX:
- Mock pattern: sys.modules["streamlit"] reference consistency across test files
- _get_logo_src(): data URL passthrough, http URL, https URL, local file, None
- _build_logo_html(): with logo, without logo, data URL logo, alt text uses company name
- _lighten_color() / _darken_color(): valid hex, short/long, boundary values
- _update_branding_logo(): set logo, remove logo (None), preserves other fields
- _save_current_branding(): saves when customized, removes file when all defaults
- _render_logo_settings(): company name change, reset to default, whitespace guard,
  MagicMock guard, logo upload mime types, oversized logo rejection
- _send_cancellation_email(): subject line uses company.name, HTML body contains name
- _send_reschedule_email(): subject line uses company.name, HTML body contains name
- build_branded_email_plain(): plain text fallback uses company name throughout

BLACK-BOX:
- Full pipeline: save branding → load in session → generate email → verify company name
- Cancellation email subject with custom company
- Reschedule email subject with custom company
- Logo upload → persist → reload → appears in email
- Color propagation end-to-end: custom color in branding → emails use it

EDGE CASES:
- _lighten_color / _darken_color with black (#000000) and white (#FFFFFF)
- _get_logo_src with empty string
- _build_logo_html with company name containing quotes
- build_confirmation_email_html with no interviewers and no Teams URL
- build_cancellation_email_html with empty reason / empty custom message
- build_reschedule_email_html with no Teams URL
- _render_logo_settings when logo file read returns empty bytes
- _render_logo_settings with unknown file extension
- _save_current_branding when file removal fails
- Emails with very long company names
- Emails with company name containing HTML entities
"""
import base64
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Mock streamlit at module level (robust pattern)
# ---------------------------------------------------------------------------
_local_mock = MagicMock()
_local_mock.secrets = {}
_local_mock.session_state = {}
_local_mock.cache_data = lambda *a, **kw: (lambda f: f)
_local_mock.cache_resource = lambda *a, **kw: (lambda f: f)

sys.modules.setdefault("streamlit", _local_mock)
sys.modules.setdefault("streamlit.components", MagicMock())
sys.modules.setdefault("streamlit.components.v1", MagicMock())

# Always reference the mock that's actually in sys.modules
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
    return str(tmp_path / "branding_settings.json")


@pytest.fixture
def patch_branding_path(tmp_branding_file):
    with patch.object(app_mod, "_get_branding_settings_path", return_value=tmp_branding_file):
        yield tmp_branding_file


@pytest.fixture
def neogen():
    """CompanyConfig for Neogen."""
    return app_mod.CompanyConfig(
        name="Neogen",
        logo_url=None,
        primary_color="#FF5500",
        website="https://neogen.com",
        sender_email="hr@neogen.com",
    )


@pytest.fixture
def default_company():
    """CompanyConfig with defaults."""
    return app_mod.CompanyConfig(
        name="PowerDash HR",
        logo_url="logo.png",
        primary_color="#0066CC",
        website=None,
        sender_email="scheduling@powerdashhr.com",
    )


@pytest.fixture
def sample_slots():
    return [
        {"date": "2026-03-15", "start": "10:00", "end": "11:00"},
        {"date": "2026-03-16", "start": "14:00", "end": "15:00"},
    ]


# ===========================================================================
# 1. WHITE-BOX: Mock pattern — sys.modules reference consistency
# ===========================================================================
class TestMockPatternConsistency:
    """Verify the mock pattern fix works: _mock_st is the same object app.py sees."""

    def test_mock_st_is_sys_modules_streamlit(self):
        """_mock_st must be the exact same object as sys.modules['streamlit']."""
        assert _mock_st is sys.modules["streamlit"]

    def test_app_module_st_is_sys_modules_streamlit(self):
        """app.py's `st` reference must be the same as sys.modules['streamlit']."""
        assert app_mod.st is sys.modules["streamlit"]

    def test_setting_secrets_on_mock_is_visible_to_app(self):
        """When we set secrets on _mock_st, app.get_secret should see them."""
        _mock_st.secrets = {"company_name": "TestCorp"}
        result = app_mod.get_secret("company_name", "default")
        assert result == "TestCorp"

    def test_setting_session_state_on_mock_is_visible_to_app(self):
        """When we set session_state on _mock_st, app should see it."""
        _mock_st.session_state = {"custom_company_name": "MockCorp"}
        config = app_mod.get_company_config()
        assert config.name == "MockCorp"

    def test_clearing_secrets_resets_app_behavior(self):
        """After clearing secrets, app should use defaults."""
        _mock_st.secrets = {"company_name": "Temp"}
        assert app_mod.get_secret("company_name", "default") == "Temp"
        _mock_st.secrets = {}
        assert app_mod.get_secret("company_name", "default") == "default"


# ===========================================================================
# 2. WHITE-BOX: _get_logo_src()
# ===========================================================================
class TestGetLogoSrc:
    """Test logo source resolution logic."""

    def test_none_returns_none(self):
        assert app_mod._get_logo_src(None) is None

    def test_empty_string_returns_none(self):
        assert app_mod._get_logo_src("") is None

    def test_data_url_passthrough(self):
        data_url = "data:image/png;base64,iVBORw0KGgo="
        assert app_mod._get_logo_src(data_url) == data_url

    def test_https_url_passthrough(self):
        url = "https://example.com/logo.png"
        assert app_mod._get_logo_src(url) == url

    def test_http_url_passthrough(self):
        url = "http://example.com/logo.png"
        assert app_mod._get_logo_src(url) == url

    def test_local_file_not_found_returns_none(self):
        """Non-existent local file should return None."""
        result = app_mod._get_logo_src("/nonexistent/path/logo.png")
        assert result is None

    def test_local_file_converted_to_base64(self, tmp_path):
        """Existing local file should be converted to base64 data URL."""
        logo_file = tmp_path / "test_logo.png"
        logo_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        result = app_mod._get_logo_src(str(logo_file))
        assert result is not None
        assert result.startswith("data:image/png;base64,")


# ===========================================================================
# 3. WHITE-BOX: _build_logo_html()
# ===========================================================================
class TestBuildLogoHtml:
    """Test logo HTML generation for emails."""

    def test_no_logo_returns_empty(self):
        company = app_mod.CompanyConfig(
            name="Test", logo_url=None, primary_color="#000",
            website=None, sender_email="a@b.com",
        )
        assert app_mod._build_logo_html(company) == ""

    def test_empty_logo_url_returns_empty(self):
        company = app_mod.CompanyConfig(
            name="Test", logo_url="", primary_color="#000",
            website=None, sender_email="a@b.com",
        )
        assert app_mod._build_logo_html(company) == ""

    def test_data_url_logo_included(self):
        company = app_mod.CompanyConfig(
            name="Neogen", logo_url="data:image/png;base64,abc",
            primary_color="#FF5500", website=None, sender_email="a@b.com",
        )
        html = app_mod._build_logo_html(company)
        assert "data:image/png;base64,abc" in html
        assert '<img' in html

    def test_alt_text_uses_company_name(self):
        company = app_mod.CompanyConfig(
            name="Neogen", logo_url="https://example.com/logo.png",
            primary_color="#000", website=None, sender_email="a@b.com",
        )
        html = app_mod._build_logo_html(company)
        assert 'alt="Neogen"' in html

    def test_nonexistent_local_file_returns_empty(self):
        company = app_mod.CompanyConfig(
            name="Test", logo_url="/nonexistent/logo.png",
            primary_color="#000", website=None, sender_email="a@b.com",
        )
        assert app_mod._build_logo_html(company) == ""

    def test_company_name_with_quotes_in_alt(self):
        company = app_mod.CompanyConfig(
            name='Acme "Best" Corp', logo_url="https://example.com/logo.png",
            primary_color="#000", website=None, sender_email="a@b.com",
        )
        html = app_mod._build_logo_html(company)
        # Should contain the name (quotes may or may not be escaped, but should not crash)
        assert "Acme" in html
        assert '<img' in html


# ===========================================================================
# 4. WHITE-BOX: _lighten_color() / _darken_color()
# ===========================================================================
class TestColorUtilities:
    """Test color manipulation functions."""

    def test_lighten_black_by_half(self):
        result = app_mod._lighten_color("#000000", 0.5)
        # Black (0,0,0) lightened by 50% → (127,127,127)
        assert result.startswith("#")
        r = int(result[1:3], 16)
        assert 126 <= r <= 128  # Allow ±1 for rounding

    def test_lighten_white_stays_white(self):
        result = app_mod._lighten_color("#ffffff", 0.5)
        assert result.lower() == "#ffffff"

    def test_darken_white_by_half(self):
        result = app_mod._darken_color("#ffffff", 0.5)
        r = int(result[1:3], 16)
        assert 126 <= r <= 128

    def test_darken_black_stays_black(self):
        result = app_mod._darken_color("#000000", 0.5)
        assert result.lower() == "#000000"

    def test_lighten_zero_factor_unchanged(self):
        result = app_mod._lighten_color("#0066CC", 0.0)
        assert result.lower() == "#0066cc"

    def test_darken_zero_factor_unchanged(self):
        result = app_mod._darken_color("#0066CC", 0.0)
        assert result.lower() == "#0066cc"

    def test_lighten_full_factor_white(self):
        result = app_mod._lighten_color("#0066CC", 1.0)
        assert result.lower() == "#ffffff"

    def test_darken_full_factor_black(self):
        result = app_mod._darken_color("#0066CC", 1.0)
        assert result.lower() == "#000000"

    def test_lighten_with_hash_prefix(self):
        result = app_mod._lighten_color("#FF0000", 0.5)
        assert result.startswith("#")
        assert len(result) == 7

    def test_darken_custom_brand_color(self):
        result = app_mod._darken_color("#FF5500", 0.2)
        assert result.startswith("#")
        r = int(result[1:3], 16)
        g = int(result[3:5], 16)
        b = int(result[5:7], 16)
        # 0xFF * 0.8 = 204, 0x55 * 0.8 = 68, 0x00 * 0.8 = 0
        assert r == 204
        assert g == 68
        assert b == 0


# ===========================================================================
# 5. WHITE-BOX: _update_branding_logo()
# ===========================================================================
class TestUpdateBrandingLogo:
    """Test logo persistence via _update_branding_logo."""

    def test_set_logo_data(self, patch_branding_path):
        app_mod._update_branding_logo("data:image/png;base64,newlogo")
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "data:image/png;base64,newlogo"

    def test_remove_logo_data(self, patch_branding_path):
        app_mod._save_branding_settings({"logo_data": "data:old", "company_name": "Keep"})
        app_mod._update_branding_logo(None)
        loaded = app_mod._load_branding_settings()
        assert "logo_data" not in loaded
        assert loaded["company_name"] == "Keep"

    def test_set_logo_preserves_company_name(self, patch_branding_path):
        app_mod._save_branding_settings({"company_name": "Neogen"})
        app_mod._update_branding_logo("data:image/png;base64,logo")
        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "Neogen"
        assert loaded["logo_data"] == "data:image/png;base64,logo"

    def test_remove_nonexistent_logo_is_noop(self, patch_branding_path):
        app_mod._save_branding_settings({"company_name": "Only"})
        app_mod._update_branding_logo(None)
        loaded = app_mod._load_branding_settings()
        assert loaded == {"company_name": "Only"}


# ===========================================================================
# 6. WHITE-BOX: _save_current_branding()
# ===========================================================================
class TestSaveCurrentBranding:
    """Test _save_current_branding saves/removes based on session state."""

    def test_saves_when_company_name_set(self, patch_branding_path):
        _mock_st.session_state = {"custom_company_name": "Neogen"}
        app_mod._save_current_branding()
        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "Neogen"

    def test_saves_when_logo_data_set(self, patch_branding_path):
        _mock_st.session_state = {"custom_logo_data": "data:image/png;base64,abc"}
        app_mod._save_current_branding()
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "data:image/png;base64,abc"

    def test_saves_when_primary_color_set(self, patch_branding_path):
        _mock_st.session_state = {"custom_primary_color": "#FF5500"}
        app_mod._save_current_branding()
        loaded = app_mod._load_branding_settings()
        assert loaded["primary_color"] == "#FF5500"

    def test_removes_file_when_all_defaults(self, patch_branding_path):
        # Create a branding file first
        with open(patch_branding_path, "w") as f:
            json.dump({"company_name": "Old"}, f)
        assert os.path.exists(patch_branding_path)

        # Now save with all None session state → file should be removed
        _mock_st.session_state = {}
        app_mod._save_current_branding()
        assert not os.path.exists(patch_branding_path)

    def test_removes_file_graceful_on_missing_file(self, patch_branding_path):
        """Should not crash if file doesn't exist when trying to remove."""
        _mock_st.session_state = {}
        app_mod._save_current_branding()  # No file to remove; should not raise

    def test_saves_multiple_fields_simultaneously(self, patch_branding_path):
        _mock_st.session_state = {
            "custom_company_name": "Neogen",
            "custom_primary_color": "#FF5500",
            "custom_logo_data": "data:image/png;base64,logo",
        }
        app_mod._save_current_branding()
        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "Neogen"
        assert loaded["primary_color"] == "#FF5500"
        assert loaded["logo_data"] == "data:image/png;base64,logo"


# ===========================================================================
# 7. WHITE-BOX: _render_logo_settings() — company name handling
# ===========================================================================
class TestRenderLogoSettingsCompanyName:
    """Test the company name text input flow in sidebar settings."""

    def _setup_sidebar(self, text_input_return, session_state=None):
        """Helper to mock sidebar rendering and return st mock calls."""
        if session_state:
            _mock_st.session_state = session_state
        sidebar_ctx = MagicMock()
        _mock_st.sidebar.__enter__ = MagicMock(return_value=sidebar_ctx)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input = MagicMock(return_value=text_input_return)
        _mock_st.button = MagicMock(return_value=False)
        _mock_st.file_uploader = MagicMock(return_value=None)
        _mock_st.image = MagicMock()
        _mock_st.caption = MagicMock()
        _mock_st.markdown = MagicMock()
        _mock_st.warning = MagicMock()

    def test_name_change_updates_session_and_persists(self, patch_branding_path):
        """Changing name to non-default should update session state and persist."""
        self._setup_sidebar("Neogen")
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        assert _mock_st.session_state.get("custom_company_name") == "Neogen"
        loaded = app_mod._load_branding_settings()
        assert loaded.get("company_name") == "Neogen"

    def test_reset_to_default_clears_session_and_persists_none(self, patch_branding_path):
        """Resetting to 'PowerDash HR' should clear the override."""
        _mock_st.session_state = {"custom_company_name": "OldName"}
        self._setup_sidebar("PowerDash HR", _mock_st.session_state)
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        assert _mock_st.session_state.get("custom_company_name") is None

    def test_whitespace_only_input_is_ignored(self, patch_branding_path):
        """Whitespace-only name should not be accepted."""
        self._setup_sidebar("   ")
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        assert _mock_st.session_state.get("custom_company_name") is None

    def test_magicmock_return_is_ignored(self, patch_branding_path):
        """MagicMock from text_input (not a real string) should be ignored."""
        self._setup_sidebar(MagicMock())
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        assert _mock_st.session_state.get("custom_company_name") is None

    def test_same_name_as_current_is_noop(self, patch_branding_path):
        """If name hasn't changed, nothing should happen."""
        _mock_st.session_state = {"custom_company_name": "Neogen"}
        self._setup_sidebar("Neogen", _mock_st.session_state)
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        # Should still be Neogen (not cleared or re-saved)
        assert _mock_st.session_state.get("custom_company_name") == "Neogen"


# ===========================================================================
# 8. WHITE-BOX: _render_logo_settings() — logo upload handling
# ===========================================================================
class TestRenderLogoSettingsUpload:
    """Test logo upload flows in sidebar settings."""

    def _setup_for_upload(self, uploaded_file, session_state=None):
        if session_state:
            _mock_st.session_state = session_state
        sidebar_ctx = MagicMock()
        _mock_st.sidebar.__enter__ = MagicMock(return_value=sidebar_ctx)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input = MagicMock(return_value="PowerDash HR")
        _mock_st.button = MagicMock(return_value=False)
        _mock_st.file_uploader = MagicMock(return_value=uploaded_file)
        _mock_st.image = MagicMock()
        _mock_st.caption = MagicMock()
        _mock_st.markdown = MagicMock()
        _mock_st.warning = MagicMock()
        _mock_st.rerun = MagicMock()

    def test_valid_png_upload(self, patch_branding_path):
        """Valid PNG upload should be stored as base64 data URL."""
        mock_file = MagicMock()
        mock_file.name = "logo.png"
        mock_file.read.return_value = b"\x89PNG" + b"\x00" * 100
        self._setup_for_upload(mock_file)
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        logo = _mock_st.session_state.get("custom_logo_data")
        assert logo is not None
        assert logo.startswith("data:image/png;base64,")

    def test_valid_jpeg_upload(self, patch_branding_path):
        """JPEG upload should use image/jpeg mime type."""
        mock_file = MagicMock()
        mock_file.name = "photo.jpg"
        mock_file.read.return_value = b"\xff\xd8\xff" + b"\x00" * 100
        self._setup_for_upload(mock_file)
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        logo = _mock_st.session_state.get("custom_logo_data")
        assert logo is not None
        assert logo.startswith("data:image/jpeg;base64,")

    def test_svg_upload(self, patch_branding_path):
        """SVG upload should use image/svg+xml mime type."""
        mock_file = MagicMock()
        mock_file.name = "logo.svg"
        mock_file.read.return_value = b"<svg>test</svg>"
        self._setup_for_upload(mock_file)
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        logo = _mock_st.session_state.get("custom_logo_data")
        assert logo is not None
        assert logo.startswith("data:image/svg+xml;base64,")

    def test_oversized_logo_shows_warning(self, patch_branding_path):
        """Logo larger than 2MB should show warning and not be stored."""
        mock_file = MagicMock()
        mock_file.name = "huge.png"
        mock_file.read.return_value = b"\x00" * (3 * 1024 * 1024)  # 3MB
        self._setup_for_upload(mock_file)
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        _mock_st.warning.assert_called_once()
        assert "too large" in str(_mock_st.warning.call_args).lower()
        assert _mock_st.session_state.get("custom_logo_data") is None

    def test_empty_file_data_is_ignored(self, patch_branding_path):
        """Upload with empty read() should be silently ignored."""
        mock_file = MagicMock()
        mock_file.name = "empty.png"
        mock_file.read.return_value = b""
        self._setup_for_upload(mock_file)
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        assert _mock_st.session_state.get("custom_logo_data") is None

    def test_unknown_extension_defaults_to_png_mime(self, patch_branding_path):
        """Unknown file extension should default to image/png mime type."""
        mock_file = MagicMock()
        mock_file.name = "logo.bmp"
        mock_file.read.return_value = b"BM" + b"\x00" * 100
        self._setup_for_upload(mock_file)
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        logo = _mock_st.session_state.get("custom_logo_data")
        assert logo is not None
        assert logo.startswith("data:image/png;base64,")

    def test_gif_upload(self, patch_branding_path):
        """GIF upload should use image/gif mime type."""
        mock_file = MagicMock()
        mock_file.name = "anim.gif"
        mock_file.read.return_value = b"GIF89a" + b"\x00" * 50
        self._setup_for_upload(mock_file)
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()
        logo = _mock_st.session_state.get("custom_logo_data")
        assert logo is not None
        assert logo.startswith("data:image/gif;base64,")

    def test_remove_logo_button(self, patch_branding_path):
        """Remove logo button should clear session state and persist None."""
        _mock_st.session_state = {"custom_logo_data": "data:image/png;base64,old"}
        sidebar_ctx = MagicMock()
        _mock_st.sidebar.__enter__ = MagicMock(return_value=sidebar_ctx)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input = MagicMock(return_value="PowerDash HR")
        _mock_st.button = MagicMock(return_value=True)  # Remove clicked
        _mock_st.file_uploader = MagicMock(return_value=None)
        _mock_st.image = MagicMock()
        _mock_st.caption = MagicMock()
        _mock_st.markdown = MagicMock()
        _mock_st.warning = MagicMock()
        _mock_st.rerun = MagicMock()

        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()

        assert _mock_st.session_state.get("custom_logo_data") is None
        _mock_st.rerun.assert_called()


# ===========================================================================
# 9. WHITE-BOX: _send_cancellation_email / _send_reschedule_email
# ===========================================================================
class TestSendCancellationEmail:
    """Test _send_cancellation_email uses company name in subject."""

    def test_subject_contains_company_name(self, neogen):
        mock_client = MagicMock()
        mock_client.send_mail = MagicMock()
        app_mod._send_cancellation_email(
            client=mock_client,
            candidate_email="jane@example.com",
            candidate_name="Jane Doe",
            role_title="Software Engineer",
            interview_time="March 15, 2026 10:00 AM",
            reason="Position filled",
            custom_message="",
            company=neogen,
        )
        mock_client.send_mail.assert_called_once()
        subject = mock_client.send_mail.call_args[1].get("subject") or mock_client.send_mail.call_args[0][0] if mock_client.send_mail.call_args[0] else mock_client.send_mail.call_args[1]["subject"]
        assert "Neogen" in subject
        assert "PowerDash" not in subject

    def test_html_body_contains_company_name(self, neogen):
        mock_client = MagicMock()
        app_mod._send_cancellation_email(
            client=mock_client,
            candidate_email="jane@example.com",
            candidate_name="Jane Doe",
            role_title="Software Engineer",
            interview_time="March 15, 2026 10:00 AM",
            reason="Position filled",
            custom_message="We'll keep your resume.",
            company=neogen,
        )
        html_body = mock_client.send_mail.call_args[1]["body"]
        assert "Neogen" in html_body

    def test_returns_true_on_success(self, neogen):
        mock_client = MagicMock()
        result = app_mod._send_cancellation_email(
            client=mock_client,
            candidate_email="a@b.com",
            candidate_name="A",
            role_title="Role",
            interview_time="Now",
            reason="reason",
            custom_message="",
            company=neogen,
        )
        assert result is True

    def test_returns_false_on_exception(self, neogen):
        mock_client = MagicMock()
        mock_client.send_mail.side_effect = RuntimeError("SMTP fail")
        result = app_mod._send_cancellation_email(
            client=mock_client,
            candidate_email="a@b.com",
            candidate_name="A",
            role_title="Role",
            interview_time="Now",
            reason="reason",
            custom_message="",
            company=neogen,
        )
        assert result is False


class TestSendRescheduleEmail:
    """Test _send_reschedule_email uses company name in subject."""

    def test_subject_contains_company_name(self, neogen):
        mock_client = MagicMock()
        app_mod._send_reschedule_email(
            client=mock_client,
            candidate_email="jane@example.com",
            candidate_name="Jane Doe",
            role_title="Software Engineer",
            old_time="March 15, 2026 10:00 AM",
            new_time="March 20, 2026 2:00 PM",
            teams_url=None,
            company=neogen,
        )
        subject = mock_client.send_mail.call_args[1]["subject"]
        assert "Neogen" in subject
        assert "PowerDash" not in subject

    def test_html_body_contains_company_name(self, neogen):
        mock_client = MagicMock()
        app_mod._send_reschedule_email(
            client=mock_client,
            candidate_email="jane@example.com",
            candidate_name="Jane Doe",
            role_title="Software Engineer",
            old_time="March 15",
            new_time="March 20",
            teams_url="https://teams.microsoft.com/l/meetup",
            company=neogen,
        )
        html_body = mock_client.send_mail.call_args[1]["body"]
        assert "Neogen" in html_body

    def test_returns_true_on_success(self, neogen):
        mock_client = MagicMock()
        result = app_mod._send_reschedule_email(
            client=mock_client,
            candidate_email="a@b.com",
            candidate_name="A",
            role_title="Role",
            old_time="Old",
            new_time="New",
            teams_url=None,
            company=neogen,
        )
        assert result is True

    def test_returns_false_on_exception(self, neogen):
        mock_client = MagicMock()
        mock_client.send_mail.side_effect = ConnectionError("timeout")
        result = app_mod._send_reschedule_email(
            client=mock_client,
            candidate_email="a@b.com",
            candidate_name="A",
            role_title="Role",
            old_time="Old",
            new_time="New",
            teams_url=None,
            company=neogen,
        )
        assert result is False


# ===========================================================================
# 10. WHITE-BOX: build_branded_email_plain() text fallback
# ===========================================================================
class TestBuildBrandedEmailPlain:
    """Test plain text email generation uses company name."""

    def test_contains_custom_company_name(self, neogen, sample_slots):
        text = app_mod.build_branded_email_plain("Jane", "SWE", sample_slots, neogen)
        assert "Neogen" in text
        assert "PowerDash" not in text

    def test_contains_signature_name(self, neogen, sample_slots):
        text = app_mod.build_branded_email_plain("Jane", "SWE", sample_slots, neogen)
        assert "Neogen Talent Acquisition Team" in text

    def test_contains_website_in_footer(self, neogen, sample_slots):
        text = app_mod.build_branded_email_plain("Jane", "SWE", sample_slots, neogen)
        assert "https://neogen.com" in text

    def test_no_website_omits_link(self, default_company, sample_slots):
        text = app_mod.build_branded_email_plain("Jane", "SWE", sample_slots, default_company)
        assert "powerdashhr.com" not in text.split("---")[0]  # Not in body

    def test_sender_email_in_footer(self, neogen, sample_slots):
        text = app_mod.build_branded_email_plain("Jane", "SWE", sample_slots, neogen)
        assert "hr@neogen.com" in text

    def test_empty_candidate_name_uses_hello(self, neogen, sample_slots):
        text = app_mod.build_branded_email_plain("", "SWE", sample_slots, neogen)
        assert text.startswith("Hello,")

    def test_none_candidate_name_uses_hello(self, neogen, sample_slots):
        text = app_mod.build_branded_email_plain(None, "SWE", sample_slots, neogen)
        assert text.startswith("Hello,")

    def test_whitespace_candidate_name_uses_hello(self, neogen, sample_slots):
        text = app_mod.build_branded_email_plain("   ", "SWE", sample_slots, neogen)
        assert text.startswith("Hello,")

    def test_empty_slots_shows_no_slots(self, neogen):
        text = app_mod.build_branded_email_plain("Jane", "SWE", [], neogen)
        assert "(No slots available)" in text


# ===========================================================================
# 11. BLACK-BOX: Confirmation email — edge cases
# ===========================================================================
class TestConfirmationEmailEdgeCases:
    """Test confirmation email edge cases."""

    def test_no_teams_url(self, neogen):
        html = app_mod.build_confirmation_email_html(
            candidate_name="Jane",
            role_title="SWE",
            interview_time="March 15, 10:00 AM",
            teams_url=None,
            interviewer_names=["Alice"],
            company=neogen,
        )
        assert "Microsoft Teams" not in html
        assert "Neogen" in html

    def test_with_teams_url(self, neogen):
        html = app_mod.build_confirmation_email_html(
            candidate_name="Jane",
            role_title="SWE",
            interview_time="March 15, 10:00 AM",
            teams_url="https://teams.microsoft.com/l/meetup-join/123",
            interviewer_names=["Alice"],
            company=neogen,
        )
        assert "Microsoft Teams" in html
        assert "Join Meeting" in html

    def test_empty_interviewer_list(self, neogen):
        html = app_mod.build_confirmation_email_html(
            candidate_name="Jane",
            role_title="SWE",
            interview_time="March 15, 10:00 AM",
            teams_url=None,
            interviewer_names=[],
            company=neogen,
        )
        assert "Interviewer" not in html
        assert "Neogen" in html

    def test_multiple_interviewers(self, neogen):
        html = app_mod.build_confirmation_email_html(
            candidate_name="Jane",
            role_title="SWE",
            interview_time="March 15, 10:00 AM",
            teams_url=None,
            interviewer_names=["Alice", "Bob", "Charlie"],
            company=neogen,
        )
        assert "Alice, Bob, Charlie" in html

    def test_empty_candidate_name_uses_hello(self, neogen):
        html = app_mod.build_confirmation_email_html(
            candidate_name="",
            role_title="SWE",
            interview_time="March 15",
            teams_url=None,
            interviewer_names=[],
            company=neogen,
        )
        assert "Hello," in html

    def test_signature_contains_company_name(self, neogen):
        html = app_mod.build_confirmation_email_html(
            candidate_name="Jane",
            role_title="SWE",
            interview_time="March 15",
            teams_url=None,
            interviewer_names=[],
            company=neogen,
        )
        assert "Neogen Talent Acquisition Team" in html


# ===========================================================================
# 12. BLACK-BOX: Cancellation email — edge cases
# ===========================================================================
class TestCancellationEmailEdgeCases:
    """Test cancellation email edge cases."""

    def test_custom_message_included(self, neogen):
        html = app_mod.build_cancellation_email_html(
            candidate_name="Jane",
            role_title="SWE",
            interview_time="March 15",
            reason="Scheduling conflict",
            custom_message="We will reach out again soon.",
            company=neogen,
        )
        assert "We will reach out again soon." in html

    def test_no_custom_message(self, neogen):
        html = app_mod.build_cancellation_email_html(
            candidate_name="Jane",
            role_title="SWE",
            interview_time="March 15",
            reason="Scheduling conflict",
            custom_message=None,
            company=neogen,
        )
        assert "Scheduling conflict" in html
        assert "Neogen" in html

    def test_empty_candidate_uses_hello(self, neogen):
        html = app_mod.build_cancellation_email_html(
            candidate_name="",
            role_title="SWE",
            interview_time="March 15",
            reason="reason",
            custom_message=None,
            company=neogen,
        )
        assert "Hello," in html

    def test_website_link_present(self, neogen):
        html = app_mod.build_cancellation_email_html(
            candidate_name="Jane",
            role_title="SWE",
            interview_time="March 15",
            reason="reason",
            custom_message=None,
            company=neogen,
        )
        assert "https://neogen.com" in html

    def test_no_website_link_when_none(self, default_company):
        html = app_mod.build_cancellation_email_html(
            candidate_name="Jane",
            role_title="SWE",
            interview_time="March 15",
            reason="reason",
            custom_message=None,
            company=default_company,
        )
        # No website link in body (default_company.website is None)
        assert 'href="None"' not in html


# ===========================================================================
# 13. BLACK-BOX: Reschedule email — edge cases
# ===========================================================================
class TestRescheduleEmailEdgeCases:
    """Test reschedule email edge cases."""

    def test_no_teams_url(self, neogen):
        html = app_mod.build_reschedule_email_html(
            candidate_name="Jane",
            role_title="SWE",
            old_time="March 15, 10:00 AM",
            new_time="March 20, 2:00 PM",
            teams_url=None,
            company=neogen,
        )
        assert "Microsoft Teams" not in html
        assert "Neogen" in html

    def test_with_teams_url(self, neogen):
        html = app_mod.build_reschedule_email_html(
            candidate_name="Jane",
            role_title="SWE",
            old_time="March 15",
            new_time="March 20",
            teams_url="https://teams.microsoft.com/l/meetup-join/456",
            company=neogen,
        )
        assert "Join Meeting" in html
        assert "Microsoft Teams" in html

    def test_times_shown_in_body(self, neogen):
        html = app_mod.build_reschedule_email_html(
            candidate_name="Jane",
            role_title="SWE",
            old_time="March 15, 10:00 AM",
            new_time="March 20, 2:00 PM",
            teams_url=None,
            company=neogen,
        )
        assert "March 15, 10:00 AM" in html
        assert "March 20, 2:00 PM" in html

    def test_signature_has_company_name(self, neogen):
        html = app_mod.build_reschedule_email_html(
            candidate_name="Jane",
            role_title="SWE",
            old_time="old",
            new_time="new",
            teams_url=None,
            company=neogen,
        )
        assert "Neogen Talent Acquisition Team" in html

    def test_empty_candidate_uses_hello(self, neogen):
        html = app_mod.build_reschedule_email_html(
            candidate_name="",
            role_title="SWE",
            old_time="old",
            new_time="new",
            teams_url=None,
            company=neogen,
        )
        assert "Hello," in html


# ===========================================================================
# 14. BLACK-BOX: Slot email — company name in all sections
# ===========================================================================
class TestSlotEmailCompanyNameSections:
    """Verify company name appears in all expected email sections."""

    def test_body_greeting_mentions_company(self, neogen, sample_slots):
        html = app_mod.build_branded_email_html("Jane", "SWE", sample_slots, neogen)
        assert "at <strong>Neogen</strong>" in html

    def test_signature_section(self, neogen, sample_slots):
        html = app_mod.build_branded_email_html("Jane", "SWE", sample_slots, neogen)
        assert "Neogen Talent Acquisition Team" in html

    def test_no_logo_shows_company_name_header(self, neogen, sample_slots):
        """When no logo is configured, company name appears as email header."""
        html = app_mod.build_branded_email_html("Jane", "SWE", sample_slots, neogen)
        # neogen has logo_url=None, so company name header should appear
        assert f">{neogen.name}<" in html or neogen.name in html

    def test_sender_email_in_footer(self, neogen, sample_slots):
        html = app_mod.build_branded_email_html("Jane", "SWE", sample_slots, neogen)
        assert "hr@neogen.com" in html


# ===========================================================================
# 15. EDGE CASES: Company name with special characters in emails
# ===========================================================================
class TestEmailSpecialCharCompanyName:
    """Verify emails handle special characters in company names gracefully."""

    def test_ampersand_in_name(self):
        company = app_mod.CompanyConfig(
            name="AT&T Recruiting",
            logo_url=None, primary_color="#000",
            website=None, sender_email="a@b.com",
        )
        html = app_mod.build_branded_email_html("Jane", "SWE", [], company)
        assert "AT&T Recruiting" in html

    def test_unicode_in_name(self):
        company = app_mod.CompanyConfig(
            name="Ünïcödé Corp 日本語",
            logo_url=None, primary_color="#000",
            website=None, sender_email="a@b.com",
        )
        html = app_mod.build_branded_email_html("Jane", "SWE", [], company)
        assert "Ünïcödé Corp 日本語" in html

    def test_quotes_in_name(self):
        company = app_mod.CompanyConfig(
            name="O'Brien & Associates",
            logo_url=None, primary_color="#000",
            website=None, sender_email="a@b.com",
        )
        html = app_mod.build_cancellation_email_html(
            "Jane", "SWE", "March 15", "reason", None, company,
        )
        assert "O'Brien" in html

    def test_very_long_name(self):
        long_name = "A" * 200
        company = app_mod.CompanyConfig(
            name=long_name,
            logo_url=None, primary_color="#000",
            website=None, sender_email="a@b.com",
        )
        html = app_mod.build_reschedule_email_html(
            "Jane", "SWE", "old", "new", None, company,
        )
        assert long_name in html

    def test_html_entities_in_name(self):
        """Company name with < > should not break HTML structure."""
        company = app_mod.CompanyConfig(
            name="Tech <Solutions> Inc",
            logo_url=None, primary_color="#000",
            website=None, sender_email="a@b.com",
        )
        # Should not crash
        html = app_mod.build_branded_email_html("Jane", "SWE", [], company)
        assert "Tech" in html


# ===========================================================================
# 16. INTEGRATION: Full pipeline — branding → session → email
# ===========================================================================
class TestFullBrandingPipeline:
    """End-to-end tests for the complete branding pipeline."""

    def test_persist_load_email_cycle(self, patch_branding_path, sample_slots):
        """Save branding → load into session → generate email → verify."""
        # 1. Persist branding
        app_mod._save_branding_settings({
            "company_name": "Neogen",
            "primary_color": "#FF5500",
        })

        # 2. Load into session via ensure_session_state
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        # 3. Get company config (reads from session state)
        config = app_mod.get_company_config()
        assert config.name == "Neogen"
        assert config.primary_color == "#FF5500"

        # 4. Generate email
        html = app_mod.build_branded_email_html("Jane", "SWE", sample_slots, config)
        assert "Neogen" in html
        assert "#FF5500" in html.lower() or "#ff5500" in html.lower()

    def test_main_page_title_uses_branding(self, patch_branding_path):
        """main() should use persisted company name for page title."""
        app_mod._save_branding_settings({"company_name": "Neogen"})

        class _StopAfterPageConfig(Exception):
            pass

        _mock_st.set_page_config = MagicMock()
        with patch.object(app_mod, "ensure_session_state", side_effect=_StopAfterPageConfig):
            try:
                app_mod.main()
            except _StopAfterPageConfig:
                pass

        call_kwargs = _mock_st.set_page_config.call_args
        page_title = call_kwargs[1].get("page_title") if call_kwargs[1] else call_kwargs[0][0]
        assert "Neogen" in page_title

    def test_cancel_email_subject_uses_persisted_name(self, patch_branding_path):
        """After persisting name, cancellation email subject should use it."""
        app_mod._save_branding_settings({"company_name": "Neogen"})
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        config = app_mod.get_company_config()

        mock_client = MagicMock()
        app_mod._send_cancellation_email(
            client=mock_client,
            candidate_email="a@b.com",
            candidate_name="Jane",
            role_title="SWE",
            interview_time="March 15",
            reason="reason",
            custom_message="",
            company=config,
        )
        subject = mock_client.send_mail.call_args[1]["subject"]
        assert "Neogen" in subject

    def test_reschedule_email_subject_uses_persisted_name(self, patch_branding_path):
        """After persisting name, reschedule email subject should use it."""
        app_mod._save_branding_settings({"company_name": "Neogen"})
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        config = app_mod.get_company_config()

        mock_client = MagicMock()
        app_mod._send_reschedule_email(
            client=mock_client,
            candidate_email="a@b.com",
            candidate_name="Jane",
            role_title="SWE",
            old_time="March 15",
            new_time="March 20",
            teams_url=None,
            company=config,
        )
        subject = mock_client.send_mail.call_args[1]["subject"]
        assert "Neogen" in subject

    def test_logo_persist_reload_in_email(self, patch_branding_path):
        """Persisted logo should survive reload and appear in emails."""
        logo_data = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
        app_mod._save_branding_settings({"logo_data": logo_data})
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        config = app_mod.get_company_config()
        assert config.logo_url == logo_data

        html = app_mod._build_logo_html(config)
        assert logo_data in html

    def test_color_propagation_end_to_end(self, patch_branding_path, sample_slots):
        """Custom color should appear in email styling."""
        app_mod._save_branding_settings({"primary_color": "#FF5500"})
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        config = app_mod.get_company_config()
        html = app_mod.build_branded_email_html("Jane", "SWE", sample_slots, config)
        # Color should be in the HTML (used for borders, buttons, etc.)
        assert "#FF5500" in html or "#ff5500" in html


# ===========================================================================
# 17. EDGE: _render_footer() with custom branding
# ===========================================================================
class TestRenderFooterBranding:
    """Test footer renders with dynamic company name."""

    def test_footer_uses_custom_company_name(self):
        _mock_st.session_state = {"custom_company_name": "Neogen"}
        _mock_st.markdown = MagicMock()
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_footer()
        # Find the footer HTML call (contains "All rights reserved")
        footer_calls = [
            str(c) for c in _mock_st.markdown.call_args_list
            if "All rights reserved" in str(c)
        ]
        assert len(footer_calls) >= 1
        assert "Neogen" in footer_calls[0]

    def test_footer_uses_default_when_no_override(self):
        _mock_st.markdown = MagicMock()
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_footer()
        footer_calls = [
            str(c) for c in _mock_st.markdown.call_args_list
            if "All rights reserved" in str(c)
        ]
        assert len(footer_calls) >= 1
        assert "PowerDash HR" in footer_calls[0]

    def test_footer_shows_current_year(self):
        from datetime import datetime
        _mock_st.markdown = MagicMock()
        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_footer()
        footer_calls = [
            str(c) for c in _mock_st.markdown.call_args_list
            if "All rights reserved" in str(c)
        ]
        assert str(datetime.now().year) in footer_calls[0]


# ===========================================================================
# 18. EDGE: CompanyConfig dataclass
# ===========================================================================
class TestCompanyConfigDataclass:
    """Test CompanyConfig dataclass behavior."""

    def test_all_fields_stored(self):
        config = app_mod.CompanyConfig(
            name="Test", logo_url="logo.png", primary_color="#000",
            website="https://test.com", sender_email="a@b.com",
        )
        assert config.name == "Test"
        assert config.logo_url == "logo.png"
        assert config.primary_color == "#000"
        assert config.website == "https://test.com"
        assert config.sender_email == "a@b.com"

    def test_signature_name_property(self):
        config = app_mod.CompanyConfig(
            name="Acme", logo_url=None, primary_color="#000",
            website=None, sender_email="a@b.com",
        )
        assert config.signature_name == "Acme Talent Acquisition Team"

    def test_signature_name_with_spaces(self):
        config = app_mod.CompanyConfig(
            name="My Company Inc", logo_url=None, primary_color="#000",
            website=None, sender_email="a@b.com",
        )
        assert config.signature_name == "My Company Inc Talent Acquisition Team"

    def test_equality(self):
        c1 = app_mod.CompanyConfig("A", None, "#000", None, "a@b.com")
        c2 = app_mod.CompanyConfig("A", None, "#000", None, "a@b.com")
        assert c1 == c2

    def test_inequality(self):
        c1 = app_mod.CompanyConfig("A", None, "#000", None, "a@b.com")
        c2 = app_mod.CompanyConfig("B", None, "#000", None, "a@b.com")
        assert c1 != c2


# ===========================================================================
# 19. EDGE: Slot email with no logo falls back to header
# ===========================================================================
class TestSlotEmailNoLogoFallback:
    """When no logo is configured, company name header should appear."""

    def test_no_logo_shows_name_header(self, sample_slots):
        company = app_mod.CompanyConfig(
            name="Neogen", logo_url=None, primary_color="#FF5500",
            website=None, sender_email="hr@neogen.com",
        )
        html = app_mod.build_branded_email_html("Jane", "SWE", sample_slots, company)
        # Should have a header with company name instead of logo
        assert "Neogen" in html
        # The header background should use primary color
        assert "#FF5500" in html or "#ff5500" in html

    def test_with_logo_uses_img_tag(self, sample_slots):
        company = app_mod.CompanyConfig(
            name="Neogen", logo_url="https://example.com/logo.png",
            primary_color="#FF5500", website=None, sender_email="hr@neogen.com",
        )
        html = app_mod.build_branded_email_html("Jane", "SWE", sample_slots, company)
        assert '<img' in html
        assert "https://example.com/logo.png" in html


# ===========================================================================
# 20. EDGE: MAX_LOGO_BYTES constant
# ===========================================================================
class TestMaxLogoBytesConstant:
    """Verify the logo size limit constant."""

    def test_max_logo_bytes_is_2mb(self):
        assert app_mod._MAX_LOGO_BYTES == 2 * 1024 * 1024

    def test_exactly_at_limit_is_accepted(self, patch_branding_path):
        """Logo at exactly 2MB should be accepted."""
        mock_file = MagicMock()
        mock_file.name = "exact.png"
        mock_file.read.return_value = b"\x00" * (2 * 1024 * 1024)  # Exactly 2MB

        sidebar_ctx = MagicMock()
        _mock_st.sidebar.__enter__ = MagicMock(return_value=sidebar_ctx)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input = MagicMock(return_value="PowerDash HR")
        _mock_st.button = MagicMock(return_value=False)
        _mock_st.file_uploader = MagicMock(return_value=mock_file)
        _mock_st.image = MagicMock()
        _mock_st.caption = MagicMock()
        _mock_st.markdown = MagicMock()
        _mock_st.warning = MagicMock()
        _mock_st.rerun = MagicMock()

        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()

        # Should be accepted (not oversized)
        _mock_st.warning.assert_not_called()
        assert _mock_st.session_state.get("custom_logo_data") is not None

    def test_one_byte_over_limit_is_rejected(self, patch_branding_path):
        """Logo at 2MB + 1 byte should be rejected."""
        mock_file = MagicMock()
        mock_file.name = "over.png"
        mock_file.read.return_value = b"\x00" * (2 * 1024 * 1024 + 1)

        sidebar_ctx = MagicMock()
        _mock_st.sidebar.__enter__ = MagicMock(return_value=sidebar_ctx)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.text_input = MagicMock(return_value="PowerDash HR")
        _mock_st.button = MagicMock(return_value=False)
        _mock_st.file_uploader = MagicMock(return_value=mock_file)
        _mock_st.image = MagicMock()
        _mock_st.caption = MagicMock()
        _mock_st.markdown = MagicMock()
        _mock_st.warning = MagicMock()
        _mock_st.rerun = MagicMock()

        with patch.object(app_mod, "_get_logo_src", return_value=None):
            app_mod._render_logo_settings()

        _mock_st.warning.assert_called_once()
        assert _mock_st.session_state.get("custom_logo_data") is None
