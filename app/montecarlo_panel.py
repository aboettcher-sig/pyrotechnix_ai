"""Burn probability tab: many random ignitions over one drawn area.

Deliberately separate from the fire-runs tab: its own session state (every key starts with `mc_`),
its own run store, its own maps. It shares only the layer cache, because the same area and date
need the same terrain, fuels and weather.

One run here is `firesim.montecarlo.run_monte_carlo`: inputs are assembled once, then N fires are
spread from random burnable cells and aggregated per cell into burn probability and fireline
intensity (kW/m — this tab reports intensity, not flame-length bands).
"""

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import folium
import numpy as np
import rasterio
import streamlit as st
from folium.plugins import Draw
from matplotlib import colormaps, colors
from streamlit_folium import st_folium

from firesim import DataStore, SimulationConfig, gee, raster, run_monte_carlo

REPO_ROOT = Path(__file__).resolve().parents[1]
MC_RUNS_DIR = Path(os.environ.get("PYROSIM_MC_RUNS_DIR", REPO_ROOT / "runs" / "montecarlo"))
CACHE_DIR = Path(os.environ.get("PYROSIM_CACHE_DIR", REPO_ROOT / "cache"))
ESRI_IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ESRI_ATTRIBUTION = "Tiles &copy; Esri"

DEFAULT_AREA = [-123.13, 41.41, -122.89, 41.56]  # the Shelly area, as a starting point
PROBABILITY_CMAP = "magma"
INTENSITY_CMAP = "inferno"
BAND_LABELS = {
    "burn_probability": "Burn probability",
    "burn_count": "Times burned",
    "mean_fireline_intensity_kw_m": "Mean intensity (kW/m)",
    "p10_fireline_intensity_kw_m": "p10 intensity (kW/m)",
    "p90_fireline_intensity_kw_m": "p90 intensity (kW/m)",
}
# Rough engine cost, measured on this laptop: ~0.2 s per projection day per 30k cells.
SECONDS_PER_DAY_PER_30K_CELLS = 0.2


# --- state and storage ---

def _init_state():
    defaults = {"mc_area": list(DEFAULT_AREA), "mc_result": None, "mc_click_seen": None}
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def saved_runs() -> list[dict]:
    if not MC_RUNS_DIR.exists():
        return []
    records = []
    for path in sorted(MC_RUNS_DIR.glob("mc_*/run.json"), reverse=True):
        records.append(json.loads(path.read_text()))
    return records


def _save(record: dict) -> None:
    directory = MC_RUNS_DIR / record["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(json.dumps(record, indent=2))


def area_km(area) -> tuple[float, float]:
    west, south, east, north = area
    mid = math.radians((south + north) / 2)
    return ((east - west) * 111.32 * math.cos(mid), (north - south) * 110.54)


def grid_estimate(area, iterations, days=1, max_pixels=160) -> dict:
    """Cells, memory for the intensity stack and a rough wall-clock estimate.

    Uses the simulator's own grid maths (a square degree step from the metric scale, snapped to a
    grid anchored at 0,0), so the numbers match what the run will actually build.
    """
    west, south, east, north = area
    cell_m = gee.compute_scale(area, max_pixels, 30.0)
    step = cell_m / 111319.49
    cols = max(1, math.ceil(east / step) - math.floor(west / step))
    rows = max(1, math.ceil(north / step) - math.floor(south / step))
    seconds = iterations * SECONDS_PER_DAY_PER_30K_CELLS * max(days, 1) * (rows * cols / 30000)
    return {
        "cell_m": cell_m,
        "rows": rows,
        "cols": cols,
        "megabytes": iterations * rows * cols * 4 / 1e6,
        "minutes": max(seconds / 60, 0.1),
    }


# --- map ---

def _overlay(values, cmap, vmin, vmax, mask):
    rgba = colormaps[cmap](colors.Normalize(vmin=vmin, vmax=vmax)(np.nan_to_num(values)))
    rgba[..., 3] = np.where(mask, 0.8, 0.0)
    return (rgba * 255).astype("uint8")


def build_map(area, record=None, band="burn_probability", threshold=0.0):
    """Imagery map of the area, with one result band drawn when a run is selected."""
    west, south, east, north = area
    fmap = folium.Map(location=[(south + north) / 2, (west + east) / 2], tiles=None, control_scale=True)
    folium.TileLayer(ESRI_IMAGERY, attr=ESRI_ATTRIBUTION, name="Aerial imagery").add_to(fmap)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(fmap)
    folium.Rectangle([[south, west], [north, east]], color="#ffb300", weight=3, fill=False,
                     tooltip="Simulation area").add_to(fmap)

    if record:
        with rasterio.open(record["output"]) as dataset:
            index = dataset.descriptions.index(band) + 1
            values = dataset.read(index).astype("float32")
            bounds = dataset.bounds
        if band.endswith("kw_m"):
            values[values == raster.NODATA] = np.nan
            mask = np.isfinite(values)
            vmax = float(np.nanpercentile(values[mask], 98)) if mask.any() else 1.0
            image = _overlay(values, INTENSITY_CMAP, 0.0, max(vmax, 1.0), mask)
        else:
            top = record["stats"]["max_burn_probability"] if band == "burn_probability" else record["stats"]["iterations"]
            mask = values > (threshold * (1 if band == "burn_probability" else record["stats"]["iterations"]))
            image = _overlay(values, PROBABILITY_CMAP, 0.0, max(top, 1e-6), mask)
        folium.raster_layers.ImageOverlay(
            image=image, bounds=[[bounds.bottom, bounds.left], [bounds.top, bounds.right]],
            mercator_project=True, name=BAND_LABELS.get(band, band),
        ).add_to(fmap)

    Draw(export=False, position="topleft",
         draw_options={"rectangle": {"shapeOptions": {"color": "#ffb300"}}, "polygon": False,
                       "polyline": False, "circle": False, "marker": False, "circlemarker": False},
         edit_options={"edit": False}).add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.fit_bounds([[south, west], [north, east]])
    return fmap


def _drawn_area(drawing):
    geometry = (drawing or {}).get("geometry") or {}
    if geometry.get("type") != "Polygon":
        return None
    points = [point for ring in geometry["coordinates"] for point in ring]
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    return [round(min(lons), 5), round(min(lats), 5), round(max(lons), 5), round(max(lats), 5)]


# --- the run itself ---

def run_montecarlo(area, ignition_date, days, iterations, seed, weather_source, fuel_source, use_cache):
    """Run in-process with a progress bar, then save the GeoTIFF and a record."""
    run_id = f"mc_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    directory = MC_RUNS_DIR / run_id
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "montecarlo.tif"

    west, south, east, north = area
    config = SimulationConfig(
        aoi_bounds=tuple(area),
        ignition_lonlat=((west + east) / 2, (south + north) / 2),  # placeholder; MC samples its own
        ignition_date=ignition_date,
        projection_days=int(days),
        weather_source=weather_source,
        fuel_source=fuel_source,
        cache_dir=str(CACHE_DIR) if use_cache else None,
    )
    store = DataStore(config.cache_dir) if config.cache_dir else None

    progress = st.progress(0.0, text="Preparing layers…")
    started = time.time()

    def tick(done, total):
        elapsed = time.time() - started
        remaining = elapsed / max(done, 1) * (total - done)
        progress.progress(done / total,
                          text=f"Simulation {done} of {total} · {elapsed:.0f}s elapsed · "
                               f"~{remaining:.0f}s left")

    aggregate = run_monte_carlo(config, int(iterations), seed=seed, store=store, progress=False,
                                progress_callback=tick)
    progress.empty()

    raster.write_monte_carlo_geotiff(aggregate, config, output)
    meta = aggregate["meta"]
    probability = aggregate["probability"]
    burned_cells = int((aggregate["burn_count"] > 0).sum())
    cell_ha = meta["scale"] ** 2 / 1e4
    record = {
        "run_id": run_id,
        "output": str(output),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.time() - started, 1),
        "scenario": {"aoi_bounds": list(area), "ignition_date": ignition_date,
                     "projection_days": int(days), "iterations": int(iterations), "seed": seed,
                     "weather_source": weather_source, "fuel_source": fuel_source},
        "weather_source_used": meta["weather_source"],
        "fuel_source_used": meta.get("fuel_source", fuel_source),
        "stats": {
            "iterations": aggregate["iterations"],
            "max_burn_probability": round(float(probability.max()), 4),
            "mean_burn_probability": round(float(probability.mean()), 5),
            "cells_burned_at_least_once": burned_cells,
            "hectares_burned_at_least_once": round(burned_cells * cell_ha, 1),
            "mean_intensity_kw_m": round(float(np.nanmean(np.where(
                aggregate["mean_intensity"] == raster.NODATA, np.nan, aggregate["mean_intensity"]))), 1)
            if burned_cells else 0.0,
            "cell_size_m": round(meta["scale"], 1),
            "grid": [meta["rows"], meta["cols"]],
        },
        "cache": meta.get("cache", {}),
    }
    _save(record)
    return record


# --- the tab ---

def render():
    _init_state()
    st.caption("Many random ignitions over one area → burn probability and fireline intensity "
               "(kW/m). No ignition point, no example fire: draw a box and run.")

    map_column, side = st.columns([3, 2], gap="medium")
    record = st.session_state.mc_result
    runs = saved_runs()

    with side:
        st.subheader("Scenario")
        area = st.session_state.mc_area
        width_km, height_km = area_km(area)
        st.caption(f"▭ {width_km:.0f} × {height_km:.0f} km — draw a rectangle on the map to change it")
        date = st.text_input("Ignition date", "2024-07-03", key="mc_date")
        days = st.number_input("Projection days", 1, 15, 3, key="mc_days")
        iterations = st.number_input("Simulations", 2, 1000, 25, step=5, key="mc_iterations")
        columns = st.columns(2)
        weather_source = columns[0].selectbox("Weather", ["gridmet", "weathernext"], key="mc_weather")
        fuel_source = columns[1].selectbox("Fuels", ["landfire", "nlcd"], key="mc_fuels")
        seed = st.number_input("Seed", 0, 10_000, 7, key="mc_seed",
                               help="Same seed and scenario reproduce the same ignition points")
        use_cache = st.checkbox("Reuse cached layers", value=True, key="mc_cache")

        estimate = grid_estimate(area, iterations, days)
        st.caption(f"Grid {estimate['rows']} × {estimate['cols']} at {estimate['cell_m']:.0f} m · "
                   f"~{estimate['megabytes']:.0f} MB memory · roughly "
                   f"{estimate['minutes']:.1f}–{estimate['minutes'] * 2:.1f} min (plus any download)")
        if estimate["megabytes"] > 2000:
            st.warning("That would need more than 2 GB for the intensity stack. Fewer simulations "
                       "or a smaller area.", icon="⚠️")

        if st.button("Run simulations", type="primary", use_container_width=True):
            try:
                st.session_state.mc_result = run_montecarlo(
                    area, date, days, iterations, int(seed), weather_source, fuel_source, use_cache)
                st.rerun()
            except Exception as error:  # surfaced, never silently swallowed
                st.error(f"{type(error).__name__}: {error}")

        if runs:
            st.divider()
            labels = {r["run_id"]: f"{r['scenario']['iterations']}× · {r['scenario']['ignition_date']} · "
                                   f"{r['scenario']['projection_days']}d · {r['run_id'][3:]}" for r in runs}
            chosen = st.selectbox("Saved runs", list(labels), format_func=lambda r: labels[r],
                                  index=0, key="mc_saved")
            if st.button("Load", use_container_width=True):
                st.session_state.mc_result = next(r for r in runs if r["run_id"] == chosen)
                st.rerun()

    with map_column:
        band = st.selectbox("Layer", list(BAND_LABELS), format_func=lambda b: BAND_LABELS[b],
                            key="mc_band", disabled=record is None)
        threshold = 0.0
        if record and band in ("burn_probability", "burn_count"):
            threshold = st.slider("Hide cells below", 0.0, 1.0, 0.0, 0.05, key="mc_threshold",
                                  help="Fraction of the maximum; 0 shows every cell that ever burned")
        fmap = build_map(st.session_state.mc_area, record, band, threshold)
        clicked = st_folium(fmap, height=560, use_container_width=True,
                            returned_objects=["last_active_drawing"], key="mc_map")
        drawn = _drawn_area((clicked or {}).get("last_active_drawing"))
        if drawn and drawn != st.session_state.mc_area:
            st.session_state.mc_area = drawn
            st.session_state.mc_result = None  # a new area invalidates the shown result
            st.rerun()

        if record:
            stats = record["stats"]
            metrics = st.columns(5)
            metrics[0].metric("Simulations", stats["iterations"])
            metrics[1].metric("Max burn probability", f"{stats['max_burn_probability']:.0%}")
            metrics[2].metric("Ever burned", f"{stats['hectares_burned_at_least_once']:,.0f} ha")
            metrics[3].metric("Mean intensity", f"{stats['mean_intensity_kw_m']:,.0f} kW/m")
            metrics[4].metric("Cell size", f"{stats['cell_size_m']:.0f} m")
            scenario = record["scenario"]
            st.caption(f"{scenario['ignition_date']} · {scenario['projection_days']} d · seed "
                       f"{scenario['seed']} · weather {record['weather_source_used']} · fuels "
                       f"{record['fuel_source_used']} · {record['seconds']}s · {record['run_id']}")
            with open(record["output"], "rb") as handle:
                st.download_button("Download 5-band GeoTIFF", handle,
                                   file_name=f"{record['run_id']}.tif", mime="image/tiff",
                                   use_container_width=True)
            st.caption("Bands: burn count, mean/p10/p90 fireline intensity (kW/m), burn probability. "
                       "Ignitions are uniform over burnable cells — not weighted by any ignition "
                       "probability map — and suppression is not modeled.")
        else:
            st.info("Draw an area, set the scenario on the right, then run.")
