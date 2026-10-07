"""Offline billing inheritance and serialization checks for full CLI observations."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'skills/fast-delegate/examples'
sys.path.insert(0, str(EXAMPLES))
import full_cli_experiment as experiment


def current_source():
    current = experiment.load_source('billing-fixture', experiment.SCRIPT.read_text())
    current.ledger_recent = lambda cid: []
    return current


class BillingProvenanceTests(unittest.TestCase):
    def prepare(self, harness, selector='native-model'):
        current = current_source()
        current.load_harness = lambda: harness
        inventory = {'models': [{'model': selector, 'defaultReasoningEffort': 'medium'}]}
        return experiment.prepare_candidates(inventory, {'models': []}, current)[0]

    def test_inherited_deployment_metadata_resolves_native_pool(self):
        candidate = self.prepare({'runtime': 'proxy', 'provider': 'nvidia'})
        self.assertEqual(candidate['billing']['pool'], 'proxy:nvidia')
        self.assertEqual(candidate['billing']['runtime'], 'proxy')

    def test_candidate_overrides_pool_and_billing_policy_win(self):
        candidate = self.prepare({
            'runtime': 'proxy', 'provider': 'nvidia',
            'billing': {'native-model': {'runtime': 'other-runtime', 'provider': 'other-provider',
                'pool': 'chosen-pool', 'mode': 'subscription', 'price_override': 2.5,
                'quota_weight': 3, 'reasoning_effort': 'high'}},
        })
        self.assertEqual(candidate['billing'], {
            'mode': 'subscription', 'price_override': 2.5, 'quota_weight': 3,
            'runtime': 'other-runtime', 'provider': 'other-provider',
            'pool': 'chosen-pool', 'reasoning_effort': 'high',
        })

    def test_explicit_modes_and_price_override_are_preserved(self):
        for mode in ('metered', 'subscription', 'local'):
            with self.subTest(mode=mode):
                candidate = self.prepare({'billing': {'native-model': {
                    'mode': mode, 'price_override': 1.25,
                }}})
                self.assertEqual(candidate['billing']['mode'], mode)
                self.assertEqual(candidate['billing']['price_override'], 1.25)

    def test_unconfigured_slash_selector_stays_unknown_and_native_defaults_codex(self):
        self.assertNotIn('pool', self.prepare({}, 'provider/model')['billing'])
        self.assertEqual(self.prepare({}, 'native-model')['billing']['pool'], 'codex')
        self.assertNotIn('provider', self.prepare({}, 'provider/model')['billing'])

    def test_sensitive_configuration_never_reaches_serialized_candidates(self):
        sentinel = 'SENTINEL_PRIVATE_VALUE'
        candidate = self.prepare({
            'api_key': sentinel, 'account_id': sentinel, 'account_label': sentinel,
            'auth': {'token': sentinel}, 'notes': sentinel,
            'billing': {'native-model': {'mode': 'local', 'api_key': sentinel,
                'account_id': sentinel, 'account_label': sentinel,
                'auth': {'token': sentinel}, 'notes': sentinel}},
        })
        serialized = json.dumps(candidate)
        self.assertNotIn(sentinel, serialized)
        self.assertEqual(candidate['actual_billing'], 'unknown; default/operator policy is not a receipt')
        self.assertIn('actual dispatched configuration not observed', candidate['reasoning_provenance'])
        self.assertFalse(candidate['native_spawnable'])


if __name__ == '__main__':
    unittest.main()
