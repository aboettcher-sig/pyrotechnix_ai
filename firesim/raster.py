"""Export a simulation result to a GeoTIFF (EPSG:4326).

Band 1 is hours between ignition and when that cell burns (0 at the ignition cell); the remaining
bands carry the fire behavior pyretechnics computed for that cell. Cells that never burn
(including masked water) are the nodata value -999 in every band.
"""

import numpy as np
import rasterio
from rasterio.transform import from_bounds

NODATA = -999.0

# Band order of the exported GeoTIFF: published name -> matrix key ("hours" is derived).
BANDS = {
    "hours_before_burn": "hours",
    "flame_length_m": "flame_length",
    "fireline_intensity_kw_m": "fireline_intensity",
    "fire_type": "fire_type",
    "spread_rate_m_min": "spread_rate",
}


def hours_before_burn(results) -> np.ndarray:
    """Per-cell hours from ignition to burn, with -999 where a cell never burns.

    pyretechnics stores `time_of_arrival` in minutes, initialized to NaN and set to the
    simulation start time at the ignition cell; masked water is also NaN. So the ignition
    cell resolves to 0 h and every unburned cell to the nodata value.
    """
    time_of_arrival = results["matrices"]["time_of_arrival"]
    start_minutes = results["meta"]["start_minutes"]
    burned = np.isfinite(time_of_arrival)

    hours = np.full(time_of_arrival.shape, NODATA, dtype="float32")
    hours[burned] = (time_of_arrival[burned] - start_minutes) / 60.0
    return hours


def stack_bands(results) -> np.ndarray:
    """(bands, rows, cols) float32 stack in BANDS order, nodata where a cell never burns."""
    hours = hours_before_burn(results)
    burned = hours != NODATA
    matrices = results["matrices"]
    layers = []
    for key in BANDS.values():
        if key == "hours":
            layers.append(hours)
        else:
            values = np.asarray(matrices[key], dtype="float32")
            layers.append(np.where(burned, values, NODATA).astype("float32"))
    return np.stack(layers)


def write_geotiff(results, config, output_path) -> str:
    """Write the fire-behavior bands as an EPSG:4326 GeoTIFF and return its path."""
    stack = stack_bands(results)
    rows, cols = stack.shape[1:]
    west, south, east, north = config.aoi_bounds
    transform = from_bounds(west, south, east, north, cols, rows)

    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=stack.shape[0],
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=NODATA,
        compress="deflate",
    ) as dataset:
        dataset.write(stack)
        for index, name in enumerate(BANDS, start=1):
            dataset.set_band_description(index, name)

    return str(output_path)
