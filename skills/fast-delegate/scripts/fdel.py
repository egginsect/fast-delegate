#!/usr/bin/env python3
"""fast-delegate v2: routing-only model recommendation.

Usage:
  fdel.py discover [--refresh] [--json]                  list every worker (custom agents + built-ins)
  fdel.py route [--task FILE|stdin] [--jev] [--refresh]  decide direct|delegate; print one JSON value
           [--brief] [--lead ID]
  fdel.py record --candidate ID --family F --outcome accepted|rejected [--note TEXT] [--tokens N] [--force]
           [--served-model MODEL] [--transcript SUBAGENT.jsonl]
  fdel.py stats                                          acceptance rate (and median tokens) per family x candidate

Task JSON fields: deliverable (required), acceptance (list), owned_paths (list),
access ("read"|"write", default "read"), family (free text, default "general"),
need_tools (bool, default True), est_input_tokens (default 20000),
est_output_tokens (default 2000), est_cached_input_share (0-1, default 0: the share of
est_input_tokens priced at the catalog's cache_read_input_token_cost when the matched
entry has one), exclude (ids), difficulty (0-4 lead
override that can only raise the required fit), context (free text background for the
worker and Jev), context_files (list of paths read as evidence for Jev,
capped per-file and in total).

`route --lead ID` (or FAST_DELEGATE_LEAD, or harness.json default_lead)
keeps only candidates strictly cheaper than the lead's blended price; unpriced or
unknown lead is a hard error, no candidates cheaper than the lead is `direct`.
`discover` reports each candidate's `state`: `enabled`, `probation` (judged and can
win, but flagged), or `disabled` (never routed, never judged).

No model names, vendor names, or tier labels in routing code paths; everything
model-specific lives in data (agent files, harness.json, the LiteLLM catalog, the ledger).

Proxy guard: an agent file whose frontmatter model carries a proxy route prefix
(`proxy-claude-native--gpt-6-luna`) only reaches that model when the session was launched
through an active proxy; in a plain `claude` session the spawn silently runs on the placeholder
model. `route` drops such candidates (reason names each id) unless the proxy is detected as
active; `discover --json` marks `proxy_routed` / `proxy_active`. FAST_DELEGATE_PROXY=1|0
overrides detection. `record --served-model MODEL` / `--transcript FILE` (assistant)
message.model values of the subagent transcript) refuse an outcome whose served model is from
another model family than the candidate's unless --force; a different version of the same family
records with served_model_version_drift and a stderr warning.

Network: discover, route, and record (without --force) read the LiteLLM catalog from
the local cache and download it only when the cache is missing, older than 24h, or
--refresh is given (a failed download falls back to any cached copy, with a warning).
FAST_DELEGATE_CATALOG=path uses a local catalog file and never downloads.
route makes at most one TypeSafe task request by default (--jev is a compatibility alias).
Task-level scores (difficulty, verifiability)
count only at confidence >= TYPESAFE_CONFIDENCE (default 0.4). Fit judgments are
advisory: scores and distributions inform lead acceptance without qualification gates. Stdlib only.
"""

import argparse, calendar, hashlib, json, math, os, re, subprocess, sys, time, urllib.request, urllib.error, uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LEDGER = Path(os.environ.get("FAST_DELEGATE_STATE", Path.home() / ".local/state/fast-delegate")) / "outcomes.jsonl"
ROUTES_LEDGER = LEDGER.parent / "routes.jsonl"  # section K4: calibration ledger of route decisions

CLAUDE_CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "fast-delegate"
LITELLM_CACHE = CACHE_DIR / "litellm.json"
LITELLM_META = CACHE_DIR / "litellm.meta.json"
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
CATALOG_MAX_AGE_S = 24 * 3600
CATALOG_HARD_STALE_S = 14 * 24 * 3600  # past this, route warns and flags stale_prices
LITELLM_REPO = "BerriAI/litellm"
LITELLM_PATH = "model_prices_and_context_window.json"
GITHUB_COMMIT_API = f"https://api.github.com/repos/{LITELLM_REPO}/commits/main"
CATALOG_MAX_BYTES = 30 * 1024 * 1024  # section 1: size bound; current file is ~2.9 MB, generous margin
CATALOG_DOWNLOAD_DEADLINE_S = 30  # section 1 (review fix 5): wall-clock cap on chunked catalog reads
CATALOG_MIN_ENTRIES = 50  # section 1: shape validation -- fewer keys than this is not a plausible catalog
TOKEN_TIER_THRESHOLDS = (200_000, 272_000)  # section 4: LiteLLM's *_above_200k/272k_tokens tiers
VALID_BILLING_MODES = {"metered", "subscription", "local"}  # section 2: harness.json builtin billing.mode

# Quota-aware selection defaults, overridable via harness.json/override's top-level
# `quota` object ({warn_at, reserve, stale_five_hour_s, stale_seven_day_s, budget_usd_per_day,
# max_concurrent}). Never block on unknown/unparseable quota (see default_quota_thresholds).
QUOTA_WARN_AT = 80.0        # used% on either window at/above this adds a warning, no block
QUOTA_RESERVE = 10.0        # remaining% below this on either window is a hard skip
QUOTA_STALE_FIVE_HOUR_S = 30 * 60     # a 5h-window snapshot older than this is flagged stale
QUOTA_STALE_SEVEN_DAY_S = 6 * 3600    # a 7d-window snapshot older than this is flagged stale
QUOTA_PRESSURE_EPS = 0.02   # pressure() floor: a comfortably-paced subscription pool costs ~free
CODEX_WINDOW_MINUTES = {"five_hour": 300, "seven_day": 10080}  # primary/secondary windows
# Review fix 1: read_codex_quota() tails rollout files instead of reading them whole (a real
# rollout file can be tens of MB; the rate_limits line we need is always near the end). Chunk size
# doubles backward from the end up to CODEX_TAIL_MAX_BYTES per file; CODEX_TAIL_MAX_FILES caps how
# many (newest-first) files are opened at all before giving up.
CODEX_TAIL_CHUNK_BYTES = 64 * 1024
CODEX_TAIL_MAX_BYTES = 4 * 1024 * 1024
CODEX_TAIL_MAX_FILES = 20

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
# docs.typesafe.ai/api.md does not document a max questions-per-request; 24 is our own cap.
JEV_MAX_QUESTIONS = 24
TYPESAFE_INPUT_PRICE_PER_M = 0.042  # default $/M input tokens for jev_cost_usd, env-overridable
MARGIN_WINDOW = 0.1  # section K2: mass within this of the gate is a "thin margin"
DIFFICULTY_FAMILY_GUARD = 2.5  # section K3: family-evidence guard kicks in at/above this difficulty

PRICE_BANDS = ["cheapest", "cheap", "mid", "pricey", "most expensive"]
RUNNABLE_TOKENS = ["cd ", "pytest", "go test", "npm", "make", "uv run", "exit 0", "passes"]
DATE_SUFFIX = re.compile(r"(-\d{4}-\d{2}-\d{2}|-\d{8}|@\d{8})$")
CONTEXT_FILE_CAP = 20000
CONTEXT_TOTAL_CAP = 80000
MATCH_SHORTLIST = 25            # catalog keys Jev judges per unmatched model
MATCH_MODELS_PER_REQUEST = 4    # 4 x 25 nouls per request; live probes used 50 in ~400 ms
MATCH_THRESHOLD = 0.7           # noul >= this counts as "same model" (TYPESAFE_MATCH_THRESHOLD)
DEFAULT_SPECIALIZATION_TAGS = ["coder", "vl", "vision", "math", "embed", "embedding", "audio", "guard"]
# Fix 2 (fixes-j.md): tokens generic enough that sharing only these must not count as a "same
# model" signal; includes letters split off size/quant tags ("7b" -> "7","b"; "a35b" -> "a","35","b").
DEFAULT_GENERIC_NAME_TOKENS = ["instruct", "chat", "it", "v", "b", "a", "preview", "latest", "turbo",
                               "cloud", "base", "hf", "fp8", "fp16", "bf16", "int4", "int8", "awq",
                               "gguf", "maas", "local", "model", "models", "accounts"]
# Fix 3 (fixes-j.md): LiteLLM's own documentation-example key, not a real model.
IGNORED_CATALOG_KEYS = {"sample_spec"}
MATCH_QUESTION_TEXT = ("Is catalog entry `models.{m}.entries[{j}]` the same model as `models.{m}.model_id`: "
                       "same family, parameter size and version? A hosting-provider prefix, date suffix, "
                       "instruct tag or quantization tag may differ.")
MATCH_CRITERIA = {"true": "Same model; only hosting, date, instruct or quantization labels differ.",
                  "false": "A different family, size, version, or a fine-tune."}

QUESTIONS = {
    "independent": {"type": "noul",
        "instructions": "Can a worker who sees only `task` (its deliverable, acceptance checks and owned paths), and not the lead's conversation, complete the deliverable and know when it is done?",
        "criteria": {"true": "The task text is self-contained; done is defined by the acceptance checks.",
                     "false": "Finishing needs unstated context, ongoing decisions, or the lead's judgment about what counts as done."}},
    "difficulty": {"type": "score",
        "instructions": "How much reasoning skill does `task.deliverable` demand from whoever does it?",
        "criteria": ["Mechanical: a lookup, rename, or copy; no judgment",
                     "Routine: one well-understood change or check in one place",
                     "Moderate: several connected steps or files; ordinary engineering judgment",
                     "Hard: subtle reasoning such as concurrency, security, or finding several non-obvious defects",
                     "Expert: novel design or research where strong engineers often get it wrong"]},
    "verifiability": {"type": "score",
        "instructions": "Using only `task.acceptance`, how objectively can the lead check that `task.deliverable` was met?",
        "criteria": ["No usable checks; success is opinion",
                     "Checks exist but are vague",
                     "Some checks are objective, others disputable",
                     "Objective checks cover most of the deliverable",
                     "Objective, runnable checks cover the whole deliverable"]},
    "irreversible": {"type": "noul",
        "instructions": "Would carrying out `task.deliverable` as written change something that cannot be undone by editing files again (for example pushing, deleting shared data, sending messages, or spending money)?",
        "criteria": {"true": "At least one step cannot be undone by editing files again.",
                     "false": "Every step can be undone by editing files again."}},
}

FIT_CRITERIA = ["Clearly unsuited: this worker would very likely fail the task",
                "Poor fit: this worker would likely miss part of the deliverable",
                "Plausible: this worker could pass the acceptance checks with some risk",
                "Good fit: this worker should reliably pass the acceptance checks",
                "Excellent fit: this worker is well matched to the task's difficulty and checks"]

# Remaining question budget after the fixed task questions goes to one fit_cN per candidate.
JEV_MAX_CANDIDATES = JEV_MAX_QUESTIONS - len(QUESTIONS)


def fit_question(cid):
    return {"type": "score",
            "instructions": f"How well suited is the worker described in `candidates.{cid}` (its model, context band, tool support and past evidence) to complete the FULL `task.deliverable` and ALL `task.acceptance` criteria, given task difficulty, verification, context, access and owned-path constraints and available tools? Follow `routing_objective`: work completion comes first; cost and availability are secondary observations, not capability. Assess sufficient capability for credible full completion, not maximum capability or perfection beyond acceptance. Do not always favor the highest fit, cheapest, or most expensive model. Among credible full-completion choices, consider cost, fresh quota headroom and recent route health, and model/provider usage distribution only when actually supplied as fresh facts. Unknown quota is not healthy quota. Prefer diversity in ties or close adequate choices to balance quota usage; never force random diversity or an unqualified model. Missing, stale or absent quota, health, usage and strength evidence remains unknown, not zero. Do not fabricate recent usage or aggregate headroom across pools. Ground model-information strength in supplied evidence, not prestige. A cheap failed attempt plus repair and verification may cost more. A higher-priced eligible candidate can be justified by concrete task capability and acceptance-coverage evidence, never prestige or price as quality. Fit scores and distributions are advisory, not actual success probabilities or a cost/probability optimization; the main model owns actual selection. Sparse history must not become a self-reinforcing preference: an unobserved model is not incapable; selected samples such as 1/1 versus 0/1 cannot alone justify always choosing the strongest or known model. A cheaper adequate or exploratory choice is defensible for a bounded, verifiable, low-risk task, never at the expense of full acceptance or through forced diversity. Justify higher cost by explaining what a cheaper alternative lacks for this task, based on concrete capabilities or risk, not scores, prestige or history alone. Small-sample history is observational, not universal capability proof. Judge capability only. Cheap prices and billing are never proof of ability. Small fit-score differences are not proof of capability differences. Missing benchmarks and empty history mean unknown strength; do not invent confidence. Cache shares are caller-supplied pricing assumptions, not verified cache hits; a latest user-chunk timestamp is not cache evidence. Identity confidence is not task-fit confidence. Deployment and quota are observations; deterministic code owns eligibility and the lead owns acceptance.",
            "criteria": FIT_CRITERIA}


# ---------------------------------------------------------------- data loading

MERGE_BY_ID_KEYS = ("builtin", "disabled", "probation")  # harness.json lists keyed by "id"
MERGE_BY_KEY_KEYS = ("billing",)  # harness.json id-keyed maps (section 2): merged per id, override wins


def merge_by_id(shipped_list, override_list):
    """Merge two lists of {"id": ..., ...} dicts by id; override entries win and unmatched
    shipped entries survive. Used for harness.json's builtin/disabled/probation (fix c: one
    helper instead of three copies of the same loop)."""
    by_id = {item["id"]: item for item in (shipped_list or []) if isinstance(item, dict) and "id" in item}
    for item in override_list or []:
        if isinstance(item, dict) and "id" in item:
            by_id[item["id"]] = item
    return list(by_id.values())


def harness_override_path():
    """Path to the local harness.json override (FAST_DELEGATE_HARNESS, else
    $FAST_DELEGATE_STATE/harness.json if it exists), or None if none is configured."""
    if os.environ.get("FAST_DELEGATE_HARNESS"):
        return Path(os.environ["FAST_DELEGATE_HARNESS"])
    candidate = Path(os.environ.get("FAST_DELEGATE_STATE", Path.home() / ".local/state/fast-delegate")) / "harness.json"
    return candidate if candidate.is_file() else None


def load_harness():
    """Load harness.json (built-ins, disabled ids, placeholders, catalog provider preference).

    Merges an optional local override (see harness_override_path()) over the shipped file:
    scalar keys are replaced; builtin/disabled/probation merge by id, override wins (merge_by_id);
    the top-level `billing` id-keyed map (section 2, review fix 2) merges per candidate id, override
    wins for any id it names, unmatched shipped ids survive. A malformed or non-object override
    file is a hard error naming the file (fail closed: silently ignoring it would fail open and
    re-enable ids the override meant to disable). Returns the merged harness dict; use
    harness_override_path() separately to learn which file, if any, was used (kept as a separate
    lookup so this function's signature doesn't churn -- review fix b)."""
    harness = json.loads((HERE / "harness.json").read_text())
    override_path = harness_override_path()
    if override_path is None:
        return harness

    try:
        override = json.loads(override_path.read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"harness override {override_path} could not be read as JSON: {e}")
    if not isinstance(override, dict):
        raise SystemExit(f"harness override {override_path} must be a JSON object")

    for key, value in override.items():
        if key in MERGE_BY_ID_KEYS:
            if not isinstance(value, list):
                raise SystemExit(f"harness override {override_path} field {key!r} must be a list")
            harness[key] = merge_by_id(harness.get(key, []), value)
        elif key in MERGE_BY_KEY_KEYS:
            if not isinstance(value, dict):
                raise SystemExit(f"harness override {override_path} field {key!r} must be an object")
            harness[key] = {**harness.get(key, {}), **value}
        else:
            harness[key] = value
    return harness


# Section N harness ids are data-like CLI identifiers, not model/vendor names, but the alias
# below keeps the literal string out of route/discover/brief_output's own source (see
# test_routing_code_names_no_model_vendor_or_tier) so that lint stays a real guarantee about
# routing *logic*, not an accident of which harness happens to share a name with a lab.
HARNESS_NATIVE = "claude"
KNOWN_HARNESSES = (HARNESS_NATIVE, "codex")


def detect_harness(explicit=None):
    """Section N1: the active CLI harness -- `explicit` ("claude"/"codex"/"auto"/None), else auto-
    detect from environment markers. auto = claude if CLAUDECODE is set (a Claude session may also
    carry CODEX_COMPANION_* from a plugin, so CLAUDECODE takes precedence), else codex if
    CODEX_THREAD_ID or CODEX_SESSION_ID is set, else a descriptive error asking for --harness."""
    if explicit and explicit != "auto":
        if explicit not in KNOWN_HARNESSES:
            raise SystemExit(f"--harness must be one of {KNOWN_HARNESSES} (or 'auto'), not {explicit!r}")
        return explicit
    if os.environ.get("CLAUDECODE"):
        return "claude"
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID"):
        return "codex"
    raise SystemExit("could not auto-detect the active harness (no CLAUDECODE, CODEX_THREAD_ID, or "
                     "CODEX_SESSION_ID in the environment); pass --harness claude|codex")


def read_bounded(resp, max_bytes, deadline_s, chunk_size=65536, clock=None):
    """Read `resp` (an open HTTP response) in bounded chunks (section 1, review fix 5): raise
    ValueError as soon as more than `max_bytes` total have been read -- a response is never
    buffered unbounded into memory before being rejected -- or TimeoutError once `deadline_s` of
    wall-clock time elapses since the first read, so a slow-drip response (many small chunks,
    each completing well inside the per-call socket `timeout`) can't hold the connection open past
    the caller's intended budget. `clock` (default time.monotonic) is injectable for deterministic
    tests. Returns the accumulated bytes."""
    clock = clock or time.monotonic
    start = clock()
    chunks, total = [], 0
    while True:
        if clock() - start > deadline_s:
            raise TimeoutError(f"catalog download exceeded {deadline_s}s wall-clock deadline")
        chunk = resp.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"catalog download exceeded {max_bytes} byte bound")
        chunks.append(chunk)
    return b"".join(chunks)


def resolve_litellm_commit(timeout=10):
    """(commit_sha, warning|None): the tip of BerriAI/litellm's main branch via the GitHub API, or
    (None, warning) when the API is unavailable (rate limit, network, malformed reply) -- section 1's
    documented fallback is the ETag + sha256 of the raw file instead (see fetch_litellm_snapshot)."""
    try:
        req = urllib.request.Request(GITHUB_COMMIT_API, headers={"accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = read_bounded(resp, 1024 * 1024, CATALOG_DOWNLOAD_DEADLINE_S)
            payload = json.loads(raw)
        sha = payload.get("sha") if isinstance(payload, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return None, "github commit API returned no usable commit sha"
        return sha, None
    except Exception as e:
        return None, f"github commit API unavailable ({type(e).__name__})"


def validate_catalog_bytes(raw):
    """(catalog dict | None, error string | None): size-bounded, shape-validated parse of a
    downloaded LiteLLM catalog -- never raises. A malformed or oversized download must never be
    promoted over the cached last-known-good snapshot (section 1)."""
    if len(raw) > CATALOG_MAX_BYTES:
        return None, f"catalog download is {len(raw)} bytes, over the {CATALOG_MAX_BYTES} byte bound"
    try:
        catalog = json.loads(raw)
    except ValueError as e:
        return None, f"catalog is not valid JSON ({type(e).__name__})"
    if not isinstance(catalog, dict) or len(catalog) < CATALOG_MIN_ENTRIES:
        return None, f"catalog shape invalid: expected an object with >= {CATALOG_MIN_ENTRIES} entries"
    priced = sum(1 for v in catalog.values()
                 if isinstance(v, dict) and _number(v.get("input_cost_per_token")) is not None)
    if priced == 0:
        return None, "catalog shape invalid: no entry has a numeric input_cost_per_token"
    return catalog, None


def fetch_litellm_snapshot(timeout=10):
    """Download and validate one LiteLLM catalog snapshot with provenance (section 1). Returns
    (catalog | None, meta | None, warnings); a bad download (network error, oversized, malformed,
    wrong shape) returns (None, None, warnings) and never touches the cache -- the caller falls
    back to the last-known-good snapshot on disk. meta is {repo, path, sha256, fetched_at} plus
    `commit` (preferred) or `etag` (fallback, when the GitHub commit API is unavailable)."""
    warnings = []
    commit, commit_warning = resolve_litellm_commit(timeout=timeout)
    if commit_warning:
        warnings.append(commit_warning)
    url = f"https://raw.githubusercontent.com/{LITELLM_REPO}/{commit}/{LITELLM_PATH}" if commit else LITELLM_URL
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            raw = read_bounded(resp, CATALOG_MAX_BYTES, CATALOG_DOWNLOAD_DEADLINE_S)
            etag = resp.headers.get("ETag") if hasattr(resp, "headers") else None
    except (ValueError, TimeoutError) as e:
        # read_bounded's own descriptive message (byte bound or wall-clock deadline) is useful
        # and safe to surface verbatim, unlike an arbitrary network exception's message.
        return None, None, warnings + [f"catalog fetch failed: {e}"]
    except Exception as e:
        return None, None, warnings + [f"catalog fetch failed: {type(e).__name__}"]
    catalog, err = validate_catalog_bytes(raw)
    if err:
        return None, None, warnings + [f"catalog fetch failed: {err}"]
    meta = {"repo": LITELLM_REPO, "path": LITELLM_PATH, "sha256": hashlib.sha256(raw).hexdigest(),
            "fetched_at": time.time()}
    if commit:
        meta["commit"] = commit
    elif etag:
        meta["etag"] = etag
    return catalog, meta, warnings


def fetch_litellm_catalog(refresh=False):
    """Return (catalog, warnings); download only when the cache is missing/stale or refresh is set.
    Thin wrapper over fetch_litellm_catalog_meta for callers that don't need provenance."""
    catalog, _meta, warnings = fetch_litellm_catalog_meta(refresh=refresh)
    return catalog, warnings


def fetch_litellm_catalog_meta(refresh=False):
    """(catalog, meta|None, warnings). meta is the provenance dict persisted to litellm.meta.json
    (section 1): {repo, commit|etag, path, sha256, fetched_at}, or None when FAST_DELEGATE_CATALOG
    points at a local file (no upstream provenance) or no catalog is available at all. A malformed
    or oversized download is validated before it can ever replace the cached last-known-good
    snapshot; on failure the stale cache (with its own meta) is returned instead, with a warning."""
    local = os.environ.get("FAST_DELEGATE_CATALOG")
    if local:
        try:
            catalog = json.loads(Path(local).read_text())
            if not isinstance(catalog, dict):
                return {}, None, ["FAST_DELEGATE_CATALOG is not a JSON object; prices unknown"]
            return catalog, None, []
        except (OSError, ValueError) as e:
            return {}, None, [f"FAST_DELEGATE_CATALOG unreadable ({type(e).__name__}); prices unknown"]
    warnings = []

    def read_cache():
        try:
            return json.loads(LITELLM_CACHE.read_text()), json.loads(LITELLM_META.read_text())
        except (OSError, ValueError, TypeError):
            return None, None

    if not refresh and LITELLM_CACHE.is_file() and LITELLM_META.is_file():
        catalog, meta = read_cache()
        if catalog is not None and meta is not None:
            try:
                age = time.time() - float(meta.get("fetched_at", 0))
            except (TypeError, ValueError):
                age = float("inf")
            if age < CATALOG_MAX_AGE_S:
                return catalog, meta, warnings

    catalog, meta, fetch_warnings = fetch_litellm_snapshot()
    warnings += fetch_warnings
    if catalog is not None and meta is not None:
        try:
            write_catalog_atomically(catalog, meta)
            return catalog, meta, warnings
        except OSError as e:
            # section 4 (review): promotion failed partway -- write_catalog_atomically already
            # cleaned up its temp files and left the on-disk snapshot untouched, so fall through
            # to the same last-known-good fallback as a rejected/failed download, not a half
            # (or mismatched cache+meta) write.
            warnings.append(f"catalog cache not written ({type(e).__name__}); keeping last-known-good")
    # A rejected or failed download (or a write that couldn't be promoted) must never replace the
    # last-known-good snapshot on disk.
    stale_catalog, stale_meta = read_cache()
    if stale_catalog is not None:
        warnings.append("using stale cached catalog")
        return stale_catalog, stale_meta, warnings
    warnings.append("no catalog available; prices unknown (offline or network error)")
    return {}, None, warnings


def write_catalog_atomically(catalog, meta):
    """Promote a validated catalog+meta pair over the cached last-known-good snapshot atomically
    (section 1/4, review fix 4): both are written to temp files in CACHE_DIR first, and only
    os.replace()'d into place once BOTH temp writes succeeded -- a failure partway (e.g. disk full
    while writing meta) never leaves a cache file whose meta doesn't match it (or vice versa), and
    any temp file left behind by the failure is removed. Raises OSError on failure; the caller is
    responsible for falling back to the last-known-good snapshot, not this function."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f".tmp{os.getpid()}"
    cache_tmp = Path(str(LITELLM_CACHE) + suffix)
    meta_tmp = Path(str(LITELLM_META) + suffix)
    try:
        cache_tmp.write_text(json.dumps(catalog))
        meta_tmp.write_text(json.dumps(meta))
    except OSError:
        for tmp in (cache_tmp, meta_tmp):
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        raise
    os.replace(cache_tmp, LITELLM_CACHE)
    os.replace(meta_tmp, LITELLM_META)


def catalog_provenance(meta):
    """{repo, commit, path, fetched_at, age_h, stale} for route's `catalog` field (section 1/5), or
    None when there is no upstream provenance to report. `stale` is True once the snapshot is at
    or past CATALOG_HARD_STALE_S old -- route surfaces this as `stale_prices` plus a warning, never
    silently. Carries `etag`/`sha256` too when present in meta (fallback provenance / integrity)."""
    if not meta:
        return None
    fetched_at = _number(meta.get("fetched_at"))
    if fetched_at is None:
        return {"repo": meta.get("repo"), "commit": meta.get("commit"), "path": meta.get("path"),
                "fetched_at": None, "age_h": None, "stale": True}
    age_s = time.time() - fetched_at
    info = {"repo": meta.get("repo"), "commit": meta.get("commit"), "path": meta.get("path"),
            "fetched_at": fetched_at, "age_h": round(age_s / 3600, 2), "stale": age_s >= CATALOG_HARD_STALE_S}
    if meta.get("etag"):
        info["etag"] = meta["etag"]
    if meta.get("sha256"):
        info["sha256"] = meta["sha256"]
    return info


def find_agent_files():
    """Map agent stem -> path for *.md under the config agents dir and <cwd>/.claude/agents."""
    files = {}
    for base in [CLAUDE_CONFIG_DIR / "agents", Path.cwd() / ".claude" / "agents"]:
        if base.is_dir():
            for path in sorted(base.glob("*.md")):
                files[path.stem] = path
    return files


def parse_frontmatter(text):
    """Parse single-line scalar YAML-ish frontmatter (quoted or not)."""
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not match:
        return {}
    fields = {}
    for line in match[1].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*$", line)
        if m:
            fields[m[1]] = m[2].strip('"\'')
    return fields


PROXY_PREFIX = re.compile(r"^[a-z]+-[a-z]+-[a-z]+--")
SERVED_MODEL_SUFFIX = re.compile(r"\[[^\]]*\]$")  # context-window tag such as "[1m]"


def strip_proxy_prefix(model_id):
    """Strip a harness/proxy prefix of the form '<letters>-<letters>-<letters>--'."""
    return PROXY_PREFIX.sub("", model_id)


def proxy_route(model):
    """The frontmatter model when it carries a proxy route prefix (`proxy-claude-native--gpt-6-luna`),
    else None. Such an agent only reaches that model through the proxy."""
    model = (model or "").strip()
    return model if PROXY_PREFIX.match(model) else None


def proxy_active(env=None):
    """True when this session reaches proxy routes. FAST_DELEGATE_PROXY=1|0 overrides detection.

    Detection is empirical (env var NAMES a child process sees): a proxied session adds
    ANTHROPIC_BASE_URL together with CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY.
    A bare ANTHROPIC_BASE_URL can point at any gateway, so it counts only together
    with the gateway-discovery flag."""
    env = os.environ if env is None else env
    override = (env.get("FAST_DELEGATE_PROXY") or "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    if override:
        raise SystemExit(f"FAST_DELEGATE_PROXY={override!r} is not 1 or 0")
    return bool(env.get("ANTHROPIC_BASE_URL")) and bool(
        env.get("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"))


def drop_inactive_proxy_candidates(candidates, active=None):
    """(kept, reasons): without an active proxy, a proxy-routed candidate would run on the
    placeholder model while reporting its own id, so it is dropped before ranking."""
    active = proxy_active() if active is None else active
    if active:
        return candidates, []
    kept, reasons = [], []
    for c in candidates:
        if c.get("proxy_routed"):
            reasons.append(f"drop {c['id']}: proxy-routed ({c.get('proxy_route')}) but the proxy is not active "
                           f"in this session, so it would run on the placeholder model instead of "
                           f"{c.get('model_id')}; launch via a proxy-enabled session or set FAST_DELEGATE_PROXY=1")
        else:
            kept.append(c)
    return kept, reasons


def normalize_served_model(model_id):
    """Comparable form of a served or declared model id: proxy prefix, provider prefix, date
    suffix and context tag stripped; case-folded; dots read as dashes."""
    model = strip_proxy_prefix((model_id or "").strip().lower()).rsplit("/", 1)[-1]
    return DATE_SUFFIX.sub("", SERVED_MODEL_SUFFIX.sub("", model)).replace(".", "-")


def served_model_family(model_id):
    """Model family: the normalized id (see normalize_served_model) with every all-digit token
    removed, tokens being '-'-separated. Versions and dates are digit tokens, so claude-sonnet-5
    and claude-sonnet-5-5 are both 'claude-sonnet', gpt-6-luna and gpt-6.1-luna both 'gpt-luna',
    claude-haiku-4-5-20251001 is 'claude-haiku'. A token mixing letters and digits (gpt-4o, o3)
    is part of the name and kept. An id of only digit tokens is its own family."""
    normalized = normalize_served_model(model_id)
    kept = [t for t in normalized.split("-") if t and not t.isdigit()]
    return "-".join(kept) or normalized


def served_model_mismatches(expected_model_id, served):
    """The served model ids from a different family than the candidate's model id: the spawn did
    not reach the candidate's model (e.g. the placeholder model ran). A version difference inside
    one family is not a mismatch (see served_model_drifts)."""
    wanted = served_model_family(expected_model_id)
    return [m for m in served if served_model_family(m) != wanted]


def served_model_drifts(expected_model_id, served):
    """The served model ids in the candidate's family but with a different normalized id, e.g.
    alias 'sonnet' declared as claude-sonnet-5 while the harness serves claude-sonnet-5-5."""
    wanted = normalize_served_model(expected_model_id)
    family = served_model_family(expected_model_id)
    return [m for m in served if served_model_family(m) == family and normalize_served_model(m) != wanted]


def transcript_models(path):
    """Distinct assistant `message.model` values, in order, from a subagent JSONL transcript.
    Malformed lines and the `<synthetic>` placeholder are skipped; unreadable is a hard error."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError as e:
        raise SystemExit(f"--transcript {path!r} unreadable ({type(e).__name__})") from None
    models = []
    for line in lines:
        try:
            msg = json.loads(line).get("message")
        except (ValueError, AttributeError):
            continue
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        model = msg.get("model")
        if isinstance(model, str) and model and model != "<synthetic>" and model not in models:
            models.append(model)
    return models


def builtin_model_id(builtin):
    """A harness.json builtin's underlying model name (model_name, else legacy catalog_id, else id)."""
    return builtin.get("model_name") or builtin.get("catalog_id") or builtin["id"]


def declared_model_ids(harness=None):
    """{candidate id: declared underlying model id} from agent files, harness builtins and
    harnesses.codex.models. Reads no catalog and makes no request (unlike discover)."""
    harness = load_harness() if harness is None else harness
    placeholders = harness.get("model_id_placeholders", harness.get("placeholder_models", []))
    ids = {m: m for m in (harness.get("harnesses") or {}).get("codex", {}).get("models") or []}
    builtin_ids = {b["id"]: builtin_model_id(b) for b in harness.get("builtin", [])}
    for b in harness.get("builtin", []):
        ids[b["id"]] = builtin_model_id(b)
    for stem, path in sorted(find_agent_files().items()):
        fm = parse_frontmatter(path.read_text())
        cid = fm.get("name") or stem
        model_id = extract_model_id(cid, fm, fm.get("description", ""), placeholders)
        ids[cid] = builtin_ids.get(model_id, model_id)
    return ids


def extract_model_id(agent_name, frontmatter, description, placeholder_models=()):
    """Underlying model id: frontmatter model (minus proxy prefix) unless it is a placeholder,
    else the first token after 'Delegate work to ' in the description, else the agent name."""
    model = strip_proxy_prefix(frontmatter.get("model", "") or "")
    if model and model not in placeholder_models:
        return model
    match = re.search(r"Delegate work to (\S+)", description or "")
    if match:
        return match.group(1)
    return agent_name


def eligible_mode(entry):
    """True when a catalog entry's `mode` is "chat" or absent/unknown (section 3): embedding,
    image, audio, rerank, or moderation entries must never price a worker, in exact/normalized
    matching (match_catalog) or fuzzy shortlisting (match_shortlist)."""
    if not isinstance(entry, dict):
        return True
    mode = entry.get("mode")
    return mode is None or mode == "chat"


def match_catalog(model_id, catalog, provider_preference=()):
    """Return (catalog_key, "exact"|"normalized"|"none").

    Order: exact key; then keys whose provider prefix stripped equals the id; then the same
    after removing a trailing date suffix from both sides. Among matches prefer a bare key,
    then a provider listed in provider_preference (data), then any single provider, then
    nested prefixes; undated before dated; then shortest and lexical for determinism.
    Section 3: a key whose catalog `mode` isn't "chat"/absent is never eligible, exact or not."""
    if not catalog or not model_id:
        return None, "none"

    def eligible(key):
        return key not in IGNORED_CATALOG_KEYS and eligible_mode(catalog.get(key))

    if model_id in catalog and eligible(model_id):
        return model_id, "exact"

    def base(key):
        return key.rsplit("/", 1)[-1]

    def rank(key):
        parts = key.split("/")
        if len(parts) == 1:
            prefix_rank = 0
        elif len(parts) == 2 and parts[0] in provider_preference:
            prefix_rank = 1 + list(provider_preference).index(parts[0]) / (len(provider_preference) + 1)
        elif len(parts) == 2:
            prefix_rank = 2
        else:
            prefix_rank = 3
        dated = DATE_SUFFIX.search(base(key)) is not None
        return (prefix_rank, dated, len(key), key)

    matches = [k for k in catalog if eligible(k) and base(k) == model_id]
    if not matches:
        wanted = DATE_SUFFIX.sub("", model_id)
        matches = [k for k in catalog if eligible(k) and DATE_SUFFIX.sub("", base(k)) == wanted]
    if not matches:
        return None, "none"
    return min(matches, key=rank), "normalized"


# ------------------------------------------------ fuzzy catalog matching (section J)

def name_tokens(name):
    """Lowercase tokens of a model name: split on separators and letter/digit boundaries,
    reading "2p5" as "2.5" so "qwen2p5" and "qwen2.5" share tokens."""
    s = re.sub(r"(?<=\d)p(?=\d)", ".", name.lower())
    return {t for t in re.split(r"[/\-_.:@\s]+|(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", s) if t}


def largest_size(name):
    """Largest parameter-size token as an (n, m) pair, else None: "480b" -> (1, 480.0); a
    mixture-of-experts token "8x7b" -> (8, 7.0), a size distinct from "7b" (1, 7.0) or "8x22b"
    (8, 22.0) even though total-parameter products can coincide (fixes-j.md #1). Tokens with a
    leading letter (active-parameter tags like "a35b") are not sizes. "Largest" orders by n*m."""
    sizes = []
    for t in re.split(r"[/\-_:@\s]+", name.lower()):
        m = re.fullmatch(r"(\d+)x(\d+(?:\.\d+)?)b", t)
        if m:
            sizes.append((int(m.group(1)), float(m.group(2))))
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)b", t)
        if m:
            sizes.append((1, float(m.group(1))))
    return max(sizes, key=lambda p: p[0] * p[1]) if sizes else None


def specialization(name, tags):
    """The specialization tags (data, from harness.json) present in a name."""
    return name_tokens(name) & set(tags)


def match_shortlist(model_id, catalog, tags, generic=DEFAULT_GENERIC_NAME_TOKENS, limit=MATCH_SHORTLIST, seed=None):
    """Priced catalog keys that could be `model_id`, best first. Code drops the LiteLLM
    documentation key (`IGNORED_CATALOG_KEYS`), keys that share no non-generic word token
    (`generic`, data from harness.json `generic_name_tokens`; fixes-j.md #2), keys that state a
    different largest parameter size, or carry a different specialization set; the rest are
    ranked by token-overlap Jaccard plus 0.5 when every numeric token of `model_id` appears in
    the key. `seed` (section L: no exact model match as a decision) is a code exact/normalized
    catalog match for `model_id`, if any; when present it is placed first in the returned list,
    ahead of the code filters, so Jev still judges it rather than it being accepted outright."""
    mt = name_tokens(model_id)
    nums = {t for t in mt if t.isdigit()}
    generic = set(generic)
    size, spec = largest_size(model_id), specialization(model_id, tags)
    scored = []
    for key, e in catalog.items():
        if key in IGNORED_CATALOG_KEYS or key == seed:
            continue
        if not isinstance(e, dict) or not eligible_mode(e) or _number(e.get("input_cost_per_token")) is None:
            continue
        kt = name_tokens(key)
        if not {t for t in mt & kt if not t.isdigit() and t not in generic}:
            continue
        ksize = largest_size(key)
        if size is not None and ksize is not None and ksize != size:
            continue
        if specialization(key, tags) != spec:
            continue
        scored.append((len(mt & kt) / len(mt | kt) + (0.5 if nums <= kt else 0), key))
    scored.sort(key=lambda sk: (-sk[0], sk[1]))
    ranked = [k for _, k in scored]
    if seed is not None and seed in catalog and seed not in IGNORED_CATALOG_KEYS:
        ranked = [seed] + ranked
    return ranked[:limit]


def pick_price_entry(keys, catalog, provider_preference=()):
    """Choose the priced entry among same-model keys: a preferred provider prefix first (in
    order), then a bare key, then the median input price.

    B2: consider only priced keys first (fall back to the full set only when none has a price) --
    otherwise an unpriced seed (e.g. a code exact/normalized match with no price data, which
    `match_shortlist` always ranks first) can win purely for being a bare key, leaving a
    same-model priced entry unused and the candidate wrongly unpriced."""
    priced = [k for k in keys if _number(catalog.get(k, {}).get("input_cost_per_token")) is not None]
    keys = priced or keys
    for prov in provider_preference:
        pref = sorted(k for k in keys if k.split("/", 1)[0] == prov and "/" in k)
        if pref:
            return pref[0]
    bare = sorted(k for k in keys if "/" not in k)
    if bare:
        return bare[0]
    by_price = sorted(keys, key=lambda k: (catalog[k].get("input_cost_per_token") or 0, k))
    return by_price[(len(by_price) - 1) // 2]


def catalog_fingerprint(catalog):
    """Identifies a catalog version for the match cache: a content hash (S1), not key count plus
    a truncated-to-the-second mtime -- two different catalogs written within the same second used
    to share a fingerprint, so a stale cached catalog_id from the first catalog would raise
    KeyError when looked up against the second. Hash the sorted keys plus each entry's price
    (the fields the match cache actually depends on), so any catalog content change gets a new
    fingerprint regardless of file mtime resolution."""
    h = hashlib.sha256()
    for key in sorted(catalog):
        entry = catalog[key]
        cin = _number(entry.get("input_cost_per_token")) if isinstance(entry, dict) else None
        cout = _number(entry.get("output_cost_per_token")) if isinstance(entry, dict) else None
        h.update(f"{key}|{cin}|{cout}\n".encode())
    return h.hexdigest()[:16]


def match_cache_path():
    """$FAST_DELEGATE_STATE/catalog_matches.json, or FAST_DELEGATE_CACHE (T1) to point it
    elsewhere -- lets a read-only repro/review task isolate the match cache without also having
    to isolate the whole ledger state directory."""
    override = os.environ.get("FAST_DELEGATE_CACHE")
    return Path(override) if override else LEDGER.parent / "catalog_matches.json"


def jev_match_models(model_ids, catalog, harness, hints=None):
    """Resolve model names against the catalog. Returns ({model_id: result}, warnings, unjudged);
    result is {catalog_id|None, entries, score}. Code shortlists and filters; one Jev noul per
    surviving entry judges "same model"; accepted and rejected results are cached per catalog.
    `hints` (section L) is an optional {model_id: catalog_key} of a code exact/normalized match to
    seed the shortlist with, ranked first but still judged, not accepted outright.

    `unjudged` (B1) is the subset of `model_ids` that never got a Jev verdict this call -- because
    their batch failed/timed out, the key was unset, or the threshold/timeout env vars were
    unusable -- as opposed to a model that legitimately has no shortlist candidates (a real "no
    match" verdict). Only `unjudged` model ids should fall back to the unverified-exact/
    unverified-normalized code match; the rest, including Jev's explicit rejections, must not."""
    hints = hints or {}
    tags = harness.get("specialization_tags", DEFAULT_SPECIALIZATION_TAGS)
    generic = harness.get("generic_name_tokens", DEFAULT_GENERIC_NAME_TOKENS)
    providers = harness.get("catalog_provider_preference", [])
    fingerprint = catalog_fingerprint(catalog)
    path = match_cache_path()
    try:
        cache = json.loads(path.read_text())
    except (OSError, ValueError):
        cache = {}
    known_raw = cache.get(fingerprint, {}) if isinstance(cache, dict) else {}
    # S1 defense-in-depth: even with a content-hash fingerprint, drop any cached catalog_id that
    # is not present in the current catalog rather than trust it -- forces a rematch instead of a
    # KeyError or a wrong price further down.
    known = {}
    for m, r in known_raw.items() if isinstance(known_raw, dict) else []:
        cid = r.get("catalog_id") if isinstance(r, dict) else None
        if cid is not None and cid not in catalog:
            continue
        known[m] = r
    results = {m: known[m] for m in model_ids if m in known}
    todo = [m for m in model_ids if m not in known]
    warnings = []
    key, key_err = None, "TYPESAFE_API_KEY unset"
    try:
        threshold = float(os.environ.get("TYPESAFE_MATCH_THRESHOLD", str(MATCH_THRESHOLD)))
    except ValueError:
        return results, ["TYPESAFE_MATCH_THRESHOLD not numeric; fuzzy catalog match skipped"], set(todo)
    timeout, timeout_err = _typesafe_timeout()
    if timeout_err:
        return results, [f"{timeout_err}; fuzzy catalog match skipped"], set(todo)
    shortlists = {m: match_shortlist(m, catalog, tags, generic, seed=hints.get(m)) for m in todo}
    for m in [m for m in todo if not shortlists[m]]:
        results[m] = known[m] = {"catalog_id": None, "entries": [], "score": None}
    todo = [m for m in todo if shortlists[m]]
    if todo:
        key, _source, key_err = typesafe_key()
    if todo and not key:
        return results, [f"{key_err}; fuzzy catalog match skipped"], set(todo)
    unjudged = set()
    for i in range(0, len(todo), MATCH_MODELS_PER_REQUEST):
        batch = todo[i:i + MATCH_MODELS_PER_REQUEST]
        models, questions = {}, {}
        for n, m in enumerate(batch):
            models[f"m{n}"] = {"model_id": m, "entries": shortlists[m]}
            for j in range(len(shortlists[m])):
                questions[f"m{n}_e{j}"] = {"type": "noul", "instructions": MATCH_QUESTION_TEXT.format(m=f"m{n}", j=j),
                                           "criteria": MATCH_CRITERIA}
        body = {"model": os.environ.get("TYPESAFE_MODEL", "jev-latest"), "state": {"models": models},
                "questions": questions}
        payload, err = jev_request(body, timeout)
        answers = payload.get("answers") if isinstance(payload, dict) else None
        if err or not isinstance(answers, dict):
            warnings.append(f"fuzzy catalog match unavailable ({err or 'malformed answers'}) for {', '.join(batch)}")
            unjudged.update(batch)  # B1: only this batch is unjudged, not every candidate
            continue
        for n, m in enumerate(batch):
            probs = {}
            for j, k in enumerate(shortlists[m]):
                a = answers.get(f"m{n}_e{j}")
                if isinstance(a, dict) and _number(a.get("noul")) is not None:
                    probs[k] = float(a["noul"])
            same = [k for k, v in probs.items() if v >= threshold]
            pick = pick_price_entry(same, catalog, providers) if same else None
            results[m] = known[m] = {"catalog_id": pick, "entries": sorted(same),
                                     "score": round(probs[pick], 3) if pick else None}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({fingerprint: known}, indent=1))
    except OSError as e:
        warnings.append(f"catalog match cache not written ({type(e).__name__})")
    return results, warnings, unjudged


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def get_price_and_context(entry):
    """(price {input_per_m, output_per_m} | None, context | None, supports_tools | None). Never invents a price."""
    if not isinstance(entry, dict):
        return None, None, None
    cin, cout = _number(entry.get("input_cost_per_token")), _number(entry.get("output_cost_per_token"))
    price = {"input_per_m": cin * 1e6, "output_per_m": cout * 1e6} if cin is not None and cout is not None else None
    context = _number(entry.get("max_input_tokens"))
    if context is None:
        context = _number(entry.get("max_tokens"))
    tools = entry.get("supports_function_calling")
    return price, context, tools if isinstance(tools, bool) else None


def normalize_billing(raw):
    """{'mode', 'price_override', 'quota_weight', ...} from a builtin/override's optional
    `billing` field (section 2, data not rules). An unrecognized or missing mode defaults to
    "metered" so a candidate with no billing config prices exactly as before. Any extra keys on
    `raw` (e.g. a future `pool` for quota-aware selection) are preserved as-is, not dropped -- this
    is the one place billing/cost fields are read, kept small and pure so quota-aware selection can
    build on it without this function needing to know its field names in advance.

    Review fix A: when `raw` did NOT name one of VALID_BILLING_MODES (no billing config, or
    an unrecognized mode -- both already default to "metered"), the result also carries
    `billing_source: "default"` -- the only way route() can tell "no one configured a mode"
    (eligible to be inferred as subscription from a pool's quota snapshot) apart from "an operator
    explicitly chose metered" (which always wins, quota or not). An explicit mode adds no such key
    at all, so an explicitly-configured candidate's billing dict is byte-for-byte what it was
    previously -- existing exact-equality fixtures depend on this."""
    if not isinstance(raw, dict):
        return {"mode": "metered", "price_override": None, "quota_weight": None, "billing_source": "default"}
    result = dict(raw)
    explicit = raw.get("mode") in VALID_BILLING_MODES
    result["mode"] = raw.get("mode") if explicit else "metered"
    result["price_override"] = _number(raw.get("price_override"))
    result["quota_weight"] = _number(raw.get("quota_weight"))
    if not explicit:
        result["billing_source"] = "default"
    return result


def resolve_billing(cid, harness, inline=None):
    """Effective billing for candidate `cid` (section 2, review fix 2): harness.json's top-level
    id-keyed `billing` map (`{"<candidate id>": {mode, price_override, quota_weight, ...}}`) wins
    when it has an entry for `cid`; otherwise `inline` (a builtin's own `billing` field, if any) is
    used; otherwise the metered default. This map is the only way to billing-configure an
    agent-file or codex --spawnable candidate, since only harness.json builtins can carry an
    inline `billing` field -- called at every discover() candidate-construction site so billing is
    reachable for any candidate source, not just builtins."""
    by_id = harness.get("billing")
    raw = by_id.get(cid) if isinstance(by_id, dict) else None
    if raw is None:
        raw = inline
    billing = normalize_billing(raw)
    # Deployment metadata applies to the execution route, independently of model identity.
    for key in ("runtime", "provider"):
        if key not in billing and harness.get(key) is not None:
            billing[key] = harness[key]
    return billing


def cost_output_key(candidate):
    """"cost_usd" for a metered (or billing-unset) candidate, else "shadow_cost" (section 2):
    a subscription/local worker's LiteLLM-derived price is a shadow pricing proxy, never a
    real dollar figure, and must never be presented as one -- a $0 catalog price for such a
    worker must never be read as "free" either. One place route()/brief_output() both call."""
    mode = (candidate.get("billing") or {}).get("mode", "metered")
    return "shadow_cost" if mode != "metered" else "cost_usd"


def zero_price_warning(cid, price):
    """Warning string when `price` ({input_per_m, output_per_m}) has a literal $0 on either side,
    else None (section 2/3, review fix 3): a catalog price of exactly 0 must never be silently
    treated as free -- LiteLLM's own $0 entries are typically placeholders or free-tier footnotes,
    not a promise that routing to that worker costs nothing. Only an explicit
    `billing.price_override` may set a genuinely $0 (or any) rate; that override is applied after
    this check (see finalize_price), so it always wins."""
    if isinstance(price, dict) and (price.get("input_per_m") == 0 or price.get("output_per_m") == 0):
        return f"{cid}: catalog price is 0; not treated as free -- set billing.price_override"
    return None


def finalize_price(facts_dict, cid, billing, warnings):
    """Apply the zero-price safeguard (zero_price_warning) and then any `billing.price_override`
    to a freshly computed facts dict's `price`, in that order so an explicit override always wins
    (section 2/3, review fixes 1 and 3). Mutates and returns `facts_dict`; appends a warning to
    `warnings` when the zero-price safeguard fires. Called at every discover() candidate-
    construction site, and again after Jev re-resolves a candidate's catalog match, so neither
    fix can be bypassed by any path."""
    w = zero_price_warning(cid, facts_dict.get("price"))
    if w:
        warnings.append(w)
        facts_dict["price"] = None
    if billing.get("price_override") is not None:
        facts_dict["price"] = {"input_per_m": billing["price_override"], "output_per_m": billing["price_override"]}
    return facts_dict


def deprecation_warning(cid, entry):
    """A warning string when catalog `entry.deprecation_date` (YYYY-MM-DD) is in the past, else
    None (section 3). Never raises on a malformed date."""
    if not isinstance(entry, dict):
        return None
    date_str = entry.get("deprecation_date")
    if not isinstance(date_str, str):
        return None
    try:
        deprecated = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
    except ValueError:
        return None
    if deprecated < time.time():
        return f"{cid}: catalog deprecation_date {date_str} has passed"
    return None


# ---------------------------------------------------------------- quota-aware selection
#
# Pool assignment is data, not rules (see resolve_pool, wired into discover()'s three candidate-
# construction sites). Quota NEVER changes the runtime eligibility (tools, context, output
# filter) -- it only (a) removes blocked workers (filter_candidates) and (b) re-weights cost among
# runtime-eligible workers (rank_candidates' effective_cost). Only billing.mode "subscription"
# candidates are ever affected; metered and local candidates are untouched in this slice (metered
# spend-cap enforcement and local concurrency caps are read from harness.json's `quota` object but
# not yet enforced -- see SKILL.md's quota section for what's deferred and why). No network calls:
# every reader here is a local file read (or, for a manually-declared pool, CLI/env text).


def resolve_pool(billing, source, harness_name, model_id=None):
    """Effective quota pool for a candidate: explicit `billing['pool']` always wins;
    explicit runtime/provider metadata resolves upstream independently of the harness.
    Unconfigured slash-qualified spawnables remain unknown; native spawnables default
    to pool "codex" for compatibility. A harness.json builtin
    defaults to the active harness name (`harness_name`, itself None for the legacy harness-
    unaware discover() caller), and an agent-file candidate has no default -- pool stays unknown
    (quota simply doesn't apply, exactly like today) unless the billing map names one explicitly."""
    if billing.get("pool") is not None:
        return billing["pool"]
    provider = billing.get("provider")
    runtime = billing.get("runtime")
    if runtime == "proxy":
        return f"proxy:{provider}" if provider else None
    if provider:
        return provider
    if source == "spawnable":
        # Slash-qualified spawnables use provider/model selectors. Without deployment
        # metadata the upstream/account is unresolved, never the native Codex pool.
        return None if model_id and "/" in model_id else "codex"
    if source == "builtin":
        return harness_name
    return None


def apply_pool(billing, source, harness_name, model_id=None):
    """Mutate `billing` with resolve_pool's result, but only when it resolves to a real pool name
    -- an unresolved (None) pool must stay entirely absent from the dict, not present as an
    explicit `None`, so a candidate with no quota data keeps exactly the billing shape it had
    before quota-aware selection (dict equality in existing fixtures/tests depends on this). Returns `billing`."""
    pool = resolve_pool(billing, source, harness_name, model_id=model_id)
    if pool is not None:
        billing["pool"] = pool
    return billing


def _parse_epoch(value):
    """Epoch seconds from a numeric value or an ISO 8601 string (a trailing 'Z' accepted), else
    None -- never raises on a malformed or absent `resets_at` (signals drift and degrades
    to unknown, never a crash)."""
    num = _number(value)
    if num is not None:
        return num
    if isinstance(value, str) and value.strip():
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def human_local_time(epoch):
    """Local-time text for a `resets_at` epoch, or "unknown" -- JSON output always carries the
    epoch number itself; this is only for human-readable text (reasons, warnings)."""
    if epoch is None:
        return "unknown"
    try:
        return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(epoch))
    except (OSError, OverflowError, ValueError):
        return "unknown"


def normalize_window(raw, now, default_window_minutes):
    """One rate-limit window (`raw`: `{used_percentage|used_percent, resets_at, window_minutes?}`)
    normalized against `now`, or None when `raw` has no usable used_percentage (an absent
    or malformed window is never fatal, just unknown). `used_percent` (Codex's naming) is read the
    same as `used_percentage` (Claude's). A `resets_at` at or before `now` means the window has
    already rolled over: usage is reported as 0 regardless of what the snapshot said, and
    `elapsed_share` is None -- a window that just reset has no useful pace yet, so it must never
    read as "maximally ahead of pace". `elapsed_share` (0..1, how much of the window's time has
    elapsed) is derived from `resets_at` and `window_minutes` (the window's own, else
    `default_window_minutes`) when `resets_at` is known and in the future; else None (pace
    unknown -- `pressure()` treats that as zero elapsed, the conservative reading)."""
    if not isinstance(raw, dict):
        return None
    used = _number(raw.get("used_percentage"))
    if used is None:
        used = _number(raw.get("used_percent"))
    resets_at = _parse_epoch(raw.get("resets_at"))
    window_minutes = _number(raw.get("window_minutes")) or default_window_minutes
    if used is None:
        return None
    if resets_at is not None and resets_at <= now:
        return {"used_percentage": 0.0, "resets_at": resets_at, "elapsed_share": None}
    elapsed_share = None
    if resets_at is not None and window_minutes:
        window_s = window_minutes * 60
        if window_s > 0:
            elapsed_share = max(0.0, min(1.0, 1 - (resets_at - now) / window_s))
    return {"used_percentage": max(0.0, min(100.0, used)), "resets_at": resets_at, "elapsed_share": elapsed_share}


def pressure(used_fraction, elapsed_share=None, eps=QUOTA_PRESSURE_EPS):
    """Quota pressure in [eps, 1]: how far a subscription worker's shadow_cost should be
    scaled toward its real relative price as usage runs ahead of pace. `used_fraction` (0..1,
    fraction of the window's quota already used) and `elapsed_share` (0..1, fraction of the
    window's time already elapsed; None -- pace unknown -- is treated as 0 elapsed, so any usage
    at all then counts as ahead of an unknown pace, the conservative reading) combine into
    `ahead = max(0, used_fraction - elapsed_share)`: "60% used with 20 min left" is barely ahead;
    "60% used 1h into a 5h window" is well ahead. Pressure is ~eps (near-free) at or behind pace,
    rising steeply (quadratically in `ahead`) toward 1 (the worker's real shadow_cost) as usage
    pulls further ahead. `used_fraction=None` (usage itself unknown) -> None: unknown quota is
    never priced as either free or maxed out. Pure function, no I/O."""
    if used_fraction is None:
        return None
    used_fraction = max(0.0, min(1.0, used_fraction))
    elapsed = 0.0 if elapsed_share is None else max(0.0, min(1.0, elapsed_share))
    ahead = max(0.0, used_fraction - elapsed)
    return round(eps + (1 - eps) * ahead ** 2, 6)


def default_quota_thresholds(harness):
    """{warn_at, reserve, stale_five_hour_s, stale_seven_day_s, budget_usd_per_day,
    max_concurrent} from harness.json/override's top-level `quota` object (data, not rules); any
    missing or non-numeric field falls back to the module default (never blocks on a malformed
    threshold). `budget_usd_per_day`/`max_concurrent` are carried through for forward
    compatibility but not yet enforced in this slice (see the quota section header above)."""
    q = harness.get("quota") if isinstance(harness.get("quota"), dict) else {}

    def num(key, default):
        v = _number(q.get(key))
        return v if v is not None else default

    return {"warn_at": num("warn_at", QUOTA_WARN_AT), "reserve": num("reserve", QUOTA_RESERVE),
            "stale_five_hour_s": num("stale_five_hour_s", QUOTA_STALE_FIVE_HOUR_S),
            "stale_seven_day_s": num("stale_seven_day_s", QUOTA_STALE_SEVEN_DAY_S),
            "budget_usd_per_day": _number(q.get("budget_usd_per_day")),
            "max_concurrent": _number(q.get("max_concurrent"))}


def quota_dir():
    """$FAST_DELEGATE_STATE/quota -- where the Claude statusline helper writes claude.json, and
    where any other manually- or file-declared pool's snapshot lives."""
    return LEDGER.parent / "quota"


def load_quota_file(pool, now=None):
    """Normalized snapshot ({pool, basis, observed_at, five_hour, seven_day,
    rate_limit_reached_type, plan_type}) from `$FAST_DELEGATE_STATE/quota/<pool>.json`, or None --
    missing, unreadable, malformed, or carrying no usable window is never an error (never
    block on unknown/unparseable quota). This is the generic reader for any pool with a
    machine-written snapshot file: the Claude statusline helper writes quota/claude.json in
    exactly this shape; any other worker type (or an operator's own script) can write
    quota/<pool>.json the same way instead of using --quota every time."""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", pool):
        return None
    try:
        raw = json.loads((quota_dir() / f"{pool}.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    now = time.time() if now is None else now
    five = normalize_window(raw.get("five_hour"), now, CODEX_WINDOW_MINUTES["five_hour"])
    seven = normalize_window(raw.get("seven_day"), now, CODEX_WINDOW_MINUTES["seven_day"])
    if five is None and seven is None:
        return None
    return {"pool": pool, "basis": raw.get("basis", "unknown"),
            "observed_at": _number(raw.get("observed_at")), "five_hour": five, "seven_day": seven,
            "rate_limit_reached_type": raw.get("rate_limit_reached_type") or None,
            "plan_type": raw.get("plan_type")}


QUOTA_SPEC_RE = re.compile(r"^([^=]+)=(.+)$")
QUOTA_WINDOW_RE = re.compile(r"(5h|7d):(\d+(?:\.\d+)?)")


def parse_quota_specs(specs, source="--quota"):
    """({pool: {"five_hour": {...}, "seven_day": {...}}}, warnings) from
    `["pool=5h:NN,7d:NN", ...]` (CLI `--quota`, repeatable, or the `FAST_DELEGATE_QUOTA` env
    var's ';'-separated equivalent). A spec that doesn't parse, or names neither window, is
    skipped with a warning, never raised -- user-declared input is never authoritative enough to
    crash routing.

    Review fix 3: `source` (the caller-supplied label, "--quota" or "FAST_DELEGATE_QUOTA") is
    threaded into every warning so a malformed env-var spec is never misreported as a `--quota`
    flag problem (or vice versa) -- the two inputs come from different places and need different
    fixes."""
    out, warnings = {}, []
    for spec in specs or []:
        spec = spec.strip()
        if not spec:
            continue
        m = QUOTA_SPEC_RE.match(spec)
        if not m:
            warnings.append(f"{source} {spec!r} not understood (expected pool=5h:NN,7d:NN)")
            continue
        pool = m[1].strip()
        windows = {}
        for wm in QUOTA_WINDOW_RE.finditer(m[2]):
            key = "five_hour" if wm[1] == "5h" else "seven_day"
            windows[key] = {"used_percentage": float(wm[2])}
        if not pool or not windows:
            warnings.append(f"{source} {spec!r} names no usable pool/5h/7d value")
            continue
        out[pool] = windows
    return out, warnings


def manual_quota_snapshots(cli_specs, now=None):
    """{pool: snapshot} manually-declared quota from `cli_specs` (route --quota, repeatable) and the
    `FAST_DELEGATE_QUOTA` env var (the same `pool=5h:NN,7d:NN` syntax, ';'-separated for multiple
    pools) -- a CLI spec wins over an env spec for the same pool. `observed_at` is `now`, so a
    manual snapshot is fresh by construction and `resets_at` is unknown (no pacing signal; treated
    as 0 elapsed by `pressure()`, same as any other window with unknown pace)."""
    now = now if now is not None else time.time()
    env_specs = [s for s in os.environ.get("FAST_DELEGATE_QUOTA", "").split(";") if s.strip()]
    parsed, warnings = parse_quota_specs(env_specs, source="FAST_DELEGATE_QUOTA")
    cli_parsed, cli_warnings = parse_quota_specs(cli_specs, source="--quota")
    parsed.update(cli_parsed)
    warnings += cli_warnings
    snapshots = {}
    for pool, windows in parsed.items():
        snapshots[pool] = {
            "pool": pool, "basis": "manual", "observed_at": now,
            "five_hour": normalize_window(windows["five_hour"], now, CODEX_WINDOW_MINUTES["five_hour"])
                        if "five_hour" in windows else None,
            "seven_day": normalize_window(windows["seven_day"], now, CODEX_WINDOW_MINUTES["seven_day"])
                        if "seven_day" in windows else None,
            "rate_limit_reached_type": None, "plan_type": None}
    return snapshots, warnings


def codex_home():
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def codex_rollout_files(base=None):
    """Every `<CODEX_HOME>/sessions/**/rollout-*.jsonl`, newest mtime first (honours
    CODEX_HOME; an absent sessions dir is just an empty list, never an error)."""
    root = (base or codex_home()) / "sessions"
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("rollout-*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def extract_rate_limits_from_line(line):
    """The `rate_limits` dict nested in one rollout JSONL line's `event_msg`/`token_count`
    payload, or None -- never raises on malformed JSON, an unexpected shape, or a literal
    `"rate_limits": null` (Codex logs null for many short-session entries; rollout scanning
    requires going back past these). Checked at a couple of plausible nesting depths defensively,
    since Codex's own schema for this has drifted before."""
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict):
        return None
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else {}
    for holder in (msg, payload, rec):
        rl = holder.get("rate_limits")
        if isinstance(rl, dict):
            return rl
    return None


def normalize_codex_rate_limits(rl, now):
    """A raw Codex `rate_limits` dict ({primary, secondary}, each optionally carrying
    `used_percent`/`resets_at`/`plan_type`/`rate_limit_reached_type`) into the same normalized
    snapshot shape `load_quota_file` returns -- `primary` is the 5h window, `secondary` the
    weekly (7d) window."""
    primary, secondary = rl.get("primary"), rl.get("secondary")
    reached = next((w.get("rate_limit_reached_type") for w in (primary, secondary)
                    if isinstance(w, dict) and w.get("rate_limit_reached_type")), None)
    plan_type = next((w.get("plan_type") for w in (primary, secondary)
                      if isinstance(w, dict) and w.get("plan_type")), None)
    return {"pool": "codex", "basis": "codex-rollout", "observed_at": now,
            "five_hour": normalize_window(primary, now, CODEX_WINDOW_MINUTES["five_hour"]),
            "seven_day": normalize_window(secondary, now, CODEX_WINDOW_MINUTES["seven_day"]),
            "rate_limit_reached_type": reached, "plan_type": plan_type}


def tail_chunks(path, chunk_size=CODEX_TAIL_CHUNK_BYTES, max_bytes=CODEX_TAIL_MAX_BYTES):
    """Review fix 1: yield `path`'s last bytes read backward from EOF in chunks that double each
    time (chunk_size, 2x, 4x, ...) up to `max_bytes` total -- never a single whole-file
    `read_text()`. Each yielded string is everything from the current start point to EOF (decoded
    leniently: a read that starts mid multi-byte character only ever mangles the very first
    partial line, which the caller drops). Stops once the file start or `max_bytes` is reached.
    Never raises -- a stat/read error just ends the generator, same as an empty file."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    read = 0
    while read < size and read < max_bytes:
        read = min(size, max(chunk_size, read * 2), max_bytes)
        try:
            with path.open("rb") as f:
                f.seek(size - read)
                data = f.read(read)
        except OSError:
            return
        yield data.decode("utf-8", errors="ignore"), read >= size
        if read >= size or read >= max_bytes:
            return


def read_codex_quota(codex_home_dir=None, now=None):
    """Read-only, null-tolerant Codex quota snapshot: tails the newest
    `<CODEX_HOME>/sessions/**/rollout-*.jsonl` files, scanning each file's lines newest-first and
    falling through to the next (older) file when none has a non-null `rate_limits` anywhere --
    short sessions often log only null entries. Never polls a network endpoint; returns None (not
    an error) when no file has ever recorded real rate limits, which the caller reports as
    "quota unknown for codex", never a block.

    Review fix 1 (reproduced: a 22.9 MB rollout fully read to find one line near EOF): reads each
    file via `tail_chunks` -- growing, bounded chunks from EOF -- instead of `read_text()` on the
    whole file, and looks at only the newest `CODEX_TAIL_MAX_FILES` rollout files (already
    newest-mtime-first from `codex_rollout_files`), never the full session history."""
    now = now if now is not None else time.time()
    for path in codex_rollout_files(codex_home_dir)[:CODEX_TAIL_MAX_FILES]:
        for text, is_whole_file in tail_chunks(path):
            lines = text.split("\n")
            if not is_whole_file and lines:
                lines = lines[1:]  # the very first line of a partial-from-EOF read may be truncated
            for line in reversed(lines):
                rl = extract_rate_limits_from_line(line)
                if rl:
                    snap = normalize_codex_rate_limits(rl, now)
                    rec = json.loads(line)
                    snap["observed_at"] = _parse_epoch(rec.get("timestamp"))
                    return snap
    return None


class QuotaAdapter:
    """Common read-only interface. Readers are injected; no adapter polls private APIs."""

    def __init__(self, reader, source):
        self.reader = reader
        self.source = source

    def read(self, pool, now):
        snapshot = self.reader(pool, now)
        if snapshot is not None and not any(snapshot.get(key) for key in ("five_hour", "seven_day")):
            snapshot = None
        if snapshot is not None:
            snapshot = {**snapshot, "pool": pool, "source": self.source,
                        "availability": "cached"}
        return snapshot

    def report(self, pool, now, thresholds=None):
        snapshot = self.read(pool, now)
        evidence = quota_evidence(pool, snapshot, thresholds or default_quota_thresholds({}), now)
        evidence["source"] = self.source
        return {**evidence, "snapshot": snapshot}


class ClaudeQuotaAdapter(QuotaAdapter):
    def __init__(self, reader=None):
        super().__init__(reader or (lambda pool, now: load_quota_file(pool, now=now)),
                         "claude-statusline")


class CodexQuotaAdapter(QuotaAdapter):
    def __init__(self, reader=None):
        super().__init__(reader or (lambda pool, now: read_codex_quota(now=now)),
                         "codex-rollout")


class CursorQuotaAdapter(QuotaAdapter):
    def __init__(self, reader=None):
        # Admin spend is not included quota. Only an operator's normalized snapshot is used.
        super().__init__(reader or (lambda pool, now: load_quota_file(pool, now=now)),
                         "cursor-snapshot")


class ProxyQuotaAdapter(QuotaAdapter):
    def __init__(self, reader=None, root=None):
        default = Path(os.environ.get("FAST_DELEGATE_STATE", Path.home() / ".local/state/fast-delegate")) / "proxy"
        self.root = Path(root) if root is not None else default
        super().__init__(reader or self.read_cache, "proxy-cache")

    def read_cache(self, pool, now):
        # Router is not a provider: require explicit upstream. Multiple accounts are
        # ambiguous without a verified selection contract; never publish their keys.
        if not pool.startswith("proxy:"):
            return None
        provider = pool.split(":", 1)[1]
        filename = "codex-quota-cache.json" if provider == "openai" else "provider-account-quota-cache.json"
        try:
            raw = json.loads((self.root / filename).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return None
        rows = raw.get("quotas" if provider == "openai" else "rows")
        if not isinstance(rows, dict):
            return None
        values = list(rows.values()) if provider == "openai" else [
            value for key, value in rows.items() if key.startswith(provider + "\0")]
        if len(values) != 1 or not isinstance(values[0], dict):
            return None
        row = values[0]
        observed = _number(row.get("updatedAt"))
        if observed is None or observed / 1000 > now:
            return None
        windows = {}
        for name, percent, reset, minutes in (
                ("five_hour", "fiveHourPercent", "fiveHourResetAt", 300),
                ("seven_day", "weeklyPercent", "weeklyResetAt", 10080)):
            used = _number(row.get(percent))
            reset_at = _number(row.get(reset))
            if reset_at is not None and reset_at > 10_000_000_000:
                reset_at /= 1000
            # Codex short windows are not necessarily five hours; leave unknown.
            valid = used is not None and 0 <= used <= 100
            unexpired = reset_at is None or reset_at > now
            windows[name] = normalize_window(
                {"used_percentage": used, "resets_at": reset_at}, now, minutes
            ) if valid and unexpired else None
        if not any(windows.values()):
            return None
        return {"pool": pool, "basis": filename, "observed_at": observed / 1000,
                "rate_limit_reached_type": None, "plan_type": None, **windows}


def quota_adapters():
    return {"claude": ClaudeQuotaAdapter(), "codex": CodexQuotaAdapter(),
            "cursor": CursorQuotaAdapter(), "proxy": ProxyQuotaAdapter()}


def gather_quota_snapshots(pools, cli_quota_specs, now=None, adapters=None):
    """Manual overrides win. Dispatch by pool, never by the worker's runtime/model family."""
    now = now if now is not None else time.time()
    manual, warnings = manual_quota_snapshots(cli_quota_specs, now=now)
    registry = adapters if adapters is not None else quota_adapters()
    out = {}
    for pool in pools:
        if pool in manual:
            out[pool] = {**manual[pool], "source": "manual", "availability": "manual"}
        else:
            adapter = registry.get(pool.split(":", 1)[0])
            if adapter is None:
                adapter = QuotaAdapter(lambda p, n: load_quota_file(p, now=n), "snapshot")
            out[pool] = adapter.read(pool, now)
    return out, warnings


def quota_evidence(pool, snapshot, thresholds, now):
    """Safe source/availability/freshness metadata even when normalized quota is unknown."""
    source = snapshot.get("source", snapshot.get("basis")) if snapshot else {
        "claude": "claude-statusline", "codex": "codex-rollout",
        "cursor": "cursor-snapshot", "proxy": "proxy-cache"
    }.get(pool.split(":", 1)[0], "snapshot")
    observed = snapshot.get("observed_at") if snapshot else None
    state = pool_status(snapshot, thresholds, now)
    freshness = "unknown" if observed is None or observed > now else (
        "stale" if state and state["stale"] else "fresh")
    return {"pool": pool, "source": source,
            "availability": snapshot.get("availability", "cached") if snapshot else "unavailable",
            "observed_at": observed, "freshness": freshness, "live": False,
            "reason": None if snapshot else (
                "No normalized snapshot; admin spend is not included quota" if pool == "cursor"
                else "No usable source data or ambiguous account selection")}


def pool_status(snapshot, thresholds, now):
    """Full per-pool classification for route() ({pool, basis, observed_at, five_hour, seven_day,
    resets_at, status, pressure, stale}), or None when `snapshot` is None (unknown quota).
    `status` is the worst of "ok"/"warn"/"reserve"/"exhausted" across both windows (plus a set
    `rate_limit_reached_type` forcing "exhausted" outright); `resets_at` is the soonest reset
    among whichever window(s) caused a "reserve"/"exhausted" status (for `wait_until`); `pressure`
    is the max of both windows' `pressure()` (quota-aware selection: "max(u5h_ahead, u7d_ahead)");
    `stale` is set once either window's snapshot age exceeds its own stale threshold."""
    if not snapshot:
        return None
    warn_at, reserve = thresholds["warn_at"], thresholds["reserve"]
    observed_at = snapshot.get("observed_at")
    age = (now - observed_at) if observed_at is not None else None
    out_windows, statuses, pressures, blocking_resets = {}, [], [], []
    for key, stale_after in (("five_hour", thresholds["stale_five_hour_s"]),
                              ("seven_day", thresholds["stale_seven_day_s"])):
        w = snapshot.get(key)
        if w is None:
            out_windows[key] = None
            continue
        used = w["used_percentage"]
        remaining = 100.0 - used
        p = pressure(used / 100.0, w.get("elapsed_share"))
        stale = age is not None and stale_after is not None and age > stale_after
        if used >= 100.0:
            status = "exhausted"
        elif remaining < reserve:
            status = "reserve"
        elif used >= warn_at:
            status = "warn"
        else:
            status = "ok"
        statuses.append(status)
        pressures.append(p)
        if status in ("exhausted", "reserve") and w.get("resets_at") is not None:
            blocking_resets.append(w["resets_at"])
        out_windows[key] = {"used_percentage": used, "resets_at": w.get("resets_at"), "stale": stale}
    if snapshot.get("rate_limit_reached_type"):
        statuses.append("exhausted")
    if not statuses:
        return None
    rank = {"exhausted": 3, "reserve": 2, "warn": 1, "ok": 0}
    status = max(statuses, key=lambda s: rank[s])
    return {"pool": snapshot.get("pool"), "basis": snapshot.get("basis"), "observed_at": observed_at,
            "five_hour": out_windows["five_hour"], "seven_day": out_windows["seven_day"],
            "resets_at": min(blocking_resets) if blocking_resets else None, "status": status,
            "pressure": max((p for p in pressures if p is not None), default=None),
            "stale": any(w["stale"] for w in out_windows.values() if w)}


def effective_cost(cost, billing, quota_by_pool, ignore_quota):
    """`cost` re-weighted by quota pressure: unchanged for metered/local billing, when
    `ignore_quota` is set, or when the candidate's pool has no known pressure; for a subscription
    candidate whose pool's pressure is known, `cost * pressure`. This only changes ranking (and,
    through it, which candidate `decide()` treats as cheapest) -- the reported cost_usd/
    shadow_cost value itself is never altered."""
    if cost is None or ignore_quota:
        return cost
    if (billing or {}).get("mode") != "subscription":
        return cost
    pool = (billing or {}).get("pool")
    qstate = (quota_by_pool or {}).get(pool) if pool else None
    p = qstate.get("pressure") if qstate else None
    return cost if p is None else round(cost * p, 8)


def candidate_state(cid, disabled_ids, probation_ids, stats):
    """Explicit operator state; observed outcomes never enable or disable a worker."""
    return "disabled" if cid in disabled_ids else "probation" if cid in probation_ids else "enabled"


def discover(refresh=False, use_jev=False, harness_name=None, spawnable=None):
    """Return (candidates, warnings). Prints nothing.

    Section L (no exact model match as a decision): every enabled or probation candidate's
    model name goes through the Jev pipeline and its catalog-version cache, independently of
    `use_jev` (which controls semantic task fit in route()). The code exact/normalized match is
    only a shortlist seed, never accepted outright. With no cache hit and no configured key, or
    on an unavailable request, an exact/normalized seed is retained only as `unverified-*` and
    a warning explains the outage; prices are never invented. Explicit Jev rejections remain
    unmatched.

    Section N (only suggest workers the active CLI harness can spawn): `harness_name` is None
    for the legacy, harness-unaware behavior (every *.md agent file plus every harness.json
    `builtin`, unfiltered -- unchanged for any caller that doesn't opt in), `HARNESS_NATIVE`
    (the Agent-tool harness), or "codex". "codex" does no agent-file discovery at all; every
    candidate comes from `spawnable` (or the harness.json/override `harnesses.codex.models`
    default), one per spawnable model name, priced through the same catalog pipeline as any
    other candidate id. `HARNESS_NATIVE` candidates are the usual agent-file + builtin
    discovery, filtered down to `spawnable` (or the matching `harnesses.<name>.models`) when
    either is given; that discovery with neither falls back to the unfiltered list with a
    warning. Either way, a candidate outside the spawnable set is dropped before any Jev request
    (fuzzy catalog matching or fit judging), each with its own "not spawnable in <harness>" reason
    in `warnings`."""
    harness = load_harness()
    override_path = harness_override_path()
    providers = harness.get("catalog_provider_preference", [])
    model_id_placeholders = harness.get("model_id_placeholders", harness.get("placeholder_models", []))
    disabled_ids = {d["id"] for d in harness.get("disabled", [])}
    probation_ids = {d["id"] for d in harness.get("probation", [])}
    stats = ledger_stats()
    catalog, warnings = fetch_litellm_catalog(refresh=refresh)
    if override_path:
        warnings.append(f"using harness override: {override_path}")

    def facts(model_id):
        key, how = match_catalog(model_id, catalog, providers)
        entry = catalog.get(key) if key else None
        price, context, tools = get_price_and_context(entry)
        return {"model_id": model_id, "catalog_id": key, "catalog_match": how,
                "price": price, "context": context, "supports_tools": tools, "catalog_entry": entry}

    harnesses_cfg = harness.get("harnesses", {})

    def model_configuration(source, billing=None):
        value = source.get("model_configuration", {}) if isinstance(source, dict) else {}
        config = dict(value) if isinstance(value, dict) else {}
        if billing and billing.get("reasoning_effort") is not None:
            config.setdefault("reasoning_effort", billing["reasoning_effort"])
        return config

    if harness_name == "codex":
        spawn_list = spawnable if spawnable is not None else harnesses_cfg.get("codex", {}).get("models")
        if spawn_list is None:
            raise SystemExit("pass --spawnable with the candidate model selectors available to your harness")
        candidates = []
        for m in spawn_list:
            state = candidate_state(m, disabled_ids, probation_ids, stats)
            model_selector = m
            codex_config = harnesses_cfg.get("codex", {})
            configured_model = (codex_config.get("model_configuration", {}) or {}).get(m)
            # Read legacy operator overrides as model facts, never as tool-call templates.
            if configured_model is None:
                old_effort = (codex_config.get("reasoning_effort", {}) or {}).get(m)
                configured_model = {"reasoning_effort": old_effort} if old_effort is not None else {}
            model_configuration_data = {"model_configuration": configured_model}
            # Review fix 2: billing is reachable for a codex --spawnable candidate too, via
            # harness.json's top-level id-keyed `billing` map (spawnable candidates have no
            # per-entry harness.json record to carry an inline `billing` field).
            billing = resolve_billing(m, harness)
            apply_pool(billing, "spawnable", harness_name, model_id=m)  # quota-aware selection
            model_configuration_data["model_configuration"] = model_configuration(model_configuration_data, billing)
            f = finalize_price(facts(m), m, billing, warnings)
            candidates.append({"id": m, "model_selector": model_selector, **model_configuration_data, **f, "description": "",
                               "enabled": state != "disabled", "state": state, "source": "spawnable",
                               "billing": billing})
    else:
        candidates = []
        for stem, path in sorted(find_agent_files().items()):
            fm = parse_frontmatter(path.read_text())
            cid = fm.get("name") or stem
            model_id = extract_model_id(cid, fm, fm.get("description", ""), model_id_placeholders)
            state = candidate_state(cid, disabled_ids, probation_ids, stats)
            # Review fix 2: same as above -- an agent-file candidate has no inline `billing`
            # field of its own, so the id-keyed map is the only way to billing-configure it.
            billing = resolve_billing(cid, harness)
            apply_pool(billing, "agent-file", harness_name)
            f = finalize_price(facts(model_id), cid, billing, warnings)
            route_model = proxy_route(fm.get("model"))
            candidates.append({"id": cid, "model_selector": cid,
                               "model_configuration": model_configuration({}, billing),
                               **f, "description": fm.get("description", ""),
                               "enabled": state != "disabled", "state": state, "source": str(path),
                               "billing": billing, "proxy_routed": route_model is not None,
                               "proxy_route": route_model})
        for b in harness.get("builtin", []):
            state = candidate_state(b["id"], disabled_ids, probation_ids, stats)
            # L2: built-ins give model_name, a descriptive name the pipeline resolves; catalog_id
            # (a pinned id) is only a compatibility fallback for harnesses that still set it.
            model_id = builtin_model_id(b)
            # Review fix 2: the top-level id-keyed `billing` map wins over a builtin's own inline
            # `billing` field (resolve_billing's `inline` fallback), not just replace it silently.
            billing = resolve_billing(b["id"], harness, inline=b.get("billing"))
            apply_pool(billing, "builtin", harness_name)
            f = finalize_price(facts(model_id), b["id"], billing, warnings)
            candidates.append({"id": b["id"], "model_selector": b.get("model_selector", b.get("model", b["id"])),
                               "model_configuration": model_configuration(b, billing),
                               **f, "description": b.get("description", ""),
                               "enabled": state != "disabled", "state": state, "source": "builtin",
                               "billing": billing})
        if harness_name == HARNESS_NATIVE:
            spawn_list = spawnable if spawnable is not None else harnesses_cfg.get(HARNESS_NATIVE, {}).get("models")
            if spawn_list is None:
                warnings.append("spawnable list not given; assuming agents dir + builtins")
            else:
                spawn_set = set(spawn_list)
                kept = []
                for c in candidates:
                    if c["id"] in spawn_set or c.get("model_selector") in spawn_set:
                        kept.append(c)
                    else:
                        warnings.append(f"drop {c['id']}: not spawnable in {harness_name}")
                candidates = kept

    live = [c for c in candidates if c["model_id"] and c["state"] != "disabled"]
    if live:
        names = sorted({c["model_id"] for c in live})
        # Seed the shortlist with the code match (exact/normalized) so it is ranked first, but
        # Jev still must confirm it; model identity matching is independent of fit judging.
        hints = {}
        for c in live:
            if c["catalog_match"] != "none" and c["model_id"] not in hints:
                hints[c["model_id"]] = c["catalog_id"]
        found, match_warnings, unjudged = jev_match_models(names, catalog, harness, hints=hints) if names else ({}, [], set())
        warnings = warnings + match_warnings
        used_unverified_seed = False
        for c in live:
            hit = found.get(c["model_id"])
            # S1 defense-in-depth: a cached/returned catalog_id that isn't in *this* catalog must
            # never be dereferenced (would KeyError, or silently resurrect a stale price).
            entry = catalog.get(hit["catalog_id"]) if hit and hit.get("catalog_id") else None
            if hit and hit.get("catalog_id") and entry is not None:
                price, context, tools = get_price_and_context(entry)
                w = zero_price_warning(c["id"], price)  # review fix 3: a $0 jev-matched price too
                if w:
                    warnings.append(w)
                    price = None
                c.update(catalog_id=hit["catalog_id"], catalog_match="jev", price=price, context=context,
                         supports_tools=tools, catalog_entry=entry, match_entries=hit["entries"],
                         match_score=hit["score"])
            elif c["model_id"] in unjudged and c["catalog_match"] in ("exact", "normalized"):
                # B1: only a candidate whose own batch was unjudged falls back to the unverified
                # code match; a candidate Jev actually judged and rejected must not be revived.
                c["catalog_match"] = f"unverified-{c['catalog_match']}"
                used_unverified_seed = True
            else:
                c.update(catalog_id=None, catalog_match="none", price=None, context=None,
                         supports_tools=None, catalog_entry=None)
            # Review fix 1: re-apply billing.price_override after Jev resolution either way -- a
            # hit overwrites `price` with the (possibly None) catalog price above, and a miss/no-
            # match wipes it to None in the `else` branch; an explicit override must survive both,
            # not just the initial facts() call before use_jev ran.
            billing = c.get("billing") or {}
            if billing.get("price_override") is not None:
                c["price"] = {"input_per_m": billing["price_override"], "output_per_m": billing["price_override"]}
        if used_unverified_seed:
            warnings.append("catalog matches unverified (configure TYPESAFE_API_KEY to verify with Jev)")
    # Section 3: a passed deprecation_date warns regardless of jev/filtering -- no silent failure.
    for c in candidates:
        w = deprecation_warning(c["id"], c.get("catalog_entry"))
        if w:
            warnings.append(w)
    active = proxy_active()
    for c in candidates:
        c.setdefault("proxy_routed", False)
        c["proxy_active"] = active
    return candidates, warnings


def print_discovery(candidates, warnings, as_json=False, out=sys.stdout):
    if as_json:
        print(json.dumps({"candidates": candidates, "warnings": warnings, "proxy_active": proxy_active()}, indent=1), file=out)
        return
    print(f"Discovered {len(candidates)} candidates", file=out)
    for c in candidates:
        price = c["price"]
        price_str = f" in ${price['input_per_m']:.2f}/M out ${price['output_per_m']:.2f}/M" if price else " price unknown"
        ctx_str = f" ctx={c['context']}" if c["context"] is not None else ""
        status = c.get("state", "enabled" if c["enabled"] else "disabled")
        proxy_str = " proxy-routed" if c.get("proxy_routed") else ""
        print(f"  {c['id']:20} {c['catalog_match']:10} {str(c['catalog_id']):24} {status:10}{price_str}{ctx_str}{proxy_str}", file=out)
    for w in warnings:
        print(f"Warning: {w}", file=out)


# ---------------------------------------------------------------- ledger

def ledger_stats():
    """{(family, candidate): {"accepted": n, "rejected": n}}; malformed lines are skipped."""
    stats = {}
    if not LEDGER.is_file():
        return stats
    for line in LEDGER.read_text().splitlines():
        try:
            rec = json.loads(line)
            key = (rec["family"], rec["candidate"])
            outcome = rec["outcome"]
        except (ValueError, KeyError, TypeError):
            continue
        if outcome in ("accepted", "rejected"):
            stats.setdefault(key, {"accepted": 0, "rejected": 0})[outcome] += 1
    return stats


def ledger_rate(stats, family, cid):
    """(accepted, n, rate|None) for one family x candidate."""
    s = stats.get((family, cid), {"accepted": 0, "rejected": 0})
    n = s["accepted"] + s["rejected"]
    return s["accepted"], n, (s["accepted"] / n if n else None)


def ledger_rate_all_families(stats, cid):
    """(accepted, n, rate|None) for one candidate summed across every family."""
    accepted = sum(s["accepted"] for (_, c), s in stats.items() if c == cid)
    n = sum(s["accepted"] + s["rejected"] for (_, c), s in stats.items() if c == cid)
    return accepted, n, (accepted / n if n else None)


def ledger_tokens():
    """{(family, candidate): [tokens ints]} from ledger lines with a positive numeric `tokens` field."""
    out = {}
    if not LEDGER.is_file():
        return out
    for line in LEDGER.read_text().splitlines():
        try:
            rec = json.loads(line)
            key = (rec["family"], rec["candidate"])
            tok = _number(rec.get("tokens"))
        except (ValueError, KeyError, TypeError):
            continue
        if tok is not None and tok > 0:
            out.setdefault(key, []).append(tok)
    return out


def ledger_recent(cid, limit=5):
    """Up to `limit` most recent ledger records for one candidate (any family), newest first."""
    if not LEDGER.is_file():
        return []
    recs = []
    for line in LEDGER.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("candidate") != cid:
            continue
        recs.append({"family": rec.get("family"), "outcome": rec.get("outcome"),
                     "note": rec.get("note", ""), "tokens": _number(rec.get("tokens"))})
    return list(reversed(recs[-limit:]))


def median(values):
    """Median of a non-empty list of numbers, or None."""
    values = sorted(values)
    n = len(values)
    if n == 0:
        return None
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2


def in_out_ratio(task):
    """(input_share, output_share) from the task's est_input:est_output tokens, summing to 1."""
    est_in = int(task.get("est_input_tokens", 20000))
    est_out = int(task.get("est_output_tokens", 2000))
    denom = est_in + est_out
    return (est_in / denom, est_out / denom) if denom else (0.5, 0.5)


def _explicit_token_floor(task):
    """(explicit_in, explicit_out): explicit minimum token requirements from the task, or None
    when the field is absent. Distinguishes an explicit 0 from an absent field."""
    raw_in = task.get("est_input_tokens")
    raw_out = task.get("est_output_tokens")
    explicit_in = int(raw_in) if raw_in is not None else None
    explicit_out = int(raw_out) if raw_out is not None else None
    return explicit_in, explicit_out


def _explicit_total_floor(task):
    """Minimum total needed for the unchanged task input/output split to meet explicit sides."""
    explicit_in, explicit_out = _explicit_token_floor(task)
    ratio_in, ratio_out = in_out_ratio(task)
    floors = []
    if explicit_in is not None and explicit_in > 0:
        floors.append(explicit_in / ratio_in)
    if explicit_out is not None and explicit_out > 0:
        floors.append(explicit_out / ratio_out)
    return max(floors, default=0)


def _estimate_tokens_detail(task, family, cid, token_stats):
    """Return (total, basis, floor_applied, required_total) for cost reporting."""
    per_cid = token_stats.get((family, cid), [])
    if per_cid:
        learned, basis = median(per_cid), "candidate"
    else:
        fam_all = [t for (fam, _c), toks in token_stats.items() if fam == family for t in toks]
        if fam_all:
            learned, basis = median(fam_all), "family"
        else:
            est_in = int(task.get("est_input_tokens", 20000))
            est_out = int(task.get("est_output_tokens", 2000))
            return est_in + est_out, "estimate", False, None
    required_total = _explicit_total_floor(task)
    if required_total and learned < required_total:
        return required_total, basis, True, required_total
    return learned, basis, False, required_total or None


def estimate_tokens(task, family, cid, token_stats):
    """(total_tokens, basis): learned median for candidate, then family, else task estimate.

    Explicit per-side minimums may raise a learned total to the smallest total that meets
    them under the task's existing input/output ratio. Implicit defaults never floor learning."""
    total, basis, _floor_applied, _required_total = _estimate_tokens_detail(task, family, cid, token_stats)
    return total, basis


def split_tokens(total, task):
    """(input_tokens, output_tokens) splitting total by the task's est_input:est_output ratio."""
    ratio_in, ratio_out = in_out_ratio(task)
    input_tokens = total * ratio_in
    output_tokens = total - input_tokens
    return input_tokens, output_tokens


def blended_price(price, ratio_in, ratio_out):
    """$/M blended at the given input/output share; price is {input_per_m, output_per_m}."""
    return price["input_per_m"] * ratio_in + price["output_per_m"] * ratio_out


def price_tokens(price, input_tokens, output_tokens):
    """cost_usd for a token split at `price`, or None when price is unknown."""
    if not price:
        return None
    return round(input_tokens * price["input_per_m"] / 1e6 + output_tokens * price["output_per_m"] / 1e6, 8)


def tiered_rate(entry, base_field, input_tokens):
    """(rate_per_token, catalog_field_name) for `base_field` ("input_cost_per_token" or
    "output_cost_per_token"): the entry's `<base_field>_above_200k_tokens` / `_above_272k_tokens`
    rate once `input_tokens` crosses that tier's threshold and the entry defines a rate for it
    (LiteLLM bills the whole request at the tier rate once the prompt crosses the threshold, not a
    marginal blend -- section 4); else the plain `base_field` rate. (None, None) when neither is
    defined on `entry`."""
    for threshold in sorted(TOKEN_TIER_THRESHOLDS, reverse=True):
        if input_tokens > threshold:
            field = f"{base_field}_above_{threshold // 1000}k_tokens"
            rate = _number(entry.get(field))
            if rate is not None:
                return rate, field
    rate = _number(entry.get(base_field))
    return (rate, base_field) if rate is not None else (None, None)


def price_tokens_full(entry, price, input_tokens, output_tokens, cached_share=0.0):
    """(cost_usd, price_basis) for a token split, honoring cached-input pricing
    (`cache_read_input_token_cost` priced at the task's `est_cached_input_share`) and tiered
    above-threshold input/output rates (section 4) when the catalog entry `entry` defines them;
    falls back to the plain `price` ({input_per_m, output_per_m}) otherwise. Returns (None, None)
    when `price` is unknown -- never invents a price. With cached_share=0 and no tiered/cache
    fields on `entry` this reproduces price_tokens exactly (metered-worker cost estimates are
    unchanged); `price_basis` is a list naming the catalog fields actually used."""
    if not price:
        return None, None
    entry = entry if isinstance(entry, dict) else {}
    cached_share = max(0.0, min(1.0, _number(cached_share) or 0.0))
    cache_rate = _number(entry.get("cache_read_input_token_cost"))
    cached_tokens = input_tokens * cached_share
    basis, cost = [], 0.0
    if cached_tokens > 0 and cache_rate is not None:
        cost += cached_tokens * cache_rate
        basis.append("cache_read_input_token_cost")
        plain_input_tokens = input_tokens - cached_tokens
    else:
        plain_input_tokens = input_tokens
    in_rate, in_field = tiered_rate(entry, "input_cost_per_token", input_tokens)
    if in_rate is None:
        in_rate, in_field = price["input_per_m"] / 1e6, "input_per_m"
    cost += plain_input_tokens * in_rate
    basis.append(in_field)
    out_rate, out_field = tiered_rate(entry, "output_cost_per_token", input_tokens)
    if out_rate is None:
        out_rate, out_field = price["output_per_m"] / 1e6, "output_per_m"
    cost += output_tokens * out_rate
    basis.append(out_field)
    return round(cost, 8), basis


# ---------------------------------------------------------------- routing (data-driven only)

def filter_candidates(candidates, task, stats, quota_by_pool=None, ignore_quota=False):
    """Apply hard filters. Returns (kept, reasons).

    `quota_by_pool` ({pool: pool_status() result}, default None) and `ignore_quota` add
    one more hard filter, after the existing ones and alongside the runtime checks: a subscription-mode
    candidate whose pool is at "reserve" or "exhausted" is skipped with a machine-readable reason
    naming the pool and, in human-readable text, when it resets. `quota_by_pool=None` (every
    existing caller) or `ignore_quota=True` makes this a no-op, so existing behavior and tests are
    unchanged."""
    family = task.get("family", "general")
    exclude = set(task.get("exclude") or [])
    need_tools = task.get("need_tools", True)
    min_context = int(task.get("est_input_tokens", 20000)) * 1.2
    est_output = int(task.get("est_output_tokens", 2000))
    kept, reasons = [], []
    for c in candidates:
        if not c["enabled"] or c["id"] in exclude:
            continue
        if need_tools and c["supports_tools"] is False:
            reasons.append(f"skip {c['id']}: no tool support")
            continue
        if c["context"] is not None and c["context"] < min_context:
            reasons.append(f"skip {c['id']}: context {c['context']} < {int(min_context)}")
            continue
        max_out = _number((c.get("catalog_entry") or {}).get("max_output_tokens"))
        if max_out is not None and max_out < est_output:
            # section 3: hard filter, machine-readable reason (max_output_tokens=N < est_output_tokens=M).
            reasons.append(f"skip {c['id']}: max_output_tokens={int(max_out)} < est_output_tokens={est_output}")
            continue
        billing = c.get("billing") or {}
        if not ignore_quota and quota_by_pool and billing.get("mode") == "subscription":
            qstate = quota_by_pool.get(billing.get("pool"))
            if qstate and qstate["status"] in ("reserve", "exhausted"):
                reasons.append(f"skip {c['id']}: quota {qstate['pool']} {qstate['status']}; "
                               f"resets at {human_local_time(qstate['resets_at'])}")
                continue
        kept.append(c)
    return kept, reasons


def rank_candidates(candidates, task, stats, token_stats=None, quota_by_pool=None, ignore_quota=False):
    """Copy candidates with cost_usd, effective_cost, price_basis, token_basis and est_tokens,
    sorted cheapest first by effective_cost, unknown price last, then ledger rate desc. cost_usd
    uses learned token use (see estimate_tokens) when available, else the task's own
    est_input_tokens/est_output_tokens.

    Pricing: a builtin's billing.price_override, if set, is a flat $/M rate
    applied to both directions, ignoring tiered/cached-input catalog fields (an operator's own
    known rate is authoritative); otherwise price_tokens_full honors cache_read_input_token_cost
    (at the task's est_cached_input_share) and above-threshold tiered input/output rates when the
    matched catalog entry defines them. price_basis names which catalog fields were used either
    way. This is still cost_usd internally for every candidate regardless of billing mode -- only
    route()/brief_output() relabel it to shadow_cost for non-metered candidates (cost_output_key).

    Ranking sorts by `effective_cost` (see effective_cost()), not cost_usd directly --
    identical to cost_usd unless `quota_by_pool` names pressure for a subscription candidate's
    pool and `ignore_quota` is false, so `quota_by_pool=None` (every older caller) reproduces
    the old sort order exactly. cost_usd/shadow_cost, as reported in route()'s output, is always
    the unweighted real number; effective_cost only ever changes which candidate ranks cheapest."""
    family = task.get("family", "general")
    cached_share = _number(task.get("est_cached_input_share")) or 0.0
    ranked = []
    for c in candidates:
        total, basis, floor_applied, required_total = _estimate_tokens_detail(
            task, family, c["id"], token_stats or {})
        in_tok, out_tok = split_tokens(total, task)
        billing = c.get("billing") or {}
        if billing.get("price_override") is not None:
            cost = price_tokens(c["price"], in_tok, out_tok)
            price_basis = ["price_override"] if cost is not None else None
        else:
            cost, price_basis = price_tokens_full(c.get("catalog_entry"), c["price"], in_tok, out_tok, cached_share)
        rate = ledger_rate(stats, family, c["id"])[2]
        eff = effective_cost(cost, billing, quota_by_pool, ignore_quota)
        ranked.append({**c, "cost_usd": cost, "effective_cost": eff, "est_tokens": round(total),
                       "ledger_rate": rate, "token_basis": basis,
                       "token_floor": {"applied": floor_applied,
                                       "required_total": round(required_total) if required_total is not None else None},
                       "price_basis": price_basis})
    ranked.sort(key=lambda c: (c["effective_cost"] is None, c["effective_cost"] or 0.0,
                               -(c["ledger_rate"] if c["ledger_rate"] is not None else 0.5), c["id"]))
    return ranked


def price_bands(ranked):
    """{id: band}: quintile by rank among known-price candidates; one known -> "only"; no price -> "unknown"."""
    known = sorted(c["cost_usd"] for c in ranked if c["cost_usd"] is not None)
    bands = {}
    for c in ranked:
        if c["cost_usd"] is None:
            bands[c["id"]] = "unknown"
        elif len(known) == 1:
            bands[c["id"]] = "only"
        else:
            r = known.index(c["cost_usd"])  # ties share the lower rank
            bands[c["id"]] = PRICE_BANDS[int(r * (len(PRICE_BANDS) - 1) / (len(known) - 1) + 0.5)]
    return bands


def jev_slots(ranked):
    """Stable shared cN numbering within the single-request question budget.

    Pre-quota cost selects coverage; quota pressure only changes presentation order.
    Every usable judged eligible candidate is returned as advisory evidence.
    """
    def pre_quota_key(c):
        # .get(), not [] -- some callers (and tests) pass a raw, un-ranked candidate list (no
        # cost_usd/ledger_rate yet); missing is treated the same as rank_candidates' own "unknown"
        # convention (sorts last), not a crash.
        cost = c.get("cost_usd")
        rate = c.get("ledger_rate")
        return (cost is None, cost or 0.0, -(rate if rate is not None else 0.5), c["id"])
    chosen_ids = {c["id"] for c in sorted(ranked, key=pre_quota_key)[:JEV_MAX_CANDIDATES]}
    slots = [c for c in ranked if c["id"] in chosen_ids]
    return [(f"c{i}", c) for i, c in enumerate(slots)]


def context_band(ctx):
    if ctx is None:
        return "unknown"
    return "small" if ctx <= 64000 else "medium" if ctx <= 256000 else "large"


def evidence_text(stats, family, cid):
    accepted, n, _ = ledger_rate(stats, family, cid)
    return "no record" if n == 0 else f"accepted {accepted} of {n} similar tasks"


def extract_supports(entry):
    """{name: bool} for every catalog `supports_*` key present with a boolean value."""
    if not isinstance(entry, dict):
        return {}
    return {k: v for k, v in entry.items() if k.startswith("supports_") and isinstance(v, bool)}


def task_semantic_state(task, context_files=None):
    """Task fields as state for the Jev request: deliverable, acceptance, owned_paths, access,
    free-text context, and context_files (already-read [{path, text, truncated}])."""
    state = {}
    for k in ("deliverable", "acceptance", "owned_paths", "access", "context", "family", "complexity"):
        v = task.get(k)
        if isinstance(v, str) and v:
            state[k] = v
        elif isinstance(v, list) and v:
            state[k] = [str(x) for x in v]
    # Invalid optional numeric evidence stays unknown, never NaN/Infinity in JSON.
    difficulty = _number(task.get("difficulty"))
    state["difficulty"] = difficulty if difficulty is not None and 0 <= difficulty <= 4 else None
    cached_share = _number(task.get("est_cached_input_share"))
    cached_share = cached_share if cached_share is not None and 0 <= cached_share <= 1 else None
    state["cached_input_estimate"] = {
        "est_cached_input_share": cached_share,
        "status": "unknown" if cached_share is None else "estimate",
        "basis": "unknown" if cached_share is None else "caller-supplied pricing assumption",
        "cache_hit_verified": False,
        "timestamp_is_cache_evidence": False,
    }
    state.setdefault("family", "general")
    state["token_estimates"] = {k: task.get(k) for k in ("est_input_tokens", "est_output_tokens")}
    state["complexity"] = task.get("complexity", "unknown")
    state.setdefault("access", "read")
    if context_files:
        state["context_files"] = context_files
    return state


def documented_strength_profile(c):
    """Optional operator evidence, never an inferred benchmark or authorization."""
    raw = (c.get("billing") or {}).get("strength_profile")
    required = ("source", "date", "model_version", "configuration", "evidence")
    if not isinstance(raw, dict) or any(not isinstance(raw.get(k), str) or not raw[k].strip() for k in required):
        return {"status": "unknown", "reason": "no complete documented profile"}
    effort = (c.get("model_configuration") or {}).get("reasoning_effort", (c.get("billing") or {}).get("reasoning_effort"))
    if not effort or raw["configuration"] != effort:
        return {"status": "unknown", "reason": "reasoning configuration missing or mismatched"}
    try:
        observed = calendar.timegm(time.strptime(raw["date"], "%Y-%m-%d"))
    except ValueError:
        return {"status": "unknown", "reason": "profile date must be YYYY-MM-DD"}
    age_days = (time.time() - observed) / 86400
    if age_days < 0 or age_days > 90:
        return {"status": "unknown", "reason": "profile date is future or older than 90 days"}
    if raw["model_version"] != c.get("model_id"):
        return {"status": "unknown", "reason": "profile model version differs from worker"}
    return {"status": "operator-documented; not independently verified", "freshness": "within 90 days", "configuration_match": True, **{k: raw[k] for k in required}}


def candidate_semantic_state(c, family, bands, stats, quota_sources=None, quota_by_pool=None, catalog_info=None):
    """Everything Jev needs to judge one candidate's fit: bands and tool support (as before),
    plus exact price, context window, max output tokens, catalog supports_* flags, the agent's
    own description, enabled/probation state, and ledger evidence (per-family rate plus the
    5 most recent records for this candidate across every family)."""
    tools = c["supports_tools"]
    entry = c.get("catalog_entry") or {}
    state = {"model": c["catalog_id"] or c["model_id"] or c["id"],
             "price_band": bands[c["id"]], "context_band": context_band(c["context"]),
             "tools": "yes" if tools is True else "no" if tools is False else "unknown",
             "state": c.get("state", "enabled")}
    billing = c.get("billing") or {}
    pool = billing.get("pool")
    state["identity"] = {"worker_model": c.get("model_id"), "routed_model": c.get("model_selector"),
                         "matched_catalog_id": c.get("catalog_id"), "canonical_model_id": "unknown",
                         "match": c.get("catalog_match", "unknown"), "confidence": c.get("match_score")}
    state["reasoning_effort"] = (c.get("model_configuration") or {}).get("reasoning_effort", billing.get("reasoning_effort", "unknown"))
    state["deployment"] = {k: billing.get(k, "unknown") for k in ("runtime", "provider")}
    state["pricing"] = {"billing_mode": billing.get("mode", "metered"),
                        "billing_source": billing.get("billing_source", "operator" if billing else "default"),
                        "cost_kind": cost_output_key(c), "estimated_cost": c.get("cost_usd"),
                        "catalog_provenance": catalog_info or {"source": "unknown"},
                        "actual_billed_cost": None, "matched_provider_price": c.get("price"),
                        "price_basis": c.get("price_basis"),
                        "actual_worker_billing_verified": False,
                        "host_relationship": "unknown; catalog match verifies model identity, not deployment or invoice",
                        "strength_signal": False}
    state["token_estimates"] = {"total": c.get("est_tokens"), "basis": c.get("token_basis", "unknown"),
                                "explicit_floor": c.get("token_floor", {"applied": False, "required_total": None})}
    state["documented_strength_profile"] = documented_strength_profile(c)
    state["quota"] = {"source": (quota_sources or {}).get(pool, {"availability": "unknown", "freshness": "unknown", "live": False}),
                      "policy_state": (quota_by_pool or {}).get(pool)}
    state["strength_benchmarks"] = {"status": "unknown", "reason": "no integrated benchmark ingestion"}
    accepted, total, _ = ledger_rate(stats, family, c["id"])
    state["observed_family_profile"] = {"family": family, "accepted": accepted, "total": total,
                                        "provenance": "local outcome ledger", "uncertainty": "unobserved" if not total else "observational; selection bias"}
    price = c.get("price")
    if price:
        state["input_price_per_m"] = price["input_per_m"]
        state["output_price_per_m"] = price["output_per_m"]
    if c.get("context") is not None:
        state["context_window"] = c["context"]
    max_out = _number(entry.get("max_output_tokens"))
    if max_out is not None:
        state["max_output_tokens"] = max_out
    supports = extract_supports(entry)
    if supports:
        state["supports"] = supports
    if c.get("description"):
        state["description"] = c["description"]
    # Use real ledger evidence always; probation is a separate field
    state["evidence"] = evidence_text(stats, family, c["id"])
    if c.get("state") == "probation":
        state["probation"] = True
    accepted, n, _ = ledger_rate(stats, family, c["id"])
    if n:
        state["family_accepted_total"] = f"{accepted}/{n}"
    # K3: this-family evidence must be visible separately from other-family evidence, so Jev
    # (and the family-evidence guard) cannot mistake evidence from an unrelated family for proof
    # here (e.g. family "0/0" vs other "2/2").
    all_accepted, all_n, _ = ledger_rate_all_families(stats, c["id"])
    state["family"] = f"{accepted}/{n}"
    state["other"] = f"{all_accepted - accepted}/{all_n - n}"
    recent = ledger_recent(c["id"])
    if recent:
        state["recent_records"] = recent
    return state


def build_jev_request(task, slots, bands, stats, context_files=None, quota_sources=None, quota_by_pool=None, catalog_info=None):
    """Request body: task state plus per-candidate state, and fit_cN questions for each slot."""
    family = task.get("family", "general")
    task_state = task_semantic_state(task, context_files)
    cands = {cid: candidate_semantic_state(c, family, bands, stats, quota_sources, quota_by_pool, catalog_info) for cid, c in slots}
    questions = dict(QUESTIONS)
    for cid, _ in slots:
        questions[f"fit_{cid}"] = fit_question(cid)
    return {"model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
            "state": {"routing_objective": {
                "primary": "complete the full task deliverable and all acceptance criteria",
                "secondary": ["cost", "fresh quota headroom", "recent route health", "model/provider usage distribution when supplied and fresh"],
                "adequacy": "sufficient capability for full acceptance, not strongest capability or perfection",
                "balance": "diversity preference among tied or close credible full-completion choices; never random or unqualified selection",
                "evidence_policy": "use supplied fresh facts only; missing or stale is unknown, not zero; unknown quota is not healthy; no fabricated usage or aggregate headroom across pools",
                "selection_policy": "do not always choose highest fit, cheapest or most expensive; explain what a cheaper alternative lacks using concrete task capabilities or risk, not scores, prestige or history alone; consider repair/verification costs",
                "history_policy": "unobserved is not incapable; thin selected samples (1/1 versus 0/1) cannot alone justify always choosing strongest or known; avoid self-reinforcing preference",
                "exploration": "cheaper adequate or exploratory choices are defensible for bounded, verifiable, low-risk tasks without compromising full acceptance or forcing diversity",
                "fit_basis": "capability under task constraints and available tools; not price",
                "acceptance_owner": "lead",
            }, "task": task_state, "candidates": cands}, "questions": questions}


FIT_LEVELS = range(len(FIT_CRITERIA))  # S2: the only levels a fit_cN probability distribution may use


def parse_probabilities(raw, valid_levels=FIT_LEVELS):
    """Parse probabilities on the documented scale; invalid entries are omitted.

    Callers must validate distribution coverage and normalization before using it.
    """
    items = raw.items() if isinstance(raw, dict) else enumerate(raw) if isinstance(raw, list) else ()
    probs = {}
    for level, p in items:
        try:
            level = int(level)
        except (TypeError, ValueError):
            continue
        if valid_levels is not None and level not in valid_levels:
            continue
        num = _number(p)
        if num is None or not (0.0 <= num <= 1.0):
            continue
        probs[level] = float(num)
    return probs


def parse_jev_answers(payload, questions, threshold):
    """Extract usable answers; never raises.

    Task-level scores count only with numeric confidence >= threshold. Fit scores (fit_cN) are
    not confidence-gated: the expected score and distribution are retained as advisory evidence."""
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if isinstance(answers, list):
        answers = {a.get("id") or a.get("question"): a for a in answers if isinstance(a, dict)}
    if not isinstance(answers, dict):
        return {}, "malformed answers"
    out = {}
    for qid, q in questions.items():
        a = answers.get(qid)
        if not isinstance(a, dict):
            continue
        if q["type"] == "noul" and _number(a.get("noul")) is not None:
            out[qid] = a["noul"]
        elif q["type"] == "score" and qid.startswith("fit_"):
            probs = parse_probabilities(a.get("probabilities"), valid_levels=FIT_LEVELS)
            if _number(a.get("score")) is not None:
                out[qid] = a["score"]
            if probs:
                out[qid + "_probs"] = probs
        elif q["type"] == "score" and _number(a.get("score")) is not None:
            conf = _number(a.get("confidence"))
            if conf is not None and conf >= threshold:
                out[qid] = a["score"]
    return out, None


_KEY_STATE = {"use_op": False, "op": {}, "rejected": set()}
OP_TIMEOUT_MAX_S = 60


def _positive_timeout(name, default):
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value if value <= OP_TIMEOUT_MAX_S else None


def _typesafe_timeout():
    timeout = _positive_timeout("TYPESAFE_TIMEOUT", 4)
    if timeout is None:
        return None, f"TYPESAFE_TIMEOUT must be finite, positive, and at most {OP_TIMEOUT_MAX_S:g} seconds"
    return timeout, None


def _valid_key(value):
    return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _op_key():
    """Read the explicitly configured op:// reference; cache result by ref, never log op output."""
    ref = os.environ.get("TYPESAFE_API_KEY_OP_REF", "").strip()
    if not ref:
        return None, "TYPESAFE_API_KEY_OP_REF unset"
    if not ref.startswith("op://") or any(ord(c) < 32 or ord(c) == 127 for c in ref):
        return None, "TYPESAFE_API_KEY_OP_REF invalid (expected op:// reference)"
    if ref in _KEY_STATE["op"]:
        return _KEY_STATE["op"][ref]
    timeout = _positive_timeout("TYPESAFE_OP_TIMEOUT", 10)
    if timeout is None:
        return None, f"TYPESAFE_OP_TIMEOUT must be finite, positive, and at most {OP_TIMEOUT_MAX_S:g} seconds"
    try:
        proc = subprocess.run(["op", "read", "--", ref], capture_output=True, text=True,
                              timeout=timeout, shell=False, check=False)
        value = proc.stdout or ""
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        value = value.strip(" ")
        if proc.returncode:
            result = (None, f"op read failed (exit {proc.returncode})")
        elif not value:
            result = (None, "op read returned empty output")
        elif not _valid_key(value):
            result = (None, "op read returned invalid credential format")
        else:
            result = (value, None)
    except FileNotFoundError:
        result = (None, "op not installed")
    except subprocess.TimeoutExpired:
        result = (None, f"op read timed out after {timeout:g}s")
    except Exception as exc:
        result = (None, f"op read failed ({type(exc).__name__})")
    _KEY_STATE["op"][ref] = result
    return result


def typesafe_key():
    """Return (key, source, safe_error), preferring env and using only an explicit op:// ref."""
    env_error = None
    if _KEY_STATE["use_op"]:
        key, err = _op_key()
        if key:
            if hashlib.sha256(key.encode()).hexdigest() not in _KEY_STATE["rejected"]:
                return key, "op", None
            env_error = "TYPESAFE_API_KEY_OP_REF credential previously rejected (HTTP 401)"
    env = os.environ.get("TYPESAFE_API_KEY")
    if env:
        if _valid_key(env):
            if hashlib.sha256(env.encode()).hexdigest() not in _KEY_STATE["rejected"]:
                return env, "env", None
            env_error = "TYPESAFE_API_KEY from env rejected previously (HTTP 401)"
        else:
            env_error = "TYPESAFE_API_KEY has invalid credential format"
    key, err = _op_key()
    if key:
        if hashlib.sha256(key.encode()).hexdigest() not in _KEY_STATE["rejected"]:
            return key, "op", None
        op_error = "TYPESAFE_API_KEY_OP_REF credential previously rejected (HTTP 401)"
    else:
        op_error = None if err == "TYPESAFE_API_KEY_OP_REF unset" else f"op fallback failed: {err}"
    if err == "TYPESAFE_API_KEY_OP_REF unset":
        return None, None, env_error or "TYPESAFE_API_KEY unset"
    if env_error:
        return None, None, f"{env_error}; {op_error}"
    return None, None, f"TYPESAFE_API_KEY unset; {op_error}"


def jev_request(body, timeout):
    """Send with shared env/op resolution; retry exactly once after an env-key 401."""
    key, source, key_err = typesafe_key()
    if not key:
        return None, key_err
    payload, err = jev_post(body, key, timeout)
    if err != "typesafe http 401":
        return payload, err
    _KEY_STATE["rejected"].add(hashlib.sha256(key.encode()).hexdigest())
    if source == "op":
        return None, "typesafe http 401: key from op rejected"
    base = "typesafe http 401: TYPESAFE_API_KEY from env rejected"
    op_key, reason = _op_key()
    if not op_key:
        if reason == "TYPESAFE_API_KEY_OP_REF unset":
            return None, f"{base}; op fallback not configured"
        return None, f"{base}; op fallback failed: {reason}"
    if hashlib.sha256(op_key.encode()).hexdigest() in _KEY_STATE["rejected"]:
        return None, f"{base}; op fallback credential previously rejected (HTTP 401)"
    payload, err = jev_post(body, op_key, timeout)
    if err is None:
        _KEY_STATE["use_op"] = True
        return payload, None
    if err == "typesafe http 401":
        _KEY_STATE["rejected"].add(hashlib.sha256(op_key.encode()).hexdigest())
        return None, f"{base}; op key also rejected (http 401)"
    return None, f"{base}; op fallback retry failed: {err}"


def jev(body):
    """POST one request. Returns (semantic dict | None, error string | None)."""
    try:
        threshold = float(os.environ.get("TYPESAFE_CONFIDENCE", "0.4"))
    except (TypeError, ValueError):
        return None, "TYPESAFE_TIMEOUT/TYPESAFE_CONFIDENCE not numeric"
    timeout, timeout_err = _typesafe_timeout()
    if timeout_err:
        return None, timeout_err
    t0 = time.time()
    payload, err = jev_request(body, timeout)
    if err:
        return None, err
    out, err = parse_jev_answers(payload, body["questions"], threshold)
    if err:
        return None, f"typesafe {err}"
    # Incomplete/malformed fit evidence is an outage, not a valid rejection.
    # A normalized low-fit distribution remains valid and must never invoke heuristics.
    for qid in body["questions"]:
        if qid.startswith("fit_"):
            probs = out.get(qid + "_probs", {})
            if not probs or not math.isclose(sum(probs.values()), 1.0, abs_tol=0.01):
                return None, "typesafe malformed fit distribution"
    out["jev_model"] = payload.get("model", "unknown")
    usage = payload.get("usage") if isinstance(payload, dict) else None
    in_tok = _number(usage.get("input_tokens")) if isinstance(usage, dict) else None
    out_tok = _number(usage.get("output_tokens")) if isinstance(usage, dict) else None
    if in_tok is not None or out_tok is not None:
        out["jev_usage"] = {"input_tokens": in_tok or 0, "output_tokens": out_tok or 0}
        try:
            price_per_m = float(os.environ.get("TYPESAFE_INPUT_PRICE_PER_M", str(TYPESAFE_INPUT_PRICE_PER_M)))
        except ValueError:
            price_per_m = TYPESAFE_INPUT_PRICE_PER_M
        out["jev_cost_usd"] = round((in_tok or 0) * price_per_m / 1e6, 8)
    out["ms"] = int((time.time() - t0) * 1000)
    return out, None


def jev_post(body, key, timeout):
    """POST one TypeSafe request. Returns (payload | None, error string | None); never raises."""
    req = urllib.request.Request(JEV_ENDPOINT, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        return None, f"typesafe http {e.code}"
    except Exception as e:  # network, timeout, malformed JSON
        return None, f"typesafe unavailable: {type(e).__name__}"


def fallback_pick(ranked, task, stats):
    """Metadata-safe compatibility recommendation; suitability belongs to the lead."""
    return min(ranked, key=lambda c: (c.get("effective_cost", c.get("cost_usd")) is None,
                                    c.get("effective_cost", c.get("cost_usd")) or 0, c["id"])), "fallback: cheapest metadata-safe candidate; lead acceptance required"


def family_profile(stats, family, cid):
    accepted, total, rate = ledger_rate(stats, family, cid)
    return {"accepted": accepted, "total": total, "acceptance_rate": rate,
            "provenance": "local outcome ledger", "uncertainty": "observational; selection bias" if total else "unobserved"}


def advisory_fit(semantic, slot):
    """Return validated score/distribution evidence, never a qualification verdict."""
    semantic = semantic or {}
    raw = semantic.get(f"fit_{slot}_probs")
    probs = parse_probabilities(raw, valid_levels=FIT_LEVELS) if raw is not None else None
    if raw is not None and (not isinstance(raw, (dict, list)) or not probs
                            or len(probs) != len(raw)
                            or not math.isclose(sum(probs.values()), 1.0, abs_tol=0.01)):
        return None, None, None
    score = _number(semantic.get(f"fit_{slot}"))
    if score is not None and (not math.isfinite(score) or not 0 <= score <= 4):
        return None, None, None
    if score is not None:
        return score, probs, "jev_raw_score"
    if probs:
        return sum(level * probability for level, probability in probs.items()), probs, "probability_expectation"
    return None, None, None


def model_information_tier(candidate):
    """Confidence in model metadata, not evidence of task competence.

    Tier 1 requires a Jev-confirmed catalog identity and known price, context, and
    tool support. Tier 0 requires a named lead override before dispatch.
    """
    price = candidate.get("price")
    usable_price = (isinstance(price, dict)
                    and _number(price.get("input_per_m")) is not None
                    and _number(price.get("output_per_m")) is not None
                    and _number(price.get("input_per_m")) >= 0
                    and _number(price.get("output_per_m")) >= 0)
    context = _number(candidate.get("context"))
    return int(candidate.get("catalog_match") == "jev"
               and usable_price
               and context is not None and context > 0
               and isinstance(candidate.get("supports_tools"), bool))


def metadata_tier_pool(candidates):
    """Prefer complete Jev-confirmed metadata within the supplied eligible pool."""
    if not candidates:
        return [], 0
    tier = max(model_information_tier(c) for c in candidates)
    return [c for c in candidates if model_information_tier(c) == tier], tier


def validate_model_information_override(value):
    """Validate the explicit, candidate-scoped exception to metadata preference."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"candidate_id", "reason"}:
        raise ValueError("model_information_override must contain exactly candidate_id and reason")
    candidate_id, reason = value.get("candidate_id"), value.get("reason")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("model_information_override.candidate_id must be a nonblank candidate id")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("model_information_override.reason must be a nonblank justification")
    return {"candidate_id": candidate_id.strip(), "reason": reason.strip()}


def require_incomplete_override_target(override, candidates):
    """Return the named candidate, refusing unknown or complete-metadata targets."""
    if override is None:
        return None
    candidate = next((c for c in candidates if c["id"] == override["candidate_id"]), None)
    if candidate is None:
        raise ValueError(f"model-information override candidate {override['candidate_id']!r} is not eligible")
    if model_information_tier(candidate):
        raise ValueError(f"model-information override candidate {override['candidate_id']!r} has complete metadata; no override is needed")
    return candidate


def decide(task, ranked, semantic, stats, bands, min_mass=None, fit_floor=None,
           model_information_override=None):
    """Recommend by metadata and cost only. Legacy threshold arguments are ignored.

    Semantic evidence never accepts/rejects a worker or blocks a recommendation.
    The lead must judge suitability and the caller owns availability and execution.
    """
    override = validate_model_information_override(model_information_override)
    override_candidate = require_incomplete_override_target(override, ranked)
    preferred, tier = metadata_tier_pool(ranked)
    if not override_candidate and not tier:
        return {"decision": "direct", "reasons": ["no complete Jev-confirmed model metadata; explicit model_information_override required"]}
    if override_candidate:
        pick, why = override_candidate, "lead model-information override"
    else:
        pick, why = fallback_pick(preferred, task, stats)
    judged = []
    for slot, c in jev_slots(ranked):
        score, probs, basis = advisory_fit(semantic, slot)
        if score is not None:
            judged.append({"id": c["id"], "fit": score, "fit_probabilities": probs})
    result = {"decision": "delegate", "pick": pick, "stop_before": None,
              "fallbacks": [c["id"] for c in ranked if c["id"] != pick["id"] and model_information_tier(c)],
              "reasons": [why, "semantic suitability and historical outcomes are advisory; lead must accept or reject"],
              "review_required": True, "judged": judged}
    if semantic is not None and not judged:
        result["reasons"].append("no usable Jev fit evidence; explicit metadata/cost fallback without invented scores")
    if override:
        result["model_information_override"] = override
    return result


def recommendation_records(task, ranked, semantic, decision, stats, bands, min_mass=None, fit_floor=None,
                           quota_by_pool=None):
    """Authoritative ordering for ONE task; compatibility pick never pins the list."""
    if decision["decision"] != "delegate":
        return []
    override = decision.get("model_information_override")
    override_id = override["candidate_id"] if override else None
    rows = []
    slots = jev_slots(ranked) if semantic is not None else [(None, c) for c in ranked]
    for slot, c in slots:
        if not model_information_tier(c) and c["id"] != override_id:
            continue
        score, probs, score_basis = advisory_fit(semantic, slot)
        review = ["lead_acceptance_required"]
        if semantic is not None and score is None:
            continue  # Unjudged or malformed evidence is not a scored recommendation.
        if c["id"] == override_id:
            review.append("model_information_override")
        if c.get("state") == "probation":
            review.append("probation")
        billing = c.get("billing") or {}
        configuration = dict(c.get("model_configuration") or {})
        if "reasoning_effort" not in configuration and billing.get("reasoning_effort") is not None:
            configuration["reasoning_effort"] = billing["reasoning_effort"]
        row = {"id": c["id"], "model_selector": c.get("model_selector", c["id"]),
               "model_id": c.get("model_id"), "model_configuration": configuration,
               "catalog_id": c.get("catalog_id"), "catalog_match": c.get("catalog_match"),
               "price": c.get("price"), "price_basis": c.get("price_basis"),
               cost_output_key(c): c.get("cost_usd"), "effective_cost": c.get("effective_cost"),
               "price_band": bands[c["id"]], "token_basis": c.get("token_basis"),
               "est_tokens": c.get("est_tokens"), "token_floor": c.get("token_floor"),
               "billing_mode": billing.get("mode", "metered"),
               "ranking_basis": "jev_raw_fit_score" if semantic is not None else "heuristic",
               "fit_score": score, "fit_score_basis": score_basis,
               "fit_probabilities": probs,
               "semantic_evidence": {k: semantic.get(k) if semantic else None for k in ("independent", "difficulty", "verifiability", "irreversible")},
               "family_history": family_profile(stats, task.get("family", "general"), c["id"]),
               "review_required": bool(review), "review_reasons": review,
               "ledger_rate": c.get("ledger_rate")}
        if c["id"] == override_id:
            row["model_information_override"] = override
        pool = billing.get("pool")
        if pool and quota_by_pool and pool in quota_by_pool:
            row["quota"] = quota_by_pool[pool]
        rows.append(row)
    def cost_key(row):
        cost = row["effective_cost"]
        return (cost is None, cost if cost is not None else 0, row["id"])
    if semantic is not None:
        rows.sort(key=lambda r: (r["fit_score"] is None, -(r["fit_score"] or 0), *cost_key(r)))
    else:
        for row in rows:
            row["heuristic_basis"] = "metadata_safe_cost"
        rows.sort(key=cost_key)
    return rows


def route(task, use_jev=True, refresh=False, lead=None, harness=None, spawnable=None,
         quota_specs=None, ignore_quota=False, model_information_override=None):
    """Route a task to a candidate or direct. Returns one JSON-serializable dict; prints nothing.

    When a lead model is configured (`lead`, else FAST_DELEGATE_LEAD, else harness.json
    default_lead), only candidates strictly cheaper than the lead's blended price are
    considered; a missing, unknown or unpriced lead is a hard error, not a guess.

    Section N: `harness` is None for the legacy, harness-unaware behavior (no `harness` key in
    the result, no spawnable filtering -- unchanged for any caller that doesn't opt in), else
    `HARNESS_NATIVE` or "codex" (already resolved by the caller -- CLI callers resolve "auto"
    through `detect_harness` before calling `route`). `spawnable` is the caller's own spawn
    tool's model enum; see `discover` for exactly how it restricts candidates.

    `quota_specs` (route --quota, repeatable "pool=5h:NN,7d:NN" strings; the
    FAST_DELEGATE_QUOTA env var is read the same way regardless) declares quota for any pool with
    no machine-readable signal. `ignore_quota` (route --ignore-quota) bypasses every quota filter
    and pressure re-weight below, but is always recorded (`result["ignore_quota"]`) so it's never
    a silent override. See the "quota-aware selection" section above filter_candidates
    for how pool assignment, pressure, and the reserve/exhausted filters work."""
    if not isinstance(task, dict) or not task.get("deliverable"):
        raise SystemExit("task.deliverable is required")
    try:
        task_override = validate_model_information_override(task.get("model_information_override"))
        cli_override = validate_model_information_override(model_information_override)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if task_override and cli_override:
        raise SystemExit("provide model_information_override in the task or CLI flags, not both")
    model_override = cli_override or task_override
    cwd = task.get("cwd", os.getcwd())
    context_files, cf_warnings = read_context_files(task.get("context_files"), cwd=cwd)
    if harness:
        candidates, warnings = discover(refresh=refresh, use_jev=use_jev, harness_name=harness, spawnable=spawnable)
    else:
        candidates, warnings = discover(refresh=refresh, use_jev=use_jev)
    discovered_ids = {c["id"] for c in candidates}
    if model_override and model_override["candidate_id"] not in discovered_ids:
        raise SystemExit(f"model-information override candidate {model_override['candidate_id']!r} is unknown or unavailable in this harness")
    warnings = warnings + cf_warnings
    # Section 1/5: provenance and staleness for whatever catalog snapshot discover() just used.
    # refresh=False here deliberately -- discover() above already forced a refresh when the caller
    # asked for one, so this reads the (now current) cache instead of a second network round trip.
    _catalog, catalog_meta, catalog_meta_warnings = fetch_litellm_catalog_meta(refresh=False)
    warnings = warnings + catalog_meta_warnings
    catalog_info = catalog_provenance(catalog_meta)
    stale_prices = bool(catalog_info and catalog_info["stale"])
    if stale_prices:
        warnings.append(f"catalog is {catalog_info['age_h']}h old (>= hard-stale threshold "
                        f"{CATALOG_HARD_STALE_S // 3600}h); prices may be outdated")

    quota_sources = {}

    def attach_route_meta(result):
        if quota_sources:
            result["quota_sources"] = quota_sources
        if catalog_info:
            result["catalog"] = catalog_info
            result["stale_prices"] = stale_prices
        if harness:
            result["harness"] = harness
        return result

    # T5: the per-candidate "jev catalog match" lines used to dominate `reasons` on every route
    # (one line per matched candidate); collapse to one summary line and move the detail to
    # `catalog_matches` in the full result, where it's still available but out of the way.
    catalog_matches = [{"id": c["id"], "model_id": c["model_id"], "catalog_id": c["catalog_id"],
                        "score": c["match_score"], "entries": c["match_entries"]}
                       for c in candidates if c["catalog_match"] == "jev"]
    reasons = [f"catalog: {len(catalog_matches)} matched by jev"] if catalog_matches else []

    harness_cfg = load_harness()
    lead_id = lead or os.environ.get("FAST_DELEGATE_LEAD") or harness_cfg.get("default_lead")
    if not lead_id:
        override_path = harness_override_path()
        if override_path:
            raise SystemExit(f"no lead configured: pass --lead, set FAST_DELEGATE_LEAD, or add default_lead to {override_path} or harness.json")
        raise SystemExit("no lead configured: pass --lead, set FAST_DELEGATE_LEAD, or add default_lead to harness.json (section I: a lead only delegates to strictly cheaper models)")
    lead_info = None
    ratio = in_out_ratio(task)
    # The lead's own price must resolve regardless of spawnable filtering (section N): the lead
    # isn't itself a routed-to candidate, so a harness-aware `candidates` list that dropped it (or
    # never discovered it, for "codex") must not block resolving what it costs. Use the
    # unrestricted discovery for this lookup only; `candidates` (used for routing) stays filtered.
    lead_pool = candidates
    if harness:
        lead_pool, _lead_pool_warnings = discover(refresh=refresh, use_jev=False)
    lead_model_id, lead_catalog_id, lead_price, lead_warnings = resolve_lead_price(
        lead_id, lead_pool, refresh=refresh)
    warnings.extend(lead_warnings)
    if not lead_price:
        raise SystemExit(f"lead {lead_id!r} has unknown price; cannot enforce the cheaper-than-lead rule")
    lead_info = {"id": lead_id, "catalog_id": lead_catalog_id,
                "blended_price_per_m": round(blended_price(lead_price, *ratio), 6)}
    candidates_before_proxy_guard = candidates
    candidates, proxy_reasons = drop_inactive_proxy_candidates(candidates)
    reasons += proxy_reasons
    proxy_dropped = {c["id"] for c in candidates_before_proxy_guard} - {c["id"] for c in candidates}
    if model_override and model_override["candidate_id"] in proxy_dropped:
        raise SystemExit(f"model-information override candidate {model_override['candidate_id']!r} is proxy-routed and the proxy is not active in this session")
    candidates, drop_reasons = filter_cheaper_than_lead(candidates, lead_price, ratio)
    reasons += drop_reasons
    if model_override and model_override["candidate_id"] not in {c["id"] for c in candidates}:
        raise SystemExit(f"model-information override candidate {model_override['candidate_id']!r} is not cheaper than lead {lead_id!r}")
    if not candidates:
        none_reason = (f"no candidate left after dropping proxy-routed candidates (proxy not active) and "
                       f"those not cheaper than lead {lead_id}" if proxy_reasons
                       else f"no candidate cheaper than lead {lead_id}")
        result = {"decision": "direct", "semantic": None, "warnings": warnings, "lead": lead_info,
                  "reasons": reasons + [none_reason]}
        return attach_route_meta(result)

    # Gather quota state for every pool any surviving candidate names, before the hard
    # filters run -- pool assignment (resolve_pool, in discover()) is data, so this never
    # references a model/vendor name. Review fix A: this must cover every candidate's pool, not
    # only ones already billing.mode "subscription" -- a shipped builtin has no billing config at
    # all (defaults to metered) and would otherwise never have its pool's snapshot fetched, so
    # quota could never be inferred for it below no matter how tight the pool actually is.
    quota_thresholds = default_quota_thresholds(harness_cfg)
    quota_pools_needed = sorted({(c.get("billing") or {}).get("pool") for c in candidates
                                 if (c.get("billing") or {}).get("pool")})
    quota_snapshots, quota_warnings = gather_quota_snapshots(quota_pools_needed, quota_specs)
    warnings += quota_warnings
    now = time.time()
    quota_sources = {p: quota_evidence(p, snap, quota_thresholds, now)
                     for p, snap in quota_snapshots.items()}
    quota_by_pool = {p: s for p, s in
                     ((p, pool_status(snap, quota_thresholds, now)) for p, snap in quota_snapshots.items())
                     if s is not None}
    if not ignore_quota:
        # Review fix A (reproduced: shipped builtins default to billing metered, so quota did
        # nothing out of the box): a candidate with no *explicit* billing mode -- normalize_billing
        # only ever sets billing_source "default" (never present at all for an explicit mode) --
        # whose pool has a real quota snapshot is treated as subscription for this route. An
        # explicit mode (including metered) always wins; this mutates each candidate's own billing
        # dict in place so filter_candidates, rank_candidates, cost_output_key and the output below
        # all see the inferred mode consistently.
        inferred_by_pool = {}
        for c in candidates:
            billing = c.get("billing") or {}
            pool = billing.get("pool")
            if billing.get("billing_source") != "default" or not pool or pool not in quota_by_pool:
                continue
            billing["mode"] = "subscription"
            billing["billing_source"] = "inferred-from-quota"
            inferred_by_pool.setdefault(pool, []).append(c["id"])
        for pool, ids in sorted(inferred_by_pool.items()):
            warnings.append(f"quota: {', '.join(sorted(ids))} have no explicit billing mode; treated "
                            f"as subscription for pool {pool} because it has a quota snapshot")
        warned_pools = set()
        for c in candidates:
            billing = c.get("billing") or {}
            if billing.get("mode") != "subscription":
                continue
            pool = billing.get("pool")
            key = pool or f"candidate:{c['id']}"
            if key in warned_pools:
                continue
            if not pool:
                warned_pools.add(key)
                warnings.append(f"quota unknown for {c['id']} (no pool assigned)")
            elif pool not in quota_by_pool:
                warned_pools.add(key)
                warnings.append(f"quota unknown for {pool}")
            elif quota_by_pool[pool]["status"] == "warn":
                warned_pools.add(key)
                w = quota_by_pool[pool]
                warnings.append(f"quota warning: {pool} usage is high (5h/7d {w['five_hour']}/{w['seven_day']})")
            if quota_by_pool.get(pool, {}).get("stale"):
                warnings.append(f"quota snapshot for {pool} is stale (observed_at too old)")

    stats = ledger_stats()
    # T6: the same non-quota filters, run once ignoring quota entirely, so the reserve/
    # exhausted path below can tell exactly which candidates quota alone removed (for the
    # `direct`-with-`wait_until` reason and `escalation_blocked`) without duplicating any of
    # filter_candidates' other filter logic.
    kept_ignoring_quota, _ = filter_candidates(candidates, task, stats)
    kept, filt_reasons = filter_candidates(candidates, task, stats, quota_by_pool, ignore_quota)
    reasons += filt_reasons
    if model_override and model_override["candidate_id"] not in {c["id"] for c in kept}:
        raise SystemExit(f"model-information override candidate {model_override['candidate_id']!r} does not pass enabled, exclude, tool, context, output, and quota eligibility filters")
    quota_blocked_ids = {c["id"] for c in kept_ignoring_quota} - {c["id"] for c in kept}
    if not kept:
        reason = "no enabled candidate passes the filters"
        result = {"decision": "direct", "semantic": None, "warnings": warnings, "reasons": None}
        if quota_blocked_ids and not ignore_quota:
            blocked_pools = {(c.get("billing") or {}).get("pool") for c in candidates
                             if c["id"] in quota_blocked_ids and (c.get("billing") or {}).get("pool") in quota_by_pool}
            states = [quota_by_pool[p] for p in blocked_pools]
            with_reset = [s for s in states if s.get("resets_at") is not None]
            soonest = min(with_reset, key=lambda s: s["resets_at"]) if with_reset else (states[0] if states else None)
            if soonest:
                reason = (f"quota: {soonest['pool']} {soonest['status']}; "
                         f"resets at {human_local_time(soonest['resets_at'])}")
                result["quota"] = soonest
                if soonest.get("resets_at") is not None:
                    result["wait_until"] = soonest["resets_at"]
        result["reasons"] = reasons + [reason]
        if ignore_quota:
            result["ignore_quota"] = True
        if lead_info:
            result["lead"] = lead_info
        return attach_route_meta(result)
    token_stats = ledger_tokens()
    ranked = rank_candidates(kept, task, stats, token_stats, quota_by_pool, ignore_quota)
    bands = price_bands(ranked)
    semantic = None
    if not use_jev:
        reasons.append("diagnostic: task Jev disabled explicitly; independent model matching unchanged")
    if use_jev:
        semantic, err = jev(build_jev_request(task, jev_slots(ranked), bands, stats, context_files,
                                             quota_sources, quota_by_pool, catalog_info))
        if err:
            reasons.append(f"jev unavailable ({err}); using fallback rule")
    try:
        d = decide(task, ranked, semantic, stats, bands, model_information_override=model_override)
    except ValueError as exc:
        raise SystemExit(str(exc))
    recommendations = recommendation_records(task, ranked, semantic, d, stats, bands,
                                             quota_by_pool=quota_by_pool)
    if semantic is not None and not any(advisory_fit(semantic, slot)[0] is not None for slot, c in jev_slots(ranked)):
        reasons.append("Jev returned no usable fit evidence; metadata/cost fallback without invented scores")
        semantic = None
        recommendations = recommendation_records(task, ranked, None, d, stats, bands, quota_by_pool=quota_by_pool)
    result = {"decision": d["decision"], "semantic": semantic, "warnings": warnings,
              "reasons": reasons + d["reasons"]}
    if lead_info:
        result["lead"] = lead_info
    result = attach_route_meta(result)
    if catalog_matches:
        result["catalog_matches"] = catalog_matches  # T5: detail behind the one-line summary
    if d.get("judged"):
        result["judged"] = d["judged"]  # Advisory evidence keyed by candidate id
    if d.get("model_information_override"):
        result["model_information_override"] = d["model_information_override"]
    if ignore_quota:
        result["ignore_quota"] = True  # always recorded, even when it changed nothing
    if d["decision"] == "delegate":
        pick = d["pick"]
        # Section 2: a subscription/local candidate's cost is presented as shadow_cost, never
        # cost_usd, so output (and README wording) never claims a dollar saving for plan users.
        cost_key = cost_output_key(pick)
        result["candidate"] = {"id": pick["id"], "model_selector": pick.get("model_selector", pick["id"]),
                               "catalog_id": pick["catalog_id"], "model_id": pick.get("model_id"), cost_key: pick["cost_usd"],
                               "price_band": bands[pick["id"]], "token_basis": pick["token_basis"],
                               "price_basis": pick.get("price_basis"), "est_tokens": pick.get("est_tokens"),
                               "token_floor": pick.get("token_floor")}
        effort = (pick.get("model_configuration") or {}).get("reasoning_effort", (pick.get("billing") or {}).get("reasoning_effort"))
        if effort is not None:
            result["candidate"]["model_configuration"] = {"reasoning_effort": effort}
        if cost_key == "shadow_cost":
            billing_mode = pick["billing"]["mode"]
            result["candidate"]["billing_mode"] = billing_mode
            billing_source = pick["billing"].get("billing_source")
            if billing_source:  # absent means an explicit mode was configured (review fix A)
                result["candidate"]["billing_source"] = billing_source
            result["warnings"] = result["warnings"] + [
                f"{pick['id']}: shadow_cost is a shadow pricing proxy from billing_mode="
                f"{billing_mode} (LiteLLM price), not a dollar figure"]
        result["recommendations"] = recommendations
        result["fallbacks"] = d["fallbacks"]
        if d.get("review_required"):
            result["review_required"] = True
        if d.get("margin"):
            result["margin"] = d["margin"]
        if pick.get("state") == "probation":
            result["warnings"] = result["warnings"] + ["probation candidate: verify strictly and record the outcome"]
        # Report the picked candidate's own pool quota state (when known), and force
        # review when every fallback is quota-blocked -- the lead has no genuine escalation path.
        pick_pool = (pick.get("billing") or {}).get("pool")
        if not ignore_quota and pick_pool in quota_by_pool:
            result["quota"] = quota_by_pool[pick_pool]
        if not ignore_quota and not d["fallbacks"] and quota_blocked_ids:
            # Review fix 2: a quota-blocked candidate is only a plausible escalation target -- and
            # so only grounds for "escalation_blocked: quota" -- when it costs more (by pre-quota
            # price, i.e. cost_usd, not quota-re-weighted effective_cost) than the pick actually
            # made. A blocked candidate that was never cheaper or stronger than the pick was never
            # going to be an escalation path anyway; attributing the block to quota there would be
            # misleading when no costlier blocked alternative exists.
            # The blocked candidate's own fit was never judged (quota removed it before ranking
            # reached the Jev gate), so the warning says exactly that.
            blocked_candidates = [c for c in candidates if c["id"] in quota_blocked_ids]
            blocked_ranked = rank_candidates(blocked_candidates, task, stats, token_stats)
            pick_cost = pick.get("cost_usd")
            costlier = sorted(b["id"] for b in blocked_ranked
                              if b["cost_usd"] is not None and pick_cost is not None and b["cost_usd"] > pick_cost)
            if costlier:
                result["escalation_blocked"] = "quota"
                result["review_required"] = True
                result["warnings"] = result["warnings"] + [
                    f"escalation_blocked: quota -- a costlier fallback candidate is blocked by quota "
                    f"reserve/exhaustion ({', '.join(costlier)}); its fit was not judged"]
        # Keep calibration feedback available to the caller without creating execution artifacts.
        result["route_id"] = write_route_record(task, d, semantic, result.get("quota"), ignore_quota,
                                               model_candidates=ranked)
    return result


def write_route_record(task, d, semantic, quota=None, ignore_quota=False, model_candidates=None):
    """Append one calibration-ledger record to $FAST_DELEGATE_STATE/routes.jsonl for a delegated
    decision and return its route_id (section K4).

    Also records the picked candidate's est_tokens and cost_usd/shadow_cost (whichever
    billing.mode calls for -- cost_output_key), plus `quota` (the picked candidate's pool state,
    when known) and `ignore_quota` (only when set). These are additive fields only -- an
    old-format record (from before cost tracking) still parses with all of them simply absent."""
    route_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    pick = d["pick"]
    rec = {"route_id": route_id, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "family": task.get("family", "general"), "picked": pick["id"],
           "candidates": d.get("judged", []),
           "difficulty": (semantic or {}).get("difficulty", task.get("difficulty")),
           "independence": (semantic or {}).get("independent") if semantic is not None else None,
           "est_tokens": pick.get("est_tokens"), cost_output_key(pick): pick.get("cost_usd")}
    # Preserve route-time identity for dynamic spawnables and aliases. A later record
    # must not depend on mutable agent files or require the spawnable list again.
    builtin_ids = {b["id"]: builtin_model_id(b) for b in load_harness().get("builtin", [])}
    rec["candidate_model_ids"] = {
        c["id"]: (c.get("catalog_id") if c.get("catalog_match") == "jev" and c.get("catalog_id")
                  else builtin_ids.get(c.get("model_id"), c.get("model_id")))
        for c in (model_candidates if model_candidates is not None else [pick])
        if c.get("model_id") or (c.get("catalog_match") == "jev" and c.get("catalog_id"))
    }
    if quota is not None:
        rec["quota"] = quota
    if ignore_quota:
        rec["ignore_quota"] = True
    if d.get("model_information_override"):
        rec["model_information_override"] = d["model_information_override"]
    ROUTES_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with ROUTES_LEDGER.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return route_id


def read_context_files(paths, cwd=None):
    """([{path, text, truncated}], [skipped_paths], warnings) reading each path, capped at CONTEXT_FILE_CAP chars
    per file and CONTEXT_TOTAL_CAP chars overall. A missing/unreadable file warns, never crashes.
    Skipped files (due to total cap) are tracked separately for the handoff."""
    out, skipped, warnings, total = [], [], [], 0
    for p in paths or []:
        fp = Path(p)
        if not fp.is_absolute() and cwd:
            fp = Path(cwd) / fp
        try:
            text = fp.read_text()
        except OSError as e:
            warnings.append(f"context_files: {p} unreadable ({type(e).__name__})")
            continue
        truncated = False
        if len(text) > CONTEXT_FILE_CAP:
            text, truncated = text[:CONTEXT_FILE_CAP], True
        remaining = CONTEXT_TOTAL_CAP - total
        if remaining <= 0:
            skipped.append(p)
            continue
        if len(text) > remaining:
            text, truncated = text[:remaining], True
        total += len(text)
        out.append({"path": p, "text": text, "truncated": truncated})
    if skipped:
        warnings.append(f"context_files: {len(skipped)} file(s) skipped; total cap reached")
    return out + [{"path": p, "text": "", "truncated": False, "not_included": "total cap reached"} for p in skipped], warnings


def brief_output(result):
    """Compact recommendation and evidence for the caller's harness-owned dispatch."""
    keys = ("decision", "harness", "stale_prices", "quota", "wait_until",
            "escalation_blocked", "ignore_quota", "model_information_override", "reasons",
            "warnings", "route_id", "candidate", "fallbacks", "recommendations", "judged",
            "review_required", "lead", "catalog_matches")
    brief = {key: result[key] for key in keys if key in result}
    candidate = result.get("candidate")
    if candidate:
        brief.update(candidate)
    return brief


def resolve_lead_price(lead_id, candidates, refresh=False):
    """(model_id, catalog_id, price|None) for the lead. `lead_id` may be a discovered candidate's
    id (reuse its already-resolved facts, which already went through the zero-price safeguard) or
    a raw model/catalog id (resolved the same way as any candidate). Review fix 3: a lead whose
    catalog price is a literal $0 on either side is treated as unpriced here too -- route()'s
    existing "lead has unknown price" hard error then fires, exactly as for any other unpriced
    lead; a $0 lead must never be read as "free to compare against"."""
    match = next((c for c in candidates if c["id"] == lead_id), None)
    if match:
        return match["model_id"], match["catalog_id"], match["price"], []
    harness = load_harness()
    providers = harness.get("catalog_provider_preference", [])
    catalog, warnings = fetch_litellm_catalog(refresh=refresh)
    key, how = match_catalog(lead_id, catalog, providers)
    matches, match_warnings, unjudged = jev_match_models(
        [lead_id], catalog, harness, hints={lead_id: key} if key and how in ("exact", "normalized") else {})
    warnings.extend(match_warnings)
    hit = matches.get(lead_id) or {}
    if hit.get("catalog_id") in catalog:
        key = hit["catalog_id"]
    elif lead_id not in unjudged:
        key = None
    price, _ctx, _tools = get_price_and_context(catalog.get(key)) if key else (None, None, None)
    if zero_price_warning(lead_id, price):
        price = None
    return lead_id, key, price, warnings


def filter_cheaper_than_lead(candidates, lead_price, ratio):
    """Keep only candidates whose blended price (task's input:output ratio) is strictly below
    the lead's. Unknown-price candidates are dropped, since they cannot be proven cheaper.
    Returns (kept, reasons). Section 2: the rule applies the same way to a subscription/local
    candidate's shadow price (its LiteLLM-derived shadow pricing proxy) -- reasons says so
    for any such candidate kept, since that comparison isn't a real dollar comparison."""
    lead_blended = blended_price(lead_price, *ratio)
    kept, reasons, shadow_kept = [], [], []
    for c in candidates:
        if not c["price"]:
            reasons.append(f"drop {c['id']}: unknown price, cannot prove cheaper than lead")
            continue
        b = blended_price(c["price"], *ratio)
        if b < lead_blended:
            kept.append(c)
            if (c.get("billing") or {}).get("mode", "metered") != "metered":
                shadow_kept.append(c["id"])
        else:
            reasons.append(f"drop {c['id']}: blended price ${b:.4f}/M not cheaper than lead's ${lead_blended:.4f}/M")
    if shadow_kept:
        reasons.append(f"cheaper-than-lead for {', '.join(shadow_kept)} uses shadow_cost "
                       "(subscription/local billing signal, not a real dollar comparison)")
    return kept, reasons


def find_route_record(route_id):
    """The routes.jsonl record with this `route_id`, or None (section K4/K5)."""
    if not ROUTES_LEDGER.is_file():
        return None
    for line in ROUTES_LEDGER.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("route_id") == route_id:
            return rec
    return None


MASS_BUCKETS = [("<0.25", 0.0, 0.25), ("0.25-0.5", 0.25, 0.5), ("0.5-0.75", 0.5, 0.75), (">=0.75", 0.75, 1.0001)]


def calibration_stats():
    """(bucket_rows, family_rows, (override_accepted, override_total)). Bucket and family rows
    cover legacy outcomes with a `mass` field; these historical buckets are descriptive.
    Overrides are counted separately across every outcome that carries one,
    whether or not the dispatched candidate happened to have a mass."""
    all_recs = []
    if LEDGER.is_file():
        for line in LEDGER.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("outcome") in ("accepted", "rejected"):
                all_recs.append(rec)
    mass_recs = [r for r in all_recs if _number(r.get("mass")) is not None]
    bucket_rows = []
    for label, lo, hi in MASS_BUCKETS:
        bucket = [r for r in mass_recs if lo <= r["mass"] < hi]
        bucket_rows.append((label, sum(1 for r in bucket if r["outcome"] == "accepted"), len(bucket)))
    family_rows = []
    for fam in sorted({r.get("family") for r in mass_recs if r.get("family")}):
        fam_recs = [r for r in mass_recs if r.get("family") == fam]
        family_rows.append((fam, sum(1 for r in fam_recs if r["outcome"] == "accepted"), len(fam_recs)))
    overrides = [r for r in all_recs if r.get("override")]
    override_row = (sum(1 for r in overrides if r["outcome"] == "accepted"), len(overrides))
    return bucket_rows, family_rows, override_row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    disc = sub.add_parser("discover")
    disc.add_argument("--refresh", action="store_true")
    disc.add_argument("--json", action="store_true")
    disc.add_argument("--jev", action="store_true",
                      help="compatibility alias; Jev model matching is automatic when available")
    disc.add_argument("--harness", choices=["claude", "codex", "auto"], default="auto",
                      help="active CLI harness (section N); auto-detects from CLAUDECODE/CODEX_THREAD_ID/CODEX_SESSION_ID")
    disc.add_argument("--spawnable", help="comma-separated candidate model selectors; "
                                          "restricts candidates to these before any Jev request")
    r = sub.add_parser("route")
    r.add_argument("--task", help="path; default stdin")
    mode = r.add_mutually_exclusive_group()
    mode.add_argument("--jev", dest="jev", action="store_true", default=True,
                      help="compatibility alias: Jev task recommendations are enabled by default")
    mode.add_argument("--no-task-jev", dest="jev", action="store_false",
                      help="diagnostic opt-out from task inference; independent catalog matching may still network")
    r.add_argument("--refresh", action="store_true")
    r.add_argument("--brief", action="store_true", help="print a compact model recommendation and evidence")
    r.add_argument("--lead", help="candidate or catalog id the lead is running as; only strictly cheaper candidates are routed to")
    r.add_argument("--model-information-override-candidate", help="candidate id for a scoped incomplete-metadata override")
    r.add_argument("--model-information-override-reason", help="lead's justification for that named candidate")
    r.add_argument("--harness", choices=["claude", "codex", "auto"], default="auto",
                   help="active CLI harness (section N); auto-detects from CLAUDECODE/CODEX_THREAD_ID/CODEX_SESSION_ID")
    r.add_argument("--spawnable", help="comma-separated candidate model selectors; "
                                       "restricts candidates to these before any Jev request")
    r.add_argument("--quota", action="append", help="pool=5h:NN,7d:NN manual quota input for a pool with no "
                                                     "machine-readable signal; repeatable. The "
                                                     "FAST_DELEGATE_QUOTA env var takes the same syntax, "
                                                     "';'-separated for multiple pools")
    r.add_argument("--ignore-quota", action="store_true",
                   help="bypass quota reserve/exhausted filters and pressure re-weighting; "
                        "always recorded in the result and routes.jsonl")
    rec = sub.add_parser("record")
    rec.add_argument("--candidate", required=True)
    rec.add_argument("--family", required=True)
    rec.add_argument("--outcome", choices=["accepted", "rejected"], required=True)
    rec.add_argument("--note", default="")
    rec.add_argument("--tokens", type=int, help="total tokens the worker used, for learning cost estimates")
    rec.add_argument("--force", action="store_true",
                     help="skip validation against discovered ids and the served-model check")
    rec.add_argument("--served-model", help="model the worker actually ran on (transcript message.model); "
                                            "refused unless it matches the candidate's model (or --force)")
    rec.add_argument("--transcript", help="subagent JSONL transcript; its assistant message.model values "
                                          "are checked like --served-model")
    rec.add_argument("--route", help="route_id from route's output; links this outcome to that routing "
                                     "recommendation and copies advisory evidence (legacy mass supported)")
    rec.add_argument("--override", help="why the lead dispatched a different candidate than --route picked "
                                        "(section K5); stores the note and both ids")
    st = sub.add_parser("stats")
    st.add_argument("--calibration", action="store_true",
                    help="acceptance rate by mass bucket and family, from records linked with --route")
    a = ap.parse_args()

    if a.cmd == "discover":
        harness_name = detect_harness(a.harness)
        spawnable = a.spawnable.split(",") if a.spawnable else None
        candidates, warnings = discover(refresh=a.refresh, use_jev=a.jev, harness_name=harness_name, spawnable=spawnable)
        print_discovery(candidates, warnings, as_json=a.json)
    elif a.cmd == "route":
        harness_name = detect_harness(a.harness)
        spawnable = a.spawnable.split(",") if a.spawnable else None
        raw = Path(a.task).read_text() if a.task else sys.stdin.read()
        try:
            task = json.loads(raw)
        except json.JSONDecodeError as e:
            source = a.task if a.task else "<stdin>"
            raise SystemExit(f"Task JSON malformed ({source}): {e.msg} at line {e.lineno} column {e.colno}") from None
        if not isinstance(task, dict):
            source = a.task if a.task else "<stdin>"
            raise SystemExit(f"Task JSON must be an object, not {type(task).__name__} ({source})")
        cli_override = None
        if a.model_information_override_candidate is not None or a.model_information_override_reason is not None:
            cli_override = {"candidate_id": a.model_information_override_candidate,
                            "reason": a.model_information_override_reason}
        result = route(task, use_jev=a.jev, refresh=a.refresh, lead=a.lead,
                       harness=harness_name, spawnable=spawnable,
                       quota_specs=a.quota, ignore_quota=a.ignore_quota,
                       model_information_override=cli_override)
        print(json.dumps(brief_output(result) if a.brief else result, indent=1))
    elif a.cmd == "record":
        route_rec = None
        if not a.force:
            if a.route:
                # Fix 1a: when --route is given, validate against route record's judged candidates
                route_rec = find_route_record(a.route)
                if route_rec is None:
                    raise SystemExit(f"unknown --route {a.route!r}: no matching record in {ROUTES_LEDGER}")
                # Valid candidate ids: the picked candidate and all judged candidates from the route
                judged_ids = {c.get("id") for c in route_rec.get("candidates", []) if c.get("id")}
                valid_ids = judged_ids | {route_rec.get("picked")}
                if a.candidate not in valid_ids:
                    raise SystemExit(f"--candidate {a.candidate!r} not in route {a.route!r}'s picked id or "
                                   f"judged candidates {sorted(valid_ids)}")
            else:
                # Fix 1b: without --route, keep today's check but include codex spawnable names from harness override
                candidates, _ = discover()
                ids = {c["id"] for c in candidates}
                # Also include codex spawnable names from harness override if configured
                harness_cfg = load_harness()
                codex_models = (harness_cfg.get("harnesses") or {}).get("codex", {}).get("models", [])
                if codex_models:
                    ids = ids | set(codex_models)
                if a.candidate not in ids:
                    raise SystemExit(f"unknown candidate {a.candidate!r}; one of {sorted(ids)} (or pass --force)")
        served, served_mismatch, served_drift = [], False, []
        if a.served_model is not None:
            if not a.served_model.strip():
                raise SystemExit("--served-model is empty")
            served.append(a.served_model.strip())
        if a.transcript is not None:
            from_transcript = transcript_models(a.transcript)
            if not from_transcript and not a.force:
                raise SystemExit(f"--transcript {a.transcript!r} has no assistant message.model values; "
                                 f"cannot verify the served model (or pass --force)")
            served += [m for m in from_transcript if m not in served]
        if served:
            if a.route and route_rec is None:
                route_rec = find_route_record(a.route)
            expected = (route_rec or {}).get("candidate_model_ids", {}).get(a.candidate)
            if not expected:
                expected = declared_model_ids().get(a.candidate)
            if expected is None:
                if not a.force:
                    raise SystemExit(f"cannot verify served model {served}: no declared model for candidate "
                                     f"{a.candidate!r} (or pass --force)")
            else:
                bad = served_model_mismatches(expected, served)
                served_mismatch = bool(bad)
                served_drift = served_model_drifts(expected, served)
                if bad and not a.force:
                    raise SystemExit(f"served model mismatch: candidate {a.candidate!r} expects model "
                                     f"{expected!r} but the worker ran on {bad}; the spawn did not reach "
                                     f"that model (proxy inactive, so the placeholder model ran?). Nothing "
                                     f"recorded; fix the dispatch, or pass --force to record anyway")
        rec_obj = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "candidate": a.candidate,
                   "family": a.family, "outcome": a.outcome, "note": a.note}
        if served:
            rec_obj["served_model"] = ",".join(served)
            if served_mismatch:
                rec_obj["served_model_mismatch"] = True
            if served_drift:
                rec_obj["served_model_version_drift"] = True
                print(f"warning: candidate {a.candidate!r} declares model {expected!r} but the worker ran on "
                      f"{served_drift} (same family, different version); pricing may be based on the declared "
                      f"catalog entry, not the served version", file=sys.stderr)
        if a.tokens is not None:
            rec_obj["tokens"] = a.tokens
        if a.route:
            # Link the outcome to advisory evidence; retain legacy mass compatibility.
            if route_rec is None:
                route_rec = find_route_record(a.route)
                if route_rec is None:
                    raise SystemExit(f"unknown --route {a.route!r}: no matching record in {ROUTES_LEDGER}")
            picked = route_rec.get("picked")
            # S3: a candidate other than the route's picked id must be disclosed with --override,
            # or `record` would silently corrupt calibration data by attributing a route's outcome
            # to a different compatibility recommendation.
            if a.candidate != picked and not a.override:
                raise SystemExit(f"--candidate {a.candidate!r} differs from route {a.route!r}'s picked "
                                 f"candidate {picked!r}; pass --override \"<why>\" to record this as a "
                                 f"deliberate override, or fix --candidate")
            rec_obj["route_id"] = a.route

            evidence = next((c for c in route_rec.get("candidates", []) if c.get("id") == a.candidate), {})
            for field in ("fit", "fit_probabilities"):
                if evidence.get(field) is not None:
                    rec_obj[field] = evidence[field]
            def mass_of(cid):
                return next((c.get("mass") for c in route_rec.get("candidates", []) if c.get("id") == cid), None)

            mass = mass_of(a.candidate)
            if mass is not None:
                rec_obj["mass"] = mass
            if a.override:
                # K5/B3: the lead dispatched a different candidate than the route picked. Store
                # both: `mass` is the dispatched candidate's (the one whose outcome was actually
                # observed), `route_picked_mass` is the picked candidate's (what the route judged),
                # so calibration can compare them instead of losing one or the other.
                rec_obj["override"] = a.override
                rec_obj["route_picked"] = picked
                rec_obj["dispatched"] = a.candidate
                picked_mass = mass_of(picked)
                if picked_mass is not None:
                    rec_obj["route_picked_mass"] = picked_mass
        elif a.override:
            rec_obj["override"] = a.override
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps(rec_obj) + "\n")
        print(json.dumps({"recorded": rec_obj, "ledger": str(LEDGER)}))
    elif getattr(a, "calibration", False):
        bucket_rows, family_rows, (o_acc, o_n) = calibration_stats()
        print("Calibration by mass bucket:")
        for label, acc, n in bucket_rows:
            print(f"  {label:10} {acc}/{n} accepted" if n else f"  {label:10} no data")
        print("Calibration by family:")
        for fam, acc, n in family_rows:
            print(f"  {fam:14} {acc}/{n} accepted")
        print(f"Overrides: {o_acc}/{o_n} accepted")
    else:
        tok_stats = ledger_tokens()
        for (family, cid), s in sorted(ledger_stats().items()):
            n = s["accepted"] + s["rejected"]
            toks = tok_stats.get((family, cid))
            tok_str = f" median_tokens={int(median(toks))}" if toks else ""
            print(f"{family:14} {cid:20} {s['accepted']}/{n} accepted{tok_str}")


if __name__ == "__main__":
    main()
