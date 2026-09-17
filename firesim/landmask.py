"""Constrain fire spread to land using the land-cover raster.

pyretechnics treats fuel-model codes 91-99 as non-burnable, so forcing water/ice cells to a
non-burnable code stops the fire from ever entering them.
"""

import numpy as np
from scipy import ndimage

NON_LAND_CLASSES = (11, 12)   # NLCD: open water, perennial ice/snow
NONBURNABLE_WATER = 98        # pyretechnics non-burnable fuel model


def non_land_mask(landcover: np.ndarray) -> np.ndarray:
    """Boolean mask of cells that must not burn (water, ice)."""
    mask = np.zeros(landcover.shape, dtype=bool)
    for code in NON_LAND_CLASSES:
        mask |= landcover == code
    return mask


def buffer_mask(mask: np.ndarray, cells: int) -> np.ndarray:
    """Dilate a mask by N cells to form an impervious boundary the fire cannot leak across."""
    if cells <= 0:
        return mask
    return ndimage.binary_dilation(mask, iterations=cells)


def enforce_land_constraint(fuel_model: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Force non-land cells to a non-burnable fuel model."""
    constrained = fuel_model.copy()
    constrained[mask] = NONBURNABLE_WATER
    return constrained
