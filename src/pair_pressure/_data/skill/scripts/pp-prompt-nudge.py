"""pair-pressure UserPromptSubmit nudge (OPT-IN; INCURS TOKEN COST).

Claude Code injects this script's stdout into the model context. We emit ONE
short line only when there are unread pair-pressure messages, then clear the
counter so it fires once per batch (~15-25 tokens, only when there is news).
Nothing is printed when there is nothing new (0 tokens).

Stdlib only and no `pp` import -- see pp-statusline.py for the rationale.
Enable/disable via `pp watch wire [--nudge] [--undo]`.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def main():
    try:
        sys.stdin.buffer.read()
    except Exception:
        pass

    unread = Path.home() / ".pair-pressure" / "unread.json"
    try:
        root = json.loads(unread.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError):
        return
    if not isinstance(root, dict):
        return

    key = os.environ.get("PAIR_PRESSURE_SESSION_ID") or "__shared__"
    legacy_flat = "count" in root and "__shared__" not in root
    if legacy_flat:
        bucket = root if key == "__shared__" else None
    else:
        bucket = root.get(key)
    if not isinstance(bucket, dict):
        return
    try:
        count = int(bucket.get("count") or 0)
    except (TypeError, ValueError):
        return
    if count <= 0:
        return

    who, where = "someone", ""
    latest = bucket.get("latest")
    if isinstance(latest, dict):
        who = latest.get("author") or who
        if latest.get("channel"):
            where = " in #{}".format(latest["channel"])

    if count == 1:
        line = ("[pair-pressure] 1 new message from {}{} - run "
                "/pp-chat:read to view".format(who, where))
    else:
        line = ("[pair-pressure] {} new messages (latest from {}{}) - run "
                "/pp-chat:read to view".format(count, who, where))
    sys.stdout.buffer.write(line.encode("utf-8") + b"\n")

    # Ack THIS bucket only so other sessions keep their badges.
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    cleared = {"count": 0, "latest": None, "updated_at": now}
    if legacy_flat:
        # migrate-in-place: wrap legacy as __shared__ and reset it
        out = {"__shared__": cleared}
    else:
        out = dict(root)
        out[key] = cleared
    try:
        unread.write_text(json.dumps(out, separators=(",", ":")),
                          encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    main()
