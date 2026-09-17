"""Export a simulation result to a single-band GeoTIFF (EPSG:4326).

Pixel value = hours between ignition and when that cell burns (0 at the ignition cell).
Cells that never burn (including masked water) are written as the nodata value -999.
"""

import numpy as np
import rasterio
from rasterio.transform import from_bounds

NODATA = -999.0


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


def write_geotiff(results, config, output_path) -> str:
    """Write the hours-before-burn grid as an EPSG:4326 GeoTIFF and return its path."""
    hours = hours_before_burn(results)
    rows, cols = hours.shape
    west, south, east, north = config.aoi_bounds
    transform = from_bounds(west, south, east, north, cols, rows)

    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=NODATA,
        compress="deflate",
    ) as dataset:
        dataset.write(hours, 1)
        dataset.set_band_description(1, "hours_before_burn")

    return str(output_path)
