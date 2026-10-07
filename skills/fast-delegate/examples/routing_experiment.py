"""Six-call routing comparison; no discovery, matching, dispatch, or worker execution.

Use --live only with explicit API-spend authorization. The supplied cache is a local
identity observation, not an upstream/version or billing verification. OP title
metadata discovery and notesPlain capture happen in memory; IDs/keys never persist.
"""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/fdel.py'
BASELINE = 'e19d52b'


def load_source(name, source):
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(SCRIPT)
    exec(compile(source, str(SCRIPT), 'exec'), module.__dict__)
    return module


def capture_key():
    def op(*args):
        result = subprocess.run(['op', *args], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('OP operation unavailable (details suppressed)')
        return json.loads(result.stdout)
    items = op('item', 'list', '--format', 'json')
    matches = [item for item in items if item.get('title') == 'TYPESAFE_API_KEY']
    if len(matches) != 1:
        raise RuntimeError('OP key title must be unique')
    item = op('item', 'get', matches[0]['id'], '--vault', matches[0]['vault']['id'], '--format', 'json')
    fields = [f for f in item.get('fields', []) if f.get('id') == 'notesPlain']
    key = fields[0].get('value', '').strip() if len(fields) == 1 else ''
    if not key or len(key.split()) != 1:
        raise RuntimeError('OP notesPlain must contain one token')
    return key


def scenarios(cache, current):
    ids = ['nvidia/openai-gpt-oss-20b', 'opencode-go/qwen3.8-max', 'gpt-6.1-sol']
    rows = {c['id']: c for c in cache['models']}
    candidates = []
    for cid in ids:
        row = copy.deepcopy(rows[cid])
        runtime = 'proxy' if '/' in cid else 'codex'
        provider = cid.split('/')[0] if '/' in cid else 'openai'
        row.update(model_id=cid, enabled=True, state='enabled', source='local cached report',
                   catalog_entry={}, model_selector=cid,
                   billing={'mode': 'subscription', 'runtime': runtime, 'provider': provider,
                            'pool': runtime + ':' + provider, 'billing_source': 'experiment fixture'})
        candidates.append(row)
    simple = {'deliverable': 'Rename the internal helper parse_row to parse_record and update its two callers in parser.py.',
              'acceptance': ['python -m unittest tests.test_parser passes; no remaining parse_row references'],
              'owned_paths': ['parser.py', 'tests/test_parser.py'], 'access': 'write',
              'family': 'refactor', 'complexity': 'routine', 'est_input_tokens': 4000, 'est_output_tokens': 1000}
    hard = {'deliverable': 'Repair a race between cancellation and connection reuse in an asynchronous pool; preserve ownership invariants and add a deterministic concurrent regression test.',
            'acceptance': ['python -m unittest tests.test_pool passes including cancellation/reuse interleavings'],
            'owned_paths': ['pool.py', 'tests/test_pool.py'], 'access': 'write',
            'family': 'concurrency', 'complexity': 'hard', 'difficulty': 3,
            'est_input_tokens': 12000, 'est_output_tokens': 3000}
    now = time.time()
    thresholds = current.default_quota_thresholds({})
    def quota(kind):
        snapshots = {}
        for i, c in enumerate(candidates):
            pool = c['billing']['pool']
            snapshots[pool] = None if kind == 'unknown' else {
                'pool': pool, 'source': 'synthetic normalized quota fixture', 'observed_at': now if kind == 'fresh' else now - 1000000,
                'five_hour': {'used_percentage': 100 if kind == 'fresh' and i == 0 else 10, 'resets_at': now + 3600},
                'seven_day': {'used_percentage': 10, 'resets_at': now + 86400}}
        sources = {p: current.quota_evidence(p, s, thresholds, now) for p, s in snapshots.items()}
        states = {p: current.pool_status(s, thresholds, now) for p, s in snapshots.items() if s is not None}
        return sources, states
    for name, task, kind in [('simple-unknown', simple, 'unknown'), ('hard-stale', hard, 'stale'), ('hard-exhausted', hard, 'fresh')]:
        sources, states = quota(kind)
        yield name, task, copy.deepcopy(candidates), sources, states


def run(args):
    current = load_source('current', SCRIPT.read_text())
    result = subprocess.run(['git', 'show', BASELINE + ':skills/fast-delegate/scripts/fdel.py'], capture_output=True, text=True, check=True)
    baseline = load_source('baseline', result.stdout)
    # Never read/write the user's outcome ledger or route state.
    current.ledger_recent = baseline.ledger_recent = lambda cid: []
    cache = json.loads(Path(args.cache).read_text())
    if args.live:
        os.environ['TYPESAFE_API_KEY'] = capture_key()
        os.environ['TYPESAFE_TIMEOUT'] = '30'
    report = {'baseline_revision': BASELINE, 'live': args.live, 'max_routing_calls': 6,
              'identity_provenance': 'local cache; not rematched; not upstream/version verification',
              'quota_provenance': 'synthetic fixtures, not current account availability',
              'strength_evidence': 'empty isolated experiment ledger; real local outcomes not loaded; benchmarks unknown',
              'no_worker_execution': True, 'scenarios': []}
    calls = 0
    lead_price = next(c['price'] for c in cache['models'] if c['id'] == 'gpt-6-astra')
    for name, task, candidates, sources, states in scenarios(cache, current):
        cheaper, cost_reasons = current.filter_cheaper_than_lead(candidates, lead_price, current.in_out_ratio(task))
        kept, filter_reasons = current.filter_candidates(cheaper, task, {}, states)
        ranked = current.rank_candidates(kept, task, {}, {}, states)
        bands = current.price_bands(ranked)
        slots = current.jev_slots(ranked)
        entry = {'name': name, 'task': task, 'candidate_profiles': candidates,
                 'quota_sources': sources, 'quota_policy': states,
                 'gate_reasons': cost_reasons + filter_reasons, 'eligible_ranked': ranked, 'runs': {}}
        for label, module in [('baseline', baseline), ('enriched', current)]:
            body = module.build_jev_request(task, slots, bands, {}) if label == 'baseline' else current.build_jev_request(task, slots, bands, {}, quota_sources=sources, quota_by_pool=states, catalog_info={'source': 'local prior report', 'freshness': 'unknown'})
            run = {'request': body}
            if args.live:
                if calls >= 6:
                    raise RuntimeError('live request budget exceeded')
                calls += 1
                payload, error = current.jev_post(body, os.environ['TYPESAFE_API_KEY'], 30)
                if error:
                    run['unavailable'] = error
                    semantic = None
                else:
                    run['response'] = {k: payload.get(k) for k in ('model', 'answers', 'usage')}
                    semantic, error = current.parse_jev_answers(payload, body['questions'], .4)
                    if error or any(not semantic.get(q + '_probs') for q in body['questions'] if q.startswith('fit_')):
                        raise RuntimeError('live response malformed (details suppressed)')
                decision = current.decide(task, ranked, semantic, {}, bands)
                run['composed_selection'] = decision
            entry['runs'][label] = run
        if args.live:
            selections = [entry['runs'][k]['composed_selection'].get('pick', {}).get('id') for k in ('baseline', 'enriched')]
            entry['selection_changed'] = selections[0] != selections[1]
        report['scenarios'].append(entry)
    report['live_routing_calls'] = calls
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    print('Routing experiment:', calls, 'live calls; 0 matching calls; 0 worker executions')
    for entry in report['scenarios']:
        if args.live:
            print(entry['name'], {k: v['composed_selection'].get('pick', {}).get('id', 'direct') for k, v in entry['runs'].items()})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--live', action='store_true')
    try:
        run(parser.parse_args())
    except Exception:
        print('Experiment unavailable; details suppressed to protect credentials', file=sys.stderr)
        sys.exit(1)
    finally:
        os.environ.pop('TYPESAFE_API_KEY', None)
