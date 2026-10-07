"""Four authorized observational Jev calls; no dispatch or identity inference."""
import json
import argparse
from datetime import datetime, timezone
from pathlib import Path
import time

import full_cli_experiment as helper
from routing_experiment import SCRIPT, capture_key, load_source

NATIVE = ('gpt-6.1-sol', 'gpt-6-luna',
          'google-antigravity/claude-sonnet-4-6',
          'google-antigravity/claude-opus-4-6-thinking')


def forbidden(selector):
    return any(word in selector.lower() for word in ('astra', 'fable'))


def remap_semantics(current, ranked, eligible, semantic):
    """Preserve full-inventory scores and probabilities by candidate identity."""
    slots = {c['id']: f'c{i}' for i, c in enumerate(ranked)}
    remapped = {k: v for k, v in semantic.items() if not k.startswith('fit_')}
    for slot, c in current.jev_slots(eligible):
        for suffix in ('', '_probs'):
            remapped['fit_' + slot + suffix] = semantic['fit_' + slots[c['id']] + suffix]
    return remapped


def run(args):
    if not args.live:
        raise ValueError('Explicit --live required before discovery, credential access or paid calls')
    directory = Path(__file__).resolve().parent
    old_path = directory / 'full-cli-routing-20261002.json'
    if not old_path.exists():
        raise FileNotFoundError('full-cli-routing-20261002.json not found. Generate with full_cli_experiment.py --live first.')
    old = json.loads(old_path.read_text())
    fields = ('id', 'catalog_id', 'catalog_match', 'match_score', 'match_entries',
              'price', 'context', 'supports_tools')
    cache = {'models': [{k: row.get(k) for k in fields} for row in old['candidate_profiles']]}
    budget = next(row['price'] for row in cache['models'] if row['id'] == 'gpt-6-astra')
    current = load_source('live_ranked_current', SCRIPT.read_text())
    current.ledger_recent = lambda cid: []
    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None
    inventory = baseline['inventory'] if baseline else helper.discover_inventory()
    helper.SPAWNABLES = NATIVE
    filtered = {**inventory, 'models': [r for r in inventory['models'] if not forbidden(r['model'])]}
    candidates = helper.prepare_candidates(filtered, cache, current)
    stats, tokens = current.ledger_stats(), current.ledger_tokens()
    pools = sorted({c['billing']['pool'] for c in candidates if c['billing'].get('pool')})
    snapshots, warnings = current.gather_quota_snapshots(pools, [])
    thresholds = current.default_quota_thresholds(current.load_harness())
    now = time.time()
    sources = {p: current.quota_evidence(p, s, thresholds, now) for p, s in snapshots.items()}
    quotas = {p: current.pool_status(s, thresholds, now) for p, s in snapshots.items() if s is not None}
    quotas = {p: s for p, s in quotas.items() if s is not None}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = directory / ('live-ranked-routing-' + ('tuned-' if baseline else '') + stamp + '.json')
    report = dict(base_revision='7c38f4e', inventory=inventory,
                  candidate_profiles=candidates, native_allowlist=list(NATIVE),
                  exclusions={'case_insensitive_substrings': ['astra', 'fable'],
                              'excluded_count': inventory['count'] - len(candidates),
                              'payload_count': len(candidates)},
                  budget_reference={'price': budget, 'input_per_million': 10,
                                    'output_per_million': 50, 'call_permitted': False},
                  quota_sources=sources, quota_policy=quotas, quota_warning_count=len(warnings),
                  max_live_routing_calls=4, live_routing_calls=0, identity_api_calls=0,
                  task_executions=0, lead_judgment='pending independent lead review', scenarios=[])
    report['variant'] = 'tuned' if baseline else 'baseline'
    report['paired_baseline'] = Path(args.baseline).name if baseline else None
    report['maximum_combined_routing_calls'] = 8
    def save():
        output.write_text(json.dumps(report, indent=2) + '\n')
    key = capture_key()  # Ignore possibly invalid environment key; no credential persistence.
    for name, task in helper.tasks()[:4]:
        if baseline:
            task = dict(task)
            original = task.get('context', '')
            task['context'] = original + '\nCaller cost preference: prefer the cheapest adequately capable model for this task. A small suitability improvement does not justify multiples of cost unless a concrete task risk or capability requirement makes the cheaper option inadequate. Judge fit as task suitability, not price or prestige. Prices are proxies, not invoices. Acceptance verification: ' + '; '.join(task['acceptance']) + '\nError consequences are limited to the described task: ' + task['deliverable'] + '\nEvidence gaps: no measured comparative strength or task success, unknown current catalog freshness, output ceilings and actual billing; unknown quota is not available quota. No additional consequences or strength claims are supplied.'
        ranked = current.rank_candidates(candidates, task, stats, tokens, quotas)
        body = helper.make_request(task, ranked, current, stats, sources, quotas)
        ids = [c['identity']['worker_model'] for c in body['state']['candidates'].values()]
        assert len(ids) == len(candidates) and set(ids) == {c['id'] for c in candidates}
        assert not any(forbidden(cid) for cid in ids)
        entry = dict(name=name, task=task, request=body, payload_ids=ids)
        if baseline:
            prior = next(s for s in baseline['scenarios'] if s['name'] == name)
            assert ids == prior['payload_ids'], 'paired candidate ordering changed'
            entry['baseline_request'] = prior['request']
            entry['request_difference'] = {'task_context_before': prior['task'].get('context'),
                                           'task_context_after': task['context'],
                                           'fit_question_meaning_changed': False}
        report['live_routing_calls'] += 1
        payload, error = current.jev_post(body, key, 45)
        if error:
            entry['unavailable'] = error
            report['scenarios'].append(entry)
            report['status'] = 'stopped: Jev unavailable; no heuristic substitute'
            save()
            print(report['status'], error, output.name, flush=True)
            return False
        entry['response'] = {k: payload.get(k) for k in ('model', 'answers', 'usage')}
        check = helper.coverage(payload, body)
        if not check['complete']:
            raise RuntimeError('Incomplete response; no partial recommendation')
        semantic, error = current.parse_jev_answers(payload, body['questions'], .4)
        if error:
            raise RuntimeError('Invalid semantic response')
        eligible, rows = [], []
        for i, c in enumerate(ranked):
            reasons = []
            if not c['native_spawnable']:
                reasons.append('outside native allowlist')
            kept, why = current.filter_cheaper_than_lead([c], budget, current.in_out_ratio(task))
            if not kept:
                reasons.extend(why)
            kept, why = current.filter_candidates([c], task, stats, quotas)
            if not kept:
                reasons.extend(why or ['hard gate'])
            if not current.model_information_tier(c):
                reasons.append('incomplete/unverified metadata')
            if not reasons:
                eligible.append(c)
            answer = payload['answers'][f'fit_c{i}']
            rows.append(dict(id=c['id'], score=answer['score'], confidence=answer['confidence'],
                             probabilities=answer['probabilities'], advisory=True, exclusions=reasons,
                             catalog_proxy_cost=c['cost_usd'], effective_proxy_cost=c['effective_cost'],
                             token_basis=c.get('token_basis'), estimated_tokens=c.get('est_tokens'),
                             quota=sources.get(c['billing'].get('pool'), 'unknown'), actual_invoice_verified=False))
        remapped = remap_semantics(current, ranked, eligible, semantic)
        bands = current.price_bands(eligible)
        decision = current.decide(task, eligible, remapped, stats, bands) if eligible else {'decision': 'direct'}
        records = current.recommendation_records(task, eligible, remapped, decision, stats, bands,
                                                 quota_by_pool=quotas)
        safe = [r for r in rows if not r['exclusions']]
        cheapest = min(safe, key=lambda r: (r['effective_proxy_cost'], r['id'])) if safe else None
        entry.update(coverage=check, full_score_list=sorted(rows, key=lambda r: (-r['score'], r['id'])),
                     recommendation_records=records, compatibility_decision=decision,
                     cheapest_eligible=cheapest,
                     old_observation=next(s['evaluation'] for s in old['scenarios'] if s['name'] == name),
                     cost_conscious_lead_judgment='pending independent lead: cheaper adequate? why expensive necessary or not supported')
        report['scenarios'].append(entry)
        save()
        print(name, 'complete; ranked', records[0]['id'] if records else 'direct', flush=True)
    report['status'] = 'complete'
    save()
    print(output.name, flush=True)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly enable authorized paid calls')
    parser.add_argument('--baseline', help='Completed baseline JSON for four tuned calls only')
    args = parser.parse_args(argv)
    if not args.live:
        print('No paid calls: explicit --live is required.', flush=True)
        return 0
    try:
        return 0 if run(args) else 1
    except Exception as error:
        print('Experiment unavailable:', type(error).__name__, '(details suppressed)', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
