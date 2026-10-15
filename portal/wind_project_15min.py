"""Observation-aligned 15-minute derivatives of committed hourly WN3 wind data."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
from datetime import datetime, timedelta
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

import numpy as np
from wind_project_worker import ROOT, BJT, UTC, CSV_HEADER, atomic_json, days_for, iso, interpolate, wind_metrics

DERIVED = ROOT / '15min'
VERSION = 1
HEADER = CSV_HEADER + ['temporal_method', 'left_lead_hours', 'right_lead_hours', 'right_weight']
README = '''新疆风电项目 · 巴里坤3 · 100米风 · 15分钟观测时段对齐版

来源为已下载的WeatherNext 3原生0.1度逐小时集合均值U/V风场。
空间双线性插值到55台风机和1个风场站点后，对同一起报批次的相邻小时U/V分别做时间线性插值。
再以sqrt(U²+V²)合成风速，以气象学来向计算风向；静风风向留空。
不直接插值风速或风向，不混用不同起报批次。
时间步长15分钟，valid_bjt与原表北京时间的整点、15、30、45分钟对齐。
仅输出原表观测起止范围内且位于该批次1小时至末小时预报范围内的时间点。
没有0小时预报，不向前或向后外推；范围无重叠的批次CSV只有表头。
原生小时值保留，temporal_method=original_hourly；新增15分钟值为linear_uv_15min。
left_lead_hours/right_lead_hours/right_weight记录插值的两个端点和右端权重。
任一分量的相邻小时端点缺测，该分量的中间15分钟值留空，不跨缺测连接、不补零。
data_status为ok/missing_u/missing_v/missing_uv，缺测时合成风速、风向留空。
15分钟值是插值结果，并非模型原生15分钟预报或新增观测。
lead_hours可为小数；U/V、风速单位m/s，风向单位度，高度100米。
每个起报批次独立CSV，全部56个点位；within_observation_period均为1。
manifest.json记录时间范围、处理方式、各批次记录数及源端文件SHA256。
原生逐小时CSV及NPZ仍可从下载页的逐小时版本取得。
'''


def quarter_vectors(u, v, run, observation_start, observation_end):
    """Only adjacent hourly endpoints; exact knots never depend on another hour."""
    if u.shape != v.shape or u.ndim != 2:
        raise ValueError('U/V hourly shapes do not match')
    ticks = np.arange(4, 4*len(u)+1)
    valid = [run + timedelta(minutes=15*int(t)) for t in ticks]
    keep = np.array([observation_start <= t <= observation_end for t in valid])
    ticks = ticks[keep]
    lower = ticks//4 - 1
    upper = np.minimum(lower + (ticks % 4 != 0), len(u)-1)
    weight = (ticks % 4)/4.
    def component(values):
        left, right = values[lower], values[upper]
        # Exact hourly values must survive a missing adjacent hour.
        return np.where(weight[:, None] == 0, left,
                        left*(1-weight[:, None]) + right*weight[:, None])
    return ticks, lower+1, upper+1, weight, component(u), component(v)


def produce_day(day):
    config = json.loads((ROOT/'project.json').read_text())
    source = ROOT/'days'/day
    source_record = json.loads((source/'record.json').read_text())
    if source_record['status'] != 'complete':
        raise ValueError('Hourly source is incomplete')
    source_sha = source_record['files']['native-grid.npz']['sha256']
    destination = DERIVED/'days'/day
    if (destination/'record.json').is_file():
        record = json.loads((destination/'record.json').read_text())
        if (record.get('version') == VERSION and record.get('source_sha256') == source_sha
                and all((destination/name).is_file() and (destination/name).stat().st_size == info['bytes']
                        for name, info in record['files'].items())):
            return record
        raise ValueError('Existing derivative differs from current source/version')
    native = source/'native-grid.npz'
    if hashlib.sha256(native.read_bytes()).hexdigest() != source_sha:
        raise ValueError('Hourly native grid checksum mismatch')
    with np.load(native, allow_pickle=False) as data:
        u, v = data['u100'].astype(float), data['v100'].astype(float)
        u[data['u100_mask']] = np.nan
        v[data['v100_mask']] = np.nan
        meta = json.loads(str(data['meta'].item()))
    points = config['points']
    affine = meta['grid']['affineTransform']
    x = (np.array([p['lon'] for p in points])-affine['translateX'])/affine['scaleX']-.5
    y = (np.array([p['lat'] for p in points])-affine['translateY'])/affine['scaleY']-.5
    up, vp = interpolate(u, x, y), interpolate(v, x, y)
    groups = {}
    for n, asset in enumerate(meta['assets']):
        p = asset['properties']
        run = datetime.fromisoformat(p['start_time'].replace('Z', '+00:00'))
        if datetime.fromisoformat(p['end_time'].replace('Z', '+00:00')) != run + timedelta(hours=p['forecast_hour']):
            raise ValueError('Source valid time mismatch')
        group = groups.setdefault(run, {})
        if p['forecast_hour'] in group:
            raise ValueError('Duplicate source lead')
        group[p['forecast_hour']] = n
    if len(groups) != 24:
        raise ValueError('Expected 24 hourly runs')
    obs_start = datetime.fromisoformat(config['observation_start_bjt']).replace(tzinfo=BJT).astimezone(UTC)
    obs_end = datetime.fromisoformat(config['observation_end_bjt']).replace(tzinfo=BJT).astimezone(UTC)
    if (set(map(float, config['observation_intervals_minutes'])) != {15.0}
            or config['unique_observation_times'] != int((obs_end-obs_start).total_seconds()/900)+1
            or obs_start.minute % 15 or obs_end.minute % 15 or obs_start.second or obs_end.second):
        raise ValueError('Observation timestamps are not a continuous 15-minute grid')
    manifest = {'schema': 1, 'version': VERSION, 'project_id': config['id'], 'day_bjt': day,
                'height_m': 100, 'grid_degrees': .1, 'interval_minutes': 15,
                'source_collection': meta['source_collection'] if 'source_collection' in meta else source_record.get('source_collection', 'projects/gcp-public-data-weathernext/assets/weathernext_3_0_0_0p1deg'),
                'source_native_sha256': source_sha, 'observation_start_bjt': config['observation_start_bjt'],
                'observation_end_bjt': config['observation_end_bjt'], 'points': points,
                'method': 'spatial bilinear U/V, adjacent-hour linear U/V, then vector speed and wind-from direction',
                'scope': 'observation period intersected with each run forecast range, no extrapolation',
                'missing_policy': 'missing adjacent endpoint propagates per component; exact knots remain unchanged',
                'run_count': 24, 'rows': 0, 'missing_rows': 0, 'runs': [], 'created_utc': iso(datetime.now(UTC))}
    DERIVED.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=day+'-', dir=DERIVED))
    try:
        with zipfile.ZipFile(stage/'forecasts-15min.zip', 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for run, indices in sorted(groups.items()):
                horizon = 360 if run.hour % 6 == 0 else 48
                if set(indices) != set(range(1, horizon+1)):
                    raise ValueError('Hourly source leads are missing or duplicated')
                order = [indices[n] for n in range(1, horizon+1)]
                ticks, left, right, weight, uq, vq = quarter_vectors(up[order], vp[order], run, obs_start, obs_end)
                speed, direction = wind_metrics(uq, vq)
                missing_u, missing_v = ~np.isfinite(uq), ~np.isfinite(vq)
                text = io.StringIO(newline='')
                writer = csv.writer(text)
                writer.writerow(HEADER)
                run_utc, run_bjt = iso(run), run.astimezone(BJT).isoformat()
                number = lambda value: '' if not math.isfinite(value) else f'{value:.6f}'
                for n, tick in enumerate(ticks):
                    valid = run + timedelta(minutes=15*int(tick))
                    prefix = [run_utc, run_bjt, f'{tick/4:g}', iso(valid), valid.astimezone(BJT).isoformat()]
                    suffix = ['original_hourly' if tick % 4 == 0 else 'linear_uv_15min',
                              int(left[n]), int(right[n]), f'{weight[n]:g}']
                    for p, point in enumerate(points):
                        quality = 'missing_uv' if missing_u[n,p] and missing_v[n,p] else 'missing_u' if missing_u[n,p] else 'missing_v' if missing_v[n,p] else 'ok'
                        writer.writerow([point['id'], point['lon'], point['lat'], 100] + prefix +
                                         [number(uq[n,p]), number(vq[n,p]), number(speed[n,p]), number(direction[n,p]), 1, quality] + suffix)
                run_id = run.strftime('%Y%m%d%H')
                archive.writestr(f'forecasts/wn3_{run_id}_wind100_points_15min.csv', text.getvalue().encode('utf-8-sig'))
                count, missing = len(ticks)*len(points), int((missing_u | missing_v).sum())
                manifest['rows'] += count
                manifest['missing_rows'] += missing
                manifest['runs'].append({'id': run_id, 'run_utc': run_utc, 'source_horizon_hours': horizon,
                                         'rows': count, 'missing_rows': missing, 'forecast_times': len(ticks),
                                         'first_valid_bjt': (run+timedelta(minutes=15*int(ticks[0]))).astimezone(BJT).isoformat() if len(ticks) else None,
                                         'last_valid_bjt': (run+timedelta(minutes=15*int(ticks[-1]))).astimezone(BJT).isoformat() if len(ticks) else None})
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode())
            archive.writestr('README.txt', README.encode('utf-8-sig'))
        atomic_json(stage/'manifest.json', manifest)
        files = {name: {'bytes': (stage/name).stat().st_size,
                        'sha256': hashlib.sha256((stage/name).read_bytes()).hexdigest()}
                 for name in ['forecasts-15min.zip', 'manifest.json']}
        record = {'version': VERSION, 'status': 'complete', 'source_sha256': source_sha,
                  'run_count': 24, 'rows': manifest['rows'], 'missing_rows': manifest['missing_rows'], 'files': files}
        atomic_json(stage/'record.json', record)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, destination)
        return record
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--day')
    args = parser.parse_args()
    DERIVED.mkdir(parents=True, exist_ok=True)
    with (DERIVED/'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads((ROOT/'project.json').read_text())
        days = days_for(config)
        if args.day and args.day not in days:
            raise ValueError('Date outside project')
        state = {'version': VERSION, 'status': 'processing', 'days': {}}
        for day in days:
            path = DERIVED/'days'/day/'record.json'
            state['days'][day] = json.loads(path.read_text()) if path.is_file() else {'status': 'queued'}
        def save():
            state['updated_utc'] = iso(datetime.now(UTC))
            atomic_json(DERIVED/'status.json', state)
        save()
        failures = 0
        pending = [args.day] if args.day else [d for d in days if state['days'][d]['status'] != 'complete']
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(produce_day, d): d for d in pending}
            for future in as_completed(futures):
                day = futures[future]
                try:
                    state['days'][day] = future.result()
                    print(json.dumps({'day': day, 'rows': state['days'][day]['rows'], 'status': 'complete'}), flush=True)
                except Exception as error:
                    state['days'][day] = {'status': 'error', 'message': str(error)}
                    failures += 1
                    print(json.dumps({'day': day, 'status': 'error', 'message': str(error)}), flush=True)
                save()
        state['status'] = 'complete' if all(r['status'] == 'complete' for r in state['days'].values()) else 'error' if failures else 'processing'
        save()
        if failures:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
