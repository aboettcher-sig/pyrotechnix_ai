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
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from . import maps

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = Path(os.environ.get("PYROSIM_RUNS_DIR", REPO_ROOT / "runs"))
NODATA = -999.0
ACRES_PER_HECTARE = 2.47105

# Cost gate: bigger requests return an estimate and need confirm=True.
MAX_SPAN_DEG = 0.5
MAX_PROJECTION_DAYS = 7
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


def _public_record(record: dict) -> dict:
    """The part of a run record worth showing the model (no absolute paths or raw logs)."""
    keys = ("run_id", "label", "status", "mode", "created_at", "scenario", "weather_source_used",
            "summary", "growth", "grid", "error")
    return {k: record[k] for k in keys if k in record}


def list_example_areas() -> dict:
    """List named example areas with a bounding box and a suggested ignition point.

    Use these when the user refers to one of them. For any other place, ask the user for
    coordinates (or propose a bounding box and get their confirmation) before running.
    """
    return {"areas": EXAMPLE_AREAS}


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
        label: Short human-readable name for this run, e.g. "baseline" or "3-day horizon".
        confirm: Set True only after the user approved a run that returned needs_confirmation.

    Returns:
        A record with run_id, status ("done", "error" or "needs_confirmation"), scenario,
        summary stats (burned hectares/acres, max flame length, mean spread rate, cell size),
        growth (burned area at checkpoint hours) and mode ("mock" or "real").
    """
    span = max(east - west, north - south)
    if not confirm and (span > MAX_SPAN_DEG or projection_days > MAX_PROJECTION_DAYS):
        return {
            "status": "needs_confirmation",
            "reason": (f"Request exceeds the default budget (area span {span:.2f} deg, limit "
                       f"{MAX_SPAN_DEG}; {projection_days} days, limit {MAX_PROJECTION_DAYS})."),
            "estimate": "Real runs of this size can take many minutes; the grid is capped at "
                        "160 cells per side, so a larger area also means coarser cells.",
            "next_step": "Ask the user to confirm, then call again with confirm=True.",
        }

    mode = _mode()
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
        "--output-name", str(tif_path),
        "--summary-json", str(summary_path),
    ]
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
            grid=summary["grid"],
            output_path=str(tif_path),
            summary={
                "burned_hectares": round(stats["burned_hectares"], 1),
                "burned_acres": round(stats["burned_acres"], 1),
                "max_flame_length_m": round(stats["max_flame_length_m"], 2),
                "mean_spread_rate_m_min": round(stats["mean_spread_rate_m_min"], 2),
                "cell_size_m": round(stats["cell_size_m"], 1),
                "stop_condition": stats["stop_condition"],
            },
            growth=_growth_profile(tif_path, stats["cell_size_m"], projection_days),
        )

    if completed is not None:
        (run_dir / "pyrosim.log").write_text(completed.stdout + completed.stderr)
    (run_dir / "run.json").write_text(json.dumps(record, indent=2))
    return _public_record(record)


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


async def show_map(run_ids: list[str], tool_context: ToolContext = None) -> dict:
    """Draw finished runs on a map: an image shown in the chat plus an interactive HTML map.

    Use after run_simulation or compare_runs whenever the user wants to see where the fire goes.
    Pass up to 4 run ids; each run gets its own panel in the image and its own toggleable layer
    in the HTML map. Colors are hours after ignition on one shared scale, with white perimeter
    lines at 6 h, 12 h and every 24 h.

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

    basemap = await asyncio.to_thread(maps.render_png, runs, png_path, max_hours)
    await asyncio.to_thread(maps.render_html, runs, html_path, max_hours)

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
        "runs": [{"run_id": r["run_id"], "label": r.get("label", ""), "mode": r.get("mode")} for r in runs],
    }
