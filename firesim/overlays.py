"""Classified rasters shipped alongside the example fires (good wildfire, readiness).

These are effects layers produced outside this repo: single-band uint8 GeoTIFFs in EPSG:4326 at
30 m, where 0 means "not classified" and every other value is a class. We only read, colour and
display them — nothing here recomputes or reinterprets the classification.
"""

import numpy as np
import rasterio

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
