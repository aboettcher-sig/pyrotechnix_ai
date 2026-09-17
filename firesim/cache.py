"""On-disk cache for fetched Earth Engine layers, split by date dependency.

Adapted from the `guiat/fetch-data` CLI cache, extended for the LANDFIRE fuels on this branch.
Two groups are cached separately so re-runs avoid redundant downloads:

- static: terrain, fuels and canopy, water — depend only on the AOI grid and the fuel source.
- weather: the WeatherStack cubes — depend on the grid as well as the date and backend.

Each group lives under `<cache_dir>/<group>/<key>/` as `arrays.npz` + `manifest.json`, keyed by a
hash of the parameters that determine its contents. Changing only the ignition point reuses both
groups; changing only the date reuses static and refetches weather.

Every parameter that changes what is fetched MUST be in the key, or a run silently reuses the
wrong layers: `fuel_source` and the LANDFIRE asset version are in there for exactly that reason.
"""

import hashlib
import json
import os
import pathlib
import tempfile

import numpy as np

from .gee import LANDFIRE_VERSION
from .weather import WeatherStack

CACHE_FORMAT = 2  # bump when the stored layer shape changes, to invalidate old entries


def _key(params: dict) -> str:
    """Stable short hash of the parameters that determine a cached group's contents."""
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def static_params(config) -> dict:
    """Parameters that fully determine the static (date-independent) layers."""
    params = {
        "format": CACHE_FORMAT,
        "aoi_bounds": [round(float(b), 6) for b in config.aoi_bounds],
        "max_pixels": config.max_pixels,
        "min_scale_m": config.min_scale_m,
        "water_fraction_threshold": config.water_fraction_threshold,
        "fuel_source": config.fuel_source,
    }
    if config.fuel_source == "landfire":
        params["landfire_version"] = LANDFIRE_VERSION
    else:  # the NLCD path bakes the canopy constants into the layers
        params["canopy"] = [config.canopy_cover, config.canopy_height,
                            config.canopy_base_height, config.canopy_bulk_density]
    return params


def weather_params(config) -> dict:
    """Parameters that fully determine the weather layers (static grid + date/backend)."""
    # Weather depends on the grid only — never on fuels, so drop every fuel-specific key.
    fuel_keys = ("fuel_source", "canopy", "landfire_version")
    params = {k: v for k, v in static_params(config).items() if k not in fuel_keys}
    params.update({
        "ignition_date": config.ignition_date,
        "projection_days": config.projection_days,
        "weather_source": config.weather_source,
        "start_hour": config.start_hour,
        "allow_gridmet_fallback": config.allow_gridmet_fallback,
    })
    if config.weather_source == "weathernext":
        params.update({
            "weathernext_stat": config.weathernext_stat,
            "weathernext_step_hours": config.weathernext_step_hours,
            "weathernext_init_time": config.weathernext_init_time,
        })
    return params


def _save_npz(directory: pathlib.Path, arrays: dict, manifest: dict) -> None:
    """Write arrays + manifest atomically, so parallel runs cannot read a half-written entry."""
    directory.mkdir(parents=True, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=directory, suffix=".npz")
    os.close(handle)
    np.savez_compressed(temp_path, **arrays)  # keeps the .npz name we gave it
    os.replace(temp_path, directory / "arrays.npz")
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))


class DataStore:
    """Read/write cached static and weather layers under a cache directory."""

    def __init__(self, cache_dir):
        self.root = pathlib.Path(cache_dir).expanduser()

    def _group_dir(self, group: str, params: dict) -> pathlib.Path:
        return self.root / group / _key(params)

    # --- static layers ---
    def load_static(self, config) -> dict | None:
        """Return the cached static arrays (terrain, fuels, canopy, water), or None on a miss."""
        path = self._group_dir("static", static_params(config)) / "arrays.npz"
        if not path.exists():
            return None
        with np.load(path) as data:
            static = {name: data[name] for name in data.files}
        static["water"] = static["water"].astype(bool)
        return static

    def save_static(self, config, static: dict) -> None:
        directory = self._group_dir("static", static_params(config))
        _save_npz(directory, static, static_params(config))

    # --- weather layers ---
    def load_weather(self, config) -> WeatherStack | None:
        """Return a WeatherStack rebuilt from cache, or None on a miss."""
        directory = self._group_dir("weather", weather_params(config))
        arrays_path, manifest_path = directory / "arrays.npz", directory / "manifest.json"
        if not (arrays_path.exists() and manifest_path.exists()):
            return None
        manifest = json.loads(manifest_path.read_text())
        with np.load(arrays_path) as data:
            cubes = {name: data[name] for name in data.files}
        return WeatherStack(cubes=cubes, band_duration_min=manifest["band_duration_min"],
                            start_minutes=manifest["start_minutes"], source=manifest["source"])

    def save_weather(self, config, weather_stack: WeatherStack) -> None:
        manifest = weather_params(config)
        # Record what was actually used: WeatherNext can fall back to GRIDMET.
        manifest.update({"band_duration_min": weather_stack.band_duration_min,
                         "start_minutes": weather_stack.start_minutes,
                         "source": weather_stack.source})
        _save_npz(self._group_dir("weather", weather_params(config)), weather_stack.cubes, manifest)

    # --- housekeeping ---
    def usage(self) -> dict:
        """Entry counts and size on disk, for the UI and `pyroSim cache`."""
        entries = {group: len(list((self.root / group).glob("*/arrays.npz"))) if (self.root / group).exists() else 0
                   for group in ("static", "weather")}
        size = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file()) if self.root.exists() else 0
        return {"path": str(self.root), **entries, "megabytes": round(size / 1e6, 1)}
