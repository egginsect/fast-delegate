#!/usr/bin/env python3
"""fast-delegate quota statusLine helper: a transparent wrapper around Claude Code's
statusLine command.

Claude Code (v2.1.80+) passes one JSON object on stdin to the statusLine command on every render,
including (when available) `rate_limits.five_hour` / `rate_limits.seven_day`, each with
`used_percentage` and `resets_at`. This script reads that stdin payload, writes a normalized
snapshot with an `observed_at` timestamp to `$FAST_DELEGATE_STATE/quota/claude.json` (never
polling any usage API -- fdel.py's `route` only ever reads that file back), then re-execs your
existing statusLine command with the same stdin and prints its output unchanged, so the visible
status line is untouched.

Usage (chain your existing statusLine command after `--`):
  quota_statusline.py -- <your-existing-statusline-command> [args...]

With no command after `--`, this still has to produce something the status line can show (a
statusLine command is expected to print a line, and Claude Code runs it from an arbitrary cwd, so
silence there reads as broken, not "no command configured") -- it prints a minimal one-line quota
summary (or a short placeholder when no rate_limits were present in this payload) instead.

settings.json -- use an ABSOLUTE path; the statusLine command runs from an arbitrary cwd, not your
repo checkout, so a relative path (e.g. "skills/fast-delegate/scripts/quota_statusline.py") fails:
  {
    "statusLine": {
      "type": "command",
      "command": "python3 ~/.claude/skills/fast-delegate/scripts/quota_statusline.py -- <your-existing-statusline-command>"
    }
  }
`~/.claude/skills/fast-delegate` is where `install.sh claude` puts this skill (or
`$CLAUDE_CONFIG_DIR/skills/fast-delegate` when that env var is set). Installed as a Claude Code
plugin instead, the script lives under your plugins directory rather than `~/.claude/skills`
(e.g. `~/.claude/plugins/<marketplace>/fast-delegate/skills/fast-delegate/scripts/`) --
`find ~/.claude/plugins -name quota_statusline.py` locates the exact path to use.

Stdlib only; no network calls.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def quota_dir():
    return Path(os.environ.get("FAST_DELEGATE_STATE", Path.home() / ".local/state/fast-delegate")) / "quota"


def extract_windows(payload):
    """{"five_hour": {...}, "seven_day": {...}} from the statusLine stdin payload's
    `rate_limits.five_hour` / `rate_limits.seven_day`, or None when neither is present. Never
    raises on an unexpected shape -- a statusLine payload from an older Claude Code build simply
    has no `rate_limits` at all."""
    rl = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(rl, dict):
        return None
    windows = {}
    for key in ("five_hour", "seven_day"):
        w = rl.get(key)
        if isinstance(w, dict):
            used = w.get("used_percentage")
            windows[key] = {
                "used_percentage": used if isinstance(used, (int, float)) and not isinstance(used, bool) else None,
                "resets_at": w.get("resets_at"),
            }
    return windows or None


def minimal_line(windows):
    """A short one-line status when there is no chained command to defer to -- never empty, so a
    bare `quota_statusline.py` invocation (e.g. while testing the settings.json snippet before
    wiring in a real statusLine command) doesn't read as a broken/blank status line."""
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = (windows or {}).get(key)
        used = w.get("used_percentage") if isinstance(w, dict) else None
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            parts.append(f"{label} {used:.0f}%")
    return "quota " + " · ".join(parts) if parts else "fast-delegate: quota tracking active"


def write_snapshot(windows, observed_at):
    """Write the normalized claude.json snapshot atomically. Never raises -- a write failure must
    never break the statusLine (the caller wraps this in try/except)."""
    if not windows:
        return
    d = quota_dir()
    d.mkdir(parents=True, exist_ok=True)
    snapshot = {"pool": "claude", "basis": "claude-statusline", "observed_at": observed_at, **windows}
    target = d / "claude.json"
    tmp = d / f"claude.json.tmp{os.getpid()}"
    tmp.write_text(json.dumps(snapshot))
    tmp.replace(target)


def main(argv):
    chain = argv[1:]
    if chain and chain[0] == "--":
        chain = chain[1:]
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    windows = extract_windows(payload)
    try:
        write_snapshot(windows, time.time())
    except OSError:
        pass  # never let a quota-file write failure break the status line
    if not chain:
        print(minimal_line(windows))
        return 0
    try:
        result = subprocess.run(chain, input=raw, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 0  # the chained command itself is not this script's responsibility to diagnose
    sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
