"""Observed fire growth from data/example_fires (NIFC/IRWIN perimeter exports).

Each GeoPackage holds `monotonic_footprints`: cumulative mapped perimeters with an observation
time, in EPSG:3310. This module reprojects them to the EPSG:4326 grid the simulator works on and
derives a scenario (ignition point, area of interest, start date) from the first footprint, so a
run can be compared against what the fire actually did.
"""

import functools
from pathlib import Path

import geopandas as gpd

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "example_fires"
LAYER = "monotonic_footprints"
ACRES_PER_M2 = 1.0 / 4046.86
AOI_PAD_FRACTION = 0.25   # pad the observed extent so the fire has room to run
AOI_MIN_PAD_DEG = 0.03


def available_fires() -> list[str]:
    return sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir()) if DATA_DIR.exists() else []


@functools.lru_cache(maxsize=8)
def load_footprints(fire: str) -> gpd.GeoDataFrame:
    """Cumulative observed perimeters in EPSG:4326, ordered in time."""
    matches = sorted((DATA_DIR / fire).glob("*.gpkg")) if (DATA_DIR / fire).is_dir() else []
    if not matches:
        raise ValueError(f"No example fire {fire!r} in {DATA_DIR} (have: {', '.join(available_fires())}).")
    footprints = gpd.read_file(matches[0], layer=LAYER).sort_values("sequence").to_crs(4326)
    footprints["acres"] = footprints["cumulative_area_m2"] * ACRES_PER_M2
    footprints["time"] = footprints["observation_time_utc"]
    return footprints.reset_index(drop=True)


def scenario(fire: str) -> dict:
    """A runnable scenario derived from the first observed footprint.

    The ignition point is a representative point inside the first footprint (not its centroid,
    which can fall outside a concave perimeter), and the area of interest is the padded extent of
    all observations, so the simulated fire has room to spread past what was observed.
    """
    footprints = load_footprints(fire)
    first = footprints.iloc[0]
    west, south, east, north = footprints.total_bounds
    pad_x = max((east - west) * AOI_PAD_FRACTION, AOI_MIN_PAD_DEG)
    pad_y = max((north - south) * AOI_PAD_FRACTION, AOI_MIN_PAD_DEG)
    point = first.geometry.representative_point()
    start, end = footprints["time"].iloc[0], footprints["time"].iloc[-1]
    span_hours = (gpd.pd.Timestamp(end) - gpd.pd.Timestamp(start)).total_seconds() / 3600.0
    return {
        "fire": fire,
        "incident_name": str(first.get("incident_name", fire)),
        "aoi_bounds": [round(west - pad_x, 4), round(south - pad_y, 4),
                       round(east + pad_x, 4), round(north + pad_y, 4)],
        "ignition_lonlat": [round(point.x, 5), round(point.y, 5)],
        "ignition_date": start[:10],
        "first_observation": start,
        "last_observation": end,
        "observed_days": round(span_hours / 24.0, 1),
        "first_acres": round(float(first["acres"])),
        "final_acres": round(float(footprints["acres"].iloc[-1])),
        "observations": [
            {"sequence": int(row.sequence), "time": row.time, "acres": round(float(row.acres))}
            for row in footprints.itertuples()
        ],
    }


def perimeter_features(fire: str) -> list[dict]:
    """Observed perimeters as {label, hours_after_start, acres, geojson} for map overlays."""
    footprints = load_footprints(fire)
    start = gpd.pd.Timestamp(footprints["time"].iloc[0])
    features = []
    for row in footprints.itertuples():
        hours = (gpd.pd.Timestamp(row.time) - start).total_seconds() / 3600.0
        features.append({
            "label": f"{row.time[:16].replace('T', ' ')} UTC · {row.acres:,.0f} ac",
            "hours_after_start": round(hours, 1),
            "acres": round(float(row.acres)),
            "geojson": gpd.GeoSeries([row.geometry], crs=4326).__geo_interface__["features"][0]["geometry"],
        })
    return features
