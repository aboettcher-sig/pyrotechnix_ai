"""Classified rasters shipped alongside the example fires (good wildfire, readiness).

These are effects layers produced outside this repo: single-band uint8 GeoTIFFs in EPSG:4326 at
30 m, where 0 means "not classified" and every other value is a class. We only read, colour and
display them — nothing here recomputes or reinterprets the classification.
"""

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

from . import severity
from .observed import DATA_DIR

MAX_OVERLAY_PIXELS = 1400  # decimate wide rasters for the web map; classes use nearest neighbour

# Palettes and labels come from the layer's authors; keep the numbering exactly as supplied.
OVERLAY_TYPES = {
    "good_fire": {
        "suffix": "_gwf_clip.tif",
        "name": "Good wildfire",
        "classes": {
            1: ("Low severity good wildfire", "#fa6ee2"),
            2: ("High severity good wildfire", "#dc0ab4"),
            3: ("High severity, low severity regime", "#ffd700"),   # gold
            4: ("Low severity, high severity regime", "#daa520"),   # goldenrod
            5: ("Too frequent", "#b22222"),                          # firebrick
        },
    },
}


def overlay_path(fire: str, kind: str):
    """Path of one classified raster for a fire, or None when that fire has no such layer."""
    spec = OVERLAY_TYPES.get(kind)
    if spec is None:
        return None
    matches = sorted((DATA_DIR / fire).glob(f"*{spec['suffix']}")) if (DATA_DIR / fire).is_dir() else []
    return matches[0] if matches else None


def available(fire: str) -> list[str]:
    """Which classified overlays exist for this fire."""
    return [kind for kind in OVERLAY_TYPES if overlay_path(fire, kind)]


def load(fire: str, kind: str = "good_fire", max_pixels: int = MAX_OVERLAY_PIXELS) -> dict | None:
    """Return {name, rgba, bounds, legend} for a classified overlay, or None if absent.

    `rgba` is ready for a map image overlay (transparent where unclassified); `legend` carries the
    label, colour and area of each class actually present, so the UI never invents a category.
    """
    path = overlay_path(fire, kind)
    if path is None:
        return None
    spec = OVERLAY_TYPES[kind]

    with rasterio.open(path) as dataset:
        west, south, east, north = dataset.bounds
        cell_ha = abs(dataset.res[0] * dataset.res[1]) * (111320.0**2) / 1e4  # rough, near enough
        full = dataset.read(1)
        scale = max(1, int(np.ceil(max(dataset.width, dataset.height) / max_pixels)))
        classes = dataset.read(
            1, out_shape=(dataset.height // scale, dataset.width // scale),
            resampling=rasterio.enums.Resampling.nearest,
        ) if scale > 1 else full

    rgba = np.zeros((*classes.shape, 4), dtype="uint8")
    legend = []
    for value, (label, color) in spec["classes"].items():
        cells = int(np.count_nonzero(full == value))
        if cells == 0:
            continue
        red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        mask = classes == value
        rgba[mask] = (red, green, blue, 205)
        legend.append({"value": value, "label": label, "color": color,
                       "hectares": round(cells * cell_ha)})

    return {
        "kind": kind,
        "name": spec["name"],
        "fire": fire,
        "rgba": rgba,
        "bounds": [west, south, east, north],
        "legend": legend,
        "source": path.name,
    }


def classes_on_grid(fire: str, kind: str, bounds, shape) -> np.ndarray | None:
    """Resample a classified raster onto a run's grid (nearest neighbour — these are categories).

    bounds is (west, south, east, north) and shape is (rows, cols) of the simulation grid; both
    rasters are EPSG:4326, so this is a straight resample with no reprojection.
    """
    path = overlay_path(fire, kind)
    if path is None:
        return None
    west, south, east, north = bounds
    destination = np.zeros(shape, dtype="uint8")
    transform = rasterio.transform.from_bounds(west, south, east, north, shape[1], shape[0])
    with rasterio.open(path) as source:
        reproject(
            source=rasterio.band(source, 1), destination=destination,
            src_transform=source.transform, src_crs=source.crs,
            dst_transform=transform, dst_crs="EPSG:4326", resampling=Resampling.nearest,
        )
    return destination


def burn_breakdown(classes: np.ndarray, burned: np.ndarray, flame_length_m: np.ndarray,
                   cell_area_ha: float, kind: str = "good_fire") -> list[dict]:
    """Burned area per class, with the flame-length mix inside each class.

    Reports every class of the layer plus an "unclassified" row (value 0), so the rows always add
    up to the simulated burned area — most of these layers are unclassified over most of the map.
    """
    spec = OVERLAY_TYPES[kind]
    total_burned = int(np.count_nonzero(burned))
    flame_ft = flame_length_m * severity.FEET_PER_METRE
    rows = []
    for value, (label, color) in [*spec["classes"].items(), (0, ("Unclassified", "#9e9e9e"))]:
        in_class = burned & (classes == value)
        cells = int(np.count_nonzero(in_class))
        if cells == 0:
            continue
        bands = []
        for lower, upper, band_name, _ in severity.FLAME_LENGTH_BANDS:
            band_cells = int(np.count_nonzero(in_class & (flame_ft >= lower) & (flame_ft < upper)))
            if band_cells:
                bands.append({"band": band_name, "fraction": round(band_cells / cells, 3)})
        rows.append({
            "value": value,
            "label": label,
            "color": color,
            "burned_hectares": round(cells * cell_area_ha, 1),
            "burned_acres": round(cells * cell_area_ha * 2.47105, 1),
            "fraction_of_fire": round(cells / total_burned, 3) if total_burned else 0.0,
            "flame_length_mix": bands,
        })
    return rows
