"""Offline losslessness and replay checks for the compact payload example."""
import copy
import json
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'skills/fast-delegate/examples'
sys.path.insert(0, str(EXAMPLES))
import compact_routing_payload as adapter
import full_cli_experiment as experiment


def serialized(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


class CompactPayloadTests(unittest.TestCase):
    def test_adversarial_values_and_model_overrides_round_trip(self):
        shared = {'unicode': '雪☃', 'zero': 0, 'false': False, 'empty': '', 'list': [], 'object': {}}
        order_ab = {'a': 1, 'b': 2}
        order_ba = {'b': 2, 'a': 1}
        value = {'models': ['model-a', 'model-b'], 'evidence': [shared, copy.deepcopy(shared)],
                 'overrides': {'model-a': {'context': None}, 'model-b': {'context': 0}},
                 'reserved': [{'$ref': 'literal'}, {'$literal': 'source data'},
                              {'$ref': {'nested': [{'$literal': None}, {'$ref': False}]}},
                              {'$reference': 'ordinary key', '$ref-extra': 7}],
                 'order': [order_ab, order_ba, copy.deepcopy(order_ab), copy.deepcopy(order_ba)],
                 'null': None, 'false': False, 'zero': 0, 'one': True,
                 'empty': '', 'list': [], 'object': {}}
        packed = adapter.compact(value)
        expanded = adapter.expand(packed)
        self.assertEqual(expanded, value)
        self.assertEqual(serialized(expanded), serialized(value))
        self.assertEqual(list(expanded['order'][1]), ['b', 'a'])
        self.assertEqual(list(expanded['order'][3]), ['b', 'a'])
        self.assertEqual(serialized(packed), serialized(adapter.compact(value)))
        self.assertIsNone(expanded['overrides']['model-a']['context'])
        self.assertIs(type(expanded['one']), bool)
        self.assertIs(type(expanded['zero']), int)

    def test_rejects_versions_unknown_references_cycles_and_bad_json(self):
        with self.assertRaisesRegex(ValueError, 'version'):
            adapter.expand({'version': 2, 'root': None, 'nodes': {}})
        with self.assertRaisesRegex(ValueError, 'unknown reference'):
            adapter.expand({'version': 1, 'root': {'$ref': 'missing'}, 'nodes': {}})
        with self.assertRaisesRegex(ValueError, 'cycle'):
            adapter.expand({'version': 1, 'root': {'$ref': 'a'}, 'nodes': {'a': {'$ref': 'a'}}})
        with self.assertRaisesRegex(ValueError, 'literal object'):
            adapter.expand({'version': 1, 'root': {'$literal': 'malformed'}, 'nodes': {}})
        cycle = []
        cycle.append(cycle)
        with self.assertRaisesRegex(ValueError, 'cyclic input'):
            adapter.compact(cycle)

    def test_six_saved_full_requests_round_trip_shrink_and_replay(self):
        report_path = EXAMPLES / 'full-cli-routing-20261002.json'
        if not report_path.exists():
            self.skipTest('full-cli-routing-20261002.json not present (regenerate with experiment script)')
        report = json.loads(report_path.read_text())
        current = experiment.load_source('compact-fixture', experiment.SCRIPT.read_text())
        current.ledger_recent = lambda cid: []
        before = after = 0
        for scenario in report['scenarios']:
            request = scenario['request']
            packed = adapter.compact(request)
            expanded = adapter.expand(packed)
            self.assertEqual(expanded, request, scenario['name'])
            self.assertEqual(serialized(expanded), serialized(request), scenario['name'])
            original_bytes, compact_bytes = len(serialized(request)), len(serialized(packed))
            self.assertLess(compact_bytes, original_bytes, scenario['name'])
            before += original_bytes
            after += compact_bytes

            response = scenario['response']
            self.assertTrue(experiment.coverage(response, expanded)['complete'])
            profiles = {c['id']: c for c in report['candidate_profiles']}
            rows = {r['id']: r for r in scenario['evaluation']['fit_table']}
            ranked = []
            for cid in scenario['payload_ids']:
                c = copy.deepcopy(profiles[cid])
                row = rows[cid]
                c.update(cost_usd=row['catalog_proxy_cost'], effective_cost=row['effective_proxy_cost'],
                         est_tokens=row['estimated_total_tokens'], price_basis=row['price_basis'],
                         token_basis=row['token_basis'], ledger_rate=None)
                ranked.append(c)
            replay = experiment.evaluate(scenario['task'], ranked, expanded, response, current, {}, report['quota_policy'])
            baseline = scenario['evaluation']
            uncompressed = experiment.evaluate(scenario['task'], ranked, scenario['request'], response, current, {}, report['quota_policy'])
            self.assertEqual(replay, uncompressed)
            self.assertTrue(replay['practical_recommendation']['review_required'])
            self.assertTrue(all('qualified' not in r for r in replay['fit_table']))
            self.assertEqual([r['id'] for r in replay['fit_table']], [r['id'] for r in baseline['fit_table']])
        self.assertEqual(len(report['inventory']['models']), 42)
        self.assertEqual(sum(s['question_count'] for s in report['scenarios']), 6 * 46)
        print(f'compact fixture UTF-8 bytes: {before} -> {after} (saved {before-after})')


if __name__ == '__main__':
    unittest.main()
