"""Monte Carlo fire simulation: many random ignitions over one fixed AOI/date/weather.

Inputs are assembled once (and reused across every iteration), then the spread engine is run N
times from random burnable ignition cells. Per-cell results are aggregated in memory into
probability and intensity statistics — no intermediate rasters are written.
"""

import warnings

import numpy as np

from .model import build_inputs, spread_once

INTENSITY_NODATA = -999.0


def _sample_ignition_cells(burnable_mask, n, rng):
    """Return n (row, col) ignition cells drawn uniformly (with replacement) from burnable land."""
    cells = np.argwhere(burnable_mask)
    if cells.size == 0:
        raise ValueError("No burnable cells in the AOI to ignite; nothing to simulate.")
    picks = rng.integers(0, len(cells), size=n)
    return [tuple(cells[i]) for i in picks]


def run_monte_carlo(config, iterations, seed=None, store=None, progress=True, progress_callback=None):
    """Run `iterations` random-ignition fires and aggregate per-cell probability + intensity.

    `progress_callback(done, total)` is called after each iteration, for UIs that draw their own
    progress bar instead of the terminal one.

    Returns a dict with the 2D aggregate bands (burn_count, mean/p10/p90 intensity, probability),
    the metadata, the iteration count, and the sampled ignition cells.
    """
    if iterations < 1:
        raise ValueError("iterations must be >= 1.")

    space_time_cubes, meta = build_inputs(config, store)
    rows, cols = meta["rows"], meta["cols"]
    rng = np.random.default_rng(seed)
    ignition_cells = _sample_ignition_cells(meta["burnable_mask"], iterations, rng)

    burn_count = np.zeros((rows, cols), dtype="int32")
    # Per-iteration fireline intensity (NaN where the cell did not burn) for exact percentiles.
    intensity_stack = np.full((iterations, rows, cols), np.nan, dtype="float32")

    cells = _progress(ignition_cells, iterations) if progress else ignition_cells
    for i, ignition_rc in enumerate(cells):
        matrices, _ = spread_once(space_time_cubes, meta, ignition_rc, config)
        burned = matrices["fire_type"] > 0
        burn_count += burned
        intensity_stack[i][burned] = matrices["fireline_intensity"][burned]
        if progress_callback:
            progress_callback(i + 1, iterations)

    aggregate = _aggregate(burn_count, intensity_stack, iterations)
    aggregate.update({"meta": meta, "iterations": iterations, "ignition_cells": ignition_cells})
    return aggregate


def _aggregate(burn_count, intensity_stack, iterations):
    """Reduce the per-iteration stack into the five output bands."""
    ever_burned = burn_count > 0
    # Cells that never burned are all-NaN slices; their warnings are expected and suppressed.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mean_intensity = np.nanmean(intensity_stack, axis=0)
        p10_intensity, p90_intensity = np.nanpercentile(intensity_stack, [10, 90], axis=0)

    # Cells that never burned have no intensity distribution -> nodata.
    for band in (mean_intensity, p10_intensity, p90_intensity):
        band[~ever_burned] = INTENSITY_NODATA

    return {
        "burn_count": burn_count.astype("float32"),
        "mean_intensity": mean_intensity.astype("float32"),
        "p10_intensity": p10_intensity.astype("float32"),
        "p90_intensity": p90_intensity.astype("float32"),
        "probability": (burn_count / iterations).astype("float32"),
    }


def _progress(iterable, total):
    """Wrap the iteration loop in a tqdm progress bar when available."""
    try:
        from tqdm import tqdm

        return tqdm(iterable, total=total, desc="Monte Carlo", unit="sim")
    except ImportError:
        return iterable
