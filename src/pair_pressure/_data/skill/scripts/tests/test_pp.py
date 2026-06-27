"""Unit + integration tests for pp.py (schema v3: one repo = one server).

Run from the scripts/ directory:
    python3 -m unittest tests.test_pp
or:
    python -m pytest tests/test_pp.py -q
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# pp imports require env vars only when verb functions run; importing the
# module is fine without them.
import pp  # noqa: E402


# ---- shared fixtures -------------------------------------------------------

_ENV_KEYS = (
    "PAIR_PRESSURE_AUTHOR", "PAIR_PRESSURE_ALIAS", "PAIR_PRESSURE_SERVER",
    "PAIR_PRESSURE_REPO", "PAIR_PRESSURE_SESSION_ID",
    "PAIR_PRESSURE_DEFAULT_CHANNEL", "PAIR_PRESSURE_OFFLINE",
    "PAIR_PRESSURE_SNIPPET_LEN", "PAIR_PRESSURE_ATTACH_ROOT",
    "PAIR_PRESSURE_NO_AUTOWIRE", "PAIR_PRESSURE_IS_WATCH_DAEMON",
    "PAIR_PRESSURE_WATCH_INTERVAL",
    "HOME", "USERPROFILE",
)


def _rmtree(path):
    def onerr(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerr)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True, check=True)


def _init_v3(path, name="srv", admins=None, with_git=True):
    """Scaffold a minimal schema-v3 chat repo (pp-init shape)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if with_git:
        _git(path, "init", "-b", "main")
        _git(path, "config", "user.email", "t@t.t")
        _git(path, "config", "user.name", "t")
        _git(path, "config", "commit.gpgsign", "false")
    ppd = path / ".pair-pressure"
    ppd.mkdir(exist_ok=True)
    (ppd / "schema-version").write_text("3\n", encoding="utf-8")
    (ppd / "server.json").write_text(json.dumps({
        "schema_version": 3, "name": name, "admins": admins or [],
        "created_at": "2026-01-01T00:00:00Z",
    }), encoding="utf-8")
    ch = path / "channels" / "general"
    ch.mkdir(parents=True, exist_ok=True)
    (ch / "channel.json").write_text(json.dumps({
        "name": "general", "description": "", "archived": False,
    }), encoding="utf-8")
    if with_git:
        _git(path, "add", "-A")
        _git(path, "commit", "-m", "init")
    return path


def _pid(i, month=1, day=1):
    """A valid v3 post id: YYYYMMDD T HHMMSSfff Z (19 chars)."""
    return f"2026{month:02d}{day:02d}T0000{i:05d}Z"


def _mkpost(repo, channel, pid, author, body, alias=None,
            via="claude-code", reply_to=None, model=None):
    by = f"{author}/{alias}" if alias else author
    shard = Path(repo) / "channels" / channel / "posts" / pp._shard_for(pid)
    shard.mkdir(parents=True, exist_ok=True)
    f = shard / f"{pid}.md"
    f.write_text(pp.dump_slim(by=by, via=via, model=model, pid=pid,
                              reply_to=reply_to, body=body),
                 encoding="utf-8")
    return f


def _mkchannel(repo, name, archived=False, private=False, members=None):
    d = Path(repo) / "channels" / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"name": name, "description": ""}
    if archived:
        meta["archived"] = True
    if private:
        meta["private"] = True
        meta["members"] = members or []
    (d / "channel.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


class PPBase(unittest.TestCase):
    """Hermetic harness: temp _PP_HOME, temp HOME, scrubbed env, reset
    module globals. Never touches the real ~/.pair-pressure."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pp-test-"))
        self.addCleanup(_rmtree, self.tmp)
        self.pp_home = self.tmp / "pp-home"
        self.pp_home.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        os.environ["PAIR_PRESSURE_AUTHOR"] = "alice"
        patcher = unittest.mock.patch.object(pp, "_PP_HOME", self.pp_home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._reset_active()
        self.addCleanup(self._reset_active)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _reset_active(self):
        pp._ACTIVE_SERVER = None
        pp._ACTIVE_REPO = None

    def _register(self, name, path, url=None):
        reg = pp._servers_load()
        reg.setdefault("servers", []).append({
            "name": name, "path": str(path), "url": url,
            "added_at": "2026-01-01T00:00:00Z"})
        reg.setdefault("schema_version", 2)
        if not reg.get("default"):
            reg["default"] = name
        pp._servers_save(reg)

    def _run(self, func, ns):
        """Reset the per-invocation server cache, silence stderr banners,
        and capture the out() payload."""
        self._reset_active()
        with contextlib.redirect_stderr(io.StringIO()):
            return pp._capture(func, ns)

    def _dies(self, func, ns):
        self._reset_active()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pp._capture(func, ns)

    # namespace builders ------------------------------------------------
    def _send_ns(self, body, **kw):
        return argparse.Namespace(
            server=kw.get("server"), channel=kw.get("channel"),
            reply_to=kw.get("reply_to"), body_file="-", body_text=body,
            via=kw.get("via", "claude-code"), model=kw.get("model"),
            alias=kw.get("alias"), attachments=kw.get("attachments"))

    def _read_ns(self, **kw):
        return argparse.Namespace(
            server=kw.get("server"), channel=None,
            target=kw.get("target"), message_id=kw.get("message_id"),
            limit=kw.get("limit", 30), since=kw.get("since"),
            no_pull=kw.get("no_pull", True), pretty=False)


class GitRepoBase(PPBase):
    """A single registered v3 repo (no remote) as the sole/default server."""

    ADMINS = None

    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git not on PATH")

    def setUp(self):
        super().setUp()
        self.repo = _init_v3(self.tmp / "chat", name="srv",
                             admins=self.ADMINS)
        self._register("srv", self.repo)


# ---- pure helpers (carried over from v0.9.x) -------------------------------

class SlugifyTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(pp.slugify("Hello World"), "hello-world")

    def test_special_chars(self):
        self.assertEqual(pp.slugify("OAuth refresh-token race!"),
                         "oauth-refresh-token-race")

    def test_collapses_separators(self):
        self.assertEqual(pp.slugify("a   b___c"), "a-b-c")

    def test_strips_edge_dashes(self):
        self.assertEqual(pp.slugify("---weird---"), "weird")

    def test_empty_falls_back(self):
        self.assertEqual(pp.slugify("!!!"), "untitled")
        self.assertEqual(pp.slugify(""), "untitled")

    def test_length_cap(self):
        self.assertLessEqual(len(pp.slugify("a" * 200)), 48)


_SLIM_HDR = "---\nby: alice via=h\nrt: 20260101T000000000Z\n---\n\n"


class SnippetTests(unittest.TestCase):
    def test_match_returns_line(self):
        text = _SLIM_HDR + "first line\nsecond OAuth line\nthird\n"
        self.assertEqual(pp._snippet(text, "oauth"), "second OAuth line")

    def test_no_match_returns_first_body_line(self):
        text = _SLIM_HDR + "actual content here\nmore\n"
        self.assertEqual(pp._snippet(text, "missing"), "actual content here")

    def test_long_line_centered(self):
        line = "x" * 200 + " NEEDLE " + "y" * 200
        text = _SLIM_HDR + line + "\n"
        snip = pp._snippet(text, "needle", width=80)
        self.assertLessEqual(len(snip), 82)
        self.assertIn("NEEDLE", snip)


class SafeSubpathTests(unittest.TestCase):
    def test_accepts_simple_name(self):
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d)
            (parent / "general").mkdir()
            self.assertEqual(pp._safe_subpath(parent, "general"),
                             (parent / "general").resolve())

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
            with self.assertRaises(SystemExit):
                pp._safe_subpath(Path(d), ".")


class OriginBranchExistsTests(unittest.TestCase):
    def test_returns_false_on_missing_ref(self):
        fake = unittest.mock.MagicMock(returncode=128, stdout="",
                                       stderr="unknown revision")
        with unittest.mock.patch.object(pp, "git", return_value=fake):
            self.assertFalse(pp._origin_branch_exists("main"))

    def test_returns_true_on_existing_ref(self):
        fake = unittest.mock.MagicMock(returncode=0, stdout="abc123\n",
                                       stderr="")
        with unittest.mock.patch.object(pp, "git", return_value=fake):
            self.assertTrue(pp._origin_branch_exists("main"))


class NotifyDispatchTests(unittest.TestCase):
    def _silence_sentinel(self):
        return unittest.mock.patch.multiple(
            pp, _watch_notify_path=unittest.mock.DEFAULT,
            _watch_log=unittest.mock.DEFAULT)

    def test_macos_routes_to_osascript(self):
        mock = unittest.mock
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
        mock = unittest.mock
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
        mock = unittest.mock
        with self._silence_sentinel(), \
                mock.patch.object(pp.sys, "platform", "win32"), \
                mock.patch.object(pp.os, "name", "nt"), \
                mock.patch.object(pp, "_notify_windows",
                                  return_value=True) as wn:
            self.assertTrue(pp._notify("t", "m"))
            wn.assert_called_once_with("t", "m")

    def test_linux_missing_notify_send_returns_false(self):
        mock = unittest.mock
        with mock.patch.object(pp.shutil, "which", return_value=None), \
                mock.patch.object(pp, "_watch_log"):
            self.assertFalse(pp._notify_linux("t", "m"))

    def test_macos_missing_osascript_returns_false_without_subprocess(self):
        mock = unittest.mock
        with mock.patch.object(pp.shutil, "which", return_value=None), \
                mock.patch.object(pp.subprocess, "run") as run, \
                mock.patch.object(pp, "_watch_log"):
            self.assertFalse(pp._notify_macos("t", "m"))
            run.assert_not_called()

    def test_helper_exception_does_not_propagate(self):
        mock = unittest.mock
        with self._silence_sentinel(), \
                mock.patch.object(pp.sys, "platform", "darwin"), \
                mock.patch.object(pp, "_notify_macos",
                                  side_effect=RuntimeError("boom")):
            self.assertFalse(pp._notify("t", "m"))


class PowershellExeTests(unittest.TestCase):
    def test_prefers_which(self):
        with unittest.mock.patch.object(
                pp.shutil, "which",
                side_effect=lambda n: r"C:\ps\pwsh.exe"
                if n == "powershell" else None):
            self.assertEqual(pp._powershell_exe(), r"C:\ps\pwsh.exe")

    def test_falls_back_to_system32_literal(self):
        with unittest.mock.patch.object(pp.shutil, "which",
                                        return_value=None), \
                unittest.mock.patch.dict(pp.os.environ,
                                         {"SystemRoot": r"C:\Windows"}), \
                unittest.mock.patch.object(pp.os.path, "exists",
                                           return_value=True):
            got = pp._powershell_exe()
            self.assertTrue(got.endswith(
                r"System32\WindowsPowerShell\v1.0\powershell.exe"))
            self.assertIn(r"C:\Windows", got)


class NotifyWindowsArgvTests(unittest.TestCase):
    def test_argv0_is_resolved_exe(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return unittest.mock.MagicMock(returncode=0, stdout="",
                                           stderr="")

        with unittest.mock.patch.object(
                pp, "_powershell_exe",
                return_value=r"C:\abs\powershell.exe"), \
                unittest.mock.patch.object(pp.subprocess, "run",
                                           side_effect=fake_run), \
                unittest.mock.patch.object(pp, "_watch_log"):
            self.assertTrue(pp._notify_windows("t", "m"))
        self.assertEqual(captured["argv"][0], r"C:\abs\powershell.exe")


class CaptureMechanismTests(unittest.TestCase):
    def test_captures_single_payload(self):
        self.assertEqual(
            pp._capture(lambda a: pp.out({"hello": "world"}), None),
            {"hello": "world"})

    def test_captures_last_payload(self):
        def fake_cmd(args):
            pp.out({"first": 1})
            pp.out({"last": 2})
        self.assertEqual(pp._capture(fake_cmd, None), {"last": 2})

    def test_does_not_leak_capture_state(self):
        pp._capture(lambda a: pp.out({"x": 1}), None)
        self.assertIsNone(pp._OUT_CAPTURE)


# ---- slim post header (v3/v4) ----------------------------------------------

class SlimHeaderTests(unittest.TestCase):
    def test_roundtrip(self):
        pid = "20260512T143022123Z"
        text = pp.dump_slim(by="alice/Echo", via="claude-code",
                            model="claude-opus-4-7", pid=pid,
                            reply_to="20260512T142811007Z",
                            body="hello body")
        fm, body = pp.parse_slim(text)
        self.assertEqual(fm["id"], pid)
        self.assertEqual(fm["reply_to"], "20260512T142811007Z")
        self.assertEqual(fm["author"], "alice")
        self.assertEqual(fm["alias"], "Echo")
        self.assertEqual(fm["via"], "claude-code")
        self.assertEqual(fm["model"], "opus47")
        self.assertEqual(fm["timestamp"], "2026-05-12T14:30:22.123Z")
        self.assertEqual(body.strip(), "hello body")

    def test_human_post_no_model_token(self):
        text = pp.dump_slim(by="alice", via="human", model="claude-opus-4-7",
                            pid="20260101T000000000Z", reply_to=None,
                            body="x")
        self.assertNotIn("m=", text.splitlines()[1])
        fm, _ = pp.parse_slim(text)
        self.assertEqual(fm["via"], "human")
        self.assertIsNone(fm["alias"])
        self.assertIsNone(fm["model"])

    def test_no_reply_omits_r_token(self):
        text = pp.dump_slim(by="a", via="claude-code", model=None,
                            pid="20260101T000000000Z", reply_to=None,
                            body="x")
        fm, _ = pp.parse_slim(text)
        self.assertIsNone(fm["reply_to"])
        self.assertNotIn("r=", text)

    def test_unknown_kv_tokens_ignored(self):
        text = ("---\nby: alice via=cc s=contradict m=opus47\n"
                "rt: 20260101T000000000Z q=zz\n---\n\nbody\n")
        fm, body = pp.parse_slim(text)
        self.assertEqual(fm["author"], "alice")
        self.assertEqual(fm["model"], "opus47")
        self.assertIsNone(fm["reply_to"])

    def test_non_slim_returns_none(self):
        self.assertEqual(pp.parse_slim("just a body\n"), (None, None))
        self.assertEqual(pp.parse_slim("---\nby: a via=cc\n---\nx"),
                         (None, None))  # missing rt line

    def test_parse_post_fallback_for_headerless(self):
        fm, body = pp.parse_post("plain text\n")
        self.assertEqual(fm, {"alias": None})
        self.assertEqual(body, "plain text\n")

    def test_body_gets_trailing_newline(self):
        text = pp.dump_slim(by="a", via="claude-code", model=None,
                            pid="20260101T000000000Z", reply_to=None,
                            body="no newline")
        self.assertTrue(text.endswith("no newline\n"))

    def test_short_via_long_via(self):
        self.assertEqual(pp._short_via("claude-code"), "cc")
        self.assertEqual(pp._short_via("human"), "h")
        self.assertEqual(pp._long_via("cc"), "claude-code")
        self.assertEqual(pp._long_via("h"), "human")
        self.assertEqual(pp._long_via(None), "claude-code")

    def test_short_model(self):
        self.assertEqual(pp._short_model("claude-opus-4-7"), "opus47")
        self.assertIsNone(pp._short_model(None))

    def test_id_to_iso(self):
        self.assertEqual(pp._id_to_iso("20260512T143022123Z"),
                         "2026-05-12T14:30:22.123Z")
        self.assertIsNone(pp._id_to_iso("001"))  # legacy ordinal
        self.assertIsNone(pp._id_to_iso(None))

    def test_post_id_shape(self):
        pid = pp.post_id()
        self.assertEqual(len(pid), 19)
        self.assertIsNotNone(pp._POST_NAME_RE.match(pid + ".md"))
        self.assertIsNotNone(pp._id_to_iso(pid))


# ---- month sharding ---------------------------------------------------------

class ShardingTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ch = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_shard_for(self):
        self.assertEqual(pp._shard_for("20260512T143022123Z"), "2026-05")
        self.assertEqual(pp._shard_for(_pid(1, month=12)), "2026-12")

    def test_stem_id(self):
        self.assertEqual(
            pp._stem_id(Path("posts/2026-01/20260101T000000001Z.md")),
            "20260101T000000001Z")

    def _seed(self):
        # two shards, deliberately created newest-first
        ids = [_pid(3, month=2), _pid(1, month=1), _pid(2, month=1)]
        for pid in ids:
            shard = self.ch / "posts" / pp._shard_for(pid)
            shard.mkdir(parents=True, exist_ok=True)
            (shard / f"{pid}.md").write_text("x")
        return sorted(ids)

    def test_post_files_chronological_across_shards(self):
        ids = self._seed()
        self.assertEqual([pp._stem_id(f) for f in pp._post_files(self.ch)],
                         ids)

    def test_post_files_ignores_decoys(self):
        self._seed()
        shard = self.ch / "posts" / "2026-01"
        (shard / "notes.txt").write_text("x")
        (shard / "bad.md").write_text("x")
        (self.ch / "posts" / "not-a-shard").mkdir()
        (self.ch / "posts" / "not-a-shard" / f"{_pid(9)}.md").write_text("x")
        self.assertEqual(len(pp._post_files(self.ch)), 3)

    def test_post_files_desc_newest_first(self):
        ids = self._seed()
        got = [pp._stem_id(f) for f in pp._post_files_desc(self.ch)]
        self.assertEqual(got, list(reversed(ids)))

    def test_post_files_desc_limit_early_stop(self):
        ids = self._seed()
        got = [pp._stem_id(f) for f in pp._post_files_desc(self.ch, limit=1)]
        self.assertEqual(got, [ids[-1]])  # newest only

    def test_post_files_desc_limit_spans_shards(self):
        ids = self._seed()
        got = [pp._stem_id(f) for f in pp._post_files_desc(self.ch, limit=2)]
        self.assertEqual(got, [ids[2], ids[1]])

    def test_empty_channel(self):
        self.assertEqual(pp._post_files(self.ch), [])
        self.assertEqual(pp._post_files_desc(self.ch, limit=5), [])


# ---- schema guard -----------------------------------------------------------

class SchemaGuardTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _dies_with(self, fragment):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                pp._require_schema_v3(self.repo)
        self.assertIn(fragment, err.getvalue())

    def test_v3_marker_passes(self):
        (self.repo / ".pair-pressure").mkdir()
        (self.repo / ".pair-pressure" / "schema-version").write_text("3\n")
        self.assertEqual(pp._require_schema_v3(self.repo), self.repo)

    def test_v2_marker_dies_with_remediation(self):
        (self.repo / ".pair-pressure").mkdir()
        (self.repo / ".pair-pressure" / "schema-version").write_text("2\n")
        self._dies_with("pp-init")

    def test_missing_marker_with_pp_dir_treated_as_v2(self):
        (self.repo / ".pair-pressure").mkdir()
        self._dies_with("schema v2")

    def test_plain_dir_dies_as_not_chat_repo(self):
        self._dies_with("not a pair-pressure chat repo")


# ---- servers registry + resolution ------------------------------------------

class ServersRegistryTests(PPBase):
    def test_load_default_when_missing(self):
        self.assertEqual(pp._servers_load(),
                         {"schema_version": 2, "servers": []})

    def test_malformed_returns_default(self):
        pp._servers_registry_path().write_text("{not json")
        self.assertEqual(pp._servers_load(),
                         {"schema_version": 2, "servers": []})

    def test_save_then_load(self):
        data = {"schema_version": 2, "default": "a",
                "servers": [{"name": "a", "path": "/x", "url": None}]}
        pp._servers_save(data)
        self.assertEqual(pp._servers_load(), data)

    def test_server_entry_lookup(self):
        self._register("work", "/p")
        self.assertEqual(pp._server_entry("work")["path"], "/p")
        self.assertIsNone(pp._server_entry("nope"))

    def test_compat_env_repo_auto_registers_default(self):
        os.environ["PAIR_PRESSURE_REPO"] = str(self.tmp / "chat")
        entry = pp._compat_env_repo_entry()
        self.assertEqual(entry["name"], "default")
        servers = pp._servers_list()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "default")

    def test_compat_env_repo_no_duplicate_when_registered(self):
        self._register("work", "/p")
        os.environ["PAIR_PRESSURE_REPO"] = str(self.tmp / "chat")
        entry = pp._compat_env_repo_entry()
        self.assertEqual(entry["name"], "default")
        self.assertEqual([s["name"] for s in pp._servers_list()], ["work"])

    def test_compat_returns_none_without_env(self):
        self.assertIsNone(pp._compat_env_repo_entry())


class ResolveServerNameTests(PPBase):
    def test_flag_wins(self):
        self._register("reg", "/p")
        os.environ["PAIR_PRESSURE_SERVER"] = "env-srv"
        self.assertEqual(pp.resolve_server_name("flag"), ("flag", "arg"))

    def test_session_beats_global(self):
        pp._state_save(server="glob-srv", source="test")  # global only
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "s1"
        pp._state_save(server="sess-srv", source="test", session_only=True)
        self.assertEqual(pp.resolve_server_name(None),
                         ("sess-srv", "session"))

    def test_global_state(self):
        pp._state_save(server="glob-srv", source="test")
        self.assertEqual(pp.resolve_server_name(None),
                         ("glob-srv", "global"))

    def test_env_var(self):
        os.environ["PAIR_PRESSURE_SERVER"] = "env-srv"
        self.assertEqual(pp.resolve_server_name(None), ("env-srv", "env"))

    def test_registry_default(self):
        self._register("a", "/a")
        self._register("b", "/b")
        reg = pp._servers_load()
        reg["default"] = "b"
        pp._servers_save(reg)
        self.assertEqual(pp.resolve_server_name(None),
                         ("b", "registry-default"))

    def test_sole_entry(self):
        self._register("only", "/p")
        reg = pp._servers_load()
        reg.pop("default", None)
        pp._servers_save(reg)
        self.assertEqual(pp.resolve_server_name(None), ("only", "sole"))

    def test_env_repo_compat_last(self):
        os.environ["PAIR_PRESSURE_REPO"] = str(self.tmp / "chat")
        self.assertEqual(pp.resolve_server_name(None),
                         ("default", "env-repo"))

    def test_nothing_resolves(self):
        self.assertEqual(pp.resolve_server_name(None), (None, None))

    def test_activate_dies_when_nothing(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pp._activate(None)

    def test_server_path_unregistered_dies(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pp._server_path("ghost")


# ---- state files -------------------------------------------------------------

class StateFileTests(PPBase):
    def test_load_missing_returns_none_none(self):
        self.assertEqual(pp._state_load(), (None, None))

    def test_save_and_load_global(self):
        pp._state_save(server="alpha", channel="general", source="test")
        sess, glob = pp._state_load()
        self.assertIsNone(sess)
        self.assertEqual(glob["server"], "alpha")
        self.assertEqual(glob["channel"], "general")
        self.assertEqual(glob["schema_version"], pp.STATE_SCHEMA_VERSION)
        self.assertEqual(glob["source"], "test")

    def test_save_with_session_writes_both(self):
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "sess-1"
        pp._state_save(server="alpha", channel="general", source="test")
        sess, glob = pp._state_load()
        self.assertEqual(sess["server"], "alpha")
        self.assertEqual(glob["server"], "alpha")

    def test_session_only_skips_global(self):
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "sess-1"
        pp._state_save(channel="dev", source="test", session_only=True)
        sess, glob = pp._state_load()
        self.assertEqual(sess["channel"], "dev")
        self.assertIsNone(glob)

    def test_merge_none_leaves_existing(self):
        pp._state_save(server="alpha", alias="Echo", source="t")
        pp._state_save(channel="dev", source="t")
        _, glob = pp._state_load()
        self.assertEqual(glob["server"], "alpha")
        self.assertEqual(glob["alias"], "Echo")
        self.assertEqual(glob["channel"], "dev")

    def test_strips_v2_legacy_keys(self):
        p = pp._state_path_global()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"server": "a", "thread_id": "t",
                                 "repo": "r", "task_index": 3}),
                     encoding="utf-8")
        pp._state_save(channel="general", source="t")
        _, glob = pp._state_load()
        self.assertEqual(glob["server"], "a")
        self.assertNotIn("thread_id", glob)
        self.assertNotIn("repo", glob)
        self.assertNotIn("task_index", glob)

    def test_malformed_global_returns_none(self):
        p = pp._state_path_global()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        self.assertIsNone(pp._state_load()[1])

    def test_session_id_sanitized(self):
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "../../etc/passwd"
        p = pp._state_path_session()
        self.assertEqual(p.resolve().parent,
                         (self.pp_home / "sessions").resolve())

    def test_no_session_id_no_session_path(self):
        self.assertIsNone(pp._state_path_session())
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "   "
        self.assertIsNone(pp._state_path_session())


class ConcurrencyIsolationTests(PPBase):
    def test_distinct_sessions_keep_distinct_state(self):
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "A"
        pp._state_save(channel="ch-a", alias="Ax", session_only=True)
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "B"
        pp._state_save(channel="ch-b", alias="Bx", session_only=True)
        sess, _ = pp._state_load()
        self.assertEqual(sess["channel"], "ch-b")
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "A"
        sess, _ = pp._state_load()
        self.assertEqual(sess["channel"], "ch-a")
        self.assertEqual(sess["alias"], "Ax")


class ResolveActiveTests(PPBase):
    def _ns(self, **kw):
        return argparse.Namespace(server=kw.get("server"),
                                  channel=kw.get("channel"))

    def test_arg_beats_state(self):
        pp._state_save(server="from-state", channel="ch-state")
        r = pp.resolve_active(self._ns(server="from-arg", channel="ch-arg"))
        self.assertEqual(r["server"], "from-arg")
        self.assertEqual(r["sources"]["server"], "arg")
        self.assertEqual(r["channel"], "ch-arg")
        self.assertEqual(r["sources"]["channel"], "arg")

    def test_session_beats_global(self):
        pp._state_save(server="g-srv", channel="g-ch")
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "s1"
        pp._state_save(server="s-srv", channel="s-ch", session_only=True)
        r = pp.resolve_active(self._ns())
        self.assertEqual(r["server"], "s-srv")
        self.assertEqual(r["sources"]["server"], "session")
        self.assertEqual(r["channel"], "s-ch")
        self.assertEqual(r["sources"]["channel"], "session")

    def test_global_used_when_no_session(self):
        pp._state_save(server="g-srv", channel="g-ch")
        r = pp.resolve_active(self._ns())
        self.assertEqual(r["sources"]["server"], "global")
        self.assertEqual(r["channel"], "g-ch")

    def test_default_channel_general(self):
        self._register("only", "/p")
        r = pp.resolve_active(self._ns())
        self.assertEqual(r["channel"], "general")
        self.assertEqual(r["sources"]["channel"], "default")

    def test_default_channel_env(self):
        self._register("only", "/p")
        os.environ["PAIR_PRESSURE_DEFAULT_CHANNEL"] = "team"
        r = pp.resolve_active(self._ns())
        self.assertEqual(r["channel"], "team")

    def test_no_server_dies(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pp.resolve_active(self._ns())

    def test_mutates_args_in_place(self):
        self._register("only", "/p")
        ns = self._ns()
        pp.resolve_active(ns)
        self.assertEqual(ns.server, "only")
        self.assertEqual(ns.channel, "general")


# ---- alias -------------------------------------------------------------------

class AliasTests(PPBase):
    def test_env_beats_persisted(self):
        pp._state_save(alias="Persisted")
        os.environ["PAIR_PRESSURE_ALIAS"] = "FromEnv"
        self.assertEqual(pp.alias(), "FromEnv")

    def test_persisted_used_without_env(self):
        pp._state_save(alias="Persisted")
        self.assertEqual(pp.alias(), "Persisted")

    def test_none_when_unset(self):
        self.assertIsNone(pp.alias())

    def test_effective_alias_flag_beats_env(self):
        os.environ["PAIR_PRESSURE_ALIAS"] = "FromEnv"
        ns = argparse.Namespace(alias="FromFlag")
        self.assertEqual(pp.effective_alias(ns), "FromFlag")
        self.assertEqual(pp.effective_alias(None), "FromEnv")

    def test_cmd_alias_persists_to_state(self):
        payload = self._run(pp.cmd_alias, argparse.Namespace(name="Echo"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["persisted"], "global")
        _, glob = pp._state_load()
        self.assertEqual(glob["alias"], "Echo")

    def test_cmd_alias_session_persist_label(self):
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "s1"
        payload = self._run(pp.cmd_alias, argparse.Namespace(name="Echo"))
        self.assertEqual(payload["persisted"], "session")

    def test_cmd_alias_show_mode(self):
        pp._state_save(alias="Echo")
        payload = self._run(pp.cmd_alias, argparse.Namespace(name=None))
        self.assertEqual(payload["alias"], "Echo")

    def test_cmd_alias_env_warning(self):
        os.environ["PAIR_PRESSURE_ALIAS"] = "EnvAlias"
        payload = self._run(pp.cmd_alias, argparse.Namespace(name="Echo"))
        self.assertIn("warning_env", payload)

    def test_cmd_alias_rejects_whitespace(self):
        # An alias with a space can't round-trip the single-line slim header.
        self._dies(pp.cmd_alias, argparse.Namespace(name="Code Reviewer"))

    def test_cmd_alias_rejects_slash(self):
        self._dies(pp.cmd_alias, argparse.Namespace(name="a/b"))

    def test_alias_in_use_elsewhere(self):
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "me"
        sessions = self.pp_home / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "other.json").write_text(json.dumps({"alias": "Echo"}),
                                             encoding="utf-8")
        self.assertTrue(pp._alias_in_use_elsewhere("Echo"))
        self.assertFalse(pp._alias_in_use_elsewhere("Nobody"))

    def test_alias_in_use_excludes_own_session(self):
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "me"
        pp._state_save(alias="Mine", session_only=True)
        self.assertFalse(pp._alias_in_use_elsewhere("Mine"))

    def test_alias_in_use_ignores_stale_sessions(self):
        sessions = self.pp_home / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        stale = sessions / "old.json"
        stale.write_text(json.dumps({"alias": "Echo"}), encoding="utf-8")
        old = time.time() - 7200
        os.utime(stale, (old, old))
        self.assertFalse(pp._alias_in_use_elsewhere("Echo"))

    def test_cmd_alias_collision_warning(self):
        sessions = self.pp_home / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "other.json").write_text(json.dumps({"alias": "Echo"}),
                                             encoding="utf-8")
        payload = self._run(pp.cmd_alias, argparse.Namespace(name="Echo"))
        self.assertIn("warning", payload)

    def test_by_for_via_human_hides_alias(self):
        os.environ["PAIR_PRESSURE_ALIAS"] = "Echo"
        self.assertEqual(pp.by_for_via("human"), "alice")
        self.assertEqual(pp.by_for_via("claude-code"), "alice/Echo")

    def test_by_for_via_flag_override(self):
        os.environ["PAIR_PRESSURE_ALIAS"] = "Echo"
        ns = argparse.Namespace(alias="Zed")
        self.assertEqual(pp.by_for_via("mcp", ns), "alice/Zed")

    def test_by_token(self):
        self.assertEqual(pp.by_token(), "alice")
        os.environ["PAIR_PRESSURE_ALIAS"] = "Echo"
        self.assertEqual(pp.by_token(), "alice/Echo")


# ---- use / where / switch ----------------------------------------------------

class UseWhereTests(GitRepoBase):
    def test_switch_to_unregistered_server_dies(self):
        self._reset_active()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pp._switch_to(server="ghost")

    def test_switch_to_missing_channel_dies(self):
        self._reset_active()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pp._switch_to(server="srv", channel="nope")

    def test_switch_to_archived_channel_dies(self):
        _mkchannel(self.repo, "old", archived=True)
        self._reset_active()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pp._switch_to(channel="old")

    def test_switch_to_private_non_member_dies(self):
        _mkchannel(self.repo, "dm-b-c", private=True,
                   members=["bob", "carol"])
        self._reset_active()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pp._switch_to(channel="dm-b-c")

    def test_switch_persists_state(self):
        self._reset_active()
        with contextlib.redirect_stderr(io.StringIO()):
            row = pp._switch_to(server="srv", channel="general")
        self.assertTrue(row["ok"])
        self.assertEqual(row["server"], "srv")
        self.assertEqual(row["channel"], "general")
        _, glob = pp._state_load()
        self.assertEqual(glob["server"], "srv")
        self.assertEqual(glob["channel"], "general")

    def test_cmd_use_server_token(self):
        payload = self._run(pp.cmd_use, argparse.Namespace(target=["srv"]))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["server"], "srv")
        self.assertEqual(payload["channel"], "general")

    def test_cmd_use_channel_token(self):
        _mkchannel(self.repo, "dev")
        self._run(pp.cmd_use, argparse.Namespace(target=["srv"]))
        payload = self._run(pp.cmd_use, argparse.Namespace(target=["#dev"]))
        self.assertEqual(payload["channel"], "dev")
        self.assertEqual(payload["server"], "srv")

    def test_cmd_use_both_tokens(self):
        _mkchannel(self.repo, "dev")
        payload = self._run(pp.cmd_use,
                            argparse.Namespace(target=["srv", "#dev"]))
        self.assertEqual(payload["server"], "srv")
        self.assertEqual(payload["channel"], "dev")
        self.assertIn("srv #dev", payload["where"])

    def test_cmd_use_two_servers_dies(self):
        self._dies(pp.cmd_use, argparse.Namespace(target=["a", "b"]))

    def test_cmd_where_line(self):
        os.environ["PAIR_PRESSURE_ALIAS"] = "Echo"
        self._run(pp.cmd_use, argparse.Namespace(target=["srv"]))
        payload = self._run(pp.cmd_where, argparse.Namespace(pretty=False))
        self.assertEqual(payload["where"], "srv #general (alias: Echo)")
        self.assertEqual(payload["server"], "srv")
        self.assertEqual(payload["channel"], "general")
        self.assertEqual(payload["alias"], "Echo")

    def test_cmd_where_no_server(self):
        pp._servers_save({"schema_version": 2, "servers": []})
        payload = self._run(pp.cmd_where, argparse.Namespace(pretty=False))
        self.assertIsNone(payload["server"])
        self.assertIn("(no server)", payload["where"])


# ---- status verdicts ----------------------------------------------------------

class StatusVerdictTests(PPBase):
    def _status(self):
        return self._run(pp.cmd_status, argparse.Namespace())

    def test_not_configured(self):
        os.environ.pop("PAIR_PRESSURE_AUTHOR", None)
        payload = self._status()
        self.assertEqual(payload["verdict"], "not_configured")

    def test_needs_restart(self):
        # Saved author + a registered server, but the env not yet loaded:
        # only a CLI restart is missing. (With NOTHING registered the
        # earlier `not_configured` branch wins by design.)
        os.environ.pop("PAIR_PRESSURE_AUTHOR", None)
        self._register("srv", "/p")
        claude = self.home / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(json.dumps(
            {"env": {"PAIR_PRESSURE_AUTHOR": "alice"}}), encoding="utf-8")
        payload = self._status()
        self.assertEqual(payload["verdict"], "needs_restart")

    def test_needs_author(self):
        os.environ.pop("PAIR_PRESSURE_AUTHOR", None)
        self._register("srv", "/p")
        payload = self._status()
        self.assertEqual(payload["verdict"], "needs_author")

    def test_needs_server(self):
        payload = self._status()
        self.assertEqual(payload["verdict"], "needs_server")
        self.assertIsNone(payload["where"])

    def test_ready(self):
        self._register("srv", "/p")
        payload = self._status()
        self.assertEqual(payload["verdict"], "ready")
        self.assertEqual(payload["server"], "srv")
        self.assertEqual(payload["where"], "srv #general")

    def test_state_channel_surfaces(self):
        self._register("srv", "/p")
        pp._state_save(channel="dev", source="t")
        payload = self._status()
        self.assertEqual(payload["channel"], "dev")
        self.assertEqual(payload["where"], "srv #dev")


# ---- offline + watch interval --------------------------------------------------

class OfflineTests(PPBase):
    def test_default_online(self):
        self.assertFalse(pp._offline())

    def test_env_true(self):
        os.environ["PAIR_PRESSURE_OFFLINE"] = "1"
        self.assertTrue(pp._offline())

    def test_env_overrides_config_false(self):
        pp._config_save({"offline": True})
        os.environ["PAIR_PRESSURE_OFFLINE"] = "0"
        self.assertFalse(pp._offline())

    def test_config_fallback(self):
        pp._config_save({"offline": True})
        self.assertTrue(pp._offline())

    def test_cmd_offline_set_and_show(self):
        payload = self._run(pp.cmd_offline, argparse.Namespace(state="true"))
        self.assertTrue(payload["offline"])
        self.assertTrue(payload["saved"])
        show = self._run(pp.cmd_offline, argparse.Namespace(state=None))
        self.assertTrue(show["offline"])
        self.assertEqual(show["source"], "config")

    def test_cmd_offline_env_warning(self):
        os.environ["PAIR_PRESSURE_OFFLINE"] = "1"
        payload = self._run(pp.cmd_offline,
                            argparse.Namespace(state="false"))
        self.assertIn("warning", payload)


class WatchIntervalTests(PPBase):
    def test_parse_interval_forms(self):
        self.assertEqual(pp._parse_interval("90"), 90)
        self.assertEqual(pp._parse_interval("90s"), 90)
        self.assertEqual(pp._parse_interval("5m"), 300)
        self.assertEqual(pp._parse_interval("1h"), 3600)
        self.assertIsNone(pp._parse_interval("junk"))
        self.assertIsNone(pp._parse_interval(""))
        self.assertIsNone(pp._parse_interval(None))

    def test_default(self):
        self.assertEqual(pp._resolve_interval(), (300, "default"))

    def test_config_then_env_precedence(self):
        pp._config_save({"watch": {"interval": 600}})
        self.assertEqual(pp._resolve_interval(), (600, "config"))
        os.environ["PAIR_PRESSURE_WATCH_INTERVAL"] = "2m"
        self.assertEqual(pp._resolve_interval(), (120, "env"))

    def test_clamped_to_minimum(self):
        os.environ["PAIR_PRESSURE_WATCH_INTERVAL"] = "1"
        secs, _ = pp._resolve_interval()
        self.assertEqual(secs, pp._WATCH_INTERVAL_MIN)


# ---- unread buckets -------------------------------------------------------------

class UnreadBucketTests(PPBase):
    def test_key_defaults_to_shared(self):
        self.assertEqual(pp._watch_unread_key(), pp._SHARED_BUCKET)
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "s1"
        self.assertEqual(pp._watch_unread_key(), "s1")

    def test_legacy_flat_shape_migrates(self):
        pp._watch_unread_path().write_text(json.dumps(
            {"count": 2, "latest": None, "updated_at": None}),
            encoding="utf-8")
        buckets = pp._watch_unread_load_all()
        self.assertEqual(buckets[pp._SHARED_BUCKET]["count"], 2)

    def test_bump_increments_every_bucket(self):
        pp._watch_unread_save_all({"A": {"count": 1}, "B": {"count": 0}})
        pp._watch_unread_bump([{"author": "bob", "channel": "general"}])
        buckets = pp._watch_unread_load_all()
        self.assertEqual(buckets["A"]["count"], 2)
        self.assertEqual(buckets["B"]["count"], 1)
        self.assertEqual(buckets["A"]["latest"]["author"], "bob")

    def test_bump_seeds_shared_when_empty(self):
        pp._watch_unread_bump([{"author": "bob", "channel": "general"},
                               {"author": "carol", "channel": "dev"}])
        buckets = pp._watch_unread_load_all()
        self.assertEqual(buckets[pp._SHARED_BUCKET]["count"], 2)

    def test_ack_clears_only_one_bucket(self):
        pp._watch_unread_save_all({"A": {"count": 3}, "B": {"count": 5}})
        os.environ["PAIR_PRESSURE_SESSION_ID"] = "A"
        pp._watch_ack()
        buckets = pp._watch_unread_load_all()
        self.assertEqual(buckets["A"]["count"], 0)
        self.assertEqual(buckets["B"]["count"], 5)


# ---- snippet length --------------------------------------------------------------

class SnippetLenTests(PPBase):
    def test_default_is_240(self):
        self.assertEqual(pp._snippet_len(), 240)

    def test_env_override(self):
        os.environ["PAIR_PRESSURE_SNIPPET_LEN"] = "80"
        self.assertEqual(pp._snippet_len(), 80)

    def test_config_fallback(self):
        pp._config_save({"snippet_len": 500})
        self.assertEqual(pp._snippet_len(), 500)

    def test_invalid_falls_back(self):
        os.environ["PAIR_PRESSURE_SNIPPET_LEN"] = "nope"
        self.assertEqual(pp._snippet_len(), 240)

    def test_nonpositive_falls_back(self):
        os.environ["PAIR_PRESSURE_SNIPPET_LEN"] = "0"
        self.assertEqual(pp._snippet_len(), 240)


# ---- admin gating -----------------------------------------------------------------

class IsAdminTests(PPBase):
    def _meta_repo(self, admins):
        repo = self.tmp / "meta-repo"
        (repo / ".pair-pressure").mkdir(parents=True)
        (repo / ".pair-pressure" / "server.json").write_text(
            json.dumps({"schema_version": 3, "name": "x",
                        "admins": admins}), encoding="utf-8")
        pp._ACTIVE_REPO = repo
        pp._ACTIVE_SERVER = "x"

    def test_empty_admins_everyone_allowed(self):
        self._meta_repo([])
        self.assertTrue(pp._is_admin("anyone"))

    def test_admin_in_list(self):
        self._meta_repo(["alice"])
        self.assertTrue(pp._is_admin("alice"))
        self.assertFalse(pp._is_admin("bob"))

    def test_require_admin_dies_for_non_admin(self):
        self._meta_repo(["alice"])
        os.environ["PAIR_PRESSURE_AUTHOR"] = "bob"
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pp._require_admin("channel new")

    def test_require_admin_returns_admin(self):
        self._meta_repo(["alice"])
        self.assertEqual(pp._require_admin("channel new"), "alice")


class AdminGatedChannelTests(GitRepoBase):
    ADMINS = ["alice"]

    def _new_ns(self, name, **kw):
        return argparse.Namespace(name=name,
                                  description=kw.get("description", ""),
                                  server=kw.get("server"))

    def test_admin_creates_channel(self):
        payload = self._run(pp.cmd_channel_new, self._new_ns("dev"))
        self.assertTrue(payload["created"])
        self.assertTrue((self.repo / "channels" / "dev" /
                         "channel.json").is_file())

    def test_non_admin_create_dies(self):
        os.environ["PAIR_PRESSURE_AUTHOR"] = "bob"
        self._dies(pp.cmd_channel_new, self._new_ns("dev"))

    def test_non_admin_archive_dies(self):
        os.environ["PAIR_PRESSURE_AUTHOR"] = "bob"
        self._dies(pp.cmd_channel_archive, self._new_ns("general"))

    def test_non_admin_unarchive_dies(self):
        os.environ["PAIR_PRESSURE_AUTHOR"] = "bob"
        self._dies(pp.cmd_channel_unarchive, self._new_ns("general"))

    def test_invalid_channel_name_dies(self):
        self._dies(pp.cmd_channel_new, self._new_ns("Bad Name"))

    def test_existing_channel_noop(self):
        payload = self._run(pp.cmd_channel_new, self._new_ns("general"))
        self.assertFalse(payload["created"])

    def test_archive_unarchive_roundtrip(self):
        payload = self._run(pp.cmd_channel_archive, self._new_ns("general"))
        self.assertTrue(payload["archived"])
        meta = json.loads((self.repo / "channels" / "general" /
                           "channel.json").read_text())
        self.assertTrue(meta["archived"])
        payload = self._run(pp.cmd_channel_unarchive,
                            self._new_ns("general"))
        self.assertFalse(payload["archived"])
        meta = json.loads((self.repo / "channels" / "general" /
                           "channel.json").read_text())
        self.assertNotIn("archived", meta)

    def test_channel_new_revives_archived(self):
        self._run(pp.cmd_channel_archive, self._new_ns("general"))
        payload = self._run(pp.cmd_channel_new, self._new_ns("general"))
        self.assertTrue(payload.get("unarchived"))


# ---- tasks ------------------------------------------------------------------------

class MatchTaskTests(unittest.TestCase):
    TASKS = [
        {"id": 1, "title": "Fix the deploy script"},
        {"id": 2, "title": "Deploy to staging"},
        {"id": 3, "title": "Write docs"},
    ]

    def test_match_by_id(self):
        self.assertEqual(pp._match_task(self.TASKS, "2")["id"], 2)
        self.assertEqual(pp._match_task(self.TASKS, 3)["id"], 3)

    def test_match_by_hash_id(self):
        self.assertEqual(pp._match_task(self.TASKS, "#1")["id"], 1)

    def test_missing_id_returns_none(self):
        self.assertIsNone(pp._match_task(self.TASKS, "#9"))

    def test_title_substring_unique(self):
        t = pp._match_task(self.TASKS, "docs")
        self.assertEqual(t["id"], 3)

    def test_title_substring_ambiguous_returns_list(self):
        hits = pp._match_task(self.TASKS, "deploy")
        self.assertIsInstance(hits, list)
        self.assertEqual(len(hits), 2)

    def test_no_match_returns_none(self):
        self.assertIsNone(pp._match_task(self.TASKS, "zzz"))


class TaskVerbTests(GitRepoBase):
    def _new(self, title, channel=None):
        return self._run(pp.cmd_task_new, argparse.Namespace(
            title=title, channel=channel, server=None))

    def _list(self, all=False):
        return self._run(pp.cmd_task_list, argparse.Namespace(
            channel=None, all=all, no_pull=True, server=None))

    def _done(self, ref):
        return self._run(pp.cmd_task_done, argparse.Namespace(
            ref=ref, channel=None, server=None))

    def test_new_assigns_sequential_ids(self):
        r1 = self._new("first task")
        r2 = self._new("second task")
        self.assertEqual(r1["task"]["id"], 1)
        self.assertEqual(r2["task"]["id"], 2)
        self.assertEqual(r1["task"]["status"], "open")
        self.assertEqual(r1["task"]["by"], "alice")

    def test_empty_title_dies(self):
        self._dies(pp.cmd_task_new, argparse.Namespace(
            title="   ", channel=None, server=None))

    def test_list_open_only_then_all(self):
        self._new("alpha")
        self._new("beta")
        self._done("#1")
        open_tasks = self._list()["tasks"]
        self.assertEqual([t["title"] for t in open_tasks], ["beta"])
        all_tasks = self._list(all=True)["tasks"]
        self.assertEqual(len(all_tasks), 2)

    def test_done_by_title_substring(self):
        self._new("ship the release")
        r = self._done("ship")
        self.assertEqual(r["task"]["status"], "done")
        self.assertEqual(r["task"]["done_by"], "alice")

    def test_new_in_archived_channel_dies(self):
        # send blocks archived channels; the task verbs must too (no writing
        # into a channel that listings/feed/watcher all hide).
        meta = pp._channel_meta(self.repo / "channels" / "general")
        meta["archived"] = True
        pp.write_json(self.repo / "channels" / "general" / "channel.json", meta)
        self._dies(pp.cmd_task_new, argparse.Namespace(
            title="late task", channel=None, server=None))

    def test_done_twice_reports_already_done(self):
        self._new("one")
        self._done("#1")
        r = self._done("#1")
        self.assertTrue(r.get("already_done"))

    def test_done_unmatched_dies(self):
        self._new("one")
        self._dies(pp.cmd_task_done, argparse.Namespace(
            ref="nothing-matches", channel=None, server=None))

    def test_done_ambiguous_dies(self):
        self._new("deploy web")
        self._new("deploy api")
        self._dies(pp.cmd_task_done, argparse.Namespace(
            ref="deploy", channel=None, server=None))

    def test_tasks_json_shape(self):
        self._new("one")
        data = json.loads((self.repo / "channels" / "general" /
                           "tasks.json").read_text())
        self.assertEqual(data["next_id"], 2)
        self.assertEqual(len(data["tasks"]), 1)

    def _claim(self, ref):
        return self._run(pp.cmd_task_claim, argparse.Namespace(
            ref=ref, channel=None, server=None))

    def _assign(self, ref, user):
        return self._run(pp.cmd_task_assign, argparse.Namespace(
            ref=ref, user=user, channel=None, server=None))

    def _release(self, ref):
        return self._run(pp.cmd_task_release, argparse.Namespace(
            ref=ref, channel=None, server=None))

    def test_new_task_has_no_assignee(self):
        self.assertIsNone(self._new("x")["task"]["assignee"])

    def test_claim_sets_assignee_and_status(self):
        self._new("ship it")
        r = self._claim("#1")
        self.assertEqual(r["task"]["assignee"], "alice")
        self.assertEqual(r["task"]["status"], "claimed")

    def test_assign_hands_off_to_other_user(self):
        self._new("ship it")
        r = self._assign("#1", "bob")
        self.assertEqual(r["task"]["assignee"], "bob")
        self.assertEqual(r["task"]["status"], "claimed")

    def test_release_returns_task_to_open(self):
        self._new("ship it")
        self._claim("#1")
        r = self._release("#1")
        self.assertIsNone(r["task"]["assignee"])
        self.assertEqual(r["task"]["status"], "open")

    def test_claim_held_by_other_dies(self):
        self._new("ship it")
        self._assign("#1", "bob")
        self._dies(pp.cmd_task_claim, argparse.Namespace(
            ref="#1", channel=None, server=None))

    def test_holder_can_reclaim(self):
        self._new("ship it")
        self._claim("#1")
        self.assertEqual(self._claim("#1")["task"]["assignee"], "alice")

    def test_release_open_task_is_noop(self):
        self._new("ship it")
        self.assertTrue(self._release("#1").get("already_open"))

    def test_claim_done_task_dies(self):
        self._new("ship it")
        self._done("#1")
        self._dies(pp.cmd_task_claim, argparse.Namespace(
            ref="#1", channel=None, server=None))

    def test_assign_done_task_dies(self):
        self._new("ship it")
        self._done("#1")
        self._dies(pp.cmd_task_assign, argparse.Namespace(
            ref="#1", user="bob", channel=None, server=None))

    def test_release_done_task_dies(self):
        self._new("ship it")
        self._done("#1")
        self._dies(pp.cmd_task_release, argparse.Namespace(
            ref="#1", channel=None, server=None))

    def test_assign_strips_alias_token(self):
        self._new("ship it")
        r = self._assign("#1", "bob/Bot")
        self.assertEqual(r["task"]["assignee"], "bob")


# ---- DMs ---------------------------------------------------------------------------

class DMTests(GitRepoBase):
    def _dm(self, users, name=None):
        return self._run(pp.cmd_dm, argparse.Namespace(
            users=users, name=name, server=None))

    def test_create_private_channel(self):
        payload = self._dm(["bob"])
        self.assertTrue(payload["created"])
        self.assertEqual(payload["channel"], "dm-alice-bob")
        self.assertIn("NOT encrypted", payload["warning"])
        meta = json.loads((self.repo / "channels" / "dm-alice-bob" /
                           "channel.json").read_text())
        self.assertTrue(meta["private"])
        self.assertEqual(meta["members"], ["alice", "bob"])

    def test_members_sorted_and_include_me(self):
        payload = self._dm(["zed", "bob"])
        meta = json.loads((self.repo / "channels" / payload["channel"] /
                           "channel.json").read_text())
        self.assertEqual(meta["members"], ["alice", "bob", "zed"])

    def test_reopen_existing(self):
        self._dm(["bob"])
        payload = self._dm(["bob"])
        self.assertFalse(payload["created"])
        self.assertEqual(payload["members"], ["alice", "bob"])

    def test_needs_another_user(self):
        self._dies(pp.cmd_dm, argparse.Namespace(users=["  "], name=None,
                                                 server=None))

    def test_name_collision_with_public_channel_dies(self):
        self._dies(pp.cmd_dm, argparse.Namespace(users=["bob"],
                                                 name="general",
                                                 server=None))

    def test_existing_dm_non_member_dies(self):
        _mkchannel(self.repo, "dm-b-c", private=True,
                   members=["bob", "carol"])
        self._dies(pp.cmd_dm, argparse.Namespace(users=["bob"],
                                                 name="dm-b-c",
                                                 server=None))

    def test_dm_sets_active_channel(self):
        self._dm(["bob"])
        _, glob = pp._state_load()
        self.assertEqual(glob["channel"], "dm-alice-bob")

    def test_dm_hidden_from_non_member_everywhere(self):
        self._dm(["bob"])
        _mkpost(self.repo, "dm-alice-bob", _pid(1), "alice",
                "secret zebra payload")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "post")

        os.environ["PAIR_PRESSURE_AUTHOR"] = "carol"
        # channels listing
        chans = self._run(pp.cmd_channels, argparse.Namespace(
            no_pull=True, all=False, server=None))
        self.assertNotIn("dm-alice-bob",
                         [c["name"] for c in chans["channels"]])
        # direct read dies
        self._dies(pp.cmd_read, self._read_ns(target="dm-alice-bob"))
        # feed skips it
        feed = self._run(pp.cmd_read, self._read_ns())
        self.assertEqual(feed["posts"], [])
        # search skips it
        res = self._run(pp.cmd_search, argparse.Namespace(
            query="zebra", channel=None, author=None, limit=20,
            no_pull=True, server=None))
        self.assertEqual(res, [])
        # id lookup skips it
        self._reset_active()
        self.assertIsNone(pp._find_post_by_id(_pid(1)))

    def test_dm_visible_to_member(self):
        self._dm(["bob"])
        _mkpost(self.repo, "dm-alice-bob", _pid(1), "bob", "secret zebra")
        chans = self._run(pp.cmd_channels, argparse.Namespace(
            no_pull=True, all=False, server=None))
        names = [c["name"] for c in chans["channels"]]
        self.assertIn("dm-alice-bob", names)
        payload = self._run(pp.cmd_read,
                            self._read_ns(target="dm-alice-bob"))
        self.assertEqual(len(payload["posts"]), 1)


# ---- channels listing + visibility ---------------------------------------------------

class ChannelVisibilityTests(PPBase):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "plain"
        self.root = self.repo / "channels"
        _mkchannel(self.repo, "general")
        _mkchannel(self.repo, "old", archived=True)
        _mkchannel(self.repo, "dm-b-c", private=True,
                   members=["bob", "carol"])

    def test_channel_archived(self):
        self.assertTrue(pp._channel_archived(self.root / "old"))
        self.assertFalse(pp._channel_archived(self.root / "general"))

    def test_active_channel_dirs_excludes_archived_and_private(self):
        names = [p.name for p in pp._active_channel_dirs(self.root)]
        self.assertEqual(names, ["general"])

    def test_active_channel_dirs_includes_member_private(self):
        names = [p.name for p in pp._active_channel_dirs(self.root,
                                                         me="bob")]
        self.assertEqual(names, ["dm-b-c", "general"])

    def test_channel_visible_flags(self):
        self.assertTrue(pp._channel_visible(self.root / "general", "alice"))
        self.assertFalse(pp._channel_visible(self.root / "old", "alice"))
        self.assertTrue(pp._channel_visible(self.root / "old", "alice",
                                            include_archived=True))
        self.assertFalse(pp._channel_visible(self.root / "dm-b-c", "alice"))
        self.assertTrue(pp._channel_visible(self.root / "dm-b-c", "bob"))

    def test_hidden_channel_names_working_tree(self):
        hidden = pp._hidden_channel_names(self.repo, "alice")
        self.assertEqual(hidden, {"old", "dm-b-c"})
        hidden = pp._hidden_channel_names(self.repo, "bob")
        self.assertEqual(hidden, {"old"})


class ChannelsListTests(GitRepoBase):
    def test_lists_fields(self):
        _mkpost(self.repo, "general", _pid(5), "bob", "hi")
        payload = self._run(pp.cmd_channels, argparse.Namespace(
            no_pull=True, all=False, server=None))
        chans = payload["channels"]
        self.assertEqual(len(chans), 1)
        c = chans[0]
        self.assertEqual(c["name"], "general")
        self.assertEqual(c["post_count"], 1)
        self.assertEqual(c["last_activity"], pp._id_to_iso(_pid(5)))
        self.assertTrue(c["active"])
        self.assertFalse(c["private"])
        self.assertIn("srv #general", payload["where"])

    def test_archived_hidden_unless_all(self):
        _mkchannel(self.repo, "old", archived=True)
        payload = self._run(pp.cmd_channels, argparse.Namespace(
            no_pull=True, all=False, server=None))
        self.assertEqual([c["name"] for c in payload["channels"]],
                         ["general"])
        payload = self._run(pp.cmd_channels, argparse.Namespace(
            no_pull=True, all=True, server=None))
        self.assertIn("old", [c["name"] for c in payload["channels"]])

    def test_private_members_listed_for_member(self):
        _mkchannel(self.repo, "dm-alice-bob", private=True,
                   members=["alice", "bob"])
        payload = self._run(pp.cmd_channels, argparse.Namespace(
            no_pull=True, all=False, server=None))
        dm = [c for c in payload["channels"]
              if c["name"] == "dm-alice-bob"][0]
        self.assertTrue(dm["private"])
        self.assertEqual(dm["members"], ["alice", "bob"])


# ---- find post by id ------------------------------------------------------------------

class FindPostByIdTests(PPBase):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "plain"
        _mkchannel(self.repo, "general")
        _mkchannel(self.repo, "old", archived=True)
        self.pid_a = "20260512T143022100Z"
        self.pid_b = "20260513T153022200Z"
        _mkpost(self.repo, "general", self.pid_a, "alice", "first body")
        _mkpost(self.repo, "general", self.pid_b, "bob", "second body")
        _mkpost(self.repo, "old", "20260514T101010999Z", "carol", "hidden")
        pp._ACTIVE_REPO = self.repo
        pp._ACTIVE_SERVER = "srv"

    def test_exact_full_id(self):
        hit = pp._find_post_by_id(self.pid_a)
        self.assertEqual(hit["author"], "alice")
        self.assertIn("first body", hit["body"])
        self.assertEqual(hit["channel"], "general")

    def test_unique_short_handle(self):
        hit = pp._find_post_by_id("022100Z")
        self.assertEqual(hit["author"], "alice")

    def test_dot_prefix_stripped(self):
        hit = pp._find_post_by_id("·022100Z")
        self.assertEqual(hit["author"], "alice")

    def test_ambiguous_short_handle(self):
        hit = pp._find_post_by_id("3022")
        self.assertIn("ambiguous", hit)
        self.assertEqual(len(hit["ambiguous"]), 2)

    def test_archived_post_not_found(self):
        self.assertIsNone(pp._find_post_by_id("20260514T101010999Z"))

    def test_no_match(self):
        self.assertIsNone(pp._find_post_by_id("zzzzzz"))

    def test_empty_query(self):
        self.assertIsNone(pp._find_post_by_id("  "))


# ---- read verb -------------------------------------------------------------------------

class ReadVerbTests(GitRepoBase):
    def setUp(self):
        super().setUp()
        _mkchannel(self.repo, "dev")
        _mkpost(self.repo, "general", _pid(1), "bob", "first general")
        _mkpost(self.repo, "general", _pid(3), "bob", "third general")
        _mkpost(self.repo, "dev", _pid(2), "carol", "second dev")

    def test_feed_merges_channels_chronologically(self):
        payload = self._run(pp.cmd_read, self._read_ns())
        self.assertEqual(payload["view"], "feed")
        ids = [p["id"] for p in payload["posts"]]
        self.assertEqual(ids, [_pid(1), _pid(2), _pid(3)])
        self.assertEqual(payload["posts"][1]["channel"], "dev")

    def test_feed_limit_keeps_newest(self):
        payload = self._run(pp.cmd_read, self._read_ns(limit=2))
        ids = [p["id"] for p in payload["posts"]]
        self.assertEqual(ids, [_pid(2), _pid(3)])

    def test_channel_view(self):
        payload = self._run(pp.cmd_read, self._read_ns(target="general"))
        self.assertEqual(payload["view"], "channel")
        self.assertEqual(payload["channel"], "general")
        self.assertEqual(len(payload["posts"]), 2)

    def test_missing_channel_dies(self):
        self._dies(pp.cmd_read, self._read_ns(target="nope"))

    def test_since_filter(self):
        payload = self._run(pp.cmd_read,
                            self._read_ns(since=pp._id_to_iso(_pid(2))))
        ids = [p["id"] for p in payload["posts"]]
        self.assertEqual(ids, [_pid(2), _pid(3)])

    def test_message_view_full_body(self):
        long_body = "z" * 500
        _mkpost(self.repo, "general", _pid(9), "bob", long_body)
        chan = self._run(pp.cmd_read, self._read_ns(target="general"))
        row = [p for p in chan["posts"] if p["id"] == _pid(9)][0]
        self.assertTrue(row["truncated"])
        msg = self._run(pp.cmd_read, self._read_ns(message_id=_pid(9)))
        self.assertEqual(msg["view"], "message")
        self.assertIn(long_body, msg["post"]["body"])
        self.assertFalse(msg["post"]["truncated"])

    def test_message_view_no_match(self):
        payload = self._run(pp.cmd_read, self._read_ns(message_id="zzz"))
        self.assertEqual(payload["view"], "message")
        self.assertFalse(payload["matched"])

    def test_message_view_ambiguous(self):
        payload = self._run(pp.cmd_read, self._read_ns(message_id="2026"))
        self.assertEqual(payload["view"], "ambiguous_message")
        self.assertGreater(len(payload["matches"]), 1)

    def test_body_wrapped_untrusted(self):
        payload = self._run(pp.cmd_read, self._read_ns(target="general"))
        body = payload["posts"][0]["body"]
        self.assertTrue(body.startswith("<untrusted-content from='bob'>"))
        self.assertTrue(body.endswith("</untrusted-content>"))


# ---- search ------------------------------------------------------------------------------

class SearchTests(GitRepoBase):
    def setUp(self):
        super().setUp()
        _mkpost(self.repo, "general", _pid(1), "bob", "the zebra crossed")
        _mkpost(self.repo, "general", _pid(2), "carol", "nothing here")

    def _search(self, query, **kw):
        return self._run(pp.cmd_search, argparse.Namespace(
            query=query, channel=kw.get("channel"),
            author=kw.get("author"), limit=kw.get("limit", 20),
            no_pull=True, server=None))

    def test_finds_match_with_snippet(self):
        res = self._search("zebra")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["author"], "bob")
        self.assertEqual(res[0]["post_id"], _pid(1))
        self.assertIn("zebra", res[0]["snippet"])

    def test_author_filter(self):
        self.assertEqual(self._search("zebra", author="carol"), [])

    def test_channel_filter(self):
        _mkchannel(self.repo, "dev")
        _mkpost(self.repo, "dev", _pid(3), "bob", "zebra two")
        res = self._search("zebra", channel="dev")
        self.assertEqual([r["channel"] for r in res], ["dev"])

    def test_no_match(self):
        self.assertEqual(self._search("unicorn"), [])


# ---- send/read end-to-end (bare origin + two clones) -------------------------------------

class SendReadE2ETests(PPBase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git not on PATH")

    def setUp(self):
        super().setUp()
        self.origin = self.tmp / "origin.git"
        _git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        self.repo_a = _init_v3(self.tmp / "clone-a", name="srv")
        _git(self.repo_a, "remote", "add", "origin", str(self.origin))
        _git(self.repo_a, "push", "-u", "origin", "main")
        _git(self.tmp, "clone", str(self.origin), str(self.tmp / "clone-b"))
        self.repo_b = self.tmp / "clone-b"
        _git(self.repo_b, "config", "user.email", "t@t.t")
        _git(self.repo_b, "config", "user.name", "t")
        _git(self.repo_b, "config", "commit.gpgsign", "false")
        self._register("a", self.repo_a)
        self._register("b", self.repo_b)

    def _send(self, body, **kw):
        kw.setdefault("server", "a")
        return self._run(pp.cmd_send, self._send_ns(body, **kw))

    def test_send_writes_post_and_pushes(self):
        r = self._send("hello team")
        self.assertTrue(r["ok"])
        pf = self.repo_a / r["path"]
        self.assertTrue(pf.is_file())
        fm, body = pp.parse_slim(pf.read_text(encoding="utf-8"))
        self.assertEqual(fm["author"], "alice")
        self.assertEqual(body.strip(), "hello team")
        # pushed: origin main == local main
        local = _git(self.repo_a, "rev-parse", "main").stdout.strip()
        remote = _git(self.origin, "rev-parse", "main").stdout.strip()
        self.assertEqual(local, remote)

    def test_other_clone_sees_post_after_pull(self):
        self._send("cross-clone hello")
        payload = self._run(pp.cmd_read, self._read_ns(
            server="b", target="general", no_pull=False))
        self.assertEqual(len(payload["posts"]), 1)
        self.assertIn("cross-clone hello", payload["posts"][0]["body"])

    def test_send_updates_state(self):
        self._send("hi")
        _, glob = pp._state_load()
        self.assertEqual(glob["server"], "a")
        self.assertEqual(glob["channel"], "general")
        self.assertEqual(glob["source"], "send")

    def test_send_banner_on_stderr(self):
        self._reset_active()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            pp._capture(pp.cmd_send, self._send_ns("hi", server="a"))
        self.assertIn("a #general", err.getvalue())

    def test_reply_to_partial_id_resolves(self):
        r1 = self._send("root message")
        r2 = self._send("the reply", reply_to=r1["post_id"][-6:])
        pf = self.repo_a / r2["path"]
        fm, _ = pp.parse_slim(pf.read_text(encoding="utf-8"))
        self.assertEqual(fm["reply_to"], r1["post_id"])

    def test_reply_to_missing_dies(self):
        self._dies(pp.cmd_send,
                   self._send_ns("x", server="a", reply_to="zzzzzz"))

    def test_reply_to_ambiguous_dies(self):
        self._send("one")
        time.sleep(0.01)
        self._send("two")
        self._dies(pp.cmd_send,
                   self._send_ns("x", server="a", reply_to="2026"))

    def test_send_to_archived_channel_dies(self):
        meta_path = self.repo_a / "channels" / "general" / "channel.json"
        meta = json.loads(meta_path.read_text())
        meta["archived"] = True
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        _git(self.repo_a, "add", "-A")
        _git(self.repo_a, "commit", "-m", "archive")
        _git(self.repo_a, "push")
        self._dies(pp.cmd_send, self._send_ns("x", server="a"))

    def test_send_to_private_non_member_dies(self):
        _mkchannel(self.repo_a, "dm-b-c", private=True,
                   members=["bob", "carol"])
        _git(self.repo_a, "add", "-A")
        _git(self.repo_a, "commit", "-m", "dm")
        _git(self.repo_a, "push")
        self._dies(pp.cmd_send,
                   self._send_ns("x", server="a", channel="dm-b-c"))

    def test_empty_body_dies(self):
        self._dies(pp.cmd_send, self._send_ns("   \n", server="a"))

    def test_via_human_hides_alias(self):
        os.environ["PAIR_PRESSURE_ALIAS"] = "Echo"
        r = self._send("typed by hand", via="human")
        fm, _ = pp.parse_slim(
            (self.repo_a / r["path"]).read_text(encoding="utf-8"))
        self.assertEqual(fm["author"], "alice")
        self.assertIsNone(fm["alias"])
        self.assertEqual(fm["via"], "human")

    def test_alias_flag_on_send(self):
        r = self._send("ai post", alias="Zed")
        fm, _ = pp.parse_slim(
            (self.repo_a / r["path"]).read_text(encoding="utf-8"))
        self.assertEqual(fm["alias"], "Zed")

    def test_send_with_attach_flag(self):
        os.environ["PAIR_PRESSURE_ATTACH_ROOT"] = str(self.tmp)
        src = self.tmp / "notes.md"
        src.write_text("## notes\n", encoding="utf-8")
        r = self._send("with file", attachments=[str(src)])
        pid = r["post_id"]
        shard = (self.repo_a / "channels" / "general" / "posts" /
                 pp._shard_for(pid))
        self.assertTrue((shard / "attachments" / pid / "notes.md").is_file())
        body = (shard / f"{pid}.md").read_text(encoding="utf-8")
        self.assertIn("## Attachments", body)
        # surfaced on read
        payload = self._run(pp.cmd_read,
                            self._read_ns(server="a", target="general"))
        row = [p for p in payload["posts"] if p["id"] == pid][0]
        self.assertEqual(row["attachments"][0]["name"], "notes.md")

    def test_concurrent_sends_survive_rebase_retry(self):
        # A posts and pushes; B (stale) posts -- push_with_retry must
        # replay B's write on top of origin so both posts survive.
        self._send("from A")
        rb = self._run(pp.cmd_send, self._send_ns("from B", server="b"))
        self.assertTrue(rb["ok"])
        _git(self.repo_a, "pull", "--rebase")
        ch = self.repo_a / "channels" / "general"
        bodies = []
        for pf in pp._post_files(ch):
            _, body = pp.parse_slim(pf.read_text(encoding="utf-8"))
            bodies.append(body.strip())
        self.assertIn("from A", bodies)
        self.assertIn("from B", bodies)

    def test_compat_env_repo_send(self):
        # No registry, just PAIR_PRESSURE_REPO -> auto-registered "default".
        pp._servers_save({"schema_version": 2, "servers": []})
        os.environ["PAIR_PRESSURE_REPO"] = str(self.repo_a)
        r = self._run(pp.cmd_send, self._send_ns("compat hello"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["server"], "default")


class TaskRebaseReplayTests(PPBase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git not on PATH")

    def setUp(self):
        super().setUp()
        self.origin = self.tmp / "origin.git"
        _git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        self.repo_a = _init_v3(self.tmp / "clone-a", name="srv")
        _git(self.repo_a, "remote", "add", "origin", str(self.origin))
        _git(self.repo_a, "push", "-u", "origin", "main")
        _git(self.tmp, "clone", str(self.origin), str(self.tmp / "clone-b"))
        self.repo_b = self.tmp / "clone-b"
        _git(self.repo_b, "config", "user.email", "t@t.t")
        _git(self.repo_b, "config", "user.name", "t")
        _git(self.repo_b, "config", "commit.gpgsign", "false")
        self._register("a", self.repo_a)
        self._register("b", self.repo_b)

    def test_concurrent_task_new_both_survive(self):
        # A creates task 1 and pushes.
        ra = self._run(pp.cmd_task_new, argparse.Namespace(
            title="from-a", channel="general", server="a"))
        self.assertEqual(ra["task"]["id"], 1)
        # B is stale (cloned before A's push): cmd_task_new pulls first,
        # so exercise the raw push_with_retry replay path instead.
        pp._ACTIVE_SERVER = "b"
        pp._ACTIVE_REPO = self.repo_b
        ch = self.repo_b / "channels" / "general"

        def write_payload():
            data = pp._tasks_load(ch)
            tid = int(data["next_id"])
            task = {"id": tid, "title": "from-b", "status": "open"}
            data["tasks"].append(task)
            data["next_id"] = tid + 1
            pp.write_json(pp._tasks_path(ch), data)
            return {"task": task}

        info = pp.push_with_retry(write_payload, lambda i: "task from b")
        self.assertEqual(info["task"]["id"], 2)  # replayed on fresh tree
        # Both tasks live at origin.
        _git(self.repo_a, "pull", "--rebase")
        data = pp._tasks_load(self.repo_a / "channels" / "general")
        self.assertEqual({t["title"] for t in data["tasks"]},
                         {"from-a", "from-b"})
        self.assertEqual({t["id"] for t in data["tasks"]}, {1, 2})

    def test_task_new_on_pulled_clone_gets_next_id(self):
        self._run(pp.cmd_task_new, argparse.Namespace(
            title="from-a", channel="general", server="a"))
        rb = self._run(pp.cmd_task_new, argparse.Namespace(
            title="from-b", channel="general", server="b"))
        self.assertEqual(rb["task"]["id"], 2)

    def _write_post(self, ch, pid, body):
        shard = ch / "posts" / pp._shard_for(pid)
        shard.mkdir(parents=True, exist_ok=True)
        (shard / f"{pid}.md").write_text(
            f"---\nby: bob via=h\nrt: {pid}\n---\n\n{body}\n", encoding="utf-8")

    def test_push_retry_preserves_unpushed_offline_commit(self):
        # B has an offline post committed locally but never pushed.
        ch_b = self.repo_b / "channels" / "general"
        self._write_post(ch_b, "20260101T000000001Z", "offline")
        _git(self.repo_b, "add", "-A")
        _git(self.repo_b, "commit", "-m", "offline post")
        # A advances origin with a different post (distinct file).
        self._run(pp.cmd_send, self._send_ns("from A", server="a"))
        # B writes a new post and pushes: the first push is rejected (B is
        # behind), and the rebase-retry must replay BOTH B commits onto the
        # new tip rather than reset --hard nuking the offline one.
        pp._ACTIVE_SERVER = "b"
        pp._ACTIVE_REPO = self.repo_b

        def write_payload():
            self._write_post(ch_b, "20260101T000000002Z", "newpost")
            return {"post_id": "20260101T000000002Z"}

        pp.push_with_retry(write_payload, lambda i: "new post from b")
        # The offline post must have reached origin (pull A's clone to check).
        _git(self.repo_a, "pull", "--rebase")
        survived = (self.repo_a / "channels" / "general" / "posts"
                    / "2026-01" / "20260101T000000001Z.md")
        self.assertTrue(survived.exists(),
                        "offline post was lost on push-retry")


# ---- server verbs ---------------------------------------------------------------------

class ServerVerbTests(PPBase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git not on PATH")

    def _add(self, name, url="u", path=None, no_clone=False):
        return self._run(pp.cmd_server_add, argparse.Namespace(
            name=name, url=url, path=path, no_clone=no_clone))

    def test_adopt_registers_and_sets_default(self):
        repo = _init_v3(self.tmp / "chat", name="alpha")
        payload = self._add("alpha", path=str(repo))
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["default"])
        entry = pp._server_entry("alpha")
        self.assertEqual(Path(entry["path"]), repo.resolve())

    def test_second_add_keeps_first_default(self):
        r1 = _init_v3(self.tmp / "c1", name="alpha")
        r2 = _init_v3(self.tmp / "c2", name="beta")
        self._add("alpha", path=str(r1))
        payload = self._add("beta", path=str(r2))
        self.assertFalse(payload["default"])
        self.assertEqual(pp._servers_load()["default"], "alpha")

    def test_bad_name_dies(self):
        self._dies(pp.cmd_server_add, argparse.Namespace(
            name="Bad Name", url="u", path=None, no_clone=False))

    def test_duplicate_dies(self):
        repo = _init_v3(self.tmp / "chat", name="alpha")
        self._add("alpha", path=str(repo))
        self._dies(pp.cmd_server_add, argparse.Namespace(
            name="alpha", url="u", path=str(repo), no_clone=False))

    def test_adopt_non_git_path_dies(self):
        d = self.tmp / "not-git"
        d.mkdir()
        self._dies(pp.cmd_server_add, argparse.Namespace(
            name="x", url="u", path=str(d), no_clone=False))

    def test_adopt_v2_repo_dies(self):
        d = self.tmp / "v2"
        d.mkdir()
        _git(d, "init", "-b", "main")
        (d / ".pair-pressure").mkdir()
        (d / ".pair-pressure" / "schema-version").write_text("2\n")
        self._dies(pp.cmd_server_add, argparse.Namespace(
            name="x", url="u", path=str(d), no_clone=False))

    def test_clone_from_local_bare(self):
        origin = self.tmp / "origin.git"
        _git(self.tmp, "init", "--bare", "-b", "main", str(origin))
        src = _init_v3(self.tmp / "src", name="cl")
        _git(src, "remote", "add", "origin", str(origin))
        _git(src, "push", "-u", "origin", "main")
        payload = self._add("cl", url=str(origin))
        self.assertTrue(payload["ok"])
        dest = self.pp_home / "servers" / "cl"
        self.assertTrue((dest / ".git").exists())
        self.assertTrue((dest / ".pair-pressure" /
                         "schema-version").is_file())

    def test_clone_offline_dies(self):
        os.environ["PAIR_PRESSURE_OFFLINE"] = "1"
        self._dies(pp.cmd_server_add, argparse.Namespace(
            name="x", url="u", path=None, no_clone=False))

    def test_bootstraps_uninitialized_adopted_repo(self):
        d = self.tmp / "uninit"
        d.mkdir()
        _git(d, "init", "-b", "main")
        _git(d, "config", "user.email", "t@t.t")
        _git(d, "config", "user.name", "t")
        _git(d, "config", "commit.gpgsign", "false")
        # Force the bundled pp-init (an installed console script may be an
        # incompatible version).
        with unittest.mock.patch.object(pp.shutil, "which",
                                        return_value=None):
            payload = self._add("boot", path=str(d))
        self.assertTrue(payload["ok"])
        self.assertEqual(
            (d / ".pair-pressure" / "schema-version").read_text().strip(),
            "3")
        self.assertTrue((d / "channels" / "general" /
                         "channel.json").is_file())

    def test_server_list_rows(self):
        repo = _init_v3(self.tmp / "chat", name="alpha")
        self._add("alpha", path=str(repo))
        payload = self._run(pp.cmd_server_list, argparse.Namespace())
        self.assertEqual(payload["default"], "alpha")
        self.assertEqual(payload["active"], "alpha")
        row = payload["servers"][0]
        self.assertEqual(row["name"], "alpha")
        self.assertTrue(row["exists"])
        self.assertTrue(row["active"])

    def test_server_use_switches(self):
        r1 = _init_v3(self.tmp / "c1", name="alpha")
        r2 = _init_v3(self.tmp / "c2", name="beta")
        self._add("alpha", path=str(r1))
        self._add("beta", path=str(r2))
        payload = self._run(pp.cmd_server_use,
                            argparse.Namespace(name="beta"))
        self.assertEqual(payload["server"], "beta")
        self.assertEqual(pp.resolve_server_name(None)[0], "beta")

    def test_remove_without_yes_dies(self):
        repo = _init_v3(self.tmp / "chat", name="alpha")
        self._add("alpha", path=str(repo))
        self._dies(pp.cmd_server_remove, argparse.Namespace(
            name="alpha", yes=False, delete_clone=False))

    def test_remove_reassigns_default(self):
        r1 = _init_v3(self.tmp / "c1", name="alpha")
        r2 = _init_v3(self.tmp / "c2", name="beta")
        self._add("alpha", path=str(r1))
        self._add("beta", path=str(r2))
        payload = self._run(pp.cmd_server_remove, argparse.Namespace(
            name="alpha", yes=True, delete_clone=False))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["default"], "beta")
        self.assertIsNone(pp._server_entry("alpha"))

    def test_remove_unknown_dies(self):
        self._dies(pp.cmd_server_remove, argparse.Namespace(
            name="ghost", yes=True, delete_clone=False))

    def test_delete_clone_outside_home_refused(self):
        repo = _init_v3(self.tmp / "chat", name="alpha")
        self._add("alpha", path=str(repo))
        self._dies(pp.cmd_server_remove, argparse.Namespace(
            name="alpha", yes=True, delete_clone=True))
        self.assertTrue(repo.exists())


class ValidServerNameTests(unittest.TestCase):
    def test_accepts_simple(self):
        self.assertTrue(pp._valid_server_name("alpha"))
        self.assertTrue(pp._valid_server_name("a"))
        self.assertTrue(pp._valid_server_name("0abc"))
        self.assertTrue(pp._valid_server_name("team-1.alpha_beta"))

    def test_rejects_empty_and_none(self):
        self.assertFalse(pp._valid_server_name(""))
        self.assertFalse(pp._valid_server_name(None))

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


# ---- watcher scan ------------------------------------------------------------------------

class WatchScanTests(PPBase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git not on PATH")

    def setUp(self):
        super().setUp()
        os.environ["PAIR_PRESSURE_OFFLINE"] = "1"  # working-tree scan
        self.repo = _init_v3(self.tmp / "chat", name="srv")
        self._register("srv", self.repo)

    def test_baseline_on_first_sight_reports_nothing(self):
        _mkpost(self.repo, "general", _pid(1), "bob", "old backlog")
        state = {}
        self.assertEqual(pp._scan_server_new("srv", state), [])
        self.assertEqual(state["srv/general"], _pid(1))

    def test_detects_newer_post_by_other_author(self):
        _mkpost(self.repo, "general", _pid(1), "bob", "old")
        state = {"srv/general": _pid(1)}
        _mkpost(self.repo, "general", _pid(2), "bob", "fresh")
        new = pp._scan_server_new("srv", state)
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["post_id"], _pid(2))
        self.assertEqual(new[0]["author"], "bob")
        self.assertEqual(new[0]["server"], "srv")
        self.assertEqual(state["srv/general"], _pid(2))

    def test_skips_own_posts_but_advances_marker(self):
        state = {"srv/general": _pid(1)}
        _mkpost(self.repo, "general", _pid(2), "alice", "mine")
        self.assertEqual(pp._scan_server_new("srv", state), [])
        self.assertEqual(state["srv/general"], _pid(2))

    def test_skips_private_non_member_channel(self):
        _mkchannel(self.repo, "dm-b-c", private=True,
                   members=["bob", "carol"])
        _mkpost(self.repo, "dm-b-c", _pid(1), "bob", "secret")
        state = {"srv/dm-b-c": _pid(0)}
        self.assertEqual(pp._scan_server_new("srv", state), [])

    def test_skips_archived_channel(self):
        _mkchannel(self.repo, "old", archived=True)
        _mkpost(self.repo, "old", _pid(2), "bob", "in archive")
        state = {"srv/old": _pid(1)}
        self.assertEqual(pp._scan_server_new("srv", state), [])

    def test_does_not_prune_foreign_keys(self):
        state = {"legacy/server/channel/thread": "x"}
        _mkpost(self.repo, "general", _pid(1), "bob", "y")
        pp._scan_server_new("srv", state)
        self.assertIn("legacy/server/channel/thread", state)

    def test_unregistered_or_missing_clone_skipped(self):
        self.assertEqual(pp._scan_server_new("ghost", {}), [])
        self._register("gone", self.tmp / "nowhere")
        self.assertEqual(pp._scan_server_new("gone", {}), [])

    def test_online_scan_reads_origin_not_working_tree(self):
        os.environ.pop("PAIR_PRESSURE_OFFLINE", None)
        origin = self.tmp / "origin.git"
        _git(self.tmp, "init", "--bare", "-b", "main", str(origin))
        _git(self.repo, "remote", "add", "origin", str(origin))
        _mkpost(self.repo, "general", _pid(2), "bob", "pushed post")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "post")
        _git(self.repo, "push", "-u", "origin", "main")
        # an uncommitted working-tree post must NOT show online
        _mkpost(self.repo, "general", _pid(3), "bob", "local only")
        state = {"srv/general": _pid(1)}
        new = pp._scan_server_new("srv", state)
        self.assertEqual([n["post_id"] for n in new], [_pid(2)])


class UnreadVerbTests(GitRepoBase):
    def _unread(self, **kw):
        return self._run(pp.cmd_unread, argparse.Namespace(
            all=kw.get("all", False), since=kw.get("since"),
            ack=kw.get("ack", False), no_pull=True,
            server=kw.get("server")))

    def test_first_sight_baselines_to_zero(self):
        _mkpost(self.repo, "general", _pid(1), "bob", "backlog")
        payload = self._unread()
        self.assertEqual(payload["count"], 0)

    def test_counts_posts_after_marker(self):
        _mkpost(self.repo, "general", _pid(2), "bob", "fresh")
        pp._watch_state_save({"srv/general": _pid(1)})
        payload = self._unread()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["author"], "bob")

    def test_does_not_persist_marker(self):
        _mkpost(self.repo, "general", _pid(2), "bob", "fresh")
        pp._watch_state_save({"srv/general": _pid(1)})
        self._unread()
        self.assertEqual(pp._watch_state_load()["srv/general"], _pid(1))
        # still unread on the next call
        self.assertEqual(self._unread()["count"], 1)

    def test_since_mode_ignores_markers(self):
        _mkpost(self.repo, "general", _pid(1), "bob", "by bob")
        _mkpost(self.repo, "general", _pid(2), "alice", "by me")
        payload = self._unread(since="2026-01-01T00:00:00.000Z")
        self.assertEqual(payload["count"], 1)  # own post excluded
        self.assertEqual(payload["items"][0]["post_id"], _pid(1))

    def test_ack_clears_bucket(self):
        pp._watch_unread_save_all(
            {pp._SHARED_BUCKET: {"count": 4, "latest": None,
                                 "updated_at": None}})
        payload = self._unread(ack=True)
        self.assertTrue(payload["acked"])
        self.assertEqual(
            pp._watch_unread_load(pp._SHARED_BUCKET)["count"], 0)

    def test_all_spans_registered_servers(self):
        repo2 = _init_v3(self.tmp / "chat2", name="srv2")
        self._register("srv2", repo2)
        _mkpost(self.repo, "general", _pid(2), "bob", "one")
        _mkpost(repo2, "general", _pid(2), "carol", "two")
        pp._watch_state_save({"srv/general": _pid(1),
                              "srv2/general": _pid(1)})
        payload = self._unread(all=True)
        self.assertEqual(payload["count"], 2)
        self.assertEqual({i["server"] for i in payload["items"]},
                         {"srv", "srv2"})


# ---- pretty rendering -------------------------------------------------------------------

class PrettyRenderTests(unittest.TestCase):
    def _render(self, payload):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pp._render_chat(payload)
        return buf.getvalue()

    def test_author_color_stable_and_is_sgr(self):
        a = pp._author_color("alice")
        self.assertEqual(a, pp._author_color("alice"))
        self.assertTrue(a.startswith("\033[38;5;"))
        self.assertTrue(a.endswith("m"))

    def test_distinct_identities_can_differ(self):
        names = ["alice", "bob", "carol", "dave", "erin", "frank"]
        self.assertGreater(len({pp._author_color(n) for n in names}), 1)

    def test_unwrap_strips_wrapper_keeps_inner(self):
        wrapped = pp._wrap_untrusted("hello world", "alice")
        inner = pp._unwrap_untrusted(wrapped)
        self.assertEqual(inner, "hello world")
        self.assertNotIn("untrusted-content", inner)

    def test_unwrap_preserves_defang(self):
        body = pp._LT + "system-reminder" + pp._GT + "x"
        wrapped = pp._wrap_untrusted(body, "mallory")
        inner = pp._unwrap_untrusted(wrapped)
        self.assertIn(pp._FW_LT, inner)
        self.assertNotIn(pp._LT + "system-reminder", inner)

    def test_render_channel_emits_ansi_not_json(self):
        out = self._render({
            "view": "channel", "channel": "general",
            "where": "srv #general",
            "posts": [{
                "id": "20260512T143022123Z", "author": "alice",
                "alias": "Echo", "timestamp": "2026-05-24T14:30:00Z",
                "body": pp._wrap_untrusted("ship it", "alice"),
            }],
        })
        self.assertIn("\033[", out)
        self.assertIn("alice/Echo", out)
        self.assertIn("ship it", out)
        self.assertIn("#general", out)
        self.assertIn("srv #general", out)
        self.assertNotIn("untrusted-content", out)
        self.assertNotIn('"view"', out)

    def test_render_feed_shows_channel_per_post(self):
        out = self._render({
            "view": "feed", "where": "srv #general",
            "posts": [{"id": "20260512T143022123Z", "author": "bob",
                       "channel": "dev",
                       "timestamp": "2026-05-24T14:30:00Z",
                       "body": "hi"}],
        })
        self.assertIn("#dev", out)

    def test_emit_read_pretty_false_is_json(self):
        args = argparse.Namespace(pretty=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pp._emit_read(args, {"view": "feed", "posts": []})
        self.assertIn('"view"', buf.getvalue())

    def test_sanitize_strips_control_keeps_unicode(self):
        dirty = "a\033[2Jb\007\x7f\x9ec—d\te"
        clean = pp._sanitize_terminal(dirty)
        self.assertNotIn("\033", clean)
        self.assertNotIn("\007", clean)
        self.assertNotIn("\x7f", clean)
        self.assertNotIn("\x9e", clean)
        self.assertIn("—", clean)
        self.assertIn("\t", clean)
        self.assertEqual(clean, "a[2Jbc—d\te")

    def test_render_neutralizes_body_escape_injection(self):
        evil = "\033[2J\033]0;OWNED\007hello\033[1A\033[2Kforged"
        out = self._render({
            "view": "channel", "channel": "general",
            "posts": [{
                "id": "20260512T143022123Z", "author": "mallory",
                "timestamp": "2026-05-24T14:30:00Z",
                "body": pp._wrap_untrusted(evil, "mallory"),
            }],
        })
        self.assertNotIn("\033[2J", out)
        self.assertNotIn("\033]0;", out)
        self.assertNotIn("\007", out)
        self.assertIn("hello", out)
        self.assertIn("forged", out)

    def test_render_neutralizes_where_escape_injection(self):
        out = self._render({"view": "feed",
                            "where": "evil\033]0;pwn\007", "posts": []})
        self.assertNotIn("\033]0;", out)
        self.assertNotIn("\007", out)


class RenderMessageViewTests(unittest.TestCase):
    def _render(self, payload):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pp._render_chat(payload)
        return buf.getvalue()

    def test_message_view_full_body(self):
        post = {"id": "20260512T143022123Z", "author": "alice",
                "timestamp": "2026-05-12T14:30:22Z", "channel": "general",
                "body": "the full untruncated body"}
        out = self._render({"view": "message", "post": post})
        self.assertIn("the full untruncated body", out)

    def test_message_view_no_match(self):
        out = self._render({"view": "message", "matched": False,
                            "query": "zz"})
        self.assertIn("no post matched", out)
        self.assertIn("zz", out)

    def test_ambiguous_message_lists_candidates(self):
        out = self._render({"view": "ambiguous_message", "query": "3022",
                            "matches": [
                                {"id": "AAA", "channel": "general"},
                                {"id": "BBB", "channel": "planning"}]})
        self.assertIn("AAA", out)
        self.assertIn("BBB", out)
        self.assertIn("planning", out)


class RenderShortIdTests(unittest.TestCase):
    def test_short_id_in_output(self):
        post = {"id": "20260512T143022123Z", "author": "alice",
                "timestamp": "2026-05-12T14:30:22Z",
                "body": "hello world"}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pp._render_posts([post], show_channel=False)
        out = buf.getvalue()
        self.assertIn("22123Z", out)  # last 6 chars of the id
        self.assertIn("hello world", out)

    def test_reply_marker_in_output(self):
        post = {"id": "20260512T143022123Z", "author": "alice",
                "reply_to": "20260512T142811007Z",
                "timestamp": "2026-05-12T14:30:22Z", "body": "re"}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pp._render_posts([post], show_channel=False)
        self.assertIn("11007Z", buf.getvalue())  # last 6 chars of reply_to


# ---- attachments ------------------------------------------------------------------------

class ProcessAttachmentsTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.scratch = Path(self._td.name)
        self.tdir = self.scratch / "shard"
        self.tdir.mkdir()
        self.pid = "20260514T010101000Z"
        self._saved = os.environ.get("PAIR_PRESSURE_ATTACH_ROOT")
        os.environ["PAIR_PRESSURE_ATTACH_ROOT"] = str(self.scratch)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("PAIR_PRESSURE_ATTACH_ROOT", None)
        else:
            os.environ["PAIR_PRESSURE_ATTACH_ROOT"] = self._saved
        self._td.cleanup()

    def _file(self, name, content="x"):
        p = self.scratch / name
        p.write_text(content)
        return p

    def test_inline_token_replaced_with_link(self):
        f = self._file("notes.md", "hello")
        body = pp._process_attachments(f"see @@{f}", self.tdir, self.pid, [])
        self.assertIn(f"[notes.md](attachments/{self.pid}/notes.md)", body)
        self.assertTrue(
            (self.tdir / "attachments" / self.pid / "notes.md").is_file())

    def test_nonexistent_inline_token_left_untouched(self):
        body = pp._process_attachments(
            "see @@/does/not/exist and email a@b.c",
            self.tdir, self.pid, [])
        self.assertIn("@@/does/not/exist", body)
        self.assertIn("a@b.c", body)
        self.assertFalse((self.tdir / "attachments").exists())

    def test_inline_collision_within_post_suffixes(self):
        f = self._file("notes.md")
        body = pp._process_attachments(
            f"first @@{f} and second @@{f}", self.tdir, self.pid, [])
        self.assertIn(f"[notes.md](attachments/{self.pid}/notes.md)", body)
        self.assertIn(f"[notes-2.md](attachments/{self.pid}/notes-2.md)",
                      body)
        att = self.tdir / "attachments" / self.pid
        self.assertTrue((att / "notes.md").is_file())
        self.assertTrue((att / "notes-2.md").is_file())

    def test_trailing_punctuation_preserved_outside_link(self):
        f = self._file("notes.md")
        body = pp._process_attachments(
            f"see @@{f}. Some prose.", self.tdir, self.pid, [])
        self.assertIn(
            f"[notes.md](attachments/{self.pid}/notes.md). Some prose.",
            body)

    def test_attach_flag_appends_section(self):
        a = self._file("a.txt", "A")
        b = self._file("b.txt", "B")
        body = pp._process_attachments("body", self.tdir, self.pid,
                                       [str(a), str(b)])
        self.assertIn("## Attachments", body)
        self.assertIn(f"- [a.txt](attachments/{self.pid}/a.txt)", body)
        self.assertIn(f"- [b.txt](attachments/{self.pid}/b.txt)", body)

    def test_attach_flag_missing_path_dies(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pp._process_attachments("body", self.tdir, self.pid,
                                        ["/does/not/exist"])

    def test_no_attachments_is_passthrough(self):
        body = pp._process_attachments("plain body with no tokens",
                                       self.tdir, self.pid, [])
        self.assertEqual(body, "plain body with no tokens")
        self.assertFalse((self.tdir / "attachments").exists())


class ReadBodyTests(unittest.TestCase):
    def test_body_text_passthrough(self):
        ns = argparse.Namespace(body_text="pre-read", body_file="-")
        self.assertEqual(pp.read_body(ns), "pre-read")

    def test_stdin_dash(self):
        ns = argparse.Namespace(body_text=None, body_file="-")
        with unittest.mock.patch.object(pp.sys, "stdin",
                                        io.StringIO("from stdin")):
            self.assertEqual(pp.read_body(ns), "from stdin")

    def test_file_path(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "body.md"
            f.write_text("file body")
            saved = os.environ.get("PAIR_PRESSURE_ATTACH_ROOT")
            os.environ["PAIR_PRESSURE_ATTACH_ROOT"] = d
            try:
                ns = argparse.Namespace(body_text=None, body_file=str(f))
                self.assertEqual(pp.read_body(ns), "file body")
            finally:
                if saved is None:
                    os.environ.pop("PAIR_PRESSURE_ATTACH_ROOT", None)
                else:
                    os.environ["PAIR_PRESSURE_ATTACH_ROOT"] = saved

    def test_file_path_utf8(self):
        # A UTF-8 body file with non-ASCII must round-trip regardless of the
        # platform default encoding (cp1252 on Windows would mangle it).
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "body.md"
            f.write_bytes("café — 日本語 🚀".encode("utf-8"))
            saved = os.environ.get("PAIR_PRESSURE_ATTACH_ROOT")
            os.environ["PAIR_PRESSURE_ATTACH_ROOT"] = d
            try:
                ns = argparse.Namespace(body_text=None, body_file=str(f))
                self.assertEqual(pp.read_body(ns), "café — 日本語 🚀")
            finally:
                if saved is None:
                    os.environ.pop("PAIR_PRESSURE_ATTACH_ROOT", None)
                else:
                    os.environ["PAIR_PRESSURE_ATTACH_ROOT"] = saved


# ---- statusline wiring ------------------------------------------------------------------

class WireStatuslineQuietTests(PPBase):
    def setUp(self):
        super().setUp()
        self.claude = self.home / ".claude"
        self.claude.mkdir()
        self.settings = self.claude / "settings.json"

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
        self.assertFalse(pp._wire_statusline_quiet())

    def test_malformed_returns_false(self):
        self.settings.write_text("{not json", encoding="utf-8")
        self.assertFalse(pp._wire_statusline_quiet())

    def test_backup_written_once(self):
        self.settings.write_text("{}", encoding="utf-8")
        pp._wire_statusline_quiet()
        bak = self.settings.with_suffix(".json.pp.bak")
        self.assertTrue(bak.exists())
        self.assertEqual(bak.read_text(), "{}")


class EnsureWiredTests(PPBase):
    def setUp(self):
        super().setUp()
        self.claude = self.home / ".claude"
        self.claude.mkdir()
        self.settings = self.claude / "settings.json"
        self.sentinel = self.pp_home / "autowire.done"

    def _args(self, cmd="status"):
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
        pp._config_save({"watch": {"autowire": False}})
        pp._ensure_wired(self._args())
        self.assertEqual(self.settings.read_text(), "{}")

    def test_no_settings_skips_without_sentinel(self):
        pp._ensure_wired(self._args())
        self.assertFalse(self.sentinel.exists())

    def test_watch_command_skipped(self):
        self.settings.write_text("{}", encoding="utf-8")
        pp._ensure_wired(self._args(cmd="watch"))
        self.assertEqual(self.settings.read_text(), "{}")

    def test_daemon_env_skipped(self):
        os.environ["PAIR_PRESSURE_IS_WATCH_DAEMON"] = "1"
        self.settings.write_text("{}", encoding="utf-8")
        pp._ensure_wired(self._args())
        self.assertEqual(self.settings.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
