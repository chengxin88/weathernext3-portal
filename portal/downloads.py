"""Read-only downloads of committed native fields. No upstream requests."""
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlencode
import zipfile

import render
from weathernext3_native_cache import VARIABLES, CORE_KEYS, DOWNLOAD_DIRECTORY, CHINA_DIRECTORY, field_grid

LABELS = {
    'surface_u': '10米风 U', 'surface_v': '10米风 V',
    'surface_t': '2米气温', 'surface_td': '2米露点',
    'surface_msl': '海平面气压', 'rain1h': '常规1小时降水',
    'surface_u100': '100米风 U', 'surface_v100': '100米风 V',
    'station_t': '气象站训练气温', 'station_td': '气象站训练露点',
    'imerg_rain1h': 'IMERG 1小时降水', 'experimental_rain1h': 'PARDIG 1小时降水',
    'cloud_total': '总云量', 'cloud_high': '高云量',
    'cloud_medium': '中云量', 'cloud_low': '低云量',
    'solar_downwards': '向下短波辐射 GHI', 'solar_direct': '直接太阳辐射 FDIR',
}
MAX_FILE_BYTES = 64 * 1024**2
MAX_BUNDLE_BYTES = 3 * 1024**3
BUNDLE_SLOTS = threading.BoundedSemaphore(2)
CHUNK = 256 * 1024


def run_info(value):
    render.parse_run(value)
    info = next((r for r in render.catalog()['runs'] if r['id'] == value), None)
    if not info:
        raise FileNotFoundError('起报时间尚未发布')
    return info


def field_leads(info, key):
    if key in CORE_KEYS:
        return info['leads']
    if key in ('surface_u100', 'surface_v100'):
        return info['wind100_leads']
    if key in ('station_t', 'station_td'):
        return info['station_leads']['t2m' if key == 'station_t' else 'td2m']
    product = {'imerg_rain1h': 'imerg_rain1', 'experimental_rain1h': 'experimental_rain1'}.get(key, key)
    return info['optional_product_leads'].get(product, [])


def field_catalog():
    catalog = render.catalog()
    return {
        'fields': [{'id': key, 'label': LABELS[key], 'unit': VARIABLES[key][1],
                    'grid_degrees': field_grid(key)} for key in VARIABLES],
        'runs': [{**{k: r[k] for k in ('id', 'run_utc', 'run_bjt', 'status', 'bounds', 'leads', 'forecast_horizon_hours')},
                  'field_leads': {key: field_leads(r, key) for key in VARIABLES}} for r in catalog['runs']],
    }


def file_record(info, lead, key):
    if key not in VARIABLES or type(lead) is not int or not 1 <= lead <= 360:
        raise ValueError('变量或时效无效')
    if lead not in field_leads(info, key):
        raise FileNotFoundError('该变量或时效尚未发布')
    directory = DOWNLOAD_DIRECTORY if info['bounds'] == list(render.DOWNLOAD_BOUNDS) else CHINA_DIRECTORY
    root = (render.ROOT / directory).resolve()
    name = f'f{lead:03}_{key}.npz'
    path = (root / info['id'] / name).resolve()
    if not path.is_relative_to(root / info['id']) or not path.is_file():
        raise FileNotFoundError('缓存文件不可用')
    stat = path.stat()
    if not 0 < stat.st_size <= MAX_FILE_BYTES:
        raise ValueError('缓存文件大小无效')
    return {'name': name, 'lead': lead, 'field': key, 'bytes': stat.st_size,
            'mtime_ns': stat.st_mtime_ns, 'path': path,
            'url': 'file?' + urlencode({'run': info['id'], 'lead': lead, 'field': key})}


def selection(query):
    info = run_info(query.get('run', ''))
    try:
        start, end = int(query.get('start', '1')), int(query.get('end', str(max(info['leads']))))
    except (ValueError, TypeError):
        raise ValueError('时效必须为整数') from None
    if not 1 <= start <= end <= 360:
        raise ValueError('请选择 1–360 小时以内的连续时效')
    keys = query.get('fields', ','.join(VARIABLES)).split(',')
    if not keys or len(keys) > len(VARIABLES) or len(keys) != len(set(keys)) or any(k not in VARIABLES for k in keys):
        raise ValueError('请选择有效变量')
    records, missing = [], []
    for key in keys:
        available = set(field_leads(info, key))
        absent = []
        for lead in range(start, end + 1):
            if lead not in available:
                absent.append(lead)
                continue
            # Catalog entries refer only to committed fields, never temporary files.
            records.append(file_record(info, lead, key))
        if absent:
            missing.append({'field': key, 'leads': absent})
    if not records:
        raise FileNotFoundError('所选范围暂无已发布数据')
    records.sort(key=lambda f: (f['lead'], f['field']))
    total = sum(f['bytes'] for f in records)
    if total > MAX_BUNDLE_BYTES:
        raise ValueError('单次打包上限为 3 GiB，请缩小时效或变量范围')
    manifest = {
        'format': 'weathernext3-native-npz-v1', 'run_id': info['id'],
        'run_utc': info['run_utc'], 'run_bjt': info['run_bjt'], 'status': info['status'],
        'bounds': info['bounds'], 'statistic': 'mean', 'start': start, 'end': end,
        'fields': [{'id': k, 'label': LABELS[k], 'unit': VARIABLES[k][1],
                    'grid_degrees': field_grid(k)} for k in keys],
        'file_count': len(records), 'total_bytes': total, 'missing': missing,
        'files': [{k: f[k] for k in ('name', 'lead', 'field', 'bytes', 'url')} for f in records],
    }
    # Pin partial-run selections so a later ZIP contains exactly the previewed files.
    fingerprint = hashlib.sha256(json.dumps([
        [f['name'], f['bytes'], f['mtime_ns']] for f in records
    ], separators=(',', ':')).encode()).hexdigest()
    manifest['snapshot'] = fingerprint
    if query.get('snapshot') and query['snapshot'] != fingerprint:
        raise ValueError('资料目录已更新，请刷新下载清单后重试')
    return manifest, records


README = '''WeatherNext 3 local native data

These NPZ files are the cached regional ensemble MEAN, not all ensemble members
or global raw output. No interpolation or new upstream download is performed.
Coverage and selected fields/units are in manifest.json. Missing forecast hours
are listed explicitly there; the ZIP contains only published available files.
Run IDs and valid times in NPZ metadata are UTC; run_bjt is UTC+8.
Each rain1h field is precipitation during the hour ending at its valid time.
Wind U/V are vector components, not speed. Solar fields are 1-hour average W/m2.
Air temperatures are degC, pressure hPa, precipitation mm, cloud cover percent.
station_t/station_td use the 0.05 degree grid; other fields use 0.1 degree.

Python example (requires numpy):
    import json, numpy as np
    with np.load("data/f001_surface_t.npz", allow_pickle=False) as data:
        lon, lat, values = data["lon"], data["lat"], data["values"]
        metadata = json.loads(str(data["meta"].item()))
        print(values.shape, metadata["valid"], metadata["unit"])
    # values[latitude_index, longitude_index]; coordinates are in degrees.

Single NPZ URLs support HTTP Range requests for resuming downloads.
ZIP bundles are streamed; if interrupted, retry or download individual files.
Keep the source computer, WSL and the website tunnel running during downloads.
'''.encode('utf-8')


def open_snapshot(record):
    stream = open(record['path'], 'rb')
    stat = os.fstat(stream.fileno())
    if stat.st_size != record['bytes'] or stat.st_mtime_ns != record['mtime_ns']:
        stream.close()
        raise ValueError('缓存已更新，请重新获取下载清单')
    return stream


def download_file(handler, query, head=False):
    try:
        lead = int(query.get('lead', ''))
    except ValueError:
        raise ValueError('时效必须为整数') from None
    info = run_info(query.get('run', ''))
    record = file_record(info, lead, query.get('field', ''))
    size = record['bytes']
    etag = f'"{info["id"]}-{record["name"]}-{size:x}-{record["mtime_ns"]:x}"'
    with open_snapshot(record) as stream:
        start, end, status = 0, size - 1, 200
        byte_range = None if head else handler.headers.get('Range')
        if handler.headers.get('If-Range', etag) != etag:
            byte_range = None
        if byte_range:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', byte_range)
            if not match or not any(match.groups()):
                start = size
            else:
                first, last = match.groups()
                if first:
                    start, end = int(first), min(int(last), end) if last else end
                elif int(last) > 0:
                    start = max(0, size - int(last))
                else:
                    start = size
            if start >= size or end < start:
                handler.send_response(416)
                handler.send_header('Content-Range', f'bytes */{size}')
                handler.send_header('Content-Length', '0')
                handler.send_header('Cache-Control', 'private, no-store')
                handler.end_headers()
                return
            status = 206
        if handler.headers.get('If-None-Match') == etag:
            handler.send_response(304)
            handler.send_header('ETag', etag)
            handler.send_header('Cache-Control', 'private, no-store')
            handler.end_headers()
            return
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/octet-stream')
        handler.send_header('Content-Disposition', f'attachment; filename="wn3_{info["id"]}_{record["name"]}"')
        handler.send_header('Content-Length', str(end - start + 1))
        handler.send_header('Cache-Control', 'private, no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('ETag', etag)
        if status == 206:
            handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        handler.end_headers()
        if head:
            return
        stream.seek(start)
        remaining = end - start + 1
        while remaining:
            content = stream.read(min(CHUNK, remaining))
            if not content:
                handler.close_connection = True
                return
            handler.wfile.write(content)
            remaining -= len(content)


def download_bundle(handler, query, head=False):
    if not head and not BUNDLE_SLOTS.acquire(blocking=False):
        handler.reply(503, '目前有两个打包下载正在传输，请稍后重试。'.encode(), 'text/plain; charset=utf-8')
        return
    try:
        manifest, records = selection(query)
        filename = f'wn3_{manifest["run_id"]}_f{manifest["start"]:03}-{manifest["end"]:03}.zip'
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/zip')
        handler.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        handler.send_header('Cache-Control', 'private, no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Accept-Ranges', 'none')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        handler.close_connection = True
        if head:
            return
        # zipfile uses data descriptors on this non-seekable output. Memory stays
        # bounded and compressed NPZ files are copied without recompression.
        try:
            with zipfile.ZipFile(handler.wfile, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
                bundle.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode())
                bundle.writestr('README.txt', README)
                for record in records:
                    entry = zipfile.ZipInfo('data/' + record['name'])
                    entry.file_size = record['bytes']
                    entry.compress_type = zipfile.ZIP_STORED
                    with open_snapshot(record) as source, bundle.open(entry, 'w') as target:
                        remaining = record['bytes']
                        while remaining:
                            content = source.read(min(CHUNK, remaining))
                            if not content:
                                raise OSError('Native file changed during transfer')
                            target.write(content)
                            remaining -= len(content)
        except (OSError, ValueError):
            # Never append a JSON error to a partially transferred ZIP.
            handler.close_connection = True
    finally:
        if not head:
            BUNDLE_SLOTS.release()
