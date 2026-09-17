"""Render pyroSim runs as maps: a static PNG (for the chat) and an interactive folium HTML.

Both read only what the CLI wrote: the hours-before-burn GeoTIFF and the run record. Colors show
hours after ignition on one shared scale, so runs with different horizons stay comparable.
"""

import base64
import io
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import branca.colormap
import folium
import matplotlib

matplotlib.use("Agg")  # headless: tools run inside a server
import numpy as np
import rasterio
import requests
from matplotlib import colormaps, colors
from matplotlib import pyplot as plt
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # firesim lives at the repo root, next to agents/
    sys.path.insert(0, str(REPO_ROOT))

from firesim import observed as observed_fires, severity  # noqa: E402

NODATA = -999.0
CMAP = "YlOrRd_r"  # early arrival = dark red, late arrival = pale yellow
ESRI_IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ESRI_ATTRIBUTION = "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"
TILE_SIZE = 256
MAX_TILES = 64


def read_hours(tif_path) -> np.ndarray:
    """Hours-before-burn grid with NaN where the cell never burns."""
    with rasterio.open(tif_path) as dataset:
        hours = dataset.read(1).astype("float32")
    hours[hours == NODATA] = np.nan
    return hours


def read_band(tif_path, name: str) -> np.ndarray:
    """One exported band by its description, NaN where the cell never burns."""
    with rasterio.open(tif_path) as dataset:
        if name not in (dataset.descriptions or ()):
            raise ValueError(f"{tif_path} has no band {name!r} (has {dataset.descriptions}).")
        values = dataset.read(dataset.descriptions.index(name) + 1).astype("float32")
    values[values == NODATA] = np.nan
    return values


def _checkpoint_levels(max_hours: float) -> list[int]:
    levels = [6, 12] + list(range(24, int(max_hours) + 1, 24))
    return [h for h in levels if h <= max_hours]


# --- Basemap tiles for the static PNG ---

def _tile_xy(lon, lat, zoom):
    n = 2**zoom
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return x, y


def _fetch_tile(z, x, y):
    response = requests.get(ESRI_IMAGERY.format(z=z, x=x, y=y), timeout=15,
                            headers={"User-Agent": "pyrosim-agent"})
    response.raise_for_status()
    return Image.open(io.BytesIO(response.content)).convert("RGB")


def fetch_basemap(bounds, target_px=900):
    """Esri imagery cropped to bounds (west, south, east, north), or None if unavailable."""
    west, south, east, north = bounds
    zoom = int(np.clip(math.floor(math.log2(target_px * 360.0 / (TILE_SIZE * (east - west)))), 1, 17))
    while True:
        x0, y0 = _tile_xy(west, north, zoom)
        x1, y1 = _tile_xy(east, south, zoom)
        tiles = [(tx, ty) for tx in range(int(x0), int(x1) + 1) for ty in range(int(y0), int(y1) + 1)]
        if len(tiles) <= MAX_TILES or zoom <= 1:
            break
        zoom -= 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        images = dict(zip(tiles, pool.map(lambda t: _fetch_tile(zoom, *t), tiles)))
    mosaic = Image.new("RGB", ((int(x1) - int(x0) + 1) * TILE_SIZE, (int(y1) - int(y0) + 1) * TILE_SIZE))
    for (tx, ty), image in images.items():
        mosaic.paste(image, ((tx - int(x0)) * TILE_SIZE, (ty - int(y0)) * TILE_SIZE))
    crop = tuple(round(v) for v in ((x0 - int(x0)) * TILE_SIZE, (y0 - int(y0)) * TILE_SIZE,
                                    (x1 - int(x0)) * TILE_SIZE, (y1 - int(y0)) * TILE_SIZE))
    return np.asarray(mosaic.crop(crop))


# --- Static PNG ---

def render_png(runs, path, max_hours, observed=None):
    """One panel per run: basemap, arrival hours, checkpoint perimeters, ignition, AOI.

    `runs` is a list of run records (with output_path); `observed` is a list of observed-perimeter
    features (firesim.observed.perimeter_features) drawn as cyan outlines. Returns a basemap note.
    """
    ncols = min(len(runs), 2)
    nrows = math.ceil(len(runs) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(7 * ncols + 1, 6.2 * nrows), squeeze=False,
                             layout="constrained")
    norm = colors.Normalize(vmin=0.0, vmax=max_hours)
    basemap_note = "Esri World Imagery"
    basemaps = {}

    for ax, run in zip(axes.flat, runs):
        west, south, east, north = run["scenario"]["aoi_bounds"]
        extent = (west, east, south, north)
        key = (west, south, east, north)
        if key not in basemaps:
            try:
                basemaps[key] = fetch_basemap(key)
            except Exception as error:  # network or tile errors: draw without a basemap
                basemaps[key] = None
                basemap_note = f"basemap unavailable ({type(error).__name__})"
        if basemaps[key] is not None:
            ax.imshow(basemaps[key], extent=extent, interpolation="bilinear")
        else:
            ax.set_facecolor("#e8e4da")

        hours = read_hours(run["output_path"])
        ax.imshow(hours, extent=extent, cmap=CMAP, norm=norm, alpha=0.75, interpolation="nearest")
        levels = [h for h in _checkpoint_levels(max_hours) if np.nanmax(np.nan_to_num(hours, nan=-1)) >= h]
        if levels:
            lons = np.linspace(west, east, hours.shape[1])
            lats = np.linspace(north, south, hours.shape[0])
            contours = ax.contour(lons, lats, np.nan_to_num(hours, nan=1e6), levels=levels,
                                  colors="white", linewidths=0.9)
            ax.clabel(contours, fmt=lambda h: f"{h:.0f} h", fontsize=7, colors="white")

        labelled = set()
        for index, feature in enumerate(observed or []):
            last = index == len(observed) - 1
            shade = colormaps["cool"](index / max(len(observed) - 1, 1))  # cyan (first) -> magenta (last)
            style = {"linewidth": 2.0, "alpha": 0.95} if last else {"linewidth": 1.0, "alpha": 0.8}
            day = feature["hours_after_start"] / 24.0
            kind = "final" if last else "earlier"
            for ring in _exterior_rings(feature["geojson"]):
                label = None if kind in labelled else (
                    f"observed final, day {day:.1f} ({feature['acres']:,.0f} ac)" if last
                    else f"observed by day (first: {feature['acres']:,.0f} ac)")
                labelled.add(kind)
                ax.plot(*zip(*ring), color=shade, label=label, **style)
            top = max(_exterior_rings(feature["geojson"]), key=lambda ring: max(y for _, y in ring))
            x, y = max(top, key=lambda point: point[1])
            ax.annotate(f"d{day:.0f}", (x, y), color=shade, fontsize=6, fontweight="bold",
                        ha="center", va="bottom")
        if observed:
            ax.legend(loc="lower left", fontsize=7, framealpha=0.6)

        ax.add_patch(plt.Rectangle((west, south), east - west, north - south,
                                   fill=False, edgecolor="#3388ff", linewidth=1.5))
        lon, lat = run["scenario"]["ignition_lonlat"]
        ax.plot(lon, lat, marker="*", markersize=16, color="red", markeredgecolor="white")

        summary = run.get("summary", {})
        title = run.get("label") or run["run_id"]
        mode = "MOCK" if run.get("mode") == "mock" else "real"
        ax.set_title(f"{title}\n{summary.get('burned_hectares', 0):,.0f} ha burned · "
                     f"{run['scenario']['projection_days']} d · {run.get('weather_source_used')} · {mode}",
                     fontsize=10)
        ax.set_xlim(west, east)
        ax.set_ylim(south, north)
        ax.set_aspect(1.0 / math.cos(math.radians((south + north) / 2.0)))
        ax.tick_params(labelsize=7)
        if run.get("mode") == "mock":
            ax.text(0.5, 0.5, "MOCK", transform=ax.transAxes, ha="center", va="center",
                    fontsize=48, color="white", alpha=0.35, fontweight="bold")

    for ax in list(axes.flat)[len(runs):]:
        ax.axis("off")
    colorbar = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=CMAP), ax=axes, shrink=0.8)
    colorbar.set_label("Hours after ignition")
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return basemap_note


def _exterior_rings(geometry):
    """Exterior rings of a (Multi)Polygon GeoJSON geometry as lists of (lon, lat)."""
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    return [[(x, y) for x, y, *_ in polygon[0]] for polygon in polygons]


# --- Interactive HTML ---

def build_folium_map(runs, max_hours, until_hours=None, show_all=False, height=None, observed=None,
                     standalone=False):
    """Folium map with one toggleable arrival-time layer per run.

    until_hours: only draw cells that have burned by this many hours after ignition (time slider).
    show_all: show every run layer at once instead of only the first.
    standalone: build a self-contained map for saving as HTML — embeds the imagery as an image
        instead of tiles (tile servers refuse requests from file:// pages), and adds the
        severity layer, day-by-day fronts and a summary panel.
    """
    all_bounds = np.array([run["scenario"]["aoi_bounds"] for run in runs])
    west, south = all_bounds[:, 0].min(), all_bounds[:, 1].min()
    east, north = all_bounds[:, 2].max(), all_bounds[:, 3].max()

    size = {"height": height} if height else {}
    fmap = folium.Map(location=[(south + north) / 2, (west + east) / 2], tiles=None,
                      control_scale=True, **size)
    if standalone:
        _add_embedded_basemap(fmap, (west, south, east, north))
    else:
        folium.TileLayer(ESRI_IMAGERY, attr=ESRI_ATTRIBUTION, name="Aerial imagery").add_to(fmap)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(fmap)

    norm = colors.Normalize(vmin=0.0, vmax=max_hours)
    cmap = colormaps[CMAP]
    for index, run in enumerate(runs):
        w, s, e, n = run["scenario"]["aoi_bounds"]
        hours = read_hours(run["output_path"])
        if until_hours is not None:
            hours = np.where(hours <= until_hours, hours, np.nan)
        rgba = cmap(norm(np.nan_to_num(hours, nan=0.0)))
        rgba[..., 3] = np.where(np.isfinite(hours), 0.75, 0.0)
        label = run.get("label") or run["run_id"]
        summary = run.get("summary", {})
        group = folium.FeatureGroup(
            name=f"{label} ({summary.get('burned_hectares', 0):,.0f} ha)", show=show_all or index == 0
        )
        folium.raster_layers.ImageOverlay(
            image=(rgba * 255).astype("uint8"), bounds=[[s, w], [n, e]], mercator_project=True,
        ).add_to(group)
        folium.Rectangle([[s, w], [n, e]], color="#3388ff", fill=False, weight=2).add_to(group)
        lon, lat = run["scenario"]["ignition_lonlat"]
        folium.Marker(
            [lat, lon],
            tooltip=(f"{label}: ignition {run['scenario']['ignition_date']}, "
                     f"{run['scenario']['projection_days']} d, {run.get('weather_source_used')}, "
                     f"{run.get('mode')} · {run['run_id']}"),
            icon=folium.Icon(color="red", icon="fire", prefix="fa"),
        ).add_to(group)
        group.add_to(fmap)

        if standalone:
            _add_daily_fronts(fmap, run, label)
            _add_severity_layer(fmap, run, label)

    add_observed_layer(fmap, observed, until_hours)
    if standalone:
        _add_summary_panel(fmap, runs, observed)

    legend = branca.colormap.LinearColormap(
        [colors.to_hex(cmap(v)) for v in np.linspace(0, 1, 8)], vmin=0, vmax=max_hours,
        caption="Hours after ignition",
    )
    legend.add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.fit_bounds([[south, west], [north, east]])
    return fmap


def observed_features(fire: str) -> list[dict]:
    """Observed perimeters of an example fire, ready for the map overlays."""
    return observed_fires.perimeter_features(fire)


def add_observed_layer(fmap, observed, until_hours=None) -> None:
    """Add one toggleable layer per observed perimeter, coloured by time.

    until_hours: hide observations made after that many hours, so the map matches the simulated
    time slider (the observed fire as it was known at that point).
    """
    if not observed:
        return
    shown = [f for f in observed if until_hours is None or f["hours_after_start"] <= until_hours]
    for index, feature in enumerate(observed):
        if feature not in shown:
            continue
        last = index == len(observed) - 1
        color = colors.to_hex(colormaps["cool"](index / max(len(observed) - 1, 1)))
        day = feature["hours_after_start"] / 24.0
        group = folium.FeatureGroup(
            name=f"Observed day {day:.1f} ({feature['acres']:,.0f} ac)",
            show=last or feature is shown[-1],
        )
        folium.GeoJson(
            feature["geojson"],
            style_function=lambda _, color=color, last=last: {
                "color": color, "weight": 3 if last else 2, "fill": False,
                "dashArray": None if last else "5,4", "opacity": 0.95,
            },
            tooltip=f"Observed {feature['label']} (+{feature['hours_after_start']:.0f} h)",
        ).add_to(group)
        group.add_to(fmap)


def _png_data_uri(rgb: np.ndarray) -> str:
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _add_embedded_basemap(fmap, bounds) -> None:
    """Imagery as one embedded picture, so the saved HTML needs no tile server."""
    west, south, east, north = bounds
    try:
        image = fetch_basemap(bounds)
    except Exception:
        return
    folium.raster_layers.ImageOverlay(
        image=_png_data_uri(image), bounds=[[south, west], [north, east]],
        name="Aerial imagery (embedded)", overlay=False, show=True,
    ).add_to(fmap)


def _add_daily_fronts(fmap, run, label) -> None:
    """One layer per simulated day: everything burned by the end of that day."""
    w, s, e, n = run["scenario"]["aoi_bounds"]
    hours = read_hours(run["output_path"])
    days = run["scenario"]["projection_days"]
    shades = colormaps[CMAP]
    for day in range(1, days + 1):
        burned_by = np.isfinite(hours) & (hours <= day * 24)
        if not burned_by.any():
            continue
        rgba = np.zeros((*hours.shape, 4), dtype="uint8")
        rgba[..., :3] = (np.array(colors.to_rgb(shades((day - 1) / max(days - 1, 1)))) * 255).astype("uint8")
        rgba[..., 3] = np.where(burned_by, 190, 0)
        group = folium.FeatureGroup(name=f"{label}: burned by day {day}", show=False)
        folium.raster_layers.ImageOverlay(image=rgba, bounds=[[s, w], [n, e]],
                                          mercator_project=True).add_to(group)
        group.add_to(fmap)


def _add_severity_layer(fmap, run, label) -> None:
    """Flame length in operational bands: green / yellow / orange / red."""
    try:
        flame_m = read_band(run["output_path"], "flame_length_m")
    except (ValueError, KeyError):
        return  # run predates the multi-band export
    flame_ft = flame_m * severity.FEET_PER_METRE
    palette = ["#2ca25f", "#fed976", "#fd8d3c", "#e31a1c"]
    rgba = np.zeros((*flame_ft.shape, 4), dtype="uint8")
    for (lower, upper, _, _), color in zip(severity.FLAME_LENGTH_BANDS, palette):
        in_band = np.isfinite(flame_ft) & (flame_ft >= lower) & (flame_ft < upper)
        rgba[in_band, :3] = (np.array(colors.to_rgb(color)) * 255).astype("uint8")
        rgba[in_band, 3] = 200
    w, s, e, n = run["scenario"]["aoi_bounds"]
    group = folium.FeatureGroup(name=f"{label}: flame length bands", show=False)
    folium.raster_layers.ImageOverlay(image=rgba, bounds=[[s, w], [n, e]],
                                      mercator_project=True).add_to(group)
    group.add_to(fmap)


def _add_summary_panel(fmap, runs, observed=None) -> None:
    """Fixed panel with headline numbers, flame-length bands and provenance."""
    blocks = []
    for run in runs:
        summary = run.get("summary", {})
        scenario = run["scenario"]
        bands = (summary.get("severity") or {}).get("flame_length_bands", [])
        band_html = "".join(
            f"<div><span style='display:inline-block;width:11px;height:11px;background:{color};"
            f"margin-right:5px'></span>{band['band']}: {band['fraction_of_burned']:.0%}</div>"
            for band, color in zip(bands, ["#2ca25f", "#fed976", "#fd8d3c", "#e31a1c"])
            if band["fraction_of_burned"]
        )
        crown = summary.get("passive_crown_cells", 0) + summary.get("active_crown_cells", 0)
        blocks.append(
            f"<div style='margin-bottom:8px'><b>{run.get('label') or run['run_id']}</b>"
            f"{' <span style=\'color:#b36b00\'>MOCK</span>' if run.get('mode') == 'mock' else ''}<br>"
            f"{summary.get('burned_hectares', 0):,.0f} ha ({summary.get('burned_acres', 0):,.0f} ac)"
            f" · {scenario['projection_days']} d<br>"
            f"max flame {summary.get('max_flame_length_m', 0):.1f} m · crown {crown:,} cells<br>"
            f"{band_html}"
            f"<span style='color:#555'>{scenario['ignition_date']} · weather "
            f"{run.get('weather_source_used')} · fuels {run.get('fuel_source_used', '-')} · "
            f"{summary.get('cell_size_m', 0):.0f} m cells<br>{run['run_id']}</span></div>"
        )
    if observed:
        blocks.append(
            f"<div style='border-top:1px solid #ccc;padding-top:5px'><b>Observed</b><br>"
            f"{observed[0]['acres']:,} ac at first mapping → {observed[-1]['acres']:,} ac after "
            f"{observed[-1]['hours_after_start'] / 24:.1f} days<br>"
            f"<span style='color:#555'>Suppression is not modeled, so the simulation is expected to "
            f"overpredict.</span></div>"
        )
    html = (
        "<div style='position:fixed;top:12px;right:12px;z-index:9999;background:rgba(255,255,255,0.93);"
        "padding:10px 12px;border-radius:6px;border:1px solid #bbb;font:12px/1.35 system-ui,sans-serif;"
        "max-width:290px;max-height:78vh;overflow:auto'>" + "".join(blocks) + "</div>"
    )
    fmap.get_root().html.add_child(folium.Element(html))


def render_html(runs, path, max_hours, observed=None):
    """Save a self-contained interactive map: embedded imagery, arrival time, daily fronts,
    flame-length bands, observed perimeters by day, and a summary panel."""
    build_folium_map(runs, max_hours, observed=observed, standalone=True).save(str(path))
