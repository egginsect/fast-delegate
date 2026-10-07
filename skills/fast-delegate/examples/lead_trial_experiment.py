#!/usr/bin/env python3
"""Headless lead-trial runner: does a delegating lead deliver a task, at what cost?

Each trial runs one isolated headless `claude -p` lead that must deliver a coding
task by delegating to subagents, grades the result with a locked hidden oracle
and reprices every model's tokens from the fast-delegate catalog.

  run        one trial (arm skill|noskill)         -> <out>/<arm>-<trial>/result.json + <out>/trials.jsonl
  smoke      live self-check with a cheap lead     -> proves spawns, pricing and discovery work end to end
  summarize  aggregate <out>/trials.jsonl per arm

Isolation per trial: a git clone of the base repo is the lead's cwd; CLAUDE_CONFIG_DIR holds
only a credentials symlink, a minimal settings.json, the proxy-* agent files (never proxy-self)
and, in the skill arm only, a copy of skills/fast-delegate (without examples/); no CLAUDE.md,
hooks, plugins or memory. FAST_DELEGATE_STATE is a per-trial seeded copy of the real state, so
the real ledger is never written. fdel.py resolves agents from $CLAUDE_CONFIG_DIR/agents plus
<cwd>/.claude/agents (fdel.py find_agent_files), so it sees the isolated agents, not
~/.claude/agents; `smoke` proves this on a live run.

Measurement hazards found while building this (live, claude 2.1.287 through a proxy
session), handled below and covered by tests:
  * A proxy-* subagent spawned with a placeholder `model` (the agent files say to pass
    "haiku") is billed by the CLI under the placeholder in modelUsage: the routed model's
    tokens are lumped into the placeholder's entry. The stream/transcripts still show the
    served model, so `attribute_usage` subtracts the transcript-visible native usage and
    assigns the remainder to the served routes. Spawning a proxy-* agent without `model`
    gives it its own modelUsage key, as does any non-placeholder model.
  * The CLI's own USD for non-Anthropic models is not a catalog price; everything is repriced.
  * A proxy launcher may regenerate $CLAUDE_CONFIG_DIR/agents from the proxy's current
    catalog, so the agents the lead sees can differ from the copied ones; pricing
    therefore runs `fdel.py discover` after the run.

Secrets: TYPESAFE_API_KEY, TYPESAFE_API_KEY_OP_REF, OP_SERVICE_ACCOUNT_TOKEN and the gateway
token are passed through by name only; their values are replaced by [REDACTED:NAME] in every
file this tool writes and never printed.
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SKILL_DIR = HERE.parent
CACHE_ROOT = Path(os.environ.get("LEAD_TRIAL_HOME") or Path.home() / ".cache" / "lead-trial")
LEAD_TOOLS = "Agent,Task,Read,Write,Edit,Bash,Glob,Grep,Skill"
DEFAULT_TIMEOUT_S = 2700
GRADE_TIMEOUT_S = 600
DISCOVER_TIMEOUT_S = 180
ARMS = ("skill", "noskill")
GRADE_CMD = ["uv", "run", "--with", "pytest", "python", "-m", "pytest", "-q"]

JEV_ENV = ("TYPESAFE_API_KEY", "TYPESAFE_API_KEY_OP_REF", "OP_SERVICE_ACCOUNT_TOKEN")
GATEWAY_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
               "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
               "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL")
SECRET_ENV = JEV_ENV + ("ANTHROPIC_AUTH_TOKEN",)
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "SHELL")
TOKEN_FIELDS = ("input", "cached_input", "cache_write", "output")
COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "*.pyc", ".mypy_cache")

FINAL_RE = re.compile(r"^[\s>*`_#-]*FINAL:\s*[*`_]*\s*(NOT_ACCEPTED|ACCEPTED)\b", re.M)
FDEL_SUB_RE = re.compile(r"fdel\.py\s+([A-Za-z_-]+)")
PROXY_PREFIX_RE = re.compile(r"^[a-z]+-[a-z]+-[a-z]+--")


class TrialError(RuntimeError):
    """A condition that makes a trial untrustworthy; fail loudly rather than record zeros."""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def zero_tokens():
    return {f: 0 for f in TOKEN_FIELDS}


def add_tokens(a, b):
    return {f: (a.get(f, 0) or 0) + (b.get(f, 0) or 0) for f in TOKEN_FIELDS}


def has_tokens(t):
    return any((t.get(f, 0) or 0) > 0 for f in TOKEN_FIELDS)


# --------------------------------------------------------------------------- secrets

def secrets_from_env(env):
    """{NAME: value} for every secret env var that is set (>= 8 chars so redaction is meaningful)."""
    return {n: env[n] for n in SECRET_ENV if env.get(n) and len(env[n]) >= 8}


def redact(text, secrets):
    for name, value in secrets.items():
        text = text.replace(value, f"[REDACTED:{name}]")
    return text


def redact_file(path, secrets):
    p = Path(path)
    if secrets and p.exists():
        raw = p.read_text(errors="replace")
        clean = redact(raw, secrets)
        if clean != raw:
            p.write_text(clean)


def write_text(path, text, secrets):
    Path(path).write_text(redact(text, secrets))


# --------------------------------------------------------------------------- answer-key lock
# Port of bench/run.py locked_keys/recover_stale_lock (git show 643ad9f:bench/run.py), extended
# so several trial processes can lock the same oracle at once: each root keeps its ORIGINAL
# modes plus a holder list in one flock-guarded state file; the modes are restored when the last
# live holder leaves, and a holder whose process died is pruned (and its roots restored) by the
# next locker or by recover_stale_lock().

def _lock_dir(state_dir=None):
    d = Path(state_dir) if state_dir else CACHE_ROOT / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextlib.contextmanager
def _guard(state_dir):
    with open(_lock_dir(state_dir) / "guard.lock", "a+") as g:
        fcntl.flock(g, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(g, fcntl.LOCK_UN)


def _load_state(state_dir):
    p = _lock_dir(state_dir) / "state.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"roots": {}, "holders": {}}


def _save_state(state_dir, state):
    p = _lock_dir(state_dir) / "state.json"
    if not state["roots"] and not state["holders"]:
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
        return
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, p)


def _snapshot_modes(root):
    """[(path, mode)] parents before children; symlinks skipped; a file root is just itself."""
    root = str(root)
    out = [(root, os.stat(root).st_mode & 0o7777)]
    if os.path.isdir(root) and not os.path.islink(root):
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath != root:
                out.append((dirpath, os.stat(dirpath).st_mode & 0o7777))
            for n in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
                p = os.path.join(dirpath, n)
                if not os.path.islink(p):
                    out.append((p, os.lstat(p).st_mode & 0o7777))
    return out


def _restore_modes(entries):
    for path, mode in entries:  # parents first so children become reachable
        if os.path.lexists(path) and not os.path.islink(path):
            os.chmod(path, mode)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _release(state, holder):
    info = state["holders"].pop(holder, None)
    for root in (info or {}).get("roots", []):
        entry = state["roots"].get(root)
        if not entry:
            continue
        entry["holders"] = [h for h in entry["holders"] if h != holder]
        if not entry["holders"]:
            _restore_modes([tuple(e) for e in entry["entries"]])
            del state["roots"][root]


def _prune_dead(state):
    dead = [h for h, info in state["holders"].items() if not _pid_alive(info["pid"])]
    for h in dead:
        _release(state, h)
    return dead


def recover_stale_lock(state_dir=None):
    """Restore modes left behind by holders that died before their finally block ran.
    Returns the list of recovered holder ids."""
    with _guard(state_dir):
        state = _load_state(state_dir)
        dead = _prune_dead(state)
        _save_state(state_dir, state)
        return dead


def _drop_nested(paths):
    """Existing, de-duplicated absolute paths with any path inside another one removed."""
    abs_paths = sorted({os.path.abspath(str(p)) for p in paths if os.path.lexists(str(p))})
    keep = []
    for p in abs_paths:
        if not any(p == k or p.startswith(k.rstrip(os.sep) + os.sep) for k in keep):
            keep.append(p)
    return keep


@contextlib.contextmanager
def locked_keys(paths, state_dir=None):
    """chmod 000 everything under `paths` for the block; ORIGINAL modes restored in finally,
    or by the next locker/recover_stale_lock if this process is killed first."""
    roots = _drop_nested(paths)
    holder = f"{os.getpid()}:{uuid.uuid4().hex}"
    fresh = []
    with _guard(state_dir):
        state = _load_state(state_dir)
        _prune_dead(state)
        for root in roots:
            if root in state["roots"]:
                state["roots"][root]["holders"].append(holder)
            else:
                state["roots"][root] = {"entries": _snapshot_modes(root), "holders": [holder]}
                fresh.append(root)
        state["holders"][holder] = {"pid": os.getpid(), "roots": roots}
        _save_state(state_dir, state)  # record the originals BEFORE touching any mode
        for root in fresh:
            for path, _ in reversed(state["roots"][root]["entries"]):  # children first
                if not os.path.islink(path):
                    os.chmod(path, 0o000)
    try:
        yield roots
    finally:
        with _guard(state_dir):
            state = _load_state(state_dir)
            _release(state, holder)
            _save_state(state_dir, state)


def copy_locked_tree(src, dest, state_dir=None):
    """Copy `src` even when another running trial holds it at mode 000: under the lock guard the
    modes are put back for the duration of the copy and re-applied straight after (a window of
    milliseconds). Without a holder it is a plain copytree."""
    root = os.path.abspath(str(src))
    with _guard(state_dir):
        state = _load_state(state_dir)
        _prune_dead(state)
        _save_state(state_dir, state)
        entry = state["roots"].get(root)
        if entry:
            _restore_modes([tuple(e) for e in entry["entries"]])
        try:
            shutil.copytree(src, dest)
        finally:
            if entry:
                for path, _ in reversed(entry["entries"]):
                    if not os.path.islink(path):
                        os.chmod(path, 0o000)


# --------------------------------------------------------------------------- isolation

def render_prompt(prompt_file, arm_rules_file, arm):
    if arm not in ARMS:
        raise TrialError(f"arm must be one of {ARMS}, got {arm!r}")
    template = Path(prompt_file).read_text()
    if "{ARM_RULE}" not in template:
        raise TrialError(f"prompt file {prompt_file} has no {{ARM_RULE}} placeholder")
    rules = json.loads(Path(arm_rules_file).read_text())
    if not isinstance(rules, dict) or not isinstance(rules.get(arm), str) or not rules[arm].strip():
        raise TrialError(f"arm rules file {arm_rules_file} has no non-empty string rule for arm {arm!r}")
    return template.replace("{ARM_RULE}", rules[arm])


def make_lead_config(config_dir, arm, skill_dir, claude_home=None):
    """Build the lead's whole CLAUDE_CONFIG_DIR. Returns the sorted relative file list."""
    if arm not in ARMS:
        raise TrialError(f"arm must be one of {ARMS}, got {arm!r}")
    claude_home = Path(claude_home) if claude_home else Path.home() / ".claude"
    cred = claude_home / ".credentials.json"
    if not cred.exists():
        raise TrialError(f"credentials not found at {cred}; cannot run a headless lead")
    d = Path(config_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".credentials.json").symlink_to(cred)
    (d / "settings.json").write_text("{}\n")
    agents = d / "agents"
    agents.mkdir()
    for f in sorted((claude_home / "agents").glob("proxy-*.md")):
        if f.stem != "proxy-self":
            shutil.copy(f, agents / f.name)
    if arm == "skill":
        src = Path(skill_dir)
        if not (src / "scripts" / "fdel.py").is_file():
            raise TrialError(f"skill dir {src} has no scripts/fdel.py")
        shutil.copytree(src, d / "skills" / "fast-delegate",
                        ignore=shutil.ignore_patterns("examples", "__pycache__", "*.pyc", ".git*"))
    return sorted(str(p.relative_to(d)) for p in d.rglob("*"))


def make_state_dir(state_dir, state_src=None):
    """Seed a per-trial FAST_DELEGATE_STATE with copies, so trials start identical and the real
    ledger is never written. Returns (seeded, missing)."""
    src = Path(state_src) if state_src else Path.home() / ".local" / "state" / "fast-delegate"
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    seeded, missing = [], []
    for name in ("harness.json", "catalog_matches.json", "outcomes.jsonl"):
        if (src / name).is_file():
            shutil.copy(src / name, d / name)
            seeded.append(name)
        else:
            missing.append(name)
    return seeded, missing


def build_env(config_dir, state_dir, tmp_dir, base_env=None):
    """Allowlisted environment for the lead: no CLAUDE_CODE_*/session markers, Jev and gateway
    variables passed through by name. Returns (env, names_passed_through)."""
    base = os.environ if base_env is None else base_env
    env = {k: base[k] for k in BASE_ENV if k in base}
    passed = [n for n in JEV_ENV + GATEWAY_ENV if base.get(n)]
    for n in passed:
        env[n] = base[n]
    env.update({"CLAUDE_CONFIG_DIR": str(config_dir), "FAST_DELEGATE_STATE": str(state_dir),
                "TMPDIR": str(tmp_dir), "DISABLE_AUTOUPDATER": "1"})
    return env, passed


def default_claude_cmd(base_env=None):
    """LEAD_TRIAL_CLAUDE_CMD if set, otherwise plain `claude`."""
    base = os.environ if base_env is None else base_env
    if base.get("LEAD_TRIAL_CLAUDE_CMD"):
        return shlex.split(base["LEAD_TRIAL_CLAUDE_CMD"])
    return ["claude"]


def lead_command(claude_cmd, prompt, lead_model):
    return [*claude_cmd, "-p", prompt, "--model", lead_model, "--output-format", "stream-json",
            "--verbose", "--allowedTools", LEAD_TOOLS]


def prepare_clone(base, dest):
    """Fresh clone of base as the lead's cwd, with the origin removed so nothing points back."""
    if not (Path(base) / ".git").exists():
        raise TrialError(f"base {base} is not a git repository")
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(base), str(dest)],
                   check=True, capture_output=True, text=True)
    git = ["git", "-C", str(dest)]
    subprocess.run([*git, "remote", "remove", "origin"], check=True, capture_output=True)
    subprocess.run([*git, "config", "user.name", "lead-trial"], check=True)
    subprocess.run([*git, "config", "user.email", "lead-trial@example.invalid"], check=True)
    return subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def check_lock_paths(lock_paths, protected):
    """A lock path that is, or contains, anything the lead needs would break the lead."""
    home = Path.home().resolve()
    for lp in lock_paths:
        r = Path(lp).resolve()
        if r in (Path("/"), home):
            raise TrialError(f"refusing to lock {lp}: it contains the lead's working area")
        for prot in protected:
            pr = Path(prot).resolve()
            if pr == r or r in pr.parents:
                raise TrialError(f"lock path {lp} contains {prot}, which the lead needs; "
                                 f"keep the answer key outside the trial and cache dirs")


# --------------------------------------------------------------------------- subprocess

def kill_group(pid):
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def run_streaming(cmd, cwd, env, timeout, stdout_path, stderr_path):
    """Run in its own process group, stdout/stderr to files; the whole group is killed on
    timeout, on exit and on any exception. Returns (returncode, timed_out)."""
    with open(stdout_path, "wb") as so, open(stderr_path, "wb") as se:
        p = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=so,
                             stderr=se, start_new_session=True)
        try:
            try:
                return p.wait(timeout=timeout), False
            except subprocess.TimeoutExpired:
                kill_group(p.pid)
                p.wait()
                return None, True
        finally:
            kill_group(p.pid)  # stray background children of a finished or crashed lead


# --------------------------------------------------------------------------- stream parsing

def parse_jsonl(text):
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def parse_final_claim(text):
    m = FINAL_RE.findall(text or "")
    return m[-1] if m else None


def _blocks(event):
    content = (event.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def leak_reasons(tool_input, locked_paths):
    blob = json.dumps(tool_input, ensure_ascii=False, default=str)
    reasons = []
    for lp in locked_paths:
        if lp and lp in blob:
            reasons.append(f"locked_path:{lp}")
    if re.search(r"oracle", blob, re.I):
        reasons.append("oracle")
    return reasons, blob


def parse_stream(text, locked_paths=()):
    """Everything the analysis needs from a stream-json run: result, FINAL claim, spawns with
    parents and served models, Skill calls, fdel.py Bash calls and leak flags."""
    locked = sorted({str(p) for p in locked_paths} | {str(Path(p).resolve()) for p in locked_paths})
    events = parse_jsonl(text)
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    results = [e for e in events if e.get("type") == "result"]
    final = results[-1] if results else None
    spawns, by_id = [], {}
    skills, fdel_calls, leaks, tool_counts = [], [], [], {}
    seen = set()
    lead_texts = []
    for idx, e in enumerate(events):
        parent = e.get("parent_tool_use_id")
        if e.get("type") == "assistant":
            served = (e.get("message") or {}).get("model")
            if parent and served and parent in by_id and served not in by_id[parent]["served_models"]:
                by_id[parent]["served_models"].append(served)
            for b in _blocks(e):
                if b.get("type") == "text" and not parent:
                    lead_texts.append(b.get("text", ""))
                if b.get("type") != "tool_use" or b.get("id") in seen:
                    continue
                seen.add(b.get("id"))
                name, inp = b.get("name"), b.get("input") or {}
                tool_counts[name] = tool_counts.get(name, 0) + 1
                reasons, blob = leak_reasons(inp, locked)
                if reasons:
                    leaks.append({"tool": name, "tool_use_id": b.get("id"), "parent": parent,
                                  "reasons": reasons, "snippet": blob[:240]})
                if name in ("Agent", "Task"):
                    rec = {"tool_use_id": b.get("id"), "tool": name, "subagent_type": inp.get("subagent_type"),
                           "model": inp.get("model"), "description": inp.get("description"), "parent": parent,
                           "background": inp.get("run_in_background"), "served_models": [], "started": False,
                           "status": None, "summary": None, "stream_index": idx}
                    spawns.append(rec)
                    by_id[b.get("id")] = rec
                elif name == "Skill":
                    skills.append({"skill": inp.get("skill"), "args": inp.get("args"), "parent": parent})
                elif name == "Bash" and "fdel.py" in str(inp.get("command", "")):
                    cmd = str(inp.get("command", ""))
                    m = FDEL_SUB_RE.search(cmd)
                    fdel_calls.append({"subcommand": m.group(1) if m else None, "command": cmd[:300],
                                       "parent": parent})
        elif e.get("type") == "system":
            rec = by_id.get(e.get("tool_use_id"))
            if rec is not None and e.get("subtype") == "task_started":
                rec["started"] = True
            elif rec is not None and e.get("subtype") == "task_notification":
                rec["status"] = e.get("status")
                rec["summary"] = str(e.get("summary") or "")[:200]
    ids = {s["tool_use_id"]: s["subagent_type"] for s in spawns}
    for s in spawns:
        s["parent_agent"] = ids.get(s["parent"], "lead") if s["parent"] else "lead"
    result = None
    if final is not None:
        result = {"subtype": final.get("subtype"), "is_error": final.get("is_error"),
                  "total_cost_usd": final.get("total_cost_usd"), "num_turns": final.get("num_turns"),
                  "duration_ms": final.get("duration_ms"), "duration_api_ms": final.get("duration_api_ms"),
                  "terminal_reason": final.get("terminal_reason"), "stop_reason": final.get("stop_reason"),
                  "permission_denials": len(final.get("permission_denials") or []),
                  "subagent_stats": final.get("subagent_stats"), "model_usage": final.get("modelUsage") or {},
                  "text": final.get("result") or ""}
    claim = parse_final_claim(result["text"]) if result else None
    if claim is None and lead_texts:
        claim = parse_final_claim(lead_texts[-1])
    return {"events": len(events), "session_id": init.get("session_id"), "lead_model_served": init.get("model"),
            "agents_available": init.get("agents"), "result": result, "result_events": len(results),
            "final_claim": claim, "spawns": spawns, "skill_calls": skills, "fdel_calls": fdel_calls,
            "leak_flags": leaks, "tool_counts": tool_counts}


# --------------------------------------------------------------------------- pricing

def fetch_discover(fdel, env, cwd, run=subprocess.run):
    """`fdel.py discover --json` under the trial's own state and config."""
    if not Path(fdel).exists():
        raise TrialError(f"fast-delegate not found at {fdel}; cannot price runs")
    res = run([sys.executable, str(fdel), "discover", "--json", "--harness", "claude"], capture_output=True, text=True,
              timeout=DISCOVER_TIMEOUT_S, env=env, cwd=str(cwd))
    if res.returncode != 0:
        raise TrialError(f"fdel discover failed (rc={res.returncode}): {res.stderr[-300:]}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise TrialError(f"fdel discover returned invalid JSON: {e}")


def price_table(discover):
    """{candidate_id: per-token rates}; candidates without both input and output rates are
    absent (unknown, never zero)."""
    table = {}
    for c in (discover or {}).get("candidates", []):
        entry = c.get("catalog_entry") or {}
        price = c.get("price") or {}
        inp, out = entry.get("input_cost_per_token"), entry.get("output_cost_per_token")
        if inp is None and price.get("input_per_m") is not None:
            inp = price["input_per_m"] / 1e6
        if out is None and price.get("output_per_m") is not None:
            out = price["output_per_m"] / 1e6
        if inp is None or out is None or not c.get("model_id"):
            continue
        table[c["id"]] = {"id": c["id"], "model_id": c["model_id"], "input": inp, "output": out,
                          "cache_read": entry.get("cache_read_input_token_cost"),
                          "cache_creation": entry.get("cache_creation_input_token_cost")}
    return table


def normalize_model_key(key):
    """modelUsage key -> catalog model id: drop a [1m] suffix, the proxy route prefix
    (proxy-claude-native--gpt-6-luna -> gpt-6-luna) and a trailing date stamp."""
    k = re.sub(r"\[[^\]]*\]$", "", str(key).strip())
    k = PROXY_PREFIX_RE.sub("", k)
    return re.sub(r"[-@]\d{8}$", "", k)


def match_price(key, table):
    base = normalize_model_key(key)
    for entry in table.values():
        if entry["model_id"] == base:
            return entry
    return table.get(base)


def compute_cost(tokens, price):
    """USD for normalised tokens (input = UNCACHED input only). Returns (usd, assumed_notes);
    a missing cache rate falls back to the input rate and is reported."""
    inp = price["input"]
    notes = []
    cache_read, cache_creation = price.get("cache_read"), price.get("cache_creation")
    if cache_read is None:
        cache_read = inp
        if tokens.get("cached_input"):
            notes.append("cache_read priced at input rate (no catalog cache-read rate)")
    if cache_creation is None:
        cache_creation = inp
        if tokens.get("cache_write"):
            notes.append("cache_write priced at input rate (no catalog cache-write rate)")
    usd = (tokens.get("input", 0) * inp + tokens.get("cached_input", 0) * cache_read
           + tokens.get("cache_write", 0) * cache_creation + tokens.get("output", 0) * price["output"])
    return usd, notes


def model_usage_tokens(mu):
    return {"input": mu.get("inputTokens", 0) or 0, "cached_input": mu.get("cacheReadInputTokens", 0) or 0,
            "cache_write": mu.get("cacheCreationInputTokens", 0) or 0, "output": mu.get("outputTokens", 0) or 0}


def message_tokens(usage):
    return {"input": usage.get("input_tokens", 0) or 0, "cached_input": usage.get("cache_read_input_tokens", 0) or 0,
            "cache_write": usage.get("cache_creation_input_tokens", 0) or 0, "output": usage.get("output_tokens", 0) or 0}


def read_transcripts(config_dir):
    """Per-message token usage from the session transcripts in the lead's config dir.
    lead/side: final (stop_reason set) messages by model, main thread vs subagents.
    partial: unfinished messages by model (proxy-routed models never finalise their usage here);
    their message-start input tokens only serve as allocation weights. Messages are de-duplicated
    by id because the CLI writes one line per content block."""
    lead, side, partial = {}, {}, {}
    files = 0
    messages = {}
    for f in sorted(Path(config_dir, "projects").glob("**/*.jsonl")):
        files += 1
        in_sub = "subagents" in f.parts
        for line in f.read_text(errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = e.get("message") or {}
            if e.get("type") != "assistant" or not m.get("id") or m.get("model") in (None, "<synthetic>"):
                continue
            messages[(str(f), m["id"])] = (m, bool(e.get("isSidechain")) or in_sub)
    for m, sidechain in messages.values():
        t = message_tokens(m.get("usage") or {})
        if m.get("stop_reason") is None:
            partial[m["model"]] = add_tokens(partial.get(m["model"], zero_tokens()), t)
        else:
            bucket = side if sidechain else lead
            bucket[m["model"]] = add_tokens(bucket.get(m["model"], zero_tokens()), t)
    return {"lead": lead, "side": side, "partial": partial, "files": files}


def _split_proportional(total, weights):
    keys = list(weights)
    wsum = sum(weights.values())
    shares = {k: (weights[k] / wsum if wsum else 1 / len(keys)) for k in keys}
    out = {k: zero_tokens() for k in keys}
    for f in TOKEN_FIELDS:
        given = 0
        for k in keys[:-1]:
            out[k][f] = int(round(total[f] * shares[k]))
            given += out[k][f]
        out[keys[-1]][f] = total[f] - given
    return out


def overridden_routes(spawns):
    """{served route: set(requested placeholders)} for spawns whose served model is not what
    the Agent call asked for (the proxy re-routed it by agent marker)."""
    routes = {}
    for s in spawns:
        req = (s.get("model") or "").lower()
        for served in s.get("served_models") or []:
            if req and req not in served.lower():
                routes.setdefault(served, set()).add(req)
    return routes


def _row(key, role, tokens, source, **extra):
    return {"model_key": key, "role": role, "tokens": tokens, "source": source, **extra}


def attribute_usage(model_usage, spawns, transcripts, lead_key):
    """Rows of {model_key, role, tokens, source}: who consumed which model's tokens.
    With transcripts: lead = main-thread messages, worker = subagent messages, and whatever the
    CLI lumped under a placeholder model beyond that goes to the routes the subagents were
    actually served by. Without transcripts: lead = the entry matching the lead model, worker =
    every other entry."""
    rows, warnings = [], []
    lead_norm = normalize_model_key(lead_key) if lead_key else None

    def is_lead_model(key):
        return bool(lead_norm) and normalize_model_key(key) == lead_norm

    if not transcripts or not transcripts.get("files"):
        warnings.append("no session transcripts: lead/worker split uses the modelUsage key only")
        return [_row(k, "lead" if is_lead_model(k) else "worker", model_usage_tokens(mu), "modelUsage")
                for k, mu in model_usage.items()], warnings
    routes = overridden_routes(spawns)
    residuals = {}
    for key, mu in model_usage.items():
        total = model_usage_tokens(mu)
        lead_t = transcripts["lead"].get(key, zero_tokens())
        side_t = transcripts["side"].get(key, zero_tokens())
        native = add_tokens(lead_t, side_t)
        over = [f for f in TOKEN_FIELDS if native[f] > total[f]]
        if over:
            warnings.append(f"transcript tokens exceed modelUsage for {key} ({', '.join(over)}); using transcripts")
        resid = {f: max(total[f] - native[f], 0) for f in TOKEN_FIELDS}
        if has_tokens(lead_t):
            rows.append(_row(key, "lead", lead_t, "transcript"))
        if has_tokens(side_t):
            rows.append(_row(key, "worker", side_t, "transcript"))
        if not has_tokens(resid):
            continue
        if key in transcripts["partial"]:  # a served route with its own modelUsage key
            rows.append(_row(key, "worker", resid, "modelUsage"))
        elif routes:
            residuals[key] = resid
        else:
            lead_like = is_lead_model(key) and not has_tokens(side_t)
            rows.append(_row(key, "lead" if lead_like else "worker", resid, "modelUsage-residual"))
            warnings.append(f"{key}: {sum(resid.values())} tokens in modelUsage not found in transcripts; kept under that key")
    for key, resid in residuals.items():
        hits = [r for r in routes if any(a in key.lower() for a in routes[r])]
        eligible = hits or (list(routes) if len(residuals) == 1 else [])
        if not eligible:
            warnings.append(f"{key}: lumped tokens could not be assigned to a served route; kept under that key")
            rows.append(_row(key, "worker", resid, "modelUsage-residual"))
            continue
        weights = {r: float(transcripts["partial"].get(r, zero_tokens())["input"]) for r in eligible}
        for route, tok in _split_proportional(resid, weights).items():
            rows.append(_row(route, "worker", tok, "residual:" + key, approximate=len(eligible) > 1))
        warnings.append(f"{key}: {sum(resid.values())} tokens were billed under a placeholder model; "
                        f"re-attributed to {', '.join(eligible)}")
    return rows, warnings


def reprice(model_usage, table, spawns=(), transcripts=None, lead_key=None, cli_total=None):
    """Reprice every usage row from catalog prices. Unknown price -> usd None plus a warning;
    a role total is None when any of its rows with tokens is unpriced (partial kept apart)."""
    rows, warnings = attribute_usage(model_usage or {}, list(spawns), transcripts, lead_key)
    totals = {"lead": 0.0, "worker": 0.0}
    no_usage = not model_usage
    if no_usage:
        warnings.append("no modelUsage in the stream; every repriced USD left null")
    unpriced = {"lead": no_usage, "worker": no_usage}
    for r in rows:
        price = match_price(r["model_key"], table)
        r["usd"], r["candidate"], r["notes"] = None, None, []
        if price is None:
            if has_tokens(r["tokens"]):
                warnings.append(f"no catalog price for model {r['model_key']!r}; USD left null")
                unpriced[r["role"]] = True
            continue
        r["candidate"] = price["id"]
        r["usd"], r["notes"] = compute_cost(r["tokens"], price)
        totals[r["role"]] += r["usd"]
        warnings.extend(f"{r['model_key']}: {n}" for n in r["notes"])
    out = {"rows": rows, "warnings": sorted(set(warnings)), "cli_total_usd": cli_total}
    for role in ("lead", "worker"):
        out[f"{role}_usd"] = None if unpriced[role] else totals[role]
        out[f"{role}_usd_priced_part"] = totals[role]
    out["total_usd"] = None if None in (out["lead_usd"], out["worker_usd"]) else out["lead_usd"] + out["worker_usd"]
    return out


# --------------------------------------------------------------------------- grading

def copy_tree(src, dest):
    shutil.copytree(src, dest, ignore=COPY_IGNORE)


def parse_junit(path):
    """[{id, status}] from a pytest junit xml; status passed|failed|error|skipped."""
    tests = []
    root = ET.parse(path).getroot()
    for tc in root.iter("testcase"):
        status = "passed"
        for child in tc:
            if child.tag in ("failure", "error", "skipped"):
                status = {"failure": "failed"}.get(child.tag, child.tag)
                break
        tests.append({"id": f"{tc.get('classname', '')}::{tc.get('name', '')}", "status": status})
    return tests


def run_pytest(tree, extra, grade_cmd, env, timeout):
    junit = Path(tree) / ".grade-junit.xml"
    cmd = [*grade_cmd, f"--junitxml={junit}", "-p", "no:cacheprovider", *extra]
    try:
        res = subprocess.run(cmd, cwd=str(tree), env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"rc": None, "tests": [], "timeout": True, "output_tail": ""}
    except FileNotFoundError as e:
        raise TrialError(f"grading command not found: {e}")
    tests = []
    if junit.exists():
        with contextlib.suppress(ET.ParseError):
            tests = parse_junit(junit)
    return {"rc": res.returncode, "tests": tests, "timeout": False,
            "output_tail": (res.stdout + res.stderr)[-1500:]}


def count(tests, status):
    return sum(1 for t in tests if t["status"] == status)


def grade_tree(repo, oracle_copy, work_root, env, grade_cmd=None, timeout=GRADE_TIMEOUT_S):
    """Copy the final tree twice: once as is (the trial's own tests), once with the oracle added.
    oracle_pass needs rc 0, at least one pass and no failures or errors."""
    grade_cmd = grade_cmd or GRADE_CMD
    work = Path(work_root)
    own_tree, oracle_tree = work / "own", work / "oracle"
    copy_tree(repo, own_tree)
    copy_tree(repo, oracle_tree)
    shutil.copytree(oracle_copy, oracle_tree, dirs_exist_ok=True)
    oracle_files = sorted(str(p.relative_to(oracle_copy)) for p in Path(oracle_copy).rglob("test_oracle.py"))
    if not oracle_files:
        raise TrialError(f"oracle copy {oracle_copy} has no test_oracle.py")
    own = run_pytest(own_tree, [], grade_cmd, env, timeout)
    orc = run_pytest(oracle_tree, oracle_files, grade_cmd, env, timeout)
    t = orc["tests"]
    passed, failed, errors, skipped = (count(t, s) for s in ("passed", "failed", "error", "skipped"))
    ok = (orc["rc"] == 0 and not orc["timeout"] and passed > 0 and failed == 0 and errors == 0)
    return {"oracle_pass": ok, "oracle": {"rc": orc["rc"], "timeout": orc["timeout"], "passed": passed,
                                         "failed": failed, "errors": errors, "skipped": skipped,
                                         "tests": t, "output_tail": "" if ok else orc["output_tail"]},
            "own_tests": {"rc": own["rc"], "timeout": own["timeout"], "passed": count(own["tests"], "passed"),
                          "failed": count(own["tests"], "failed") + count(own["tests"], "error"),
                          "tests": own["tests"], "output_tail": "" if own["rc"] == 0 else own["output_tail"]}}


def git_report(repo, base_head):
    def git(*args):
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    untracked = [l[3:] for l in git("status", "--porcelain", "--untracked-files=all").splitlines()
                 if l.startswith("??") and not re.search(r"(^|/)(\.venv|__pycache__|\.pytest_cache)(/|$)|\.pyc$", l[3:])]
    tracked = [l for l in git("status", "--porcelain", "--untracked-files=no").splitlines() if l.strip()]
    ahead = git("rev-list", "--count", f"{base_head}..HEAD")
    return {"base_head": base_head, "head": git("rev-parse", "HEAD"),
            "commits_since_base": int(ahead) if ahead.isdigit() else None,
            "log": git("log", "--format=%h %s", "-n", "30").splitlines(),
            "changed_files": [l for l in git("diff", "--name-only", f"{base_head}..HEAD").splitlines() if l],
            "uncommitted_tracked": tracked[:20], "untracked": untracked[:20],
            "committed_clean": not tracked and not untracked}


# --------------------------------------------------------------------------- one trial

def append_jsonl(path, record, secrets):
    line = redact(json.dumps(record, sort_keys=True), secrets) + "\n"
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def safe_discover(discover_fn, fdel, env, cwd, errors, label):
    try:
        return discover_fn(fdel, env, cwd)
    except Exception as e:  # pricing degrades to nulls with a warning; the trial still records
        errors.append(f"discover ({label}) failed: {type(e).__name__}: {str(e)[:200]}")
        return None


def run_trial(arm, trial, base, oracle, out, prompt_file, arm_rules, lead_model="opus", timeout=DEFAULT_TIMEOUT_S,
              locks=(), skill_dir=None, claude_cmd=None, claude_home=None, state_src=None, base_env=None,
              discover_fn=None, grade_cmd=None, lock_state_dir=None, cache_root=None, label=None):
    """Run one lead trial end to end and return the result record (also written to disk)."""
    cache_root = Path(cache_root) if cache_root else CACHE_ROOT
    skill_dir = Path(skill_dir) if skill_dir else DEFAULT_SKILL_DIR
    base_env = dict(os.environ if base_env is None else base_env)
    discover_fn = discover_fn or fetch_discover
    claude_cmd = claude_cmd or default_claude_cmd(base_env)
    fdel = skill_dir / "scripts" / "fdel.py"
    prompt = render_prompt(prompt_file, arm_rules, arm)
    oracle, out = Path(oracle).resolve(), Path(out).resolve()
    if not oracle.is_dir() or not list(oracle.rglob("test_oracle.py")):
        raise TrialError(f"oracle dir {oracle} must exist and contain test_oracle.py")
    for lp in locks:
        if not Path(lp).exists():
            raise TrialError(f"--lock path {lp} does not exist")
    trial_dir = out / f"{arm}-{trial}"
    if trial_dir.exists():
        raise TrialError(f"{trial_dir} already exists; pick another --trial or remove it")
    out.mkdir(parents=True, exist_ok=True)
    trial_dir.mkdir()

    run_id = f"{arm}-{trial}-{uuid.uuid4().hex[:8]}"
    run_root = cache_root / "run" / run_id
    private = cache_root / "private" / run_id
    config_dir, state_dir, tmp_dir = run_root / "config", run_root / "state", run_root / "tmp"
    repo = trial_dir / "repo"
    errors = []
    secret_vals = {}
    started = time.time()
    try:
        tmp_dir.mkdir(parents=True)
        private.mkdir(parents=True)
        private_oracle = private / "oracle"
        copy_locked_tree(oracle, private_oracle, lock_state_dir)  # made before locking; grading reads this copy
        for p in [private, *private_oracle.rglob("*")]:
            if not p.is_symlink():
                p.chmod(p.stat().st_mode | 0o700)
        lock_paths = [oracle, *map(Path, locks), private]
        check_lock_paths(lock_paths, [trial_dir, config_dir, state_dir, tmp_dir, skill_dir])
        base_head = prepare_clone(base, repo)
        config_files = make_lead_config(config_dir, arm, skill_dir, claude_home)
        agents_copied = sorted(Path(f).stem for f in config_files if f.startswith("agents/"))
        seeded, missing = make_state_dir(state_dir, state_src)
        env, passed = build_env(config_dir, state_dir, tmp_dir, base_env)
        secret_vals = secrets_from_env(env)
        pre = safe_discover(discover_fn, fdel, env, repo, errors, "pre-run")
        cmd = lead_command(claude_cmd, prompt, lead_model)

        stream_path, stderr_path = trial_dir / "stream.jsonl", trial_dir / "stderr.txt"
        t0 = time.time()
        with locked_keys(lock_paths, lock_state_dir) as locked_roots:
            rc, timed_out = run_streaming(cmd, repo, env, timeout, stream_path, stderr_path)
        wall = time.time() - t0
        redact_file(stream_path, secret_vals)
        redact_file(stderr_path, secret_vals)

        parsed = parse_stream(stream_path.read_text(errors="replace"), locked_roots)
        post = safe_discover(discover_fn, fdel, env, repo, errors, "post-run")
        agents_after = sorted(f.stem for f in (config_dir / "agents").glob("*.md")) if (config_dir / "agents").is_dir() else []
        table = {**price_table(pre), **price_table(post)}
        transcripts = None
        try:
            transcripts = read_transcripts(config_dir)
        except Exception as e:
            errors.append(f"transcripts unreadable: {type(e).__name__}: {str(e)[:200]}")
        res = parsed["result"] or {}
        cost = reprice(res.get("model_usage"), table, parsed["spawns"], transcripts, parsed["lead_model_served"],
                       res.get("total_cost_usd"))
        if not table:
            cost["warnings"].append("empty price table: every USD is null")
        if parsed["result"] is None:
            errors.append("no result event in the stream" + ("; timed out" if timed_out else f"; rc={rc}"))

        grade_env = {k: v for k, v in env.items() if k in BASE_ENV or k == "TMPDIR"}
        try:
            grade = grade_tree(repo, private_oracle, tmp_dir / "grade", grade_env, grade_cmd)
        except Exception as e:
            grade = {"oracle_pass": None, "error": f"{type(e).__name__}: {str(e)[:300]}"}
            errors.append(f"grading failed: {grade['error']}")
        git = git_report(repo, base_head)

        shutil.copytree(state_dir, trial_dir / "state")
        if (config_dir / "projects").is_dir():
            shutil.copytree(config_dir / "projects", trial_dir / "transcripts")
        record = {
            "label": label, "arm": arm, "trial": trial, "run_id": run_id, "started_at": now_iso(),
            "lead_model": lead_model, "lead_model_served": parsed["lead_model_served"],
            "launcher": claude_cmd, "returncode": rc, "timeout": timed_out, "wall_s": round(wall, 1),
            "setup_s": round(t0 - started, 1), "timeout_s": timeout,
            "final_claim": parsed["final_claim"], "result": {k: v for k, v in res.items() if k not in ("text", "model_usage")},
            "result_text_tail": (res.get("text") or "")[-600:], "model_usage": res.get("model_usage"),
            "cost": cost, "spawns": parsed["spawns"], "skill_calls": parsed["skill_calls"],
            "fdel_calls": parsed["fdel_calls"], "leak_flags": parsed["leak_flags"], "tool_counts": parsed["tool_counts"],
            "agents_available": parsed["agents_available"], "grade": grade, "git": git,
            "isolation": {"config_files": config_files, "state_seeded": seeded, "state_missing": missing,
                          "env_passthrough": passed, "locked": locked_roots,
                          "agents_copied": agents_copied, "agents_after_run": agents_after,
                          "discover_pre": sorted(c["id"] for c in (pre or {}).get("candidates", [])),
                          "discover_post": sorted(c["id"] for c in (post or {}).get("candidates", []))},
            "transcripts": {"files": (transcripts or {}).get("files")}, "errors": errors,
            "paths": {"trial_dir": str(trial_dir), "stream": str(stream_path)},
        }
        write_text(trial_dir / "result.json", json.dumps(record, indent=2, sort_keys=True), secret_vals)
        append_jsonl(out / "trials.jsonl", record, secret_vals)
        return record
    except BaseException as e:
        with contextlib.suppress(OSError):
            write_text(trial_dir / "error.txt", f"{type(e).__name__}: {e}\n", secret_vals)
        raise
    finally:
        for d in (run_root, private):
            for p in [d, *(d.rglob("*") if d.exists() else [])]:
                with contextlib.suppress(OSError):
                    if not p.is_symlink():
                        p.chmod(p.stat().st_mode | 0o700)
            shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- summarize

def load_trials(out):
    path = Path(out) / "trials.jsonl"
    if not path.exists():
        raise TrialError(f"{path} not found; run trials first")
    latest = {}
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            latest[(r["arm"], r["trial"])] = r  # a re-run of the same trial replaces the earlier row
    return list(latest.values())


def stats(values):
    vals = [v for v in values if v is not None]
    return {"n": len(vals), "mean": statistics.fmean(vals) if vals else None,
            "median": statistics.median(vals) if vals else None}


def summarize(records):
    summary = {}
    for arm in sorted({r["arm"] for r in records}):
        rs = [r for r in records if r["arm"] == arm]
        claimed = [r for r in rs if r.get("final_claim") == "ACCEPTED"]
        spawns, fdel = {}, {}
        for r in rs:
            for s in r.get("spawns", []):
                k = f"{s.get('subagent_type')} / {s.get('model') or '-'}"
                spawns[k] = spawns.get(k, 0) + 1
            for c in r.get("fdel_calls", []):
                fdel[c.get("subcommand") or "?"] = fdel.get(c.get("subcommand") or "?", 0) + 1
        cost = lambda r, k: (r.get("cost") or {}).get(k)  # noqa: E731
        summary[arm] = {
            "n": len(rs),
            "oracle_accepted": sum(1 for r in rs if (r.get("grade") or {}).get("oracle_pass") is True),
            "ungraded": sum(1 for r in rs if (r.get("grade") or {}).get("oracle_pass") is None),
            "lead_claimed_accepted": len(claimed),
            "false_accepts": sum(1 for r in claimed if (r.get("grade") or {}).get("oracle_pass") is not True),
            "timeouts": sum(1 for r in rs if r.get("timeout")),
            "cli_total_usd": stats([cost(r, "cli_total_usd") for r in rs]),
            "lead_usd": stats([cost(r, "lead_usd") for r in rs]),
            "worker_usd": stats([cost(r, "worker_usd") for r in rs]),
            "repriced_total_usd": stats([cost(r, "total_usd") for r in rs]),
            "spawns_by_agent_model": dict(sorted(spawns.items())),
            "fdel_calls": {"total": sum(fdel.values()), "by_subcommand": dict(sorted(fdel.items()))},
            "skill_calls": sum(len(r.get("skill_calls", [])) for r in rs),
            "leak_flags": {"total": sum(len(r.get("leak_flags", [])) for r in rs),
                           "trials_with_flags": sum(1 for r in rs if r.get("leak_flags"))},
        }
    return summary


def format_summary(summary):
    def usd(s):
        return "n/a" if s["mean"] is None else f"${s['mean']:.4f}/${s['median']:.4f} (n={s['n']})"
    lines = []
    for arm, s in summary.items():
        lines += [f"== arm {arm}: n={s['n']} oracle_accepted={s['oracle_accepted']} "
                  f"lead_claimed_accepted={s['lead_claimed_accepted']} false_accepts={s['false_accepts']} "
                  f"timeouts={s['timeouts']} ungraded={s['ungraded']}",
                  f"   USD mean/median  cli_total={usd(s['cli_total_usd'])}  lead={usd(s['lead_usd'])}  "
                  f"worker={usd(s['worker_usd'])}  repriced_total={usd(s['repriced_total_usd'])}",
                  f"   fdel calls={s['fdel_calls']['total']} {s['fdel_calls']['by_subcommand']}  "
                  f"skill calls={s['skill_calls']}  leak flags={s['leak_flags']['total']} "
                  f"(in {s['leak_flags']['trials_with_flags']} trials)",
                  "   spawns: " + (", ".join(f"{k} x{v}" for k, v in s["spawns_by_agent_model"].items()) or "none")]
    return "\n".join(lines)


# --------------------------------------------------------------------------- smoke

SMOKE_RULE = ("Using the Agent tool, spawn exactly two subagents in one message and nothing else. "
              "Subagent 1: the available agent type whose name starts with `proxy-` and contains `luna` "
              "(use `proxy-gpt-5-6-luna` if it is listed){proxy_model}. "
              "Subagent 2: agent type `general-purpose` with model `haiku`. "
              "Ask each of them exactly: What is 17 times 3? Reply with only the number. "
              "Wait for both answers, then end with one line `FINAL: ACCEPTED` if both said 51, "
              "else `FINAL: NOT_ACCEPTED`. Do not use any other tool.")


def smoke_checks(record):
    """Pure pass/fail checks of the smoke acceptance criteria against a trial record."""
    spawns = record.get("spawns", [])
    rows = (record.get("cost") or {}).get("rows", [])
    iso = record.get("isolation", {})
    proxy_spawn = any(str(s.get("subagent_type", "")).startswith("proxy-") and "luna" in str(s.get("subagent_type"))
                    for s in spawns)
    haiku_spawn = any(s.get("model") == "haiku" and not str(s.get("subagent_type", "")).startswith("proxy-")
                      for s in spawns)

    def priced(is_proxy):
        return any(("--" in r["model_key"] or not r["model_key"].startswith("claude")) == is_proxy
                   and has_tokens(r["tokens"]) and (r.get("usd") or 0) > 0 for r in rows)

    def proxy_ids(ids):
        return sorted(i for i in ids if i.startswith("proxy-"))

    return {
        "spawn_proxy_luna": proxy_spawn,
        "spawn_haiku": haiku_spawn,
        "proxy_model_has_tokens_and_usd": priced(True),
        "claude_model_has_tokens_and_usd": priced(False),
        "discover_pre_lists_copied_proxy_agents": bool(iso.get("agents_copied"))
        and proxy_ids(iso.get("discover_pre", [])) == proxy_ids(iso.get("agents_copied", [])),
        "discover_post_lists_isolated_proxy_agents": bool(iso.get("agents_after_run"))
        and proxy_ids(iso.get("discover_post", [])) == proxy_ids(iso.get("agents_after_run", [])),
        "discover_excludes_proxy_self": "proxy-self" not in iso.get("discover_pre", []) + iso.get("discover_post", []),
        "final_claim_present": record.get("final_claim") is not None,
        "cli_cost_nonzero": (record.get("result") or {}).get("total_cost_usd") not in (None, 0),
    }


SMOKE_MARKER = ".lead-trial-smoke"


def run_smoke(out, lead_model="haiku", timeout=900, claude_cmd=None, placeholder=False, base_env=None):
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        if not (out / SMOKE_MARKER).exists():
            raise TrialError(f"{out} is not empty and not a previous smoke dir; refusing to wipe it")
        shutil.rmtree(out)
    for sub in ("base", "oracle"):
        (out / sub).mkdir(parents=True)
    (out / SMOKE_MARKER).write_text("")
    (out / "base" / "README.md").write_text("smoke\n")
    (out / "oracle" / "test_oracle.py").write_text("def test_oracle():\n    assert True\n")
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=smoke", "-c", "user.email=s@example.invalid",
                                                "commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(out / "base"), *cmd], check=True, capture_output=True)
    proxy_model = " with model `haiku`" if placeholder else " and pass NO `model` parameter"
    (out / "prompt.txt").write_text("You are a smoke-test lead. {ARM_RULE}\n")
    (out / "arm-rules.json").write_text(json.dumps({a: SMOKE_RULE.format(proxy_model=proxy_model) for a in ARMS}))
    record = run_trial("skill", 1, out / "base", out / "oracle", out / "trials", out / "prompt.txt",
                       out / "arm-rules.json", lead_model=lead_model, timeout=timeout, claude_cmd=claude_cmd,
                       base_env=base_env)
    checks = smoke_checks(record)
    cost = record["cost"]
    return {
        "checks": checks, "all_passed": all(checks.values()), "launcher": record["launcher"],
        "lead_model_served": record["lead_model_served"], "final_claim": record["final_claim"],
        "wall_s": record["wall_s"], "cli_total_usd": cost["cli_total_usd"],
        "repriced": {k: cost[k] for k in ("lead_usd", "worker_usd", "total_usd")},
        "spawns": [{k: s[k] for k in ("subagent_type", "model", "served_models", "parent_agent", "status")}
                   for s in record["spawns"]],
        "model_usage_keys": sorted(record.get("model_usage") or {}),
        "cost_rows": [{k: r.get(k) for k in ("model_key", "role", "source", "tokens", "usd", "candidate")}
                      for r in cost["rows"]],
        "cost_warnings": cost["warnings"], "errors": record["errors"],
        "isolation": {k: record["isolation"][k] for k in ("config_files", "env_passthrough", "agents_copied",
                                                           "agents_after_run", "discover_pre", "discover_post")},
        "result_json": record["paths"]["trial_dir"] + "/result.json",
    }


# --------------------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(description="Headless lead-trial runner for delegation experiments")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one trial")
    r.add_argument("--arm", choices=ARMS, required=True)
    r.add_argument("--trial", type=int, required=True)
    r.add_argument("--base", type=Path, required=True, help="git repo the lead starts from (cloned)")
    r.add_argument("--oracle", type=Path, required=True, help="dir with the hidden test_oracle.py (locked)")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--prompt-file", type=Path, required=True)
    r.add_argument("--arm-rules", type=Path, required=True)
    r.add_argument("--lead-model", default="opus")
    r.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    r.add_argument("--lock", action="append", default=[], type=Path, help="extra path to chmod 000 during the run")
    r.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR)
    r.add_argument("--claude-cmd", help="launcher command, e.g. 'claude' (default: see default_claude_cmd)")
    s = sub.add_parser("smoke", help="live end-to-end self check with a cheap lead")
    s.add_argument("--out", type=Path, default=CACHE_ROOT / "smoke")
    s.add_argument("--lead-model", default="haiku")
    s.add_argument("--timeout", type=int, default=900)
    s.add_argument("--claude-cmd")
    s.add_argument("--placeholder", action="store_true", help="spawn the proxy agent with model=haiku (the polluted case)")
    z = sub.add_parser("summarize", help="aggregate trials.jsonl per arm")
    z.add_argument("--out", type=Path, required=True)
    z.add_argument("--json", action="store_true")
    return p


def emit(text):
    print(redact(text, secrets_from_env(os.environ)))


def main(argv=None):
    args = build_parser().parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # let finally blocks restore modes
    cmd = shlex.split(args.claude_cmd) if getattr(args, "claude_cmd", None) else None
    try:
        if args.cmd == "run":
            rec = run_trial(args.arm, args.trial, args.base, args.oracle, args.out, args.prompt_file, args.arm_rules,
                            args.lead_model, args.timeout, args.lock, args.skill_dir, cmd)
            brief = {"arm": rec["arm"], "trial": rec["trial"], "final_claim": rec["final_claim"],
                     "oracle_pass": (rec["grade"] or {}).get("oracle_pass"), "timeout": rec["timeout"],
                     "cli_total_usd": rec["cost"]["cli_total_usd"], "lead_usd": rec["cost"]["lead_usd"],
                     "worker_usd": rec["cost"]["worker_usd"], "spawns": len(rec["spawns"]),
                     "leak_flags": len(rec["leak_flags"]), "errors": rec["errors"], "result": rec["paths"]["trial_dir"] + "/result.json"}
            emit(json.dumps(brief, indent=2))
            return 0
        if args.cmd == "smoke":
            summary = run_smoke(args.out, args.lead_model, args.timeout, cmd, args.placeholder)
            emit(json.dumps(summary, indent=2))
            return 0 if summary["all_passed"] else 1
        records = load_trials(args.out)
        summary = summarize(records)
        (Path(args.out) / "summary.json").write_text(json.dumps(summary, indent=2))
        emit(json.dumps(summary, indent=2) if args.json else format_summary(summary))
        return 0
    except TrialError as e:
        print(redact(f"error: {e}", secrets_from_env(os.environ)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
