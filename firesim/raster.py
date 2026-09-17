"""Export simulation results to single-band GeoTIFFs (EPSG:4326).

Three products share the same AOI grid and the -999 nodata convention:
- hours: hours between ignition and burn (0 at the ignition cell).
- intensity: Byram's fireline intensity (kW/m).
- severity: flame-length fire-behavior class (1-4); modeled behavior, not satellite burn severity.

Cells that never burn (including masked water) are written as the nodata value.
"""

import numpy as np
import rasterio
from rasterio.transform import from_bounds

NODATA = -999.0
SEVERITY_NODATA = 0

# Flame-length fire-behavior classes (m), from the Fire Characteristics Chart.
SEVERITY_BREAKS_M = (1.2, 2.4, 3.4)
SEVERITY_LABELS = {
    1: "Low (<1.2 m)",
    2: "Moderate (1.2-2.4 m)",
    3: "High (2.4-3.4 m)",
    4: "Very high (>3.4 m)",
}


def _burned_mask(results) -> np.ndarray:
    """Cells that actually burned (fire_type > 0); excludes unburned and non-burnable water."""
    return results["matrices"]["fire_type"] > 0


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


def intensity_map(results) -> np.ndarray:
    """Fireline intensity (kW/m) per burned cell, with -999 where a cell never burns."""
    fireline_intensity = results["matrices"]["fireline_intensity"]
    burned = _burned_mask(results)

    intensity = np.full(fireline_intensity.shape, NODATA, dtype="float32")
    intensity[burned] = fireline_intensity[burned]
    return intensity


def severity_map(results, breaks=SEVERITY_BREAKS_M) -> np.ndarray:
    """Flame-length severity class (1-4) per burned cell, with 0 where a cell never burns."""
    flame_length = results["matrices"]["flame_length"]
    burned = _burned_mask(results)

    # np.digitize maps flame length to class index 1..len(breaks)+1.
    classes = (np.digitize(flame_length, breaks) + 1).astype("uint8")
    severity = np.full(flame_length.shape, SEVERITY_NODATA, dtype="uint8")
    severity[burned] = classes[burned]
    return severity


def _write_geotiff(array, config, output_path, nodata, band_description) -> str:
    """Write a single-band array as an EPSG:4326 GeoTIFF over the AOI and return its path."""
    rows, cols = array.shape
    west, south, east, north = config.aoi_bounds
    transform = from_bounds(west, south, east, north, cols, rows)

    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype=array.dtype,
        crs="EPSG:4326",
        transform=transform,
        nodata=nodata,
        compress="deflate",
    ) as dataset:
        dataset.write(array, 1)
        dataset.set_band_description(1, band_description)

    return str(output_path)


def write_geotiff(results, config, output_path) -> str:
    """Write the hours-before-burn grid as an EPSG:4326 GeoTIFF and return its path."""
    return _write_geotiff(hours_before_burn(results), config, output_path, NODATA, "hours_before_burn")


def write_intensity_geotiff(results, config, output_path) -> str:
    """Write the fireline-intensity (kW/m) grid as an EPSG:4326 GeoTIFF and return its path."""
    return _write_geotiff(intensity_map(results), config, output_path, NODATA, "fireline_intensity_kw_m")


def write_severity_geotiff(results, config, output_path) -> str:
    """Write the flame-length severity-class grid as an EPSG:4326 GeoTIFF and return its path."""
    return _write_geotiff(severity_map(results), config, output_path, SEVERITY_NODATA, "flame_length_severity_class")


def _write_multiband_geotiff(bands, descriptions, config, output_path, nodata) -> str:
    """Write a stack of same-shape arrays as a multiband EPSG:4326 GeoTIFF over the AOI."""
    rows, cols = bands[0].shape
    west, south, east, north = config.aoi_bounds
    transform = from_bounds(west, south, east, north, cols, rows)

    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=len(bands),
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=nodata,
        compress="deflate",
    ) as dataset:
        for index, (array, description) in enumerate(zip(bands, descriptions), start=1):
            dataset.write(array.astype("float32"), index)
            dataset.set_band_description(index, description)

    return str(output_path)


MONTE_CARLO_BANDS = (
    ("burn_count", "burn_count"),
    ("mean_intensity", "mean_fireline_intensity_kw_m"),
    ("p10_intensity", "p10_fireline_intensity_kw_m"),
    ("p90_intensity", "p90_fireline_intensity_kw_m"),
    ("probability", "burn_probability"),
)


def write_monte_carlo_geotiff(aggregate, config, output_path) -> str:
    """Write the 5-band Monte Carlo aggregate as an EPSG:4326 GeoTIFF and return its path.

    Bands: burn count, mean/p10/p90 fireline intensity (kW/m), burn probability. Intensity bands
    use -999 nodata where a cell never burned; count and probability use 0 (a valid value).
    """
    bands = [aggregate[key] for key, _ in MONTE_CARLO_BANDS]
    descriptions = [desc for _, desc in MONTE_CARLO_BANDS]
    return _write_multiband_geotiff(bands, descriptions, config, output_path, NODATA)
