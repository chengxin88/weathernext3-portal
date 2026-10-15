"""Resumable WeatherNext 3 native 100 m wind downloads for registered points."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import zipfile

import numpy as np

ROOT = Path(os.getenv('WN3_WIND_PROJECT_ROOT', '/home/chengyang/gods-eye-weather/weather-data/projects/xinjiang-wind-100m'))
COLLECTION = 'projects/gcp-public-data-weathernext/assets/weathernext_3_0_0_0p1deg'
BANDS = ['u_component_of_wind_100m_mean', 'v_component_of_wind_100m_mean']
UTC = timezone.utc
BJT = timezone(timedelta(hours=8))
MISSING = -9999.


def iso(value):
    return value.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temporary, path)


def days_for(config):
    first = datetime.fromisoformat(config['observation_start_bjt']).date()
    last = datetime.fromisoformat(config['observation_end_bjt']).date()
    return [(first + timedelta(days=n)).isoformat() for n in range((last-first).days+1)]


def expected_run_hours(day):
    start = datetime.fromisoformat(day).replace(tzinfo=BJT).astimezone(UTC)
    return [(start + timedelta(hours=n), 360 if (start.hour+n) % 6 == 0 else 48) for n in range(24)]


def expression_for(assets):
    def invoke(name, **arguments):
        return {'functionInvocationValue': {'functionName': name, 'arguments': arguments}}
    def constant(value):
        return {'constantValue': value}
    def ref(value):
        return {'valueReference': value}
    values, images, names = {}, [], []
    for n, asset in enumerate(assets):
        values[f'i{n}'] = invoke('Image.load', id=constant(asset['name']))
        values[f's{n}'] = invoke('Image.select', input=ref(f'i{n}'), bandSelectors=constant(BANDS))
        images.append(ref(f's{n}'))
        names.extend([f'b{n:04}u', f'b{n:04}v'])
    values['images'] = invoke('ImageCollection.fromImages', images={'arrayValue': {'values': images}})
    values['stack'] = invoke('ImageCollection.toBands', collection=ref('images'))
    values['renamed'] = invoke('Image.rename', input=ref('stack'), names=constant(names))
    values['missing'] = invoke('Image.constant', value=constant(MISSING))
    values['result'] = invoke('Image.unmask', input=ref('renamed'), value=ref('missing'), sameFootprint=constant(True))
    return {'values': values, 'result': 'result'}, names


def grid_for(points, native_grid):
    if native_grid['crsCode'] != 'EPSG:4326':
        raise ValueError('Unexpected source CRS')
    a = native_grid['affineTransform']
    sx, sy = a['scaleX'], a['scaleY']
    if abs(sx-.1) > 1e-9 or abs(abs(sy)-.1) > 1e-9 or a.get('shearX', 0) or a.get('shearY', 0):
        raise ValueError('Expected a native 0.1 degree regular grid')
    x = (np.array([p['lon'] for p in points])-a['translateX'])/sx-.5
    y = (np.array([p['lat'] for p in points])-a['translateY'])/sy-.5
    left, top = math.floor(float(x.min())), math.floor(float(y.min()))
    right, bottom = math.ceil(float(x.max())), math.ceil(float(y.max()))
    if left < 0 or top < 0 or right >= native_grid['dimensions']['width'] or bottom >= native_grid['dimensions']['height']:
        raise ValueError('Points exceed the native source grid')
    affine = {**a, 'translateX': a['translateX']+left*sx, 'translateY': a['translateY']+top*sy}
    grid = {'crsCode': native_grid['crsCode'], 'affineTransform': affine,
            'dimensions': {'width': right-left+1, 'height': bottom-top+1}}
    return grid, x-left, y-top


def interpolate(values, x, y):
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1 = np.minimum(x0+1, values.shape[2]-1)
    y1 = np.minimum(y0+1, values.shape[1]-1)
    wx, wy = x-x0, y-y0
    result = np.zeros((values.shape[0], len(x)))
    for yy, xx, weight in [(y0, x0, (1-wx)*(1-wy)), (y0, x1, wx*(1-wy)),
                            (y1, x0, (1-wx)*wy), (y1, x1, wx*wy)]:
        result += np.where(weight > 1e-12, values[:, yy, xx]*weight, 0.)
    return result


def wind_metrics(u, v):
    speed = np.hypot(u, v)
    direction = np.degrees(np.arctan2(-u, -v)) % 360
    return speed, np.where(speed < 1e-9, np.nan, direction)


class Budget:
    def __init__(self):
        self.bytes = 0
        self.lock = threading.Lock()
    def check(self, *args):
        if self.bytes > 512*1024**2:
            raise RuntimeError('One-day transfer limit exceeded')
    def record(self, amount):
        with self.lock:
            self.bytes += amount
        self.check()


def client_for(config):
    sys.path.insert(0, os.getenv('WN3_BACKEND_ROOT', '/home/chengyang/gods-eye-weather/weather_backend'))
    from prepare_weathernext3_china import RegionalClient
    return RegionalClient(config['earth_engine_project'], Budget())


def request_json(client, path, **kwargs):
    response = client.request('GET', path, **kwargs)
    try:
        return response.json()
    finally:
        response.close()


def list_day(client, day):
    start = expected_run_hours(day)[0][0]
    stop = start + timedelta(days=1)
    filter_value = f'properties.start_time>="{iso(start)}" AND properties.start_time<"{iso(stop)}" AND properties.forecast_hour<=360'
    assets, token = [], None
    for _ in range(8):
        params = {'pageSize': 1000, 'view': 'FULL', 'filter': filter_value,
                  'fields': 'assets(name),nextPageToken'}
        if token:
            params['pageToken'] = token
        data = request_json(client, COLLECTION+':listAssets', params=params)
        assets.extend(data.get('assets', []))
        token = data.get('nextPageToken')
        if not token:
            break
    else:
        raise ValueError('Unexpected listing size')
    # Retrieve only the four time-identity properties. Listing FULL properties
    # otherwise repeats 114 band descriptions on every source image.
    def invoke(name, **arguments):
        return {'functionInvocationValue': {'functionName': name, 'arguments': arguments}}
    constant = lambda value: {'constantValue': value}
    ref = lambda value: {'valueReference': value}
    nodes, refs = {}, []
    for n, asset in enumerate(assets):
        if not asset['name'].startswith(COLLECTION+'/'):
            raise ValueError('Unexpected source collection')
        nodes[f'i{n}'] = invoke('Image.load', id=constant(asset['name']))
        refs.append(ref(f'i{n}'))
    nodes['collection'] = invoke('ImageCollection.fromImages', images={'arrayValue': {'values': refs}})
    keys = ['start_time', 'end_time', 'forecast_hour', 'system:index']
    for n, key in enumerate(keys):
        nodes[f'p{n}'] = invoke('AggregateFeatureCollection.array', collection=ref('collection'), property=constant(key))
    nodes['result'] = invoke('Dictionary.fromLists', keys=constant(keys),
                             values={'arrayValue': {'values': [ref(f'p{n}') for n in range(4)]}})
    response = client.request('POST', f'projects/{client.project}/value:compute',
                              json={'expression': {'values': nodes, 'result': 'result'}})
    try:
        properties = response.json()['result']
    finally:
        response.close()
    if any(len(properties.get(key, [])) != len(assets) for key in keys):
        raise ValueError('Source property lengths differ')
    mapping = {}
    for n, identifier in enumerate(properties['system:index']):
        if identifier in mapping:
            raise ValueError('Unexpected/duplicate source asset')
        mapping[identifier] = {key: properties[key][n] for key in keys[:3]}
    if set(mapping) != {a['name'].rsplit('/', 1)[-1] for a in assets}:
        raise ValueError('Source property identities differ from the selected images')
    assets = [{'name': a['name'], 'properties': mapping[a['name'].rsplit('/', 1)[-1]]} for a in assets]
    expected = {(iso(run), lead) for run, end in expected_run_hours(day) for lead in range(1, end+1)}
    found = {}
    for asset in assets:
        p = asset['properties']
        hour = p.get('forecast_hour')
        if not isinstance(hour, (int, float)) or int(hour) != hour:
            raise ValueError('Invalid forecast hour')
        identity = (p.get('start_time'), int(hour))
        if identity not in expected or identity in found or not asset['name'].startswith(COLLECTION+'/'):
            raise ValueError('Unexpected/duplicate source asset')
        run = datetime.fromisoformat(identity[0].replace('Z', '+00:00'))
        if p.get('end_time') != iso(run+timedelta(hours=identity[1])):
            raise ValueError('Source valid time mismatch')
        found[identity] = asset
    if set(found) != expected:
        raise ValueError(f'Source missing {len(expected-set(found))} forecast times')
    return [found[key] for key in sorted(found)]


CSV_HEADER = ['point_id', 'longitude_deg', 'latitude_deg', 'height_m', 'run_utc', 'run_bjt',
              'lead_hours', 'valid_utc', 'valid_bjt', 'u100_mean_m_s', 'v100_mean_m_s',
              'wind_speed100_from_mean_uv_m_s', 'wind_direction100_from_deg', 'within_observation_period', 'data_status']
README = '''新疆风电项目 · 巴里坤3 · WeatherNext 3 百米风

坐标来自用户提供的工作簿“经纬度”表，含55台风机和1个风场站点。
按照原表坐标取数，未进行坐标系偏移。气象量高度为离地100米。
起报范围为原表观测时段内的每小时起报。UTC和北京时间均保留。
00/06/12/18 UTC主起报含1–360小时，其余起报含1–48小时。
完整预报有效时间可延伸到原表观测结束之后，CSV最后一列标识是否在观测时段内。
来源为Google WeatherNext 3的0.1度全球原生集合均值风场。
先对100米U/V分量分别进行空间双线性插值，再以sqrt(U²+V²)计算风速。
这是集合均值矢量合成风速，区别于成员风速的集合平均值。
风向按气象学来向定义，0°为北风、90°为东风；静风风向留空。
风速/U/V单位m/s，风向单位度。不进行15分钟时间插值或125米高度外推。
forecasts目录每个CSV对应一个起报批次，包含全部56个点和所有原生小时预报。
native-grid.npz保存用于点位插值的原生小网格及原始浮点精度。
manifest.json记录来源、时次、网格、取数方式及点位。
源端缺测值在CSV中留空，并用data_status标为missing_u、missing_v或missing_uv。
原生NPZ保留源端掩膜，u100_mask/v100_mask为True的格点不得当作数值使用。
缺失资料不会补零；清单给出每批缺测时效和全日缺测记录数。
'''


def download_day(day, config):
    started = time.monotonic()
    client = client_for(config)
    try:
        assets = list_day(client, day)
        full = request_json(client, assets[0]['name'])
        source_bands = [next(b for b in full['bands'] if b['id'] == name) for name in BANDS]
        if source_bands[0]['grid'] != source_bands[1]['grid']:
            raise ValueError('100 m wind grids differ')
        grid, x, y = grid_for(config['points'], source_bands[0]['grid'])
        shape = (grid['dimensions']['height'], grid['dimensions']['width'])
        checkpoints = ROOT/'.chunks'/day
        checkpoints.mkdir(parents=True, exist_ok=True)
        parts_u, parts_v, checksums = [], [], []
        # computePixels allows at most 1024 bands; 480 images use 960 U/V bands.
        def fetch_part(offset):
            import requests
            chosen = assets[offset:offset+480]
            signature = hashlib.sha256(json.dumps([grid, BANDS, chosen], sort_keys=True).encode()).hexdigest()
            checkpoint = checkpoints/f'{offset:04}.npz'
            cached = None
            if checkpoint.is_file():
                with np.load(checkpoint, allow_pickle=False) as saved:
                    if str(saved['signature'].item()) == signature:
                        cached = (saved['u'].copy(), saved['v'].copy(), str(saved['checksum'].item()))
            if cached is None:
                expression, names = expression_for(chosen)
                body = {'expression': expression, 'fileFormat': 'NPY', 'grid': grid}
                # A separate session per request keeps concurrent requests from
                # sharing mutable cookies/auth state. The existing user token
                # remains in memory and is never saved or printed.
                with requests.Session() as session:
                    session.trust_env = False
                    session.headers['Authorization'] = client.auth.session.headers['Authorization']
                    for attempt in range(3):
                        response = session.post(f'https://earthengine.googleapis.com/v1/projects/{client.project}/image:computePixels',
                                                json=body, timeout=(15,90),
                                                headers={'X-Goog-User-Project':client.project,'Accept-Encoding':'identity'},
                                                proxies={'http':client.proxy,'https':client.proxy})
                        try:
                            raw=response.content
                            client.budget.record(len(raw))
                            if response.ok:
                                break
                            if response.status_code not in (408,429,500,502,503,504) or attempt==2:
                                raise RuntimeError(response.json().get('error',{}).get('message','Pixel request failed'))
                        finally:
                            response.close()
                        time.sleep(.5*2**attempt)
                array = np.load(io.BytesIO(raw), allow_pickle=False, max_header_size=2*1024**2)
                if array.shape != shape or list(array.dtype.names or []) != names:
                    raise ValueError('Returned pixel layout differs from the source selection')
                cu = np.stack([array[name] for name in names[::2]])
                cv = np.stack([array[name] for name in names[1::2]])
                checksum = hashlib.sha256(raw).hexdigest()
                temporary = checkpoint.with_suffix('.tmp')
                with temporary.open('wb') as stream:
                    np.savez_compressed(stream, u=cu, v=cv, checksum=checksum, signature=signature)
                os.replace(temporary, checkpoint)
            else:
                cu, cv, checksum = cached
            if cu.shape != (len(chosen), *shape) or cv.shape != cu.shape:
                raise ValueError('Checkpoint grid layout differs')
            print('PIXELS_READY', day, min(offset+480, len(assets)), '/', len(assets), flush=True)
            return cu, cv, checksum
        with ThreadPoolExecutor(max_workers=3) as pixels:
            for cu, cv, checksum in pixels.map(fetch_part, range(0, len(assets), 480)):
                parts_u.append(cu); parts_v.append(cv); checksums.append(checksum)
        u, v = np.concatenate(parts_u), np.concatenate(parts_v)
        u_mask, v_mask = ~np.isfinite(u)|(u == MISSING), ~np.isfinite(v)|(v == MISSING)
        up = interpolate(np.where(u_mask, np.nan, u), x, y)
        vp = interpolate(np.where(v_mask, np.nan, v), x, y)
        speed, direction = wind_metrics(up, vp)
        missing_points = ~np.isfinite(up)|~np.isfinite(vp)
        if np.isfinite(speed).any() and np.nanmax(speed) > 150:
            raise ValueError('Wind values exceed expected physical range')
        obs_start = datetime.fromisoformat(config['observation_start_bjt']).replace(tzinfo=BJT).astimezone(UTC)
        obs_end = datetime.fromisoformat(config['observation_end_bjt']).replace(tzinfo=BJT).astimezone(UTC)
        stage = Path(tempfile.mkdtemp(prefix=f'.{day}-', dir=ROOT))
        try:
            native = stage/'native-grid.npz'
            meta = {'source_collection': COLLECTION, 'source_bands': BANDS, 'statistic': 'mean',
                    'height_m': 100, 'grid': grid, 'assets': assets,
                    'pixel_response_sha256': checksums, 'missing_transport_value': MISSING}
            np.savez_compressed(native, u100=u, v100=v, u100_mask=u_mask, v100_mask=v_mask,
                                meta=json.dumps(meta, ensure_ascii=False))
            day_manifest = {'schema': 1, 'day_bjt': day, 'project_id': config['id'],
                            'source_collection': COLLECTION, 'source_bands': BANDS,
                            'statistic': 'mean', 'height_m': 100, 'grid': grid,
                            'method': 'bilinear U/V, then hypot and meteorological wind-from direction',
                            'points': config['points'], 'run_count': 24, 'forecast_times': len(assets),
                            'rows': len(assets)*len(config['points']), 'runs': [],
                            'missing_rows': int(missing_points.sum()),
                            'missing_forecast_times': int(missing_points.any(axis=1).sum()),
                            'created_utc': iso(datetime.now(UTC)),
                            'observation_start_bjt': config['observation_start_bjt'],
                            'observation_end_bjt': config['observation_end_bjt']}
            archive = stage/'forecasts.zip'
            with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
                cursor = 0
                for run, end in expected_run_hours(day):
                    first_index = cursor
                    text = io.StringIO(newline='')
                    writer = csv.writer(text)
                    writer.writerow(CSV_HEADER)
                    for lead in range(1, end+1):
                        asset = assets[cursor]
                        assert (asset['properties']['start_time'], asset['properties']['forecast_hour']) == (iso(run), lead)
                        valid = run + timedelta(hours=lead)
                        for n, point in enumerate(config['points']):
                            d = direction[cursor, n]
                            missing_u, missing_v = not np.isfinite(up[cursor,n]), not np.isfinite(vp[cursor,n])
                            quality = 'missing_uv' if missing_u and missing_v else 'missing_u' if missing_u else 'missing_v' if missing_v else 'ok'
                            number = lambda value: '' if not np.isfinite(value) else f'{value:.6f}'
                            writer.writerow([point['id'], point['lon'], point['lat'], 100,
                                             iso(run), run.astimezone(BJT).isoformat(), lead,
                                             iso(valid), valid.astimezone(BJT).isoformat(),
                                             number(up[cursor,n]), number(vp[cursor,n]),
                                             number(speed[cursor,n]), number(d),
                                             int(obs_start <= valid <= obs_end), quality])
                        cursor += 1
                    run_id = run.strftime('%Y%m%d%H')
                    bundle.writestr(f'forecasts/wn3_{run_id}_wind100_points.csv', text.getvalue().encode('utf-8-sig'))
                    day_manifest['runs'].append({'id': run_id, 'run_utc': iso(run),
                                                'run_bjt': run.astimezone(BJT).isoformat(), 'hours': end,
                                                'rows': end*len(config['points']),
                                                'missing_leads': [h+1 for h, bad in enumerate(missing_points[first_index:cursor].any(axis=1)) if bad],
                                                'missing_rows': int(missing_points[first_index:cursor].sum())})
                bundle.writestr('README.txt', README.encode('utf-8-sig'))
                bundle.writestr('manifest.json', json.dumps(day_manifest, ensure_ascii=False, indent=2).encode())
                bundle.write(native, 'native-grid.npz')
            atomic_json(stage/'manifest.json', day_manifest)
            files = {}
            for name in ['forecasts.zip', 'native-grid.npz', 'manifest.json']:
                p = stage/name
                files[name] = {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            record = {'status': 'complete', 'run_count': 24, 'forecast_times': len(assets),
                      'rows': day_manifest['rows'], 'files': files,
                      'missing_rows': day_manifest['missing_rows'],
                      'missing_forecast_times': day_manifest['missing_forecast_times'],
                      'download_bytes': client.budget.bytes, 'seconds': round(time.monotonic()-started, 2)}
            atomic_json(stage/'record.json', record)
            target = ROOT/'days'/day
            if target.exists():
                raise ValueError('Completed day already exists')
            os.replace(stage, target)
            return record
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit-days', type=int, default=0)
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    (ROOT/'days').mkdir(parents=True, exist_ok=True)
    with (ROOT/'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        config = json.loads((ROOT/'project.json').read_text())
        state = {'schema': 1, 'project_id': config['id'], 'status': 'downloading', 'days': {}}
        for day in days_for(config):
            record = ROOT/'days'/day/'record.json'
            state['days'][day] = json.loads(record.read_text()) if record.is_file() else {'status': 'queued'}
        def save():
            state['updated_utc'] = iso(datetime.now(UTC))
            atomic_json(ROOT/'status.json', state)
        save()
        todo = [day for day, value in state['days'].items() if value['status'] != 'complete']
        if args.limit_days:
            todo = todo[:args.limit_days]
        workers = max(1, min(4, args.workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {}
            iterator = iter(todo)
            def launch_next():
                day = next(iterator, None)
                if day:
                    state['days'][day] = {'status': 'downloading'}
                    save()
                    pending[pool.submit(download_day, day, config)] = day
            for _ in range(workers):
                launch_next()
            while pending:
                future = next(as_completed(pending))
                day = pending.pop(future)
                try:
                    state['days'][day] = future.result()
                    print('COMPLETE', day, json.dumps(state['days'][day]), flush=True)
                except Exception as error:
                    state['days'][day] = {'status': 'error', 'error': str(error)[:500]}
                    print('ERROR', day, type(error).__name__, str(error)[:500], flush=True)
                save()
                launch_next()
        state['status'] = 'complete' if all(d['status'] == 'complete' for d in state['days'].values()) else 'partial'
        save()
        if any(d['status'] == 'error' for d in state['days'].values()):
            raise SystemExit(1)


if __name__ == '__main__':
    main()
