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
  --output-name tahoe_fire.tif
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

Run `pyroSim run --help` for the full reference.

## Output

A single-band GeoTIFF (`EPSG:4326`) named after `--output-name`:

- **Pixel value** = hours between ignition and when the cell burns.
- **`0`** = the ignition cell.
- A cell that burns two weather cycles later = `2 × weather step` hours (e.g. 12 h for a
  6‑hourly WeatherNext step, 48 h for daily GRIDMET).
- **`-999`** = cells that never burn (including masked water); this is the nodata value.
