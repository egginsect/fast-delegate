"""Observe fit for every current CLI-visible model; never dispatch a worker.

No production cap override or eligibility filtering happens before inference.
The practical recommendation separately uses caller-attested native spawnables
and production gates. OP credentials and protocol errors never leave memory.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import select
import subprocess
import time

from routing_experiment import SCRIPT, capture_key, load_source

SPAWNABLES = ('gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna',
              'google-antigravity/claude-opus-4-6-thinking',
              'google-antigravity/claude-sonnet-4-6')
MODEL_FIELDS = ('id', 'model', 'displayName', 'description', 'hidden', 'isDefault',
                'supportedReasoningEfforts', 'defaultReasoningEffort', 'inputModalities')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def discover_inventory(command=('codex', 'app-server', '--stdio')):
    """Read every page of model/list, no threads/tasks; persist only safe model fields."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1)
    def send(value):
        process.stdin.write(json.dumps(value) + '\n')
        process.stdin.flush()
    def receive(request_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not select.select([process.stdout], [], [], 1)[0]:
                continue
            line = process.stdout.readline()
            if not line:
                raise RuntimeError('CLI inventory stream closed')
            value = json.loads(line)
            if value.get('id') == request_id:
                if 'error' in value:
                    raise RuntimeError('CLI inventory RPC rejected; details suppressed')
                return value['result']
        raise RuntimeError('CLI inventory timed out')
    rows, cursors = [], set()
    try:
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'full-cli-fit-observation', 'version': '1'}}})
        receive(1)
        send({'method': 'initialized', 'params': {}})
        cursor, request_id = None, 2
        while True:
            params = {'includeHidden': False, 'limit': 1000}
            if cursor:
                params['cursor'] = cursor
            send({'id': request_id, 'method': 'model/list', 'params': params})
            page = receive(request_id)
            rows.extend({k: row[k] for k in MODEL_FIELDS if k in row} for row in page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                break
            if cursor in cursors:
                raise RuntimeError('CLI inventory repeated cursor')
            cursors.add(cursor)
            request_id += 1
        ids = [row['model'] for row in rows]
        if not ids or len(set(ids)) != len(ids):
            raise RuntimeError('CLI inventory empty or duplicate model selectors')
        version = subprocess.run(['codex', '--version'], capture_output=True, text=True, check=True).stdout.strip()
        return {'captured_at': datetime.now(timezone.utc).isoformat(), 'source': 'installed codex model/list; includeHidden=false; all pages',
                'cli_version': version, 'count': len(rows), 'ids_sha256': digest(ids),
                'inventory_sha256': digest(rows), 'models': rows,
                'native_spawnability_provenance': 'five selectors attested by lead from current native spawn tool; CLI visibility is not permission'}
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def tasks():
    common = {'need_tools': True, 'access': 'read', 'est_input_tokens': 4000, 'est_output_tokens': 1000}
    profiles = [
        ('mechanical rename', dict(deliverable='Rename parse_row to parse_record and update its two callers in parser.py.',
             acceptance=['python -m unittest tests.test_parser passes; no parse_row references remain'],
             owned_paths=['parser.py', 'tests/test_parser.py'], family='refactor', complexity='routine', access='write')),
        ('concurrency race', dict(deliverable='Repair the race between cancellation and connection reuse in an asynchronous pool. Preserve ownership invariants and add deterministic interleaving regression tests.',
             acceptance=['python -m unittest tests.test_pool passes including cancellation/reuse interleavings'],
             owned_paths=['pool.py', 'tests/test_pool.py'], family='concurrency', complexity='hard', difficulty=3,
             est_input_tokens=12000, est_output_tokens=3000, access='write')),
        ('security authorization audit', dict(deliverable='Read the tenant authorization middleware and handlers; audit whether an authenticated tenant can access another tenant through direct object IDs. Report evidence-linked defects and executable regression-test proposals; do not change access controls.',
             acceptance=['Every finding cites file and line, expected authorization, concrete cross-tenant request, and expected denied response; include negative controls for all audited handlers'],
             owned_paths=['auth.py', 'handlers.py', 'tests/test_auth.py'], family='security', complexity='hard', difficulty=3,
             est_input_tokens=18000, est_output_tokens=4000)),
        ('large context investigation', dict(deliverable='Investigate a cross-repository request flow using the full supplied 200000-token snapshot in a single context. No external retrieval, summarization or chunking is permitted. Trace all affected repositories and explain the fault with citations.',
             context='Exactly 200000 input tokens are required simultaneously; production includes 20 percent headroom, so minimum context is 240000. No retrieval or chunking allowed.',
             acceptance=['Cite each repository boundary and invariant; reconcile every referenced symbol against the supplied complete snapshot'],
             owned_paths=['repo-a/', 'repo-b/', 'repo-c/'], family='investigation', complexity='hard', difficulty=3,
             est_input_tokens=200000, est_output_tokens=6000)),
        ('precise extraction', dict(deliverable='Read the supplied 3000-token records file and extract exact record IDs whose status is ACTIVE, without inferring absent values.',
             acceptance=['Output exactly one JSON array of original ID strings in source order, preserving duplicates; no prose; json.loads succeeds and exact fixture comparison passes'],
             owned_paths=['records.json'], family='extraction', complexity='routine', est_input_tokens=3000, est_output_tokens=1000, need_tools=False)),
        ('ambiguous architectural design', dict(deliverable='Invent the best novel architecture for our next product. Requirements, constraints, stakeholders and existing system are unspecified; resolve everything as you see fit.',
             acceptance=['The architecture feels elegant and innovative'], owned_paths=[], family='architecture', complexity='novel', difficulty=4,
             context='No additional requirements or constraints are provided. Success depends on unstated stakeholder preferences.'))]
    return [(name, {**common, **profile}) for name, profile in profiles]


def prepare_candidates(inventory, cache, current):
    cached = {row['id']: row for row in cache['models']}
    harness = current.load_harness()
    candidates = []
    for row in inventory['models']:
        cid = row['model']
        prior = cached.get(cid, {})
        # Match production policy/default precedence, then retain only safe evidence fields.
        # Harness configuration may contain credentials or private account notes.
        resolved = current.resolve_billing(cid, harness)
        allowed = ('mode', 'price_override', 'quota_weight', 'runtime', 'provider',
                   'pool', 'reasoning_effort', 'billing_source')
        safe_billing = {k: resolved[k] for k in allowed if k in resolved}
        billing = current.normalize_billing(safe_billing)
        current.apply_pool(billing, 'spawnable', 'codex', cid)
        effort = row.get('defaultReasoningEffort', 'unknown')
        c = {'id': cid, 'model_id': cid, 'catalog_id': prior.get('catalog_id'),
             'catalog_match': prior.get('catalog_match', 'none'), 'match_score': prior.get('match_score'),
             'match_entries': prior.get('match_entries', []), 'price': prior.get('price'),
             'context': prior.get('context'), 'supports_tools': prior.get('supports_tools'),
             'catalog_entry': {}, 'description': row.get('description', ''),
             'enabled': True, 'state': 'enabled', 'billing': billing,
             'model_selector': cid, 'model_configuration': {'reasoning_effort': effort},
             'cli_visible': True, 'native_spawnable': cid in SPAWNABLES,
             'identity_provenance': 'prior local cache, exact unchanged selector' if prior else 'unknown; no matching request',
             'reasoning_provenance': 'CLI default; actual dispatched configuration not observed',
             'actual_billing': 'unknown; default/operator policy is not a receipt',
             'max_output_tokens': 'unknown; prior report has no catalog entry'}
        candidates.append(c)
    return candidates


def make_request(task, ranked, current, stats, sources, quotas):
    # Deliberately enumerate ALL models; do not call jev_slots or mutate its cap.
    slots = [(f'c{i}', c) for i, c in enumerate(ranked)]
    body = current.build_jev_request(task, slots, current.price_bands(ranked), stats,
                                   quota_sources=sources, quota_by_pool=quotas,
                                   catalog_info={'source': 'prior local cached identity report', 'freshness': 'unknown'})
    body['state']['task']['need_tools'] = task['need_tools']
    body['state']['task']['context_required_with_margin'] = int(task['est_input_tokens'] * 1.2)
    for slot, c in slots:
        body['state']['candidates'][slot].update(cli_visible=True, native_spawnable=c['native_spawnable'],
            reasoning_provenance=c['reasoning_provenance'], actual_billing=c['actual_billing'],
            identity_provenance=c['identity_provenance'], output_limit_evidence='unknown')
    actual = [body['state']['candidates'][slot]['identity']['worker_model'] for slot, _ in slots]
    expected = [c['id'] for c in ranked]
    if actual != expected or len(set(actual)) != len(actual) or len(slots) != len(ranked):
        raise ValueError('full request inventory coverage failed')
    return body


def coverage(payload, body):
    answers = payload.get('answers', {}) if isinstance(payload, dict) else {}
    if not isinstance(answers, dict):
        return {'complete': False, 'missing': list(body['questions']), 'invalid': ['answers'], 'extra': []}
    missing = sorted(set(body['questions']) - set(answers))
    extra = sorted(set(answers) - set(body['questions']))
    invalid = []
    for qid, question in body['questions'].items():
        a = answers.get(qid)
        if qid in missing:
            continue
        if not isinstance(a, dict) or a.get('type') != question['type']:
            invalid.append(qid)
            continue
        if question['type'] == 'noul':
            value = a.get('noul')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                invalid.append(qid)
        else:
            probs = a.get('probabilities', {})
            values = list(probs.values()) if isinstance(probs, dict) else probs if isinstance(probs, list) else []
            levels = list(probs) if isinstance(probs, dict) else list(range(len(values)))
            try:
                valid = (bool(values) and all(str(level) in {'0', '1', '2', '3', '4'} for level in levels)
                         and all(not isinstance(v, bool) and isinstance(v, (int, float)) and 0 <= v <= 1 for v in values)
                         and math.isclose(sum(values), 1, abs_tol=.01)
                         and isinstance(a.get('score'), (int, float)) and 0 <= a['score'] <= 4
                         and isinstance(a.get('confidence'), (int, float)) and 0 <= a['confidence'] <= 1)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                invalid.append(qid)
    return {'complete': not (missing or invalid or extra), 'missing': missing, 'invalid': invalid, 'extra': extra}


def evaluate(task, ranked, body, payload, current, stats, quotas, lead_id='gpt-6-astra'):
    check = coverage(payload, body)
    if not check['complete']:
        return {'coverage': check, 'practical_recommendation': {'decision': 'unavailable', 'reason': 'response coverage failure; no partial recommendation'}}
    semantic, error = current.parse_jev_answers(payload, body['questions'], .4)
    if error:
        raise ValueError('response parse failure')
    lead = next(c for c in ranked if c['id'] == lead_id)
    rows, eligible = [], []
    slot_ids = {}
    for i, c in enumerate(ranked):
        slot = f'c{i}'
        slot_ids[c['id']] = slot
        reasons = []
        if not c['native_spawnable']:
            reasons.append('not in lead-attested native spawnable set')
        kept, cost_reasons = current.filter_cheaper_than_lead([c], lead['price'], current.in_out_ratio(task))
        if not kept:
            reasons.extend(cost_reasons)
        kept, hard_reasons = current.filter_candidates([c], task, stats, quotas)
        if not kept:
            reasons.extend(hard_reasons or ['disabled or excluded'])
        if not current.model_information_tier(c):
            reasons.append('incomplete/unverified model metadata; no override authorized')
        if reasons == []:
            eligible.append(c)
        a = payload['answers']['fit_' + slot]
        rows.append({'id': c['id'], 'expected_fit': a['score'], 'confidence': a['confidence'],
                     'probabilities': a['probabilities'], 'advisory': True, 'eligible': not reasons,
                     'native_spawnable': c['native_spawnable'], 'exclusions': reasons,
                     'catalog_proxy_cost': c['cost_usd'], 'effective_proxy_cost': c['effective_cost'],
                     'quota': c['billing'].get('pool'), 'reasoning_effort': c.get('model_configuration', {}).get('reasoning_effort', 'unknown'),
                     'price_basis': c.get('price_basis'), 'token_basis': c.get('token_basis'),
                     'estimated_total_tokens': c.get('est_tokens'),
                     'estimated_input_tokens': current.split_tokens(c['est_tokens'], task)[0],
                     'estimated_output_tokens': current.split_tokens(c['est_tokens'], task)[1],
                     'scenario_required_input_tokens': task['est_input_tokens'],
                     'estimate_below_required_input': current.split_tokens(c['est_tokens'], task)[0] < task['est_input_tokens'],
                     'billing_mode_policy': c['billing'].get('mode', 'metered'),
                     'billing_source': c['billing'].get('billing_source', 'operator'),
                     'actual_invoice_verified': False})
    # Remap original full-inventory slots to the <=5 native production slots.
    # Never re-infer or cap the observational inventory.
    remapped = {k: v for k, v in semantic.items() if not k.startswith('fit_')}
    for slot, c in current.jev_slots(eligible):
        original = slot_ids[c['id']]
        remapped['fit_' + slot] = semantic.get('fit_' + original)
        remapped['fit_' + slot + '_probs'] = semantic['fit_' + original + '_probs']
    if not eligible:
        decision = {'decision': 'direct', 'reasons': ['no native eligible candidate passes deterministic gates']}
    else:
        decision = current.decide(task, eligible, remapped, stats, current.price_bands(eligible))
    if 'pick' in decision:
        decision['pick'] = {k: decision['pick'][k] for k in ('id', 'cost_usd', 'effective_cost')}
    top = sorted(rows, key=lambda row: (-row['expected_fit'], row['id']))[:5]
    return {'coverage': check, 'independent_probability': semantic.get('independent'),
            'difficulty': semantic.get('difficulty'), 'verifiability': semantic.get('verifiability'),
            'required_context_with_margin': int(task['est_input_tokens'] * 1.2),
            'fit_table': rows, 'top_five_raw_fits': top, 'practical_recommendation': decision}


def run(args):
    current = load_source('current', SCRIPT.read_text())
    # Raw free-text ledger records are excluded; genuine aggregate outcomes remain.
    current.ledger_recent = lambda cid: []
    inventory = discover_inventory()
    print('Current CLI inventory:', inventory['count'], 'models;', inventory['captured_at'], 'hash', inventory['ids_sha256'], flush=True)
    cache = json.loads(Path(args.cache).read_text())
    candidates = prepare_candidates(inventory, cache, current)
    pools = sorted({c['billing']['pool'] for c in candidates if c['billing'].get('pool')})
    snapshots, warnings = current.gather_quota_snapshots(pools, [])
    # Manual quota env overrides are not accepted as observations in this experiment.
    thresholds = current.default_quota_thresholds(current.load_harness())
    now = time.time()
    sources = {p: current.quota_evidence(p, s, thresholds, now) for p, s in snapshots.items()}
    quotas = {p: current.pool_status(s, thresholds, now) for p, s in snapshots.items() if s is not None}
    quotas = {p: s for p, s in quotas.items() if s is not None}
    stats, token_stats = current.ledger_stats(), current.ledger_tokens()
    rubric = json.loads(Path(args.rubric).read_text())
    report = {'inventory': inventory, 'candidate_profiles': candidates, 'quota_sources': sources, 'quota_policy': quotas,
              'quota_warning_count': len(warnings), 'lead_rubric': rubric, 'lead_verdicts': None,
              'limits': ['no worker/task execution', 'no measured task success', 'no measured accepted-task cost',
                         'cached model identities/prices, unknown current catalog freshness', 'CLI defaults not executed reasoning configuration',
                         'output token ceilings missing from prior cache', 'unknown quota for unmapped deployment/account',
                         'no integrated strength benchmarks; ledger evidence is observational'],
              'max_live_routing_calls': 6, 'live_routing_calls': 0, 'identity_api_calls': 0,
              'native_spawnables': list(SPAWNABLES),
              'budget_reference_model': {'id': 'gpt-6-astra', 'provenance': 'lead-specified routing budget reference from existing session; actual serving model not independently verified'},
              'scenarios': []}
    if args.live:
        os.environ['TYPESAFE_API_KEY'] = capture_key()
    for name, task in tasks():
        ranked = current.rank_candidates(candidates, task, stats, token_stats, quotas)
        body = make_request(task, ranked, current, stats, sources, quotas)
        ids = [v['identity']['worker_model'] for v in body['state']['candidates'].values()]
        if len(ids) != inventory['count'] or set(ids) != {m['model'] for m in inventory['models']}:
            raise ValueError('inventory request mismatch')
        entry = {'name': name, 'task': task, 'payload_ids': ids, 'payload_count': len(ids),
                 'question_count': len(body['questions']), 'request': body}
        if args.live:
            if report['live_routing_calls'] >= 6:
                raise ValueError('live budget exceeded')
            report['live_routing_calls'] += 1
            payload, error = current.jev_post(body, os.environ['TYPESAFE_API_KEY'], 45)
            if error:
                entry['response_failure'] = error
                entry['evaluation'] = {'practical_recommendation': {'decision': 'unavailable', 'reason': 'request failed; no retry or truncation'}}
            else:
                entry['response'] = {k: payload.get(k) for k in ('model', 'answers', 'usage')}
                entry['evaluation'] = evaluate(task, ranked, body, payload, current, stats, quotas)
            d = entry['evaluation']['practical_recommendation']
            print(name, 'payload', len(ids), 'coverage', entry['evaluation'].get('coverage', {}).get('complete'),
                  'practical', d.get('pick', {}).get('id', d['decision']), flush=True)
        report['scenarios'].append(entry)
        Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    print('Completed:', report['live_routing_calls'], 'routing calls; 0 identity calls; 0 task executions', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', required=True)
    parser.add_argument('--rubric', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--live', action='store_true')
    try:
        # Do not treat manual declarations as fresh quota snapshots.
        os.environ.pop('FAST_DELEGATE_QUOTA', None)
        run(parser.parse_args())
    except Exception as error:
        print('Full inventory experiment unavailable:', type(error).__name__, '(details suppressed)')
        raise SystemExit(1)
    finally:
        os.environ.pop('TYPESAFE_API_KEY', None)
