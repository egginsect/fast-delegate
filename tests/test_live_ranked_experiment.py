"""Offline spend guard and noncontiguous paired semantic mapping checks."""
import argparse
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/fast-delegate/examples'))
import live_ranked_experiment as experiment
from test_full_cli_experiment import candidate, source


class LiveRankedTests(unittest.TestCase):
    def test_no_live_flag_cannot_access_credentials_discover_or_infer(self):
        current = source()
        with patch.object(experiment, 'capture_key') as key, \
             patch.object(experiment.helper, 'discover_inventory') as inventory, \
             patch.object(experiment, 'load_source', return_value=current) as load, \
             patch.object(current, 'jev_post') as infer, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(experiment.main([]), 0)
            self.assertEqual(experiment.main(['--baseline', '/nonexistent/baseline.json']), 0)
            with self.assertRaisesRegex(ValueError, 'Explicit --live'):
                experiment.run(argparse.Namespace(live=False, baseline=None))
            key.assert_not_called()
            inventory.assert_not_called()
            load.assert_not_called()
            infer.assert_not_called()

    def test_paired_noncontiguous_remap_preserves_scores_probabilities_and_ranking(self):
        current = source()
        task = experiment.helper.tasks()[0][1]
        ranked = current.rank_candidates([candidate('non-native', .1, False),
                                         candidate('cheap-native', 1),
                                         candidate('hidden-middle', 1.5, False),
                                         candidate('strong-native', 2)], task, {})
        eligible = [c for c in ranked if c['native_spawnable']]
        for cheap_score, strong_score in ((2.1, 3.9), (3.8, 3.1)):
            semantic = {'independent': .95, 'difficulty': 1, 'verifiability': 4,
                        'fit_c0': 4, 'fit_c0_probs': {4: 1},
                        'fit_c1': cheap_score, 'fit_c1_probs': {3: .6, 4: .4},
                        'fit_c2': 0, 'fit_c2_probs': {0: 1},
                        'fit_c3': strong_score, 'fit_c3_probs': {3: .2, 4: .8}}
            remapped = experiment.remap_semantics(current, ranked, eligible, semantic)
            self.assertEqual(remapped['fit_c0'], cheap_score)
            self.assertEqual(remapped['fit_c1'], strong_score)
            self.assertEqual(remapped['fit_c0_probs'], {3: .6, 4: .4})
            self.assertEqual(remapped['fit_c1_probs'], {3: .2, 4: .8})
            self.assertNotIn('fit_c3', remapped)
            bands = current.price_bands(eligible)
            decision = current.decide(task, eligible, remapped, {}, bands)
            records = current.recommendation_records(task, eligible, remapped, decision,
                                                     {}, bands)
            expected = ['strong-native', 'cheap-native'] if strong_score > cheap_score else ['cheap-native', 'strong-native']
            self.assertEqual([r['id'] for r in records], expected)
            self.assertTrue(all(r['review_required'] for r in records))
