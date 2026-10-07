"""Provider quota sources stay isolated and never manufacture availability."""
import json
import unittest
import test_quota


class ProviderQuotaTests(unittest.TestCase):
    # Reuse isolated fixture helpers without importing/repeating its test suite.
    setUp = test_quota.QuotaTests.setUp
    tearDown = test_quota.QuotaTests.tearDown
    load = test_quota.QuotaTests.load
    write_catalog = test_quota.QuotaTests.write_catalog


    def test_dispatch_and_unknown(self):
        f = self.load()
        calls = []
        def reader(pool, now):
            calls.append(pool)
            return {'observed_at': now, 'five_hour': {'used_percentage': 30}, 'seven_day': None}
        adapters = {name: cls(reader) for name, cls in (
            ('claude', f.ClaudeQuotaAdapter), ('codex', f.CodexQuotaAdapter),
            ('cursor', f.CursorQuotaAdapter), ('proxy', f.ProxyQuotaAdapter))}
        pools = ['claude', 'codex', 'cursor', 'proxy:nvidia']
        snaps, _ = f.gather_quota_snapshots(pools, [], now=1000, adapters=adapters)
        self.assertEqual(calls, pools)
        self.assertEqual([snaps[p]['source'] for p in pools],
                         ['claude-statusline', 'codex-rollout', 'cursor-snapshot', 'proxy-cache'])
        unavailable = f.quota_evidence('cursor', None, f.default_quota_thresholds({}), 1000)
        self.assertEqual(unavailable['availability'], 'unavailable')
        self.assertFalse(unavailable['live'])
        self.assertIn('admin spend', unavailable['reason'])
        report = f.CursorQuotaAdapter(lambda p, n: None).report('cursor', 1000)
        self.assertEqual(report['availability'], 'unavailable')
        self.assertIsNone(report['snapshot'])
        self.assertEqual(report['source'], 'cursor-snapshot')

    def test_runtime_pool_isolation(self):
        f = self.load()
        self.assertIsNone(f.resolve_pool({}, 'spawnable', 'codex', 'google-antigravity/model'))
        self.assertIsNone(f.resolve_pool({}, 'spawnable', 'codex', 'nvidia/model'))
        self.assertEqual(f.resolve_pool({}, 'spawnable', 'codex', 'native-model'), 'codex')
        self.assertIsNone(f.resolve_pool({'runtime': 'proxy'}, 'spawnable', 'codex'))
        self.assertEqual(f.resolve_pool({'runtime': 'proxy', 'provider': 'nvidia'}, 'spawnable', 'codex'), 'proxy:nvidia')
        self.assertEqual(f.resolve_pool({'provider': 'cursor'}, 'spawnable', 'codex'), 'cursor')
        self.assertEqual(f.resolve_pool({'pool': 'custom', 'runtime': 'proxy'}, 'spawnable', 'codex'), 'custom')
        billing = f.resolve_billing('worker', {'runtime': 'proxy', 'provider': 'nvidia'})
        self.assertEqual(f.resolve_pool(billing, 'spawnable', 'codex'), 'proxy:nvidia')

    def test_proxy_cache_units_and_identity(self):
        f = self.load()
        root = self.tmp / 'proxy'
        root.mkdir()
        row = {'updatedAt': 900000, 'weeklyPercent': 23, 'weeklyResetAt': 2000, 'resetCredits': 99}
        path = root / 'codex-quota-cache.json'
        path.write_text(json.dumps({'version': 1, 'quotas': {'private-account': row}}))
        adapter = f.ProxyQuotaAdapter(root=root)
        snap = adapter.read('proxy:openai', 1000)
        self.assertEqual(snap['observed_at'], 900)
        self.assertEqual(snap['seven_day']['used_percentage'], 23)
        self.assertIsNone(snap['five_hour'])
        self.assertNotIn('private-account', json.dumps(snap))
        self.assertIsNone(adapter.read('proxy:nvidia', 1000))
        self.assertIsNone(adapter.read('proxy', 1000))
        path.write_text(json.dumps({'version': 1, 'quotas': {'a': row, 'b': row}}))
        self.assertIsNone(adapter.read('proxy:openai', 1000))

    def test_provider_rows_and_staleness(self):
        f = self.load()
        root = self.tmp / 'proxy'
        root.mkdir()
        row = {'updatedAt': 100000, 'fiveHourPercent': 80, 'fiveHourResetAt': 1000000}
        (root / 'provider-account-quota-cache.json').write_text(json.dumps(
            {'version': 1, 'rows': {'nvidia\0private': row, 'anthropic\0other': {**row, 'fiveHourPercent': 99}}}))
        snap = f.ProxyQuotaAdapter(root=root).read('proxy:nvidia', 10000)
        self.assertEqual(snap['five_hour']['used_percentage'], 80)
        evidence = f.quota_evidence('proxy:nvidia', snap, f.default_quota_thresholds({}), 10000)
        self.assertEqual(evidence['freshness'], 'stale')
        self.assertEqual(evidence['observed_at'], 100)
        self.assertNotIn('private', json.dumps(evidence))

    def test_manual_override_and_source_timestamp(self):
        f = self.load()
        snaps, _ = f.gather_quota_snapshots(['cursor'], ['cursor=5h:12'], now=1000,
                                         adapters={'cursor': f.CursorQuotaAdapter(lambda p, n: None)})
        self.assertEqual(snaps['cursor']['availability'], 'manual')
        home = self.tmp / 'rollouts'
        folder = home / 'sessions'
        folder.mkdir(parents=True)
        (folder / 'rollout-1.jsonl').write_text(json.dumps({'timestamp': '2026-01-01T00:00:00Z',
            'payload': {'rate_limits': {'primary': {'used_percent': 15}}}}) + '\n')
        snap = f.read_codex_quota(home, now=2000000000)
        self.assertEqual(snap['observed_at'], f._parse_epoch('2026-01-01T00:00:00Z'))
        self.assertIsNone(f.load_quota_file('../outside'))
        self.assertIsNone(f.normalize_window({'resets_at': 1}, 1000, 300))

    def test_discovery_does_not_assign_codex_to_qualified_worker(self):
        self.write_catalog({'google-antigravity/model': test_quota.entry(1, 2)})
        f = self.load(test_quota.make_harness([]))
        candidates, _ = f.discover(harness_name='codex', spawnable=['google-antigravity/model'])
        self.assertEqual(len(candidates), 1)
        self.assertNotIn('pool', candidates[0]['billing'])

    def test_empty_malformed_unsupported_and_future_are_unknown(self):
        f = self.load()
        root = self.tmp / 'proxy'
        root.mkdir()
        path = root / 'provider-account-quota-cache.json'
        adapter = f.ProxyQuotaAdapter(root=root)
        for raw in ([], {'version': 2, 'rows': {}}, {'version': 1, 'rows': {}},
                    {'version': 1, 'rows': {'nvidia\0opaque': {'updatedAt': 2000000, 'weeklyPercent': 15}}},
                    {'version': 1, 'rows': {'nvidia\0opaque': {'updatedAt': 900000, 'monthlyPercent': 15}}}):
            path.write_text(json.dumps(raw))
            report = adapter.report('proxy:nvidia', 1000)
            self.assertEqual(report['availability'], 'unavailable')
            self.assertIsNone(report['snapshot'])
        path.write_text('invalid-json')
        self.assertIsNone(adapter.read('proxy:nvidia', 1000))

    def test_route_reports_unavailable_cursor(self):
        h = test_quota.make_harness([
            test_quota.builtin('lead', 'model-lead'),
            test_quota.builtin('worker', 'model-worker', {'pool': 'cursor'})])
        self.write_catalog({'model-worker': test_quota.entry(1, 2)})
        f = self.load(h)
        result = f.route(test_quota.task(), harness='claude')
        report = result['quota_sources']['cursor']
        self.assertEqual(report['availability'], 'unavailable')
        self.assertEqual(report['freshness'], 'unknown')
        self.assertFalse(report['live'])
