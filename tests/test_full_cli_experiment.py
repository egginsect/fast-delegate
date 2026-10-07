"""Offline full-inventory coverage and model-ID remapping checks; no live API calls."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'skills/fast-delegate/examples'
sys.path.insert(0, str(EXAMPLES))
import full_cli_experiment as experiment


def source():
    current = experiment.load_source('fixture-current', experiment.SCRIPT.read_text())
    current.ledger_recent = lambda cid: []
    return current


def candidate(cid, price=1, native=True, context=300000):
    return dict(id=cid, model_id=cid, catalog_id=cid, catalog_match='jev', match_score=.9,
                price={'input_per_m': price, 'output_per_m': price}, context=context,
                supports_tools=True, enabled=True, state='enabled', billing={'mode': 'metered'},
                catalog_entry={}, model_selector=cid, model_configuration={'reasoning_effort': 'high'},
                native_spawnable=native, reasoning_provenance='offline fixture only',
                actual_billing='unknown', identity_provenance='offline fixture only')


def response(body, fits=None):
    fits = fits or {}
    answers = {}
    for qid, question in body['questions'].items():
        if question['type'] == 'noul':
            answers[qid] = {'type': 'noul', 'noul': .95 if qid == 'independent' else 0}
        else:
            level = fits.get(qid, 4 if qid == 'verifiability' else 1 if qid == 'difficulty' else 4)
            answers[qid] = {'type': 'score', 'score': level, 'confidence': 1, 'probabilities': {str(level): 1}}
    return {'model': 'offline-fixture-only', 'answers': answers, 'usage': {'input_tokens': 0, 'output_tokens': 0}}


class FullInventoryTests(unittest.TestCase):
    def test_every_inventory_model_has_state_and_question_beyond_production_cap(self):
        current = source()
        candidates = [candidate('model-' + str(i), native=False) for i in range(47)]
        candidates[0].update(catalog_match='none', price=None, context=None, supports_tools=None)
        task = experiment.tasks()[0][1]
        ranked = current.rank_candidates(candidates, task, {})
        body = experiment.make_request(task, ranked, current, {}, {}, {})
        ids = [c['identity']['worker_model'] for c in body['state']['candidates'].values()]
        self.assertEqual(set(ids), {c['id'] for c in candidates})
        self.assertEqual(len(ids), 47)
        self.assertEqual(len(body['questions']), 51)
        self.assertEqual(current.JEV_MAX_CANDIDATES, 20)
        self.assertEqual(len(experiment.evaluate(task, ranked + [], body, response(body), current, {}, {}, lead_id='model-1')['fit_table']), 47)

    def test_missing_last_model_answer_is_explicit_coverage_failure(self):
        current = source()
        task = experiment.tasks()[0][1]
        ranked = current.rank_candidates([candidate('gpt-6-astra', 10)] + [candidate('m' + str(i)) for i in range(41)], task, {})
        body = experiment.make_request(task, ranked, current, {}, {}, {})
        payload = response(body)
        del payload['answers']['fit_c41']
        evaluated = experiment.evaluate(task, ranked, body, payload, current, {}, {})
        self.assertFalse(evaluated['coverage']['complete'])
        self.assertEqual(evaluated['coverage']['missing'], ['fit_c41'])
        self.assertEqual(evaluated['practical_recommendation']['decision'], 'unavailable')
        self.assertNotIn('top_five_raw_fits', evaluated)

    def test_noncontiguous_slots_are_remapped_by_model_id_and_probabilities(self):
        current = source()
        task = experiment.tasks()[0][1]
        ranked = current.rank_candidates([candidate('gpt-6-astra', 10), candidate('non-native', .1, False),
            candidate('cheap-native', 1), candidate('middle-hidden', 1.5, False), candidate('strong-native', 2)], task, {})
        body = experiment.make_request(task, ranked, current, {}, {}, {})
        # Eligible natives occupy original c1 and c3, not new subset c0 and c1.
        self.assertEqual([c['id'] for c in ranked], ['non-native', 'cheap-native', 'middle-hidden', 'strong-native', 'gpt-6-astra'])
        payload = response(body, {'fit_c0': 4, 'fit_c1': 0, 'fit_c2': 0, 'fit_c3': 4, 'fit_c4': 4})
        evaluated = experiment.evaluate(task, ranked, body, payload, current, {}, {})
        self.assertEqual(evaluated['practical_recommendation']['pick']['id'], 'cheap-native')
        judged = {c['id']: c['fit'] for c in evaluated['practical_recommendation']['judged']}
        self.assertEqual(judged, {'cheap-native': 0, 'strong-native': 4})
        self.assertEqual(len(evaluated['fit_table']), 5)

    def test_context_margin_is_real_production_requirement_and_independence_is_advisory(self):
        current = source()
        task = dict(experiment.tasks())['large context investigation']
        self.assertEqual(task['est_input_tokens'], 200000)
        self.assertNotIn('required_context', task)
        ranked = current.rank_candidates([candidate('gpt-6-astra', 10), candidate('small', 1, context=200000), candidate('large', 2)], task, {})
        body = experiment.make_request(task, ranked, current, {}, {}, {})
        payload = response(body)
        evaluated = experiment.evaluate(task, ranked, body, payload, current, {}, {})
        small = next(c for c in evaluated['fit_table'] if c['id'] == 'small')
        self.assertIn('skip small: context 200000 < 240000', small['exclusions'])
        self.assertEqual(len(body['state']['candidates']), 3)
        payload['answers']['independent']['noul'] = .05
        evaluated = experiment.evaluate(task, ranked, body, payload, current, {}, {})
        self.assertEqual(evaluated['practical_recommendation']['decision'], 'delegate')

    def test_extra_and_malformed_responses_never_silently_qualify(self):
        current = source()
        task = experiment.tasks()[0][1]
        ranked = current.rank_candidates([candidate('gpt-6-astra', 10)], task, {})
        body = experiment.make_request(task, ranked, current, {}, {}, {})
        payload = response(body)
        payload['answers']['fit_c0']['probabilities'] = {'999': 1}
        self.assertEqual(experiment.coverage(payload, body)['invalid'], ['fit_c0'])
        payload = response(body)
        payload['answers']['unexpected'] = {'type': 'noul', 'noul': 1}
        self.assertFalse(experiment.coverage(payload, body)['complete'])

    def test_committed_six_by_42_report_inventory_coverage_and_lead_alignment(self):
        report_path = EXAMPLES / 'full-cli-routing-20261002.json'
        if not report_path.exists():
            self.skipTest('full-cli-routing-20261002.json not present (regenerate with experiment script)')
        report = json.loads(report_path.read_text())
        inventory = report['inventory']
        ids = [m['model'] for m in inventory['models']]
        self.assertEqual(len(ids), 42)
        self.assertEqual(len(set(ids)), 42)
        self.assertEqual(inventory['ids_sha256'], experiment.digest(ids))
        self.assertEqual(inventory['inventory_sha256'], experiment.digest(inventory['models']))
        self.assertEqual(report['live_routing_calls'], 6)
        self.assertEqual(report['identity_api_calls'], 0)
        self.assertEqual(len(report['scenarios']), 6)
        lead = json.loads((EXAMPLES / 'full-cli-lead-verdicts-20261002.json').read_text())
        self.assertEqual(report['lead_verdicts'], lead)
        self.assertEqual([s['name'] for s in report['scenarios']], [v['name'] for v in lead['scenario_verdicts']])
        self.assertEqual(sorted(v['verdict'] for v in lead['scenario_verdicts']),
                         ['questionable'] * 3 + ['reasonable'] * 3)
        current = source()
        profiles = {c['id']: c for c in report['candidate_profiles']}
        for scenario, verdict in zip(report['scenarios'], lead['scenario_verdicts']):
            self.assertEqual(scenario['payload_count'], 42)
            self.assertEqual(len(scenario['payload_ids']), 42)
            self.assertEqual(set(scenario['payload_ids']), set(ids))
            body = scenario['request']
            self.assertEqual(len(body['state']['candidates']), 42)
            self.assertEqual(scenario['question_count'], 46)
            self.assertEqual(len(body['questions']), 46)
            self.assertTrue(experiment.coverage(scenario['response'], body)['complete'])
            self.assertEqual(len(scenario['evaluation']['fit_table']), 42)
            fit_rows = {r['id']: r for r in scenario['evaluation']['fit_table']}
            ranked = []
            for cid in scenario['payload_ids']:
                row = fit_rows[cid]
                c = copy.deepcopy(profiles[cid])
                c.update(cost_usd=row['catalog_proxy_cost'], effective_cost=row['effective_proxy_cost'],
                         est_tokens=row['estimated_total_tokens'], price_basis=row['price_basis'],
                         token_basis=row['token_basis'], ledger_rate=None)
                ranked.append(c)
            replay = experiment.evaluate(scenario['task'], ranked, body, scenario['response'], current, {}, report['quota_policy'])
            original = scenario['evaluation']['practical_recommendation']
            self.assertTrue(replay['practical_recommendation']['review_required'])
            self.assertTrue(all('qualified' not in r for r in replay['fit_table']))
            self.assertEqual(original.get('pick', {}).get('id', original['decision']), verdict['recommendation'])
            self.assertEqual([r['expected_fit'] for r in replay['top_five_raw_fits']], sorted([r['expected_fit'] for r in replay['fit_table']], reverse=True)[:5])
            if scenario['name'] == 'large context investigation':
                row = fit_rows['google-antigravity/claude-sonnet-4-6']
                self.assertEqual(row['token_basis'], 'family')
                self.assertEqual(row['estimated_total_tokens'], 65540)
                self.assertTrue(row['estimate_below_required_input'])
                self.assertEqual(scenario['evaluation']['required_context_with_margin'], 240000)
                self.assertEqual(row['catalog_proxy_cost'], .21952718)
            if scenario['name'] == 'ambiguous architectural design':
                self.assertEqual(original['decision'], 'direct')
                self.assertGreater(scenario['evaluation']['independent_probability'], .35)


if __name__ == '__main__':
    unittest.main()
