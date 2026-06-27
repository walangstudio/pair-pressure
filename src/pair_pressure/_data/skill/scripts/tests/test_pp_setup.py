"""Tests for pp-setup pure helpers (no network, no real Claude config).

The pp-setup script lives at scripts/pp-setup.py (sibling of pp.py's
parent tree). It's loaded via importlib.util so the dashed filename is OK.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Locate pp-setup.py. HERE = .../skill/scripts/tests/. Walk up to _data/
# and across to the sibling `scripts/` dir that holds the setup script:
#   parents[0] = scripts/  (the skill's own scripts)
#   parents[1] = skill/
#   parents[2] = _data/     <- both `skill/` and `scripts/` (the setup
#                              tooling) live here
HERE = Path(__file__).resolve().parent
DATA_ROOT = HERE.parents[2]
INSTALL_PATH = DATA_ROOT / "scripts" / "pp-setup.py"


def _load_install_module():
    spec = importlib.util.spec_from_file_location("pp_setup", INSTALL_PATH)
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
class MajorVersionTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()

    def test_parses_major(self):
        self.assertEqual(self.mod._major("1.0.0"), 1)
        self.assertEqual(self.mod._major("0.9.1"), 0)
        self.assertEqual(self.mod._major("12.3.4"), 12)

    def test_garbage_is_minus_one(self):
        self.assertEqual(self.mod._major("nope"), -1)
        self.assertEqual(self.mod._major(""), -1)
        self.assertEqual(self.mod._major(None), -1)


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class MergeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        # Point SETTINGS_PATH at a temp file via monkey-patch.
        self.fake_path = Path(self.tmpdir.name) / "settings.local.json"
        self._orig = self.mod.SETTINGS_PATH
        self.mod.SETTINGS_PATH = self.fake_path
        self._orig_glob = self.mod.SETTINGS_GLOBAL_PATH
        self.mod.SETTINGS_GLOBAL_PATH = Path(self.tmpdir.name) / "settings.json"

    def tearDown(self):
        self.mod.SETTINGS_PATH = self._orig
        self.mod.SETTINGS_GLOBAL_PATH = self._orig_glob
        self.tmpdir.cleanup()

    def test_creates_file_if_absent(self):
        self.mod.merge_settings({"FOO": "bar"}, backup=False)
        data = json.loads(self.fake_path.read_text())
        self.assertEqual(data, {"env": {"FOO": "bar"}})

    def test_writes_both_settings_files(self):
        self.mod.merge_settings({"FOO": "bar"}, backup=False)
        data = json.loads(self.mod.SETTINGS_GLOBAL_PATH.read_text())
        self.assertEqual(data["env"]["FOO"], "bar")

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
class InstallSlashCommandsTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.user_commands = Path(self.tmpdir.name) / "commands" / "pp-chat"
        self._orig = self.mod.USER_COMMANDS_PATH
        self.mod.USER_COMMANDS_PATH = self.user_commands
        # The skill-version sentinel gates the major-bump behavior; point it
        # at the temp dir so the real ~/.claude is never consulted.
        self.user_skill = Path(self.tmpdir.name) / "skills" / "pair-pressure"
        self._orig_skill = self.mod.USER_SKILL_PATH
        self.mod.USER_SKILL_PATH = self.user_skill

    def tearDown(self):
        self.mod.USER_COMMANDS_PATH = self._orig
        self.mod.USER_SKILL_PATH = self._orig_skill
        self.tmpdir.cleanup()

    def _template_count(self):
        # Source of truth is the install module's own COMMAND_SOURCES.
        return sum(1 for p in self.mod.COMMAND_SOURCES.iterdir()
                   if p.name.endswith(".md"))

    def _set_installed_version(self, version):
        self.user_skill.mkdir(parents=True, exist_ok=True)
        (self.user_skill / ".pp-version").write_text(version + "\n")

    def test_copies_new_files(self):
        actions = self.mod.install_slash_commands()
        expected = self._template_count()
        # Canonical files ship under templates/commands/; count is the
        # source of truth, not a magic literal.
        self.assertEqual(actions["new"], expected)
        self.assertEqual(actions["updated"], 0)
        self.assertEqual(actions["kept"], 0)
        self.assertEqual(actions["unchanged"], 0)
        self.assertEqual(actions["removed"], 0)
        # Sanity-check the v1.0 set landed (and the dead v0.x ones can't).
        self.assertTrue((self.user_commands / "send.md").is_file())
        self.assertTrue((self.user_commands / "use.md").is_file())
        self.assertTrue((self.user_commands / "dm.md").is_file())
        self.assertFalse((self.user_commands / "peek.md").exists())
        self.assertFalse((self.user_commands / "repo.md").exists())

    def test_skip_unchanged(self):
        # First install, then immediately re-run: everything should be
        # 'unchanged' (same checksum).
        self.mod.install_slash_commands()
        actions = self.mod.install_slash_commands()
        expected = self._template_count()
        self.assertEqual(actions["unchanged"], expected)
        self.assertEqual(actions["new"], 0)

    def test_bin_name_rewrite(self):
        self.mod.install_slash_commands(bin_name="pair-pp")
        body = (self.user_commands / "send.md").read_text()
        # 'pp' standalone should be rewritten; longer words containing
        # 'pp' should not (regex \bpp\b enforces word boundaries).
        self.assertIn("pair-pp", body)

    def test_major_bump_removes_stale_and_overwrites(self):
        # Simulate a v0.x install: stale commands + a customized current one.
        self._set_installed_version("0.9.1")
        self.user_commands.mkdir(parents=True, exist_ok=True)
        (self.user_commands / "peek.md").write_text("dead verb dispatch")
        (self.user_commands / "repo.md").write_text("dead verb dispatch")
        (self.user_commands / "send.md").write_text("customized v0.9 send")
        actions = self.mod.install_slash_commands()
        self.assertEqual(actions["removed"], 2)
        self.assertFalse((self.user_commands / "peek.md").exists())
        self.assertFalse((self.user_commands / "repo.md").exists())
        # Customized send.md was force-overwritten, no prompt.
        self.assertEqual(actions["kept"], 0)
        self.assertNotIn("customized v0.9 send",
                         (self.user_commands / "send.md").read_text())

    def test_same_major_keeps_customized_without_force(self):
        self._set_installed_version(self.mod.__version__)
        self.mod.install_slash_commands()
        (self.user_commands / "send.md").write_text("my customized version")
        self.mod.PromptCtx.non_interactive = True
        try:
            actions = self.mod.install_slash_commands()
        finally:
            self.mod.PromptCtx.non_interactive = False
        self.assertEqual(actions["kept"], 1)
        self.assertEqual((self.user_commands / "send.md").read_text(),
                         "my customized version")


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class InstalledSkillVersionTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        self._orig = self.mod.USER_SKILL_PATH
        self.mod.USER_SKILL_PATH = Path(self.tmpdir.name) / "pair-pressure"

    def tearDown(self):
        self.mod.USER_SKILL_PATH = self._orig
        self.tmpdir.cleanup()

    def test_absent_is_empty(self):
        self.assertEqual(self.mod.installed_skill_version(), "")

    def test_reads_sentinel(self):
        self.mod.USER_SKILL_PATH.mkdir(parents=True)
        (self.mod.USER_SKILL_PATH / ".pp-version").write_text("0.9.1\n")
        self.assertEqual(self.mod.installed_skill_version(), "0.9.1")


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class VerifyTests(unittest.TestCase):
    """verify() runs `pp status` and reports verdict + where."""

    def setUp(self):
        self.mod = _load_install_module()

    def _fake_proc(self, *, stdout="", stderr="", returncode=0):
        return mock.MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)

    def _run_with(self, fake):
        with mock.patch.object(self.mod.shutil, "which", return_value="/fake/pp"):
            with mock.patch.object(self.mod.subprocess, "run", return_value=fake):
                return self.mod.verify("alice")

    def test_ready_verdict_is_ok(self):
        fake = self._fake_proc(stdout=json.dumps({
            "verdict": "ready", "where": "team #general", "message": "Ready.",
        }))
        status, msg = self._run_with(fake)
        self.assertEqual(status, "ok")
        self.assertIn("ready", msg)
        self.assertIn("team #general", msg)

    def test_needs_restart_is_ok(self):
        fake = self._fake_proc(stdout=json.dumps({
            "verdict": "needs_restart", "where": None, "message": "restart",
        }))
        status, _msg = self._run_with(fake)
        self.assertEqual(status, "ok")

    def test_other_verdict_passes_through(self):
        fake = self._fake_proc(stdout=json.dumps({
            "verdict": "needs_server", "where": None, "message": "add one",
        }))
        status, msg = self._run_with(fake)
        self.assertEqual(status, "needs_server")
        self.assertIn("(no server)", msg)

    def test_pp_not_on_path_returns_skip(self):
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            status, _msg = self.mod.verify("alice")
        self.assertEqual(status, "skip")

    def test_nonzero_exit_returns_fail(self):
        fake = self._fake_proc(stderr="boom", returncode=2)
        status, msg = self._run_with(fake)
        self.assertEqual(status, "fail")
        self.assertIn("boom", msg)

    def test_non_json_output_returns_fail(self):
        fake = self._fake_proc(stdout="not json")
        status, msg = self._run_with(fake)
        self.assertEqual(status, "fail")
        self.assertIn("did not return JSON", msg)

    def test_author_passed_through_env(self):
        seen = {}

        def fake_run(cmd, env, capture_output, text):
            seen["cmd"] = cmd
            seen["author"] = env.get("PAIR_PRESSURE_AUTHOR")
            return mock.MagicMock(
                stdout=json.dumps({"verdict": "ready", "where": "a #b",
                                   "message": ""}),
                stderr="", returncode=0)

        with mock.patch.object(self.mod.shutil, "which", return_value="/fake/pp"):
            with mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run):
                self.mod.verify("alice")

        self.assertIn("status", seen["cmd"])
        self.assertEqual(seen["author"], "alice")


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class McpClientConfigTests(unittest.TestCase):
    """MCP snippet generation: correct shape per client, written under
    ~/.pair-pressure/mcp/, idempotent. v1.0: env carries identity only —
    servers come from the registry, so no PAIR_PRESSURE_REPO."""

    def setUp(self):
        self.mod = _load_install_module()
        self._home = tempfile.TemporaryDirectory()
        self._orig = self.mod.PP_HOME
        self.mod.PP_HOME = Path(self._home.name) / ".pair-pressure"

    def tearDown(self):
        self.mod.PP_HOME = self._orig
        self._home.cleanup()

    def test_mcpservers_shape(self):
        path, dest = self.mod.write_mcp_client_config("cursor", "alice")
        data = json.loads(path.read_text(encoding="utf-8"))
        srv = data["mcpServers"]["pair-pressure"]
        self.assertEqual(srv["command"], "pair-pressure-mcp")
        self.assertEqual(srv["env"], {"PAIR_PRESSURE_AUTHOR": "alice"})
        self.assertIn(".cursor", dest)

    def test_opencode_shape(self):
        path, _ = self.mod.write_mcp_client_config(
            "opencode", "alice", alias="Echo")
        data = json.loads(path.read_text(encoding="utf-8"))
        srv = data["mcp"]["pair-pressure"]
        self.assertEqual(srv["type"], "local")
        self.assertEqual(srv["command"], ["pair-pressure-mcp"])
        self.assertEqual(srv["environment"]["PAIR_PRESSURE_ALIAS"], "Echo")
        self.assertNotIn("PAIR_PRESSURE_REPO", srv["environment"])

    def test_codex_toml_shape(self):
        path, _ = self.mod.write_mcp_client_config("codex", "alice")
        text = path.read_text(encoding="utf-8")
        self.assertIn("[mcp_servers.pair-pressure]", text)
        self.assertIn('command = "pair-pressure-mcp"', text)
        self.assertIn('PAIR_PRESSURE_AUTHOR = "alice"', text)

    def test_codex_toml_escapes_backslashes(self):
        # Backslashes in a TOML basic string must be escaped or the file is
        # invalid TOML (\c is an illegal escape, \t parses as a tab).
        snip = self.mod._mcp_snippet(
            "toml", {"PAIR_PRESSURE_AUTHOR": r"dom\alice"})
        self.assertIn(r'PAIR_PRESSURE_AUTHOR = "dom\\alice"', snip)

    def test_idempotent_overwrite(self):
        p1, _ = self.mod.write_mcp_client_config("cline", "alice")
        first = p1.read_text(encoding="utf-8")
        p2, _ = self.mod.write_mcp_client_config("cline", "alice")
        self.assertEqual(p1, p2)
        self.assertEqual(first, p2.read_text(encoding="utf-8"))

    def test_written_under_pp_home_mcp(self):
        path, _ = self.mod.write_mcp_client_config("kilo", "alice")
        self.assertEqual(path.parent, self.mod.PP_HOME / "mcp")

    def test_every_client_has_agents_destination(self):
        for client, entry in self.mod.MCP_CLIENTS.items():
            self.assertEqual(len(entry), 4, client)
            self.assertTrue(entry[3], client)


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class AgentsSnippetTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()
        self._home = tempfile.TemporaryDirectory()
        self._orig = self.mod.PP_HOME
        self.mod.PP_HOME = Path(self._home.name) / ".pair-pressure"

    def tearDown(self):
        self.mod.PP_HOME = self._orig
        self._home.cleanup()

    def test_writes_snippet_copy(self):
        out = self.mod.write_agents_snippet()
        self.assertIsNotNone(out)
        self.assertEqual(out.parent, self.mod.PP_HOME)
        text = out.read_text(encoding="utf-8")
        self.assertIn("untrusted-content", text)
        self.assertIn("not encrypted", text)

    def test_missing_source_returns_none(self):
        orig = self.mod.AGENTS_SNIPPET_SOURCE
        self.mod.AGENTS_SNIPPET_SOURCE = Path(self._home.name) / "nope.md"
        try:
            self.assertIsNone(self.mod.write_agents_snippet())
        finally:
            self.mod.AGENTS_SNIPPET_SOURCE = orig


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class PickClientsTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()
        self._orig_ni = self.mod.PromptCtx.non_interactive

    def tearDown(self):
        self.mod.PromptCtx.non_interactive = self._orig_ni

    def _args(self, clients=None, mcp_client=None):
        return mock.MagicMock(clients=clients, mcp_client=mcp_client)

    def test_explicit_clients_flag(self):
        got = self.mod.pick_clients(self._args(clients="codex, opencode"))
        self.assertEqual(got, ["codex", "opencode"])

    def test_unknown_client_dies(self):
        with self.assertRaises(SystemExit):
            self.mod.pick_clients(self._args(clients="emacs"))

    def test_legacy_mcp_client_includes_claude_when_detected(self):
        with mock.patch.object(self.mod, "claude_code_detected",
                               return_value=True):
            got = self.mod.pick_clients(self._args(mcp_client=["codex"]))
        self.assertEqual(got, ["claude", "codex"])

    def test_legacy_mcp_client_without_claude(self):
        with mock.patch.object(self.mod, "claude_code_detected",
                               return_value=False):
            got = self.mod.pick_clients(self._args(mcp_client=["kilo"]))
        self.assertEqual(got, ["kilo"])

    def test_non_interactive_defaults_to_claude_when_detected(self):
        self.mod.PromptCtx.non_interactive = True
        with mock.patch.object(self.mod, "claude_code_detected",
                               return_value=True):
            self.assertEqual(self.mod.pick_clients(self._args()), ["claude"])

    def test_non_interactive_defaults_to_none_without_claude(self):
        self.mod.PromptCtx.non_interactive = True
        with mock.patch.object(self.mod, "claude_code_detected",
                               return_value=False):
            self.assertEqual(self.mod.pick_clients(self._args()), [])

    def test_interactive_prompt_filters_unknown(self):
        self.mod.PromptCtx.non_interactive = False
        with mock.patch.object(self.mod, "claude_code_detected",
                               return_value=True):
            with mock.patch("builtins.input",
                            return_value="claude, emacs, codex"):
                got = self.mod.pick_clients(self._args())
        self.assertEqual(got, ["claude", "codex"])


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class RegisteredServersTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()
        self._home = tempfile.TemporaryDirectory()
        self._orig = self.mod.PP_HOME
        self.mod.PP_HOME = Path(self._home.name) / ".pair-pressure"

    def tearDown(self):
        self.mod.PP_HOME = self._orig
        self._home.cleanup()

    def test_no_registry_is_empty(self):
        self.assertEqual(self.mod._registered_servers(), [])

    def test_reads_names(self):
        self.mod.PP_HOME.mkdir(parents=True)
        (self.mod.PP_HOME / "servers.json").write_text(json.dumps({
            "schema_version": 2,
            "servers": [{"name": "team"}, {"name": "oss"}],
            "default": "team",
        }))
        self.assertEqual(self.mod._registered_servers(), ["team", "oss"])

    def test_garbage_registry_is_empty(self):
        self.mod.PP_HOME.mkdir(parents=True)
        (self.mod.PP_HOME / "servers.json").write_text("{nope")
        self.assertEqual(self.mod._registered_servers(), [])


@unittest.skipUnless(INSTALL_PATH.exists(), f"missing {INSTALL_PATH}")
class SetupServerTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_install_module()
        self._home = tempfile.TemporaryDirectory()
        self._orig = self.mod.PP_HOME
        self.mod.PP_HOME = Path(self._home.name) / ".pair-pressure"
        self._orig_ni = self.mod.PromptCtx.non_interactive

    def tearDown(self):
        self.mod.PP_HOME = self._orig
        self.mod.PromptCtx.non_interactive = self._orig_ni
        self._home.cleanup()

    def _args(self, server=None, remote=None, path=None):
        return mock.MagicMock(server=server, remote=remote, path=path)

    def test_non_interactive_without_flags_skips(self):
        self.mod.PromptCtx.non_interactive = True
        self.assertIsNone(self.mod.setup_server(self._args(), "alice"))

    def test_existing_registry_skips_without_flags(self):
        self.mod.PP_HOME.mkdir(parents=True)
        (self.mod.PP_HOME / "servers.json").write_text(json.dumps({
            "servers": [{"name": "team"}], "default": "team",
        }))
        self.mod.PromptCtx.non_interactive = True
        self.assertIsNone(self.mod.setup_server(self._args(), "alice"))

    def test_bad_server_name_dies(self):
        with self.assertRaises(SystemExit):
            self.mod.setup_server(
                self._args(server="Bad Name!", remote="git@x:y/z.git"),
                "alice")

    def test_flags_drive_pp_server_add(self):
        seen = {}

        def fake_add(name, url, author, path=None):
            seen.update(name=name, url=url, author=author, path=path)
            return (True, "registered")

        with mock.patch.object(self.mod, "_pp_server_add",
                               side_effect=fake_add):
            got = self.mod.setup_server(
                self._args(server="team", remote="git@x:y/z.git"), "alice")
        self.assertEqual(got, "team")
        self.assertEqual(seen["url"], "git@x:y/z.git")
        self.assertEqual(seen["author"], "alice")

    def test_failed_add_returns_none(self):
        with mock.patch.object(self.mod, "_pp_server_add",
                               return_value=(False, "boom")):
            got = self.mod.setup_server(
                self._args(server="team", remote="git@x:y/z.git"), "alice")
        self.assertIsNone(got)


if __name__ == "__main__":
    os.environ.setdefault("PAIR_PRESSURE_AUTHOR", "test")
    unittest.main()
