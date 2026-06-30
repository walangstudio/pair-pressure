"""Hermetic tests for the MCP shim (mcp/server.py).

The mcp SDK is stubbed out with unittest.mock so the suite runs without
`pip install .[mcp]`.  Tests cover:
  - tool registration (all 18 tools must appear)
  - _server_args() helper
  - _run() JSON parsing and error surface
  - key tool functions: that they marshal Python args to CLI argv correctly

Run from the scripts/ directory (or via the repo-root pytest invocation):
    python -m pytest tests/test_mcp_server.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
# HERE = .../skill/scripts/tests/
# HERE.parents[1] = .../skill/
MCP_SERVER = HERE.parents[1] / "mcp" / "server.py"

# ---------------------------------------------------------------------------
# Load server.py once at import time, with the mcp SDK mocked.
#
# server.py does `from mcp.server.fastmcp import FastMCP` at module scope,
# then `mcp = FastMCP("pair-pressure")`, then `@mcp.tool()` decorates each
# function.  We provide a fake FastMCP that records the decorated functions
# so the test suite can inspect and call them directly.
# ---------------------------------------------------------------------------

def _load_server():
    """Return (module, tool_registry) with the mcp SDK mocked."""
    registry: dict = {}

    fake_inst = mock.MagicMock()

    def _tool():
        def _wrap(fn):
            registry[fn.__name__] = fn
            return fn
        return _wrap

    fake_inst.tool = _tool

    fastmcp_mod = mock.MagicMock()
    fastmcp_mod.FastMCP = mock.MagicMock(return_value=fake_inst)

    stubs = {
        "mcp": mock.MagicMock(),
        "mcp.server": mock.MagicMock(),
        "mcp.server.fastmcp": fastmcp_mod,
    }
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("_pp_mcp_server", MCP_SERVER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod, registry


_MOD, _REGISTERED = _load_server()

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_EXPECTED_TOOLS = frozenset([
    "pull", "read", "search", "list_channels", "unread",
    "send", "channel_new", "dm_new",
    "task_new", "task_list", "task_done", "task_claim", "task_assign",
    "task_release",
    "use", "where", "status", "server_list",
])


def _ok(payload: dict):
    r = mock.MagicMock()
    r.returncode = 0
    r.stdout = json.dumps(payload)
    r.stderr = ""
    return r


def _err(stderr_json: dict | None = None, stderr_text: str = "",
         stdout_text: str = ""):
    r = mock.MagicMock()
    r.returncode = 1
    r.stdout = stdout_text
    r.stderr = json.dumps(stderr_json) if stderr_json is not None else stderr_text
    return r


# ---------------------------------------------------------------------------

class ToolRegistrationTests(unittest.TestCase):
    """All expected tools must be registered; no extras."""

    def test_all_expected_tools_registered(self):
        missing = _EXPECTED_TOOLS - set(_REGISTERED)
        self.assertEqual(missing, frozenset(), f"missing tools: {missing}")

    def test_no_unexpected_tools(self):
        extra = set(_REGISTERED) - _EXPECTED_TOOLS
        self.assertEqual(extra, set(), f"unexpected tools: {extra}")

    def test_tool_count(self):
        self.assertEqual(len(_REGISTERED), len(_EXPECTED_TOOLS))


class ServerArgsTests(unittest.TestCase):
    def test_with_name(self):
        self.assertEqual(_MOD._server_args("prod"), ["--server", "prod"])

    def test_none_returns_empty(self):
        self.assertEqual(_MOD._server_args(None), [])

    def test_empty_string_returns_empty(self):
        self.assertEqual(_MOD._server_args(""), [])


class RunHelperTests(unittest.TestCase):
    """_run() parses stdout JSON on success; surfaces errors on failure."""

    def _call(self, fake_result, *args, body=None):
        with mock.patch("subprocess.run", return_value=fake_result):
            return _MOD._run(*args, body=body)

    def test_success_returns_parsed_json(self):
        r = self._call(_ok({"ok": True}), "status")
        self.assertEqual(r, {"ok": True})

    def test_error_stderr_json_returned_as_dict(self):
        r = self._call(_err(stderr_json={"error": "no server"}), "status")
        self.assertEqual(r, {"error": "no server"})

    def test_error_plain_stderr_wrapped_in_error_key(self):
        r = self._call(_err(stderr_text="something broke"), "status")
        self.assertEqual(r, {"error": "something broke"})

    def test_error_empty_stderr_falls_back_to_stdout(self):
        r = self._call(_err(stdout_text="partial output"), "status")
        self.assertEqual(r, {"error": "partial output"})

    def test_cli_args_forwarded_to_subprocess(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return _ok({})

        with mock.patch("subprocess.run", side_effect=fake_run):
            _MOD._run("send", "--body-file", "-")

        self.assertIn("send", captured["argv"])
        self.assertIn("--body-file", captured["argv"])

    def test_body_passed_as_stdin_input(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["input"] = kw.get("input")
            return _ok({})

        with mock.patch("subprocess.run", side_effect=fake_run):
            _MOD._run("send", "--body-file", "-", body="hello team")

        self.assertEqual(captured["input"], "hello team")

    def test_none_body_sends_empty_string(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["input"] = kw.get("input")
            return _ok({})

        with mock.patch("subprocess.run", side_effect=fake_run):
            _MOD._run("status")

        self.assertEqual(captured["input"], "")


class ToolArgMarshalTests(unittest.TestCase):
    """Key tools marshal their Python args to CLI argv correctly."""

    def setUp(self):
        self._calls: list[dict] = []

        def capture(*args, body=None):
            self._calls.append({"args": list(args), "body": body})
            return {}

        self._patch = mock.patch.object(_MOD, "_run", side_effect=capture)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _last(self):
        return self._calls[-1]

    # ---- send ----

    def test_send_pipes_body_via_stdin(self):
        _REGISTERED["send"](body="hello", server=None)
        c = self._last()
        self.assertEqual(c["body"], "hello")
        self.assertIn("--body-file", c["args"])

    def test_send_sets_via_mcp(self):
        _REGISTERED["send"](body="x", server=None)
        c = self._last()
        self.assertIn("--via", c["args"])
        self.assertEqual(c["args"][c["args"].index("--via") + 1], "mcp")

    def test_send_optional_args_absent_when_not_given(self):
        _REGISTERED["send"](body="hi", server=None)
        c = self._last()
        self.assertNotIn("--reply-to", c["args"])
        self.assertNotIn("--alias", c["args"])
        self.assertNotIn("--model", c["args"])

    def test_send_reply_to_and_alias_forwarded(self):
        _REGISTERED["send"](body="r", reply_to="abc123", alias="Echo", server=None)
        c = self._last()
        self.assertIn("--reply-to", c["args"])
        self.assertIn("abc123", c["args"])
        self.assertIn("--alias", c["args"])
        self.assertIn("Echo", c["args"])

    # ---- read ----

    def test_read_no_args_uses_read_verb(self):
        _REGISTERED["read"]()
        c = self._last()
        self.assertIn("read", c["args"])

    def test_read_channel_positional(self):
        _REGISTERED["read"](channel="general")
        c = self._last()
        self.assertIn("general", c["args"])

    def test_read_message_flag(self):
        _REGISTERED["read"](message="abc123")
        c = self._last()
        self.assertIn("--message", c["args"])
        self.assertIn("abc123", c["args"])

    def test_read_limit_forwarded(self):
        _REGISTERED["read"](limit=5)
        c = self._last()
        self.assertIn("--limit", c["args"])
        self.assertIn("5", c["args"])

    # ---- channels ----

    def test_list_channels_archived_flag_when_requested(self):
        _REGISTERED["list_channels"](include_archived=True)
        self.assertIn("--all", self._last()["args"])

    def test_list_channels_no_archived_flag_by_default(self):
        _REGISTERED["list_channels"]()
        self.assertNotIn("--all", self._last()["args"])

    # ---- search ----

    def test_search_forwards_query_and_filters(self):
        _REGISTERED["search"](query="oauth", channel="general",
                               author="alice", server=None)
        c = self._last()
        self.assertIn("--query", c["args"])
        self.assertIn("oauth", c["args"])
        self.assertIn("--channel", c["args"])
        self.assertIn("--author", c["args"])

    # ---- unread ----

    def test_unread_ack_flag(self):
        _REGISTERED["unread"](ack=True)
        self.assertIn("--ack", self._last()["args"])

    def test_unread_all_servers_flag(self):
        _REGISTERED["unread"](all_servers=True)
        self.assertIn("--all", self._last()["args"])

    # ---- tasks ----

    def test_task_claim_forwards_ref(self):
        _REGISTERED["task_claim"](ref="#1", server=None)
        c = self._last()
        self.assertIn("task", c["args"])
        self.assertIn("claim", c["args"])
        self.assertIn("#1", c["args"])

    def test_task_assign_forwards_user(self):
        _REGISTERED["task_assign"](ref="#2", user="bob", server=None)
        c = self._last()
        self.assertIn("assign", c["args"])
        self.assertIn("bob", c["args"])

    def test_task_release_forwards_ref(self):
        _REGISTERED["task_release"](ref="#3", server=None)
        c = self._last()
        self.assertIn("release", c["args"])
        self.assertIn("#3", c["args"])

    # ---- dm ----

    def test_dm_new_with_name(self):
        _REGISTERED["dm_new"](users=["bob", "carol"], name="the-team",
                               server=None)
        c = self._last()
        self.assertIn("dm", c["args"])
        self.assertIn("--name", c["args"])
        self.assertIn("the-team", c["args"])
        self.assertIn("bob", c["args"])

    # ---- use ----

    def test_use_splits_whitespace_target(self):
        _REGISTERED["use"](target="myserver #general")
        c = self._last()
        self.assertIn("use", c["args"])
        self.assertIn("myserver", c["args"])
        self.assertIn("#general", c["args"])

    def test_use_empty_target_returns_error_without_calling_run(self):
        result = _REGISTERED["use"](target="   ")
        self.assertIn("error", result)
        self.assertEqual(self._calls, [])

    # ---- server scoping ----

    def test_server_arg_appended_when_given(self):
        _REGISTERED["pull"](server="remote-srv")
        c = self._last()
        self.assertIn("--server", c["args"])
        self.assertIn("remote-srv", c["args"])

    def test_no_server_arg_for_server_less_tools(self):
        _REGISTERED["where"]()
        self.assertNotIn("--server", self._last()["args"])

    def test_server_list_verb_args(self):
        _REGISTERED["server_list"]()
        c = self._last()
        self.assertIn("server", c["args"])
        self.assertIn("list", c["args"])


if __name__ == "__main__":
    unittest.main()
