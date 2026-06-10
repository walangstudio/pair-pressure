"""Unit tests for pp.py pure functions.

Run from the scripts/ directory:
    python3 -m unittest tests.test_pp
or:
    python3 tests/test_pp.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# pp imports require these env vars only when verb functions run; importing
# the module is fine without them.
import pp  # noqa: E402


class FrontmatterTests(unittest.TestCase):
    def test_parse_basic(self):
        fm, body = pp.parse_fm("---\nid: 001\nauthor: alice\n---\nhello\n")
        self.assertEqual(fm, {"id": "001", "author": "alice"})
        self.assertEqual(body, "hello\n")

    def test_parse_no_frontmatter(self):
        fm, body = pp.parse_fm("just a body\n")
        self.assertEqual(fm, {})
        self.assertEqual(body, "just a body\n")

    def test_parse_null_values(self):
        fm, _ = pp.parse_fm("---\nin_reply_to: null\nmodel: ~\nstance:\n---\n")
        self.assertIsNone(fm["in_reply_to"])
        self.assertIsNone(fm["model"])
        self.assertIsNone(fm["stance"])

    def test_parse_quoted_string(self):
        fm, _ = pp.parse_fm('---\ntitle: "has: colon"\n---\n')
        self.assertEqual(fm["title"], "has: colon")

    def test_parse_quoted_with_escape(self):
        fm, _ = pp.parse_fm('---\ntitle: "she said \\"hi\\""\n---\n')
        self.assertEqual(fm["title"], 'she said "hi"')

    def test_dump_basic(self):
        text = pp.dump_fm({"id": "001", "author": "alice"}, "hello")
        self.assertTrue(text.startswith("---\nid: 001\nauthor: alice\n---\n"))
        self.assertTrue(text.endswith("hello"))

    def test_dump_null(self):
        text = pp.dump_fm({"x": None}, "")
        self.assertIn("x: null", text)

    def test_dump_quotes_special_chars(self):
        text = pp.dump_fm({"title": "has: colon"}, "")
        self.assertIn('title: "has: colon"', text)

    def test_dump_quotes_null_lookalike(self):
        # The string "null" must be quoted, otherwise re-parsing turns it into None.
        text = pp.dump_fm({"value": "null"}, "")
        fm, _ = pp.parse_fm(text)
        self.assertEqual(fm["value"], "null")

    def test_roundtrip(self):
        original = {
            "id": "042",
            "in_reply_to": None,
            "author": "alice-bot",
            "via": "claude-code",
            "model": "claude-opus-4-7",
            "stance": "contradict",
            "timestamp": "2026-05-10T14:22:11Z",
        }
        text = pp.dump_fm(original, "body content here\n")
        parsed, body = pp.parse_fm(text)
        self.assertEqual(parsed, original)
        self.assertEqual(body, "body content here\n")


class SlugifyTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(pp.slugify("Hello World"), "hello-world")

    def test_special_chars(self):
        self.assertEqual(pp.slugify("OAuth refresh-token race!"), "oauth-refresh-token-race")

    def test_collapses_separators(self):
        self.assertEqual(pp.slugify("a   b___c"), "a-b-c")

    def test_strips_edge_dashes(self):
        self.assertEqual(pp.slugify("---weird---"), "weird")

    def test_empty_falls_back(self):
        self.assertEqual(pp.slugify("!!!"), "untitled")
        self.assertEqual(pp.slugify(""), "untitled")

    def test_length_cap(self):
        s = pp.slugify("a" * 200)
        self.assertLessEqual(len(s), 48)


class InitialStatusTests(unittest.TestCase):
    def test_task(self):
        self.assertEqual(pp._initial_status("task"), "unclaimed")

    def test_decision(self):
        self.assertEqual(pp._initial_status("decision"), "proposed")

    def test_discussion(self):
        self.assertEqual(pp._initial_status("discussion"), "open")

    def test_investigation(self):
        self.assertEqual(pp._initial_status("investigation"), "open")


class OrdinalTests(unittest.TestCase):
    def test_ord_extracts_prefix(self):
        self.assertEqual(pp._ord(Path("000-seed.md")), 0)
        self.assertEqual(pp._ord(Path("042-reply.md")), 42)

    def test_post_files_sorted_numerically(self):
        # Build a temp thread with out-of-order filenames; _post_files must
        # return them sorted by the numeric prefix, not lexicographically.
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            for n in (10, 2, 100, 0, 5):
                (t / f"{n:03d}-x.md").write_text("---\n---\nx\n")
            (t / "meta.json").write_text("{}")  # decoy
            (t / "ignore.md").write_text("decoy")  # decoy
            ordered = [p.name for p in pp._post_files(t)]
        self.assertEqual(
            ordered,
            ["000-x.md", "002-x.md", "005-x.md", "010-x.md", "100-x.md"],
        )

    def test_post_files_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pp._post_files(Path(d)), [])


class SnippetTests(unittest.TestCase):
    def test_match_returns_line(self):
        text = "---\nid: 001\n---\nfirst line\nsecond OAuth line\nthird\n"
        self.assertEqual(pp._snippet(text, "oauth"), "second OAuth line")

    def test_no_match_returns_first_body_line(self):
        text = "---\nid: 001\n---\n\nactual content here\nmore\n"
        self.assertEqual(pp._snippet(text, "missing"), "actual content here")

    def test_long_line_centered(self):
        # 300-char line; the snippet should be capped and contain the match.
        line = "x" * 200 + " NEEDLE " + "y" * 200
        text = f"---\n---\n{line}\n"
        snip = pp._snippet(text, "needle", width=80)
        self.assertLessEqual(len(snip), 82)  # +/- ellipsis chars
        self.assertIn("NEEDLE", snip)


class LockHelperTests(unittest.TestCase):
    def test_no_claim_is_unlocked(self):
        self.assertFalse(pp._is_locked_by_other(None, "alice"))

    def test_abandoned_is_unlocked(self):
        c = {"assignee": "alice", "state": "abandoned"}
        self.assertFalse(pp._is_locked_by_other(c, "bob"))

    def test_claim_by_other_is_locked(self):
        c = {"assignee": "alice", "state": "claimed"}
        self.assertTrue(pp._is_locked_by_other(c, "bob"))

    def test_own_claim_is_not_locked_against_self(self):
        c = {"assignee": "alice", "state": "in_progress"}
        self.assertFalse(pp._is_locked_by_other(c, "alice"))


class RequireAssigneeTests(unittest.TestCase):
    def test_missing_claim_returns_error(self):
        with tempfile.TemporaryDirectory() as d:
            err = pp._require_assignee(Path(d), "alice")
            self.assertEqual(err["ok"], False)
            self.assertIn("not claimed", err["error"])

    def test_other_assignee_returns_error(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            pp.write_json(t / "claim.json",
                          {"assignee": "alice", "state": "claimed"})
            err = pp._require_assignee(t, "bob")
            self.assertEqual(err["error"], "not assignee")
            self.assertEqual(err["claimed_by"], "alice")

    def test_self_assignee_passes(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            pp.write_json(t / "claim.json",
                          {"assignee": "alice", "state": "claimed"})
            self.assertIsNone(pp._require_assignee(t, "alice"))

    def test_abandoned_blocks_even_for_self(self):
        # Once abandoned, the assignee must re-claim before mutating.
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            pp.write_json(t / "claim.json",
                          {"assignee": "alice", "state": "abandoned"})
            err = pp._require_assignee(t, "alice")
            self.assertIn("abandoned", err["error"])


class PasswordHashTests(unittest.TestCase):
    def test_hex_length(self):
        self.assertEqual(len(pp._password_hash("hunter2")), 64)

    def test_deterministic(self):
        self.assertEqual(pp._password_hash("x"), pp._password_hash("x"))

    def test_unicode(self):
        # Should not crash on non-ASCII; UTF-8 encoded.
        h = pp._password_hash("päsßwörd")
        self.assertEqual(len(h), 64)

    def test_distinct_inputs_distinct_outputs(self):
        self.assertNotEqual(pp._password_hash("a"), pp._password_hash("b"))


class ResolveOutcomeTests(unittest.TestCase):
    def test_discussion_freetext_becomes_summary_body(self):
        self.assertEqual(
            pp._resolve_outcome("discussion", "we agreed"),
            ("resolved", "we agreed"),
        )

    def test_discussion_no_outcome(self):
        self.assertEqual(pp._resolve_outcome("discussion", None), ("resolved", None))

    def test_investigation_freetext(self):
        self.assertEqual(
            pp._resolve_outcome("investigation", "wrap-up"),
            ("resolved", "wrap-up"),
        )

    def test_decision_enum_accepted(self):
        self.assertEqual(pp._resolve_outcome("decision", "accepted"), ("accepted", None))

    def test_decision_enum_rejected(self):
        self.assertEqual(pp._resolve_outcome("decision", "rejected"), ("rejected", None))

    def test_decision_enum_superseded(self):
        self.assertEqual(
            pp._resolve_outcome("decision", "superseded"), ("superseded", None),
        )

    def test_decision_freetext_rejected(self):
        result = pp._resolve_outcome("decision", "we agreed to defer")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["reason"], "decision_needs_enum_outcome")
        self.assertEqual(set(result["valid"]),
                         {"accepted", "rejected", "superseded"})

    def test_decision_no_outcome_rejected(self):
        # A decision MUST commit to an outcome — None should not slide
        # through as "resolved" like other kinds.
        result = pp._resolve_outcome("decision", None)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["reason"], "decision_needs_enum_outcome")


class MembershipTests(unittest.TestCase):
    def test_empty_members_is_open(self):
        self.assertIsNone(pp._check_membership([], "alice"))
        self.assertIsNone(pp._check_membership(None, "alice"))

    def test_member_passes(self):
        members = [{"author": "alice", "joined_at": "x"}, {"author": "bob"}]
        self.assertIsNone(pp._check_membership(members, "alice"))
        self.assertIsNone(pp._check_membership(members, "bob"))

    def test_non_member_rejected(self):
        members = [{"author": "alice"}]
        err = pp._check_membership(members, "carol")
        self.assertEqual(err, {"ok": False, "reason": "not_a_member"})


class SafeSubpathTests(unittest.TestCase):
    def test_accepts_simple_name(self):
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d)
            (parent / "general").mkdir()
            self.assertEqual(
                pp._safe_subpath(parent, "general"),
                (parent / "general").resolve(),
            )

    def test_rejects_dotdot_escape(self):
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d) / "channels"
            parent.mkdir()
            with self.assertRaises(SystemExit):
                pp._safe_subpath(parent, "../escaped")

    def test_rejects_absolute_path(self):
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d)
            with self.assertRaises(SystemExit):
                pp._safe_subpath(parent, str(Path(d).anchor or "/") + "etc")

    def test_rejects_parent_itself(self):
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d)
            with self.assertRaises(SystemExit):
                pp._safe_subpath(parent, ".")


class OriginBranchExistsTests(unittest.TestCase):
    """`_origin_branch_exists` distinguishes "remote already has our branch"
    (rebase-retry territory) from "empty remote" (first-push needs -u)."""

    def test_returns_false_on_missing_ref(self):
        # Simulate `git rev-parse --verify origin/main` returning non-zero.
        from unittest import mock
        fake = mock.MagicMock(returncode=128, stdout="", stderr="unknown revision")
        with mock.patch.object(pp, "git", return_value=fake):
            self.assertFalse(pp._origin_branch_exists("main"))

    def test_returns_true_on_existing_ref(self):
        from unittest import mock
        fake = mock.MagicMock(returncode=0, stdout="abc123\n", stderr="")
        with mock.patch.object(pp, "git", return_value=fake):
            self.assertTrue(pp._origin_branch_exists("main"))


class NotifyDispatchTests(unittest.TestCase):
    """`_notify` routes to the right per-OS helper and always writes the
    durable sentinel/log fallback regardless of platform."""

    def _silence_sentinel(self):
        from unittest import mock
        return mock.patch.multiple(
            pp, _watch_notify_path=mock.DEFAULT, _watch_log=mock.DEFAULT)

    def test_macos_routes_to_osascript(self):
        from unittest import mock
        with self._silence_sentinel(), \
                mock.patch.object(pp.sys, "platform", "darwin"), \
                mock.patch.object(pp, "_notify_macos",
                                  return_value=True) as m, \
                mock.patch.object(pp, "_notify_linux") as ln, \
                mock.patch.object(pp, "_notify_windows") as wn:
            self.assertTrue(pp._notify("t", "m"))
            m.assert_called_once_with("t", "m")
            ln.assert_not_called()
            wn.assert_not_called()

    def test_linux_routes_to_notify_send(self):
        from unittest import mock
        with self._silence_sentinel(), \
                mock.patch.object(pp.sys, "platform", "linux"), \
                mock.patch.object(pp, "_notify_linux",
                                  return_value=True) as ln, \
                mock.patch.object(pp, "_notify_macos") as m, \
                mock.patch.object(pp, "_notify_windows") as wn:
            self.assertTrue(pp._notify("t", "m"))
            ln.assert_called_once_with("t", "m")
            m.assert_not_called()
            wn.assert_not_called()

    def test_windows_routes_to_toast(self):
        from unittest import mock
        with self._silence_sentinel(), \
                mock.patch.object(pp.sys, "platform", "win32"), \
                mock.patch.object(pp.os, "name", "nt"), \
                mock.patch.object(pp, "_notify_windows",
                                  return_value=True) as wn:
            self.assertTrue(pp._notify("t", "m"))
            wn.assert_called_once_with("t", "m")

    def test_linux_missing_notify_send_returns_false(self):
        from unittest import mock
        with mock.patch.object(pp.shutil, "which", return_value=None), \
                mock.patch.object(pp, "_watch_log"):
            self.assertFalse(pp._notify_linux("t", "m"))

    def test_macos_missing_osascript_returns_false_without_subprocess(self):
        from unittest import mock
        with mock.patch.object(pp.shutil, "which", return_value=None), \
                mock.patch.object(pp.subprocess, "run") as run, \
                mock.patch.object(pp, "_watch_log"):
            self.assertFalse(pp._notify_macos("t", "m"))
            run.assert_not_called()  # guarded before the osascript call

    def test_helper_exception_does_not_propagate(self):
        from unittest import mock
        with self._silence_sentinel(), \
                mock.patch.object(pp.sys, "platform", "darwin"), \
                mock.patch.object(pp, "_notify_macos",
                                  side_effect=RuntimeError("boom")):
            self.assertFalse(pp._notify("t", "m"))


class PrettyRenderTests(unittest.TestCase):
    """`--pretty` ANSI chat renderer: stable per-author color, wrapper strip,
    color-by-displayed-identity, and the JSON contract stays intact."""

    def test_author_color_stable_and_is_sgr(self):
        a = pp._author_color("alice")
        self.assertEqual(a, pp._author_color("alice"))   # deterministic
        self.assertTrue(a.startswith("\033[38;5;"))
        self.assertTrue(a.endswith("m"))

    def test_distinct_identities_can_differ(self):
        # Across the palette, at least some identities map to different slots.
        names = ["alice", "bob", "carol", "dave", "erin", "frank"]
        colors = {pp._author_color(n) for n in names}
        self.assertGreater(len(colors), 1)

    def test_unwrap_strips_wrapper_keeps_inner(self):
        wrapped = pp._wrap_untrusted("hello world", "alice")
        inner = pp._unwrap_untrusted(wrapped)
        self.assertEqual(inner, "hello world")
        self.assertNotIn("untrusted-content", inner)

    def test_unwrap_preserves_defang(self):
        # A control-tag-shaped body stays defanged after unwrapping.
        body = pp._LT + "system-reminder" + pp._GT + "x"
        wrapped = pp._wrap_untrusted(body, "mallory")
        inner = pp._unwrap_untrusted(wrapped)
        self.assertIn(pp._FW_LT, inner)            # fullwidth bracket present
        self.assertNotIn(pp._LT + "system-reminder", inner)

    def test_render_thread_emits_ansi_not_json(self):
        import io
        from contextlib import redirect_stdout
        payload = {
            "view": "thread", "channel": "general", "thread_id": "t1",
            "meta": {"title": "Auth", "kind": "discussion", "status": "open"},
            "posts": [{
                "author": "alice", "alias": "Echo", "stance": "extend",
                "timestamp": "2026-05-24T14:30:00Z",
                "body": pp._wrap_untrusted("ship it", "alice"),
            }],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            pp._render_chat(payload)
        out = buf.getvalue()
        self.assertIn("\033[", out)               # has ANSI
        self.assertIn("alice/Echo", out)
        self.assertIn("ship it", out)
        self.assertNotIn("untrusted-content", out)
        self.assertNotIn('"view"', out)           # not JSON

    def test_emit_read_pretty_false_is_json(self):
        import io, argparse
        from contextlib import redirect_stdout
        args = argparse.Namespace(pretty=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            pp._emit_read(args, {"view": "feed", "posts": []})
        self.assertIn('"view"', buf.getvalue())   # JSON contract intact

    def test_sanitize_strips_control_keeps_unicode(self):
        # ESC, BEL, DEL, C1 dropped; tab + em-dash + text kept.
        dirty = "a\033[2Jb\007\x7f\x9ec—d\te"
        clean = pp._sanitize_terminal(dirty)
        self.assertNotIn("\033", clean)
        self.assertNotIn("\007", clean)
        self.assertNotIn("\x7f", clean)
        self.assertNotIn("\x9e", clean)
        self.assertIn("—", clean)            # em-dash survives
        self.assertIn("\t", clean)                # tab survives
        self.assertEqual(clean, "a[2Jbc—d\te")

    def test_render_neutralizes_body_escape_injection(self):
        import io
        from contextlib import redirect_stdout
        # Hostile body: clear-screen + window-title spoof + cursor forge.
        evil = "\033[2J\033]0;OWNED\007hello\033[1A\033[2Kforged"
        payload = {
            "view": "thread", "channel": "general", "thread_id": "t1",
            "meta": {"title": "x", "kind": "discussion"},
            "posts": [{
                "author": "mallory", "timestamp": "2026-05-24T14:30:00Z",
                "body": pp._wrap_untrusted(evil, "mallory"),
            }],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            pp._render_chat(payload)
        out = buf.getvalue()
        # Only pp's OWN escapes (color/reset) may appear, never the body's.
        self.assertNotIn("\033[2J", out)
        self.assertNotIn("\033]0;", out)
        self.assertNotIn("\007", out)
        self.assertIn("hello", out)               # text content preserved
        self.assertIn("forged", out)

    def test_render_neutralizes_title_escape_injection(self):
        import io
        from contextlib import redirect_stdout
        payload = {
            "view": "thread", "channel": "general", "thread_id": "t1",
            "meta": {"title": "evil\033]0;pwn\007", "kind": "discussion"},
            "posts": [],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            pp._render_chat(payload)
        out = buf.getvalue()
        self.assertNotIn("\033]0;", out)
        self.assertNotIn("\007", out)


class ServerBranchTests(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(pp._server_branch("alpha"), "server/alpha")

    def test_prefix_with_dots_and_hyphens(self):
        self.assertEqual(pp._server_branch("team-a.b"), "server/team-a.b")

    def test_constant_is_stable(self):
        self.assertEqual(pp.SERVER_BRANCH_PREFIX, "server/")


class ValidServerNameTests(unittest.TestCase):
    def test_accepts_simple(self):
        self.assertTrue(pp._valid_server_name("alpha"))
        self.assertTrue(pp._valid_server_name("a"))
        self.assertTrue(pp._valid_server_name("0abc"))
        self.assertTrue(pp._valid_server_name("team-1.alpha_beta"))

    def test_rejects_empty(self):
        self.assertFalse(pp._valid_server_name(""))

    def test_rejects_uppercase(self):
        self.assertFalse(pp._valid_server_name("Alpha"))

    def test_rejects_leading_punctuation(self):
        self.assertFalse(pp._valid_server_name(".start"))
        self.assertFalse(pp._valid_server_name("-start"))
        self.assertFalse(pp._valid_server_name("_start"))

    def test_rejects_space(self):
        self.assertFalse(pp._valid_server_name("has space"))

    def test_rejects_too_long(self):
        self.assertFalse(pp._valid_server_name("a" * 65))

    def test_accepts_max_length(self):
        self.assertTrue(pp._valid_server_name("a" + "b" * 63))


class ServerArgPriorityTests(unittest.TestCase):
    """`_server_arg` priority chain:
    1. explicit args.server  2. PAIR_PRESSURE_SERVER  3. sole-server  4. die.
    """

    def setUp(self):
        self._saved = os.environ.pop("PAIR_PRESSURE_SERVER", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["PAIR_PRESSURE_SERVER"] = self._saved
        else:
            os.environ.pop("PAIR_PRESSURE_SERVER", None)

    def _args(self, server=None):
        import argparse
        ns = argparse.Namespace()
        ns.server = server
        return ns

    def test_explicit_flag_wins_over_env(self):
        from unittest import mock
        os.environ["PAIR_PRESSURE_SERVER"] = "env-val"
        with mock.patch.object(pp, "_registry_load",
                               return_value={"servers": [{"name": "only"}]}):
            self.assertEqual(pp._server_arg(self._args("explicit")), "explicit")

    def test_env_used_when_no_flag(self):
        from unittest import mock
        os.environ["PAIR_PRESSURE_SERVER"] = "from-env"
        with mock.patch.object(pp, "_registry_load",
                               return_value={"servers": [{"name": "x"},
                                                          {"name": "y"}]}):
            self.assertEqual(pp._server_arg(self._args(None)), "from-env")

    def test_sole_server_fallback(self):
        from unittest import mock
        with mock.patch.object(pp, "_registry_load",
                               return_value={"servers": [{"name": "only-one"}]}):
            self.assertEqual(pp._server_arg(self._args(None)), "only-one")

    def test_ambiguous_registry_dies(self):
        from unittest import mock
        with mock.patch.object(pp, "_registry_load",
                               return_value={"servers": [{"name": "a"},
                                                          {"name": "b"}]}):
            with self.assertRaises(SystemExit):
                pp._server_arg(self._args(None))

    def test_empty_registry_dies(self):
        from unittest import mock
        with mock.patch.object(pp, "_registry_load",
                               return_value={"servers": []}):
            with self.assertRaises(SystemExit):
                pp._server_arg(self._args(None))


class RegistryRoundtripTests(unittest.TestCase):
    """Save and load servers.json off a tempdir repo."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        repo = Path(self._td.name)
        (repo / ".git").mkdir()
        self._saved_repo = os.environ.get("PAIR_PRESSURE_REPO")
        os.environ["PAIR_PRESSURE_REPO"] = str(repo)

    def tearDown(self):
        if self._saved_repo is not None:
            os.environ["PAIR_PRESSURE_REPO"] = self._saved_repo
        self._td.cleanup()

    def test_load_default_when_missing(self):
        data = pp._registry_load()
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["servers"], [])

    def test_save_then_load(self):
        original = {
            "schema_version": 2,
            "servers": [
                {"name": "alpha", "description": "x", "channels": ["general"]},
                {"name": "beta",  "description": "y", "channels": ["general", "deploys"]},
            ],
        }
        pp._registry_save(original)
        loaded = pp._registry_load()
        self.assertEqual(loaded, original)

    def test_registry_path_under_main_checkout(self):
        # _registry_path is always anchored at _main_repo_path(), not the
        # active worktree, so server-content writes never accidentally
        # touch the registry.
        self.assertEqual(
            pp._registry_path(),
            pp._main_repo_path() / ".pair-pressure" / "servers.json",
        )


class WorktreeRootTests(unittest.TestCase):
    """`_worktree_root` always returns a path under the main checkout."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        repo = Path(self._td.name)
        (repo / ".git").mkdir()
        self._saved_repo = os.environ.get("PAIR_PRESSURE_REPO")
        os.environ["PAIR_PRESSURE_REPO"] = str(repo)

    def tearDown(self):
        if self._saved_repo is not None:
            os.environ["PAIR_PRESSURE_REPO"] = self._saved_repo
        else:
            os.environ.pop("PAIR_PRESSURE_REPO", None)
        self._td.cleanup()

    def test_returns_pp_worktrees_subdir(self):
        wt_root = pp._worktree_root()
        self.assertEqual(wt_root.name, ".pp-worktrees")
        self.assertEqual(wt_root.parent, pp._main_repo_path())


class StateFileTests(unittest.TestCase):
    """Smart-verb state file: load/save, missing, malformed."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        repo = Path(self._td.name)
        (repo / ".git").mkdir()
        self._saved = {
            "PAIR_PRESSURE_REPO": os.environ.get("PAIR_PRESSURE_REPO"),
            "PAIR_PRESSURE_SESSION_ID": os.environ.get("PAIR_PRESSURE_SESSION_ID"),
            "HOME": os.environ.get("HOME"),
            "USERPROFILE": os.environ.get("USERPROFILE"),
        }
        os.environ["PAIR_PRESSURE_REPO"] = str(repo)
        # Redirect HOME for per-session path tests
        self._home = tempfile.TemporaryDirectory()
        os.environ["HOME"] = self._home.name
        os.environ["USERPROFILE"] = self._home.name
        os.environ.pop("PAIR_PRESSURE_SESSION_ID", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()
        self._home.cleanup()

    def test_load_missing_returns_none_none(self):
        sess, glob = pp._state_load()
        self.assertIsNone(sess)
        self.assertIsNone(glob)

    def test_save_and_load_global(self):
        pp._state_save(server="alpha", channel="general",
                       thread_id="2026-05-13_foo", source="test")
        sess, glob = pp._state_load()
        self.assertIsNone(sess)
        self.assertEqual(glob["server"], "alpha")
        self.assertEqual(glob["channel"], "general")
        self.assertEqual(glob["thread_id"], "2026-05-13_foo")
        self.assertEqual(glob["schema_version"], pp.STATE_SCHEMA_VERSION)

    def test_save_with_session_writes_both(self):
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "sess-1"
        pp._state_save(server="alpha", channel="general",
                       thread_id="t1", source="test")
        sess, glob = pp._state_load()
        self.assertEqual(sess["thread_id"], "t1")
        self.assertEqual(glob["thread_id"], "t1")

    def test_malformed_global_returns_none(self):
        pp._state_path_global().parent.mkdir(parents=True, exist_ok=True)
        pp._state_path_global().write_text("{not json", encoding="utf-8")
        _, glob = pp._state_load()
        self.assertIsNone(glob)

    def test_session_id_sanitized(self):
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "../../etc/passwd"
        p = pp._state_path_session()
        # Must stay under ~/.pair-pressure/sessions
        sessions_root = (Path.home() / ".pair-pressure" / "sessions").resolve()
        self.assertEqual(p.resolve().parent, sessions_root)


class ResolveActiveTests(unittest.TestCase):
    """resolve_active precedence: arg > session > global > env > sole."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        repo = Path(self._td.name)
        (repo / ".git").mkdir()
        self._saved = {k: os.environ.get(k) for k in
                       ("PAIR_PRESSURE_REPO", "PAIR_PRESSURE_SERVER",
                        "PAIR_PRESSURE_DEFAULT_CHANNEL",
                        "PAIR_PRESSURE_DEFAULT_THREAD_TITLE",
                        "PAIR_PRESSURE_SESSION_ID",
                        "HOME", "USERPROFILE")}
        os.environ["PAIR_PRESSURE_REPO"] = str(repo)
        self._home = tempfile.TemporaryDirectory()
        os.environ["HOME"] = self._home.name
        os.environ["USERPROFILE"] = self._home.name
        for k in ("PAIR_PRESSURE_SERVER", "PAIR_PRESSURE_DEFAULT_CHANNEL",
                  "PAIR_PRESSURE_DEFAULT_THREAD_TITLE",
                  "PAIR_PRESSURE_SESSION_ID"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()
        self._home.cleanup()

    def _ns(self, **kw):
        ns = unittest.mock.MagicMock()  # accepts any getattr
        ns.server = kw.get("server")
        ns.channel = kw.get("channel")
        ns.thread = kw.get("thread")
        return ns

    def _ns_simple(self, **kw):
        import argparse
        return argparse.Namespace(server=kw.get("server"),
                                  channel=kw.get("channel"),
                                  thread=kw.get("thread"))

    def test_arg_beats_state(self):
        pp._state_save(server="from-state", channel="ch-state", thread_id="t-state")
        ns = self._ns_simple(server="from-arg")
        r = pp.resolve_active(ns)
        self.assertEqual(r["server"], "from-arg")
        self.assertEqual(r["sources"]["server"], "arg")

    def test_session_beats_global(self):
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "s1"
        # Write global, then overwrite with different per-session values.
        pp._state_save(server="g-server", channel="g-ch", thread_id="g-t")
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "s2"
        pp._state_save(server="ses-server", channel="ses-ch", thread_id="ses-t")
        r = pp.resolve_active(self._ns_simple())
        self.assertEqual(r["server"], "ses-server")
        self.assertEqual(r["sources"]["server"], "session")
        self.assertEqual(r["channel"], "ses-ch")
        self.assertEqual(r["thread"], "ses-t")

    def test_global_used_when_no_session(self):
        # No session ID set; only global state file exists.
        os.environ.pop("PAIR_PRESSURE_SESSION_ID", None)
        pp._state_save(server="g-server", channel="g-ch", thread_id="g-t")
        r = pp.resolve_active(self._ns_simple())
        self.assertEqual(r["sources"]["server"], "global")
        self.assertEqual(r["server"], "g-server")

    def test_env_used_when_no_state(self):
        os.environ["PAIR_PRESSURE_SERVER"] = "env-server"
        # Stub registry to avoid sole-server fallback short-circuit.
        from unittest import mock
        with mock.patch.object(pp, "_registry_load",
                               return_value={"servers": [{"name": "a"}, {"name": "b"}]}):
            r = pp.resolve_active(self._ns_simple())
        self.assertEqual(r["server"], "env-server")
        self.assertEqual(r["sources"]["server"], "env")

    def test_sole_server_fallback(self):
        from unittest import mock
        with mock.patch.object(pp, "_registry_load",
                               return_value={"servers": [{"name": "only"}]}):
            r = pp.resolve_active(self._ns_simple())
        self.assertEqual(r["server"], "only")
        self.assertEqual(r["sources"]["server"], "sole-server")

    def test_no_server_dies(self):
        from unittest import mock
        with mock.patch.object(pp, "_registry_load",
                               return_value={"servers": []}):
            with self.assertRaises(SystemExit):
                pp.resolve_active(self._ns_simple())

    def test_default_channel_falls_through_to_env_then_general(self):
        os.environ["PAIR_PRESSURE_SERVER"] = "x"
        from unittest import mock
        with mock.patch.object(pp, "_registry_load",
                               return_value={"servers": [{"name": "x"}]}):
            r = pp.resolve_active(self._ns_simple())
            self.assertEqual(r["channel"], "general")
            self.assertEqual(r["sources"]["channel"], "default")
            os.environ["PAIR_PRESSURE_DEFAULT_CHANNEL"] = "team"
            r2 = pp.resolve_active(self._ns_simple())
            self.assertEqual(r2["channel"], "team")


class TitleSlugMatchTests(unittest.TestCase):
    """_find_thread_by_title_slug picks the freshest thread whose id ends in
    the slug."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name)
        self.ch = self.repo / "channels" / "general"
        self.ch.mkdir(parents=True)
        pp._CURRENT_REPO = self.repo

    def tearDown(self):
        pp._CURRENT_REPO = None
        self._td.cleanup()

    def _mk_thread(self, name, mtime=None):
        d = self.ch / name
        d.mkdir()
        (d / "meta.json").write_text("{}")
        if mtime is not None:
            os.utime(d, (mtime, mtime))
        return d

    def test_no_match_returns_none(self):
        self.assertIsNone(pp._find_thread_by_title_slug("general", "absent"))

    def test_exact_slug_match(self):
        self._mk_thread("2026-05-12_oauth-refresh-token")
        self.assertEqual(
            pp._find_thread_by_title_slug("general", "OAuth refresh-token"),
            "2026-05-12_oauth-refresh-token",
        )

    def test_disambiguated_suffix_match(self):
        self._mk_thread("2026-05-12_general-chat", mtime=1000)
        self._mk_thread("2026-05-13_general-chat-2", mtime=2000)
        self.assertEqual(
            pp._find_thread_by_title_slug("general", "general-chat"),
            "2026-05-13_general-chat-2",
        )

    def test_no_slug_collision(self):
        # `general-chats` should NOT match `general-chat`.
        self._mk_thread("2026-05-12_general-chats")
        self.assertIsNone(pp._find_thread_by_title_slug("general", "general-chat"))


class CaptureMechanismTests(unittest.TestCase):
    """_capture intercepts out() payloads from nested cmd_* calls."""

    def test_captures_single_payload(self):
        def fake_cmd(args):
            pp.out({"hello": "world"})
        result = pp._capture(fake_cmd, None)
        self.assertEqual(result, {"hello": "world"})

    def test_captures_last_payload(self):
        def fake_cmd(args):
            pp.out({"first": 1})
            pp.out({"last": 2})
        self.assertEqual(pp._capture(fake_cmd, None), {"last": 2})

    def test_does_not_leak_capture_state(self):
        # After _capture returns, out() must go to stdout again.
        pp._capture(lambda a: pp.out({"x": 1}), None)
        self.assertIsNone(pp._OUT_CAPTURE)


class SmartVerbsE2ETests(unittest.TestCase):
    """End-to-end against real git: pp send seeds then replies on the same
    thread; state file is updated after each call; pp status surfaces the
    current thread."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git not on PATH")

    def setUp(self):
        import subprocess as _sp
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name) / "chat"
        self.repo.mkdir(parents=True)
        self._home = tempfile.TemporaryDirectory()

        # Init a git repo on `main` branch.
        _sp.run(["git", "init", "-b", "main", str(self.repo)],
                check=True, capture_output=True)
        _sp.run(["git", "-C", str(self.repo), "config", "user.email", "t@t.t"],
                check=True, capture_output=True)
        _sp.run(["git", "-C", str(self.repo), "config", "user.name", "t"],
                check=True, capture_output=True)
        # Seed registry on main with one server.
        (self.repo / ".pair-pressure").mkdir()
        (self.repo / ".pair-pressure" / "servers.json").write_text(
            '{"schema_version": 2, "servers": [{"name": "main", '
            '"description": "", "channels": ["general"]}]}\n'
        )
        _sp.run(["git", "-C", str(self.repo), "add", "-A"],
                check=True, capture_output=True)
        _sp.run(["git", "-C", str(self.repo), "commit", "-m", "init"],
                check=True, capture_output=True)
        # Create a worktree on a server/main branch so pp can find it.
        wt = self.repo / ".pp-worktrees" / "main"
        _sp.run(["git", "-C", str(self.repo), "worktree", "add",
                 "-b", "server/main", str(wt), "main"],
                check=True, capture_output=True)
        # Strip the registry from the server worktree (pp invariant).
        for f in (wt / ".pair-pressure").iterdir():
            f.unlink()
        (wt / ".pair-pressure").rmdir()
        _sp.run(["git", "-C", str(wt), "add", "-A"], check=True, capture_output=True)
        _sp.run(["git", "-C", str(wt), "commit", "-m", "strip registry"],
                check=True, capture_output=True)

        self._saved = {k: os.environ.get(k) for k in
                       ("PAIR_PRESSURE_REPO", "PAIR_PRESSURE_AUTHOR",
                        "PAIR_PRESSURE_SERVER", "PAIR_PRESSURE_SESSION_ID",
                        "PAIR_PRESSURE_ALIAS", "PAIR_PRESSURE_DEFAULT_CHANNEL",
                        "PAIR_PRESSURE_DEFAULT_THREAD_TITLE",
                        "HOME", "USERPROFILE")}
        os.environ["PAIR_PRESSURE_REPO"] = str(self.repo)
        os.environ["PAIR_PRESSURE_AUTHOR"] = "alice"
        os.environ["HOME"] = self._home.name
        os.environ["USERPROFILE"] = self._home.name
        for k in ("PAIR_PRESSURE_SERVER", "PAIR_PRESSURE_SESSION_ID",
                  "PAIR_PRESSURE_ALIAS", "PAIR_PRESSURE_DEFAULT_CHANNEL",
                  "PAIR_PRESSURE_DEFAULT_THREAD_TITLE"):
            os.environ.pop(k, None)

    def tearDown(self):
        pp._CURRENT_REPO = None
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()
        self._home.cleanup()

    def _send(self, body, **flags):
        import argparse
        ns = argparse.Namespace(
            server=flags.get("server"),
            channel=flags.get("channel"),
            thread=flags.get("thread"),
            stance=flags.get("stance", "extend"),
            in_reply_to=None,
            body_file="-",
            body_text=body,
            summary=None,
            via=flags.get("via", "human"),
            model=None,
            alias=None,
            password=None,
            password_stdin=False,
            attachments=flags.get("attachments"),
        )
        return pp._capture(pp.cmd_send, ns)

    def test_first_send_creates_thread(self):
        result = self._send("hello team")
        self.assertEqual(result["kind"], "seed")
        self.assertEqual(result["channel"], "general")
        self.assertTrue(result["thread_id"].endswith("_general-chat"))
        # State got written.
        _, glob = pp._state_load()
        self.assertEqual(glob["thread_id"], result["thread_id"])
        self.assertEqual(glob["source"], "send")

    def test_second_send_replies_on_same_thread(self):
        r1 = self._send("first")
        pp._CURRENT_REPO = None  # simulate fresh process
        r2 = self._send("second")
        self.assertEqual(r2["kind"], "reply")
        self.assertEqual(r2["thread_id"], r1["thread_id"])
        self.assertIsNotNone(r2["post_id"])

    def test_explicit_channel_creates_in_that_channel(self):
        result = self._send("on-deploys", channel="deploys")
        self.assertEqual(result["channel"], "deploys")
        # Channel was auto-ensured.
        self.assertTrue((self.repo / ".pp-worktrees" / "main"
                         / "channels" / "deploys").is_dir())

    def test_stored_thread_in_wrong_channel_falls_through_to_title_match(self):
        # First send into channel A; then switch channel to B explicitly.
        # The stored thread_id from the first send lives in A; pp send into B
        # must NOT try to reply on a thread that doesn't exist in B.
        r1 = self._send("init", channel="alpha")
        pp._CURRENT_REPO = None
        r2 = self._send("hello", channel="beta")
        self.assertEqual(r2["kind"], "seed")
        self.assertEqual(r2["channel"], "beta")
        # Same date+slug yields the same id string, but the new thread lives
        # in a different channel -- distinct on-disk paths.
        wt = self.repo / ".pp-worktrees" / "main"
        self.assertTrue((wt / "channels" / "alpha" / r1["thread_id"]).is_dir())
        self.assertTrue((wt / "channels" / "beta" / r2["thread_id"]).is_dir())
        self.assertNotEqual(r1["channel"], r2["channel"])

    def _read_thread(self, channel, thread):
        import argparse
        ns = argparse.Namespace(
            server=None, channel=channel, thread=thread,
            since=0, no_pull=True,
        )
        return pp._capture(pp.cmd_read_thread, ns)

    def _stage_file(self, name, content):
        """Stage a file in a temp scratch dir and return its absolute path."""
        f = Path(self._td.name) / name
        f.write_text(content)
        return f

    def test_send_with_at_at_token_attaches_and_links(self):
        src = self._stage_file("notes.md", "## notes body\n")
        r = self._send(f"see @@{src} for context")
        wt = self.repo / ".pp-worktrees" / "main"
        tdir = wt / "channels" / r["channel"] / r["thread_id"]
        # One post (the seed) created; find its post-id from the attachments dir.
        att_root = tdir / "attachments"
        self.assertTrue(att_root.is_dir(), "attachments/ dir missing")
        pids = list(att_root.iterdir())
        self.assertEqual(len(pids), 1)
        pid = pids[0].name
        # File copied with the original basename.
        self.assertTrue((att_root / pid / "notes.md").is_file())
        # Seed post body contains the rewritten markdown link.
        seed = next(tdir.glob("*-seed.md")).read_text()
        self.assertIn(f"[notes.md](attachments/{pid}/notes.md)", seed)
        # read-thread surfaces the attachment.
        payload = self._read_thread(r["channel"], r["thread_id"])
        posts = payload["posts"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(posts[0]["attachments"]), 1)
        self.assertEqual(posts[0]["attachments"][0]["name"], "notes.md")
        self.assertEqual(
            posts[0]["attachments"][0]["path"],
            f"attachments/{pid}/notes.md",
        )

    def test_send_with_attach_flag_appends_section(self):
        a = self._stage_file("a.png", "fakepng")
        b = self._stage_file("b.csv", "x,y\n1,2\n")
        r = self._send("payload", attachments=[str(a), str(b)])
        wt = self.repo / ".pp-worktrees" / "main"
        tdir = wt / "channels" / r["channel"] / r["thread_id"]
        pids = list((tdir / "attachments").iterdir())
        self.assertEqual(len(pids), 1)
        pid = pids[0].name
        self.assertTrue((tdir / "attachments" / pid / "a.png").is_file())
        self.assertTrue((tdir / "attachments" / pid / "b.csv").is_file())
        seed = next(tdir.glob("*-seed.md")).read_text()
        self.assertIn("## Attachments", seed)
        self.assertIn(f"[a.png](attachments/{pid}/a.png)", seed)
        self.assertIn(f"[b.csv](attachments/{pid}/b.csv)", seed)
        payload = self._read_thread(r["channel"], r["thread_id"])
        names = sorted(a["name"] for a in payload["posts"][0]["attachments"])
        self.assertEqual(names, ["a.png", "b.csv"])

    def test_attachments_isolated_per_post(self):
        # Same filename in a seed AND a reply must coexist under different
        # post-id subdirs without collision.
        src = self._stage_file("shared.md", "v1")
        r1 = self._send(f"first @@{src}")
        # Mutate the source so we can verify the reply's copy is independent.
        src.write_text("v2")
        pp._CURRENT_REPO = None
        r2 = self._send(f"second @@{src}")
        self.assertEqual(r2["thread_id"], r1["thread_id"])
        wt = self.repo / ".pp-worktrees" / "main"
        tdir = wt / "channels" / r1["channel"] / r1["thread_id"]
        att_root = tdir / "attachments"
        pids = sorted(p.name for p in att_root.iterdir())
        self.assertEqual(len(pids), 2)
        copies = sorted((att_root / pid / "shared.md").read_text() for pid in pids)
        self.assertEqual(copies, ["v1", "v2"])


class ProcessAttachmentsTests(unittest.TestCase):
    """Unit tests for the pure body-rewrite helper. No git, no env."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.scratch = Path(self._td.name)
        self.tdir = self.scratch / "thread"
        self.tdir.mkdir()
        self.pid = "20260514T010101000Z"

    def tearDown(self):
        self._td.cleanup()

    def _file(self, name, content="x"):
        p = self.scratch / name
        p.write_text(content)
        return p

    def test_inline_token_replaced_with_link(self):
        f = self._file("notes.md", "hello")
        body = pp._process_attachments(f"see @@{f}", self.tdir, self.pid, [])
        self.assertIn(f"[notes.md](attachments/{self.pid}/notes.md)", body)
        self.assertTrue((self.tdir / "attachments" / self.pid / "notes.md").is_file())

    def test_nonexistent_inline_token_left_untouched(self):
        body = pp._process_attachments(
            "see @@/does/not/exist and email a@b.c",
            self.tdir, self.pid, [],
        )
        self.assertIn("@@/does/not/exist", body)
        self.assertIn("a@b.c", body)
        self.assertFalse((self.tdir / "attachments").exists())

    def test_inline_collision_within_post_suffixes(self):
        f = self._file("notes.md")
        body = pp._process_attachments(
            f"first @@{f} and second @@{f}", self.tdir, self.pid, [],
        )
        self.assertIn(f"[notes.md](attachments/{self.pid}/notes.md)", body)
        self.assertIn(f"[notes-2.md](attachments/{self.pid}/notes-2.md)", body)
        att = self.tdir / "attachments" / self.pid
        self.assertTrue((att / "notes.md").is_file())
        self.assertTrue((att / "notes-2.md").is_file())

    def test_trailing_punctuation_preserved_outside_link(self):
        f = self._file("notes.md")
        body = pp._process_attachments(
            f"see @@{f}. Some prose.", self.tdir, self.pid, [],
        )
        # The period sits OUTSIDE the markdown link, not inside the URL.
        self.assertIn(
            f"[notes.md](attachments/{self.pid}/notes.md). Some prose.",
            body,
        )

    def test_attach_flag_appends_section(self):
        a = self._file("a.txt", "A")
        b = self._file("b.txt", "B")
        body = pp._process_attachments("body", self.tdir, self.pid, [str(a), str(b)])
        self.assertIn("## Attachments", body)
        self.assertIn(f"- [a.txt](attachments/{self.pid}/a.txt)", body)
        self.assertIn(f"- [b.txt](attachments/{self.pid}/b.txt)", body)

    def test_attach_flag_missing_path_dies(self):
        with self.assertRaises(SystemExit):
            pp._process_attachments(
                "body", self.tdir, self.pid, ["/does/not/exist"],
            )

    def test_no_attachments_is_passthrough(self):
        body = pp._process_attachments(
            "plain body with no tokens", self.tdir, self.pid, [],
        )
        self.assertEqual(body, "plain body with no tokens")
        self.assertFalse((self.tdir / "attachments").exists())


class TaskSafetyBannerTests(unittest.TestCase):
    """The trust banner short-circuits when stderr isn't a TTY, and when it
    DOES fire it names the seed_author so the operator can verify trust."""

    def test_banner_skipped_when_stderr_not_tty(self):
        import io
        buf = io.StringIO()  # no isatty -> defaults to False
        with unittest.mock.patch.object(sys, "stderr", buf):
            pp._print_task_safety_banner(
                {"seed_author": "mallory", "title": "evil", "kind": "task"},
                action="claim",
            )
        self.assertEqual(buf.getvalue(), "")

    def test_banner_includes_giver_and_title_when_tty(self):
        import io

        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

        buf = FakeTTY()
        with unittest.mock.patch.object(sys, "stderr", buf):
            pp._print_task_safety_banner(
                {"seed_author": "mallory", "title": "deploy prod",
                 "kind": "task"},
                action="claim",
            )
        out = buf.getvalue()
        self.assertIn("TRUST CHECK", out)
        self.assertIn("CLAIM", out)
        self.assertIn("mallory", out)
        self.assertIn("deploy prod", out)

    def test_banner_defaults_when_meta_empty(self):
        import io

        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

        buf = FakeTTY()
        with unittest.mock.patch.object(sys, "stderr", buf):
            pp._print_task_safety_banner({}, action="start")
        out = buf.getvalue()
        self.assertIn("<unknown>", out)
        self.assertIn("<no title>", out)
        self.assertIn("START", out)


class StatusCurrentBlockTests(unittest.TestCase):
    """cmd_status emits a `current` block reflecting per-session > global > none."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        repo = Path(self._td.name)
        (repo / ".git").mkdir()
        self._home = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k) for k in
                       ("PAIR_PRESSURE_REPO", "PAIR_PRESSURE_SESSION_ID",
                        "HOME", "USERPROFILE")}
        os.environ["PAIR_PRESSURE_REPO"] = str(repo)
        os.environ["HOME"] = self._home.name
        os.environ["USERPROFILE"] = self._home.name
        os.environ.pop("PAIR_PRESSURE_SESSION_ID", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()
        self._home.cleanup()

    def test_no_state_returns_source_none(self):
        import argparse
        payload = pp._capture(pp.cmd_status, argparse.Namespace())
        self.assertEqual(payload["current"]["source"], "none")
        self.assertIsNone(payload["current"]["thread_id"])

    def test_global_state_surfaces(self):
        import argparse
        pp._state_save(server="alpha", channel="general",
                       thread_id="2026-05-13_x", source="send")
        payload = pp._capture(pp.cmd_status, argparse.Namespace())
        self.assertEqual(payload["current"]["source"], "global")
        self.assertEqual(payload["current"]["thread_id"], "2026-05-13_x")

    def test_per_session_overrides_global(self):
        import argparse
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "sid"
        pp._state_save(server="alpha", channel="general",
                       thread_id="sess-thread", source="send")
        payload = pp._capture(pp.cmd_status, argparse.Namespace())
        self.assertEqual(payload["current"]["source"], "per-session")
        self.assertEqual(payload["current"]["thread_id"], "sess-thread")


# ---- multi-repo (v0.9+) ----

def _mkgit(parent, name):
    """Create a tempdir-like child with a .git marker; return its Path."""
    d = Path(parent) / name
    (d / ".git").mkdir(parents=True)
    return d


class ReposRegistryTests(unittest.TestCase):
    """repos.json load/save/tolerance, anchored under _PP_HOME."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._patch = unittest.mock.patch.object(
            pp, "_PP_HOME", Path(self._home.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._home.cleanup()

    def test_load_default_when_missing(self):
        self.assertEqual(pp._repos_load(), {"schema_version": 1, "repos": []})

    def test_save_then_load(self):
        data = {"schema_version": 1,
                "repos": [{"name": "work", "path": "/x", "remote": "u"}]}
        pp._repos_save(data)
        self.assertEqual(pp._repos_load(), data)

    def test_malformed_returns_default(self):
        p = pp._repos_registry_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        self.assertEqual(pp._repos_load(), {"schema_version": 1, "repos": []})

    def test_path_under_pp_home(self):
        self.assertEqual(pp._repos_registry_path(),
                         Path(self._home.name) / "repos.json")

    def test_repo_entry_lookup(self):
        pp._repos_save({"schema_version": 1,
                        "repos": [{"name": "a", "path": "/p"}]})
        self.assertEqual(pp._repo_entry("a")["path"], "/p")
        self.assertIsNone(pp._repo_entry("nope"))


class MainRepoPathPriorityTests(unittest.TestCase):
    """_main_repo_path priority: override > session > env > sole > die.
    The env-only / no-registry path is the back-compat guarantee."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._ppwd = tempfile.TemporaryDirectory()
        self._patch_home = unittest.mock.patch.object(
            pp, "_PP_HOME", Path(self._ppwd.name))
        self._patch_home.start()
        self._scratch = tempfile.TemporaryDirectory()
        self._envrepo = _mkgit(self._scratch.name, "envrepo")
        self._regrepo = _mkgit(self._scratch.name, "regrepo")
        self._saved = {k: os.environ.get(k) for k in
                       ("PAIR_PRESSURE_REPO", "PAIR_PRESSURE_SESSION_ID",
                        "HOME", "USERPROFILE")}
        os.environ["HOME"] = self._home.name
        os.environ["USERPROFILE"] = self._home.name
        os.environ.pop("PAIR_PRESSURE_SESSION_ID", None)
        pp._REPO_OVERRIDE = None

    def tearDown(self):
        pp._REPO_OVERRIDE = None
        self._patch_home.stop()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._home.cleanup()
        self._ppwd.cleanup()
        self._scratch.cleanup()

    def test_env_used_when_no_registry_backcompat(self):
        os.environ["PAIR_PRESSURE_REPO"] = str(self._envrepo)
        with unittest.mock.patch.object(pp, "_session_repo_name",
                                        return_value=None):
            self.assertEqual(pp._main_repo_path(), self._envrepo)

    def test_override_wins(self):
        os.environ["PAIR_PRESSURE_REPO"] = str(self._envrepo)
        pp._REPO_OVERRIDE = self._regrepo
        self.assertEqual(pp._main_repo_path(), self._regrepo)

    def test_session_repo_beats_env(self):
        os.environ["PAIR_PRESSURE_REPO"] = str(self._envrepo)
        with unittest.mock.patch.object(pp, "_session_repo_name",
                                        return_value="work"), \
             unittest.mock.patch.object(
                 pp, "_repos_list",
                 return_value=[{"name": "work", "path": str(self._regrepo)}]):
            self.assertEqual(pp._main_repo_path(), self._regrepo)

    def test_session_pin_ignored_when_no_registry(self):
        # A stale session pin with an empty registry must not block the env
        # var (a pin can only name a registered repo).
        os.environ["PAIR_PRESSURE_REPO"] = str(self._envrepo)
        with unittest.mock.patch.object(pp, "_session_repo_name",
                                        return_value="work"), \
             unittest.mock.patch.object(pp, "_repos_list", return_value=[]):
            self.assertEqual(pp._main_repo_path(), self._envrepo)

    def test_sole_repo_fallback(self):
        os.environ.pop("PAIR_PRESSURE_REPO", None)
        with unittest.mock.patch.object(pp, "_session_repo_name",
                                        return_value=None), \
             unittest.mock.patch.object(
                 pp, "_repos_list",
                 return_value=[{"name": "only", "path": str(self._regrepo)}]):
            self.assertEqual(pp._main_repo_path(), self._regrepo)

    def test_die_when_nothing_resolves(self):
        os.environ.pop("PAIR_PRESSURE_REPO", None)
        with unittest.mock.patch.object(pp, "_session_repo_name",
                                        return_value=None), \
             unittest.mock.patch.object(pp, "_repos_list", return_value=[]):
            with self.assertRaises(SystemExit):
                pp._main_repo_path()

    def test_session_repo_name_no_recursion(self):
        # _session_repo_name reads the per-session file under HOME and must
        # NOT route through _main_repo_path (which would recurse).
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "sess-x"
        sp = pp._state_path_session()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text('{"repo": "work"}', encoding="utf-8")
        with unittest.mock.patch.object(
                pp, "_main_repo_path",
                side_effect=AssertionError("must not recurse")):
            self.assertEqual(pp._session_repo_name(), "work")


class StateRepoFieldTests(unittest.TestCase):
    """The per-session `repo` field: persist, preserve, clear, schema bump."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        repo = Path(self._td.name)
        (repo / ".git").mkdir()
        self._saved = {k: os.environ.get(k) for k in
                       ("PAIR_PRESSURE_REPO", "PAIR_PRESSURE_SESSION_ID",
                        "HOME", "USERPROFILE")}
        os.environ["PAIR_PRESSURE_REPO"] = str(repo)
        self._home = tempfile.TemporaryDirectory()
        os.environ["HOME"] = self._home.name
        os.environ["USERPROFILE"] = self._home.name
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "s1"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()
        self._home.cleanup()

    def _session(self):
        return pp._state_load_one(pp._state_path_session())

    def test_repo_persists_and_schema_is_2(self):
        pp._state_save(repo="work", source="repo_use", session_only=True)
        sess = self._session()
        self.assertEqual(sess["repo"], "work")
        self.assertEqual(sess["schema_version"], 2)

    def test_routine_write_preserves_repo(self):
        pp._state_save(repo="work", session_only=True)
        pp._state_save(server="eng", channel="general", thread_id="t",
                       session_only=True)
        sess = self._session()
        self.assertEqual(sess["repo"], "work")
        self.assertEqual(sess["server"], "eng")

    def test_clear_repo_unpins(self):
        pp._state_save(repo="work", session_only=True)
        pp._state_save(clear_repo=True, session_only=True)
        sess = self._session()
        self.assertIsNone(sess["repo"])

    def test_old_schema1_file_loads(self):
        sp = pp._state_path_session()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text('{"schema_version": 1, "server": "a", '
                      '"channel": "c", "thread_id": "t"}', encoding="utf-8")
        sess = self._session()
        self.assertEqual(sess["server"], "a")
        self.assertIsNone(sess.get("repo"))


class ConcurrencyIsolationTests(unittest.TestCase):
    """Two sessions pin different repos without clobbering each other."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("PAIR_PRESSURE_SESSION_ID", "HOME", "USERPROFILE")}
        self._home = tempfile.TemporaryDirectory()
        os.environ["HOME"] = self._home.name
        os.environ["USERPROFILE"] = self._home.name

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._home.cleanup()

    def test_distinct_session_repos(self):
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "A"
        pp._state_save(repo="work", session_only=True)
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "B"
        pp._state_save(repo="oss", session_only=True)
        self.assertEqual(pp._session_repo_name(), "oss")
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "A"
        self.assertEqual(pp._session_repo_name(), "work")


class RepoVerbTests(unittest.TestCase):
    """cmd_repo_use / cmd_repo_add / cmd_repo_remove validation + effects."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._patch_home = unittest.mock.patch.object(
            pp, "_PP_HOME", Path(self._home.name))
        self._patch_home.start()
        self._saved = {k: os.environ.get(k) for k in
                       ("PAIR_PRESSURE_AUTHOR", "PAIR_PRESSURE_SESSION_ID",
                        "PAIR_PRESSURE_REPO", "HOME", "USERPROFILE")}
        os.environ["HOME"] = self._home.name
        os.environ["USERPROFILE"] = self._home.name
        os.environ["PAIR_PRESSURE_AUTHOR"] = "alice"
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "s1"
        os.environ.pop("PAIR_PRESSURE_REPO", None)  # hermetic vs ambient env
        self._scratch = tempfile.TemporaryDirectory()
        self._repo = _mkgit(self._scratch.name, "work")
        pp._REPO_OVERRIDE = None

    def tearDown(self):
        pp._REPO_OVERRIDE = None
        self._patch_home.stop()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._home.cleanup()
        self._scratch.cleanup()

    def _ns(self, **kw):
        import argparse
        return argparse.Namespace(**kw)

    def test_repo_use_pins_and_clears_server(self):
        with unittest.mock.patch.object(
                pp, "_repo_entry",
                return_value={"name": "work", "path": str(self._repo)}):
            res = pp._capture(pp.cmd_repo_use, self._ns(name="work"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["active_repo"], "work")
        self.assertTrue(res["sticky"])
        # Read the per-session file directly: cmd_repo_use writes session-only,
        # and _state_load() would also resolve the global path via
        # _main_repo_path (registry lookup of the just-pinned repo).
        sess = pp._state_load_one(pp._state_path_session())
        self.assertEqual(sess["repo"], "work")
        self.assertIsNone(sess["server"])

    def test_repo_use_unknown_dies(self):
        with unittest.mock.patch.object(pp, "_repo_entry", return_value=None):
            with self.assertRaises(SystemExit):
                pp.cmd_repo_use(self._ns(name="ghost"))

    def test_repo_add_bad_name_dies(self):
        with self.assertRaises(SystemExit):
            pp.cmd_repo_add(self._ns(name="Bad Name", url="u", path=None,
                                     no_clone=False, with_server=None,
                                     channels=None))

    def test_repo_add_duplicate_dies(self):
        with unittest.mock.patch.object(
                pp, "_repo_entry", return_value={"name": "work"}):
            with self.assertRaises(SystemExit):
                pp.cmd_repo_add(self._ns(name="work", url="u", path=None,
                                         no_clone=False, with_server=None,
                                         channels=None))

    def test_repo_remove_without_yes_dies(self):
        with unittest.mock.patch.object(
                pp, "_repo_entry",
                return_value={"name": "work", "path": str(self._repo)}):
            with self.assertRaises(SystemExit):
                pp.cmd_repo_remove(self._ns(name="work", yes=False,
                                            delete_clone=False))

    def test_repo_remove_yes_unregisters(self):
        pp._repos_save({"schema_version": 1,
                        "repos": [{"name": "work", "path": str(self._repo)}]})
        res = pp._capture(pp.cmd_repo_remove,
                          self._ns(name="work", yes=True, delete_clone=False))
        self.assertTrue(res["ok"])
        self.assertIsNone(pp._repo_entry("work"))


class CollectFeedPostsTests(unittest.TestCase):
    """_collect_feed_posts tags server/repo and honors --since."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "channels"
        thread = self.root / "general" / "2026-01-01_topic"
        thread.mkdir(parents=True)
        (thread / "meta.json").write_text(
            '{"title": "Topic", "kind": "discussion", "status": "open"}',
            encoding="utf-8")
        # Two legacy-format posts (parse_post falls back to YAML frontmatter).
        (thread / "001-seed.md").write_text(
            "---\nid: 001\nauthor: alice\nvia: human\nstance: extend\n"
            "timestamp: 2026-01-01T00:00:00Z\n---\nfirst\n", encoding="utf-8")
        (thread / "002-reply.md").write_text(
            "---\nid: 002\nauthor: bob\nvia: human\nstance: extend\n"
            "timestamp: 2026-02-01T00:00:00Z\n---\nsecond\n", encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def test_tags_server_and_repo(self):
        posts = pp._collect_feed_posts(self.root, None, server="eng",
                                       repo="work")
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(p["server"] == "eng" for p in posts))
        self.assertTrue(all(p["repo"] == "work" for p in posts))

    def test_no_tags_when_unset(self):
        posts = pp._collect_feed_posts(self.root, None)
        self.assertNotIn("server", posts[0])
        self.assertNotIn("repo", posts[0])

    def test_since_filters(self):
        posts = pp._collect_feed_posts(self.root, "2026-01-15T00:00:00Z")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["author"], "bob")


class PowershellExeTests(unittest.TestCase):
    """`_powershell_exe` resolves an ABSOLUTE path so the toast/statusline work
    from the detached daemon / a PATH without System32 (the FileNotFoundError
    bug)."""

    def test_prefers_which(self):
        with unittest.mock.patch.object(pp.shutil, "which",
                                        side_effect=lambda n: r"C:\ps\pwsh.exe"
                                        if n == "powershell" else None):
            self.assertEqual(pp._powershell_exe(), r"C:\ps\pwsh.exe")

    def test_falls_back_to_system32_literal(self):
        with unittest.mock.patch.object(pp.shutil, "which", return_value=None), \
                unittest.mock.patch.dict(pp.os.environ,
                                         {"SystemRoot": r"C:\Windows"}), \
                unittest.mock.patch.object(pp.os.path, "exists",
                                           return_value=True):
            got = pp._powershell_exe()
            self.assertTrue(got.endswith(
                r"System32\WindowsPowerShell\v1.0\powershell.exe"))
            self.assertIn(r"C:\Windows", got)


class NotifyWindowsArgvTests(unittest.TestCase):
    """`_notify_windows` invokes the resolved absolute powershell, not bare
    'powershell' (which raised FileNotFoundError in the daemon)."""

    def test_argv0_is_resolved_exe(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return unittest.mock.MagicMock(returncode=0, stdout="", stderr="")

        with unittest.mock.patch.object(pp, "_powershell_exe",
                                        return_value=r"C:\abs\powershell.exe"), \
                unittest.mock.patch.object(pp.subprocess, "run",
                                           side_effect=fake_run), \
                unittest.mock.patch.object(pp, "_watch_log"):
            self.assertTrue(pp._notify_windows("t", "m"))
        self.assertEqual(captured["argv"][0], r"C:\abs\powershell.exe")
        self.assertNotEqual(captured["argv"][0], "powershell")


class ChannelArchiveHelperTests(unittest.TestCase):
    """archived channels drop out of enumeration but keep their files."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "channels"
        for ch, archived in (("general", False), ("planning", True),
                             ("newtest", True)):
            d = self.root / ch
            d.mkdir(parents=True)
            meta = {"name": ch, "description": ""}
            if archived:
                meta["archived"] = True
            (d / "channel.json").write_text(json.dumps(meta), encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def test_channel_archived(self):
        self.assertTrue(pp._channel_archived(self.root / "planning"))
        self.assertFalse(pp._channel_archived(self.root / "general"))

    def test_active_channel_dirs_excludes_archived(self):
        names = [p.name for p in pp._active_channel_dirs(self.root)]
        self.assertEqual(names, ["general"])

    def test_archived_names(self):
        # _archived_names reads <wt>/channels/*/channel.json
        wt = Path(self._td.name)
        self.assertEqual(pp._archived_names(wt), {"planning", "newtest"})

    def test_collect_feed_skips_archived(self):
        # seed a post into the archived channel; feed must ignore it
        thr = self.root / "planning" / "2026-01-01_x"
        thr.mkdir(parents=True)
        (thr / "meta.json").write_text('{"title": "X"}', encoding="utf-8")
        (thr / "001-seed.md").write_text(
            "---\nid: 001\nauthor: a\ntimestamp: 2026-01-01T00:00:00Z\n---\nhi\n",
            encoding="utf-8")
        posts = pp._collect_feed_posts(self.root, None)
        self.assertEqual(posts, [])

    def test_collect_feed_explicit_archived_channel_is_empty(self):
        # Even naming the archived channel explicitly keeps it off the feed.
        thr = self.root / "planning" / "2026-01-01_y"
        thr.mkdir(parents=True)
        (thr / "meta.json").write_text('{"title": "Y"}', encoding="utf-8")
        (thr / "001-seed.md").write_text(
            "---\nid: 001\nauthor: a\ntimestamp: 2026-01-01T00:00:00Z\n---\nhi\n",
            encoding="utf-8")
        self.assertEqual(
            pp._collect_feed_posts(self.root, None, channel="planning"), [])


class RenderMessageViewTests(unittest.TestCase):
    """`_render_chat` handles the new message / ambiguous_message views."""

    def _render(self, payload):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pp._render_chat(payload)
        return buf.getvalue()

    def test_message_view_full_body(self):
        post = {"id": "20260512T143022123Z", "author": "alice",
                "timestamp": "2026-05-12T14:30:22Z", "stance": "extend",
                "channel": "general", "thread_title": "T",
                "body": "the full untruncated body"}
        out = self._render({"view": "message", "post": post})
        self.assertIn("the full untruncated body", out)

    def test_message_view_no_match(self):
        out = self._render({"view": "message", "matched": False, "query": "zz"})
        self.assertIn("no post matched", out)
        self.assertIn("zz", out)

    def test_ambiguous_message_lists_candidates(self):
        out = self._render({"view": "ambiguous_message", "query": "3022",
                            "matches": [
                                {"id": "AAA", "channel": "general",
                                 "thread_title": "T1"},
                                {"id": "BBB", "channel": "planning",
                                 "thread_title": "T2"}]})
        self.assertIn("AAA", out)
        self.assertIn("BBB", out)


class SnippetLenTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("PAIR_PRESSURE_SNIPPET_LEN")
        os.environ.pop("PAIR_PRESSURE_SNIPPET_LEN", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("PAIR_PRESSURE_SNIPPET_LEN", None)
        else:
            os.environ["PAIR_PRESSURE_SNIPPET_LEN"] = self._saved

    def test_default_is_240(self):
        with unittest.mock.patch.object(pp, "_config_load", return_value={}):
            self.assertEqual(pp._snippet_len(), 240)

    def test_env_override(self):
        os.environ["PAIR_PRESSURE_SNIPPET_LEN"] = "80"
        self.assertEqual(pp._snippet_len(), 80)

    def test_config_fallback(self):
        with unittest.mock.patch.object(pp, "_config_load",
                                        return_value={"snippet_len": 500}):
            self.assertEqual(pp._snippet_len(), 500)

    def test_invalid_falls_back(self):
        os.environ["PAIR_PRESSURE_SNIPPET_LEN"] = "nope"
        self.assertEqual(pp._snippet_len(), 240)


class FindPostByIdTests(unittest.TestCase):
    """`_find_post_by_id` matches full id + short handle, skips archived."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k) for k in
                       ("PAIR_PRESSURE_REPO", "HOME", "USERPROFILE")}
        repo = Path(self._td.name)
        (repo / ".git").mkdir()
        os.environ["PAIR_PRESSURE_REPO"] = str(repo)
        pp._CURRENT_REPO = None
        self.repo = repo
        self._mkchannel("general", False)
        self._mkchannel("archived-ch", True)
        # two posts in general; ids share the substring "3022"
        self._mkpost("general", "20260512T143022100Z", "alice", "first body")
        self._mkpost("general", "20260513T153022200Z", "bob", "second body")
        # a post only in the archived channel
        self._mkpost("archived-ch", "20260514T101010999Z", "carol", "hidden")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        pp._CURRENT_REPO = None
        self._td.cleanup()

    def _mkchannel(self, name, archived):
        d = self.repo / "channels" / name
        d.mkdir(parents=True)
        meta = {"name": name, "description": ""}
        if archived:
            meta["archived"] = True
        (d / "channel.json").write_text(json.dumps(meta), encoding="utf-8")

    def _mkpost(self, channel, pid, author, body):
        thr = self.repo / "channels" / channel / "2026-01-01_t"
        thr.mkdir(parents=True, exist_ok=True)
        if not (thr / "meta.json").exists():
            (thr / "meta.json").write_text('{"title": "T"}', encoding="utf-8")
        fm = {"id": pid, "in_reply_to": None, "author": author, "alias": None,
              "via": "human", "model": None, "stance": "extend",
              "timestamp": "2026-05-12T14:30:22Z"}
        (thr / f"{pid}-seed.md").write_text(pp.dump_fm(fm, body),
                                            encoding="utf-8")

    def test_exact_full_id(self):
        hit = pp._find_post_by_id("20260512T143022100Z")
        self.assertEqual(hit["author"], "alice")
        self.assertIn("first body", hit["body"])

    def test_unique_short_handle(self):
        hit = pp._find_post_by_id("022100Z")  # only in alice's id
        self.assertEqual(hit["author"], "alice")

    def test_ambiguous_short_handle(self):
        hit = pp._find_post_by_id("3022")  # in both ids
        self.assertIn("ambiguous", hit)
        self.assertEqual(len(hit["ambiguous"]), 2)

    def test_archived_post_not_found(self):
        self.assertIsNone(pp._find_post_by_id("20260514T101010999Z"))

    def test_no_match(self):
        self.assertIsNone(pp._find_post_by_id("zzzzzz"))


class RenderShortIdTests(unittest.TestCase):
    """`_render_posts` prints the short id handle so a post can be re-fetched
    with `pp read --message <id>`."""

    def test_short_id_in_output(self):
        post = {"id": "20260512T143022123Z", "author": "alice",
                "timestamp": "2026-05-12T14:30:22Z", "stance": "extend",
                "body": "hello world"}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pp._render_posts([post], show_channel=False)
        out = buf.getvalue()
        self.assertIn("22123Z", out)  # last 6 chars of the id
        self.assertIn("hello world", out)


class WireStatuslineQuietTests(unittest.TestCase):
    """`_wire_statusline_quiet` is the silent, non-dying core of `watch wire`."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = self._home.name
        os.environ["USERPROFILE"] = self._home.name
        self.claude = Path(self._home.name) / ".claude"
        self.claude.mkdir()
        self.settings = self.claude / "settings.json"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._home.cleanup()

    def test_wires_and_preserves_prev(self):
        self.settings.write_text(
            '{"statusLine": {"type": "command", "command": "echo hi"}}',
            encoding="utf-8")
        self.assertTrue(pp._wire_statusline_quiet())
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["_pp_prev_statusline"], "echo hi")
        self.assertIn("pp-statusline.ps1", data["statusLine"]["command"])

    def test_no_prev_is_empty_string(self):
        self.settings.write_text("{}", encoding="utf-8")
        pp._wire_statusline_quiet()
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["_pp_prev_statusline"], "")

    def test_idempotent(self):
        self.settings.write_text("{}", encoding="utf-8")
        self.assertTrue(pp._wire_statusline_quiet())
        self.assertFalse(pp._wire_statusline_quiet())  # already pp-wired

    def test_malformed_returns_false(self):
        self.settings.write_text("{not json", encoding="utf-8")
        self.assertFalse(pp._wire_statusline_quiet())


class EnsureWiredTests(unittest.TestCase):
    """auto-wire-on-any-pp: one-shot, opt-out, Claude-Code-only, never raises."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._pp = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k) for k in
                       ("HOME", "USERPROFILE", "PAIR_PRESSURE_NO_AUTOWIRE",
                        "PAIR_PRESSURE_IS_WATCH_DAEMON")}
        os.environ["HOME"] = self._home.name
        os.environ["USERPROFILE"] = self._home.name
        os.environ.pop("PAIR_PRESSURE_NO_AUTOWIRE", None)
        os.environ.pop("PAIR_PRESSURE_IS_WATCH_DAEMON", None)
        self.claude = Path(self._home.name) / ".claude"
        self.claude.mkdir()
        self.settings = self.claude / "settings.json"
        self.sentinel = Path(self._pp.name) / "autowire.done"
        self._patch = unittest.mock.patch.object(pp, "_PP_HOME",
                                                  Path(self._pp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._home.cleanup()
        self._pp.cleanup()

    def _args(self, cmd="status"):
        import argparse
        return argparse.Namespace(cmd=cmd)

    def _silence(self):
        return contextlib.redirect_stderr(io.StringIO())

    def test_wires_and_stamps_sentinel(self):
        self.settings.write_text("{}", encoding="utf-8")
        with self._silence():
            pp._ensure_wired(self._args())
        self.assertTrue(self.sentinel.exists())
        data = json.loads(self.settings.read_text())
        self.assertIn("pp-statusline.ps1", data["statusLine"]["command"])

    def test_sentinel_blocks_rewire(self):
        self.sentinel.touch()
        self.settings.write_text("{}", encoding="utf-8")
        pp._ensure_wired(self._args())
        self.assertEqual(self.settings.read_text(), "{}")

    def test_optout_env_skips(self):
        os.environ["PAIR_PRESSURE_NO_AUTOWIRE"] = "1"
        self.settings.write_text("{}", encoding="utf-8")
        pp._ensure_wired(self._args())
        self.assertEqual(self.settings.read_text(), "{}")
        self.assertFalse(self.sentinel.exists())

    def test_optout_config_skips(self):
        self.settings.write_text("{}", encoding="utf-8")
        with unittest.mock.patch.object(pp, "_config_load",
                                        return_value={"watch": {"autowire": False}}):
            pp._ensure_wired(self._args())
        self.assertEqual(self.settings.read_text(), "{}")

    def test_no_settings_skips_without_sentinel(self):
        # no settings.json -> not a Claude Code install -> skip, no sentinel
        pp._ensure_wired(self._args())
        self.assertFalse(self.sentinel.exists())

    def test_watch_command_skipped(self):
        self.settings.write_text("{}", encoding="utf-8")
        pp._ensure_wired(self._args(cmd="watch"))
        self.assertEqual(self.settings.read_text(), "{}")


if __name__ == "__main__":
    # Don't require env vars to be set just to run tests.
    os.environ.setdefault("PAIR_PRESSURE_REPO", "/tmp/_pp_unused")
    os.environ.setdefault("PAIR_PRESSURE_AUTHOR", "test")
    unittest.main()
