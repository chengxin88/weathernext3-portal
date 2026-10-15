"""Read-only catalog and downloads for the Xinjiang wind project."""
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import zipfile

import downloads
from wind_project_worker import ROOT, BJT, UTC, days_for, expected_run_hours, README
from wind_project_15min import README as README_15MIN

SLOTS = threading.BoundedSemaphore(2)
FILES = {'zip': 'forecasts.zip', 'native': 'native-grid.npz', 'manifest': 'manifest.json'}
FILES_15MIN = {'zip': 'forecasts-15min.zip', 'manifest': 'manifest.json'}


def variant(interval):
    if interval == 'hourly':
        return ROOT, FILES, README
    if interval == '15min':
        return ROOT/'15min', FILES_15MIN, README_15MIN
    raise ValueError('时间分辨率须为hourly或15min')


def catalog(interval='hourly'):
    product_root, files, _ = variant(interval)
    config = json.loads((ROOT/'project.json').read_text())
    state_path = product_root/'status.json'
    state = json.loads(state_path.read_text()) if state_path.is_file() else {'status': 'queued', 'days': {}}
    days = []
    for day in days_for(config):
        record = state.get('days', {}).get(day, {'status': 'queued'})
        item = {'day': day, 'status': record['status'], 'expected_runs': 24,
                'run_count': record.get('run_count', 0), 'rows': record.get('rows', 0),
                'missing_rows': record.get('missing_rows', 0)}
        if record['status'] == 'complete':
            item['files'] = {kind: {'bytes': record['files'][name]['bytes'],
                                    'sha256': record['files'][name]['sha256'],
                                    'url': f'file?day={day}&kind={kind}&interval={interval}'} for kind, name in files.items()}
        elif record['status'] == 'error':
            item['message'] = '该日资料尚未处理完成，需重试。' if interval == '15min' else '源端资料未取齐，需重试；尚未发布该日文件。'
        days.append(item)
    complete = [d for d in days if d['status'] == 'complete']
    expected_rows = len(days)*2400*len(config['points'])
    if interval == '15min':
        obs_end = datetime.fromisoformat(config['observation_end_bjt']).replace(tzinfo=BJT).astimezone(UTC)
        expected_rows = sum(max(0, min(horizon*4, int((obs_end-run).total_seconds()//900))-3)
                            for day in days for run, horizon in expected_run_hours(day['day']))*len(config['points'])
    return {'schema': 1, 'id': config['id'], 'title': config['title'], 'site': config['site'],
            'height_m': 100, 'grid_degrees': .1, 'statistic': 'mean',
            'interval': interval, 'interval_minutes': 15 if interval == '15min' else 60,
            'temporal_method': 'linear U/V, no extrapolation or crossing missing endpoints' if interval == '15min' else 'native hourly',
            'source_collection': 'projects/gcp-public-data-weathernext/assets/weathernext_3_0_0_0p1deg',
            'start_bjt': config['observation_start_bjt'], 'end_bjt': config['observation_end_bjt'],
            'points': config['points'], 'status': state['status'], 'updated_utc': state.get('updated_utc'),
            'total_days': len(days), 'complete_days': len(complete), 'expected_runs': len(days)*24,
            'complete_runs': sum(d['run_count'] for d in complete),
            'expected_rows': expected_rows,
            'complete_rows': sum(d['rows'] for d in complete),
            'missing_rows': sum(d['missing_rows'] for d in complete),
            'total_bytes': sum(d['files']['zip']['bytes'] for d in complete), 'days': days}


def day_record(day, kind, interval='hourly', data=None):
    product_root, files, _ = variant(interval)
    if kind not in files or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day or ''):
        raise ValueError('日期或文件类型无效')
    date.fromisoformat(day)
    data = data if data is not None else catalog(interval)
    item = next((d for d in data['days'] if d['day'] == day), None)
    if not item or item['status'] != 'complete':
        raise FileNotFoundError('该日资料尚未发布')
    root = (product_root/'days'/day).resolve()
    path = (root/files[kind]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise FileNotFoundError('项目文件不可用')
    stat = path.stat()
    if stat.st_size != item['files'][kind]['bytes']:
        raise ValueError('项目文件已变化，请刷新目录')
    return {'name': f'wn3_balikun3_{day}_100m_{files[kind]}', 'path': path,
            'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
            'sha256': item['files'][kind]['sha256']}


def download_file(handler, query, head=False):
    record = day_record(query.get('day', ''), query.get('kind', 'zip'), query.get('interval', 'hourly'))
    size, status = record['bytes'], 200
    start, end = 0, size-1
    etag = '"'+record['sha256']+'"'
    header = None if head else handler.headers.get('Range')
    if handler.headers.get('If-Range', etag) != etag:
        header = None
    if header:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
        if not match or not any(match.groups()):
            start = size
        else:
            first, last = match.groups()
            if first:
                start, end = int(first), min(int(last), end) if last else end
            elif int(last) > 0:
                start = max(0, size-int(last))
            else:
                start = size
        if start >= size or end < start:
            handler.send_response(416)
            handler.send_header('Content-Range', f'bytes */{size}')
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return
        status = 206
    with downloads.open_snapshot(record) as stream:
        if handler.headers.get('If-None-Match') == etag:
            handler.send_response(304)
            handler.send_header('ETag', etag)
            handler.end_headers()
            return
        kind = 'application/zip' if query.get('kind', 'zip') == 'zip' else 'application/json' if query.get('kind') == 'manifest' else 'application/octet-stream'
        handler.send_response(status)
        for name, value in [('Content-Type', kind), ('Content-Length', str(end-start+1)),
                            ('Content-Disposition', f'attachment; filename="{record["name"]}"'),
                            ('Cache-Control', 'private, no-store'), ('Accept-Ranges', 'bytes'),
                            ('ETag', etag), ('X-Content-Type-Options', 'nosniff')]:
            handler.send_header(name, value)
        if status == 206:
            handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        handler.end_headers()
        if head:
            return
        stream.seek(start)
        remaining = end-start+1
        while remaining:
            chunk = stream.read(min(downloads.CHUNK, remaining))
            if not chunk:
                handler.close_connection = True
                return
            handler.wfile.write(chunk)
            remaining -= len(chunk)


def bundle_selection(query):
    interval = query.get('interval', 'hourly')
    data = catalog(interval)
    start, end = query.get('start', data['days'][0]['day']), query.get('end', data['days'][-1]['day'])
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', start) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', end):
        raise ValueError('请选择有效日期')
    date.fromisoformat(start); date.fromisoformat(end)
    if start > end or start < data['days'][0]['day'] or end > data['days'][-1]['day']:
        raise ValueError('下载日期须在项目起报范围内')
    selected = [d for d in data['days'] if start <= d['day'] <= end]
    ready = [d for d in selected if d['status'] == 'complete']
    if not ready:
        raise FileNotFoundError('所选日期暂无完成的资料')
    records = [day_record(d['day'], 'zip', interval, data) for d in ready]
    digest = hashlib.sha256(json.dumps([[d['day'], d['files']['zip']['sha256']] for d in ready]).encode()).hexdigest()
    if query.get('snapshot') and query['snapshot'] != digest:
        raise ValueError('已完成日期有更新，请刷新项目目录后下载')
    manifest = {k: data[k] for k in ['id', 'site', 'height_m', 'grid_degrees', 'statistic', 'source_collection', 'points',
                                  'interval', 'interval_minutes', 'temporal_method']}
    manifest.update({'start_bjt': start, 'end_bjt': end, 'days': [d['day'] for d in ready],
                     'missing_days': [d['day'] for d in selected if d['status'] != 'complete'],
                     'run_count': sum(d['run_count'] for d in ready), 'snapshot': digest})
    return manifest, records


def download_bundle(handler, query, head=False):
    if not head and not SLOTS.acquire(blocking=False):
        handler.reply(503, '已有两个项目下载正在传输，请稍后重试。'.encode(), 'text/plain; charset=utf-8')
        return
    try:
        manifest, records = bundle_selection(query)
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/zip')
        suffix = '_15min' if manifest['interval'] == '15min' else ''
        handler.send_header('Content-Disposition', f'attachment; filename="wn3_balikun3_100m_{manifest["start_bjt"]}_{manifest["end_bjt"]}{suffix}.zip"')
        handler.send_header('Cache-Control', 'private, no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        handler.close_connection = True
        if head:
            return
        try:
            with zipfile.ZipFile(handler.wfile, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
                bundle.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode())
                bundle.writestr('README.txt', variant(manifest['interval'])[2].encode('utf-8-sig'))
                for record in records:
                    with downloads.open_snapshot(record) as source, bundle.open('daily/'+record['name'], 'w', force_zip64=True) as target:
                        remaining = record['bytes']
                        while remaining:
                            chunk = source.read(min(downloads.CHUNK, remaining))
                            if not chunk:
                                raise OSError('Project file changed during transfer')
                            target.write(chunk)
                            remaining -= len(chunk)
        except (OSError, ValueError):
            handler.close_connection = True
    finally:
        if not head:
            SLOTS.release()
