"""Offline fixtures only: no production native-directory enumeration or deletion."""
import concurrent.futures
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen
from http.server import ThreadingHTTPServer

import render
import catalog_index as ci
import server

BACKUP = Path(__file__).parent/'tests/fixtures/legacy_render.py'


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(render, 'ROOT', self.root)
        self.root_patch.start()
        self.regional = render.DOWNLOAD_DIRECTORY
        self.legacy = render.CHINA_DIRECTORY
        self.run = '2026101000'
        self.manifest(self.run, 'preparing', [1, 2, 25], 360)
        self.fields(self.regional, self.run, [1, 2, 3, 25])
        folder = self.root / self.regional / self.run
        for lead in (1, 25):
            (folder / f'f{lead:03}_station_t.npz').touch()
            (folder / f'f{lead:03}_station_td.npz').touch()
        # Uncommitted lead 3, incomplete lead 2, precedence over legacy.
        (folder / 'f002_surface_td.npz').unlink()
        self.fields(self.legacy, self.run, [1, 2, 3, 4])
        self.fields(self.legacy, '2026100918', [1, 2])
        self.manifest('2026100900', 'complete', [1, 2], 2)
        self.fields(self.regional, '2026100900', [1, 2])
        # A download folder without a valid committed manifest is not exposed.
        self.fields(self.regional, '2026100818', [1])
        self.manifest('2026100812', 'preparing', [1], 360)
        self.fields(self.regional, '2026100812', [1])
        (self.root / self.regional / '2026100812' / 'f001_surface_msl.npz').unlink()
        self.payload = render._scan_catalog()
        self.path = self.root / 'local' / 'index.json'

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def fields(self, directory, run, leads):
        folder = self.root / directory / run
        folder.mkdir(parents=True, exist_ok=True)
        for lead in leads:
            for key in ('rain1h', 'surface_t', 'surface_td', 'surface_u', 'surface_v', 'surface_msl'):
                (folder / f'f{lead:03}_{key}.npz').touch()

    def manifest(self, run, status, leads, horizon):
        content = {'status': status, 'run': render.parse_run(run).strftime('%Y-%m-%dT%H:%M:%SZ'),
                   'bounds': list(render.DOWNLOAD_BOUNDS), 'native_cache_directory': self.regional,
                   'frames': [{'lead': lead} for lead in leads], 'forecast_horizon_hours': horizon}
        (self.root / f'run_{run}.json').write_text(json.dumps(content))

    def index(self):
        result = ci.CatalogIndex(self.path, scanner=lambda: copy.deepcopy(self.payload))
        self.assertTrue(result.refresh())
        return result

    def test_original_correctness_and_precedence(self):
        spec = importlib.util.spec_from_file_location('original_render', BACKUP)
        original = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(original)
        original.ROOT = self.root
        current = render.available_runs()
        historical = original.available_runs()
        self.assertEqual(len(current), len(historical))
        # Preserve the historical catalog contract while checking fields added
        # for optional products and 100 m winds against this fixture separately.
        self.assertEqual([{key: item[key] for key in old}
                          for item, old in zip(current, historical)], historical)
        for item in current:
            self.assertEqual(item['wind100_leads'], [])
            self.assertTrue(item['optional_product_leads'])
            self.assertTrue(all(leads == [] for leads in item['optional_product_leads'].values()))
        runs = self.payload['runs']
        self.assertEqual([r['id'] for r in runs], [self.run, '2026100918', '2026100900'])
        first = runs[0]
        self.assertEqual(first['leads'], [1, 25])
        self.assertEqual(first['bounds'], list(render.DOWNLOAD_BOUNDS))
        self.assertEqual(first['station_leads'], {'t2m': [1, 25], 'td2m': [1, 25], 'rh2m': [1, 25], 'dt24': [25]})
        self.assertEqual(first['status'], 'partial')
        self.assertEqual(first['forecast_horizon_hours'], 360)
        self.assertEqual(runs[1]['status'], 'complete')
        self.assertEqual(runs[1]['forecast_horizon_hours'], 2)
        self.assertEqual(runs[2]['status'], 'complete')
        self.assertEqual(first['regions'], render._covered_regions(render.DOWNLOAD_BOUNDS))

    def test_failed_empty_refresh_preserves_disk_and_memory(self):
        index = self.index()
        before = self.path.read_bytes()
        index.scanner = lambda: (_ for _ in ()).throw(OSError('Drive unavailable'))
        self.assertFalse(index.refresh())
        self.assertEqual(index.snapshot(), self.payload)
        index.scanner = lambda: {**self.payload, 'runs': []}
        self.assertFalse(index.refresh())
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(ci.CatalogIndex(self.path).snapshot(), self.payload)
        with patch('catalog_index.subprocess.run', side_effect=__import__('subprocess').TimeoutExpired('scan', 900)):
            index.scanner = index.scan_child
            self.assertFalse(index.refresh())
        self.assertEqual(before, self.path.read_bytes())

    def test_version_integrity_root_and_stale(self):
        index = self.index()
        original = json.loads(self.path.read_text())
        for field, value in [('version', 99), ('sha256', 'broken'), ('root', '/different-root')]:
            envelope = {**original, field: value}
            self.path.write_text(json.dumps(envelope))
            self.assertEqual(ci.CatalogIndex(self.path).snapshot()['catalog_status'], 'preparing')
        original['updated_at'] = time.time() - 86400
        self.path.write_text(json.dumps(original))
        self.assertEqual(ci.CatalogIndex(self.path).snapshot(), self.payload)

    def test_refresh_new_committed_hour(self):
        index = self.index()
        self.fields(self.regional, self.run, [2, 3])
        self.manifest(self.run, 'preparing', [1, 2, 3, 25], 360)
        index.scanner = render._scan_catalog
        self.assertTrue(index.refresh())
        self.assertEqual(index.snapshot()['runs'][0]['leads'], [1, 2, 3, 25])

    def test_http_fast_while_scan_blocked_single_flight(self):
        index = self.index()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def blocked():
            calls.append(1)
            entered.set()
            release.wait(10)
            return self.payload
        index.scanner = blocked
        index.start()
        index.start()
        self.assertTrue(entered.wait(2))
        self.assertFalse(index.refresh())
        other = ci.CatalogIndex(self.path, scanner=lambda: self.fail('cross-process lock not held'))
        self.assertFalse(other.refresh())
        class FixtureHTTPServer(ThreadingHTTPServer):
            # Eight simultaneous clients must not wait on the default five-slot
            # TCP listen queue; this test measures response work, not retries.
            request_queue_size = 32
        http = FixtureHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(ci, '_singleton', index), patch.object(render, 'available_runs', side_effect=AssertionError('HTTP scanned disk')), patch.object(render, 'prune_cache', side_effect=AssertionError('HTTP pruned images')):
                def request(_):
                    start = time.monotonic()
                    with urlopen(f'http://127.0.0.1:{http.server_port}/api/catalog', timeout=2) as reply:
                        self.assertEqual(json.load(reply), self.payload)
                    return time.monotonic() - start
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    timings = list(pool.map(request, range(24)))
                self.assertLess(max(timings), 1)
                self.assertEqual(len(calls), 1)
                print('24 HTTP requests with blocked scanner: max %.4fs' % max(timings))
        finally:
            index.stop.set()
            release.set()
            index.thread.join(3)
            http.shutdown()
            http.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
