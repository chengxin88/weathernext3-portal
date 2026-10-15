"""RMAPS-style static maps from the locally prepared WeatherNext 3 China cache.

Reads verified native 0.05/0.1-degree fields; never triggers a download.
"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
import io
import json
import os
import re
import sys
import tempfile
import threading

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as path_effects
from matplotlib import font_manager
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.text import Text

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, "/home/chengyang/gods-eye-weather/weather_backend")
from weathernext3_native_cache import (  # noqa: E402
    china_field, CHINA_DIRECTORY, CHINA,
    download_field, DOWNLOAD_DIRECTORY, DOWNLOAD_BOUNDS,
)

ROOT = Path(os.getenv("WN3_ROOT", "/home/chengyang/gods-eye-weather/weather-data/region-v4/weathernext3"))
CACHE = Path(os.getenv("FORECAST_CACHE_ROOT", str(BASE / "cache"))) / "WN3_PORTAL"
MODEL_LABEL = "WeatherNext 3 ( Google DeepMind · 集合均值 )"
STYLE_VERSION = "v9-geography"
CITY_PLACES = Path(__file__).with_name("cities.json")
MAP_GEOGRAPHY = Path(__file__).with_name("geography.json")
CACHE_SOURCES = (
    (DOWNLOAD_DIRECTORY, download_field, DOWNLOAD_BOUNDS),
    (CHINA_DIRECTORY, china_field, CHINA),
)
RENDER_LOCK = threading.Lock()
UTC = timezone.utc
BJT = timedelta(hours=8)

for path in (BASE / "data" / "NotoSansCJKsc-Regular.otf",
             Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")):
    if path.is_file():
        font_manager.fontManager.addfont(str(path))
        plt.rcParams["font.sans-serif"] = [font_manager.FontProperties(fname=str(path)).get_name(), "DejaVu Sans"]
        break
plt.rcParams["axes.unicode_minus"] = False

# west, south, east, north; each run offers only regions inside its coverage.
REGIONS = {
    "southern_china": ("南部中国区域", DOWNLOAD_BOUNDS),
    "china": ("中国区域", (72.0, 15.0, 136.0, 55.0)),
    "north": ("华北区域", (105.5, 34.0, 123.5, 46.5)),
    "east": ("华东区域", (112.0, 23.5, 125.0, 36.5)),
    "south": ("华南区域", (104.0, 17.0, 122.0, 29.0)),
    "guangdong": ("广东及周边", (109.0, 20.0, 118.0, 26.0)),
}
SOUTHERN_TEMPERATURE_REGIONS = {"southern_china", "south", "guangdong"}

RAIN_LEVELS = {
    1: [0.1, 1, 5, 10, 20, 50],
    3: [0.1, 1, 5, 10, 20, 50, 80],
    6: [0.1, 1, 5, 10, 25, 50, 100],
    12: [0.1, 1, 5, 10, 25, 50, 100, 150],
    24: [0.1, 10, 25, 50, 100, 250, 400],
}
# RMAPS-like ramp: light green, green, light blue, blue, magenta, then darker
RAIN_COLORS = ["#a6f28f", "#3dba3d", "#61b8ff", "#0000ff", "#fa00fa", "#800040", "#4b0026", "#2a0016"]

PRODUCTS = {
    "rain1": "1小时累计降水", "rain3": "3小时累计降水", "rain6": "6小时累计降水",
    "rain12": "12小时累计降水", "rain24": "24小时累计降水",
    "t2m": "2米气温", "td2m": "2米露点", "rh2m": "2米相对湿度",
    "wind10": "10米风", "mslp": "海平面气压",
    "dt24": "24小时变温",
}


def run_id(run):
    return run.strftime("%Y%m%d%H")


def parse_run(value):
    if not re.fullmatch(r"\d{10}", value or ""):
        raise ValueError("起报时间格式应为 YYYYMMDDHH（UTC）")
    return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=UTC)


@lru_cache(maxsize=32)
def _read_regional_catalog(path, modified, size):
    return json.loads(Path(path).read_text())


def _regional_catalog(value):
    """Expose only the hours committed after both native grids are ready."""
    path = ROOT / f"run_{value}.json"
    try:
        info = path.stat()
        catalog = _read_regional_catalog(str(path), info.st_mtime_ns, info.st_size)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if (catalog.get("status") in ("preparing", "complete")
            and catalog.get("run") == parse_run(value).strftime("%Y-%m-%dT%H:%M:%SZ")
            and catalog.get("bounds") == list(DOWNLOAD_BOUNDS)
            and catalog.get("native_cache_directory") == DOWNLOAD_DIRECTORY
            and catalog.get("frames")):
        return catalog
    return None


def _covered_regions(bounds):
    west, south, east, north = bounds
    return [key for key, (_, region) in REGIONS.items()
            if west <= region[0] and south <= region[1]
            and east >= region[2] and north >= region[3]]


def _run_source(run):
    value = run_id(run)
    for directory, reader, bounds in CACHE_SOURCES:
        if not (ROOT / directory / value).is_dir():
            continue
        if directory == DOWNLOAD_DIRECTORY and not _regional_catalog(value):
            continue
        return reader, bounds
    raise FileNotFoundError(f"起报 {value} 尚无可用的已下载时次")


def available_runs():
    """Runs with at least one complete set of cached surface fields."""
    runs = {}
    for directory, _, bounds in CACHE_SOURCES:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for folder in base.iterdir():
            if (not folder.is_dir() or not re.fullmatch(r"\d{10}", folder.name)
                    or folder.name in runs):
                continue
            regional = _regional_catalog(folder.name) if directory == DOWNLOAD_DIRECTORY else None
            if directory == DOWNLOAD_DIRECTORY and not regional:
                continue
            names = {p.name for p in folder.glob("f???_*.npz")}
            committed = {frame["lead"] for frame in regional["frames"]} if regional else None
            leads = sorted(int(n[1:4]) for n in names if n.endswith("_rain1h.npz")
                           and (committed is None or int(n[1:4]) in committed)
                           and all(n[:5] + k + ".npz" in names for k in ("surface_t", "surface_td", "surface_u", "surface_v", "surface_msl")))
            if leads:
                station_leads = {
                    "t2m": [lead for lead in leads if f"f{lead:03}_station_t.npz" in names],
                    "td2m": [lead for lead in leads if f"f{lead:03}_station_td.npz" in names],
                }
                station_leads["rh2m"] = [lead for lead in station_leads["t2m"]
                                         if lead in station_leads["td2m"]]
                station_leads["dt24"] = [lead for lead in station_leads["t2m"]
                                         if lead > 24 and lead - 24 in station_leads["t2m"]]
                runs[folder.name] = {"id": folder.name, "leads": leads,
                                     "station_leads": station_leads,
                                     "status": ("complete" if not regional or regional["status"] == "complete" else "partial"),
                                     "forecast_horizon_hours": (regional["forecast_horizon_hours"] if regional else max(leads)),
                                     "bounds": list(bounds), "regions": _covered_regions(bounds)}
    return [runs[value] for value in sorted(runs, reverse=True)]


def _field(run, lead, key, bounds):
    reader, coverage = _run_source(run)
    if reader is download_field:
        regional = _regional_catalog(run_id(run))
        if not regional or lead not in {frame["lead"] for frame in regional["frames"]}:
            raise FileNotFoundError(f"+{lead}h 尚未下载完成")
    west, south, east, north = coverage
    if not (west <= bounds[0] and south <= bounds[1]
            and east >= bounds[2] and north >= bounds[3]):
        raise ValueError("所选区域超出该起报资料的覆盖范围")
    data = reader(ROOT, run, lead, key, bounds)
    if data is None:
        raise FileNotFoundError(f"+{lead}h {key} 尚未缓存")
    return data


def _rain(run, lead, hours, bounds):
    if lead < hours:
        raise FileNotFoundError(f"+{lead}h 不足 {hours} 小时，无法累计")
    xs, ys, total = _field(run, lead - hours + 1, "rain1h", bounds)
    total = total.copy()
    for hour in range(lead - hours + 2, lead + 1):
        total += _field(run, hour, "rain1h", bounds)[2]
    return xs, ys, total


def _smooth(values, size):
    """Box-filter only for drawing isobars; shading keeps every native cell."""
    pad = size // 2
    padded = np.pad(values, pad, mode="edge")
    total = np.cumsum(np.cumsum(padded, axis=0), axis=1)
    total = np.pad(total, ((1, 0), (1, 0)))
    window = total[size:, size:] - total[:-size, size:] - total[size:, :-size] + total[:-size, :-size]
    return window / (size * size)


def _rh(t, td):
    return np.clip(100 * np.exp(17.625 * td / (243.04 + td) - 17.625 * t / (243.04 + t)), 0, 100)


@lru_cache(maxsize=1)
def _geography():
    return json.loads(MAP_GEOGRAPHY.read_text())


@lru_cache(maxsize=1)
def _boundaries():
    """Use Chinese province lines and national outline with foreign borders."""
    from cartopy.io import shapereader
    from shapely.geometry import box, LineString
    china = box(*REGIONS["china"][1]).buffer(15)
    geography = _geography()
    coast = shapereader.Reader(shapereader.natural_earth("10m", "physical", "coastline"))

    def lines(names):
        geometries = (LineString(line) for name in names for line in geography[name])
        return [g for g in geometries if g.intersects(china)]
    return {
        "coast": [g for g in coast.geometries() if g.intersects(china)],
        "country": lines(("borders", "foreignBorders")),
        "province": lines(("provinces",)),
        "claims": lines(("claims",)),
    }


def _draw_boundaries(ax, crs, region):
    lines = _boundaries()
    ax.add_geometries(lines["coast"], crs, facecolor="none", edgecolor="black", linewidth=.6, zorder=4)
    ax.add_geometries(lines["country"], crs, facecolor="none", edgecolor="black", linewidth=.8, zorder=4)
    ax.add_geometries(lines["province"], crs, facecolor="none", edgecolor="#333333",
                      linewidth=.45 if region == "china" else .6, zorder=4)
    ax.add_geometries(lines["claims"], crs, facecolor="none", edgecolor="black",
                      linewidth=1, zorder=4.5)


def _draw_seas(ax, crs, region):
    west, south, east, north = REGIONS[region][1]
    ax.apply_aspect()
    renderer = ax.figure.canvas.get_renderer()
    frame = ax.get_window_extent(renderer).padded(-3)
    blocked = []
    for place in _geography()["seas"]:
        x, y = place.get("positions", {}).get(region, (place["lon"], place["lat"]))
        if not (west < x < east and south < y < north):
            continue
        label = ax.text(x, y, place["name"], transform=crs, ha="center", va="center",
                        fontsize=place["size"], color="#42677c", fontweight="medium",
                        rotation=place.get("rotation", 0), zorder=8, clip_on=True,
                        path_effects=[path_effects.withStroke(linewidth=2.3, foreground="white")])
        label_box = label.get_window_extent(renderer).padded(2)
        if (frame.contains(label_box.x0, label_box.y0) and frame.contains(label_box.x1, label_box.y1)
                and not any(label_box.overlaps(other) for other in blocked)):
            blocked.append(label_box)
        else:
            label.remove()
    return blocked


@lru_cache(maxsize=1)
def _city_places():
    return json.loads(CITY_PLACES.read_text())["places"]


def _draw_cities(ax, crs, region, blocked=()):
    """Keep major anchors, then fill the largest gaps between city markers."""
    west, south, east, north = REGIONS[region][1]
    candidates = [place for place in _city_places()
                  if west < place["lon"] < east and south < place["lat"] < north]
    if region == "china":
        candidates = [place for place in candidates
                      if place["priority"] or place["tier"] == 0]
    limit = {"china": 65, "southern_china": 64, "south": 45}.get(region, 35)
    spacing = {"china": 48, "southern_china": 48, "south": 42}.get(region, 38)
    offsets = ((4, 4, "left", "bottom"), (-4, 4, "right", "bottom"),
               (4, -4, "left", "top"), (-4, -4, "right", "top"),
               (0, 10, "center", "bottom"), (0, -10, "center", "top"),
               (12, 0, "left", "center"), (-12, 0, "right", "center"),
               (4, 16, "left", "bottom"), (-4, -16, "right", "top"))
    ax.apply_aspect()
    renderer = ax.figure.canvas.get_renderer()
    frame = ax.get_window_extent(renderer).padded(-3)
    blocked = list(blocked)
    transform = crs._as_mpl_transform(ax)
    positions = {id(place): transform.transform((place["lon"], place["lat"]))
                 for place in candidates}
    anchors = sorted((place for place in candidates if place["tier"] == 0),
                     key=lambda place: (not place["priority"], -place["population"], place["name"]))
    remaining = [place for place in candidates if place["tier"] != 0]
    placed = []

    def distance(place):
        point = positions[id(place)]
        return min((float(np.sum((point - other) ** 2)) for other in placed),
                   default=float("inf"))

    while (anchors or remaining) and len(placed) < limit:
        anchor = bool(anchors)
        if anchor:
            place = anchors.pop(0)
        else:
            place = max(remaining, key=lambda item: (
                distance(item) * (1.2 if item["priority"] else 1),
                -item["tier"], item["population"]))
            remaining.remove(place)
            if distance(place) < (spacing * (.75 if place["priority"] else 1)) ** 2:
                continue
        x, y = place["lon"], place["lat"]
        for dx, dy, ha, va in offsets:
            leader = ({"arrowstyle": "-", "color": "#204c72", "linewidth": .5,
                       "shrinkA": 2, "shrinkB": 3} if max(abs(dx), abs(dy)) >= 10 else None)
            label = ax.annotate(place["name"], xy=(x, y), xycoords=crs._as_mpl_transform(ax),
                                xytext=(dx, dy), textcoords="offset points", ha=ha, va=va,
                                fontsize=8.5, fontweight="medium", color="#173f65",
                                zorder=10, annotation_clip=True, arrowprops=leader,
                                path_effects=[path_effects.withStroke(linewidth=2, foreground="white")])
            label.update_positions(renderer)
            box = Text.get_window_extent(label, renderer).padded(2)
            inside = (frame.contains(box.x0, box.y0) and frame.contains(box.x1, box.y1))
            if inside and not any(box.overlaps(other) for other in blocked):
                blocked.append(box)
                placed.append(positions[id(place)])
                ax.plot(x, y, marker="o", markersize=3.2, markerfacecolor="#173f65",
                        markeredgecolor="white", markeredgewidth=.65, linestyle="none",
                        transform=crs, zorder=9)
                break
            label.remove()
    return blocked


def _scalar_style(product, values, region):
    if product == "t2m" or product == "td2m":
        levels = (list(range(-10, 41, 2))
                  if region in SOUTHERN_TEMPERATURE_REGIONS
                  else list(range(-40, 44, 4)))
        cmap = plt.get_cmap("turbo", len(levels) + 1)
        return levels, ListedColormap(cmap(np.arange(len(levels) + 1))), "℃"
    if product == "rh2m":
        levels = [10, 20, 30, 40, 50, 60, 70, 80, 90, 95]
        cmap = plt.get_cmap("YlGnBu", len(levels) + 1)
        return levels, ListedColormap(cmap(np.arange(len(levels) + 1))), "%"
    if product == "wind10":
        levels = [1, 3.4, 5.5, 8, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7]  # Beaufort 1–12
        colors = ["#ffffff", "#e0f3f8", "#abd9e9", "#74add1", "#4575b4", "#91cf60", "#fee08b",
                  "#fdae61", "#f46d43", "#d73027", "#a50026", "#67001f"]
        return levels, ListedColormap(colors), "m/s"
    if product == "dt24":
        levels = [-12, -8, -6, -4, -2, -1, 1, 2, 4, 6, 8, 12]
        cmap = plt.get_cmap("RdBu_r", len(levels) + 1)
        colors = cmap(np.arange(len(levels) + 1))
        colors[len(levels) // 2] = (1, 1, 1, 1)
        return levels, ListedColormap(colors), "℃"
    raise ValueError(product)


def _thermodynamic_fields(run, bounds, requests):
    """Prefer the station head; keep all inputs on the same original grid."""
    for prefix in ("station", "surface"):
        try:
            fields = [_field(run, lead, f"{prefix}_{suffix}", bounds)
                      for lead, suffix in requests]
        except FileNotFoundError:
            if prefix == "surface":
                raise
            continue
        xs, ys = fields[0][:2]
        if any(not np.array_equal(xs, field[0]) or not np.array_equal(ys, field[1])
               for field in fields[1:]):
            raise ValueError("温露点计算需要相同的原生格点")
        return xs, ys, [field[2] for field in fields]


def compute(run, lead, product, bounds):
    """Return (xs, ys, shaded values, extras dict, label for shading, accumulation hours)."""
    extras = {}
    if product.startswith("rain"):
        hours = int(product[4:])
        xs, ys, values = _rain(run, lead, hours, bounds)
        return xs, ys, values, extras, hours
    if product == "mslp":
        xs, ys, p = _field(run, lead, "surface_msl", bounds)
        extras["wind"] = (_field(run, lead, "surface_u", bounds)[2], _field(run, lead, "surface_v", bounds)[2])
        extras["rain"] = _field(run, lead, "rain1h", bounds)[2]
        return xs, ys, p, extras, None
    if product == "wind10":
        xs, ys, u = _field(run, lead, "surface_u", bounds)
        v = _field(run, lead, "surface_v", bounds)[2]
        extras["wind"] = (u, v)
        return xs, ys, np.hypot(u, v), extras, None
    if product in ("t2m", "td2m", "rh2m", "dt24"):
        requests = [(lead, "td" if product == "td2m" else "t")]
        if product == "rh2m":
            requests.append((lead, "td"))
        elif product == "dt24":
            if lead <= 24:
                raise FileNotFoundError("24小时变温需要预报时效大于 24 小时")
            requests.append((lead - 24, "t"))
        xs, ys, fields = _thermodynamic_fields(run, bounds, requests)
        values = (_rh(*fields) if product == "rh2m"
                  else fields[0] - fields[1] if product == "dt24" else fields[0])
        return xs, ys, values, extras, None
    raise ValueError("未知产品")


def _rain_label_points(xs, ys, values):
    """Space labels evenly while keeping their original grid coordinates."""
    stride = max(1, int(np.ceil(max(len(xs) / 24, len(ys) / 16))))
    margin = max(1, stride // 2)
    for row in range(margin, len(ys) - margin, stride):
        for column in range(margin, len(xs) - margin, stride):
            value = float(values[row, column])
            if np.isfinite(value) and value >= 0.1:
                yield xs[column], ys[row], value


def _draw_rain_values(ax, crs, xs, ys, values, blocked=()):
    renderer = ax.figure.canvas.get_renderer()
    for x, y, value in _rain_label_points(xs, ys, values):
        label = f"{value:.1f}".rstrip("0").rstrip(".")
        text = ax.text(x, y, label, transform=crs, ha="center", va="center",
                       fontsize=7.5, color="#111111", zorder=7, clip_on=True,
                       path_effects=[path_effects.withStroke(linewidth=1, foreground="white")])
        if any(text.get_window_extent(renderer).padded(1).overlaps(box) for box in blocked):
            text.remove()


def render_png(run, lead, product, region, rain_values=True):
    import cartopy.crs as ccrs
    region_name, bounds = REGIONS[region]
    xs, ys, values, extras, hours = compute(run, lead, product, bounds)
    crs = ccrs.PlateCarree()
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    fig = plt.figure(figsize=(8.6, max(5.2, 8.6 * height / width * .86 + 1.6)), dpi=110)
    ax = fig.add_axes([.08, .2, .88, .72], projection=crs)
    ax.set_extent((bounds[0], bounds[2], bounds[1], bounds[3]), crs=crs)
    ax.set_facecolor("white")
    dx, dy = float(xs[1] - xs[0]), float(ys[1] - ys[0])
    grid_degrees = round(dx, 6)
    extent = (xs[0] - dx / 2, xs[-1] + dx / 2,
              ys[0] - dy / 2, ys[-1] + dy / 2)

    if hours is not None:
        levels = RAIN_LEVELS[hours]
        cmap = ListedColormap(RAIN_COLORS[:len(levels) - 1])
        cmap.set_under((1, 1, 1, 0))
        cmap.set_over(RAIN_COLORS[len(levels) - 1])
        norm = BoundaryNorm(levels, cmap.N)
        image = ax.imshow(np.ma.masked_less(values, levels[0]), extent=extent, origin="lower",
                          interpolation="nearest", cmap=cmap, norm=norm, transform=crs, zorder=1)
        unit = "mm"
        shade_label = f"{hours}hr ACCP ( mm shaded )"
        extend = "max"
    elif product == "mslp":
        unit = "hPa（等值线）/ mm（色斑）"
        rain = extras["rain"]
        levels = RAIN_LEVELS[1]
        cmap = ListedColormap(RAIN_COLORS[:len(levels) - 1])
        cmap.set_over(RAIN_COLORS[len(levels) - 1])
        image = ax.imshow(np.ma.masked_less(rain, levels[0]), extent=extent, origin="lower",
                          interpolation="nearest", cmap=cmap, norm=BoundaryNorm(levels, cmap.N),
                          transform=crs, zorder=1, alpha=.85)
        step = 2.5 if region != "china" else 4
        contour = ax.contour(xs, ys, _smooth(values, 7), levels=np.arange(900, 1080, step), colors="#1f3a93",
                             linewidths=.8, transform=crs, zorder=3)
        ax.clabel(contour, fontsize=7, fmt="%g")
        shade_label = "MSLP ( hPa ) + 1hr ACCP ( mm ) + 10m wind"
        extend = "max"
    else:
        levels, cmap, unit = _scalar_style(product, values, region)
        norm = BoundaryNorm(levels, cmap.N, extend="both")
        image = ax.imshow(values, extent=extent, origin="lower", interpolation="nearest",
                          cmap=cmap, norm=norm, transform=crs, zorder=1)
        shade_label = f"{PRODUCTS[product]} ( {unit} shaded )"
        extend = "both"

    if "wind" in extras:
        u, v = extras["wind"]
        stride = max(1, int(round(max(width, height) / grid_degrees / 26)))
        sl = (slice(stride // 2, None, stride), slice(stride // 2, None, stride))
        # Standard station-model barbs: half barb 2 m/s, full barb 4 m/s, flag 20 m/s.
        ax.barbs(xs[sl[1]], ys[sl[0]], u[sl], v[sl], length=4.6, linewidth=.45,
                 barb_increments={"half": 2, "full": 4, "flag": 20},
                 color="#222222", transform=crs, zorder=5)

    _draw_boundaries(ax, crs, region)
    sea_boxes = _draw_seas(ax, crs, region)
    city_boxes = _draw_cities(ax, crs, region, blocked=sea_boxes)
    if hours is not None and rain_values:
        _draw_rain_values(ax, crs, xs, ys, values, blocked=city_boxes)
    grid = ax.gridlines(crs=crs, draw_labels=True, linewidth=0, alpha=0)
    grid.top_labels = grid.right_labels = False
    grid.xlabel_style = grid.ylabel_style = {"size": 9}

    valid = run + timedelta(hours=lead)
    start = valid - timedelta(hours=hours) if hours else None
    period = (f"{(start + BJT):%Y%m%d%H} - {(valid + BJT):%Y%m%d%H} BJT" if hours
              else f"{(valid + BJT):%Y%m%d%H} BJT")
    fig.text(.08, .935, f"{shade_label} : {period}", fontsize=10, ha="left", va="bottom")
    fig.text(.96, .968, MODEL_LABEL, fontsize=9, ha="right", va="bottom", color="#333333")
    fig.text(.08, .1, f"{run:%Y%m%d%H}UTC  +{lead}hr\n{(run + BJT):%Y%m%d%H}BJT  +{lead}hr",
             fontsize=9, ha="left", va="top")
    fig.text(.96, .1, f"{valid:%Y%m%d%H}UTC\n{(valid + BJT):%Y%m%d%H}BJT", fontsize=9, ha="right", va="top")
    cax = fig.add_axes([.32, .11, .4, .025])
    if product in ("t2m", "td2m") and region in SOUTHERN_TEMPERATURE_REGIONS:
        ticks = list(range(-10, 41, 5))
    else:
        ticks = levels[::2] if len(levels) > 12 else levels
    bar = fig.colorbar(image, cax=cax, orientation="horizontal", extend=extend, ticks=ticks,
                       spacing="uniform", drawedges=True)
    bar.ax.tick_params(labelsize=8, length=0)
    bar.ax.set_xticklabels([f"{x:g}" for x in ticks])
    bar.outline.set_linewidth(.5)
    bar.dividers.set_linewidth(.4)
    source_label = " · 气象站观测训练" if grid_degrees == .05 else ""
    resolution_km = 5 if grid_degrees == .05 else 10
    fig.text(.52, .045, f"{region_name} · 原生分辨率约 {resolution_km} km{source_label} · 单位 {unit}",
             fontsize=8, ha="center", color="#555555")

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=110)
    plt.close(fig)
    return buffer.getvalue()


def cached_png(run_value, lead, product, region, rain_values=True):
    run = parse_run(run_value)
    if product not in PRODUCTS:
        raise ValueError("未知产品")
    if region not in REGIONS:
        raise ValueError("未知区域")
    _, coverage = _run_source(run)
    if region not in _covered_regions(coverage):
        raise ValueError("所选区域超出该起报资料的覆盖范围")
    if not 1 <= lead <= 360:
        raise ValueError("预报时效超出范围")
    suffix = "_values" if product.startswith("rain") and rain_values else ""
    target = CACHE / STYLE_VERSION / run_id(run) / region / f"{product}_f{lead:03d}{suffix}.png"
    if target.is_file():
        return target.read_bytes()
    with RENDER_LOCK:
        if target.is_file():
            return target.read_bytes()
        content = render_png(run, lead, product, region, rain_values=rain_values)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False, suffix=".tmp") as handle:
            handle.write(content)
            temporary = Path(handle.name)
        os.replace(temporary, target)
    return content


def prune_cache(keep):
    """Drop rendered images of runs that the upstream cache no longer retains."""
    import shutil
    base = CACHE / STYLE_VERSION
    if base.is_dir():
        for folder in base.iterdir():
            if folder.is_dir() and folder.name not in keep:
                shutil.rmtree(folder, ignore_errors=True)


def catalog():
    runs = []
    items = available_runs()
    prune_cache({item["id"] for item in items})
    for item in items:
        run = parse_run(item["id"])
        runs.append({**item, "run_utc": run.strftime("%Y-%m-%dT%H:%MZ"),
                     "run_bjt": (run + BJT).strftime("%Y-%m-%d %H:%M")})
    queue = []
    try:
        queued = json.loads((ROOT / "backfill-queue.json").read_text()).get("runs", [])
        for item in queued:
            run = parse_run(item["id"])
            queue.append({"id": item["id"], "status": item["status"],
                          "run_bjt": (run + BJT).strftime("%Y-%m-%d %H:%M")})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"model": MODEL_LABEL, "runs": runs, "backfill_queue": queue, "map_version": STYLE_VERSION,
            "regions": {k: v[0] for k, v in REGIONS.items()}, "products": PRODUCTS}


if __name__ == "__main__":
    print(json.dumps(catalog(), ensure_ascii=False)[:400])
