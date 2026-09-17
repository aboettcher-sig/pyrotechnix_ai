"""Fire intensity products and the operational bands used to read them.

We report **fireline intensity and flame length**, not ecological burn severity (BARC/dNBR),
which this model does not produce. The flame-length bands are the standard suppression
interpretation, and the same numbers are meant to be shared with the readiness scoring, so keep
them here rather than duplicating them in prompts or UI code.
"""

import numpy as np

FEET_PER_METRE = 3.28084

# (lower ft, upper ft, name, what it means operationally)
FLAME_LENGTH_BANDS = (
    (0.0, 4.0, "under 4 ft", "hand crews and hand line can generally hold the fire"),
    (4.0, 8.0, "4-8 ft", "too intense for direct attack at the head; dozers, engines and aircraft can work"),
    (8.0, 11.0, "8-11 ft", "torching, crowning and spotting; control at the head likely ineffective"),
    (11.0, float("inf"), "over 11 ft", "major runs; head attack ineffective"),
)

# Burn-probability levels used when summarizing ensembles (fractions, not percent).
# 0.0005 (0.05%) was named as the level that starts to matter; confirm its basis before relying on it.
BURN_PROBABILITY_LEVELS = (0.0005, 0.01, 0.05, 0.2, 0.5, 0.8)


def flame_length_bands(flame_length_m: np.ndarray, burned: np.ndarray, cell_area_ha: float) -> list[dict]:
    """Share of burned area in each operational flame-length band."""
    flame_ft = np.where(burned, flame_length_m * FEET_PER_METRE, np.nan)
    total = int(np.count_nonzero(burned))
    bands = []
    for lower, upper, name, meaning in FLAME_LENGTH_BANDS:
        in_band = burned & (flame_ft >= lower) & (flame_ft < upper)
        cells = int(np.count_nonzero(in_band))
        bands.append({
            "band": name,
            "lower_ft": lower,
            "upper_ft": None if upper == float("inf") else upper,
            "meaning": meaning,
            "cells": cells,
            "hectares": round(cells * cell_area_ha, 1),
            "fraction_of_burned": round(cells / total, 3) if total else 0.0,
        })
    return bands


def summarize(matrices: dict, cell_size_m: float) -> dict:
    """Intensity summary for one run: flame-length bands, intensity percentiles, crown fire."""
    fire_type = matrices["fire_type"]
    burned = fire_type > 0
    cell_area_ha = cell_size_m**2 / 1e4
    flame = np.asarray(matrices["flame_length"], dtype="float64")
    intensity = np.asarray(matrices.get("fireline_intensity", np.zeros_like(flame)), dtype="float64")
    burned_flame = flame[burned]
    burned_intensity = intensity[burned]

    def percentiles(values):
        if values.size == 0:
            return {"p50": 0.0, "p90": 0.0, "max": 0.0}
        return {
            "p50": round(float(np.nanpercentile(values, 50)), 2),
            "p90": round(float(np.nanpercentile(values, 90)), 2),
            "max": round(float(np.nanmax(values)), 2),
        }

    return {
        "flame_length_m": percentiles(burned_flame),
        "fireline_intensity_kw_m": percentiles(burned_intensity),
        "flame_length_bands": flame_length_bands(flame, burned, cell_area_ha),
        "passive_crown_cells": int(np.count_nonzero(fire_type == 2)),
        "active_crown_cells": int(np.count_nonzero(fire_type == 3)),
        "crown_fraction_of_burned": round(float(np.count_nonzero(fire_type >= 2) / max(np.count_nonzero(burned), 1)), 3),
    }
