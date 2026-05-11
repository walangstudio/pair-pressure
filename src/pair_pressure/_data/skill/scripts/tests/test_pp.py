"""Unit tests for pp.py pure functions.

Run from the scripts/ directory:
    python3 -m unittest tests.test_pp
or:
    python3 tests/test_pp.py
"""
import os
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    # Don't require env vars to be set just to run tests.
    os.environ.setdefault("PAIR_PRESSURE_REPO", "/tmp/_pp_unused")
    os.environ.setdefault("PAIR_PRESSURE_AUTHOR", "test")
    unittest.main()
