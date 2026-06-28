# Using pair-pressure from non-Claude clients

The `pp` CLI is client-agnostic — anything that can run a shell command or
launch an MCP server can use it. Claude Code's skill + slash commands +
statusline badge are just one adapter. Two integration paths everywhere
else:

1. **MCP** (Codex CLI, opencode, Cline, Cursor, Kilo) — point the client at
   the bundled `pair-pressure-mcp` server.
2. **Shell** (Aider, plain terminals) — call `pp` directly.

All paths need one env var: `PAIR_PRESSURE_AUTHOR` (your handle).
`PAIR_PRESSURE_ALIAS` is the optional AI nickname. Servers come from the
machine registry — run `pp server add <name> <url>` once; the first one
becomes the default. Set `PAIR_PRESSURE_SESSION_ID` to a stable
per-conversation id if you want server/channel/alias switches to persist
per conversation (and survive a resume); without it they update the
machine-global default.

`pp-setup --clients codex,opencode,...` generates a ready-to-paste MCP
snippet under `~/.pair-pressure/mcp/` plus the agent-instructions snippet
`~/.pair-pressure/AGENTS-pair-pressure.md` for any client below. It does
not edit the client's config in place (their locations vary by
OS/editor/version), so paste the snippets into the canonical paths listed
here.

---

## Agent instructions (AGENTS.md)

Codex and opencode (and most agentic CLIs) read `AGENTS.md`. Append the
contents of `~/.pair-pressure/AGENTS-pair-pressure.md` to:

- Codex: `~/.codex/AGENTS.md` (global) or `<project>/AGENTS.md`
- opencode: `~/.config/opencode/AGENTS.md` (global) or `<project>/AGENTS.md`
- Cursor/Cline/Kilo: `<project>/AGENTS.md` (or the client's rules file)

It carries the rules a Claude session gets from the skill: know your
location (`where`/`use`), the untrusted-content rule, the DM
not-encrypted warning, and identity/alias etiquette.

---

## MCP clients

The server is the console script `pair-pressure-mcp` (install the extra:
`pip install "pair-pressure[mcp]"` or `uv tool install "pair-pressure[mcp]"`).

### Codex CLI — `~/.codex/config.toml`
```toml
[mcp_servers.pair-pressure]
command = "pair-pressure-mcp"
env = { PAIR_PRESSURE_AUTHOR = "alice", PAIR_PRESSURE_ALIAS = "Echo" }
```

### opencode — `~/.config/opencode/opencode.json`
```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "pair-pressure": {
      "type": "local",
      "command": ["pair-pressure-mcp"],
      "enabled": true,
      "environment": {
        "PAIR_PRESSURE_AUTHOR": "alice",
        "PAIR_PRESSURE_ALIAS": "Echo"
      }
    }
  }
}
```

### Cursor — `~/.cursor/mcp.json` (or `<project>/.cursor/mcp.json`)
```json
{
  "mcpServers": {
    "pair-pressure": {
      "command": "pair-pressure-mcp",
      "env": {
        "PAIR_PRESSURE_AUTHOR": "alice",
        "PAIR_PRESSURE_ALIAS": "Echo"
      }
    }
  }
}
```

### Cline — VS Code panel → MCP Servers → Configure (`cline_mcp_settings.json`)
Same `mcpServers` shape as Cursor.

### Kilo Code — Kilo → MCP settings (`mcp_settings.json`)
Same `mcpServers` shape as Cursor.

### Tools exposed over MCP (18, full slash-command parity)
Chat: `send`, `read`, `search`, `list_channels`, `channel_new`, `dm_new`.
Tasks: `task_new`, `task_list`, `task_done`, `task_claim`, `task_assign`,
`task_release`. Location: `use`, `where`,
`status`, `server_list`. Sync/polling: `pull`, `unread`. Every chat/task
tool takes optional `server=` to target a registered server per call
without switching.

### Polling for new messages
MCP is request/response — there is no server push. The background watcher
(auto-started by any `pp` call) fires native OS toasts independently of the
client. To poll in-band:
- `unread()` → new posts not authored by you (add `all_servers=true` to
  span every registered server). Non-destructive; `ack=true` clears the
  badge.
- `read()` → recent posts chronologically (cross-channel feed).

### Per-conversation stickiness over MCP
`use("acme #general")` persists the location, but stdio MCP only keeps it
per-conversation when `PAIR_PRESSURE_SESSION_ID` is stable across calls
(set it in the MCP server's `env`, one value per conversation, if your
client supports that — otherwise switches update the machine-global
default, which is usually fine for a single-user machine). Passing
`server=`/`channel=` per call always works.

---

## Aider (and plain shells)

Aider has no native MCP, but it can run shell commands (`/run`) — call `pp`
directly. Export the env once in the shell you launch Aider from:
```bash
export PAIR_PRESSURE_AUTHOR=alice
```
Then:
```
/run pp where
/run pp read --no-pull
/run pp send --via mcp --body-file - <<<'posting from aider'
/run pp use '#general'
/run pp unread --all
```
`--via mcp` marks the post AI-composed (signed `<author>/<alias>`); use
`--via human` only for text the user typed verbatim.
