"""Interactive folium map and matplotlib charts for a simulation result."""

import base64
import io

import folium
import numpy as np
from matplotlib import colormaps, colors
from matplotlib import pyplot as plt
from PIL import Image


def _rgba_to_data_uri(rgba_uint8: np.ndarray) -> str:
    image = Image.fromarray(rgba_uint8, mode="RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _time_of_arrival_uri(time_of_arrival: np.ndarray, projection_days: int) -> str:
    """Render time-of-arrival (minutes) to an RGBA PNG colored by arrival day."""
    toa_days = time_of_arrival / 1440.0
    burned = np.isfinite(toa_days)
    norm = colors.Normalize(vmin=0.0, vmax=max(projection_days, 1))
    rgba = colormaps["YlOrRd"](norm(np.nan_to_num(toa_days, nan=0.0)))
    rgba[..., 3] = np.where(burned, 0.8, 0.0)
    return _rgba_to_data_uri((rgba * 255).astype("uint8"))


def _mask_uri(mask: np.ndarray, color=(200, 30, 0), alpha=160) -> str:
    rgba = np.zeros((*mask.shape, 4), dtype="uint8")
    rgba[mask, 0], rgba[mask, 1], rgba[mask, 2], rgba[mask, 3] = (*color, alpha)
    return _rgba_to_data_uri(rgba)


ESRI_WORLD_IMAGERY = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
)


def build_map(config, results, save_html=None) -> folium.Map:
    """Interactive map: AOI, ignition point, arrival-day overlay, daily perimeters, crown fire.

    Pass `save_html="path.html"` to also write the map to a standalone HTML file.
    """
    west, south, east, north = config.aoi_bounds
    bounds = [[south, west], [north, east]]
    fmap = folium.Map(
        location=[(south + north) / 2.0, (west + east) / 2.0],
        zoom_start=11,
        control_scale=True,
        tiles=None,
    )
    folium.TileLayer(
        tiles=ESRI_WORLD_IMAGERY,
        attr="Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
        name="Aerial imagery",
        overlay=False,
        control=True,
    ).add_to(fmap)

    folium.Rectangle(bounds, color="#3388ff", fill=False, weight=2, tooltip="AOI").add_to(fmap)
    ilon, ilat = config.ignition_lonlat
    folium.Marker(
        [ilat, ilon],
        tooltip="Ignition point",
        icon=folium.Icon(color="red", icon="fire", prefix="fa"),
    ).add_to(fmap)

    time_of_arrival = results["matrices"]["time_of_arrival"]
    folium.raster_layers.ImageOverlay(
        image=_time_of_arrival_uri(time_of_arrival, config.projection_days),
        bounds=bounds,
        opacity=0.8,
        name="Time of arrival (days)",
    ).add_to(fmap)

    for day in range(1, config.projection_days + 1):
        mask = np.isfinite(time_of_arrival) & (time_of_arrival <= day * 1440.0)
        if not mask.any():
            continue
        folium.raster_layers.ImageOverlay(
            image=_mask_uri(mask),
            bounds=bounds,
            opacity=0.5,
            name=f"Burned by day {day}",
            show=(day == config.projection_days),
        ).add_to(fmap)

    crown = results["matrices"]["fire_type"] >= 2  # passive (2) or active (3) crown fire
    if crown.any():
        folium.raster_layers.ImageOverlay(
            image=_mask_uri(crown, color=(110, 0, 150), alpha=200),
            bounds=bounds,
            opacity=0.7,
            name="Crown fire",
            show=False,
        ).add_to(fmap)

    folium.LayerControl(collapsed=False).add_to(fmap)

    if save_html:
        fmap.save(str(save_html))
    return fmap


def plot_charts(config, results):
    """Charts: cumulative burned area, flame-length distribution, spread rate over time."""
    matrices = results["matrices"]
    time_of_arrival = matrices["time_of_arrival"]
    scale = results["meta"]["scale"]
    cell_ha = (scale * scale) / 1e4
    burned = np.isfinite(time_of_arrival)

    fig, axes = plt.subplots(1, 3, figsize=(16, 4))

    hours = np.arange(0, config.projection_days * 24 + 1, 6)
    areas = [np.count_nonzero(burned & (time_of_arrival <= h * 60.0)) * cell_ha for h in hours]
    axes[0].plot(hours / 24.0, areas, color="firebrick")
    axes[0].set(xlabel="Days since ignition", ylabel="Burned area (ha)",
                title="Cumulative burned area")

    flame = matrices["flame_length"][burned]
    flame = flame[np.isfinite(flame)]
    if flame.size:
        axes[1].hist(flame, bins=30, color="darkorange")
    axes[1].set(xlabel="Flame length (m)", ylabel="Cells", title="Flame length distribution")

    axes[2].scatter(time_of_arrival[burned] / 1440.0, matrices["spread_rate"][burned],
                    s=3, alpha=0.3, color="teal")
    axes[2].set(xlabel="Arrival time (days)", ylabel="Spread rate (m/min)",
                title="Spread rate over time")

    fig.tight_layout()
    return fig
