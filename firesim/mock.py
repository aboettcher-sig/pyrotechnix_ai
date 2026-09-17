"""Mock simulation for `pyroSim run --mock`: same results shape as `model.run_simulation`.

No Earth Engine and no pyretechnics. Spread is a wind-driven ellipse from the ignition cell,
slowed or sped up by a smooth random "fuel" field. Every scenario gets its own seeded wind,
spread rate and fuel field, so outputs look plausible, differ between scenarios, and are
reproducible. This is NOT fire behavior: use it to build and test tools and agents against
the CLI contract without waiting on real runs.
"""

import hashlib
from math import ceil, cos, floor, radians

import numpy as np
from scipy import ndimage

from .gee import compute_scale
from .model import compute_stats, lonlat_to_rc

MOCK_ECCENTRICITY = 0.8  # 0 = circle; closer to 1 = longer, narrower downwind run


def _scenario_rng(config) -> np.random.Generator:
    key = repr((config.aoi_bounds, config.ignition_lonlat, config.ignition_date, config.weather_source))
    return np.random.default_rng(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))


def run_simulation(config):
    """Return a synthetic result dict with the keys `raster.write_geotiff` and the CLI use."""
    rng = _scenario_rng(config)
    west, south, east, north = config.aoi_bounds
    scale = compute_scale(config.aoi_bounds, config.max_pixels, config.min_scale_m)
    # Same grid Earth Engine returns for crs=EPSG:4326 + scale in meters: square pixels of
    # scale/111319.49 degrees, snapped to a grid anchored at 0,0.
    step = scale / 111319.49
    cols = max(1, ceil(east / step) - floor(west / step))
    rows = max(1, ceil(north / step) - floor(south / step))
    lat = (south + north) / 2.0

    wind_from = rng.uniform(0.0, 360.0)               # deg CW from N, where wind comes from
    head_rate = rng.uniform(0.8, 2.5)                  # m/min at the head of the fire
    smooth = ndimage.gaussian_filter(rng.normal(size=(rows, cols)), sigma=6)
    fuel = np.exp(0.35 * smooth / smooth.std())       # patchy multiplier, roughly 0.5-2x

    r0, c0 = lonlat_to_rc(*config.ignition_lonlat, config.aoi_bounds, rows, cols)
    rr, cc = np.mgrid[0:rows, 0:cols]
    dx = (cc - c0) * step * 111320.0 * cos(radians(lat))  # east, m
    dy = (r0 - rr) * step * 110540.0                       # north, m
    distance = np.hypot(dx, dy)
    heading = np.radians((wind_from + 180.0) % 360.0)  # direction the fire runs toward
    cos_theta = np.divide(dx * np.sin(heading) + dy * np.cos(heading), distance,
                          out=np.ones_like(distance), where=distance > 0)
    spread_rate = head_rate * (1 - MOCK_ECCENTRICITY) / (1 - MOCK_ECCENTRICITY * cos_theta) * fuel

    start_minutes = config.start_hour * 60.0 if config.weather_source == "gridmet" else 0.0
    minutes = distance / spread_rate
    burned = minutes <= config.projection_days * 1440.0
    time_of_arrival = np.where(burned, start_minutes + minutes, np.nan).astype("float32")

    matrices = {
        "time_of_arrival": time_of_arrival,
        "fire_type": burned.astype("uint8"),
        "spread_rate": np.where(burned, spread_rate, 0.0).astype("float32"),
        "flame_length": np.where(burned, 0.5 + 0.8 * spread_rate, 0.0).astype("float32"),
    }
    meta = {
        "scale": scale,
        "rows": rows,
        "cols": cols,
        "start_minutes": start_minutes,
        "weather_source": f"mock ({config.weather_source})",
        "non_land_mask": None,
    }
    stats = compute_stats(matrices, scale, {"stop_condition": "max duration reached"})
    return {"matrices": matrices, "meta": meta, "stats": stats, "ignition_rc": (r0, c0)}
