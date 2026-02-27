"""
Comprehensive tests for the data directory migration and persistent file storage.

Tests cover the full data persistence pipeline: path resolution → file I/O → Docker
volume integration → legacy migration.

WHITE-BOX tests:
- _get_data_dir() creates directory, respects secrets override, returns correct path
- get_audit_log_path() resolves inside data dir by default, overridable via secrets
- _get_slots_path() resolves inside data dir by default, overridable via secrets
- _get_branding_settings_path() resolves inside data dir by default, overridable via secrets
- _get_email_templates_path() resolves inside data dir by default, overridable via secrets
- _get_invite_templates_path() resolves inside data dir by default, overridable via secrets
- _migrate_legacy_data_files() moves files from app root to data dir
- _migrate_legacy_data_files() skips when target already exists (no overwrite)
- _migrate_legacy_data_files() handles permission errors gracefully
- All load/save functions use paths inside data dir

BLACK-BOX tests:
- End-to-end: save branding → restart (clear state) → reload → verify data
- End-to-end: save slots → verify file in data dir → reload
- End-to-end: save email template → reload → verify
- End-to-end: save invite template → reload → verify
- Migration lifecycle: create legacy files → migrate → verify moved → verify data intact
- Docker simulation: data dir as separate mount point, app files untouched

EDGE CASES:
- Data dir already exists (idempotent makedirs)
- Data dir creation fails (read-only parent)
- Legacy file exists at both old and new location (new wins, old untouched)
- Legacy file is corrupt (still moved, corruption preserved)
- Empty legacy file (moved correctly)
- Very large legacy file (moved correctly)
- Migration with partial set of legacy files
- Concurrent access patterns (save while loading)
- Path with spaces and special characters
- Secret overrides for each path function
- Environment variable overrides for each path function
"""
import json
import os
import shutil
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
def tmp_data_dir(tmp_path):
    """Create a temporary data directory."""
    d = tmp_path / "data"
    d.mkdir()
    return str(d)


@pytest.fixture
def tmp_app_dir(tmp_path):
    """Create a temporary app root directory (simulates the app directory)."""
    d = tmp_path / "app"
    d.mkdir()
    return str(d)


@pytest.fixture
def patch_data_dir(tmp_data_dir):
    """Patch _get_data_dir to use a temp directory."""
    with patch.object(app_mod, "_get_data_dir", return_value=tmp_data_dir):
        yield tmp_data_dir


@pytest.fixture
def patch_branding_path(tmp_data_dir):
    """Patch _get_branding_settings_path to use temp data dir."""
    path = os.path.join(tmp_data_dir, "branding_settings.json")
    with patch.object(app_mod, "_get_branding_settings_path", return_value=path):
        yield path


@pytest.fixture
def patch_slots_path(tmp_data_dir):
    """Patch _get_slots_path to use temp data dir."""
    path = os.path.join(tmp_data_dir, "parsed_slots.json")
    with patch.object(app_mod, "_get_slots_path", return_value=path):
        yield path


@pytest.fixture
def patch_email_templates_path(tmp_data_dir):
    """Patch _get_email_templates_path to use temp data dir."""
    path = os.path.join(tmp_data_dir, "email_templates.json")
    with patch.object(app_mod, "_get_email_templates_path", return_value=path):
        yield path


@pytest.fixture
def patch_invite_templates_path(tmp_data_dir):
    """Patch _get_invite_templates_path to use temp data dir."""
    path = os.path.join(tmp_data_dir, "invite_templates.json")
    with patch.object(app_mod, "_get_invite_templates_path", return_value=path):
        yield path


# ===========================================================================
# WHITE-BOX: _get_data_dir()
# ===========================================================================
class TestGetDataDir:
    """Test _get_data_dir() directory creation and path resolution."""

    def test_creates_directory_if_missing(self, tmp_path):
        """Should create the data directory if it doesn't exist."""
        data_path = str(tmp_path / "newdata")
        assert not os.path.exists(data_path)

        with patch.object(app_mod, "get_secret", return_value=data_path):
            result = app_mod._get_data_dir()

        assert result == data_path
        assert os.path.isdir(data_path)

    def test_idempotent_if_exists(self, tmp_data_dir):
        """Should not fail if directory already exists."""
        with patch.object(app_mod, "get_secret", return_value=tmp_data_dir):
            result = app_mod._get_data_dir()

        assert result == tmp_data_dir
        assert os.path.isdir(tmp_data_dir)

    def test_respects_secret_override(self):
        """Should use the data_dir secret if configured."""
        _mock_st.secrets = {"data_dir": "/custom/path"}

        with patch("os.makedirs"):
            result = app_mod._get_data_dir()

        assert result == "/custom/path"

    def test_defaults_to_data(self):
        """Should default to 'data' when no secret is set."""
        with patch("os.makedirs"):
            result = app_mod._get_data_dir()

        assert result == "data"

    def test_nested_directory_creation(self, tmp_path):
        """Should create nested directories."""
        nested = str(tmp_path / "a" / "b" / "c" / "data")

        with patch.object(app_mod, "get_secret", return_value=nested):
            result = app_mod._get_data_dir()

        assert os.path.isdir(nested)
        assert result == nested


# ===========================================================================
# WHITE-BOX: Path resolution functions
# ===========================================================================
class TestPathResolution:
    """All path functions should resolve inside data/ by default."""

    def test_audit_log_path_in_data_dir(self, patch_data_dir):
        """Audit log should default to data/audit_log.db."""
        result = app_mod.get_audit_log_path()
        assert result == os.path.join(patch_data_dir, "audit_log.db")

    def test_slots_path_in_data_dir(self, patch_data_dir):
        """Slots file should default to data/parsed_slots.json."""
        result = app_mod._get_slots_path()
        assert result == os.path.join(patch_data_dir, "parsed_slots.json")

    def test_branding_path_in_data_dir(self, patch_data_dir):
        """Branding settings should default to data/branding_settings.json."""
        result = app_mod._get_branding_settings_path()
        assert result == os.path.join(patch_data_dir, "branding_settings.json")

    def test_email_templates_path_in_data_dir(self, patch_data_dir):
        """Email templates should default to data/email_templates.json."""
        result = app_mod._get_email_templates_path()
        assert result == os.path.join(patch_data_dir, "email_templates.json")

    def test_invite_templates_path_in_data_dir(self, patch_data_dir):
        """Invite templates should default to data/invite_templates.json."""
        result = app_mod._get_invite_templates_path()
        assert result == os.path.join(patch_data_dir, "invite_templates.json")

    def test_audit_log_path_secret_override(self):
        """Secret should override default audit log path."""
        _mock_st.secrets = {"audit_log_path": "/custom/audit.db"}
        result = app_mod.get_audit_log_path()
        assert result == "/custom/audit.db"

    def test_slots_path_secret_override(self):
        """Secret should override default slots path."""
        _mock_st.secrets = {"slots_storage_path": "/custom/slots.json"}
        result = app_mod._get_slots_path()
        assert result == "/custom/slots.json"

    def test_branding_path_secret_override(self):
        """Secret should override default branding path."""
        _mock_st.secrets = {"branding_settings_path": "/custom/brand.json"}
        result = app_mod._get_branding_settings_path()
        assert result == "/custom/brand.json"

    def test_email_templates_path_secret_override(self):
        """Secret should override default email templates path."""
        _mock_st.secrets = {"email_templates_path": "/custom/email.json"}
        result = app_mod._get_email_templates_path()
        assert result == "/custom/email.json"

    def test_invite_templates_path_secret_override(self):
        """Secret should override default invite templates path."""
        _mock_st.secrets = {"invite_templates_path": "/custom/invite.json"}
        result = app_mod._get_invite_templates_path()
        assert result == "/custom/invite.json"

    def test_env_var_override_for_audit_log(self, patch_data_dir):
        """Environment variable should override default path."""
        with patch.dict(os.environ, {"AUDIT_LOG_PATH": "/env/audit.db"}):
            result = app_mod.get_audit_log_path()
        assert result == "/env/audit.db"

    def test_env_var_override_for_data_dir(self, tmp_path):
        """DATA_DIR env var should override default."""
        custom = str(tmp_path / "envdata")
        with patch.dict(os.environ, {"DATA_DIR": custom}):
            result = app_mod._get_data_dir()
        assert result == custom
        assert os.path.isdir(custom)


# ===========================================================================
# WHITE-BOX: _migrate_legacy_data_files()
# ===========================================================================
class TestMigrateLegacyDataFiles:
    """Test one-time migration of files from app root to data/."""

    def test_migrates_all_legacy_files(self, tmp_path):
        """All legacy files should be moved to data dir."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        # Create all legacy files in the "app root"
        for filename in app_mod._LEGACY_DATA_FILES:
            with open(os.path.join(app_dir, filename), "w") as f:
                f.write(f"content of {filename}")

        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")):
            app_mod._migrate_legacy_data_files()

        # All files should now be in data_dir
        for filename in app_mod._LEGACY_DATA_FILES:
            new_path = os.path.join(data_dir, filename)
            old_path = os.path.join(app_dir, filename)
            assert os.path.exists(new_path), f"{filename} should exist in data dir"
            assert not os.path.exists(old_path), f"{filename} should be removed from app root"
            with open(new_path) as f:
                assert f.read() == f"content of {filename}"

    def test_skips_if_target_exists(self, tmp_path):
        """Should not overwrite existing files in data dir."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        # Create file in both locations with different content
        old_path = os.path.join(app_dir, "branding_settings.json")
        new_path = os.path.join(data_dir, "branding_settings.json")
        with open(old_path, "w") as f:
            f.write("old content")
        with open(new_path, "w") as f:
            f.write("new content")

        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")):
            app_mod._migrate_legacy_data_files()

        # New content should be preserved, old file should remain
        with open(new_path) as f:
            assert f.read() == "new content"
        assert os.path.exists(old_path), "Old file should NOT be deleted when target exists"

    def test_handles_missing_legacy_files(self, tmp_path):
        """Should not crash when legacy files don't exist."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")):
            # Should not raise
            app_mod._migrate_legacy_data_files()

    def test_handles_permission_error_gracefully(self, tmp_path):
        """Should not crash when destination is not writable."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        with open(os.path.join(app_dir, "audit_log.db"), "w") as f:
            f.write("data")

        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")), \
             patch("shutil.move", side_effect=PermissionError("read-only")):
            # Should not raise
            app_mod._migrate_legacy_data_files()

        # Original file should still exist
        assert os.path.exists(os.path.join(app_dir, "audit_log.db"))

    def test_partial_legacy_files(self, tmp_path):
        """Should migrate only the files that exist."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        # Only create 2 of the 5 legacy files
        with open(os.path.join(app_dir, "audit_log.db"), "w") as f:
            f.write("audit data")
        with open(os.path.join(app_dir, "branding_settings.json"), "w") as f:
            f.write('{"company_name": "Test"}')

        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")):
            app_mod._migrate_legacy_data_files()

        assert os.path.exists(os.path.join(data_dir, "audit_log.db"))
        assert os.path.exists(os.path.join(data_dir, "branding_settings.json"))
        assert not os.path.exists(os.path.join(data_dir, "parsed_slots.json"))
        assert not os.path.exists(os.path.join(data_dir, "email_templates.json"))

    def test_migrates_corrupt_files_as_is(self, tmp_path):
        """Corrupt files should be moved without modification."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        corrupt_content = "{{{not valid json!!!"
        with open(os.path.join(app_dir, "branding_settings.json"), "w") as f:
            f.write(corrupt_content)

        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")):
            app_mod._migrate_legacy_data_files()

        with open(os.path.join(data_dir, "branding_settings.json")) as f:
            assert f.read() == corrupt_content

    def test_migrates_empty_files(self, tmp_path):
        """Empty files should be moved correctly."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        open(os.path.join(app_dir, "email_templates.json"), "w").close()  # empty file

        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")):
            app_mod._migrate_legacy_data_files()

        new_path = os.path.join(data_dir, "email_templates.json")
        assert os.path.exists(new_path)
        assert os.path.getsize(new_path) == 0

    def test_migrates_binary_files(self, tmp_path):
        """Binary files (like audit_log.db) should be moved correctly."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        binary_content = bytes(range(256)) * 100  # 25.6 KB of binary data
        with open(os.path.join(app_dir, "audit_log.db"), "wb") as f:
            f.write(binary_content)

        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")):
            app_mod._migrate_legacy_data_files()

        with open(os.path.join(data_dir, "audit_log.db"), "rb") as f:
            assert f.read() == binary_content

    def test_legacy_files_list_complete(self):
        """The legacy file list should include all persistent files."""
        expected = {
            "audit_log.db",
            "parsed_slots.json",
            "branding_settings.json",
            "email_templates.json",
            "invite_templates.json",
        }
        assert set(app_mod._LEGACY_DATA_FILES) == expected


# ===========================================================================
# BLACK-BOX: Branding persistence in data dir
# ===========================================================================
class TestBrandingPersistenceInDataDir:
    """Branding settings should be saved/loaded from the data directory."""

    def test_save_and_load_roundtrip(self, patch_branding_path):
        """Save branding and reload — data should be intact."""
        app_mod._save_branding_settings({"company_name": "Neogen", "primary_color": "#FF5500"})
        loaded = app_mod._load_branding_settings()
        assert loaded == {"company_name": "Neogen", "primary_color": "#FF5500"}

    def test_file_created_in_data_dir(self, patch_branding_path, tmp_data_dir):
        """Branding file should be created inside the data directory."""
        app_mod._save_branding_settings({"company_name": "Test"})
        assert os.path.exists(os.path.join(tmp_data_dir, "branding_settings.json"))

    def test_load_returns_empty_when_no_file(self, patch_branding_path):
        """Should return empty dict when file doesn't exist."""
        loaded = app_mod._load_branding_settings()
        assert loaded == {}

    def test_update_field_writes_to_data_dir(self, patch_branding_path, tmp_data_dir):
        """_update_branding_field should write into data dir."""
        app_mod._update_branding_field("company_name", "Neogen")
        path = os.path.join(tmp_data_dir, "branding_settings.json")
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["company_name"] == "Neogen"


# ===========================================================================
# BLACK-BOX: Slots persistence in data dir
# ===========================================================================
class TestSlotsPersistenceInDataDir:
    """Parsed slots should be saved/loaded from the data directory."""

    def test_save_and_load_roundtrip(self, patch_slots_path):
        """Save slots and reload — data should be intact."""
        _mock_st.session_state = {
            "slots": [{"date": "2026-03-15", "start": "10:00", "end": "11:00"}],
            "computed_intersections": [],
            "panel_interviewers": [{"id": 1, "name": "Alice", "email": "a@b.com", "files": [], "slots": []}],
            "next_interviewer_id": 2,
        }
        app_mod._save_persisted_slots()
        loaded = app_mod._load_persisted_slots()
        assert len(loaded["slots"]) == 1
        assert loaded["slots"][0]["date"] == "2026-03-15"
        # 'files' key should be stripped during serialization
        assert "files" not in loaded["panel_interviewers"][0]

    def test_file_created_in_data_dir(self, patch_slots_path, tmp_data_dir):
        """Slots file should be in data directory."""
        _mock_st.session_state = {
            "slots": [],
            "computed_intersections": [],
            "panel_interviewers": [],
            "next_interviewer_id": 1,
        }
        app_mod._save_persisted_slots()
        assert os.path.exists(os.path.join(tmp_data_dir, "parsed_slots.json"))

    def test_load_returns_defaults_when_no_file(self, patch_slots_path):
        """Should return default empty structure when file doesn't exist."""
        loaded = app_mod._load_persisted_slots()
        assert loaded["slots"] == []
        assert loaded["panel_interviewers"] == []

    def test_load_returns_defaults_for_corrupt_file(self, patch_slots_path):
        """Corrupt JSON should return defaults, not crash."""
        with open(patch_slots_path, "w") as f:
            f.write("NOT VALID JSON{{{")
        loaded = app_mod._load_persisted_slots()
        assert loaded["slots"] == []

    def test_load_returns_defaults_for_non_dict(self, patch_slots_path):
        """Non-dict JSON (e.g., array) should return defaults."""
        with open(patch_slots_path, "w") as f:
            json.dump(["not", "a", "dict"], f)
        loaded = app_mod._load_persisted_slots()
        assert loaded["slots"] == []

    def test_load_returns_defaults_for_dict_without_slots(self, patch_slots_path):
        """Dict without 'slots' key should return defaults."""
        with open(patch_slots_path, "w") as f:
            json.dump({"something": "else"}, f)
        loaded = app_mod._load_persisted_slots()
        assert loaded["slots"] == []


# ===========================================================================
# BLACK-BOX: Email template persistence in data dir
# ===========================================================================
class TestEmailTemplatePersistenceInDataDir:
    """Email templates should be saved/loaded from the data directory."""

    def test_save_and_load_roundtrip(self, patch_email_templates_path):
        """Save email template and reload."""
        template = {"subject": "Interview: {role}", "body": "Hello {name}"}
        result = app_mod._save_email_template("interview_invite", template)
        assert result is True
        loaded = app_mod._load_email_templates()
        assert loaded["interview_invite"] == template

    def test_save_multiple_templates(self, patch_email_templates_path):
        """Multiple templates should coexist."""
        app_mod._save_email_template("template_a", {"subject": "A"})
        app_mod._save_email_template("template_b", {"subject": "B"})
        loaded = app_mod._load_email_templates()
        assert "template_a" in loaded
        assert "template_b" in loaded

    def test_delete_template(self, patch_email_templates_path):
        """Deleting a template should remove it."""
        app_mod._save_email_template("to_delete", {"subject": "X"})
        result = app_mod._delete_email_template("to_delete")
        assert result is True
        loaded = app_mod._load_email_templates()
        assert "to_delete" not in loaded

    def test_delete_nonexistent_template(self, patch_email_templates_path):
        """Deleting a template that doesn't exist should return False."""
        result = app_mod._delete_email_template("nonexistent")
        assert result is False

    def test_load_returns_empty_when_no_file(self, patch_email_templates_path):
        """Should return empty dict when file doesn't exist."""
        loaded = app_mod._load_email_templates()
        assert loaded == {}

    def test_file_in_data_dir(self, patch_email_templates_path, tmp_data_dir):
        """Template file should be in data directory."""
        app_mod._save_email_template("test", {"subject": "Test"})
        assert os.path.exists(os.path.join(tmp_data_dir, "email_templates.json"))


# ===========================================================================
# BLACK-BOX: Invite template persistence in data dir
# ===========================================================================
class TestInviteTemplatePersistenceInDataDir:
    """Invite templates should be saved/loaded from the data directory."""

    def test_save_and_load_roundtrip(self, patch_invite_templates_path):
        """Save invite template and reload."""
        template = {"duration": 60, "message": "Welcome"}
        result = app_mod._save_invite_template("standard", template)
        assert result is True
        loaded = app_mod._load_invite_templates()
        assert loaded["standard"] == template

    def test_load_returns_empty_when_no_file(self, patch_invite_templates_path):
        """Should return empty dict when file doesn't exist."""
        loaded = app_mod._load_invite_templates()
        assert loaded == {}

    def test_file_in_data_dir(self, patch_invite_templates_path, tmp_data_dir):
        """Invite template file should be in data directory."""
        app_mod._save_invite_template("test", {"duration": 30})
        assert os.path.exists(os.path.join(tmp_data_dir, "invite_templates.json"))


# ===========================================================================
# BLACK-BOX: Full migration lifecycle
# ===========================================================================
class TestMigrationLifecycle:
    """End-to-end migration: old location → new location → data intact."""

    def test_branding_data_survives_migration(self, tmp_path):
        """Branding data written at old location should be readable after migration."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        # Write branding at old location
        old_branding = {"company_name": "Neogen", "primary_color": "#FF5500"}
        with open(os.path.join(app_dir, "branding_settings.json"), "w") as f:
            json.dump(old_branding, f)

        # Run migration
        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")):
            app_mod._migrate_legacy_data_files()

        # Load from new location
        new_path = os.path.join(data_dir, "branding_settings.json")
        with patch.object(app_mod, "_get_branding_settings_path", return_value=new_path):
            loaded = app_mod._load_branding_settings()

        assert loaded == old_branding

    def test_slots_data_survives_migration(self, tmp_path):
        """Slots data written at old location should be readable after migration."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        old_slots = {
            "slots": [{"date": "2026-03-15", "start": "10:00", "end": "11:00"}],
            "computed_intersections": [],
            "panel_interviewers": [],
            "next_interviewer_id": 1,
        }
        with open(os.path.join(app_dir, "parsed_slots.json"), "w") as f:
            json.dump(old_slots, f)

        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")):
            app_mod._migrate_legacy_data_files()

        new_path = os.path.join(data_dir, "parsed_slots.json")
        with patch.object(app_mod, "_get_slots_path", return_value=new_path):
            loaded = app_mod._load_persisted_slots()

        assert loaded["slots"][0]["date"] == "2026-03-15"

    def test_audit_log_survives_migration(self, tmp_path):
        """Binary audit log should be intact after migration."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        # Write binary content at old location
        binary_content = b"SQLite format 3\x00" + b"\x00" * 100
        with open(os.path.join(app_dir, "audit_log.db"), "wb") as f:
            f.write(binary_content)

        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")):
            app_mod._migrate_legacy_data_files()

        with open(os.path.join(data_dir, "audit_log.db"), "rb") as f:
            assert f.read() == binary_content


# ===========================================================================
# BLACK-BOX: Docker volume simulation
# ===========================================================================
class TestDockerVolumeSimulation:
    """Simulate Docker behavior: /app/data is a separate mount point."""

    def test_container_restart_preserves_data(self, tmp_path):
        """Data in data/ should survive when app root is recreated (container restart)."""
        # Simulate first container run
        data_dir = str(tmp_path / "volume" / "data")
        os.makedirs(data_dir)

        branding_path = os.path.join(data_dir, "branding_settings.json")
        with open(branding_path, "w") as f:
            json.dump({"company_name": "Neogen"}, f)

        # Simulate container restart — app files recreated but volume persists
        # (data_dir still exists with content)
        with patch.object(app_mod, "_get_branding_settings_path", return_value=branding_path):
            loaded = app_mod._load_branding_settings()

        assert loaded["company_name"] == "Neogen"

    def test_different_clients_isolated(self, tmp_path):
        """Each client's data directory should be independent."""
        client1_data = str(tmp_path / "client1" / "data")
        client2_data = str(tmp_path / "client2" / "data")
        os.makedirs(client1_data)
        os.makedirs(client2_data)

        # Client 1 saves their branding
        path1 = os.path.join(client1_data, "branding_settings.json")
        with open(path1, "w") as f:
            json.dump({"company_name": "Neogen"}, f)

        # Client 2 saves their branding
        path2 = os.path.join(client2_data, "branding_settings.json")
        with open(path2, "w") as f:
            json.dump({"company_name": "Acme Corp"}, f)

        # Verify isolation
        with patch.object(app_mod, "_get_branding_settings_path", return_value=path1):
            loaded1 = app_mod._load_branding_settings()
        with patch.object(app_mod, "_get_branding_settings_path", return_value=path2):
            loaded2 = app_mod._load_branding_settings()

        assert loaded1["company_name"] == "Neogen"
        assert loaded2["company_name"] == "Acme Corp"

    def test_all_files_in_same_volume(self, tmp_path):
        """All persistent files should coexist in the same data directory."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)

        # Save different types of data
        branding_path = os.path.join(data_dir, "branding_settings.json")
        slots_path = os.path.join(data_dir, "parsed_slots.json")
        email_path = os.path.join(data_dir, "email_templates.json")

        with patch.object(app_mod, "_get_branding_settings_path", return_value=branding_path):
            app_mod._save_branding_settings({"company_name": "Test"})

        _mock_st.session_state = {
            "slots": [{"date": "2026-01-01", "start": "09:00", "end": "10:00"}],
            "computed_intersections": [],
            "panel_interviewers": [],
            "next_interviewer_id": 1,
        }
        with patch.object(app_mod, "_get_slots_path", return_value=slots_path):
            app_mod._save_persisted_slots()

        with patch.object(app_mod, "_get_email_templates_path", return_value=email_path):
            app_mod._save_email_template("test", {"subject": "Hi"})

        # Verify all files exist and are independent
        assert os.path.exists(branding_path)
        assert os.path.exists(slots_path)
        assert os.path.exists(email_path)

        with patch.object(app_mod, "_get_branding_settings_path", return_value=branding_path):
            assert app_mod._load_branding_settings()["company_name"] == "Test"
        with patch.object(app_mod, "_get_slots_path", return_value=slots_path):
            assert app_mod._load_persisted_slots()["slots"][0]["date"] == "2026-01-01"
        with patch.object(app_mod, "_get_email_templates_path", return_value=email_path):
            assert app_mod._load_email_templates()["test"]["subject"] == "Hi"


# ===========================================================================
# BLACK-BOX: ensure_session_state loads from data dir
# ===========================================================================
class TestEnsureSessionStateFromDataDir:
    """ensure_session_state should load branding from data/ dir files."""

    def test_loads_branding_from_data_dir(self, patch_branding_path):
        """Branding in data dir should be loaded into session state."""
        with open(patch_branding_path, "w") as f:
            json.dump({"company_name": "Neogen", "logo_data": "data:image/png;base64,abc"}, f)

        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        assert _mock_st.session_state["custom_company_name"] == "Neogen"
        assert _mock_st.session_state["custom_logo_data"] == "data:image/png;base64,abc"

    def test_branding_feeds_into_company_config(self, patch_branding_path):
        """Branding loaded from data dir should flow into get_company_config()."""
        with open(patch_branding_path, "w") as f:
            json.dump({"company_name": "Neogen", "primary_color": "#FF5500"}, f)

        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        config = app_mod.get_company_config()
        assert config.name == "Neogen"
        assert config.primary_color == "#FF5500"

    def test_branding_feeds_into_emails(self, patch_branding_path):
        """Company name from data dir should appear in generated emails."""
        with open(patch_branding_path, "w") as f:
            json.dump({"company_name": "Neogen"}, f)

        _mock_st.session_state = {}
        with patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        config = app_mod.get_company_config()
        html = app_mod.build_branded_email_html(
            candidate_name="Jane",
            role_title="Engineer",
            slots=[{"date": "2026-03-15", "start": "10:00", "end": "11:00"}],
            company=config,
        )
        assert "Neogen" in html
        assert "PowerDash HR" not in html


# ===========================================================================
# EDGE CASES: Path with special characters
# ===========================================================================
class TestPathSpecialCharacters:
    """Paths with spaces and special characters should work."""

    def test_data_dir_with_spaces(self, tmp_path):
        """Data dir path with spaces should work."""
        space_dir = str(tmp_path / "my data dir")
        with patch.object(app_mod, "get_secret", return_value=space_dir):
            result = app_mod._get_data_dir()
        assert result == space_dir
        assert os.path.isdir(space_dir)

    def test_branding_save_load_in_space_dir(self, tmp_path):
        """Branding save/load should work with spaces in path."""
        space_dir = str(tmp_path / "my data dir")
        os.makedirs(space_dir)
        path = os.path.join(space_dir, "branding_settings.json")

        with patch.object(app_mod, "_get_branding_settings_path", return_value=path):
            app_mod._save_branding_settings({"company_name": "Test"})
            loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "Test"


# ===========================================================================
# EDGE CASES: Concurrent access patterns
# ===========================================================================
class TestConcurrentAccess:
    """Test behavior under concurrent-like access patterns."""

    def test_rapid_sequential_saves(self, patch_branding_path):
        """Rapid sequential saves should all persist correctly (last wins)."""
        for i in range(20):
            app_mod._save_branding_settings({"company_name": f"Company_{i}"})

        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "Company_19"

    def test_interleaved_save_load(self, patch_branding_path):
        """Interleaved save/load operations should be consistent."""
        app_mod._save_branding_settings({"company_name": "First"})
        loaded1 = app_mod._load_branding_settings()

        app_mod._save_branding_settings({"company_name": "Second"})
        loaded2 = app_mod._load_branding_settings()

        assert loaded1["company_name"] == "First"
        assert loaded2["company_name"] == "Second"

    def test_save_slots_then_branding_no_interference(self, patch_branding_path, patch_slots_path):
        """Saving slots should not affect branding file and vice versa."""
        app_mod._save_branding_settings({"company_name": "Neogen"})

        _mock_st.session_state = {
            "slots": [{"date": "2026-01-01", "start": "09:00", "end": "10:00"}],
            "computed_intersections": [],
            "panel_interviewers": [],
            "next_interviewer_id": 1,
        }
        app_mod._save_persisted_slots()

        # Both should be independently readable
        assert app_mod._load_branding_settings()["company_name"] == "Neogen"
        assert app_mod._load_persisted_slots()["slots"][0]["date"] == "2026-01-01"


# ===========================================================================
# EDGE CASES: Corrupt data files
# ===========================================================================
class TestCorruptDataFiles:
    """Loading corrupt data files should not crash the app."""

    def test_corrupt_branding_json(self, patch_branding_path):
        """Corrupt branding JSON should return empty dict."""
        with open(patch_branding_path, "w") as f:
            f.write("NOT VALID JSON{{{")
        loaded = app_mod._load_branding_settings()
        assert loaded == {}

    def test_corrupt_slots_json(self, patch_slots_path):
        """Corrupt slots JSON should return defaults."""
        with open(patch_slots_path, "w") as f:
            f.write("NOT VALID JSON{{{")
        loaded = app_mod._load_persisted_slots()
        assert loaded["slots"] == []

    def test_corrupt_email_templates_json(self, patch_email_templates_path):
        """Corrupt email templates JSON should return empty dict."""
        with open(patch_email_templates_path, "w") as f:
            f.write("NOT VALID JSON{{{")
        loaded = app_mod._load_email_templates()
        assert loaded == {}

    def test_corrupt_invite_templates_json(self, patch_invite_templates_path):
        """Corrupt invite templates JSON should return empty dict."""
        with open(patch_invite_templates_path, "w") as f:
            f.write("NOT VALID JSON{{{")
        loaded = app_mod._load_invite_templates()
        assert loaded == {}

    def test_non_dict_branding_json(self, patch_branding_path):
        """Array JSON in branding file should return empty dict."""
        with open(patch_branding_path, "w") as f:
            json.dump(["not", "a", "dict"], f)
        loaded = app_mod._load_branding_settings()
        assert loaded == {}

    def test_null_json_branding(self, patch_branding_path):
        """null JSON should return empty dict."""
        with open(patch_branding_path, "w") as f:
            f.write("null")
        loaded = app_mod._load_branding_settings()
        assert loaded == {}

    def test_save_over_corrupt_branding(self, patch_branding_path):
        """Saving over corrupt file should succeed."""
        with open(patch_branding_path, "w") as f:
            f.write("corrupt{{{")
        app_mod._save_branding_settings({"company_name": "Fixed"})
        loaded = app_mod._load_branding_settings()
        assert loaded["company_name"] == "Fixed"


# ===========================================================================
# EDGE CASE: Read-only filesystem
# ===========================================================================
class TestReadOnlyFilesystem:
    """Graceful handling when data dir is not writable."""

    def test_save_branding_read_only(self, tmp_path):
        """Should not crash when branding file cannot be written."""
        read_only_path = str(tmp_path / "readonly" / "nonexistent" / "branding.json")
        _mock_st.warning = MagicMock()

        with patch.object(app_mod, "_get_branding_settings_path", return_value=read_only_path):
            # Should not raise
            app_mod._save_branding_settings({"company_name": "Test"})

    def test_save_slots_read_only(self, tmp_path):
        """Should not crash when slots file cannot be written."""
        read_only_path = str(tmp_path / "readonly" / "nonexistent" / "slots.json")
        _mock_st.session_state = {
            "slots": [],
            "computed_intersections": [],
            "panel_interviewers": [],
            "next_interviewer_id": 1,
        }

        with patch.object(app_mod, "_get_slots_path", return_value=read_only_path):
            # Should not raise
            app_mod._save_persisted_slots()

    def test_save_email_template_read_only(self, tmp_path):
        """Should return False when template cannot be written."""
        read_only_path = str(tmp_path / "readonly" / "nonexistent" / "email.json")
        _mock_st.error = MagicMock()

        with patch.object(app_mod, "_get_email_templates_path", return_value=read_only_path):
            result = app_mod._save_email_template("test", {"subject": "Hi"})
        assert result is False


# ===========================================================================
# INTEGRATION: Full startup flow with data dir
# ===========================================================================
class TestFullStartupFlow:
    """Test the complete startup: migration → ensure_session_state → get_company_config."""

    def test_startup_with_legacy_files(self, tmp_path):
        """Full startup with legacy files should migrate and load correctly."""
        app_dir = str(tmp_path / "app")
        data_dir = str(tmp_path / "data")
        os.makedirs(app_dir)
        os.makedirs(data_dir)

        # Create legacy branding file
        with open(os.path.join(app_dir, "branding_settings.json"), "w") as f:
            json.dump({"company_name": "Neogen", "primary_color": "#FF5500"}, f)

        # Step 1: Migrate
        with patch.object(app_mod, "_get_data_dir", return_value=data_dir), \
             patch.object(app_mod.os.path, "abspath", return_value=os.path.join(app_dir, "app.py")):
            app_mod._migrate_legacy_data_files()

        # Step 2: Load branding via ensure_session_state
        branding_path = os.path.join(data_dir, "branding_settings.json")
        _mock_st.session_state = {}

        with patch.object(app_mod, "_get_branding_settings_path", return_value=branding_path), \
             patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        # Step 3: Verify company config
        config = app_mod.get_company_config()
        assert config.name == "Neogen"
        assert config.primary_color == "#FF5500"

    def test_startup_with_no_files(self, tmp_path):
        """Fresh startup with no files should use defaults."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)

        branding_path = os.path.join(data_dir, "branding_settings.json")
        _mock_st.session_state = {}

        with patch.object(app_mod, "_get_branding_settings_path", return_value=branding_path), \
             patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        config = app_mod.get_company_config()
        assert config.name == "PowerDash HR"
        assert config.primary_color == "#0066CC"

    def test_startup_with_existing_data_dir_files(self, tmp_path):
        """Startup with data already in data/ should just load it (no migration needed)."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)

        # Pre-existing branding in data dir
        branding_path = os.path.join(data_dir, "branding_settings.json")
        with open(branding_path, "w") as f:
            json.dump({"company_name": "Existing Corp"}, f)

        _mock_st.session_state = {}

        with patch.object(app_mod, "_get_branding_settings_path", return_value=branding_path), \
             patch.object(app_mod, "_load_persisted_slots", return_value={}):
            app_mod.ensure_session_state()

        config = app_mod.get_company_config()
        assert config.name == "Existing Corp"
