"""Render pyroSim runs as maps: a static PNG (for the chat) and an interactive folium HTML.

Both read only what the CLI wrote: the hours-before-burn GeoTIFF and the run record. Colors show
hours after ignition on one shared scale, so runs with different horizons stay comparable.
"""

import io
import math
from concurrent.futures import ThreadPoolExecutor

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

def render_png(runs, path, max_hours):
    """One panel per run: basemap, arrival hours, checkpoint perimeters, ignition, AOI.

    `runs` is a list of run records (with output_path). Returns a note about the basemap.
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


# --- Interactive HTML ---

def build_folium_map(runs, max_hours, until_hours=None, show_all=False, height=None):
    """Folium map with one toggleable arrival-time layer per run.

    until_hours: only draw cells that have burned by this many hours after ignition (time slider).
    show_all: show every run layer at once instead of only the first.
    """
    all_bounds = np.array([run["scenario"]["aoi_bounds"] for run in runs])
    west, south = all_bounds[:, 0].min(), all_bounds[:, 1].min()
    east, north = all_bounds[:, 2].max(), all_bounds[:, 3].max()

    size = {"height": height} if height else {}
    fmap = folium.Map(location=[(south + north) / 2, (west + east) / 2], tiles=None,
                      control_scale=True, **size)
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

    legend = branca.colormap.LinearColormap(
        [colors.to_hex(cmap(v)) for v in np.linspace(0, 1, 8)], vmin=0, vmax=max_hours,
        caption="Hours after ignition",
    )
    legend.add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.fit_bounds([[south, west], [north, east]])
    return fmap


def render_html(runs, path, max_hours):
    """Save the interactive map (first run shown, others toggleable) as a standalone HTML file."""
    build_folium_map(runs, max_hours).save(str(path))
