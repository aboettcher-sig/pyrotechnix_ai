"""Agent tools: run the pyroSim CLI and read back what it wrote.

The CLI is the simulation contract (see "docs/Fire weather agent — system design.md"). These
tools never compute fire behavior: they validate a request, build a `pyroSim run` command, run
it, and summarize the GeoTIFF + summary JSON it produced. Every run is recorded under
`runs/<run_id>/` so it can be listed, re-read and compared later.

Environment:
- PYROSIM_MODE      "mock" (default, synthetic spread, seconds) or "real" (Earth Engine + pyretechnics)
- PYROSIM_RUNS_DIR  where run records go (default: <repo>/runs)
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from . import maps

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # firesim lives at the repo root, next to agents/
    sys.path.insert(0, str(REPO_ROOT))

from firesim import observed  # noqa: E402

RUNS_DIR = Path(os.environ.get("PYROSIM_RUNS_DIR", REPO_ROOT / "runs"))
CACHE_DIR = Path(os.environ.get("PYROSIM_CACHE_DIR", REPO_ROOT / "cache"))
NODATA = -999.0
ACRES_PER_HECTARE = 2.47105

# Cost gate: bigger requests return an estimate and need confirm=True.
MAX_SPAN_DEG = 0.6
MAX_PROJECTION_DAYS = 14   # 10-day scenarios are the point; gate only what is genuinely expensive
REAL_RUN_TIMEOUT_S = 1800
MAX_MAP_RUNS = 4

EXAMPLE_AREAS = {
    "sierra_example": {
        "description": "Sierra Nevada foothills west of Lake Tahoe, CA (README example scenario)",
        "bounds": [-120.55, 39.00, -120.30, 39.20],
        "ignition_lonlat": [-120.45, 39.10],
    },
    "trabuco_canyon": {
        "description": "Trabuco Canyon, Orange County, CA (Airport fire area, design doc example)",
        "bounds": [-117.6, 33.6, -117.4, 33.8],
        "ignition_lonlat": [-117.51, 33.71],
    },
}


def _mode() -> str:
    return os.environ.get("PYROSIM_MODE", "mock").strip().lower()


def _run_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id


def _load_record(run_id: str) -> dict | None:
    path = _run_dir(run_id) / "run.json"
    return json.loads(path.read_text()) if path.exists() else None


def load_run_record(run_id: str) -> dict | None:
    """Full stored record of a run (including output_path), for UIs. Not an agent tool."""
    return _load_record(run_id)


def _checkpoints(projection_days: int) -> list[int]:
    hours = [1, 3, 6, 12] + [24 * d for d in range(1, projection_days + 1)]
    return [h for h in hours if h <= projection_days * 24]


def _growth_profile(tif_path: Path, cell_size_m: float, projection_days: int) -> list[dict]:
    """Burned area reached by each checkpoint hour, read from the hours-before-burn raster."""
    with rasterio.open(tif_path) as dataset:
        hours = dataset.read(1)
    burned = hours != NODATA
    cell_ha = cell_size_m**2 / 1e4
    profile = []
    for h in _checkpoints(projection_days):
        hectares = float(np.count_nonzero(burned & (hours <= h)) * cell_ha)
        profile.append({"hours_after_ignition": h, "burned_hectares": round(hectares, 1),
                        "burned_acres": round(hectares * ACRES_PER_HECTARE, 1)})
    return profile


def _edge_limited(tif_path: Path) -> dict:
    """Did the fire reach the edge of the area? If so the burned area is an underestimate."""
    with rasterio.open(tif_path) as dataset:
        hours = dataset.read(1)
    burned = hours != NODATA
    border = np.zeros_like(burned)
    border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
    edge_cells = int(np.count_nonzero(burned & border))
    return {
        "reached_area_edge": edge_cells > 0,
        "edge_cells": edge_cells,
        "note": ("The fire reached the edge of the area, so burned area is an underestimate: "
                 "rerun with a larger area." if edge_cells else ""),
    }


def _public_record(record: dict) -> dict:
    """The part of a run record worth showing the model (no absolute paths or raw logs)."""
    keys = ("run_id", "label", "status", "mode", "created_at", "scenario", "weather_source_used",
            "fuel_source_used", "cache", "summary", "growth", "edge", "grid", "error")
    return {k: record[k] for k in keys if k in record}


def list_example_areas() -> dict:
    """List named example areas with a bounding box and a suggested ignition point.

    Use these when the user refers to one of them. For any other place, ask the user for
    coordinates (or propose a bounding box and get their confirmation) before running.
    """
    return {"areas": EXAMPLE_AREAS}


def list_example_fires() -> dict:
    """List real 2024 fires with observed growth we can simulate and compare against.

    Each entry gives a ready-to-run scenario (area of interest, ignition point inside the first
    mapped perimeter, ignition date) plus the observed timeline: how many acres had burned at each
    mapped observation. Use it when the user names one of these fires, then call run_simulation
    with those values and show_map with observed_fire set to draw the real perimeters.
    """
    fires = {}
    for name in observed.available_fires():
        scenario = observed.scenario(name)
        fires[name] = {
            "incident_name": scenario["incident_name"],
            "aoi_bounds": scenario["aoi_bounds"],
            "ignition_lonlat": scenario["ignition_lonlat"],
            "ignition_date": scenario["ignition_date"],
            "first_observation": scenario["first_observation"],
            "last_observation": scenario["last_observation"],
            "observed_days": scenario["observed_days"],
            "first_acres": scenario["first_acres"],
            "final_acres": scenario["final_acres"],
            "observations": scenario["observations"],
            "note": ("The first mapped perimeter already covers first_acres, so a simulation from a "
                     "single ignition point is only comparable when first_acres is small."),
        }
    return {"fires": fires}


def run_simulation(
    west: float,
    south: float,
    east: float,
    north: float,
    ignition_lon: float,
    ignition_lat: float,
    ignition_date: str,
    projection_days: int,
    weather_source: str = "gridmet",
    fuel_source: str = "landfire",
    crown_fire: bool = True,
    label: str = "",
    confirm: bool = False,
) -> dict:
    """Run one deterministic pyroSim fire-spread simulation and return its run record.

    Args:
        west, south, east, north: Area of interest in decimal degrees (lon/lat, EPSG:4326).
        ignition_lon, ignition_lat: Ignition point; must be inside the area.
        ignition_date: Date the fire starts, YYYY-MM-DD. The fire starts at 12:00 UTC with
            gridmet weather, and at the forecast start with weathernext.
        projection_days: Whole days to simulate (>= 1).
        weather_source: "gridmet" (daily historical, 1979 to present) or "weathernext"
            (recent forecast; falls back to gridmet when no forecast covers the date).
        fuel_source: "landfire" (LANDFIRE 2023 fuel models and canopy, 30 m, CONUS only) or
            "nlcd" (coarse land-cover crosswalk, no canopy). Keep landfire unless asked.
        crown_fire: False zeroes the canopy so only surface fire spreads. Note this also removes
            the canopy's wind sheltering, so a surface-only run often spreads faster, not slower;
            it is not a clean crown-fire on/off switch.
        label: Short human-readable name for this run, e.g. "baseline" or "3-day horizon".
        confirm: Set True only after the user approved a run that returned needs_confirmation.

    Returns:
        A record with run_id, status ("done", "error" or "needs_confirmation"), scenario,
        summary stats (burned hectares/acres, crown-fire cells, max flame length, mean spread
        rate, cell size), growth (burned area at checkpoint hours) and mode ("mock" or "real").
    """
    mode = _mode()
    span = max(east - west, north - south)
    over_budget = span > MAX_SPAN_DEG or projection_days > MAX_PROJECTION_DAYS
    if mode == "real" and not confirm and over_budget:  # mock runs are seconds; never gate them
        return {
            "status": "needs_confirmation",
            "reason": (f"Request exceeds the default budget (area span {span:.2f} deg, limit "
                       f"{MAX_SPAN_DEG}; {projection_days} days, limit {MAX_PROJECTION_DAYS})."),
            "estimate": "Real runs of this size can take several minutes; the grid is capped at "
                        "160 cells per side, so a larger area also means coarser cells.",
            "next_step": "Ask the user to confirm, then call again with confirm=True.",
        }
    run_id = f"run_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    tif_path = run_dir / "hours_before_burn.tif"
    summary_path = run_dir / "summary.json"

    command = [
        sys.executable, "-m", "firesim", "run",
        "--aoi-bounds", str(west), str(south), str(east), str(north),
        "--ignition-lonlat", str(ignition_lon), str(ignition_lat),
        "--ignition-date", ignition_date,
        "--projection-days", str(projection_days),
        "--weather-source", weather_source,
        "--fuel-source", fuel_source,
        "--cache-dir", str(CACHE_DIR),
        "--output-name", str(tif_path),
        "--summary-json", str(summary_path),
    ]
    if not crown_fire:
        command.append("--no-crown-fire")
    if mode != "real":
        command.append("--mock")

    record = {
        "run_id": run_id,
        "label": label,
        "mode": "real" if mode == "real" else "mock",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command": command,
        "scenario": {
            "aoi_bounds": [west, south, east, north],
            "ignition_lonlat": [ignition_lon, ignition_lat],
            "ignition_date": ignition_date,
            "projection_days": projection_days,
            "weather_source": weather_source,
            "fuel_source": fuel_source,
            "crown_fire": crown_fire,
        },
    }

    try:
        completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True,
                                   timeout=REAL_RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        completed = None
        record.update(status="error", error=f"pyroSim timed out after {REAL_RUN_TIMEOUT_S} s.")

    if completed is not None and (completed.returncode != 0 or not summary_path.exists()):
        lines = [line for line in completed.stderr.strip().splitlines() if line.strip()]
        errors = [line for line in lines if "error" in line.lower()] or lines[-5:]
        record.update(status="error", error="\n".join(errors[-5:]) or "pyroSim failed without output.")
    elif completed is not None:
        summary = json.loads(summary_path.read_text())
        stats = summary["stats"]
        record.update(
            status="done",
            weather_source_used=summary["weather_source_used"],
            fuel_source_used=summary.get("fuel_source_used", fuel_source),
            cache=summary.get("cache", {}),
            grid=summary["grid"],
            output_path=str(tif_path),
            summary={
                "burned_hectares": round(stats["burned_hectares"], 1),
                "burned_acres": round(stats["burned_acres"], 1),
                "max_flame_length_m": round(stats["max_flame_length_m"], 2),
                "mean_spread_rate_m_min": round(stats["mean_spread_rate_m_min"], 2),
                "passive_crown_cells": stats.get("passive_crown_cells", 0),
                "active_crown_cells": stats.get("active_crown_cells", 0),
                "severity": stats.get("severity"),
                "cell_size_m": round(stats["cell_size_m"], 1),
                "stop_condition": stats["stop_condition"],
            },
            growth=_growth_profile(tif_path, stats["cell_size_m"], projection_days),
            edge=_edge_limited(tif_path),
        )

    if completed is not None:
        (run_dir / "pyrosim.log").write_text(completed.stdout + completed.stderr)
    (run_dir / "run.json").write_text(json.dumps(record, indent=2))
    return _public_record(record)


def severity_summary(run_id: str) -> dict:
    """Fire intensity for a finished run: flame-length bands, intensity percentiles, crown fire.

    Bands are the standard suppression reading: under 4 ft hand crews can hold it; 4-8 ft needs
    dozers, engines or aircraft; 8-11 ft means torching and spotting with head attack likely
    ineffective; over 11 ft means major runs. This is modeled fireline intensity and flame length,
    NOT ecological burn severity (BARC/dNBR) — say so when reporting it.
    """
    record = _load_record(run_id)
    if record is None or record.get("status") != "done":
        return {"status": "error", "error": f"No finished run with id {run_id!r}."}
    severity = (record.get("summary") or {}).get("severity")
    if not severity:
        return {"status": "error", "error": "This run predates intensity export; run it again."}
    return {
        "status": "done",
        "run_id": run_id,
        "label": record.get("label", ""),
        "mode": record.get("mode"),
        "burned_hectares": record["summary"]["burned_hectares"],
        **severity,
    }


def prepare_area(west: float, south: float, east: float, north: float, ignition_date: str,
                 projection_days: int, weather_source: str = "gridmet",
                 fuel_source: str = "landfire") -> dict:
    """Download an area's terrain, fuels, canopy and weather once, so later runs are fast.

    Call this before running several simulations in the same area and date — for example before
    comparing ignition points. Afterwards each run skips the download entirely (roughly 13 s to
    3.5 s on a typical area), and needs no network at all.

    Returns how long the download took and which layers were already cached.
    """
    command = [
        sys.executable, "-m", "firesim", "fetch",
        "--aoi-bounds", str(west), str(south), str(east), str(north),
        "--ignition-date", ignition_date,
        "--projection-days", str(projection_days),
        "--weather-source", weather_source,
        "--fuel-source", fuel_source,
        "--cache-dir", str(CACHE_DIR),
    ]
    if _mode() != "real":
        return {"status": "done", "note": "Mock mode fetches nothing, so there is nothing to prepare.",
                "mode": "mock"}
    started = time.time()
    completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True,
                               timeout=REAL_RUN_TIMEOUT_S)
    if completed.returncode != 0:
        lines = [line for line in completed.stderr.strip().splitlines() if line.strip()]
        return {"status": "error", "error": "\n".join(lines[-3:]) or "pyroSim fetch failed."}
    return {
        "status": "done",
        "seconds": round(time.time() - started, 1),
        "area": [west, south, east, north],
        "ignition_date": ignition_date,
        "detail": completed.stdout.strip().splitlines()[-2:],
        "note": "Runs in this area and date now skip the download.",
    }


def cache_status() -> dict:
    """How much simulation data is cached on disk (entries and megabytes)."""
    from firesim.cache import DataStore
    return {"status": "done", **DataStore(CACHE_DIR).usage()}


def get_run(run_id: str) -> dict:
    """Return the full record of a previous run: scenario, status, summary stats and growth."""
    record = _load_record(run_id)
    if record is None:
        return {"status": "error", "error": f"No run with id {run_id!r}."}
    return _public_record(record)


def list_runs(limit: int = 20) -> dict:
    """List the most recent runs (newest first) with their label, scenario and burned area."""
    records = []
    if RUNS_DIR.exists():
        for path in sorted(RUNS_DIR.glob("run_*/run.json"), reverse=True)[:limit]:
            record = json.loads(path.read_text())
            records.append({
                "run_id": record["run_id"],
                "label": record.get("label", ""),
                "status": record.get("status"),
                "mode": record.get("mode"),
                "scenario": record.get("scenario"),
                "burned_hectares": record.get("summary", {}).get("burned_hectares"),
            })
    return {"runs": records}


def compare_runs(run_ids: list[str]) -> dict:
    """Compare finished runs side by side: burned area, flame length, spread rate and growth.

    When runs share the same grid (same area), also returns the pairwise Sørensen overlap of
    their burned areas (1.0 = identical footprint, 0.0 = no overlap).
    """
    rows, masks, missing = [], {}, []
    for run_id in run_ids:
        record = _load_record(run_id)
        if record is None or record.get("status") != "done":
            missing.append(run_id)
            continue
        rows.append({
            "run_id": run_id,
            "label": record.get("label", ""),
            "scenario": record["scenario"],
            "weather_source_used": record.get("weather_source_used"),
            "fuel_source_used": record.get("fuel_source_used"),
            "summary": record["summary"],
            "growth": record["growth"],
        })
        with rasterio.open(record["output_path"]) as dataset:
            masks[run_id] = (tuple(dataset.bounds), dataset.read(1) != NODATA)

    overlaps = []
    ids = list(masks)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            (bounds_a, mask_a), (bounds_b, mask_b) = masks[a], masks[b]
            if bounds_a != bounds_b or mask_a.shape != mask_b.shape:
                continue
            total = mask_a.sum() + mask_b.sum()
            score = 2 * np.count_nonzero(mask_a & mask_b) / total if total else 1.0
            overlaps.append({"runs": [a, b], "sorensen_overlap": round(float(score), 3)})

    result = {"runs": rows, "overlaps": overlaps}
    if missing:
        result["not_compared"] = {"run_ids": missing, "reason": "unknown run id or run did not finish"}
    return result


async def show_map(run_ids: list[str], observed_fire: str = "", tool_context: ToolContext = None) -> dict:
    """Draw finished runs on a map: an image shown in the chat plus an interactive HTML map.

    Use after run_simulation or compare_runs whenever the user wants to see where the fire goes.
    Pass up to 4 run ids; each run gets its own panel in the image and its own toggleable layer
    in the HTML map. Colors are hours after ignition on one shared scale, with white perimeter
    lines at 6 h, 12 h and every 24 h. Set observed_fire (from list_example_fires) to draw that
    fire's real mapped perimeters in cyan on top, for a visual check against what happened.

    Returns:
        status, image_artifact (the image shown in the chat), html_path and html_url (open in a
        browser for the interactive map), basemap, and the runs drawn.
    """
    if not run_ids:
        return {"status": "error", "error": "Give at least one run id."}
    if len(run_ids) > MAX_MAP_RUNS:
        return {"status": "error", "error": f"At most {MAX_MAP_RUNS} runs per map; got {len(run_ids)}."}

    runs, problems = [], []
    for run_id in run_ids:
        record = _load_record(run_id)
        if record is None or record.get("status") != "done":
            problems.append(run_id)
        else:
            runs.append(record)
    if problems:
        return {"status": "error", "error": f"Unknown or unfinished runs: {', '.join(problems)}."}

    if len(runs) == 1:
        out_dir, stem = _run_dir(runs[0]["run_id"]), "map"
    else:
        out_dir, stem = RUNS_DIR / "maps", f"compare_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path, html_path = out_dir / f"{stem}.png", out_dir / f"{stem}.html"
    max_hours = max(run["scenario"]["projection_days"] for run in runs) * 24.0

    features = observed.perimeter_features(observed_fire) if observed_fire else None
    basemap = await asyncio.to_thread(maps.render_png, runs, png_path, max_hours, features)
    await asyncio.to_thread(maps.render_html, runs, html_path, max_hours, features)

    artifact_name = None
    if tool_context is not None:
        artifact_name = f"{stem}_{'_'.join(r['run_id'][-6:] for r in runs)}.png"
        await tool_context.save_artifact(
            artifact_name, types.Part.from_bytes(data=png_path.read_bytes(), mime_type="image/png")
        )

    return {
        "status": "done",
        "image_artifact": artifact_name,
        "image_path": str(png_path),
        "html_path": str(html_path),
        "html_url": html_path.resolve().as_uri(),
        "basemap": basemap,
        "observed_fire": observed_fire or None,
        "runs": [{"run_id": r["run_id"], "label": r.get("label", ""), "mode": r.get("mode")} for r in runs],
    }
