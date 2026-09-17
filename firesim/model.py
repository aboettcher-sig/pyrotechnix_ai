"""Assemble pyretechnics inputs, run the spread engine, and summarize results."""

import time

import numpy as np
import pyretechnics.eulerian_level_set as els
from pyretechnics.space_time_cube import SpaceTimeCube

from . import gee, landmask, physics, severity, weather

DAY_MINUTES = 1440.0


def lonlat_to_rc(lon, lat, bounds, rows, cols):
    """Convert a lon/lat point to a (row, col) cell index in the north-up AOI grid."""
    west, south, east, north = bounds
    col = int((lon - west) / (east - west) * cols)
    row = int((north - lat) / (north - south) * rows)
    return (min(max(row, 0), rows - 1), min(max(col, 0), cols - 1))


def _crop(arr, rows, cols):
    return arr[..., :rows, :cols]


def fetch_fuels(config, region, scale):
    """Return (fuel_model, canopy cubes, water mask) from the configured fuel source."""
    if config.fuel_source == "landfire":
        raw = gee.fetch_landfire(region, scale)
        fuel_model = raw["fuel_model"]
        canopy = physics.landfire_to_canopy(raw)
        water = raw["water_fraction"] > config.water_fraction_threshold
    elif config.fuel_source == "nlcd":
        fuel_model = physics.nlcd_to_fuel_model(gee.fetch_landcover(region, scale))
        canopy = {
            name: np.full(fuel_model.shape, getattr(config, name), dtype="float32")
            for name in physics.CANOPY_LAYERS
        }
        water = gee.fetch_water_mask(region, scale, config.water_fraction_threshold)
    else:
        raise ValueError(f"Unknown fuel_source {config.fuel_source!r}; use 'landfire' or 'nlcd'.")

    return physics.sanitize_fuel_model(fuel_model), canopy, water


def fetch_layers(config, store=None):
    """Return (static arrays, WeatherStack, info), using and populating `store` when given.

    Earth Engine is initialized lazily, so a fully cached area/date runs with no auth and no
    network call — that is what makes repeated runs at new ignition points fast.
    """
    scale = gee.compute_scale(config.aoi_bounds, config.max_pixels, config.min_scale_m)
    region_state = {}

    def region():
        if "region" not in region_state:
            gee.initialize_ee(config.ee_project)
            region_state["region"] = gee.aoi_geometry(config.aoi_bounds)
        return region_state["region"]

    info = {"static": "hit", "weather": "hit", "cache_dir": str(store.root) if store else None}
    started = time.time()

    static = store.load_static(config) if store else None
    if static is None:
        info["static"] = "miss" if store else "off"
        static = fetch_static(config, region(), scale)
        if store:
            store.save_static(config, static)

    weather_stack = store.load_weather(config) if store else None
    if weather_stack is None:
        info["weather"] = "miss" if store else "off"
        weather_stack = weather.get_weather(config, region(), scale)
        if store:
            store.save_weather(config, weather_stack)

    info["fetch_seconds"] = round(time.time() - started, 2)
    return static, weather_stack, info


def fetch_static(config, region, scale) -> dict:
    """Fetch the date-independent layers: terrain, fuel model, canopy, water.

    Canopy is stored as fetched; `enable_crown_fire` is applied when assembling, so switching it
    does not need a refetch.
    """
    slope, aspect = gee.fetch_topography(region, scale)
    fuel_model, canopy, water = fetch_fuels(config, region, scale)
    return {"slope": slope, "aspect": aspect, "fuel_model": fuel_model, "water": water, **canopy}


def assemble_inputs(config, static, weather_stack, scale):
    """Wrap fetched layers as SpaceTimeCubes on one aligned grid (no network)."""
    slope, aspect, water = static["slope"], static["aspect"], static["water"]
    fuel_model = static["fuel_model"]
    canopy = {name: static[name] for name in physics.CANOPY_LAYERS}
    if not config.enable_crown_fire:
        canopy = {name: np.zeros_like(arr) for name, arr in canopy.items()}

    # Align all layers to a common grid (downloads can differ by a pixel).
    sample = next(iter(weather_stack.cubes.values()))
    rows = min(slope.shape[-2], fuel_model.shape[-2], sample.shape[-2])
    cols = min(slope.shape[-1], fuel_model.shape[-1], sample.shape[-1])
    bands = sample.shape[0]
    cube_shape = (bands, rows, cols)

    fuel_model = _crop(fuel_model, rows, cols)
    non_land = water[:rows, :cols]
    if config.constrain_to_land:
        non_land = landmask.buffer_mask(non_land, config.water_buffer_cells)
        fuel_model = landmask.enforce_land_constraint(fuel_model, non_land)

    def const(value):
        return np.full((rows, cols), value, dtype="float32")

    arrays = {
        "slope": _crop(slope, rows, cols),
        "aspect": _crop(aspect, rows, cols),
        "fuel_model": fuel_model,
        "fuel_moisture_live_herbaceous": const(config.live_herbaceous),
        "fuel_moisture_live_woody": const(config.live_woody),
        "foliar_moisture": const(config.foliar),
    }
    for name, cube in {**canopy, **weather_stack.cubes}.items():
        arrays[name] = _crop(cube, rows, cols)

    space_time_cubes = {
        name: SpaceTimeCube(cube_shape, np.ascontiguousarray(arr, dtype="float32"))
        for name, arr in arrays.items()
    }
    meta = {
        "scale": scale,
        "cube_shape": cube_shape,
        "rows": rows,
        "cols": cols,
        "bands": bands,
        "band_duration_min": weather_stack.band_duration_min,
        "start_minutes": weather_stack.start_minutes,
        "weather_source": weather_stack.source,
        "fuel_source": config.fuel_source,
        "non_land_mask": non_land if config.constrain_to_land else None,
    }
    return space_time_cubes, meta


def build_inputs(config, store=None):
    """Fetch (or load cached) every layer and wrap them as SpaceTimeCubes."""
    scale = gee.compute_scale(config.aoi_bounds, config.max_pixels, config.min_scale_m)
    static, weather_stack, info = fetch_layers(config, store)
    space_time_cubes, meta = assemble_inputs(config, static, weather_stack, scale)
    meta["cache"] = info
    return space_time_cubes, meta


def run_simulation(config, store=None):
    """Build inputs, spread the fire, and return matrices + metadata + stats."""
    started = time.time()
    space_time_cubes, meta = build_inputs(config, store)
    build_seconds = time.time() - started
    ignition_rc = lonlat_to_rc(*config.ignition_lonlat, config.aoi_bounds, meta["rows"], meta["cols"])

    spread_state = els.SpreadState(meta["cube_shape"]).ignite_cell(ignition_rc)
    cube_resolution = (meta["band_duration_min"], meta["scale"], meta["scale"])
    result = els.spread_fire_with_phi_field(
        space_time_cubes,
        spread_state,
        cube_resolution,
        start_time=meta["start_minutes"],
        max_duration=config.projection_days * DAY_MINUTES,
    )
    matrices = result["spread_state"].get_full_matrices()
    if meta["non_land_mask"] is not None:
        matrices["time_of_arrival"][meta["non_land_mask"]] = np.nan
    meta.setdefault("cache", {})["engine_seconds"] = round(time.time() - started - build_seconds, 2)
    stats = compute_stats(matrices, meta["scale"], result)
    return {"matrices": matrices, "meta": meta, "stats": stats, "ignition_rc": ignition_rc}


def compute_stats(matrices, scale, result):
    """Summarize a spread result into a small dictionary of headline numbers."""
    fire_type = matrices["fire_type"]
    burned = fire_type > 0
    n_burned = int(np.count_nonzero(burned))
    cell_ha = (scale * scale) / 1e4
    flame = matrices["flame_length"][burned]
    spread = matrices["spread_rate"][burned]
    return {
        "severity": severity.summarize(matrices, scale),
        "burned_cells": n_burned,
        "burned_hectares": n_burned * cell_ha,
        "burned_acres": n_burned * cell_ha * 2.47105,
        "passive_crown_cells": int(np.count_nonzero(fire_type == 2)),
        "active_crown_cells": int(np.count_nonzero(fire_type == 3)),
        "max_flame_length_m": float(np.nanmax(flame)) if n_burned else 0.0,
        "mean_spread_rate_m_min": float(np.nanmean(spread)) if n_burned else 0.0,
        "stop_condition": result["stop_condition"],
        "cell_size_m": scale,
    }
