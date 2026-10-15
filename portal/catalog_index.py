"""Local last-good catalog. Only a bounded child process touches native directories."""
import copy
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

VERSION = 2
INDEX_PATH = Path(os.environ.get('WN3_CATALOG_INDEX', str(Path.home()/'.local/state/wn3-portal/catalog-v1.json')))
INTERVAL = 60
SCAN_TIMEOUT = 900
LOG = logging.getLogger(__name__)


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def validate(payload):
    import render
    if not isinstance(payload, dict) or payload.get('model') != render.MODEL_LABEL:
        raise ValueError('Invalid catalog model')
    if payload.get('products') != render.PRODUCTS or payload.get('regions') != {k: v[0] for k, v in render.REGIONS.items()} or payload.get('map_version') != render.STYLE_VERSION:
        raise ValueError('Incompatible catalog schema')
    runs = payload.get('runs')
    if not isinstance(runs, list) or not runs:
        raise ValueError('Empty catalog cannot replace last-good data')
    ids = []
    for run in runs:
        parsed = render.parse_run(run['id'])
        ids.append(run['id'])
        leads = run['leads']
        if not leads or any(type(n) is not int or not 0 <= n <= 999 for n in leads) or leads != sorted(set(leads)):
            raise ValueError('Invalid leads')
        if run['status'] not in ('partial', 'complete') or type(run['forecast_horizon_hours']) is not int or run['forecast_horizon_hours'] < max(leads):
            raise ValueError('Invalid horizon/status')
        if run['bounds'] not in [list(source[2]) for source in render.CACHE_SOURCES] or run['regions'] != render._covered_regions(run['bounds']):
            raise ValueError('Invalid coverage')
        optional=run['optional_product_leads']
        expected=set(render.PRODUCTS)-{'rain1','rain3','rain6','rain12','rain24',
            't2m','td2m','rh2m','dt24','wind10','wind100','mslp'}
        if set(optional)!=expected:raise ValueError('Invalid optional product identity')
        for values in optional.values():
            if not isinstance(values,list) or any(type(n)!=int for n in values) or values!=sorted(set(values)) or not set(values)<=set(leads):
                raise ValueError('Invalid optional product leads')
        for product,(_,hours,_) in render.RAIN_PRODUCTS.items():
            if product.startswith('rain'):continue
            prefix=product.rsplit('rain',1)[0];hourly=set(optional[prefix+'rain1'])
            if optional[product]!=[h for h in sorted(hourly) if h>=hours and all(n in hourly for n in range(h-hours+1,h+1))]:
                raise ValueError('Invalid optional rain accumulation window')
        stations = run['station_leads']
        wind100 = run['wind100_leads']
        if (not isinstance(wind100, list)
                or any(type(n) is not int for n in wind100)
                or wind100 != sorted(set(wind100))
                or not set(wind100) <= set(leads)):
            raise ValueError('Invalid 100m wind leads')
        for key in ('t2m', 'td2m', 'rh2m', 'dt24'):
            values = stations[key]
            if values != sorted(set(values)) or not set(values) <= set(leads):
                raise ValueError('Invalid station leads')
        if stations['rh2m'] != sorted(set(stations['t2m']) & set(stations['td2m'])) or stations['dt24'] != [n for n in stations['t2m'] if n > 24 and n - 24 in stations['t2m']]:
            raise ValueError('Invalid derived station leads')
        if run['run_utc'] != parsed.strftime('%Y-%m-%dT%H:%MZ') or run['run_bjt'] != (parsed + render.BJT).strftime('%Y-%m-%d %H:%M'):
            raise ValueError('Invalid run timestamps')
    if ids != sorted(set(ids), reverse=True) or not isinstance(payload.get('backfill_queue'), list):
        raise ValueError('Invalid runs/queue')
    for queued in payload['backfill_queue']:
        parsed = render.parse_run(queued['id'])
        if not isinstance(queued['status'], str) or queued['run_bjt'] != (parsed + render.BJT).strftime('%Y-%m-%d %H:%M'):
            raise ValueError('Invalid queue')
    return payload


class CatalogIndex:
    def __init__(self, path=INDEX_PATH, scanner=None, interval=INTERVAL):
        self.path = Path(path)
        self.scanner = scanner or self.scan_child
        self.interval = interval
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.payload = None
        self.updated_at = 0
        self.thread = None
        self.stop = threading.Event()
        self.load()

    def load(self):
        import render
        try:
            envelope = json.loads(self.path.read_text())
            if envelope['version'] != VERSION or envelope['root'] != str(render.ROOT) or envelope['sha256'] != digest(envelope['catalog']):
                raise ValueError('Invalid index version/root/checksum')
            updated = envelope['updated_at']
            if not isinstance(updated, (int, float)) or not 0 < updated <= time.time() + 60:
                raise ValueError('Invalid index timestamp')
            self.payload = validate(envelope['catalog'])
            self.updated_at = updated
            if time.time() - updated > 3600:
                LOG.warning('Serving stale last-good WN3 index until refresh succeeds')
        except (OSError, ValueError, KeyError, TypeError) as error:
            LOG.warning('WN3 local index unavailable: %s', error)

    def scan_child(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='scan-', suffix='.json', dir=self.path.parent)
        os.close(fd)
        try:
            subprocess.run([sys.executable, str(Path(__file__).with_name('catalog_index.py')), '--scan', name], check=True, timeout=SCAN_TIMEOUT, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            return json.loads(Path(name).read_text())
        finally:
            Path(name).unlink(missing_ok=True)

    def persist(self, payload, updated):
        import render
        envelope = {'version': VERSION, 'root': str(render.ROOT), 'updated_at': updated, 'sha256': digest(payload), 'catalog': payload}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.catalog-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(envelope, stream, ensure_ascii=True, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    def refresh(self):
        if not self.refresh_lock.acquire(blocking=False):
            return False
        started = time.monotonic()
        lease = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lease = open(str(self.path) + '.lock', 'a')
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            payload = validate(self.scanner())
            updated = time.time()
            self.persist(payload, updated)
            with self.lock:
                self.payload, self.updated_at = payload, updated
            LOG.warning('WN3 index refreshed: %s runs in %.3fs', len(payload['runs']), time.monotonic() - started)
            return True
        except Exception:
            LOG.exception('WN3 refresh failed; preserving last-good catalog')
            return False
        finally:
            if lease is not None:
                lease.close()
            self.refresh_lock.release()

    def start(self):
        with self.lock:
            if self.thread is not None:
                return
            def worker():
                while not self.stop.is_set():
                    self.refresh()
                    if self.stop.wait(self.interval):
                        break
            self.thread = threading.Thread(target=worker, name='wn3-catalog-refresh', daemon=True)
            self.thread.start()

    def snapshot(self):
        import render
        with self.lock:
            if self.payload is not None:
                return copy.deepcopy(self.payload)
        return {'model': render.MODEL_LABEL, 'runs': [], 'backfill_queue': [], 'map_version': render.STYLE_VERSION,
                'regions': {k: v[0] for k, v in render.REGIONS.items()}, 'products': render.PRODUCTS,
                'catalog_status': 'preparing', 'message': 'Local catalog index is preparing; please retry.'}


_singleton = None
_singleton_lock = threading.Lock()


def get_catalog():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = CatalogIndex()
            _singleton.start()
    return _singleton.snapshot()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--scan':
        import render
        Path(sys.argv[2]).write_text(json.dumps(render._scan_catalog(), ensure_ascii=True))
    elif sys.argv[1:] == ['--seed']:
        index = CatalogIndex()
        if not index.refresh():
            sys.exit(1)
        print(json.dumps({'index': str(index.path), 'runs': len(index.payload['runs']), 'latest': index.payload['runs'][0]}, ensure_ascii=True))
    else:
        sys.exit('Use --seed for bounded offline indexing')
