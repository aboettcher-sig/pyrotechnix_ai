"""Command-line interface for firesim.

Usage::

    pyroSim run \
        --aoi-bounds -120.55 39.00 -120.30 39.20 \
        --ignition-lonlat -120.45 39.10 \
        --ignition-date 2026-09-10 \
        --projection-days 5 \
        --weather-source weathernext \
        --output-name tahoe_fire.tif

Saves a single EPSG:4326 GeoTIFF where each pixel is the number of hours before that cell
burns (0 at the ignition cell, -999 where it never burns).

`--summary-json PATH` also writes a machine-readable run summary (for agents and scripts), and
`--mock` swaps the real simulation for a fast synthetic one with the same outputs.

`--cache-dir DIR` reuses fetched Earth Engine layers across runs: static layers (terrain, fuels,
canopy, water) are keyed by area and fuel source, weather by area plus date and backend. So a new
ignition point in the same area and date needs no download at all. `pyroSim fetch` warms that
cache without running the engine.
"""

import argparse
import datetime
import json
import pathlib
import sys

from . import mock, raster
from .cache import DataStore
from .montecarlo import run_monte_carlo
from .config import SimulationConfig
from .model import fetch_layers, run_simulation


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyroSim",
        description="Simulate wildfire spread and export an hours-before-burn GeoTIFF (EPSG:4326).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one simulation and save a GeoTIFF.")
    fetch_parser = subparsers.add_parser(
        "fetch", help="Download an area's layers into the cache without running the engine.")
    fetch_parser.add_argument("--static-only", action="store_true",
                              help="Fetch only the date-independent layers (terrain, fuels, canopy).")
    fetch_parser.set_defaults(func=_cmd_fetch)
    _add_scenario_args(run_parser)
    _add_scenario_args(fetch_parser)
    run_parser.add_argument("--ignition-lonlat", type=float, nargs=2, required=True,
                            metavar=("LON", "LAT"),
                            help="Ignition point as lon lat (must fall inside the AOI).")
    run_parser.add_argument(
        "--output-name",
        "-o",
        required=True,
        help="Output GeoTIFF filename (a .tif extension is added if missing).",
    )
    run_parser.add_argument(
        "--no-crown-fire",
        action="store_true",
        help="Zero the canopy layers so only surface fire spreads (this also removes the canopy's "
             "wind sheltering, so spread is often faster).",
    )
    run_parser.add_argument(
        "--summary-json",
        metavar="PATH",
        help="Also write a JSON summary (scenario, stats, grid, provenance) to PATH.",
    )
    run_parser.add_argument(
        "--mock",
        action="store_true",
        help="Skip Earth Engine and pyretechnics; write a synthetic result with the same format.",
    )
    run_parser.set_defaults(func=_cmd_run)

    mc_parser = subparsers.add_parser(
        "montecarlo", aliases=["mc"],
        help="Run N random-ignition simulations and save a probabilistic multiband GeoTIFF.")
    _add_scenario_args(mc_parser)
    mc_parser.add_argument("--iterations", "-n", type=int, required=True,
                           help="Number of random-ignition simulations to run.")
    mc_parser.add_argument("--seed", type=int, default=None,
                           help="Random seed for reproducible ignition points.")
    mc_parser.add_argument("--output-name", "-o", required=True,
                           help="Output multiband GeoTIFF filename (a .tif extension is added).")
    mc_parser.add_argument("--summary-json", metavar="PATH",
                           help="Also write a JSON summary (scenario, stats, grid, provenance).")
    mc_parser.set_defaults(func=_cmd_montecarlo)
    return parser


def _add_scenario_args(sub) -> None:
    """Area/date/weather/fuel arguments shared by `run` and `fetch`."""
    sub.add_argument("--aoi-bounds", type=float, nargs=4, required=True,
                     metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                     help="Area of interest as lon/lat: west south east north.")
    sub.add_argument("--ignition-date", required=True, metavar="YYYY-MM-DD",
                     help="Date the fire departs.")
    sub.add_argument("--projection-days", type=int, required=True, help="Projection horizon in days.")
    sub.add_argument("--weather-source", default="gridmet", choices=["gridmet", "weathernext"],
                     help="Weather backend (default: gridmet).")
    sub.add_argument("--fuel-source", default="landfire", choices=["landfire", "nlcd"],
                     help="Fuels: landfire (LANDFIRE 2023 FBFM40 + canopy, default) or nlcd.")
    sub.add_argument("--cache-dir",
                     help="Directory for cached layers, reused across runs on the same area/date.")


def _config_from(args, **overrides) -> SimulationConfig:
    return SimulationConfig(
        aoi_bounds=tuple(args.aoi_bounds),
        ignition_date=args.ignition_date,
        projection_days=args.projection_days,
        weather_source=args.weather_source,
        fuel_source=args.fuel_source,
        cache_dir=args.cache_dir,
        **overrides,
    )


def _cmd_fetch(args: argparse.Namespace) -> int:
    """Warm the cache for an area/date so later runs skip the download."""
    if not args.cache_dir:
        raise ValueError("fetch requires --cache-dir.")
    _validate_scenario(tuple(args.aoi_bounds), None, args.ignition_date, args.projection_days)
    config = _config_from(args, ignition_lonlat=(0.0, 0.0))
    store = DataStore(config.cache_dir)
    print(f"Fetching layers into {config.cache_dir}...")
    if args.static_only:
        from .gee import aoi_geometry, compute_scale, initialize_ee
        from .model import fetch_static
        initialize_ee(config.ee_project)
        scale = compute_scale(config.aoi_bounds, config.max_pixels, config.min_scale_m)
        store.save_static(config, fetch_static(config, aoi_geometry(config.aoi_bounds), scale))
        print("Cached static layers (terrain, fuels, canopy, water).")
    else:
        _, _, info = fetch_layers(config, store)
        print(f"Cached static={info['static']} weather={info['weather']} in {info['fetch_seconds']}s.")
    usage = store.usage()
    print(f"Cache: {usage['static']} static + {usage['weather']} weather entries, "
          f"{usage['megabytes']} MB at {usage['path']}")
    return 0


def _resolve_output_path(output_name: str) -> pathlib.Path:
    path = pathlib.Path(output_name).expanduser()
    if path.suffix.lower() not in (".tif", ".tiff"):
        path = path.with_suffix(".tif")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _validate_scenario(aoi_bounds, ignition_lonlat, ignition_date, projection_days) -> None:
    west, south, east, north = aoi_bounds
    if west >= east or south >= north:
        raise ValueError("aoi-bounds must be ordered as west south east north with west<east, south<north.")
    if ignition_lonlat is not None:
        lon, lat = ignition_lonlat
        if not (west <= lon <= east and south <= lat <= north):
            raise ValueError("ignition-lonlat must fall inside the AOI bounds.")
    try:
        datetime.date.fromisoformat(ignition_date)
    except ValueError:
        raise ValueError("ignition-date must be a valid date formatted YYYY-MM-DD.") from None
    if projection_days < 1:
        raise ValueError("projection-days must be >= 1.")


def _write_summary(path, config, results, output_path, is_mock) -> None:
    meta = results["meta"]
    summary = {
        "output": str(output_path),
        "mock": is_mock,
        "scenario": {
            "aoi_bounds": list(config.aoi_bounds),
            "ignition_lonlat": list(config.ignition_lonlat),
            "ignition_date": config.ignition_date,
            "projection_days": config.projection_days,
            "weather_source": config.weather_source,
            "fuel_source": config.fuel_source,
            "crown_fire": config.enable_crown_fire,
        },
        "weather_source_used": meta["weather_source"],
        "cache": meta.get("cache", {}),
        "fuel_source_used": meta.get("fuel_source", config.fuel_source),
        "stats": results["stats"],
        "grid": {
            "crs": "EPSG:4326",
            "shape": [meta["rows"], meta["cols"]],
            "cell_size_m": meta["scale"],
            "nodata": raster.NODATA,
            "band": "hours_before_burn",
        },
    }
    path = pathlib.Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2))


def _cmd_montecarlo(args: argparse.Namespace) -> int:
    """Many random ignitions over one area: burn probability and intensity percentiles."""
    aoi_bounds = tuple(args.aoi_bounds)
    _validate_scenario(aoi_bounds, None, args.ignition_date, args.projection_days)
    if args.iterations < 1:
        raise ValueError("iterations must be >= 1.")
    output_path = _resolve_output_path(args.output_name)

    west, south, east, north = aoi_bounds
    # Monte Carlo samples its own ignition cells; the centre is just an in-bounds placeholder.
    config = _config_from(args, ignition_lonlat=((west + east) / 2.0, (south + north) / 2.0))
    store = DataStore(config.cache_dir) if config.cache_dir else None

    print(f"Running {args.iterations} Monte Carlo simulations..."
          + (f" (cache: {config.cache_dir})" if store else ""))
    aggregate = run_monte_carlo(config, args.iterations, seed=args.seed, store=store)
    saved = raster.write_monte_carlo_geotiff(aggregate, config, output_path)

    probability = aggregate["probability"]
    meta = aggregate["meta"]
    burned_cells = int((aggregate["burn_count"] > 0).sum())
    print(f"Saved Monte Carlo GeoTIFF: {saved}")
    print(f"Iterations: {aggregate['iterations']} | max burn probability: {float(probability.max()):.2f} | "
          f"cells burned at least once: {burned_cells} | cell size: {meta['scale']:.0f} m | "
          f"weather: {meta['weather_source']} | fuels: {meta.get('fuel_source')}")

    if args.summary_json:
        cell_ha = meta["scale"] ** 2 / 1e4
        summary = {
            "output": saved,
            "kind": "montecarlo",
            "scenario": {
                "aoi_bounds": list(aoi_bounds),
                "ignition_date": config.ignition_date,
                "projection_days": config.projection_days,
                "weather_source": config.weather_source,
                "fuel_source": config.fuel_source,
                "iterations": aggregate["iterations"],
                "seed": args.seed,
            },
            "weather_source_used": meta["weather_source"],
            "fuel_source_used": meta.get("fuel_source", config.fuel_source),
            "stats": {
                "iterations": aggregate["iterations"],
                "max_burn_probability": round(float(probability.max()), 4),
                "mean_burn_probability": round(float(probability.mean()), 4),
                "cells_burned_at_least_once": burned_cells,
                "hectares_burned_at_least_once": round(burned_cells * cell_ha, 1),
                "cell_size_m": round(meta["scale"], 1),
            },
            "grid": {"crs": "EPSG:4326", "shape": [meta["rows"], meta["cols"]],
                     "cell_size_m": meta["scale"], "nodata": raster.NODATA,
                     "bands": [description for _, description in raster.MONTE_CARLO_BANDS]},
            "cache": meta.get("cache", {}),
        }
        path = pathlib.Path(args.summary_json).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    aoi_bounds = tuple(args.aoi_bounds)
    ignition_lonlat = tuple(args.ignition_lonlat)
    _validate_scenario(aoi_bounds, ignition_lonlat, args.ignition_date, args.projection_days)
    output_path = _resolve_output_path(args.output_name)

    config = _config_from(args, ignition_lonlat=ignition_lonlat,
                          enable_crown_fire=not args.no_crown_fire)

    if args.mock:
        print("Running MOCK simulation (synthetic spread, no Earth Engine)...")
        results = mock.run_simulation(config)  # nothing is fetched, so the cache is bypassed
    else:
        store = DataStore(config.cache_dir) if config.cache_dir else None
        print("Running simulation..." + (f" (cache: {config.cache_dir})" if store else ""))
        results = run_simulation(config, store)

    saved = raster.write_geotiff(results, config, output_path)
    if args.summary_json:
        _write_summary(args.summary_json, config, results, saved, args.mock)

    stats = results["stats"]
    print(f"Saved GeoTIFF: {saved}")
    print(
        f"Burned cells: {stats['burned_cells']} "
        f"({stats['burned_hectares']:.1f} ha) | "
        f"cell size: {stats['cell_size_m']:.0f} m | "
        f"weather: {results['meta']['weather_source']} | "
        f"fuels: {results['meta'].get('fuel_source', config.fuel_source)} | "
        f"crown cells: {stats.get('passive_crown_cells', 0) + stats.get('active_crown_cells', 0)} | "
        f"stop: {stats['stop_condition']}"
    )
    cache = results["meta"].get("cache") or {}
    if cache.get("cache_dir"):
        print(f"Cache: static={cache['static']} weather={cache['weather']} | "
              f"fetch {cache.get('fetch_seconds', 0)}s | engine {cache.get('engine_seconds', 0)}s")
    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    sys.exit(main())
