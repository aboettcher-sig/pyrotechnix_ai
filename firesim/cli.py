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
"""

import argparse
import datetime
import json
import pathlib
import sys

from . import mock, raster
from .config import SimulationConfig
from .gee import initialize_ee
from .model import run_simulation


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyroSim",
        description="Simulate wildfire spread and export an hours-before-burn GeoTIFF (EPSG:4326).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one simulation and save a GeoTIFF.")
    run_parser.add_argument(
        "--aoi-bounds",
        type=float,
        nargs=4,
        required=True,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="Area of interest as lon/lat: west south east north.",
    )
    run_parser.add_argument(
        "--ignition-lonlat",
        type=float,
        nargs=2,
        required=True,
        metavar=("LON", "LAT"),
        help="Ignition point as lon lat (must fall inside the AOI).",
    )
    run_parser.add_argument(
        "--ignition-date",
        required=True,
        metavar="YYYY-MM-DD",
        help="Date the fire departs.",
    )
    run_parser.add_argument(
        "--projection-days",
        type=int,
        required=True,
        help="Projection horizon in days.",
    )
    run_parser.add_argument(
        "--weather-source",
        default="gridmet",
        choices=["gridmet", "weathernext"],
        help="Weather backend (default: gridmet).",
    )
    run_parser.add_argument(
        "--output-name",
        "-o",
        required=True,
        help="Output GeoTIFF filename (a .tif extension is added if missing).",
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
    return parser


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
        },
        "weather_source_used": meta["weather_source"],
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


def _cmd_run(args: argparse.Namespace) -> int:
    aoi_bounds = tuple(args.aoi_bounds)
    ignition_lonlat = tuple(args.ignition_lonlat)
    _validate_scenario(aoi_bounds, ignition_lonlat, args.ignition_date, args.projection_days)
    output_path = _resolve_output_path(args.output_name)

    config = SimulationConfig(
        aoi_bounds=aoi_bounds,
        ignition_lonlat=ignition_lonlat,
        ignition_date=args.ignition_date,
        projection_days=args.projection_days,
        weather_source=args.weather_source,
    )

    if args.mock:
        print("Running MOCK simulation (synthetic spread, no Earth Engine)...")
        results = mock.run_simulation(config)
    else:
        print(f"Initializing Earth Engine (project: {config.ee_project or 'unset'})...")
        initialize_ee(config.ee_project)

        print("Running simulation...")
        results = run_simulation(config)

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
        f"stop: {stats['stop_condition']}"
    )
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
