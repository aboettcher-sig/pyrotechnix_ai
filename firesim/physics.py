"""Derived physical quantities: unit conversions, dead fuel moisture, fuel mapping.

These turn raw GRIDMET/NLCD/LANDFIRE arrays into the units pyretechnics expects.
"""

import numpy as np
from pyretechnics.fuel_models import list_fuel_model_numbers

# NLCD land-cover class -> Scott & Burgan 40 fuel model number.
# Non-burnable codes (91-99) stop spread on water/urban/barren/agriculture.
NLCD_TO_FUEL = {
    11: 98,   # open water
    12: 92,   # perennial ice/snow
    21: 91, 22: 91, 23: 91, 24: 91,  # developed
    31: 99,   # barren
    41: 186,  # deciduous forest (TL6)
    42: 185,  # evergreen forest (TL5)
    43: 165,  # mixed forest (TU5)
    51: 142, 52: 142,             # shrub (SH2)
    71: 102, 72: 102, 73: 102, 74: 102,  # herbaceous/grass (GR2)
    81: 101,  # pasture/hay (GR1)
    82: 93,   # cultivated crops (agriculture, non-burnable)
    90: 188,  # woody wetlands (TL8)
    95: 102,  # herbaceous wetlands (GR2)
}
DEFAULT_FUEL = 101  # fallback: low-load dry grass


def nlcd_to_fuel_model(landcover: np.ndarray) -> np.ndarray:
    """Map an NLCD class raster to a Scott & Burgan 40 fuel-model raster."""
    fuel = np.full(landcover.shape, DEFAULT_FUEL, dtype="float32")
    for code, model in NLCD_TO_FUEL.items():
        fuel[landcover == code] = model
    return fuel


CANOPY_LAYERS = ("canopy_cover", "canopy_height", "canopy_base_height", "canopy_bulk_density")

# LANDFIRE 2023 canopy encodings -> pyretechnics units (matches pyretechnics.load_landfire).
#   cube key               raw key  multiplier  max    raw -> model
LANDFIRE_CANOPY = {
    "canopy_cover":        ("cc",  0.01, 0.95),  # percent       -> fraction 0-1
    "canopy_height":       ("ch",  0.10, 51.0),  # m x 10        -> m
    "canopy_base_height":  ("cbh", 0.10, 10.0),  # m x 10        -> m
    "canopy_bulk_density": ("cbd", 0.01, 0.45),  # kg/m3 x 100   -> kg/m3
}

VALID_FUEL_MODELS = np.array(sorted(list_fuel_model_numbers()))
NONBURNABLE_BARREN = 99  # NB9


def sanitize_fuel_model(fuel_model: np.ndarray) -> np.ndarray:
    """Replace any code pyretechnics does not define (fill values, masked cells) with NB9.

    pyretechnics looks fuel models up in an unchecked C array indexed by the code, so an
    unknown code (e.g. LANDFIRE's -9999) reads garbage memory instead of raising an error.
    """
    finite = np.where(np.isfinite(fuel_model), fuel_model, NONBURNABLE_BARREN)
    codes = np.rint(np.clip(finite, -1, 1000)).astype("int32")
    return np.where(np.isin(codes, VALID_FUEL_MODELS), codes, NONBURNABLE_BARREN).astype("float32")


def landfire_to_canopy(raw: dict) -> dict:
    """Convert raw LANDFIRE CC/CH/CBH/CBD arrays to pyretechnics canopy cubes.

    Values are clipped to LANDFIRE's documented ranges (fill and non-finite -> 0). Cells without
    canopy cover get zero height/base/density, and base height never exceeds canopy height.
    """
    canopy = {
        key: np.clip(np.where(np.isfinite(raw[name]), raw[name], 0.0) * multiplier, 0.0, upper)
        .astype("float32")
        for key, (name, multiplier, upper) in LANDFIRE_CANOPY.items()
    }
    height, base = canopy["canopy_height"], canopy["canopy_base_height"]
    canopy["canopy_base_height"] = np.where(height > 0.0, np.minimum(base, height), base)
    no_canopy = canopy["canopy_cover"] <= 0.0
    for arr in canopy.values():
        arr[no_canopy] = 0.0
    return canopy


def gridmet_to_weather(stack: dict):
    """Convert a GRIDMET band stack to pyretechnics weather units.

    Returns (wind_speed_kmh, upwind_direction_deg, temperature_c, relative_humidity_pct, fm100_pct).
    """
    wind_speed = stack["vs"] * 3.6                         # m/s -> km/hr
    upwind_direction = stack["th"]                         # deg CW from N (direction FROM)
    temperature_c = (stack["tmmx"] + stack["tmmn"]) / 2.0 - 273.15
    relative_humidity = (stack["rmin"] + stack["rmax"]) / 2.0
    return wind_speed, upwind_direction, temperature_c, relative_humidity, stack["fm100"]


def simard_emc(temp_c: np.ndarray, relative_humidity: np.ndarray) -> np.ndarray:
    """Equilibrium moisture content (%) via the Simard (1968) equations."""
    temp_f = temp_c * 9.0 / 5.0 + 32.0
    rh = np.clip(relative_humidity, 0.0, 100.0)
    return np.where(
        rh < 10.0,
        0.03229 + 0.281073 * rh - 0.000578 * rh * temp_f,
        np.where(
            rh < 50.0,
            2.22749 + 0.160107 * rh - 0.014784 * temp_f,
            21.0606 + 0.005565 * rh**2 - 0.00035 * rh * temp_f - 0.483199 * rh,
        ),
    )


def dead_fuel_moisture(temp_c, relative_humidity, fm100_pct):
    """Return (1hr, 10hr, 100hr) dead fuel moisture as fractions (kg/kg).

    1hr/10hr are modeled from equilibrium moisture content; 100hr is GRIDMET's fm100.
    """
    emc = simard_emc(temp_c, relative_humidity)
    dead_1hr = emc / 100.0
    dead_10hr = np.clip(emc * 1.2, 0.0, None) / 100.0
    dead_100hr = fm100_pct / 100.0
    return dead_1hr, dead_10hr, dead_100hr


def wind_uv_to_speed_kmh(u, v):
    """Wind speed (km/hr) from eastward/northward components (m/s)."""
    return np.hypot(u, v) * 3.6


def wind_uv_to_upwind_direction(u, v):
    """Meteorological FROM direction (degrees clockwise from North) of a u/v wind."""
    return (270.0 - np.degrees(np.arctan2(v, u))) % 360.0


def relative_humidity_from_dewpoint(temp_c, dewpoint_c):
    """Relative humidity (%) from temperature and dewpoint (both C) via the Magnus formula."""
    def _sat(t):
        return np.exp(17.625 * t / (243.04 + t))

    return np.clip(100.0 * _sat(dewpoint_c) / _sat(temp_c), 0.0, 100.0)


def dead_fuel_moisture_from_emc(temp_c, relative_humidity):
    """Return (1hr, 10hr, 100hr) dead fuel moisture fractions from EMC only (no fm100 source)."""
    emc = simard_emc(temp_c, relative_humidity)
    dead_1hr = emc / 100.0
    dead_10hr = np.clip(emc * 1.2, 0.0, None) / 100.0
    dead_100hr = np.clip(emc * 1.4, 0.0, None) / 100.0
    return dead_1hr, dead_10hr, dead_100hr
