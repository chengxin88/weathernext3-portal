"""Bounded WeatherNext 3 adapter for the existing regional forecast cache.

Only UTC 00, +24h, Shenzhen 2m temperature ensemble mean is enabled in
this pilot. UI readers never download; the existing warmer fetches one cycle.
GCS uses the existing user ADC with no requester billing project.
"""
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import tempfile
import time
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

MODEL = "Google WeatherNext 3（集合均值）"
VERSION = "weathernext_3_0_0_statistics"
VARIABLE = "temperature_2m_mean"
SCHEMA = "wn3-shenzhen-temperature-v1"
BUCKET = "weathernext3_statistics_spatial"
BOUNDS = [113.4, 115.1, 22.2, 23.1]  # west, east, south, north; interpolation halo
DEFAULT_ROOT = Path(__file__).resolve().parent / "cache"
MAX_BYTES = 32 * 1024 * 1024  # includes partial reads and retries


class WeatherNext3Error(RuntimeError):
    def __init__(self, status, detail, *, identity=None, path=None, http_status=None):
        self.status = status
        self.record = {"status": status, "detail": detail, "identity": identity,
                       "path": path, "http_status": http_status}
        super().__init__(json.dumps(self.record, ensure_ascii=False))


def utc(run):
    value = pd.Timestamp(run)
    return value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")


def cache_root(root=None):
    return Path(root or os.getenv("FORECAST_CACHE_ROOT", str(DEFAULT_ROOT)))


def pilot_identity(run, lead=24, variable="temperature_2m", accumulation=None):
    run = utc(run)
    if variable not in ("temperature_2m", "气温") or accumulation is not None:
        raise WeatherNext3Error("unsupported", "试接入仅开放2米气温；降水累计尚未验收")
    if run.minute or run.second or run.microsecond or run.hour != 0 or int(lead) != lead or int(lead) != 24:
        raise WeatherNext3Error("unsupported", "试接入范围为UTC 00起报、+24小时气温集合均值")
    return {"schema": SCHEMA, "version": VERSION, "statistic": "mean",
            "variable": VARIABLE, "run_utc": run.isoformat(), "lead_hours": 24,
            "valid_utc": (run + pd.Timedelta(hours=24)).isoformat(),
            "bounds": BOUNDS, "unit": "degC", "accumulation_hours": None,
            "native_grid_degrees": 0.1}


def manifest_path(root, run):
    run = utc(run)
    return cache_root(root) / (run + pd.Timedelta(hours=8)).strftime("%Y%m%d%HBJT") / "WEATHERNEXT3" / "temperature_2m_mean_f024.json"


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".wn3-", delete=False) as handle:
        temp = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def validate_arrays(lon, lat, field):
    if lon.ndim != 1 or lat.ndim != 1 or len(lon) < 2 or len(lat) < 2 or field.shape != (len(lat), len(lon)):
        raise ValueError("Regional grid shape mismatch")
    if not np.isfinite(lon).all() or not np.isfinite(lat).all() or not (np.diff(lon) > 0).all() or not (np.diff(lat) > 0).all():
        raise ValueError("Regional coordinates are not finite ascending axes")
    if not np.allclose(np.diff(lon), .1, atol=2e-5) or not np.allclose(np.diff(lat), .1, atol=2e-5):
        raise ValueError("Native grid resolution mismatch")
    if not np.allclose([lon[0], lon[-1], lat[0], lat[-1]], BOUNDS, atol=2e-5):
        raise ValueError("Regional bounds mismatch")
    finite = np.isfinite(field)
    if not finite.any() or np.any((field[finite] < -100) | (field[finite] > 80)):
        raise ValueError("Invalid Celsius field")


def read_cached(run, lead=24, variable="temperature_2m", accumulation=None, root=None):
    identity = pilot_identity(run, lead, variable, accumulation)
    path = manifest_path(root, run)
    try:
        meta = json.loads(path.read_text())
        if meta.get("status") != "complete" or any(meta.get(k) != v for k, v in identity.items()):
            raise ValueError("Cache identity mismatch")
        if not meta.get("source_generation") or meta.get("billing_header_sent") is not False:
            raise ValueError("Cache provenance incomplete")
        name = meta["artifact"]
        if not re.fullmatch(r"temperature_2m_mean_f024_[a-f0-9]{24}\.npz", name):
            raise ValueError("Invalid cache artifact")
        data = (path.parent / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != meta["artifact_sha256"]:
            raise ValueError("Cache checksum mismatch")
        with np.load(path.parent / name, allow_pickle=False) as archive:
            lon, lat, field = (archive[key].copy() for key in ("lon", "lat", "field"))
        validate_arrays(lon, lat, field)
        if int(np.isfinite(field).sum()) != meta["finite_count"]:
            raise ValueError("Cache finite count mismatch")
        return lon, lat, field, meta
    except FileNotFoundError:
        detail = "所选WeatherNext 3气温场尚未缓存；页面不会触发下载"
        status_file = path.with_suffix(".status.json")
        try:
            status = json.loads(status_file.read_text())
            raise WeatherNext3Error(status["status"], status["detail"], identity=status.get("identity"),
                                    path=status.get("path"), http_status=status.get("http_status"))
        except (OSError, ValueError, KeyError):
            raise WeatherNext3Error("missing_forecast", detail) from None
    except (OSError, ValueError, KeyError) as error:
        raise WeatherNext3Error("invalid_cache", str(error), path=str(path)) from None


def fetch_field(run, lead=24, variable="temperature_2m", accumulation=None, root=None):
    return read_cached(run, lead, variable, accumulation, root)[:3]


def cache_signature(root=None):
    """Include newly committed fields in existing verification result identities."""
    return "|".join(f"{p.parent.parent.name}:{p.stat().st_mtime_ns}" for p in
                    sorted(cache_root(root).glob("??????????BJT/WEATHERNEXT3/temperature_2m_mean_f024.json")))


def cached_records(root=None):
    records = []
    for path in sorted(cache_root(root).glob("??????????BJT/WEATHERNEXT3/temperature_2m_mean_f024.json"), reverse=True):
        try:
            meta = json.loads(path.read_text())
            _, _, _, checked = read_cached(meta["run_utc"], root=root)
            records.append(checked)
        except (WeatherNext3Error, OSError, ValueError, KeyError):
            continue
    return records


class StatisticsClient:
    """JSON API + generation-pinned media, explicit no-billing transport.

    No gcloud subprocess, new login, ADC write or service-account fallback.
    Only the demonstrated authorized_user credential type is accepted.
    """
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        proxy = os.getenv("WEATHERNEXT3_PROXY", "http://127.0.0.1:17907")
        self.session.proxies = {"http": proxy, "https": proxy} if proxy else {}
        self.identity = None
        self.transferred = 0
        self.objects = {}
        credential = Path(os.getenv("GOOGLE_APPLICATION_CREDENTIALS", str(Path.home() / ".config/gcloud/application_default_credentials.json")))
        try:
            cred = json.loads(credential.read_text())
        except (OSError, ValueError):
            raise WeatherNext3Error("authorization", "既有ADC无法读取；请检查该服务器上的Google用户授权") from None
        if cred.get("type") != "authorized_user":
            raise WeatherNext3Error("authorization", "当前ADC不是已验证的用户身份；未自动采用服务账号", path=str(credential))
        try:
            auth = self.request("POST", "https://oauth2.googleapis.com/token", data={
                "grant_type": "refresh_token", "refresh_token": cred["refresh_token"],
                "client_id": cred["client_id"], "client_secret": cred["client_secret"]}).json()
            self.session.headers["Authorization"] = "Bearer " + auth["access_token"]
            info = self.request("GET", "https://www.googleapis.com/oauth2/v2/userinfo").json()
            self.identity = info.get("email")
            if not self.identity or info.get("verified_email") is not True:
                raise WeatherNext3Error("authorization", "无法核实当前Google用户邮箱")
        except (KeyError, ValueError):
            raise WeatherNext3Error("authorization", "既有用户ADC刷新失败；未修改凭据") from None

    def request(self, method, url, **kwargs):
        # Never propagate quota_project_id, userProject or ambient auth headers.
        if "userProject" in kwargs.get("params", {}) or any(k.lower() == "x-goog-user-project" for k in self.session.headers):
            raise WeatherNext3Error("billing_blocked", "请求含计费项目，已阻止")
        for attempt in range(3):
            try:
                response = self.session.request(method, url, timeout=(10, 60), **kwargs)
            except requests.RequestException:
                if attempt == 2:
                    raise WeatherNext3Error("network", "连接失败，3次有界重试已耗尽", identity=self.identity, path=url) from None
            else:
                if response.status_code == 200:
                    return response
                status = response.status_code
                response.close()
                if status not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                    category = "authorization" if status in (400, 401, 403) and "oauth2" in url or status in (401, 403) else "not_published" if status == 404 else "upstream"
                    raise WeatherNext3Error(category, f"HTTP {status}；数据读取失败", identity=self.identity, path=url, http_status=status)
            time.sleep(.5 * 2 ** attempt + random.uniform(0, .25))
        raise AssertionError("Unreachable")

    def object_info(self, name):
        return self.request("GET", self.object_url(name), params={"fields": "name,size,generation,crc32c"}).json()

    def object_url(self, name):
        return "https://storage.googleapis.com/storage/v1/b/" + BUCKET + "/o/" + quote(name, safe="")

    def media(self, name, info=None):
        info = info or self.object_info(name)
        size = int(info["size"])
        for attempt in range(3):
            if self.transferred + size > MAX_BYTES:
                raise WeatherNext3Error("transfer_limit", "本任务32MiB传输上限不足，停止下载", identity=self.identity, path="gs://" + BUCKET + "/" + name)
            data = bytearray()
            response = self.request("GET", self.object_url(name), params={"alt": "media", "generation": info["generation"]}, stream=True)
            try:
                for piece in response.iter_content(65536):
                    self.transferred += len(piece)
                    if self.transferred > MAX_BYTES or len(data) + len(piece) > size:
                        raise WeatherNext3Error("transfer_limit", "实际传输超出声明大小或上限", path="gs://" + BUCKET + "/" + name)
                    data.extend(piece)
                if len(data) != size:
                    raise requests.ConnectionError("Object length mismatch")
                self.objects[name] = {"generation": info["generation"], "bytes": size, "crc32c": info.get("crc32c")}
                return bytes(data)
            except requests.RequestException:
                if attempt == 2:
                    raise WeatherNext3Error("network", "数据块传输中断，3次重试已耗尽", identity=self.identity, path="gs://" + BUCKET + "/" + name) from None
                time.sleep(.5 * 2 ** attempt + random.uniform(0, .25))
            finally:
                response.close()

    def select_run(self, run):
        prefix = VERSION + "/zarr/" + "2026_to_present/" + utc(run).strftime("%Y%m%d_%Hhr_")
        found = self.request("GET", "https://storage.googleapis.com/storage/v1/b/" + BUCKET + "/o", params={
            "prefix": prefix, "delimiter": "/", "maxResults": 20, "fields": "prefixes,nextPageToken"}).json()
        folders = [p for p in found.get("prefixes", []) if re.fullmatch(re.escape(prefix) + r"\d+_preds/", p)]
        if len(folders) != 1 or found.get("nextPageToken"):
            raise WeatherNext3Error("not_published" if not folders else "ambiguous_run", "起报目录未发布或存在多个目录；未猜测版本后缀", identity=self.identity, path="gs://" + BUCKET + "/" + prefix)
        return folders[0] + "predictions.zarr/"

    def close(self):
        self.session.headers.pop("Authorization", None)
        self.session.close()


def read_native(client, prefix, run, root_meta):
    import zarr
    from zarr.core.buffer import default_buffer_prototype

    consolidated = root_meta.get("consolidated_metadata", {}).get("metadata", {})
    names = (VARIABLE, "lat_0p1", "lon_0p1", "lead_time", "init_time", "datetime")
    if any(name not in consolidated for name in names):
        raise WeatherNext3Error("metadata", "必需的统计变量或坐标缺失")
    attrs = consolidated[VARIABLE]["attributes"]
    if attrs.get("units") != "K" or attrs.get("step_type") != "instant" or attrs.get("parameter_conversion_statistic") != "mean" or attrs.get("grid_degrees") != .1:
        raise WeatherNext3Error("metadata", "气温单位、瞬时值、集合均值或0.1度网格不匹配")
    if consolidated[VARIABLE].get("dimension_names") != ["lead_time", "lat_0p1", "lon_0p1"] or consolidated[VARIABLE]["chunk_grid"]["configuration"]["chunk_shape"] != [1, 1801, 3600]:
        raise WeatherNext3Error("metadata", "数据布局改变，停止而非扩大读取范围")
    buffers = {name + "/zarr.json": default_buffer_prototype().buffer.from_bytes(json.dumps(consolidated[name]).encode()) for name in names}
    def add(key, fill_zero=False):
        try:
            data = client.media(prefix + key)
        except WeatherNext3Error as error:
            if not fill_zero or error.record["http_status"] != 404:
                raise
            if consolidated["init_time"]["fill_value"] != 0:
                raise WeatherNext3Error("metadata", "缺少起报坐标且默认值非零")
            return
        buffers[key] = default_buffer_prototype().buffer.from_bytes(data)
    def array(name):
        return zarr.open_array(store=zarr.storage.MemoryStore(store_dict=buffers, read_only=True), path=name, mode="r")
    for key in ("lat_0p1/c/0", "lon_0p1/c/0", "lead_time/c/0", "init_time/c"):
        add(key, fill_zero=(key == "init_time/c"))
    lat, lon, leads = (np.asarray(array(name)[:]) for name in ("lat_0p1", "lon_0p1", "lead_time"))
    if consolidated["lead_time"]["attributes"]["units"] != "hours":
        raise WeatherNext3Error("metadata", "时效单位非小时")
    units = consolidated["init_time"]["attributes"]["units"]
    if not units.startswith("days since ") or utc(units.split(" since ")[1]) + pd.Timedelta(days=int(array("init_time")[()])) != utc(run):
        raise WeatherNext3Error("time_mismatch", "起报坐标不匹配")
    indexes = np.flatnonzero(leads == 24)
    if len(indexes) != 1:
        raise WeatherNext3Error("missing_forecast", "精确+24小时时效缺失或重复")
    step = int(indexes[0])
    if consolidated["datetime"]["attributes"]["units"] != "nanoseconds since 1970-01-01":
        raise WeatherNext3Error("metadata", "有效时间坐标单位改变")
    add(f"datetime/c/{step}")
    if pd.Timestamp(int(array("datetime")[step]), unit="ns", tz="UTC") != utc(run) + pd.Timedelta(hours=24):
        raise WeatherNext3Error("time_mismatch", "有效时间与起报+时效不匹配")
    iy = np.flatnonzero((lat >= BOUNDS[2] - 2e-5) & (lat <= BOUNDS[3] + 2e-5))
    ix = np.flatnonzero((lon >= BOUNDS[0] - 2e-5) & (lon <= BOUNDS[1] + 2e-5))
    if not len(iy) or not len(ix) or not (np.diff(iy) == 1).all() or not (np.diff(ix) == 1).all():
        raise WeatherNext3Error("metadata", "区域坐标切片不连续")
    add(f"{VARIABLE}/c/{step}/0/0")
    field = np.asarray(array(VARIABLE)[step, slice(int(iy[0]), int(iy[-1]) + 1), slice(int(ix[0]), int(ix[-1]) + 1)], dtype=np.float64) - 273.15
    lon, lat = lon[ix].astype(float), lat[iy].astype(float)
    validate_arrays(lon, lat, field)
    return lon, lat, field


def prefetch(run, root=None, client_factory=StatisticsClient, check_generation=True):
    identity = pilot_identity(run)
    path = manifest_path(root, run)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            cached = read_cached(run, root=root)
        except WeatherNext3Error:
            cached = None
        if cached and not check_generation:
            return {**cached[3], "status": "cache_hit", "download_bytes_this_request": 0}
        client = None
        try:
            client = client_factory()
            prefix = client.select_run(run)
            root_info = client.object_info(prefix + "zarr.json")
            # The validated +24h coordinate was index 23 in this hourly dataset.
            # Confirm it again on any download; metadata-only dedup uses all saved objects.
            if cached and cached[3]["source_path"] == "gs://" + BUCKET + "/" + prefix:
                saved = cached[3]["source_objects"]
                unchanged = all(client.object_info(name)["generation"] == info["generation"] for name, info in saved.items())
                if unchanged:
                    path.with_suffix(".status.json").unlink(missing_ok=True)
                    return {**cached[3], "status": "cache_hit", "download_bytes_this_request": 0}
            root_meta = json.loads(client.media(prefix + "zarr.json", root_info))
            lon, lat, field = read_native(client, prefix, run, root_meta)
            fingerprint = hashlib.sha256(json.dumps(client.objects, sort_keys=True).encode()).hexdigest()[:24]
            artifact = path.parent / f"temperature_2m_mean_f024_{fingerprint}.npz"
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".wn3-", delete=False) as handle:
                temp = Path(handle.name)
                np.savez_compressed(handle, lon=lon, lat=lat, field=field)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.replace(temp, artifact)
            finally:
                temp.unlink(missing_ok=True)
            meta = {**identity, "status": "complete", "model": MODEL,
                    "source_path": "gs://" + BUCKET + "/" + prefix,
                    "source_generation": fingerprint, "source_objects": client.objects,
                    "identity": client.identity, "source_unit": "K", "step_type": "instant",
                    "created_utc": datetime.now(timezone.utc).isoformat(), "artifact": artifact.name,
                    "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "finite_count": int(np.isfinite(field).sum()), "shape": list(field.shape),
                    "download_bytes": client.transferred, "billing_header_sent": False,
                    "attribution": "Google DeepMind WeatherNext 3 / precomputed mean / historical data CC BY 4.0"}
            atomic_json(path, meta)  # committed manifest is the only readiness marker
            read_cached(run, root=root)
            path.with_suffix(".status.json").unlink(missing_ok=True)
            # Keep older generation artifacts until the existing dated-directory
            # pruner removes the cycle; concurrent readers may hold the old manifest.
            return {**meta, "download_bytes_this_request": client.transferred}
        except Exception as error:
            safe = error if isinstance(error, WeatherNext3Error) else WeatherNext3Error("invalid_data", type(error).__name__ + ": 资料校验或本地写入失败")
            atomic_json(path.with_suffix(".status.json"), {**safe.record, "run_utc": identity["run_utc"], "checked_utc": datetime.now(timezone.utc).isoformat(), "retry_after_seconds": 3600})
            raise safe from None
        finally:
            if client is not None:
                client.close()


def warm_latest(root=None, now=None):
    """One daily UTC00/+24h cycle with existing grid observations; no backfill."""
    root = cache_root(root)
    safe = utc(now or datetime.now(timezone.utc)) - pd.Timedelta(hours=1)
    valid = safe.normalize()
    run = valid - pd.Timedelta(hours=24)
    obs = root / "OBSERVATION/grid" / (valid + pd.Timedelta(hours=8)).strftime("%Y%m%d%H.pkl")
    if not obs.is_file():
        return {"status": "missing_observation", "valid_utc": valid.isoformat(), "detail": "等待既有深圳格点实况缓存"}
    path = manifest_path(root, run)
    try:
        read_cached(run, root=root)
        return {"status": "cache_hit", "run_utc": run.isoformat(), "download_bytes_this_request": 0}
    except WeatherNext3Error:
        pass
    status_path = path.with_suffix(".status.json")
    if status_path.is_file() and time.time() - status_path.stat().st_mtime < 3600:
        return {"status": "retry_deferred", "run_utc": run.isoformat()}
    return prefetch(run, root=root, check_generation=False)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prefetch", "inspect"))
    parser.add_argument("--run", required=True, help="UTC initialization, e.g. 2026-10-08T00:00:00Z")
    args = parser.parse_args()
    try:
        result = prefetch(args.run) if args.command == "prefetch" else read_cached(args.run)[3]
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except WeatherNext3Error as error:
        print(str(error))
        raise SystemExit(1)
