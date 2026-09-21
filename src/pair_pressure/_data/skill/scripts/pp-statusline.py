"""pair-pressure statusline (0 LLM tokens).

Claude Code renders our stdout at the bottom; the model never sees it.
Composable: if another statusLine was configured before pp wired itself, we
run that prior command (its string is saved in settings.json
`_pp_prev_statusline`), feed it the same session JSON on stdin, and APPEND
the pp badge after its output -- so other statusline plugins keep working.
A failure in the prior command can never break our line (fails to empty).

Stdlib only and no `pp` import: this runs on every statusline refresh, so it
must start fast and must never break a session. Python rather than a shell
script because it is the one interpreter guaranteed present (it is how `pp`
itself runs) and it behaves identically on Windows, macOS and Linux.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# Windows: keep the child console-less. Without this, spawning the prior
# statusline flashes a visible command prompt on every refresh.
_CREATE_NO_WINDOW = 0x08000000


def _comspec():
    """Absolute cmd.exe. COMSPEC first, then the System32 literal that always
    exists -- bare "cmd.exe" is unresolvable when the host spawns this hook
    with a PATH stripped of System32."""
    exe = os.environ.get("COMSPEC")
    if exe and os.path.exists(exe):
        return exe
    sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
    cand = os.path.join(sysroot, "System32", "cmd.exe")
    return cand if os.path.exists(cand) else "cmd.exe"


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError):
        return None


def _unread(pp_home):
    """(count, author, channel) for this session's bucket.

    Bucket key is $PAIR_PRESSURE_SESSION_ID or '__shared__'. Tolerates the
    legacy flat shape ({count,latest,updated_at}) by treating it as
    __shared__.
    """
    root = _load(pp_home / "unread.json")
    if not isinstance(root, dict):
        return 0, None, None
    key = os.environ.get("PAIR_PRESSURE_SESSION_ID") or "__shared__"
    if "count" in root and "__shared__" not in root:
        bucket = root if key == "__shared__" else None
    else:
        bucket = root.get(key)
    if not isinstance(bucket, dict):
        return 0, None, None
    try:
        count = int(bucket.get("count") or 0)
    except (TypeError, ValueError):
        return 0, None, None
    latest = bucket.get("latest")
    if not isinstance(latest, dict):
        return count, None, None
    return count, latest.get("author"), latest.get("channel")


def _run_prev(prev, stdin_bytes):
    """Run the previously-configured statusline, forwarding the session JSON.

    Its stdout becomes the left-hand side of our line. Any failure yields ''
    so a broken prior command can never take the pp badge down with it.
    """
    try:
        if os.name == "nt":
            # `cmd /s /c "<command>"` strips exactly the first and last quote
            # and takes the rest verbatim, which is what survives prior
            # commands containing quoted paths with spaces. Resolve cmd.exe
            # absolutely: a hook spawned by the host can run with a PATH that
            # lacks System32, where bare "cmd.exe" is unresolvable.
            args = '"{}" /s /c "{}"'.format(_comspec(), prev)
            kwargs = {"creationflags": _CREATE_NO_WINDOW}
        else:
            args = ["/bin/sh", "-c", prev]
            kwargs = {}
        # A hung prior statusline must not freeze every refresh; on timeout
        # subprocess kills it and the broad except below yields ''.
        res = subprocess.run(
            args, input=stdin_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=5, **kwargs
        )
        return res.stdout.decode("utf-8", "replace").rstrip("\r\n")
    except Exception:
        return ""


def main():
    # Buffer stdin ONCE (it can only be read once). Claude Code feeds session
    # JSON; we don't use it ourselves, but the prior statusline might.
    try:
        stdin_bytes = sys.stdin.buffer.read()
    except Exception:
        stdin_bytes = b""

    pp_home = Path.home() / ".pair-pressure"

    cfg = _load(pp_home / "config.json")
    offline = bool(cfg.get("offline")) if isinstance(cfg, dict) else False

    count, who, where = _unread(pp_home)

    # Silent when nothing to report and online (lets the user reclaim screen).
    # Always shows when offline so the mode is visible.
    badge = ""
    if count > 0 or offline:
        parts = ["pp"]
        if offline:
            parts.append("(offline)")
        if count > 0:
            detail = ""
            if who and where:
                detail = " {} #{}".format(who, where)
            elif who:
                detail = " {}".format(who)
            elif where:
                detail = " #{}".format(where)
            parts.append("{} new{}".format(count, detail))
        badge = "[" + " ".join(parts) + "]"

    settings = _load(Path.home() / ".claude" / "settings.json")
    prev = ""
    if isinstance(settings, dict):
        prev = str(settings.get("_pp_prev_statusline") or "")
    prev_out = _run_prev(prev, stdin_bytes) if prev.strip() else ""

    if prev_out and badge:
        line = prev_out + " " + badge
    else:
        line = prev_out or badge
    # Claude Code accepts a blank statusline. stdout is None under a
    # GUI-subsystem interpreter when the host leaves it unredirected, and
    # closed or broken when the host tore the pipe down first. Nothing to
    # render to in any of those cases, and raising would break the session.
    try:
        sys.stdout.buffer.write(line.encode("utf-8") + b"\n")
    except (AttributeError, ValueError, OSError):
        pass


if __name__ == "__main__":
    main()
