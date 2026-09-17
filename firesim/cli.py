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
burns (0 at the ignition cell, -999 where it never burns). Optionally also saves sibling
intensity (kW/m) and flame-length severity-class GeoTIFFs.
"""

import argparse
import pathlib
import sys

from . import raster
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
        "--intensity",
        action="store_true",
        help="Also save a fireline-intensity (kW/m) GeoTIFF as <name>_intensity.tif.",
    )
    run_parser.add_argument(
        "--severity",
        action="store_true",
        help="Also save a flame-length severity-class GeoTIFF as <name>_severity.tif.",
    )
    run_parser.set_defaults(func=_cmd_run)
    return parser


def _sibling_path(output_path: pathlib.Path, suffix: str) -> pathlib.Path:
    """Build a sibling GeoTIFF path like `<stem>_<suffix>.tif` next to the hours output."""
    return output_path.with_name(f"{output_path.stem}_{suffix}.tif")


def _resolve_output_path(output_name: str) -> pathlib.Path:
    path = pathlib.Path(output_name).expanduser()
    if path.suffix.lower() not in (".tif", ".tiff"):
        path = path.with_suffix(".tif")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _validate_scenario(aoi_bounds, ignition_lonlat, projection_days) -> None:
    west, south, east, north = aoi_bounds
    if west >= east or south >= north:
        raise ValueError("aoi-bounds must be ordered as west south east north with west<east, south<north.")
    lon, lat = ignition_lonlat
    if not (west <= lon <= east and south <= lat <= north):
        raise ValueError("ignition-lonlat must fall inside the AOI bounds.")
    if projection_days < 1:
        raise ValueError("projection-days must be >= 1.")


def _cmd_run(args: argparse.Namespace) -> int:
    aoi_bounds = tuple(args.aoi_bounds)
    ignition_lonlat = tuple(args.ignition_lonlat)
    _validate_scenario(aoi_bounds, ignition_lonlat, args.projection_days)
    output_path = _resolve_output_path(args.output_name)

    config = SimulationConfig(
        aoi_bounds=aoi_bounds,
        ignition_lonlat=ignition_lonlat,
        ignition_date=args.ignition_date,
        projection_days=args.projection_days,
        weather_source=args.weather_source,
    )

    print(f"Initializing Earth Engine (project: {config.ee_project or 'unset'})...")
    initialize_ee(config.ee_project)

    print("Running simulation...")
    results = run_simulation(config)

    saved = raster.write_geotiff(results, config, output_path)
    print(f"Saved hours GeoTIFF: {saved}")
    if args.intensity:
        intensity_path = raster.write_intensity_geotiff(results, config, _sibling_path(output_path, "intensity"))
        print(f"Saved intensity GeoTIFF: {intensity_path}")
    if args.severity:
        severity_path = raster.write_severity_geotiff(results, config, _sibling_path(output_path, "severity"))
        print(f"Saved severity GeoTIFF: {severity_path}")

    stats = results["stats"]
    print(
        f"Burned cells: {stats['burned_cells']} "
        f"({stats['burned_hectares']:.1f} ha) | "
        f"max fireline intensity: {stats['max_fireline_intensity_kw_m']:.0f} kW/m | "
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
