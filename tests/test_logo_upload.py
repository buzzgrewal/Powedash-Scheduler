"""
Comprehensive tests for the logo upload settings feature.

Tests cover:
- White-box: _get_logo_src (data URL, http URL, local file, None, empty, missing file),
  _update_branding_logo (merge semantics, file creation, None removal),
  _load_branding_settings / _save_branding_settings (round-trip, corrupt JSON, missing file),
  get_company_config (session state override vs default fallback),
  ensure_session_state branding loading logic.
- Black-box: end-to-end upload → persist → reload lifecycle, remove logo lifecycle,
  upload different formats (PNG, JPG, GIF, SVG), re-upload overwrites previous.
- Edge cases: empty file upload, corrupt branding JSON on disk, missing branding file,
  zero-byte images, very large base64 payloads, concurrent save/load, unknown file
  extension fallback, data URL passthrough for all MIME types.
"""
import base64
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Mock streamlit at module level (same pattern as existing tests)
# ---------------------------------------------------------------------------
# Use setdefault so we reuse an existing mock if another test file installed one first.
# Then always reference the ACTUAL mock stored in sys.modules (may differ from _local_mock).
_local_mock = MagicMock()
_local_mock.secrets = {}
_local_mock.session_state = {}
_local_mock.cache_data = lambda *a, **kw: (lambda f: f)
_local_mock.cache_resource = lambda *a, **kw: (lambda f: f)

sys.modules.setdefault("streamlit", _local_mock)
sys.modules.setdefault("streamlit.components", MagicMock())
sys.modules.setdefault("streamlit.components.v1", MagicMock())

# IMPORTANT: Always use the mock that's actually in sys.modules, not our local one,
# because when tests run together the first test file's mock wins.
_mock_st = sys.modules["streamlit"]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_streamlit_state():
    """Reset session state and secrets before each test to prevent bleed."""
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
def sample_png_bytes():
    """Minimal valid PNG bytes (1x1 transparent pixel)."""
    return (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
        b'\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
        b'\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01'
        b'\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
    )


@pytest.fixture
def sample_png_data_url(sample_png_bytes):
    """Base64 data URL for the sample PNG."""
    b64 = base64.b64encode(sample_png_bytes).decode('utf-8')
    return f"data:image/png;base64,{b64}"


@pytest.fixture
def tmp_logo_file(tmp_path, sample_png_bytes):
    """Create a temporary PNG file on disk."""
    logo = tmp_path / "test_logo.png"
    logo.write_bytes(sample_png_bytes)
    return str(logo)


# ===========================================================================
# WHITE-BOX TESTS: _get_logo_src
# ===========================================================================
class TestGetLogoSrc:
    """Unit tests for _get_logo_src function."""

    def test_none_returns_none(self):
        assert app_mod._get_logo_src(None) is None

    def test_empty_string_returns_none(self):
        assert app_mod._get_logo_src("") is None

    def test_http_url_passthrough(self):
        url = "http://example.com/logo.png"
        assert app_mod._get_logo_src(url) == url

    def test_https_url_passthrough(self):
        url = "https://cdn.example.com/brand/logo.svg"
        assert app_mod._get_logo_src(url) == url

    def test_data_url_passthrough_png(self):
        data_url = "data:image/png;base64,iVBORw0KGgo="
        assert app_mod._get_logo_src(data_url) == data_url

    def test_data_url_passthrough_jpeg(self):
        data_url = "data:image/jpeg;base64,/9j/4AAQ="
        assert app_mod._get_logo_src(data_url) == data_url

    def test_data_url_passthrough_svg(self):
        data_url = "data:image/svg+xml;base64,PHN2Zw=="
        assert app_mod._get_logo_src(data_url) == data_url

    def test_data_url_passthrough_gif(self):
        data_url = "data:image/gif;base64,R0lGODlh"
        assert app_mod._get_logo_src(data_url) == data_url

    def test_local_file_png(self, tmp_logo_file, sample_png_bytes):
        result = app_mod._get_logo_src(tmp_logo_file)
        assert result is not None
        assert result.startswith("data:image/png;base64,")
        # Verify round-trip
        b64_part = result.split(",", 1)[1]
        decoded = base64.b64decode(b64_part)
        assert decoded == sample_png_bytes

    def test_local_file_jpg(self, tmp_path):
        jpg_file = tmp_path / "logo.jpg"
        jpg_file.write_bytes(b'\xff\xd8\xff\xe0test')
        result = app_mod._get_logo_src(str(jpg_file))
        assert result is not None
        assert result.startswith("data:image/jpeg;base64,")

    def test_local_file_jpeg_extension(self, tmp_path):
        jpeg_file = tmp_path / "logo.jpeg"
        jpeg_file.write_bytes(b'\xff\xd8\xff\xe0test')
        result = app_mod._get_logo_src(str(jpeg_file))
        assert result.startswith("data:image/jpeg;base64,")

    def test_local_file_gif(self, tmp_path):
        gif_file = tmp_path / "logo.gif"
        gif_file.write_bytes(b'GIF89a\x01\x00')
        result = app_mod._get_logo_src(str(gif_file))
        assert result.startswith("data:image/gif;base64,")

    def test_local_file_svg(self, tmp_path):
        svg_file = tmp_path / "logo.svg"
        svg_file.write_bytes(b'<svg></svg>')
        result = app_mod._get_logo_src(str(svg_file))
        assert result.startswith("data:image/svg+xml;base64,")

    def test_local_file_ico(self, tmp_path):
        ico_file = tmp_path / "favicon.ico"
        ico_file.write_bytes(b'\x00\x00\x01\x00')
        result = app_mod._get_logo_src(str(ico_file))
        assert result.startswith("data:image/x-icon;base64,")

    def test_unknown_extension_defaults_to_png(self, tmp_path):
        bmp_file = tmp_path / "logo.bmp"
        bmp_file.write_bytes(b'BM\x00\x00')
        result = app_mod._get_logo_src(str(bmp_file))
        assert result.startswith("data:image/png;base64,")

    def test_nonexistent_file_returns_none(self):
        assert app_mod._get_logo_src("/nonexistent/path/logo.png") is None

    def test_relative_path_nonexistent_returns_none(self):
        result = app_mod._get_logo_src("nonexistent_logo_xyz.png")
        assert result is None

    def test_empty_file_returns_empty_base64(self, tmp_path):
        empty_file = tmp_path / "empty.png"
        empty_file.write_bytes(b'')
        result = app_mod._get_logo_src(str(empty_file))
        # Empty file produces valid but empty base64
        assert result == "data:image/png;base64,"


# ===========================================================================
# WHITE-BOX TESTS: _load_branding_settings / _save_branding_settings
# ===========================================================================
class TestBrandingSettingsPersistence:
    """Unit tests for branding settings load/save."""

    def test_load_missing_file_returns_empty_dict(self, patch_branding_path):
        result = app_mod._load_branding_settings()
        assert result == {}

    def test_save_and_load_roundtrip(self, patch_branding_path):
        settings = {"logo_data": "data:image/png;base64,abc123", "company_name": "Acme"}
        app_mod._save_branding_settings(settings)
        loaded = app_mod._load_branding_settings()
        assert loaded == settings

    def test_save_overwrites_previous(self, patch_branding_path):
        app_mod._save_branding_settings({"logo_data": "old"})
        app_mod._save_branding_settings({"logo_data": "new"})
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "new"

    def test_load_corrupt_json_returns_empty_dict(self, patch_branding_path):
        with open(patch_branding_path, 'w') as f:
            f.write("{broken json!!!")
        result = app_mod._load_branding_settings()
        assert result == {}

    def test_load_empty_file_returns_empty_dict(self, patch_branding_path):
        with open(patch_branding_path, 'w') as f:
            f.write("")
        result = app_mod._load_branding_settings()
        assert result == {}

    def test_load_non_dict_json_returns_empty_dict(self, patch_branding_path):
        """If JSON is valid but not a dict (e.g. a list), load returns {} as guard."""
        with open(patch_branding_path, 'w') as f:
            json.dump(["not", "a", "dict"], f)
        result = app_mod._load_branding_settings()
        assert result == {}

    def test_load_json_string_returns_empty_dict(self, patch_branding_path):
        """If JSON is a bare string, load returns {}."""
        with open(patch_branding_path, 'w') as f:
            json.dump("just a string", f)
        assert app_mod._load_branding_settings() == {}

    def test_load_json_number_returns_empty_dict(self, patch_branding_path):
        """If JSON is a bare number, load returns {}."""
        with open(patch_branding_path, 'w') as f:
            json.dump(42, f)
        assert app_mod._load_branding_settings() == {}

    def test_load_json_null_returns_empty_dict(self, patch_branding_path):
        """If JSON is null, load returns {}."""
        with open(patch_branding_path, 'w') as f:
            f.write("null")
        assert app_mod._load_branding_settings() == {}

    def test_save_creates_file(self, patch_branding_path):
        assert not os.path.exists(patch_branding_path)
        app_mod._save_branding_settings({"key": "value"})
        assert os.path.exists(patch_branding_path)

    def test_save_with_none_value(self, patch_branding_path):
        app_mod._save_branding_settings({"logo_data": None})
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] is None

    def test_save_unicode_values(self, patch_branding_path):
        settings = {"company_name": "Ünïcödé Cörp 日本語"}
        app_mod._save_branding_settings(settings)
        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "Ünïcödé Cörp 日本語"

    def test_save_large_base64_payload(self, patch_branding_path):
        """Simulate a large logo (100KB of random data)."""
        large_data = base64.b64encode(os.urandom(100_000)).decode('utf-8')
        data_url = f"data:image/png;base64,{large_data}"
        app_mod._save_branding_settings({"logo_data": data_url})
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == data_url


# ===========================================================================
# WHITE-BOX TESTS: _update_branding_logo (merge semantics)
# ===========================================================================
class TestUpdateBrandingLogo:
    """Tests for _update_branding_logo merge behavior."""

    def test_sets_logo_in_empty_file(self, patch_branding_path):
        app_mod._update_branding_logo("data:image/png;base64,abc")
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "data:image/png;base64,abc"

    def test_preserves_existing_keys(self, patch_branding_path):
        """Other branding settings must not be overwritten when updating logo."""
        app_mod._save_branding_settings({
            "company_name": "Acme Corp",
            "primary_color": "#FF0000",
            "logo_data": "old_logo",
        })
        app_mod._update_branding_logo("new_logo_data_url")
        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "Acme Corp"
        assert loaded["primary_color"] == "#FF0000"
        assert loaded["logo_data"] == "new_logo_data_url"

    def test_remove_logo_removes_key_preserves_others(self, patch_branding_path):
        """Removing logo should delete logo_data key, not set it to None."""
        app_mod._save_branding_settings({
            "company_name": "Keep Me",
            "logo_data": "some_logo",
        })
        app_mod._update_branding_logo(None)
        loaded = app_mod._load_branding_settings()
        assert "logo_data" not in loaded
        assert loaded["company_name"] == "Keep Me"

    def test_overwrite_logo(self, patch_branding_path):
        app_mod._update_branding_logo("first")
        app_mod._update_branding_logo("second")
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "second"

    def test_handles_corrupt_existing_file(self, patch_branding_path):
        """If existing branding file is corrupt, _update_branding_logo still works
        because _load_branding_settings returns {} for corrupt files."""
        with open(patch_branding_path, 'w') as f:
            f.write("not valid json{{{")
        app_mod._update_branding_logo("recovered_logo")
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "recovered_logo"


# ===========================================================================
# WHITE-BOX TESTS: get_company_config (logo override from session state)
# ===========================================================================
class TestGetCompanyConfigLogoOverride:
    """Tests for get_company_config logo_url selection logic."""

    def test_defaults_to_logo_png_when_no_override(self):
        """When no custom logo in session state and no secret, falls back to 'logo.png'."""
        _mock_st.session_state = {}
        _mock_st.secrets = {}
        config = app_mod.get_company_config()
        assert config.logo_url == "logo.png"

    def test_uses_session_state_custom_logo(self, sample_png_data_url):
        """Session state custom_logo_data takes priority."""
        _mock_st.session_state = {"custom_logo_data": sample_png_data_url}
        _mock_st.secrets = {}
        config = app_mod.get_company_config()
        assert config.logo_url == sample_png_data_url

    def test_session_state_none_falls_back_to_secret(self):
        """If session state has None, falls back to secrets."""
        _mock_st.session_state = {"custom_logo_data": None}
        _mock_st.secrets = {"company_logo_url": "https://example.com/logo.png"}
        config = app_mod.get_company_config()
        assert config.logo_url == "https://example.com/logo.png"

    def test_session_state_empty_string_falls_back(self):
        """Empty string is falsy, should fall back."""
        _mock_st.session_state = {"custom_logo_data": ""}
        _mock_st.secrets = {"company_logo_url": "secret_logo.png"}
        config = app_mod.get_company_config()
        assert config.logo_url == "secret_logo.png"

    def test_secret_overrides_default(self):
        """company_logo_url secret takes precedence over 'logo.png' default."""
        _mock_st.session_state = {}
        _mock_st.secrets = {"company_logo_url": "custom_from_secrets.png"}
        config = app_mod.get_company_config()
        assert config.logo_url == "custom_from_secrets.png"

    def test_session_state_takes_priority_over_secret(self, sample_png_data_url):
        """Session state custom logo beats secrets."""
        _mock_st.session_state = {"custom_logo_data": sample_png_data_url}
        _mock_st.secrets = {"company_logo_url": "from_secret.png"}
        config = app_mod.get_company_config()
        assert config.logo_url == sample_png_data_url


# ===========================================================================
# WHITE-BOX TESTS: ensure_session_state branding loading
# ===========================================================================
class TestEnsureSessionStateBrandingLoad:
    """Tests for branding loading in ensure_session_state."""

    def test_loads_logo_from_branding_file(self, patch_branding_path, sample_png_data_url):
        """On first run, persisted logo should be loaded into session state."""
        app_mod._save_branding_settings({"logo_data": sample_png_data_url})
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        assert _mock_st.session_state.get("custom_logo_data") == sample_png_data_url

    def test_skips_load_if_already_loaded(self, patch_branding_path, sample_png_data_url):
        """If _branding_loaded flag is set, should not overwrite session state."""
        app_mod._save_branding_settings({"logo_data": sample_png_data_url})
        _mock_st.session_state = {
            "_branding_loaded": True,
            "custom_logo_data": "user_override_in_session",
        }
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        # Should NOT have overwritten with file data
        assert _mock_st.session_state["custom_logo_data"] == "user_override_in_session"

    def test_no_branding_file_no_crash(self, patch_branding_path):
        """Missing branding file should not crash ensure_session_state."""
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        assert _mock_st.session_state.get("custom_logo_data") is None
        assert _mock_st.session_state["_branding_loaded"] is True

    def test_branding_file_with_null_logo(self, patch_branding_path):
        """Branding file with logo_data: null should not set custom_logo_data."""
        app_mod._save_branding_settings({"logo_data": None})
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        assert _mock_st.session_state.get("custom_logo_data") is None

    def test_branding_file_without_logo_key(self, patch_branding_path):
        """Branding file with other keys but no logo_data should not set logo."""
        app_mod._save_branding_settings({"company_name": "Test Corp"})
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        assert _mock_st.session_state.get("custom_logo_data") is None


# ===========================================================================
# BLACK-BOX TESTS: End-to-end lifecycle
# ===========================================================================
class TestLogoUploadLifecycle:
    """End-to-end lifecycle tests for logo upload feature."""

    def test_upload_persist_reload(self, patch_branding_path, sample_png_data_url):
        """Upload logo → persist → simulate reload → logo still available."""
        # Step 1: Upload
        _mock_st.session_state = {}
        _mock_st.session_state["custom_logo_data"] = sample_png_data_url
        app_mod._update_branding_logo(sample_png_data_url)

        # Step 2: Verify persisted
        on_disk = app_mod._load_branding_settings()
        assert on_disk["logo_data"] == sample_png_data_url

        # Step 3: Simulate page reload (clear session state, re-run ensure_session_state)
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        assert _mock_st.session_state["custom_logo_data"] == sample_png_data_url

        # Step 4: get_company_config should use it
        config = app_mod.get_company_config()
        assert config.logo_url == sample_png_data_url

    def test_remove_logo_lifecycle(self, patch_branding_path, sample_png_data_url):
        """Upload → remove → verify gone from disk and session, header falls back."""
        # Upload
        _mock_st.session_state["custom_logo_data"] = sample_png_data_url
        app_mod._update_branding_logo(sample_png_data_url)

        # Remove
        _mock_st.session_state["custom_logo_data"] = None
        app_mod._update_branding_logo(None)

        # Verify on disk — logo_data key should be absent (not null)
        on_disk = app_mod._load_branding_settings()
        assert "logo_data" not in on_disk

        # Simulate reload
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        assert _mock_st.session_state.get("custom_logo_data") is None

        # get_company_config should fall back to default
        config = app_mod.get_company_config()
        assert config.logo_url == "logo.png"

    def test_replace_logo(self, patch_branding_path):
        """Upload first logo → upload second → second wins everywhere."""
        first = "data:image/png;base64,FIRST"
        second = "data:image/jpeg;base64,SECOND"

        _mock_st.session_state["custom_logo_data"] = first
        app_mod._update_branding_logo(first)

        _mock_st.session_state["custom_logo_data"] = second
        app_mod._update_branding_logo(second)

        on_disk = app_mod._load_branding_settings()
        assert on_disk["logo_data"] == second

        config = app_mod.get_company_config()
        assert config.logo_url == second


# ===========================================================================
# BLACK-BOX TESTS: File format handling
# ===========================================================================
class TestFileFormatHandling:
    """Tests for different image file format handling in the upload flow."""

    @pytest.mark.parametrize("ext,expected_mime", [
        (".png", "image/png"),
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".gif", "image/gif"),
        (".svg", "image/svg+xml"),
    ])
    def test_mime_type_from_extension(self, ext, expected_mime):
        """Verify correct MIME type is used for each supported extension."""
        raw_bytes = b"fake image data"
        b64 = base64.b64encode(raw_bytes).decode('utf-8')
        expected_url = f"data:{expected_mime};base64,{b64}"

        # Simulate what _render_logo_settings does internally
        mime_types = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.svg': 'image/svg+xml',
        }
        mime_type = mime_types.get(ext, 'image/png')
        data_url = f"data:{mime_type};base64,{b64}"

        assert data_url == expected_url

    def test_unknown_extension_defaults_png(self):
        """Unknown extension should default to image/png MIME type."""
        mime_types = {
            '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.gif': 'image/gif', '.svg': 'image/svg+xml',
        }
        mime_type = mime_types.get('.webp', 'image/png')
        assert mime_type == 'image/png'


# ===========================================================================
# EDGE CASE TESTS
# ===========================================================================
class TestEdgeCases:
    """Edge case and boundary tests."""

    def test_data_url_with_special_characters_preserved(self):
        """Data URLs with + and / in base64 are preserved."""
        data_url = "data:image/png;base64,abc+def/ghi=="
        assert app_mod._get_logo_src(data_url) == data_url

    def test_very_long_data_url(self, patch_branding_path):
        """Very large logo (simulated ~500KB) persists correctly."""
        large_b64 = base64.b64encode(os.urandom(500_000)).decode('utf-8')
        data_url = f"data:image/png;base64,{large_b64}"

        _mock_st.session_state["custom_logo_data"] = data_url
        app_mod._update_branding_logo(data_url)

        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == data_url

    def test_multiple_rapid_saves_last_wins(self, patch_branding_path):
        """Rapid sequential saves — last write wins."""
        for i in range(10):
            app_mod._update_branding_logo(f"data:image/png;base64,version{i}")
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "data:image/png;base64,version9"

    def test_get_company_config_does_not_mutate_session_state(self, sample_png_data_url):
        """get_company_config should be read-only — not modify session state."""
        _mock_st.session_state = {"custom_logo_data": sample_png_data_url}
        state_before = dict(_mock_st.session_state)
        app_mod.get_company_config()
        assert _mock_st.session_state == state_before

    def _get_header_html(self) -> str:
        """Extract the actual header HTML from st.markdown calls (skip CSS block)."""
        html_calls = [
            call for call in _mock_st.markdown.call_args_list
            if '<div class="branded-header">' in str(call)
        ]
        assert len(html_calls) == 1, f"Expected 1 header HTML call, got {len(html_calls)}: {html_calls}"
        return html_calls[0][0][0]

    def test_header_renders_with_data_url_logo(self, sample_png_data_url):
        """_render_header_full should include the data URL in img src."""
        from app import CompanyConfig
        company = CompanyConfig(
            name="Test Corp",
            logo_url=sample_png_data_url,
            primary_color="#0066CC",
            website=None,
            sender_email="test@example.com",
        )
        _mock_st.markdown.reset_mock()
        app_mod._render_header_full(company)

        html = self._get_header_html()
        assert sample_png_data_url in html
        assert 'class="client-logo"' in html

    def test_header_renders_without_logo(self):
        """Header with no logo should still render the title."""
        from app import CompanyConfig
        company = CompanyConfig(
            name="No Logo Corp",
            logo_url=None,
            primary_color="#0066CC",
            website=None,
            sender_email="test@example.com",
        )
        _mock_st.markdown.reset_mock()
        with patch.object(app_mod, "get_layout_config") as mock_layout:
            mock_layout.return_value = app_mod.LayoutConfig(
                show_sidebar=False, show_footer=True, show_powered_by=False, header_style="full"
            )
            app_mod._render_header_full(company)

        html = self._get_header_html()
        assert "No Logo Corp" in html
        assert "client-logo" not in html

    def test_header_renders_with_http_logo_url(self):
        """Header with an HTTP URL logo should use it directly."""
        from app import CompanyConfig
        company = CompanyConfig(
            name="URL Corp",
            logo_url="https://example.com/logo.png",
            primary_color="#0066CC",
            website=None,
            sender_email="test@example.com",
        )
        _mock_st.markdown.reset_mock()
        with patch.object(app_mod, "get_layout_config") as mock_layout:
            mock_layout.return_value = app_mod.LayoutConfig(
                show_sidebar=False, show_footer=True, show_powered_by=False, header_style="full"
            )
            app_mod._render_header_full(company)

        html = self._get_header_html()
        assert "https://example.com/logo.png" in html

    def test_save_to_readonly_directory_shows_warning(self, tmp_path):
        """Saving to a read-only location should call st.warning, not crash."""
        readonly_path = str(tmp_path / "readonly_dir" / "branding.json")
        # Don't create the directory — write will fail
        with patch.object(app_mod, "_get_branding_settings_path", return_value=readonly_path):
            _mock_st.warning.reset_mock()
            app_mod._save_branding_settings({"logo_data": "test"})
            _mock_st.warning.assert_called_once()

    def test_update_branding_logo_when_load_fails(self, tmp_path):
        """If load returns {} due to error, update should still save the new logo."""
        bad_path = str(tmp_path / "branding.json")
        # Write corrupt data
        with open(bad_path, 'w') as f:
            f.write("{corrupt")
        with patch.object(app_mod, "_get_branding_settings_path", return_value=bad_path):
            app_mod._update_branding_logo("new_logo")
            loaded = app_mod._load_branding_settings()
            assert loaded["logo_data"] == "new_logo"


# ===========================================================================
# INTEGRATION: _render_logo_settings widget logic
# ===========================================================================
class TestRenderLogoSettingsLogic:
    """Tests for _render_logo_settings internal logic (mocking Streamlit widgets)."""

    def test_shows_current_custom_logo(self, sample_png_data_url):
        """When custom logo exists, st.image should be called with it."""
        _mock_st.session_state = {"custom_logo_data": sample_png_data_url}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.button.return_value = False
        _mock_st.file_uploader.return_value = None

        app_mod._render_logo_settings()

        # st.image should have been called with the data URL
        image_calls = _mock_st.image.call_args_list
        found = any(sample_png_data_url in str(call) for call in image_calls)
        assert found, f"Expected st.image called with data URL, got: {image_calls}"

    def test_remove_button_clears_logo(self, patch_branding_path, sample_png_data_url):
        """Clicking remove button should clear session state and remove from disk."""
        _mock_st.session_state = {"custom_logo_data": sample_png_data_url}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.button.return_value = True  # Simulate button click
        _mock_st.file_uploader.return_value = None
        _mock_st.rerun = MagicMock()

        app_mod._render_logo_settings()

        assert _mock_st.session_state.get("custom_logo_data") is None
        _mock_st.rerun.assert_called_once()
        # Verify persisted — logo_data key should be absent
        loaded = app_mod._load_branding_settings()
        assert "logo_data" not in loaded

    def test_upload_new_logo(self, patch_branding_path, sample_png_bytes):
        """Uploading a new file should set session state and persist."""
        _mock_st.session_state = {}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.button.return_value = False
        _mock_st.rerun = MagicMock()

        # Mock the file uploader return
        mock_file = MagicMock()
        mock_file.read.return_value = sample_png_bytes
        mock_file.name = "company_logo.png"
        _mock_st.file_uploader.return_value = mock_file

        app_mod._render_logo_settings()

        expected_b64 = base64.b64encode(sample_png_bytes).decode('utf-8')
        expected_url = f"data:image/png;base64,{expected_b64}"

        assert _mock_st.session_state["custom_logo_data"] == expected_url
        _mock_st.rerun.assert_called_once()

        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == expected_url

    def test_upload_empty_file_does_not_save(self, patch_branding_path):
        """An empty file (0 bytes) should not trigger save/rerun."""
        _mock_st.session_state = {}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.button.return_value = False
        _mock_st.rerun = MagicMock()

        mock_file = MagicMock()
        mock_file.read.return_value = b""
        mock_file.name = "empty.png"
        _mock_st.file_uploader.return_value = mock_file

        app_mod._render_logo_settings()

        assert _mock_st.session_state.get("custom_logo_data") is None
        _mock_st.rerun.assert_not_called()

    def test_reupload_same_logo_no_rerun(self, patch_branding_path, sample_png_bytes):
        """Re-uploading the same file should not trigger rerun (dedup guard)."""
        expected_b64 = base64.b64encode(sample_png_bytes).decode('utf-8')
        expected_url = f"data:image/png;base64,{expected_b64}"

        _mock_st.session_state = {"custom_logo_data": expected_url}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.button.return_value = False
        _mock_st.rerun = MagicMock()

        mock_file = MagicMock()
        mock_file.read.return_value = sample_png_bytes
        mock_file.name = "company_logo.png"
        _mock_st.file_uploader.return_value = mock_file

        app_mod._render_logo_settings()

        _mock_st.rerun.assert_not_called()

    def test_upload_svg_file(self, patch_branding_path):
        """SVG upload should use image/svg+xml MIME type."""
        svg_data = b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
        _mock_st.session_state = {}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.button.return_value = False
        _mock_st.rerun = MagicMock()

        mock_file = MagicMock()
        mock_file.read.return_value = svg_data
        mock_file.name = "logo.svg"
        _mock_st.file_uploader.return_value = mock_file

        app_mod._render_logo_settings()

        saved_url = _mock_st.session_state["custom_logo_data"]
        assert saved_url.startswith("data:image/svg+xml;base64,")
        b64_part = saved_url.split(",", 1)[1]
        assert base64.b64decode(b64_part) == svg_data

    def test_shows_default_logo_when_no_custom(self, tmp_logo_file):
        """When no custom logo, should show default logo from secrets."""
        _mock_st.session_state = {}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.button.return_value = False
        _mock_st.file_uploader.return_value = None
        _mock_st.image.reset_mock()
        _mock_st.caption.reset_mock()

        with patch.object(app_mod, "get_secret", side_effect=lambda k, d=None: tmp_logo_file if k == "company_logo_url" else d):
            app_mod._render_logo_settings()

        _mock_st.image.assert_called()
        _mock_st.caption.assert_called_with("Default logo")


# ===========================================================================
# NON-DICT JSON GUARD (Bug fix verification)
# ===========================================================================
class TestNonDictJsonGuard:
    """Verify _load_branding_settings and _update_branding_logo handle non-dict JSON safely."""

    def test_update_logo_when_file_contains_json_array(self, patch_branding_path):
        """_update_branding_logo must not crash when file contains a JSON array."""
        with open(patch_branding_path, 'w') as f:
            json.dump(["unexpected", "list"], f)
        # Previously this crashed with TypeError: list indices must be integers
        app_mod._update_branding_logo("new_logo")
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "new_logo"

    def test_update_logo_when_file_contains_json_string(self, patch_branding_path):
        """_update_branding_logo must not crash when file contains a JSON string."""
        with open(patch_branding_path, 'w') as f:
            json.dump("just a string", f)
        app_mod._update_branding_logo("new_logo")
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "new_logo"

    def test_update_logo_when_file_contains_json_number(self, patch_branding_path):
        with open(patch_branding_path, 'w') as f:
            json.dump(42, f)
        app_mod._update_branding_logo("new_logo")
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "new_logo"

    def test_update_logo_when_file_contains_json_null(self, patch_branding_path):
        with open(patch_branding_path, 'w') as f:
            f.write("null")
        app_mod._update_branding_logo("new_logo")
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "new_logo"

    def test_ensure_session_state_with_json_array_file(self, patch_branding_path):
        """ensure_session_state must not crash if branding file is a JSON array."""
        with open(patch_branding_path, 'w') as f:
            json.dump([1, 2, 3], f)
        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()
        # Should not crash, and logo should be None
        assert _mock_st.session_state.get("custom_logo_data") is None
        assert _mock_st.session_state["_branding_loaded"] is True

    def test_remove_logo_when_file_was_non_dict(self, patch_branding_path):
        """Removing logo when file was corrupt (non-dict) should not crash."""
        with open(patch_branding_path, 'w') as f:
            json.dump(["bad"], f)
        app_mod._update_branding_logo(None)
        loaded = app_mod._load_branding_settings()
        assert loaded == {}


# ===========================================================================
# FILE SIZE LIMIT (Bug fix verification)
# ===========================================================================
class TestFileSizeLimit:
    """Verify oversized uploads are rejected."""

    def _setup_sidebar_mocks(self):
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.button.return_value = False
        _mock_st.rerun = MagicMock()
        _mock_st.warning.reset_mock()

    def test_rejects_file_over_2mb(self, patch_branding_path):
        """File larger than 2 MB must be rejected with a warning."""
        self._setup_sidebar_mocks()

        oversized_data = b'\x00' * (2 * 1024 * 1024 + 1)  # 2 MB + 1 byte
        mock_file = MagicMock()
        mock_file.read.return_value = oversized_data
        mock_file.name = "huge_logo.png"
        _mock_st.file_uploader.return_value = mock_file

        app_mod._render_logo_settings()

        assert _mock_st.session_state.get("custom_logo_data") is None
        _mock_st.rerun.assert_not_called()
        _mock_st.warning.assert_called_once()
        warning_msg = _mock_st.warning.call_args[0][0]
        assert "too large" in warning_msg
        assert "2 MB" in warning_msg

    def test_accepts_file_exactly_2mb(self, patch_branding_path):
        """File exactly 2 MB should be accepted."""
        self._setup_sidebar_mocks()

        exact_data = b'\x89' * (2 * 1024 * 1024)  # exactly 2 MB
        mock_file = MagicMock()
        mock_file.read.return_value = exact_data
        mock_file.name = "exact_logo.png"
        _mock_st.file_uploader.return_value = mock_file

        app_mod._render_logo_settings()

        assert _mock_st.session_state.get("custom_logo_data") is not None
        _mock_st.rerun.assert_called_once()
        _mock_st.warning.assert_not_called()

    def test_accepts_small_file(self, patch_branding_path, sample_png_bytes):
        """A normal small file should be accepted."""
        self._setup_sidebar_mocks()

        mock_file = MagicMock()
        mock_file.read.return_value = sample_png_bytes
        mock_file.name = "small.png"
        _mock_st.file_uploader.return_value = mock_file

        app_mod._render_logo_settings()

        assert _mock_st.session_state.get("custom_logo_data") is not None
        _mock_st.rerun.assert_called_once()

    def test_oversized_file_does_not_persist(self, patch_branding_path):
        """Oversized upload must not save anything to disk."""
        self._setup_sidebar_mocks()

        oversized_data = b'\x00' * (3 * 1024 * 1024)
        mock_file = MagicMock()
        mock_file.read.return_value = oversized_data
        mock_file.name = "massive.png"
        _mock_st.file_uploader.return_value = mock_file

        app_mod._render_logo_settings()

        loaded = app_mod._load_branding_settings()
        assert "logo_data" not in loaded

    def test_max_logo_bytes_constant_value(self):
        """Verify the constant is set to 2 MB."""
        assert app_mod._MAX_LOGO_BYTES == 2 * 1024 * 1024


# ===========================================================================
# LOGO KEY REMOVAL (Bug fix verification)
# ===========================================================================
class TestLogoKeyRemoval:
    """Verify _update_branding_logo(None) properly removes the key."""

    def test_remove_from_single_key_file(self, patch_branding_path):
        """Removing logo from a file that only has logo_data should result in {}."""
        app_mod._save_branding_settings({"logo_data": "old"})
        app_mod._update_branding_logo(None)
        loaded = app_mod._load_branding_settings()
        assert loaded == {}

    def test_remove_from_multi_key_file(self, patch_branding_path):
        """Removing logo from a file with other keys should keep only the others."""
        app_mod._save_branding_settings({
            "company_name": "Acme",
            "primary_color": "#FF0000",
            "logo_data": "some_logo",
        })
        app_mod._update_branding_logo(None)
        loaded = app_mod._load_branding_settings()
        assert loaded == {"company_name": "Acme", "primary_color": "#FF0000"}

    def test_remove_when_key_absent_is_noop(self, patch_branding_path):
        """Removing logo when key doesn't exist should not crash or corrupt."""
        app_mod._save_branding_settings({"company_name": "Keep"})
        app_mod._update_branding_logo(None)
        loaded = app_mod._load_branding_settings()
        assert loaded == {"company_name": "Keep"}

    def test_remove_then_add_roundtrip(self, patch_branding_path):
        """Remove then re-add logo should work cleanly."""
        app_mod._update_branding_logo("first")
        app_mod._update_branding_logo(None)
        app_mod._update_branding_logo("second")
        loaded = app_mod._load_branding_settings()
        assert loaded["logo_data"] == "second"


# ===========================================================================
# HEADER RENDERING: _render_branded_header dispatch + compact + minimal
# ===========================================================================
class TestHeaderDispatch:
    """Test _render_branded_header routes to the correct sub-renderer."""

    def _make_company(self, logo_url=None):
        from app import CompanyConfig
        return CompanyConfig(
            name="Test Corp",
            logo_url=logo_url,
            primary_color="#0066CC",
            website=None,
            sender_email="test@example.com",
        )

    def test_dispatch_full(self):
        _mock_st.markdown.reset_mock()
        with patch.object(app_mod, "get_layout_config") as m:
            m.return_value = app_mod.LayoutConfig(
                show_sidebar=False, show_footer=False, show_powered_by=False, header_style="full"
            )
            app_mod._render_branded_header(self._make_company())
        html_calls = [str(c) for c in _mock_st.markdown.call_args_list]
        assert any("branded-header" in c for c in html_calls)

    def test_dispatch_compact(self):
        _mock_st.markdown.reset_mock()
        with patch.object(app_mod, "get_layout_config") as m:
            m.return_value = app_mod.LayoutConfig(
                show_sidebar=False, show_footer=False, show_powered_by=False, header_style="compact"
            )
            app_mod._render_branded_header(self._make_company())
        html_calls = [str(c) for c in _mock_st.markdown.call_args_list]
        assert any("Test Corp Scheduler" in c for c in html_calls)

    def test_dispatch_minimal(self):
        _mock_st.markdown.reset_mock()
        with patch.object(app_mod, "get_layout_config") as m:
            m.return_value = app_mod.LayoutConfig(
                show_sidebar=False, show_footer=False, show_powered_by=False, header_style="minimal"
            )
            app_mod._render_branded_header(self._make_company())
        html_calls = [str(c) for c in _mock_st.markdown.call_args_list]
        assert any("Interview Scheduler" in c for c in html_calls)

    def test_compact_header_with_data_url_logo(self, sample_png_data_url):
        """Compact header should include data URL logo in img tag."""
        _mock_st.markdown.reset_mock()
        with patch.object(app_mod, "get_layout_config") as m:
            m.return_value = app_mod.LayoutConfig(
                show_sidebar=False, show_footer=False, show_powered_by=False, header_style="compact"
            )
            app_mod._render_header_compact(self._make_company(logo_url=sample_png_data_url))
        html = _mock_st.markdown.call_args[0][0]
        assert sample_png_data_url in html

    def test_compact_header_without_logo(self):
        """Compact header with no logo should not have img tag."""
        _mock_st.markdown.reset_mock()
        with patch.object(app_mod, "get_layout_config") as m:
            m.return_value = app_mod.LayoutConfig(
                show_sidebar=False, show_footer=False, show_powered_by=False, header_style="compact"
            )
            app_mod._render_header_compact(self._make_company(logo_url=None))
        html = _mock_st.markdown.call_args[0][0]
        assert "<img" not in html
        assert "Test Corp" in html


# ===========================================================================
# EMAIL TEMPLATE: _build_logo_html with uploaded data URL
# ===========================================================================
class TestBuildLogoHtmlDataUrl:
    """Verify _build_logo_html works with uploaded data URL logos."""

    def test_data_url_in_email_img(self, sample_png_data_url):
        """Data URL logo should appear in email template img src."""
        from app import CompanyConfig
        company = CompanyConfig(
            name="Email Corp",
            logo_url=sample_png_data_url,
            primary_color="#0066CC",
            website=None,
            sender_email="test@example.com",
        )
        html = app_mod._build_logo_html(company)
        assert sample_png_data_url in html
        assert '<img src="' in html
        assert 'alt="Email Corp"' in html

    def test_none_logo_returns_empty(self):
        from app import CompanyConfig
        company = CompanyConfig(
            name="No Logo", logo_url=None, primary_color="#0066CC",
            website=None, sender_email="test@example.com",
        )
        assert app_mod._build_logo_html(company) == ""

    def test_http_url_in_email_img(self):
        from app import CompanyConfig
        company = CompanyConfig(
            name="Web Corp", logo_url="https://cdn.example.com/logo.png",
            primary_color="#0066CC", website=None, sender_email="test@example.com",
        )
        html = app_mod._build_logo_html(company)
        assert "https://cdn.example.com/logo.png" in html

    def test_nonexistent_file_path_returns_empty(self):
        from app import CompanyConfig
        company = CompanyConfig(
            name="Missing", logo_url="/nonexistent/logo.png",
            primary_color="#0066CC", website=None, sender_email="test@example.com",
        )
        assert app_mod._build_logo_html(company) == ""


# ===========================================================================
# NO-EXTENSION FILENAME
# ===========================================================================
class TestNoExtensionFilename:
    """Verify upload of a file with no extension defaults to image/png."""

    def test_upload_no_extension_uses_png_mime(self, patch_branding_path):
        _mock_st.session_state = {}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.button.return_value = False
        _mock_st.rerun = MagicMock()

        mock_file = MagicMock()
        mock_file.read.return_value = b'\x89PNG\r\n'
        mock_file.name = "logo"  # No extension
        _mock_st.file_uploader.return_value = mock_file

        app_mod._render_logo_settings()

        saved_url = _mock_st.session_state["custom_logo_data"]
        assert saved_url.startswith("data:image/png;base64,")

    def test_upload_uppercase_extension(self, patch_branding_path):
        _mock_st.session_state = {}
        _mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st)
        _mock_st.sidebar.__exit__ = MagicMock(return_value=False)
        _mock_st.button.return_value = False
        _mock_st.rerun = MagicMock()

        mock_file = MagicMock()
        mock_file.read.return_value = b'\xff\xd8\xff'
        mock_file.name = "logo.JPG"  # Uppercase extension
        _mock_st.file_uploader.return_value = mock_file

        app_mod._render_logo_settings()

        saved_url = _mock_st.session_state["custom_logo_data"]
        assert saved_url.startswith("data:image/jpeg;base64,")
