"""Offline checks for the lead-trial runner: stream parsing, repricing, answer-key locking,
config isolation and secret hygiene. The live smoke (`lead_trial_experiment.py smoke`) is the
real acceptance gate for the pieces these fixtures can only imitate."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'skills/fast-delegate/examples'
sys.path.insert(0, str(EXAMPLES))
import lead_trial_experiment as lt

SECRETS = {'TYPESAFE_API_KEY': 'test-key-not-real', 'TYPESAFE_API_KEY_OP_REF': 'op://example/item/field',
           'OP_SERVICE_ACCOUNT_TOKEN': 'test-token-not-real'}


def tok(i=0, c=0, w=0, o=0):
    return {'input': i, 'cached_input': c, 'cache_write': w, 'output': o}


def mu(i=0, c=0, w=0, o=0, cost=0.0):
    return {'inputTokens': i, 'cacheReadInputTokens': c, 'cacheCreationInputTokens': w, 'outputTokens': o, 'costUSD': cost}


def candidate(cid, model_id, inp=None, out=None, cache_read=None, cache_creation=None, per_m=None):
    entry = {k: v for k, v in {'input_cost_per_token': inp, 'output_cost_per_token': out,
                               'cache_read_input_token_cost': cache_read,
                               'cache_creation_input_token_cost': cache_creation}.items() if v is not None}
    return {'id': cid, 'model_id': model_id, 'catalog_entry': entry, 'price': per_m}


DISCOVER = {'candidates': [
    candidate('haiku', 'claude-haiku-4-5', 1e-6, 5e-6, 1e-7, 1.25e-6),
    candidate('opus', 'claude-opus-5-5', 4e-6, 2e-5, 2e-7, 5e-6),
    candidate('proxy-gpt-5-6-luna', 'gpt-5.6-luna', 2e-7, 1.2e-6, 2e-8, 2.5e-7),
    candidate('proxy-gpt-5-6-terra', 'gpt-5.6-terra', 2e-6, 1.2e-5, 2e-7, 2.5e-6),
    candidate('proxy-gpt-5-5', 'gpt-5.5', 5e-6, 3e-5, 5e-7, None),                      # no cache-write rate
    candidate('proxy-per-m', 'per-m-model', per_m={'input_per_m': 3.0, 'output_per_m': 9.0}),  # only per-million prices
    {'id': 'proxy-self', 'model_id': None, 'catalog_entry': {}, 'price': None},          # unpriced
]}


def asst(blocks, parent=None, model='claude-opus-5-5', mid=None):
    return {'type': 'assistant', 'parent_tool_use_id': parent,
            'message': {'id': mid or f'msg_{abs(hash(json.dumps(blocks, sort_keys=True))) % 10**8}', 'model': model, 'content': blocks}}


def tool(name, tid, **inp):
    return {'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}


def text(t):
    return {'type': 'text', 'text': t}


def stream_events(final_text='done\nFINAL: ACCEPTED'):
    return [
        {'type': 'system', 'subtype': 'init', 'session_id': 's1', 'model': 'claude-opus-5-5', 'agents': ['proxy-a']},
        asst([tool('Agent', 'toolu_A', subagent_type='proxy-gpt-5-6-luna', model='haiku', description='implement')]),
        asst([tool('Agent', 'toolu_A', subagent_type='proxy-gpt-5-6-luna', model='haiku', description='implement')]),  # duplicate block event
        {'type': 'system', 'subtype': 'task_started', 'tool_use_id': 'toolu_A', 'subagent_type': 'proxy-gpt-5-6-luna'},
        asst([tool('Bash', 'toolu_B', command='cat /secret/oracle/test_oracle.py')], parent='toolu_A',
             model='proxy-claude-native--gpt-5.6-luna'),
        asst([tool('Agent', 'toolu_C', subagent_type='haiku-sub', model='haiku', description='nested')], parent='toolu_A',
             model='proxy-claude-native--gpt-5.6-luna'),
        asst([tool('Skill', 'toolu_D', skill='fast-delegate')]),
        asst([tool('Bash', 'toolu_E', command='python3 /x/skills/fast-delegate/scripts/fdel.py route --task t.json')]),
        asst([tool('Bash', 'toolu_F', command='ls /locked/key/dir/inner')]),
        asst([tool('Read', 'toolu_G', file_path='/repo/ledger/summary.py')]),
        {'type': 'system', 'subtype': 'task_notification', 'tool_use_id': 'toolu_A', 'status': 'completed', 'summary': 'ok'},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'total_cost_usd': 0.5, 'num_turns': 3,
         'duration_ms': 1000, 'modelUsage': {}, 'result': 'interim'},
        asst([text(final_text)]),
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'total_cost_usd': 1.25, 'num_turns': 7,
         'duration_ms': 4200, 'duration_api_ms': 3900, 'terminal_reason': 'completed',
         'permission_denials': [{'tool_name': 'WebFetch'}], 'subagent_stats': {'spawned': 2},
         'modelUsage': {'claude-opus-5-5': mu(10, 20, 30, 40, 1.0)}, 'result': final_text},
    ]


def as_text(events):
    return '\n'.join(['not json at all', ''] + [json.dumps(e) for e in events]) + '\n'


class StreamParsing(unittest.TestCase):
    def setUp(self):
        self.parsed = lt.parse_stream(as_text(stream_events()), locked_paths=['/locked/key/dir'])

    def test_result_fields_come_from_the_last_result_event(self):
        r = self.parsed['result']
        self.assertEqual((r['total_cost_usd'], r['num_turns'], r['duration_ms'], r['is_error']), (1.25, 7, 4200, False))
        self.assertEqual((r['terminal_reason'], r['permission_denials']), ('completed', 1))
        self.assertEqual(r['model_usage'], {'claude-opus-5-5': mu(10, 20, 30, 40, 1.0)})
        self.assertEqual(self.parsed['result_events'], 2)
        self.assertEqual(self.parsed['lead_model_served'], 'claude-opus-5-5')

    def test_final_claim(self):
        self.assertEqual(self.parsed['final_claim'], 'ACCEPTED')

    def test_spawns_have_type_model_description_parent_and_status(self):
        spawns = {s['tool_use_id']: s for s in self.parsed['spawns']}
        self.assertEqual(sorted(spawns), ['toolu_A', 'toolu_C'])  # duplicate event not double counted
        a, c = spawns['toolu_A'], spawns['toolu_C']
        self.assertEqual((a['subagent_type'], a['model'], a['description']), ('proxy-gpt-5-6-luna', 'haiku', 'implement'))
        self.assertEqual((a['parent'], a['parent_agent']), (None, 'lead'))
        self.assertEqual((a['started'], a['status']), (True, 'completed'))
        self.assertEqual(a['served_models'], ['proxy-claude-native--gpt-5.6-luna'])
        self.assertEqual((c['parent'], c['parent_agent']), ('toolu_A', 'proxy-gpt-5-6-luna'))

    def test_skill_and_fdel_calls(self):
        self.assertEqual([s['skill'] for s in self.parsed['skill_calls']], ['fast-delegate'])
        self.assertEqual([c['subcommand'] for c in self.parsed['fdel_calls']], ['route'])
        self.assertEqual(self.parsed['tool_counts']['Bash'], 3)

    def test_leak_flags_cover_lead_and_subagent_tool_inputs(self):
        flags = {f['tool_use_id']: f for f in self.parsed['leak_flags']}
        self.assertEqual(sorted(flags), ['toolu_B', 'toolu_F'])
        self.assertEqual(flags['toolu_B']['parent'], 'toolu_A')  # a subagent touched 'oracle'
        self.assertEqual(flags['toolu_B']['reasons'], ['oracle'])
        self.assertIn('locked_path:/locked/key/dir', flags['toolu_F']['reasons'])  # the lead touched a locked path
        self.assertIsNone(flags['toolu_F']['parent'])

    def test_final_claim_variants(self):
        for body, want in [('FINAL: NOT_ACCEPTED', 'NOT_ACCEPTED'), ('**FINAL: ACCEPTED**', 'ACCEPTED'),
                           ('text\nFINAL: ACCEPTED\nmore\nFINAL: NOT_ACCEPTED\n- agent list', 'NOT_ACCEPTED'),
                           ('I think FINAL: ACCEPTED mid line', None), ('no claim at all', None)]:
            self.assertEqual(lt.parse_final_claim(body), want, body)

    def test_run_that_died_without_a_result_event(self):
        events = [e for e in stream_events('partial\nFINAL: NOT_ACCEPTED') if e['type'] != 'result']
        parsed = lt.parse_stream(as_text(events))
        self.assertIsNone(parsed['result'])
        self.assertEqual(parsed['final_claim'], 'NOT_ACCEPTED')  # falls back to the lead's last message


class Repricing(unittest.TestCase):
    def setUp(self):
        self.table = lt.price_table(DISCOVER)

    def test_price_table_skips_unpriced_and_accepts_per_million_prices(self):
        self.assertNotIn('proxy-self', self.table)
        self.assertAlmostEqual(self.table['proxy-per-m']['input'], 3e-6)
        self.assertAlmostEqual(self.table['proxy-per-m']['output'], 9e-6)

    def test_model_key_normalisation(self):
        n = lt.normalize_model_key
        self.assertEqual(n('proxy-claude-native--gpt-5.6-luna'), 'gpt-5.6-luna')
        self.assertEqual(n('claude-haiku-4-5-20251001'), 'claude-haiku-4-5')
        self.assertEqual(n('claude-opus-5-5[1m]'), 'claude-opus-5-5')
        self.assertEqual(lt.match_price('proxy-claude-native--gpt-5.6-luna', self.table)['id'], 'proxy-gpt-5-6-luna')
        self.assertIsNone(lt.match_price('mystery-model', self.table))

    def test_cost_includes_cache_read_and_write(self):
        usd, notes = lt.compute_cost(tok(1000, 10000, 2000, 500), self.table['haiku'])
        self.assertAlmostEqual(usd, 1000 * 1e-6 + 10000 * 1e-7 + 2000 * 1.25e-6 + 500 * 5e-6)
        self.assertEqual(notes, [])

    def test_missing_cache_write_rate_falls_back_to_input_rate_and_says_so(self):
        usd, notes = lt.compute_cost(tok(0, 0, 100, 0), self.table['proxy-gpt-5-5'])
        self.assertAlmostEqual(usd, 100 * 5e-6)
        self.assertEqual(len(notes), 1)
        self.assertEqual(lt.compute_cost(tok(0, 0, 0, 10), self.table['proxy-gpt-5-5'])[1], [])  # nothing to assume

    def test_lead_and_worker_usd_split_by_model_and_unknown_model_is_null_not_zero(self):
        usage = {'claude-opus-5-5[1m]': mu(1000, 0, 0, 100, 0.123),
                 'proxy-claude-native--gpt-5.6-luna': mu(5000, 0, 0, 50, 9.99),
                 'mystery-model': mu(777, 0, 0, 7, 0.5)}
        out = lt.reprice(usage, self.table, lead_key='claude-opus-5-5', cli_total=10.0)
        self.assertAlmostEqual(out['lead_usd'], 1000 * 4e-6 + 100 * 2e-5)
        self.assertIsNone(out['worker_usd'])  # one worker row has no price
        self.assertAlmostEqual(out['worker_usd_priced_part'], 5000 * 2e-7 + 50 * 1.2e-6)
        self.assertIsNone(out['total_usd'])
        self.assertEqual(out['cli_total_usd'], 10.0)  # the CLI figure is carried unchanged
        self.assertTrue(any("'mystery-model'" in w for w in out['warnings']))
        priced = {r['model_key']: r for r in out['rows']}
        self.assertIsNone(priced['mystery-model']['usd'])
        self.assertEqual(priced['proxy-claude-native--gpt-5.6-luna']['candidate'], 'proxy-gpt-5-6-luna')

    def test_fully_priced_run_has_a_total(self):
        out = lt.reprice({'claude-opus-5-5': mu(100), 'proxy-claude-native--gpt-5.6-luna': mu(100)}, self.table,
                         lead_key='claude-opus-5-5')
        self.assertAlmostEqual(out['total_usd'], 100 * 4e-6 + 100 * 2e-7)
        self.assertEqual(out['warnings'], ['no session transcripts: lead/worker split uses the modelUsage key only'])

    def test_no_model_usage_means_null_never_zero(self):
        out = lt.reprice({}, self.table, lead_key='claude-opus-5-5')
        self.assertEqual((out['lead_usd'], out['worker_usd'], out['total_usd']), (None, None, None))
        self.assertTrue(any('no modelUsage' in w for w in out['warnings']))
        empty = lt.reprice({'claude-opus-5-5': mu(5)}, {}, lead_key='claude-opus-5-5')
        self.assertIsNone(empty['lead_usd'])


class PlaceholderAttribution(unittest.TestCase):
    """modelUsage lumps a proxy-routed subagent under the placeholder model (observed live);
    the transcripts hold the exactly-known native usage, the remainder belongs to the route."""

    def setUp(self):
        self.table = lt.price_table(DISCOVER)
        self.haiku = 'claude-haiku-4-5-20251001'
        self.luna = 'proxy-claude-native--gpt-5.6-luna'
        self.terra = 'proxy-claude-native--gpt-5.6-terra'
        self.spawns = [{'model': 'haiku', 'served_models': [self.luna], 'subagent_type': 'proxy-gpt-5-6-luna'},
                       {'model': 'haiku', 'served_models': [self.haiku], 'subagent_type': 'general-purpose'}]

    def transcripts(self, partial=None):
        return {'files': 2, 'lead': {self.haiku: tok(30, 3000, 400, 300)}, 'side': {self.haiku: tok(10, 100, 50, 60)},
                'partial': partial or {self.luna: tok(48000)}}

    def test_remainder_goes_to_the_served_route_and_is_priced_with_its_rates(self):
        lumped = {self.haiku: mu(30 + 10 + 48000, 3000 + 100, 400 + 50, 300 + 60 + 25)}
        out = lt.reprice(lumped, self.table, self.spawns, self.transcripts(), self.haiku)
        rows = {(r['model_key'], r['role']): r for r in out['rows']}
        self.assertEqual(rows[(self.luna, 'worker')]['tokens'], tok(48000, 0, 0, 25))
        self.assertEqual(rows[(self.haiku, 'lead')]['tokens'], tok(30, 3000, 400, 300))
        self.assertAlmostEqual(rows[(self.luna, 'worker')]['usd'], 48000 * 2e-7 + 25 * 1.2e-6)
        self.assertAlmostEqual(out['lead_usd'], 30 * 1e-6 + 3000 * 1e-7 + 400 * 1.25e-6 + 300 * 5e-6)
        self.assertAlmostEqual(out['worker_usd'], (10 * 1e-6 + 100 * 1e-7 + 50 * 1.25e-6 + 60 * 5e-6)
                               + 48000 * 2e-7 + 25 * 1.2e-6)
        self.assertTrue(any('placeholder' in w for w in out['warnings']))

    def test_two_routes_share_the_remainder_by_message_start_input_weights_and_conserve_tokens(self):
        spawns = self.spawns + [{'model': 'haiku', 'served_models': [self.terra], 'subagent_type': 'proxy-gpt-5-6-terra'}]
        transcripts = self.transcripts({self.luna: tok(30000), self.terra: tok(10000)})
        out = lt.reprice({self.haiku: mu(40 + 1001, 3100, 450, 360 + 101)}, self.table, spawns, transcripts, self.haiku)
        routed = [r for r in out['rows'] if r['source'].startswith('residual:')]
        self.assertEqual({r['model_key'] for r in routed}, {self.luna, self.terra})
        self.assertTrue(all(r['approximate'] for r in routed))
        self.assertEqual(sum(r['tokens']['input'] for r in routed), 1001)
        self.assertEqual(sum(r['tokens']['output'] for r in routed), 101)

    def test_a_route_with_its_own_modelusage_key_is_just_a_worker_row(self):
        usage = {self.haiku: mu(40, 3100, 450, 360), self.luna: mu(5000, 0, 0, 7)}
        spawns = [{'model': None, 'served_models': [self.luna], 'subagent_type': 'proxy-gpt-5-6-luna'}]
        out = lt.reprice(usage, self.table, spawns, self.transcripts(), self.haiku)
        row = next(r for r in out['rows'] if r['model_key'] == self.luna)
        self.assertEqual((row['role'], row['source'], row['tokens']), ('worker', 'modelUsage', tok(5000, 0, 0, 7)))
        self.assertFalse(any('placeholder' in w for w in out['warnings']))

    def test_worker_on_the_same_model_as_the_lead_is_split_by_transcript_thread(self):
        t = {'files': 1, 'lead': {'claude-opus-5-5': tok(10, 0, 0, 5)}, 'side': {'claude-opus-5-5': tok(90, 0, 0, 45)}, 'partial': {}}
        out = lt.reprice({'claude-opus-5-5': mu(100, 0, 0, 50)}, self.table, [], t, 'claude-opus-5-5')
        self.assertAlmostEqual(out['lead_usd'], 10 * 4e-6 + 5 * 2e-5)
        self.assertAlmostEqual(out['worker_usd'], 90 * 4e-6 + 45 * 2e-5)

    def test_unexplained_modelusage_surplus_is_kept_and_reported(self):
        t = {'files': 1, 'lead': {'claude-opus-5-5': tok(10)}, 'side': {}, 'partial': {}}
        out = lt.reprice({'claude-opus-5-5': mu(15)}, self.table, [], t, 'claude-opus-5-5')
        self.assertAlmostEqual(out['lead_usd'] + out['worker_usd'], 15 * 4e-6)
        self.assertTrue(any('not found in transcripts' in w for w in out['warnings']))

    def test_read_transcripts_dedupes_message_ids_and_separates_threads(self):
        with tempfile.TemporaryDirectory() as d:
            proj = Path(d, 'projects', 'p')
            (proj / 'sess' / 'subagents').mkdir(parents=True)

            def line(mid, model, stop, usage, sidechain=False):
                return json.dumps({'type': 'assistant', 'isSidechain': sidechain,
                                   'message': {'id': mid, 'model': model, 'stop_reason': stop, 'usage': usage}})
            u = {'input_tokens': 5, 'cache_read_input_tokens': 7, 'cache_creation_input_tokens': 11, 'output_tokens': 13}
            (proj / 'sess.jsonl').write_text('\n'.join([line('m1', 'h', 'end_turn', u)] * 2 + [line('m2', 'h', 'tool_use', u),
                                                                                              '{"type":"user"}', 'junk']))
            (proj / 'sess' / 'subagents' / 'agent-x.jsonl').write_text('\n'.join([
                line('m3', 'h', 'end_turn', u, True), line('m4', 'proxy-r', None, {'input_tokens': 900, 'output_tokens': 0}, True)]))
            tr = lt.read_transcripts(d)
        self.assertEqual(tr['files'], 2)
        self.assertEqual(tr['lead']['h'], tok(10, 14, 22, 26))  # m1 once, m2 once
        self.assertEqual(tr['side']['h'], tok(5, 7, 11, 13))
        self.assertEqual(tr['partial']['proxy-r']['input'], 900)


class AnswerKeyLock(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self.cleanup)
        self.state = self.tmp / 'lockstate'
        self.key = self.tmp / 'key'
        (self.key / 'sub').mkdir(parents=True)
        (self.key / 'a.py').write_text('a')
        (self.key / 'sub' / 'b.py').write_text('b')
        os.chmod(self.key / 'a.py', 0o640)
        os.chmod(self.key / 'sub' / 'b.py', 0o600)
        os.chmod(self.key / 'sub', 0o750)
        self.paths = [self.key, self.key / 'a.py', self.key / 'sub', self.key / 'sub' / 'b.py']
        self.original = {p: p.stat().st_mode & 0o7777 for p in self.paths}

    def cleanup(self):
        for p in [self.key, self.key / 'sub', self.key / 'a.py', self.key / 'sub' / 'b.py']:
            with contextlib.suppress(OSError):
                os.chmod(p, 0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def modes(self):
        return {p: os.lstat(p).st_mode & 0o7777 for p in self.paths}

    def locked_modes(self):
        """Modes of every path while the directories are 000: open each directory for a moment to
        look, then close it again."""
        dirs, saved = [self.key, self.key / 'sub'], {}
        try:
            for d in dirs:  # outermost first: a directory cannot be inspected before its parent is open
                saved[d] = os.stat(d).st_mode & 0o7777
                os.chmod(d, 0o700)
            return {p: saved.get(p, os.lstat(p).st_mode & 0o7777) for p in self.paths}
        finally:
            for d in reversed(list(saved)):
                os.chmod(d, saved[d])

    def test_everything_is_000_inside_and_original_modes_come_back(self):
        with lt.locked_keys([self.key], self.state):
            self.assertEqual(set(self.locked_modes().values()), {0})
            self.assertEqual(os.stat(self.key).st_mode & 0o7777, 0)
        self.assertEqual(self.modes(), self.original)
        self.assertFalse((self.state / 'state.json').exists())

    def test_modes_are_restored_when_the_block_raises(self):
        with self.assertRaises(RuntimeError):
            with lt.locked_keys([self.key], self.state):
                raise RuntimeError('lead blew up')
        self.assertEqual(self.modes(), self.original)

    def test_single_file_and_nested_paths(self):
        with lt.locked_keys([self.key / 'a.py', self.key, self.key / 'sub'], self.state) as roots:
            self.assertEqual(roots, [str(self.key)])  # nested paths collapse into their root
        self.assertEqual(self.modes(), self.original)
        with lt.locked_keys([self.key / 'a.py'], self.state):
            self.assertEqual(self.modes()[self.key / 'a.py'], 0)
            self.assertEqual(self.modes()[self.key / 'sub'], self.original[self.key / 'sub'])
        self.assertEqual(self.modes(), self.original)

    def test_overlapping_holders_keep_the_lock_until_the_last_one_leaves(self):
        with lt.locked_keys([self.key], self.state):
            with lt.locked_keys([self.key], self.state):
                pass
            self.assertEqual(set(self.locked_modes().values()), {0})  # inner exit must not unlock
        self.assertEqual(self.modes(), self.original)  # and the second holder never recorded 000 as 'original'

    def test_stale_sidecar_from_a_killed_process_is_recovered(self):
        code = textwrap.dedent(f'''
            import sys, time
            sys.path.insert(0, {str(EXAMPLES)!r})
            import lead_trial_experiment as lt
            with lt.locked_keys([{str(self.key)!r}], {str(self.state)!r}):
                print("locked", flush=True)
                time.sleep(120)
        ''')
        proc = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.stdout.close)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'locked')
            proc.send_signal(signal.SIGKILL)  # no finally block runs
            proc.wait()
        finally:
            proc.kill()
        self.assertEqual(set(self.locked_modes().values()), {0})
        self.assertTrue((self.state / 'state.json').exists())
        recovered = lt.recover_stale_lock(self.state)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(self.modes(), self.original)
        self.assertFalse((self.state / 'state.json').exists())
        self.assertEqual(lt.recover_stale_lock(self.state), [])

    def test_the_next_locker_recovers_a_dead_holders_stale_lock_itself(self):
        other = self.tmp / 'other'
        other.mkdir()
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        entries = lt._snapshot_modes(self.key)
        state = {'roots': {str(self.key): {'entries': entries, 'holders': ['dead:1']}},
                 'holders': {'dead:1': {'pid': dead.pid, 'roots': [str(self.key)]}}}
        self.state.mkdir()
        for path, _ in reversed(entries):
            os.chmod(path, 0)
        (self.state / 'state.json').write_text(json.dumps(state))
        with lt.locked_keys([other], self.state):
            self.assertEqual(self.modes(), self.original)  # dead holder's root restored on entry
        self.assertFalse((self.state / 'state.json').exists())

    def test_copy_locked_tree_reads_a_tree_another_holder_has_locked_and_relocks_it(self):
        with lt.locked_keys([self.key], self.state):
            dest = self.tmp / 'copy'
            lt.copy_locked_tree(self.key, dest, self.state)
            self.assertEqual((dest / 'sub' / 'b.py').read_text(), 'b')
            self.assertEqual(set(self.locked_modes().values()), {0})
        self.assertEqual(self.modes(), self.original)


class IsolationAndEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'claude-home'
        (self.home / 'agents').mkdir(parents=True)
        (self.home / '.credentials.json').write_text('{"fake": true}')
        for name in ('proxy-a', 'proxy-b', 'proxy-self', 'plain-agent'):
            (self.home / 'agents' / f'{name}.md').write_text(f'---\nname: {name}\n---\n')
        (self.home / 'CLAUDE.md').write_text('must never be copied')
        (self.home / 'hooks').mkdir()
        (self.home / 'plugins').mkdir()
        self.skill = self.tmp / 'skill'
        (self.skill / 'scripts' / '__pycache__').mkdir(parents=True)
        (self.skill / 'examples').mkdir()
        (self.skill / 'SKILL.md').write_text('skill')
        (self.skill / 'harness.json').write_text('{}')
        (self.skill / 'scripts' / 'fdel.py').write_text('print(1)')
        (self.skill / 'scripts' / '__pycache__' / 'x.pyc').write_text('x')
        (self.skill / 'examples' / 'big-results.json').write_text('{}')

    def config(self, arm):
        d = self.tmp / f'cfg-{arm}'
        files = lt.make_lead_config(d, arm, self.skill, self.home)
        return d, files

    def test_skill_arm_contents(self):
        d, files = self.config('skill')
        self.assertEqual(set(files), {'.credentials.json', 'settings.json', 'agents', 'agents/proxy-a.md', 'agents/proxy-b.md',
                                      'skills', 'skills/fast-delegate', 'skills/fast-delegate/SKILL.md',
                                      'skills/fast-delegate/harness.json', 'skills/fast-delegate/scripts',
                                      'skills/fast-delegate/scripts/fdel.py'})
        self.assertTrue((d / '.credentials.json').is_symlink())
        self.assertEqual((d / '.credentials.json').resolve(), (self.home / '.credentials.json').resolve())
        self.assertEqual(json.loads((d / 'settings.json').read_text()), {})
        self.assertFalse((d / 'agents' / 'proxy-self.md').exists())

    def test_noskill_arm_has_no_skills_at_all(self):
        d, files = self.config('noskill')
        self.assertEqual(set(files), {'.credentials.json', 'settings.json', 'agents', 'agents/proxy-a.md', 'agents/proxy-b.md'})
        self.assertFalse((d / 'skills').exists())

    def test_neither_arm_carries_memory_hooks_plugins_or_claude_md(self):
        for arm in lt.ARMS:
            d, files = self.config(arm)
            for forbidden in ('CLAUDE.md', 'hooks', 'plugins', 'projects', 'settings.local.json'):
                self.assertNotIn(forbidden, files)
            self.assertNotIn('hooks', json.loads((d / 'settings.json').read_text()))

    def test_missing_credentials_and_bad_arm_fail_with_descriptive_errors(self):
        (self.home / '.credentials.json').unlink()
        with self.assertRaisesRegex(lt.TrialError, 'credentials not found'):
            lt.make_lead_config(self.tmp / 'x', 'skill', self.skill, self.home)
        with self.assertRaisesRegex(lt.TrialError, 'arm must be one of'):
            lt.make_lead_config(self.tmp / 'y', 'both', self.skill, self.home)

    def test_state_is_seeded_with_independent_copies(self):
        src = self.tmp / 'state-src'
        src.mkdir()
        for n in ('harness.json', 'catalog_matches.json', 'outcomes.jsonl', 'routes.jsonl'):
            (src / n).write_text(n)
        dest = self.tmp / 'state'
        seeded, missing = lt.make_state_dir(dest, src)
        self.assertEqual(seeded, ['harness.json', 'catalog_matches.json', 'outcomes.jsonl'])
        self.assertFalse((dest / 'routes.jsonl').exists())
        (dest / 'outcomes.jsonl').write_text('trial wrote here')
        self.assertEqual((src / 'outcomes.jsonl').read_text(), 'outcomes.jsonl')  # the real ledger is untouched
        (src / 'harness.json').unlink()
        self.assertEqual(lt.make_state_dir(self.tmp / 'state2', src)[1], ['harness.json'])

    def test_env_is_an_allowlist_with_jev_passthrough_by_name(self):
        base = {'PATH': '/bin', 'HOME': '/h', 'CLAUDECODE': '1', 'CLAUDE_CODE_SESSION_ID': 'x', 'ANTHROPIC_API_KEY': 'k',
                'RANDOM_VAR': 'v', 'ANTHROPIC_BASE_URL': 'http://127.0.0.1:1', **SECRETS}
        env, passed = lt.build_env('/c', '/s', '/t', base)
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], '/c')
        self.assertEqual(env['FAST_DELEGATE_STATE'], '/s')
        self.assertEqual(env['TMPDIR'], '/t')
        for name, value in SECRETS.items():
            self.assertEqual(env[name], value)
        self.assertEqual(set(passed), set(SECRETS) | {'ANTHROPIC_BASE_URL'})
        for absent in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'ANTHROPIC_API_KEY', 'RANDOM_VAR'):
            self.assertNotIn(absent, env)
        env2, passed2 = lt.build_env('/c', '/s', '/t', {'PATH': '/bin', 'HOME': '/h'})
        self.assertEqual(passed2, [])
        self.assertNotIn('TYPESAFE_API_KEY', env2)

    def test_launcher_choice(self):
        self.assertEqual(lt.default_claude_cmd({'LEAD_TRIAL_CLAUDE_CMD': 'my claude --x'}), ['my', 'claude', '--x'])
        self.assertEqual(lt.default_claude_cmd({'ANTHROPIC_BASE_URL': 'http://x', 'PATH': os.environ['PATH']}), ['claude'])
        self.assertEqual(lt.default_claude_cmd({'PATH': str(self.tmp)}), ['claude'])
        self.assertEqual(lt.default_claude_cmd({}), ['claude'])

    def test_lead_command_matches_the_contract(self):
        cmd = lt.lead_command(['claude'], 'the prompt', 'opus')
        self.assertEqual(cmd, ['claude', '-p', 'the prompt', '--model', 'opus', '--output-format', 'stream-json', '--verbose',
                               '--allowedTools', 'Agent,Task,Read,Write,Edit,Bash,Glob,Grep,Skill'])

    def test_prompt_rendering(self):
        (self.tmp / 'p.txt').write_text('before {ARM_RULE} after')
        (self.tmp / 'r.json').write_text(json.dumps({'skill': 'USE SKILL', 'noskill': 'CHOOSE YOURSELF'}))
        self.assertEqual(lt.render_prompt(self.tmp / 'p.txt', self.tmp / 'r.json', 'noskill'), 'before CHOOSE YOURSELF after')
        (self.tmp / 'bad.txt').write_text('no placeholder')
        with self.assertRaisesRegex(lt.TrialError, 'placeholder'):
            lt.render_prompt(self.tmp / 'bad.txt', self.tmp / 'r.json', 'skill')
        (self.tmp / 'r2.json').write_text(json.dumps({'skill': 'x'}))
        with self.assertRaisesRegex(lt.TrialError, 'noskill'):
            lt.render_prompt(self.tmp / 'p.txt', self.tmp / 'r2.json', 'noskill')

    def test_lock_paths_that_would_break_the_lead_are_refused(self):
        trial = self.tmp / 'out' / 'skill-1'
        trial.mkdir(parents=True)
        with self.assertRaisesRegex(lt.TrialError, 'contains'):
            lt.check_lock_paths([self.tmp / 'out'], [trial])
        with self.assertRaisesRegex(lt.TrialError, 'working area'):
            lt.check_lock_paths([Path.home()], [trial])
        lt.check_lock_paths([self.tmp / 'elsewhere'], [trial])  # unrelated path is fine


FAKE_CLAUDE = r'''
import json, os, subprocess, sys, time
ORACLE, PIDFILE = {oracle!r}, {pidfile!r}
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
cfg = os.environ["CLAUDE_CONFIG_DIR"]
bad = []
if any(k == "CLAUDECODE" or k.startswith("CLAUDE_CODE") for k in os.environ): bad.append("session env leaked")
if os.stat(ORACLE).st_mode & 0o777 != 0: bad.append("oracle not locked")
if os.path.exists(os.path.join(cfg, "CLAUDE.md")): bad.append("CLAUDE.md in config")
if subprocess.run(["git", "remote"], capture_output=True, text=True).stdout.strip(): bad.append("origin remote present")
if "--allowedTools" not in args or "stream-json" not in args: bad.append("bad flags")
if bad:
    print("INVARIANT FAILED: " + repr(bad), file=sys.stderr); sys.exit(4)
secret = os.environ.get("TYPESAFE_API_KEY", "")
def emit(e): print(json.dumps(e), flush=True)
def asst(blocks, model="claude-opus-5-5", parent=None, mid="m0"):
    return {{"type": "assistant", "parent_tool_use_id": parent, "message": {{"id": mid, "model": model, "content": blocks}}}}
emit({{"type": "system", "subtype": "init", "session_id": "sess", "model": "claude-opus-5-5", "agents": ["proxy-a"]}})
emit(asst([{{"type": "tool_use", "id": "toolu_1", "name": "Agent",
            "input": {{"subagent_type": "proxy-gpt-5-6-luna", "model": "haiku", "description": "impl"}}}}], mid="m1"))
emit(asst([{{"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {{"command": "echo $TYPESAFE_API_KEY"}}}}], mid="m2"))
emit(asst([{{"type": "text", "text": "key is " + secret}}], parent="toolu_1", model="proxy-claude-native--gpt-5.6-luna", mid="m3"))
print("stderr leak " + secret, file=sys.stderr, flush=True)
if "MODE=timeout" in prompt:
    child = subprocess.Popen(["sleep", "300"])
    open(PIDFILE, "w").write(str(child.pid))
    time.sleep(300)
open("solution.txt", "w").write("ok")
subprocess.run(["git", "add", "solution.txt"], check=True)
subprocess.run(["git", "commit", "-q", "-m", "add solution"], check=True)
proj = os.path.join(cfg, "projects", "p"); os.makedirs(os.path.join(proj, "sess", "subagents"))
def tline(mid, model, stop, usage):
    return json.dumps({{"type": "assistant", "message": {{"id": mid, "model": model, "stop_reason": stop, "usage": usage}}}})
open(os.path.join(proj, "sess.jsonl"), "w").write(tline("m1", "claude-opus-5-5", "end_turn",
    {{"input_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 100}}) + "\n")
open(os.path.join(proj, "sess", "subagents", "agent-1.jsonl"), "w").write(tline("m3", "proxy-claude-native--gpt-5.6-luna", None,
    {{"input_tokens": 5000, "output_tokens": 0}}) + "\n")
final = "all done " + secret + "\nFINAL: ACCEPTED"
emit(asst([{{"type": "text", "text": final}}], mid="m4"))
emit({{"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 9.99, "num_turns": 4, "duration_ms": 10,
      "modelUsage": {{"claude-opus-5-5": {{"inputTokens": 1000, "outputTokens": 100, "cacheReadInputTokens": 0,
                                           "cacheCreationInputTokens": 0, "costUSD": 1.0}},
                     "proxy-claude-native--gpt-5.6-luna": {{"inputTokens": 5000, "outputTokens": 9, "cacheReadInputTokens": 0,
                                                          "cacheCreationInputTokens": 0, "costUSD": 8.0}}}},
      "result": final}})
'''

FAKE_PYTEST = r'''
import os, sys
xml = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--junitxml="))
oracle = "test_oracle.py" in " ".join(sys.argv)
solved = os.path.exists("solution.txt")
cases = [("test_a", True), ("test_b", solved)] if oracle else [("test_own", True)]
body = "".join('<testcase classname="%s" name="%s"/>' % ("oracle" if oracle else "own", n) if ok else
               '<testcase classname="oracle" name="%s"><failure message="x"/></testcase>' % n for n, ok in cases)
open(xml, "w").write("<testsuites><testsuite>%s</testsuite></testsuites>" % body)
sys.exit(0 if all(ok for _, ok in cases) else 1)
'''


class FullTrialWithFakeClaude(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self.cleanup)
        t = self.tmp
        self.base = t / 'base'
        self.base.mkdir()
        for cmd in (['init', '-q'], ['config', 'user.email', 'a@b.c'], ['config', 'user.name', 'n']):
            subprocess.run(['git', '-C', str(self.base), *cmd], check=True)
        (self.base / 'README.md').write_text('hi')
        subprocess.run(['git', '-C', str(self.base), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.base), 'commit', '-q', '-m', 'init'], check=True)
        self.oracle = t / 'oracle'
        (self.oracle / 'sub').mkdir(parents=True)
        (self.oracle / 'test_oracle.py').write_text('# hidden')
        os.chmod(self.oracle / 'sub', 0o750)
        self.oracle_modes = {p: p.stat().st_mode & 0o7777 for p in (self.oracle, self.oracle / 'sub', self.oracle / 'test_oracle.py')}
        self.pidfile = t / 'child.pid'
        (t / 'fake_claude.py').write_text(FAKE_CLAUDE.format(oracle=str(self.oracle), pidfile=str(self.pidfile)))
        (t / 'fake_pytest.py').write_text(FAKE_PYTEST)
        (t / 'prompt.txt').write_text('lead prompt. {ARM_RULE}')
        (t / 'rules.json').write_text(json.dumps({'skill': 'use the skill', 'noskill': 'MODE=timeout pick yourself'}))
        self.home = t / 'claude-home'
        (self.home / 'agents').mkdir(parents=True)
        (self.home / '.credentials.json').write_text('{}')
        (self.home / 'agents' / 'proxy-gpt-5-6-luna.md').write_text('---\nname: x\n---\n')
        self.skill = t / 'skill'
        (self.skill / 'scripts').mkdir(parents=True)
        (self.skill / 'scripts' / 'fdel.py').write_text('')
        self.env = {'PATH': os.environ['PATH'], 'HOME': os.environ['HOME'], **SECRETS}
        self.lockstate = t / 'lockstate'

    def cleanup(self):
        for p in [self.oracle, self.oracle / 'sub', self.oracle / 'test_oracle.py']:
            with contextlib.suppress(OSError):
                os.chmod(p, 0o755)
        if self.pidfile.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(self.pidfile.read_text()), signal.SIGKILL)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_trial(self, arm='skill', trial=1, **kw):
        args = dict(arm=arm, trial=trial, base=self.base, oracle=self.oracle, out=self.tmp / 'out',
                    prompt_file=self.tmp / 'prompt.txt', arm_rules=self.tmp / 'rules.json', lead_model='opus',
                    timeout=60, skill_dir=self.skill, claude_cmd=[sys.executable, str(self.tmp / 'fake_claude.py')],
                    claude_home=self.home, state_src=self.tmp / 'nostate', base_env=self.env,
                    discover_fn=lambda fdel, env, cwd: DISCOVER, grade_cmd=[sys.executable, str(self.tmp / 'fake_pytest.py')],
                    lock_state_dir=self.lockstate, cache_root=self.tmp / 'cache')
        args.update(kw)
        return lt.run_trial(**args)

    def test_full_trial_parses_prices_grades_and_never_writes_secret_values(self):
        rec = self.run_trial()
        self.assertEqual(rec['returncode'], 0, 'the fake lead checks isolation invariants itself; see stderr.txt')
        self.assertEqual(rec['final_claim'], 'ACCEPTED')
        self.assertFalse(rec['timeout'])
        self.assertEqual([s['subagent_type'] for s in rec['spawns']], ['proxy-gpt-5-6-luna'])
        self.assertEqual(rec['spawns'][0]['served_models'], ['proxy-claude-native--gpt-5.6-luna'])
        self.assertEqual(rec['result']['total_cost_usd'], 9.99)
        cost = rec['cost']
        self.assertAlmostEqual(cost['lead_usd'], 1000 * 4e-6 + 100 * 2e-5)
        self.assertAlmostEqual(cost['worker_usd'], 5000 * 2e-7 + 9 * 1.2e-6)
        self.assertEqual(cost['cli_total_usd'], 9.99)
        self.assertEqual(cost['warnings'], [])
        grade = rec['grade']
        self.assertTrue(grade['oracle_pass'])
        self.assertEqual({t['id']: t['status'] for t in grade['oracle']['tests']},
                         {'oracle::test_a': 'passed', 'oracle::test_b': 'passed'})
        self.assertEqual((grade['own_tests']['passed'], grade['own_tests']['failed']), (1, 0))
        self.assertEqual(rec['git']['commits_since_base'], 1)
        self.assertTrue(rec['git']['committed_clean'])
        self.assertEqual(rec['git']['log'][0].split(' ', 1)[1], 'add solution')
        self.assertEqual(rec['isolation']['agents_copied'], ['proxy-gpt-5-6-luna'])
        self.assertEqual(rec['isolation']['env_passthrough'], sorted(SECRETS, key=list(SECRETS).index))
        out = self.tmp / 'out'
        for name in ('stream.jsonl', 'stderr.txt', 'result.json', 'state', 'transcripts', 'repo'):
            self.assertTrue((out / 'skill-1' / name).exists(), name)
        rows = [json.loads(l) for l in (out / 'trials.jsonl').read_text().splitlines()]
        self.assertEqual([(r['arm'], r['trial']) for r in rows], [('skill', 1)])
        # secret values: not in any file written under out/, and replaced by markers where they appeared
        for path in out.rglob('*'):
            if path.is_file() and '.git' not in path.parts:
                data = path.read_bytes()
                for name, value in SECRETS.items():
                    self.assertNotIn(value.encode(), data, f'{name} value leaked into {path}')
        self.assertIn('[REDACTED:TYPESAFE_API_KEY]', (out / 'skill-1' / 'stream.jsonl').read_text())
        self.assertIn('[REDACTED:TYPESAFE_API_KEY]', (out / 'skill-1' / 'stderr.txt').read_text())
        self.assertIn('[REDACTED:TYPESAFE_API_KEY]', (out / 'skill-1' / 'result.json').read_text())

    def test_answer_key_and_scratch_dirs_are_restored_and_cleaned(self):
        self.run_trial()
        self.assertEqual({p: p.stat().st_mode & 0o7777 for p in self.oracle_modes}, self.oracle_modes)
        self.assertFalse((self.lockstate / 'state.json').exists())
        self.assertEqual(list((self.tmp / 'cache' / 'run').iterdir()), [])
        self.assertEqual(list((self.tmp / 'cache' / 'private').iterdir()), [])

    def test_a_failing_oracle_is_reported_as_not_passing(self):
        shutil.rmtree(self.base / '.git' / 'hooks', ignore_errors=True)
        fake = (self.tmp / 'fake_claude.py').read_text().replace('open("solution.txt", "w").write("ok")', 'pass')
        fake = fake.replace('["git", "add", "solution.txt"]', '["git", "status"]').replace(
            '["git", "commit", "-q", "-m", "add solution"], check=True', '["git", "status"], check=True')
        (self.tmp / 'fake_claude.py').write_text(fake)
        rec = self.run_trial()
        self.assertFalse(rec['grade']['oracle_pass'])
        self.assertEqual(rec['grade']['oracle']['failed'], 1)
        self.assertEqual(rec['final_claim'], 'ACCEPTED')  # a false accept: claimed, oracle fails
        self.assertEqual(lt.summarize([rec])['skill']['false_accepts'], 1)

    def test_timeout_kills_the_whole_process_group_and_still_restores_the_key(self):
        rec = self.run_trial(arm='noskill', timeout=3)
        self.assertTrue(rec['timeout'])
        self.assertIsNone(rec['final_claim'])
        self.assertEqual(rec['result'], {})
        self.assertIsNone(rec['cost']['total_usd'])  # no usage -> null, not zero
        self.assertTrue(any('no result event' in e for e in rec['errors']))
        child = int(self.pidfile.read_text())
        time_left = 20
        while time_left and lt._pid_alive(child) and not self._is_zombie(child):
            time_left -= 1
            subprocess.run(['sleep', '0.1'])
        self.assertTrue(not lt._pid_alive(child) or self._is_zombie(child), 'grandchild survived the timeout')
        self.assertEqual({p: p.stat().st_mode & 0o7777 for p in self.oracle_modes}, self.oracle_modes)

    @staticmethod
    def _is_zombie(pid):
        try:
            return Path(f'/proc/{pid}/stat').read_text().split(') ')[1].startswith('Z')
        except OSError:
            return True

    def test_harness_errors_fail_before_launch(self):
        with self.assertRaisesRegex(lt.TrialError, 'already exists'):
            self.run_trial()
            self.run_trial()
        with self.assertRaisesRegex(lt.TrialError, 'contains'):
            self.run_trial(trial=2, locks=[self.tmp / 'out'])
        with self.assertRaisesRegex(lt.TrialError, 'does not exist'):
            self.run_trial(trial=3, locks=[self.tmp / 'nope'])
        with self.assertRaisesRegex(lt.TrialError, 'test_oracle.py'):
            self.run_trial(trial=4, oracle=self.tmp / 'claude-home')
        self.assertTrue((self.tmp / 'out' / 'skill-2' / 'error.txt').exists())


class RedactionAndCli(unittest.TestCase):
    def test_redact_replaces_every_secret_value_but_ignores_short_ones(self):
        secrets = lt.secrets_from_env({**SECRETS, 'OTHER': 'abcdefghijkl'})
        self.assertEqual(set(secrets), set(SECRETS))
        out = lt.redact(f"a {SECRETS['TYPESAFE_API_KEY']} b {SECRETS['OP_SERVICE_ACCOUNT_TOKEN']}", secrets)
        self.assertEqual(out, 'a [REDACTED:TYPESAFE_API_KEY] b [REDACTED:OP_SERVICE_ACCOUNT_TOKEN]')
        self.assertEqual(lt.secrets_from_env({'TYPESAFE_API_KEY': 'short'}), {})

    def test_cli_output_never_contains_secret_values(self):
        record = {'arm': 'skill', 'trial': 1, 'final_claim': 'ACCEPTED', 'grade': {'oracle_pass': True}, 'timeout': False,
                  'cost': {'cli_total_usd': 1, 'lead_usd': 1, 'worker_usd': 1}, 'spawns': [], 'leak_flags': [],
                  'errors': ['failure mentioning ' + SECRETS['OP_SERVICE_ACCOUNT_TOKEN']], 'paths': {'trial_dir': '/x'}}
        out = io.StringIO()
        with mock.patch.dict(os.environ, SECRETS), mock.patch.object(lt, 'run_trial', return_value=record), \
                contextlib.redirect_stdout(out):
            rc = lt.main(['run', '--arm', 'skill', '--trial', '1', '--base', '/b', '--oracle', '/o', '--out', '/out',
                          '--prompt-file', '/p', '--arm-rules', '/r', '--claude-cmd', 'echo hi'])
        self.assertEqual(rc, 0)
        for value in SECRETS.values():
            self.assertNotIn(value, out.getvalue())
        self.assertIn('[REDACTED:OP_SERVICE_ACCOUNT_TOKEN]', out.getvalue())

    def test_run_requires_all_documented_arguments(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            lt.main(['run', '--arm', 'skill'])
        args = lt.build_parser().parse_args(['run', '--arm', 'noskill', '--trial', '2', '--base', 'b', '--oracle', 'o',
                                             '--out', 'out', '--prompt-file', 'p', '--arm-rules', 'r', '--lock', 'a',
                                             '--lock', 'b'])
        self.assertEqual((args.lead_model, args.timeout, args.skill_dir), ('opus', 2700, lt.DEFAULT_SKILL_DIR))
        self.assertEqual([str(p) for p in args.lock], ['a', 'b'])
        self.assertTrue((args.skill_dir / 'scripts' / 'fdel.py').is_file())


def trial_record(arm, trial, claim, oracle, cli, lead, worker, spawns=(), fdel=0, leaks=0, timeout=False):
    return {'arm': arm, 'trial': trial, 'final_claim': claim, 'timeout': timeout, 'grade': {'oracle_pass': oracle},
            'cost': {'cli_total_usd': cli, 'lead_usd': lead, 'worker_usd': worker,
                     'total_usd': None if None in (lead, worker) else lead + worker},
            'spawns': [{'subagent_type': a, 'model': m} for a, m in spawns],
            'fdel_calls': [{'subcommand': 'route'}] * fdel + [{'subcommand': 'record'}] * (1 if fdel else 0),
            'skill_calls': [{'skill': 'fast-delegate'}] * (1 if fdel else 0), 'leak_flags': [{}] * leaks}


class Summarize(unittest.TestCase):
    def setUp(self):
        self.records = [
            trial_record('skill', 1, 'ACCEPTED', True, 1.0, 0.4, 0.2, [('proxy-gpt-5-6-luna', 'haiku'), ('general-purpose', 'sonnet')], fdel=2),
            trial_record('skill', 2, 'ACCEPTED', False, 3.0, 0.8, None, [('proxy-gpt-5-6-luna', 'haiku')], fdel=1, leaks=1),
            trial_record('skill', 3, None, False, 2.0, 0.6, 0.4, timeout=True),
            trial_record('noskill', 1, 'NOT_ACCEPTED', False, 5.0, 3.0, 1.0, [('general-purpose', 'opus')]),
            trial_record('noskill', 2, 'ACCEPTED', True, 7.0, 4.0, 2.0, [('general-purpose', 'opus'), ('general-purpose', 'opus')]),
        ]
        self.s = lt.summarize(self.records)

    def test_counts(self):
        sk, no = self.s['skill'], self.s['noskill']
        self.assertEqual((sk['n'], sk['oracle_accepted'], sk['lead_claimed_accepted'], sk['false_accepts'], sk['timeouts']),
                         (3, 1, 2, 1, 1))
        self.assertEqual((no['n'], no['oracle_accepted'], no['lead_claimed_accepted'], no['false_accepts'], no['timeouts']),
                         (2, 1, 1, 0, 0))

    def test_cost_statistics_skip_null_rather_than_counting_zero(self):
        sk = self.s['skill']
        self.assertEqual((sk['cli_total_usd']['mean'], sk['cli_total_usd']['median']), (2.0, 2.0))
        self.assertAlmostEqual(sk['lead_usd']['mean'], 0.6)
        self.assertEqual(sk['worker_usd'], {'n': 2, 'mean': 0.30000000000000004, 'median': 0.30000000000000004})
        self.assertEqual(self.s['noskill']['worker_usd']['median'], 1.5)

    def test_spawns_fdel_and_leaks(self):
        sk = self.s['skill']
        self.assertEqual(sk['spawns_by_agent_model'], {'general-purpose / sonnet': 1, 'proxy-gpt-5-6-luna / haiku': 2})
        self.assertEqual(sk['fdel_calls'], {'total': 5, 'by_subcommand': {'record': 2, 'route': 3}})
        self.assertEqual(sk['skill_calls'], 2)
        self.assertEqual(sk['leak_flags'], {'total': 1, 'trials_with_flags': 1})
        self.assertEqual(self.s['noskill']['spawns_by_agent_model'], {'general-purpose / opus': 3})

    def test_load_trials_keeps_the_latest_row_per_trial_and_cli_writes_summary(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self.records + [trial_record('skill', 1, 'NOT_ACCEPTED', False, 9.0, 1.0, 1.0)]
            Path(d, 'trials.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
            loaded = lt.load_trials(d)
            self.assertEqual(len(loaded), 5)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(lt.main(['summarize', '--out', d, '--json']), 0)
            self.assertEqual(json.loads(out.getvalue())['skill']['lead_claimed_accepted'], 1)
            self.assertTrue(Path(d, 'summary.json').exists())
            self.assertIn('== arm skill', lt.format_summary(lt.summarize(loaded)))
            with self.assertRaisesRegex(lt.TrialError, 'not found'):
                lt.load_trials(Path(d, 'missing'))


class SmokeChecks(unittest.TestCase):
    def record(self):
        return {'spawns': [{'subagent_type': 'proxy-gpt-6-luna', 'model': None}, {'subagent_type': 'general-purpose', 'model': 'haiku'}],
                'cost': {'rows': [{'model_key': 'claude-haiku-4-5-20251001', 'tokens': tok(1, 2, 3, 4), 'usd': 0.1},
                                  {'model_key': 'proxy-claude-native--gpt-6-luna', 'tokens': tok(10), 'usd': 0.01}]},
                'isolation': {'agents_copied': ['proxy-a'], 'agents_after_run': ['proxy-b'], 'discover_pre': ['haiku', 'proxy-a'],
                              'discover_post': ['haiku', 'proxy-b']},
                'final_claim': 'ACCEPTED', 'result': {'total_cost_usd': 0.3}}

    def test_all_pass_on_a_good_record(self):
        checks = lt.smoke_checks(self.record())
        self.assertTrue(all(checks.values()), checks)

    def test_each_failure_mode_is_detected(self):
        r = self.record()
        r['cost']['rows'][1]['usd'] = None  # unpriced proxy row
        r['isolation']['discover_post'] = ['haiku', 'proxy-b', 'proxy-self']  # discovery read ~/.claude/agents
        r['spawns'] = r['spawns'][:1]
        r['result'] = {'total_cost_usd': 0}
        checks = lt.smoke_checks(r)
        for name in ('proxy_model_has_tokens_and_usd', 'discover_post_lists_isolated_proxy_agents', 'discover_excludes_proxy_self',
                     'spawn_haiku', 'cli_cost_nonzero'):
            self.assertFalse(checks[name], name)
        self.assertTrue(checks['spawn_proxy_luna'])

    def test_smoke_refuses_to_wipe_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'important.txt').write_text('keep')
            with self.assertRaisesRegex(lt.TrialError, 'refusing to wipe'):
                lt.run_smoke(d)
            self.assertTrue(Path(d, 'important.txt').exists())


if __name__ == '__main__':
    unittest.main()
