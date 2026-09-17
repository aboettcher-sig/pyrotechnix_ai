# pyroSim — Wildfire Spread CLI

`pyroSim` simulates how a wildfire could spread from a chosen ignition point using open
geospatial data ([pyretechnics](https://github.com/pyregence/pyretechnics) + Google Earth
Engine). It saves a single **EPSG:4326 GeoTIFF** where each pixel is the number of **hours
before that cell burns** (`0` at the ignition cell, `-999` where it never burns).

## Prerequisites

- Python 3.10+
- A Google **Earth Engine** account with an approved project (and a WeatherNext data request if
  you want the `weathernext` forecast backend).

## 1. Install requirements

The project ships an editable install that also registers the `pyroSim` command. Pick one:

### Option A — uv (recommended)

```bash
uv venv                       # create .venv (skip if it already exists)
uv pip install -e .           # install deps from requirements.txt + the pyroSim command
```

### Option B — pip

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Either option reads dependencies from [requirements.txt](requirements.txt) and installs the
`pyroSim` console script into the environment.

## 2. Configure Earth Engine

Create a `.env` file in the repo root with your Earth Engine project id:

```
EE_PROJECT=your-earth-engine-project
```

On the first run, Earth Engine will prompt you to authenticate in the browser.

## 3. Run the CLI

```bash
pyroSim run \
  --aoi-bounds -120.55 39.00 -120.30 39.20 \
  --ignition-lonlat -120.45 39.10 \
  --ignition-date 2026-09-10 \
  --projection-days 5 \
  --weather-source weathernext \
  --output-name tahoe_fire.tif \
  --intensity \
  --severity
```

If you used the pip venv, activate it first (`source .venv/bin/activate`). With uv you can also
run it via `uv run pyroSim run ...` or `.venv/bin/pyroSim run ...`. The equivalent module form is
`python -m firesim run ...`.

### Arguments

| Argument | Required | Description |
| --- | --- | --- |
| `--aoi-bounds WEST SOUTH EAST NORTH` | yes | Area of interest as lon/lat (`west south east north`). |
| `--ignition-lonlat LON LAT` | yes | Ignition point; must fall inside the AOI. |
| `--ignition-date YYYY-MM-DD` | yes | Date the fire departs. |
| `--projection-days N` | yes | Projection horizon in days. |
| `--weather-source {gridmet,weathernext}` | no | Weather backend (default: `gridmet`). |
| `--output-name NAME`, `-o NAME` | yes | Output GeoTIFF filename (a `.tif` extension is added if missing). |
| `--intensity` | no | Also save a fireline-intensity GeoTIFF as `<name>_intensity.tif`. |
| `--severity` | no | Also save a flame-length severity-class GeoTIFF as `<name>_severity.tif`. |

Run `pyroSim run --help` for the full reference.

## Outputs

All products are single-band GeoTIFFs (`EPSG:4326`) sharing the same AOI grid.

### Hours before burn — `<name>.tif` (always)

- **Pixel value** = hours between ignition and when the cell burns.
- **`0`** = the ignition cell.
- A cell that burns two weather cycles later = `2 × weather step` hours (e.g. 12 h for a
  6‑hourly WeatherNext step, 48 h for daily GRIDMET).
- **`-999`** = cells that never burn (including masked water); this is the nodata value.

### Fireline intensity — `<name>_intensity.tif` (with `--intensity`)

- **Pixel value** = Byram's fireline intensity in **kW/m** for each burned cell.
- **`-999`** = cells that never burn (nodata).

### Severity class — `<name>_severity.tif` (with `--severity`)

Modeled fire-behavior severity from flame-length classes (Fire Characteristics Chart), **not**
satellite burn severity (dNBR). Since the MVP is surface-fire only, values reflect surface
intensity.

| Value | Class | Flame length |
| --- | --- | --- |
| `0` | Unburned (nodata) | — |
| `1` | Low | < 1.2 m |
| `2` | Moderate | 1.2–2.4 m |
| `3` | High | 2.4–3.4 m |
| `4` | Very high | > 3.4 m |

## Reusing fetched data (cache)

Earth Engine downloads are the slow part of a run. Pass `--cache-dir DIR` to store the fetched
layers on disk and reuse them. The cache is split into two groups:

- **static** — slope, aspect, land cover, water (depend only on the AOI grid).
- **weather** — the weather cubes (depend on the AOI grid *and* the date/backend).

So changing only the **ignition point** reuses everything (no download); changing only the
**date** reuses the static layers and refetches just the weather.

### Pre-fetch, then run several ignition points

```bash
# Download once for an AOI/date into ./tahoe_cache
pyroSim fetch \
  --aoi-bounds -120.55 39.00 -120.30 39.20 \
  --ignition-date 2026-09-10 \
  --projection-days 5 \
  --weather-source weathernext \
  --cache-dir ./tahoe_cache

# Run different ignition points with no re-download
pyroSim run --aoi-bounds -120.55 39.00 -120.30 39.20 --ignition-lonlat -120.45 39.10 \
  --ignition-date 2026-09-10 --projection-days 5 --weather-source weathernext \
  --cache-dir ./tahoe_cache -o point_a.tif

pyroSim run --aoi-bounds -120.55 39.00 -120.30 39.20 --ignition-lonlat -120.40 39.05 \
  --ignition-date 2026-09-10 --projection-days 5 --weather-source weathernext \
  --cache-dir ./tahoe_cache -o point_b.tif
```

`fetch` accepts `--static-only` to download just the date-independent layers. `run` also
populates the cache on a miss, so the first `run` alone is enough to speed up later ones — the
explicit `fetch` step is optional. The AOI/date/grid parameters must match for a cache hit;
delete the cache directory to invalidate it.

Earth Engine is initialized **lazily**, only when a layer is missing from the cache. A `run`
whose AOI/date is fully cached needs no EE authentication or network call at all, so batches of
runs at different ignition points stay offline and fast.

## Monte Carlo (probabilistic burn map)

`pyroSim montecarlo` runs many simulations from **random ignition points** on the same AOI, date,
projection, and weather, then aggregates them into one multiband GeoTIFF — no intermediate files.
Inputs are assembled once and reused across every iteration, and ignition points are drawn only
from burnable land.

```bash
pyroSim montecarlo \
  --aoi-bounds -120.55 39.00 -120.30 39.20 \
  --ignition-date 2026-09-10 \
  --projection-days 5 \
  --weather-source weathernext \
  --iterations 200 \
  --seed 42 \
  --cache-dir ./tahoe_cache \
  --output-name tahoe_montecarlo.tif
```

`--iterations`/`-n` sets the number of simulations; `--seed` makes the random ignitions
reproducible; `--cache-dir` is recommended so the data is fetched once. It accepts no
`--ignition-lonlat` (the points are random).

### Output bands (`EPSG:4326`, float32)

| Band | Name | Meaning |
| --- | --- | --- |
| 1 | `burn_count` | Number of iterations the cell burned |
| 2 | `mean_fireline_intensity_kw_m` | Mean intensity (kW/m) over iterations where it burned |
| 3 | `p10_fireline_intensity_kw_m` | 10th-percentile intensity (same set) |
| 4 | `p90_fireline_intensity_kw_m` | 90th-percentile intensity (same set) |
| 5 | `burn_probability` | `burn_count / iterations` — probability of active fire within the projection window |

Intensity bands (2–4) use `-999` nodata where a cell never burned; `burn_count` and
`burn_probability` use `0` (a valid value). Memory scales with `iterations × rows × cols`, so for
very large runs keep the AOI/`--max-pixels` modest.
