"""Tests for pp-install pure helpers (no network, no real Claude config).

The pp-install script lives at scripts/pp-install.py (sibling of pp.py's
parent tree). It's loaded via importlib.util so the dashed filename is OK.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Locate pp-install.py. HERE = .../skill/scripts/tests/. Walk up to _data/
# and across to the sibling `scripts/` dir that holds the install script:
#   parents[0] = scripts/  (the skill's own scripts)
#   parents[1] = skill/
#   parents[2] = _data/     <- both `skill/` and `scripts/` (the install
#                              tooling) live here in v0.4
HERE = Path(__file__).resolve().parent
DATA_ROOT = HERE.parents[2]
INSTALL_PATH = DATA_ROOT / "scripts" / "pp-install.py"


def _load_install_module():
    spec = importlib.util.spec_from_file_location("pp_install", INSTALL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class GitDefaultTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()

    def test_returns_none_when_git_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(self.mod.git_default("user.name"))

    def test_returns_stripped_value(self):
        fake = mock.MagicMock(stdout="  alice  \n", returncode=0)
        with mock.patch("subprocess.run", return_value=fake):
            self.assertEqual(self.mod.git_default("user.name"), "alice")

    def test_returns_none_for_blank(self):
        fake = mock.MagicMock(stdout="", returncode=1)
        with mock.patch("subprocess.run", return_value=fake):
            self.assertIsNone(self.mod.git_default("user.name"))


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class MergeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        # Point SETTINGS_PATH at a temp file via monkey-patch.
        self.fake_path = Path(self.tmpdir.name) / "settings.local.json"
        self._orig = self.mod.SETTINGS_PATH
        self.mod.SETTINGS_PATH = self.fake_path

    def tearDown(self):
        self.mod.SETTINGS_PATH = self._orig
        self.tmpdir.cleanup()

    def test_creates_file_if_absent(self):
        self.mod.merge_settings({"FOO": "bar"}, backup=False)
        data = json.loads(self.fake_path.read_text())
        self.assertEqual(data, {"env": {"FOO": "bar"}})

    def test_preserves_unrelated_top_level_keys(self):
        self.fake_path.write_text(json.dumps({
            "permissions": {"allow": ["X"]},
            "env": {"OLD": "value"},
        }))
        self.mod.merge_settings({"NEW": "1"}, backup=False)
        data = json.loads(self.fake_path.read_text())
        self.assertEqual(data["permissions"], {"allow": ["X"]})
        self.assertEqual(data["env"]["OLD"], "value")
        self.assertEqual(data["env"]["NEW"], "1")

    def test_updates_existing_env_key(self):
        self.fake_path.write_text(json.dumps({"env": {"K": "old"}}))
        self.mod.merge_settings({"K": "new"}, backup=False)
        data = json.loads(self.fake_path.read_text())
        self.assertEqual(data["env"]["K"], "new")

    def test_backup_created_when_requested(self):
        self.fake_path.write_text('{"env": {"K": "v"}}')
        self.mod.merge_settings({"K": "v2"}, backup=True)
        bak = self.fake_path.with_suffix(".json.bak")
        self.assertTrue(bak.exists())
        self.assertEqual(json.loads(bak.read_text())["env"]["K"], "v")

    def test_tolerates_utf8_bom(self):
        # PowerShell 5.1's `Set-Content -Encoding utf8` writes a BOM. The
        # wizard MUST handle this -- earlier versions choked with
        # "Expecting value: line 1 column 1".
        with open(self.fake_path, "wb") as f:
            f.write(b"\xef\xbb\xbf" + b'{"env": {"K": "v"}}')
        self.mod.merge_settings({"K": "v2"}, backup=False)
        data = json.loads(self.fake_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(data["env"]["K"], "v2")

    def test_tolerates_empty_file(self):
        self.fake_path.write_text("")
        self.mod.merge_settings({"K": "v"}, backup=False)
        data = json.loads(self.fake_path.read_text())
        self.assertEqual(data, {"env": {"K": "v"}})

    def test_tolerates_whitespace_only_file(self):
        self.fake_path.write_text("   \n\n\t  \n")
        self.mod.merge_settings({"K": "v"}, backup=False)
        data = json.loads(self.fake_path.read_text())
        self.assertEqual(data, {"env": {"K": "v"}})

    def test_writes_without_bom(self):
        # Inverse of test_tolerates_utf8_bom: the wizard's own writes must
        # NOT include a BOM, or future reads (by python or any other tool)
        # would hit the same trap.
        self.mod.merge_settings({"K": "v"}, backup=False)
        first_bytes = self.fake_path.read_bytes()[:3]
        self.assertNotEqual(first_bytes, b"\xef\xbb\xbf",
                            "merge_settings wrote a UTF-8 BOM")


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class PromptTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()
        self._orig = self.mod.PromptCtx.non_interactive

    def tearDown(self):
        self.mod.PromptCtx.non_interactive = self._orig

    def test_non_interactive_returns_default(self):
        self.mod.PromptCtx.non_interactive = True
        self.assertEqual(self.mod.prompt("label", default="x"), "x")

    def test_non_interactive_dies_with_no_default(self):
        self.mod.PromptCtx.non_interactive = True
        with self.assertRaises(SystemExit):
            self.mod.prompt("label")

    def test_interactive_uses_default_on_empty(self):
        self.mod.PromptCtx.non_interactive = False
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(self.mod.prompt("label", default="x"), "x")

    def test_interactive_returns_input(self):
        self.mod.PromptCtx.non_interactive = False
        with mock.patch("builtins.input", return_value="alice"):
            self.assertEqual(self.mod.prompt("label", default="bob"), "alice")

    def test_choices_validation(self):
        self.mod.PromptCtx.non_interactive = False
        # First input bad, second input good.
        with mock.patch("builtins.input", side_effect=["maybe", "y"]):
            self.assertEqual(
                self.mod.prompt("ok?", choices=["y", "n"]),
                "y",
            )


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class RepoNameFromUrlTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()

    def test_https_no_suffix(self):
        self.assertEqual(self.mod.repo_name_from_url("https://github.com/org/repo"), "repo")

    def test_https_with_git_suffix(self):
        self.assertEqual(self.mod.repo_name_from_url("https://github.com/org/repo.git"), "repo")

    def test_ssh_scp_form(self):
        self.assertEqual(self.mod.repo_name_from_url("git@github.com:org/repo.git"), "repo")

    def test_ssh_url_form(self):
        self.assertEqual(self.mod.repo_name_from_url("ssh://git@host/path/to/repo.git"), "repo")

    def test_trailing_slash_stripped(self):
        self.assertEqual(self.mod.repo_name_from_url("https://github.com/org/repo/"), "repo")

    def test_hyphenated_name(self):
        self.assertEqual(self.mod.repo_name_from_url("https://github.com/walangstudio/pp-chat-test"), "pp-chat-test")

    def test_empty_returns_none(self):
        self.assertIsNone(self.mod.repo_name_from_url(""))
        self.assertIsNone(self.mod.repo_name_from_url(None))


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class ResolveTargetPathTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()
        self.default = str(Path.home() / "code" / "pair-pressure-chat")

    def test_empty_input_returns_default(self):
        self.assertEqual(self.mod.resolve_target_path("", self.default),
                         Path(self.default).expanduser().resolve())

    def test_absolute_path_passes_through(self):
        abs_path = str(Path.home().anchor + "tmp" + os.sep + "elsewhere") if os.name == "nt" else "/tmp/elsewhere"
        self.assertEqual(self.mod.resolve_target_path(abs_path, self.default),
                         Path(abs_path).resolve())

    def test_bare_name_uses_default_parent(self):
        result = self.mod.resolve_target_path("pp-chat-test", self.default)
        expected = (Path(self.default).expanduser().parent / "pp-chat-test").resolve()
        self.assertEqual(result, expected)

    def test_relative_with_separator_uses_home(self):
        # The whole point of this helper -- a 'code/foo' input should NOT
        # be resolved against cwd (which is usually the tooling repo when
        # the wizard runs). It should resolve against $HOME.
        result = self.mod.resolve_target_path("code/elsewhere", self.default)
        expected = (Path.home() / "code" / "elsewhere").resolve()
        self.assertEqual(result, expected)

    def test_tilde_expands(self):
        result = self.mod.resolve_target_path("~/elsewhere", self.default)
        self.assertEqual(result, (Path.home() / "elsewhere").resolve())

    def test_not_resolved_against_cwd(self):
        # If we ever resolved against cwd we'd land at <cwd>/foo. Confirm
        # the result is NOT <cwd>/foo for a bare name.
        result = self.mod.resolve_target_path("foo", self.default)
        cwd_relative = (Path.cwd() / "foo").resolve()
        # They MIGHT coincidentally be the same if cwd == default_dir's parent.
        # Cover both cases: result must be under default's parent regardless.
        self.assertEqual(result.parent, Path(self.default).expanduser().parent.resolve())


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class ChoiceHintRenderTests(unittest.TestCase):
    """The hint rendering follows the standard `[Y/n]` / `[y/N]` convention
    where the capital letter IS the default. Anything else is jarring."""

    def setUp(self):
        self.mod = _load_install_module()

    def test_yesno_default_yes(self):
        self.assertEqual(self.mod._render_choice_hint(["y", "n"], "y"), "Y/n")

    def test_yesno_default_no(self):
        self.assertEqual(self.mod._render_choice_hint(["y", "n"], "n"), "y/N")

    def test_yesno_no_default(self):
        # No default = no capitalization, just the choices.
        self.assertEqual(self.mod._render_choice_hint(["y", "n"], None), "y/n")

    def test_three_letter_choice_capitalizes_default(self):
        self.assertEqual(self.mod._render_choice_hint(["a", "b", "c"], "b"), "a/B/c")

    def test_numeric_choices_fall_back_to_default_annotation(self):
        # Numbers can't carry case, so we add "default: X" instead.
        self.assertEqual(
            self.mod._render_choice_hint(["1", "2", "3"], "2"),
            "1/2/3, default: 2",
        )

    def test_default_case_insensitive_match(self):
        # If user typed default="Y" with choices ["y","n"], still capitalize Y.
        self.assertEqual(self.mod._render_choice_hint(["y", "n"], "Y"), "Y/n")


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class MatchesChoiceTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()

    def test_single_letter_case_insensitive(self):
        self.assertTrue(self.mod._matches_choice("Y", ["y", "n"]))
        self.assertTrue(self.mod._matches_choice("y", ["y", "n"]))
        self.assertTrue(self.mod._matches_choice("N", ["y", "n"]))
        self.assertFalse(self.mod._matches_choice("maybe", ["y", "n"]))

    def test_multi_char_case_sensitive(self):
        self.assertTrue(self.mod._matches_choice("yes", ["yes", "no"]))
        self.assertFalse(self.mod._matches_choice("Yes", ["yes", "no"]))


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class ScaffoldDetectionTests(unittest.TestCase):
    """_is_scaffolded distinguishes a real pair-pressure chat repo from a
    bare clone of an empty remote."""

    def setUp(self):
        self.mod = _load_install_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.target = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_empty_dir_is_not_scaffolded(self):
        self.assertFalse(self.mod._is_scaffolded(self.target))

    def test_git_only_dir_is_not_scaffolded(self):
        # Simulates `git clone` of an empty remote: just a .git/ dir,
        # nothing else.
        (self.target / ".git").mkdir()
        self.assertFalse(self.mod._is_scaffolded(self.target))

    def test_proper_scaffold_is_recognized(self):
        (self.target / ".pair-pressure").mkdir()
        (self.target / ".pair-pressure" / "schema-version").write_text("1\n")
        self.assertTrue(self.mod._is_scaffolded(self.target))

    def test_schema_version_as_dir_does_not_count(self):
        # Edge case: schema-version must be a FILE, not a directory.
        (self.target / ".pair-pressure" / "schema-version").mkdir(parents=True)
        self.assertFalse(self.mod._is_scaffolded(self.target))


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class InstallSlashCommandsTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.user_commands = Path(self.tmpdir.name) / "commands" / "pp-chat"
        self._orig = self.mod.USER_COMMANDS_PATH
        self.mod.USER_COMMANDS_PATH = self.user_commands

    def tearDown(self):
        self.mod.USER_COMMANDS_PATH = self._orig
        self.tmpdir.cleanup()

    def test_copies_new_files(self):
        actions = self.mod.install_slash_commands()
        # 6 canonical files ship in v0.4.1 (consolidated from 15: send,
        # ai-reply, read, server, task, status). All should be 'new' on a
        # blank user-commands dir.
        self.assertEqual(actions["new"], 6)
        self.assertEqual(actions["updated"], 0)
        self.assertEqual(actions["kept"], 0)
        self.assertEqual(actions["unchanged"], 0)
        # Sanity-check one specific file landed.
        self.assertTrue((self.user_commands / "send.md").is_file())

    def test_skip_unchanged(self):
        # First install, then immediately re-run: everything should be
        # 'unchanged' (same checksum).
        self.mod.install_slash_commands()
        actions = self.mod.install_slash_commands()
        self.assertEqual(actions["unchanged"], 6)
        self.assertEqual(actions["new"], 0)

    def test_bin_name_rewrite(self):
        actions = self.mod.install_slash_commands(bin_name="pair-pp")
        body = (self.user_commands / "send.md").read_text()
        # 'pp' standalone should be rewritten; longer words containing
        # 'pp' should not (regex \bpp\b enforces word boundaries).
        self.assertIn("pair-pp", body)


if __name__ == "__main__":
    os.environ.setdefault("PAIR_PRESSURE_REPO", "/tmp/_pp_unused")
    os.environ.setdefault("PAIR_PRESSURE_AUTHOR", "test")
    unittest.main()
