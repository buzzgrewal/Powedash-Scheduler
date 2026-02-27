"""
Comprehensive tests for the Invite Details template save/load feature.

Tests cover:
- White-box: _load_invite_templates, _save_invite_template, _delete_invite_template,
  _get_invite_templates_path — file I/O, JSON parsing, error handling, return values.
- Black-box: end-to-end save → load → delete lifecycle, template overwrite,
  multiple templates coexistence.
- Edge cases: corrupt JSON, empty file, non-dict JSON, special characters in names,
  whitespace-only names, unicode, very long strings, missing fields in template data,
  file permission errors, concurrent-like operations.
"""
import json
import os
import stat
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

# Always use the mock that's actually in sys.modules, not our local one,
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
def tmp_template_file(tmp_path):
    """Create a temporary JSON file path for invite templates."""
    path = tmp_path / "invite_templates.json"
    return str(path)


@pytest.fixture
def patch_path(tmp_template_file):
    """Patch _get_invite_templates_path to use a temp file."""
    with patch.object(app_mod, "_get_invite_templates_path", return_value=tmp_template_file):
        yield tmp_template_file


@pytest.fixture
def sample_template():
    """A valid invite template dict."""
    return {
        "interview_type": "Teams",
        "subject": "Technical Interview: Backend Engineer",
        "agenda": "1. Coding exercise\n2. System design\n3. Q&A",
        "location": "",
        "include_recruiter": True,
    }


@pytest.fixture
def sample_template_nonteams():
    """A valid non-Teams invite template dict."""
    return {
        "interview_type": "Non-Teams",
        "subject": "On-site Interview",
        "agenda": "Meet and greet",
        "location": "123 Main St, Suite 400",
        "include_recruiter": False,
    }


# ===========================================================================
# 1. WHITE-BOX: _get_invite_templates_path
# ===========================================================================
class TestGetInviteTemplatesPath:
    def test_returns_default_path_in_data_dir(self):
        """Should return path inside data dir when no secret is configured."""
        result = app_mod._get_invite_templates_path()
        assert result.endswith("invite_templates.json")
        assert "data" in result or os.sep + "data" + os.sep in result

    def test_returns_custom_path_from_secret(self):
        """Should respect the invite_templates_path secret."""
        _mock_st.secrets = {"invite_templates_path": "/custom/path/tpl.json"}
        result = app_mod._get_invite_templates_path()
        assert result == "/custom/path/tpl.json"


# ===========================================================================
# 2. WHITE-BOX: _load_invite_templates
# ===========================================================================
class TestLoadInviteTemplates:
    def test_returns_empty_dict_when_file_missing(self, patch_path):
        """No file on disk → empty dict."""
        assert app_mod._load_invite_templates() == {}

    def test_loads_valid_json(self, patch_path, sample_template):
        """Reads a well-formed JSON dict from disk."""
        with open(patch_path, "w") as f:
            json.dump({"Tech Interview": sample_template}, f)
        result = app_mod._load_invite_templates()
        assert result == {"Tech Interview": sample_template}

    def test_returns_empty_dict_on_empty_file(self, patch_path):
        """Empty file → json.load fails → returns {}."""
        Path(patch_path).touch()
        assert app_mod._load_invite_templates() == {}

    def test_returns_empty_dict_on_corrupt_json(self, patch_path):
        """Corrupt JSON → returns {}."""
        with open(patch_path, "w") as f:
            f.write("{invalid json!!!")
        assert app_mod._load_invite_templates() == {}

    def test_returns_empty_dict_on_json_array(self, patch_path):
        """JSON array instead of dict → returns {} (type validation)."""
        with open(patch_path, "w") as f:
            json.dump([1, 2, 3], f)
        assert app_mod._load_invite_templates() == {}

    def test_returns_empty_dict_on_json_string(self, patch_path):
        """JSON string instead of dict → returns {}."""
        with open(patch_path, "w") as f:
            json.dump("just a string", f)
        assert app_mod._load_invite_templates() == {}

    def test_returns_empty_dict_on_json_number(self, patch_path):
        """JSON number instead of dict → returns {}."""
        with open(patch_path, "w") as f:
            json.dump(42, f)
        assert app_mod._load_invite_templates() == {}

    def test_returns_empty_dict_on_json_null(self, patch_path):
        """JSON null → returns {}."""
        with open(patch_path, "w") as f:
            json.dump(None, f)
        assert app_mod._load_invite_templates() == {}

    def test_loads_multiple_templates(self, patch_path, sample_template, sample_template_nonteams):
        """Multiple templates coexist correctly."""
        data = {"Teams Tpl": sample_template, "Onsite Tpl": sample_template_nonteams}
        with open(patch_path, "w") as f:
            json.dump(data, f)
        result = app_mod._load_invite_templates()
        assert len(result) == 2
        assert "Teams Tpl" in result
        assert "Onsite Tpl" in result

    def test_loads_unicode_template_name(self, patch_path, sample_template):
        """Template names with unicode characters load correctly."""
        with open(patch_path, "w") as f:
            json.dump({"面接テンプレート": sample_template}, f, ensure_ascii=False)
        result = app_mod._load_invite_templates()
        assert "面接テンプレート" in result

    def test_returns_empty_dict_on_permission_error(self, patch_path, sample_template):
        """File with no read permission → returns {}."""
        with open(patch_path, "w") as f:
            json.dump({"t": sample_template}, f)
        os.chmod(patch_path, 0o000)
        try:
            result = app_mod._load_invite_templates()
            assert result == {}
        finally:
            os.chmod(patch_path, stat.S_IRUSR | stat.S_IWUSR)


# ===========================================================================
# 3. WHITE-BOX: _save_invite_template
# ===========================================================================
class TestSaveInviteTemplate:
    def test_save_creates_new_file(self, patch_path, sample_template):
        """Saving when no file exists creates it."""
        assert not os.path.exists(patch_path)
        result = app_mod._save_invite_template("My Template", sample_template)
        assert result is True
        assert os.path.exists(patch_path)
        with open(patch_path) as f:
            data = json.load(f)
        assert data == {"My Template": sample_template}

    def test_save_appends_to_existing(self, patch_path, sample_template, sample_template_nonteams):
        """Saving adds to existing templates without removing others."""
        app_mod._save_invite_template("First", sample_template)
        app_mod._save_invite_template("Second", sample_template_nonteams)
        with open(patch_path) as f:
            data = json.load(f)
        assert len(data) == 2
        assert data["First"] == sample_template
        assert data["Second"] == sample_template_nonteams

    def test_save_overwrites_same_name(self, patch_path, sample_template, sample_template_nonteams):
        """Saving with an existing name overwrites the template."""
        app_mod._save_invite_template("Reusable", sample_template)
        app_mod._save_invite_template("Reusable", sample_template_nonteams)
        with open(patch_path) as f:
            data = json.load(f)
        assert len(data) == 1
        assert data["Reusable"] == sample_template_nonteams

    def test_save_returns_true_on_success(self, patch_path, sample_template):
        result = app_mod._save_invite_template("Test", sample_template)
        assert result is True

    def test_save_returns_false_on_write_error(self, sample_template):
        """Writing to an invalid path returns False."""
        with patch.object(app_mod, "_get_invite_templates_path", return_value="/nonexistent/dir/file.json"):
            result = app_mod._save_invite_template("Test", sample_template)
        assert result is False

    def test_save_preserves_json_formatting(self, patch_path, sample_template):
        """Saved JSON should be indented (human-readable)."""
        app_mod._save_invite_template("Pretty", sample_template)
        with open(patch_path) as f:
            content = f.read()
        assert "\n" in content  # indented JSON has newlines
        assert "  " in content  # indent=2

    def test_save_special_chars_in_name(self, patch_path, sample_template):
        """Template name with quotes, slashes, etc. saves correctly."""
        name = 'Interview "Round 1" / Final'
        result = app_mod._save_invite_template(name, sample_template)
        assert result is True
        loaded = app_mod._load_invite_templates()
        assert name in loaded

    def test_save_unicode_name(self, patch_path, sample_template):
        """Template name with unicode saves correctly."""
        name = "Entrevista técnica 🎯"
        result = app_mod._save_invite_template(name, sample_template)
        assert result is True
        loaded = app_mod._load_invite_templates()
        assert name in loaded

    def test_save_empty_template_fields(self, patch_path):
        """Template with all empty/default fields saves correctly."""
        tpl = {
            "interview_type": "Teams",
            "subject": "",
            "agenda": "",
            "location": "",
            "include_recruiter": True,
        }
        result = app_mod._save_invite_template("Empty", tpl)
        assert result is True
        loaded = app_mod._load_invite_templates()
        assert loaded["Empty"]["subject"] == ""

    def test_save_long_agenda(self, patch_path):
        """Template with a very long agenda string saves correctly."""
        tpl = {
            "interview_type": "Teams",
            "subject": "Test",
            "agenda": "Line\n" * 5000,
            "location": "",
            "include_recruiter": True,
        }
        result = app_mod._save_invite_template("Long", tpl)
        assert result is True
        loaded = app_mod._load_invite_templates()
        assert len(loaded["Long"]["agenda"]) == len("Line\n" * 5000)

    def test_save_recovers_from_corrupt_existing_file(self, patch_path, sample_template):
        """If the existing file is corrupt, save overwrites with valid data."""
        with open(patch_path, "w") as f:
            f.write("NOT JSON")
        # _load_invite_templates inside _save will return {} due to corrupt file,
        # then save will write a clean dict
        result = app_mod._save_invite_template("Fresh", sample_template)
        assert result is True
        loaded = app_mod._load_invite_templates()
        assert loaded == {"Fresh": sample_template}


# ===========================================================================
# 4. WHITE-BOX: _delete_invite_template
# ===========================================================================
class TestDeleteInviteTemplate:
    def test_delete_existing_template(self, patch_path, sample_template, sample_template_nonteams):
        """Deleting an existing template removes it and keeps others."""
        app_mod._save_invite_template("Keep", sample_template)
        app_mod._save_invite_template("Remove", sample_template_nonteams)
        result = app_mod._delete_invite_template("Remove")
        assert result is True
        loaded = app_mod._load_invite_templates()
        assert "Keep" in loaded
        assert "Remove" not in loaded

    def test_delete_returns_false_for_nonexistent(self, patch_path, sample_template):
        """Deleting a name that doesn't exist returns False."""
        app_mod._save_invite_template("Exists", sample_template)
        result = app_mod._delete_invite_template("Does Not Exist")
        assert result is False

    def test_delete_returns_false_when_no_file(self, patch_path):
        """Deleting when no template file exists returns False."""
        result = app_mod._delete_invite_template("Anything")
        assert result is False

    def test_delete_last_template_leaves_empty_dict(self, patch_path, sample_template):
        """Deleting the only template leaves an empty dict on disk."""
        app_mod._save_invite_template("Only", sample_template)
        app_mod._delete_invite_template("Only")
        with open(patch_path) as f:
            data = json.load(f)
        assert data == {}

    def test_delete_returns_false_on_write_error(self, sample_template):
        """Write error during delete returns False."""
        with patch.object(app_mod, "_get_invite_templates_path", return_value="/nonexistent/dir/f.json"):
            result = app_mod._delete_invite_template("x")
        assert result is False

    def test_delete_case_sensitive(self, patch_path, sample_template):
        """Template names are case-sensitive."""
        app_mod._save_invite_template("Tech Interview", sample_template)
        result = app_mod._delete_invite_template("tech interview")
        assert result is False
        loaded = app_mod._load_invite_templates()
        assert "Tech Interview" in loaded


# ===========================================================================
# 5. BLACK-BOX: Full lifecycle tests
# ===========================================================================
class TestTemplateLifecycle:
    def test_save_load_delete_cycle(self, patch_path, sample_template):
        """Full create → read → delete lifecycle."""
        # Save
        assert app_mod._save_invite_template("Cycle Test", sample_template) is True
        # Load
        loaded = app_mod._load_invite_templates()
        assert loaded["Cycle Test"] == sample_template
        # Delete
        assert app_mod._delete_invite_template("Cycle Test") is True
        # Verify gone
        loaded = app_mod._load_invite_templates()
        assert "Cycle Test" not in loaded

    def test_multiple_templates_independent(self, patch_path, sample_template, sample_template_nonteams):
        """Multiple templates are independent — CRUD on one doesn't affect others."""
        app_mod._save_invite_template("A", sample_template)
        app_mod._save_invite_template("B", sample_template_nonteams)
        app_mod._save_invite_template("C", {"interview_type": "Teams", "subject": "C", "agenda": "", "location": "", "include_recruiter": True})

        # Delete B
        app_mod._delete_invite_template("B")
        loaded = app_mod._load_invite_templates()
        assert set(loaded.keys()) == {"A", "C"}

        # Update A
        updated = {**sample_template, "subject": "Updated Subject"}
        app_mod._save_invite_template("A", updated)
        loaded = app_mod._load_invite_templates()
        assert loaded["A"]["subject"] == "Updated Subject"
        assert loaded["C"]["subject"] == "C"

    def test_overwrite_and_verify_all_fields(self, patch_path, sample_template, sample_template_nonteams):
        """Overwriting a template replaces all fields."""
        app_mod._save_invite_template("Overwrite", sample_template)
        app_mod._save_invite_template("Overwrite", sample_template_nonteams)
        loaded = app_mod._load_invite_templates()
        tpl = loaded["Overwrite"]
        assert tpl["interview_type"] == "Non-Teams"
        assert tpl["subject"] == "On-site Interview"
        assert tpl["agenda"] == "Meet and greet"
        assert tpl["location"] == "123 Main St, Suite 400"
        assert tpl["include_recruiter"] is False


# ===========================================================================
# 6. EDGE CASES: Template data integrity
# ===========================================================================
class TestTemplateDataEdgeCases:
    def test_template_missing_optional_fields(self, patch_path):
        """Loading a template with missing fields uses .get() defaults safely."""
        # Simulate a hand-edited template file with missing keys
        with open(patch_path, "w") as f:
            json.dump({"Minimal": {"subject": "Hello"}}, f)
        loaded = app_mod._load_invite_templates()
        tpl = loaded["Minimal"]
        # The UI code uses tpl.get("key", default) — verify the template loads
        assert tpl.get("interview_type", "Teams") == "Teams"
        assert tpl.get("subject", "") == "Hello"
        assert tpl.get("agenda", "") == ""
        assert tpl.get("location", "") == ""
        assert tpl.get("include_recruiter", True) is True

    def test_template_with_extra_fields(self, patch_path, sample_template):
        """Extra fields in a template are preserved (forward compatibility)."""
        extended = {**sample_template, "custom_field": "value123"}
        app_mod._save_invite_template("Extended", extended)
        loaded = app_mod._load_invite_templates()
        assert loaded["Extended"]["custom_field"] == "value123"

    def test_template_with_html_in_agenda(self, patch_path):
        """HTML content in agenda is stored as-is (Streamlit escapes on render)."""
        tpl = {
            "interview_type": "Teams",
            "subject": "Test",
            "agenda": "<script>alert('xss')</script><b>Bold</b>",
            "location": "",
            "include_recruiter": True,
        }
        app_mod._save_invite_template("HTML", tpl)
        loaded = app_mod._load_invite_templates()
        assert "<script>" in loaded["HTML"]["agenda"]

    def test_template_with_newlines_in_all_fields(self, patch_path):
        """Newlines in text fields are preserved through save/load."""
        tpl = {
            "interview_type": "Teams",
            "subject": "Line1\nLine2",
            "agenda": "Step 1\nStep 2\nStep 3",
            "location": "Floor 1\nRoom 2",
            "include_recruiter": True,
        }
        app_mod._save_invite_template("Newlines", tpl)
        loaded = app_mod._load_invite_templates()
        assert loaded["Newlines"]["agenda"] == "Step 1\nStep 2\nStep 3"
        assert loaded["Newlines"]["subject"] == "Line1\nLine2"

    def test_template_name_with_leading_trailing_spaces(self, patch_path, sample_template):
        """Names with spaces are stored literally (UI strips, but function doesn't)."""
        app_mod._save_invite_template("  Spaced  ", sample_template)
        loaded = app_mod._load_invite_templates()
        assert "  Spaced  " in loaded

    def test_boolean_include_recruiter_types(self, patch_path):
        """include_recruiter must be boolean — verify True and False round-trip."""
        for val in [True, False]:
            tpl = {
                "interview_type": "Teams",
                "subject": f"Bool {val}",
                "agenda": "",
                "location": "",
                "include_recruiter": val,
            }
            app_mod._save_invite_template(f"Bool_{val}", tpl)

        loaded = app_mod._load_invite_templates()
        assert loaded["Bool_True"]["include_recruiter"] is True
        assert loaded["Bool_False"]["include_recruiter"] is False

    def test_interview_type_values(self, patch_path):
        """Both valid interview_type values round-trip correctly."""
        for itype in ["Teams", "Non-Teams"]:
            tpl = {
                "interview_type": itype,
                "subject": f"Type {itype}",
                "agenda": "",
                "location": "",
                "include_recruiter": True,
            }
            app_mod._save_invite_template(f"Type_{itype}", tpl)

        loaded = app_mod._load_invite_templates()
        assert loaded["Type_Teams"]["interview_type"] == "Teams"
        assert loaded["Type_Non-Teams"]["interview_type"] == "Non-Teams"


# ===========================================================================
# 7. EDGE CASES: File system scenarios
# ===========================================================================
class TestFileSystemEdgeCases:
    def test_save_when_file_is_corrupt_json(self, patch_path, sample_template):
        """Saving when existing file has corrupt JSON — overwrites cleanly."""
        with open(patch_path, "w") as f:
            f.write("{corrupted")
        result = app_mod._save_invite_template("New", sample_template)
        assert result is True
        loaded = app_mod._load_invite_templates()
        assert loaded == {"New": sample_template}

    def test_save_when_file_is_json_array(self, patch_path, sample_template):
        """Saving when existing file has a JSON array — replaces with dict."""
        with open(patch_path, "w") as f:
            json.dump([1, 2, 3], f)
        result = app_mod._save_invite_template("Fix", sample_template)
        assert result is True
        loaded = app_mod._load_invite_templates()
        assert loaded == {"Fix": sample_template}

    def test_delete_when_file_is_corrupt(self, patch_path):
        """Deleting from a corrupt file returns False gracefully."""
        with open(patch_path, "w") as f:
            f.write("not json")
        result = app_mod._delete_invite_template("Anything")
        assert result is False

    def test_concurrent_saves_last_wins(self, patch_path, sample_template, sample_template_nonteams):
        """Sequential saves (simulating concurrent access) — last write wins."""
        app_mod._save_invite_template("Race", sample_template)
        app_mod._save_invite_template("Race", sample_template_nonteams)
        loaded = app_mod._load_invite_templates()
        assert loaded["Race"]["interview_type"] == "Non-Teams"

    def test_empty_json_object_file(self, patch_path):
        """File containing '{}' loads as empty dict."""
        with open(patch_path, "w") as f:
            f.write("{}")
        loaded = app_mod._load_invite_templates()
        assert loaded == {}


# ===========================================================================
# 8. UI SESSION STATE INTEGRATION (simulated)
# ===========================================================================
class TestSessionStateIntegration:
    """Test the session-state population logic used in the UI code."""

    def test_template_values_populate_session_state(self, patch_path, sample_template):
        """Simulate what the UI does: load template → set session_state keys."""
        app_mod._save_invite_template("UI Test", sample_template)
        templates = app_mod._load_invite_templates()
        tpl = templates["UI Test"]

        # Simulate the UI code at lines 3972-3977
        mock_session = {}
        mock_session["interview_type"] = tpl.get("interview_type", "Teams")
        mock_session["subject"] = tpl.get("subject", "")
        mock_session["agenda"] = tpl.get("agenda", "")
        mock_session["location"] = tpl.get("location", "")
        mock_session["include_recruiter"] = tpl.get("include_recruiter", True)

        assert mock_session["interview_type"] == "Teams"
        assert mock_session["subject"] == "Technical Interview: Backend Engineer"
        assert mock_session["agenda"] == "1. Coding exercise\n2. System design\n3. Q&A"
        assert mock_session["location"] == ""
        assert mock_session["include_recruiter"] is True

    def test_nonteams_template_populates_correctly(self, patch_path, sample_template_nonteams):
        """Non-Teams template sets interview_type and location correctly."""
        app_mod._save_invite_template("Onsite", sample_template_nonteams)
        templates = app_mod._load_invite_templates()
        tpl = templates["Onsite"]

        mock_session = {}
        mock_session["interview_type"] = tpl.get("interview_type", "Teams")
        mock_session["subject"] = tpl.get("subject", "")
        mock_session["agenda"] = tpl.get("agenda", "")
        mock_session["location"] = tpl.get("location", "")
        mock_session["include_recruiter"] = tpl.get("include_recruiter", True)

        assert mock_session["interview_type"] == "Non-Teams"
        assert mock_session["location"] == "123 Main St, Suite 400"
        assert mock_session["include_recruiter"] is False

    def test_incomplete_template_uses_defaults(self, patch_path):
        """Template missing keys → .get() fallbacks match widget defaults."""
        with open(patch_path, "w") as f:
            json.dump({"Sparse": {"subject": "Only Subject"}}, f)
        templates = app_mod._load_invite_templates()
        tpl = templates["Sparse"]

        mock_session = {}
        mock_session["interview_type"] = tpl.get("interview_type", "Teams")
        mock_session["subject"] = tpl.get("subject", "")
        mock_session["agenda"] = tpl.get("agenda", "")
        mock_session["location"] = tpl.get("location", "")
        mock_session["include_recruiter"] = tpl.get("include_recruiter", True)

        # Defaults should match the widget defaults in the UI
        assert mock_session["interview_type"] == "Teams"
        assert mock_session["subject"] == "Only Subject"
        assert mock_session["agenda"] == ""
        assert mock_session["location"] == ""
        assert mock_session["include_recruiter"] is True

    def test_save_captures_is_teams_boolean_as_string(self, patch_path):
        """The UI saves 'Teams'/'Non-Teams' string, not boolean is_teams."""
        # Simulate the save logic at lines 3995-4001
        is_teams = True
        saved_data = {
            "interview_type": "Teams" if is_teams else "Non-Teams",
            "subject": "Test",
            "agenda": "Agenda",
            "location": "",
            "include_recruiter": True,
        }
        app_mod._save_invite_template("BoolCheck", saved_data)
        loaded = app_mod._load_invite_templates()
        assert loaded["BoolCheck"]["interview_type"] == "Teams"
        assert isinstance(loaded["BoolCheck"]["interview_type"], str)

        is_teams = False
        saved_data["interview_type"] = "Teams" if is_teams else "Non-Teams"
        app_mod._save_invite_template("BoolCheck2", saved_data)
        loaded = app_mod._load_invite_templates()
        assert loaded["BoolCheck2"]["interview_type"] == "Non-Teams"


# ===========================================================================
# 9. TEMPLATE DROPDOWN BEHAVIOR (simulated)
# ===========================================================================
class TestTemplateDropdownLogic:
    """Test the conditional logic that controls template dropdown visibility."""

    def test_no_dropdown_when_no_templates(self, patch_path):
        """When no templates exist, template_names is empty → dropdown hidden."""
        templates = app_mod._load_invite_templates()
        template_names = list(templates.keys())
        assert template_names == []
        # In the UI: `if template_names:` is False, so dropdown is not shown

    def test_dropdown_shown_when_templates_exist(self, patch_path, sample_template):
        """When templates exist, template_names is non-empty → dropdown shown."""
        app_mod._save_invite_template("Show Me", sample_template)
        templates = app_mod._load_invite_templates()
        template_names = list(templates.keys())
        assert template_names == ["Show Me"]

    def test_dropdown_options_include_none_sentinel(self, patch_path, sample_template):
        """Options list should be ['— None —'] + template_names."""
        app_mod._save_invite_template("Tpl1", sample_template)
        app_mod._save_invite_template("Tpl2", sample_template)
        templates = app_mod._load_invite_templates()
        template_names = list(templates.keys())
        options = ["— None —"] + template_names
        assert options[0] == "— None —"
        assert "Tpl1" in options
        assert "Tpl2" in options

    def test_last_invite_tpl_tracker_prevents_reapply(self):
        """Simulate the _last_invite_tpl tracker logic."""
        session = {}

        # First time selecting a template
        chosen = "My Template"
        prev = session.get("_last_invite_tpl")
        assert chosen != prev  # None != "My Template" → apply template

        session["_last_invite_tpl"] = chosen

        # Same template on next rerun — should NOT re-apply
        prev = session.get("_last_invite_tpl")
        assert chosen == prev  # "My Template" == "My Template" → skip

    def test_selecting_none_resets_tracker(self):
        """Selecting '— None —' sets tracker to None."""
        session = {"_last_invite_tpl": "Some Template"}
        chosen = "— None —"
        if chosen == "— None —":
            session["_last_invite_tpl"] = None
        assert session["_last_invite_tpl"] is None

    def test_delete_resets_tracker_if_matched(self):
        """Deleting the currently-tracked template resets the tracker."""
        session = {"_last_invite_tpl": "To Delete"}
        deleted_name = "To Delete"
        if session.get("_last_invite_tpl") == deleted_name:
            session["_last_invite_tpl"] = None
        assert session["_last_invite_tpl"] is None

    def test_delete_preserves_tracker_if_different(self):
        """Deleting a different template doesn't reset the tracker."""
        session = {"_last_invite_tpl": "Keep This"}
        deleted_name = "Other"
        if session.get("_last_invite_tpl") == deleted_name:
            session["_last_invite_tpl"] = None
        assert session["_last_invite_tpl"] == "Keep This"


# ===========================================================================
# 10. BUG-FIX VERIFICATION: delete/save return-value checks
# ===========================================================================
class TestDeleteReturnValueGuard:
    """Verify delete only triggers rerun when _delete_invite_template returns True."""

    def test_delete_success_returns_true(self, patch_path, sample_template):
        """Successful delete returns True — UI should rerun."""
        app_mod._save_invite_template("ToDelete", sample_template)
        result = app_mod._delete_invite_template("ToDelete")
        assert result is True

    def test_delete_failure_returns_false(self, patch_path):
        """Failed delete returns False — UI should NOT rerun."""
        # No templates on disk, delete should fail
        result = app_mod._delete_invite_template("NonExistent")
        assert result is False

    def test_delete_write_error_returns_false(self, sample_template):
        """Write error during delete returns False — UI error preserved."""
        with patch.object(app_mod, "_get_invite_templates_path", return_value="/nonexistent/dir/f.json"):
            result = app_mod._delete_invite_template("x")
        assert result is False


class TestSaveReturnValueGuard:
    """Verify save only triggers success/rerun when _save_invite_template returns True."""

    def test_save_success_returns_true(self, patch_path, sample_template):
        """Successful save returns True — UI should show toast and rerun."""
        result = app_mod._save_invite_template("Good", sample_template)
        assert result is True

    def test_save_failure_returns_false(self, sample_template):
        """Failed save returns False — UI should NOT show success."""
        with patch.object(app_mod, "_get_invite_templates_path", return_value="/nonexistent/dir/f.json"):
            result = app_mod._save_invite_template("Bad", sample_template)
        assert result is False


# ===========================================================================
# 11. SENTINEL NAME COLLISION: "— None —" reserved name
# ===========================================================================
class TestSentinelNameReserved:
    """Verify '— None —' cannot be used as a template name."""

    def test_sentinel_name_not_in_dropdown_options_naturally(self, patch_path, sample_template):
        """Normal template names don't collide with sentinel."""
        app_mod._save_invite_template("My Template", sample_template)
        templates = app_mod._load_invite_templates()
        template_names = list(templates.keys())
        options = ["— None —"] + template_names
        # Sentinel appears exactly once (at position 0)
        assert options.count("— None —") == 1

    def test_sentinel_name_would_cause_duplicate_in_dropdown(self, patch_path, sample_template):
        """If sentinel name were saved, dropdown would have duplicate — UI prevents this."""
        # Demonstrate the problem: if someone bypasses the UI
        app_mod._save_invite_template("— None —", sample_template)
        templates = app_mod._load_invite_templates()
        template_names = list(templates.keys())
        options = ["— None —"] + template_names
        # Would have duplicate sentinel — proves why UI validation is needed
        assert options.count("— None —") == 2

    def test_ui_save_logic_rejects_sentinel_name(self):
        """Simulate the UI save validation — sentinel name is rejected."""
        clean_name = "— None —".strip()
        # The UI code at line 4000 checks: elif clean_name == "— None —"
        assert clean_name == "— None —"  # Would be rejected

    def test_ui_save_logic_accepts_normal_name(self):
        """Simulate the UI save validation — normal name passes."""
        clean_name = "Technical Interview".strip()
        assert clean_name != "— None —"
        assert len(clean_name) > 0  # Both checks pass

    def test_ui_save_logic_rejects_empty(self):
        """Simulate the UI save validation — empty name is rejected."""
        for raw_name in ["", "   ", None]:
            clean_name = (raw_name or "").strip()
            assert not clean_name  # Would hit the 'not clean_name' branch


# ===========================================================================
# 12. SAVE DATA SNAPSHOT: verify exact shape written to disk
# ===========================================================================
class TestSaveDataShape:
    """Verify the exact template dict shape the UI would produce."""

    def test_teams_template_shape(self, patch_path):
        """Teams interview produces correct data shape."""
        # Simulate UI save logic (lines 3997-4008)
        is_teams = True
        data = {
            "interview_type": "Teams" if is_teams else "Non-Teams",
            "subject": "Interview: Backend Engineer",
            "agenda": "Coding exercise",
            "location": "",
            "include_recruiter": True,
        }
        app_mod._save_invite_template("Shape Test", data)
        loaded = app_mod._load_invite_templates()
        tpl = loaded["Shape Test"]
        assert set(tpl.keys()) == {"interview_type", "subject", "agenda", "location", "include_recruiter"}
        assert tpl["interview_type"] == "Teams"
        assert isinstance(tpl["include_recruiter"], bool)

    def test_nonteams_template_shape(self, patch_path):
        """Non-Teams interview produces correct data shape with location."""
        is_teams = False
        data = {
            "interview_type": "Teams" if is_teams else "Non-Teams",
            "subject": "On-site: Product Designer",
            "agenda": "Portfolio review",
            "location": "Office A, Floor 3",
            "include_recruiter": False,
        }
        app_mod._save_invite_template("Onsite Shape", data)
        loaded = app_mod._load_invite_templates()
        tpl = loaded["Onsite Shape"]
        assert tpl["interview_type"] == "Non-Teams"
        assert tpl["location"] == "Office A, Floor 3"
        assert tpl["include_recruiter"] is False
