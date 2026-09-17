# Integrating the data cache: report

2026-09-17 · how `guiat/fetch-data` fits with the LANDFIRE work, the severity work now on `main`, and the agent/UI. No code changes yet.

> **Status, 2026-09-17:** the cache is now implemented on `feature/fire-agent-phase0`, taking the
> design from `guiat/fetch-data` (`firesim/cache.py`, `pyroSim fetch`, `--cache-dir`,
> `prepare_area`/`cache_status` tools, Prepare button and cache panel in the app). The key
> includes `fuel_source` and the LANDFIRE version (section 4b), and weather keys exclude fuels
> entirely. What remains is upstream reconciliation: merging PR #1, aligning severity with
> `ceeb42e`, and deciding whether this implementation or `guiat/fetch-data` becomes the shared one.

## 1. What is actually on the repo

There is **no open PR for the cache** — only PR #1 (LANDFIRE, still open). The cache work is a pushed branch, and a second piece of work already landed on `main`:

| Where | What | Touches |
| --- | --- | --- |
| `origin/main` @ `ceeb42e` | "add intensity and severity layers to CLI outputs" (Guillaume) | `cli.py`, `model.py`, `raster.py`, `viz.py`, notebooks, README |
| `origin/guiat/fetch-data` (no PR) | `firesim/cache.py`, a `pyroSim fetch` subcommand, `--cache-dir`, lazy EE init | `cli.py`, `model.py`, `config.py`, `__init__.py`, README |
| PR #1 `feature/landfire-2023-fuels` | LANDFIRE 2023 fuels + canopy, crown fire | `gee.py`, `model.py`, `physics.py`, `config.py`, docs, notebooks |
| `feature/fire-agent-phase0` (local) | agent, Streamlit app, mock mode, maps, `--summary-json`, `--fuel-source`, severity module | `cli.py`, `model.py`, `raster.py`, plus `agents/`, `app/` |

All four touch `cli.py` and `model.py`. That is the integration problem, not the cache itself.

## 2. What the cache branch does

Well-designed and close to what the roadmap asked for:

- **Two cache groups**, keyed by a hash of the parameters that determine their contents:
  - `static/` — slope, aspect, landcover, water. Depends only on the AOI grid.
  - `weather/` — the `WeatherStack` cubes. Depends on the grid *and* date/backend/percentile.
- **Consequence:** changing only the **ignition point** is a full cache hit; changing only the **date** refetches weather alone.
- **`pyroSim fetch`** pre-populates a cache for an AOI/date (`--static-only` for just the static half); `run` also populates on a miss, so `fetch` is optional.
- **Lazy Earth Engine init** — a fully cached run needs no EE auth and no network at all.
- Storage is `arrays.npz` + a `manifest.json` per key. The example caches in the branch are small: 82 KB static, 348 KB weather.

## 3. What it buys us, measured

Timings from a real 10-day Shelly run on this laptop (0.24° × 0.15°, 123 m cells):

| Stage | Time | Depends on |
| --- | --- | --- |
| Earth Engine init | 1.0 s | — (skipped entirely on a full hit) |
| Topography fetch | 0.7 s | AOI grid |
| LANDFIRE fuels + canopy fetch | 1.6 s | AOI grid |
| Weather fetch (GRIDMET, 11 days) | 5.9 s | AOI grid + date |
| **Fetch subtotal** | **7.7 s** | |
| Spread engine | 3.7 s | everything |
| **Total** | **11.4 s** | |

So **about two thirds of a run is fetching**, and weather is the single biggest piece.

What that means for the thing you actually want — many runs in one drawn area:

- **Same AOI and date, different ignition points:** 11.4 s → **~3.7 s per run**, roughly **3× faster**, and offline.
- **A 20-member ensemble** varying ignition only: ~228 s → **~82 s serial**, and roughly **15–25 s** across cores. That is what makes burn probability interactive rather than a coffee break.
- **Varying weather** (percentiles or dates) still pays the 5.9 s weather fetch per distinct weather key, but keeps the 2.3 s static half. Worth keying the cache carefully so percentile members do not collide.

## 4. Conflicts to resolve before it can be used

**a. Two severity implementations.** `main` now writes intensity and a flame-length class as **separate sibling GeoTIFFs** (`SEVERITY_BREAKS_M = (1.2, 2.4, 3.4)` m, classes 1–4, `uint8`, nodata 0) and adds `severity_class_cells` to stats. My branch writes **one multi-band GeoTIFF** (hours, flame length, intensity, fire type, spread rate) and a `severity.py` with the same thresholds expressed in feet (4/8/11 ft ≈ 1.2/2.4/3.4 m) plus band fractions.

They agree on the physics and the breakpoints; they disagree on file layout. **Recommendation:** keep `main`'s sibling-file layout as the published contract (it is upstream and already in the README), and keep my `severity.py` as the single source of the thresholds and the fraction rollup, deleting the duplicate constants. One catch: `main` does not export **flame length** itself, only the class, so the map's severity layer would read the class raster instead of computing bands from metres. That is fine and cheaper.

**b. The cache does not know about LANDFIRE.** `static_params()` hashes `aoi_bounds`, `max_pixels`, `min_scale_m`, `water_fraction_threshold` — but PR #1 makes the static layers depend on **`fuel_source`** and, for the canopy, on **`enable_crown_fire`**. It also replaces `landcover` with fuel model + four canopy arrays and a water fraction. As-is, a LANDFIRE run and an NLCD run over the same box would **share a cache key and silently reuse the wrong layers**. Fixes needed:
- add `fuel_source` (and the LANDFIRE asset version from the manifest) to `static_params`
- store the new static dict shape (`fuel_model`, `canopy_*`, `water`)
- keep `enable_crown_fire` out of the key and apply the canopy-zeroing after load, since it is a cheap post-step

**c. `cli.py` is being restructured by three branches at once.** The cache branch factors shared arguments into `_add_data_args` and adds a `fetch` subcommand; my branch adds `--mock`, `--summary-json`, `--fuel-source`, `--no-crown-fire` and date validation; `main` added the intensity/severity outputs. These are compatible in spirit but will conflict line-by-line.

**d. Mock mode must bypass the cache** — it fetches nothing, so it should neither read nor write cache entries.

## 5. Suggested integration order

1. **Merge PR #1** (LANDFIRE) into `main`. It is reviewed, clean and everything else builds on it.
2. **Reconcile severity** between `main` and my branch (choose sibling files, single thresholds source). Small, and it unblocks the map's severity layer.
3. **Update the cache branch for LANDFIRE** (item 4b), then open it as a PR and merge.
4. **Wire the cache into the agent and UI** (section 6).
5. **Then** the ensemble runner from the roadmap, which the cache makes worthwhile.

Steps 2 and 3 are each small; the value comes from doing them in this order rather than merging in parallel.

## 6. How the agent and UI would use it

**One cache directory, not one per area.** The branch already hashes AOI/date into the key, so a single `PYROSIM_CACHE_DIR` (default `<repo>/cache`, gitignored) is enough. The agent's `run_simulation` tool passes `--cache-dir` on every call; nothing else changes.

**A "prepare this area" step in the chat.** A new tool, `prepare_area(west, south, east, north, ignition_date, projection_days, weather_source)`, shelling out to `pyroSim fetch`. The agent would call it when the user draws an area and asks for several runs, then report "area prepared in 8 s; further runs take about 4 s each". Every later run in that area is a hit.

**Report hits, so the speedup is visible.** The run record should carry `cache: {static: hit|miss, weather: hit|miss, fetch_seconds, engine_seconds}`. The CLI knows this; it can go in the summary JSON. The UI shows a "⚡ cached" badge on the run card, and the agent can say why a run was fast or slow.

**In the Streamlit app:**
- After you draw an area, a **Prepare area** button next to *Grow 2×* warms the cache for the current date, with a spinner and the resulting timing.
- The sidebar gets cache size and a **Clear cache** button (invalidation today is "delete the directory").
- Once ensembles exist, the member progress bar is honest because members after the first are pure compute.

**Ensembles (roadmap M2/M3) get two extra wins:**
- an **in-process layer cache** so members in one batch do not re-decompress the same `.npz` per run
- **atomic writes** (temp file + rename) so parallel members that miss simultaneously do not corrupt an entry

## 7. Risks worth naming

| Risk | Note |
| --- | --- |
| **Silent wrong reuse** | The main one. Any parameter that changes the layers but is missing from the key returns stale data with no error. Today `fuel_source` is missing (4b). A `manifest.json` version field plus an explicit `--no-cache` escape hatch is cheap insurance. |
| **LANDFIRE/GRIDMET updates upstream** | The catalog replaces assets in place. Record the LANDFIRE version in the manifest and treat a mismatch as a miss. |
| **Disk growth** | Small per entry (~0.4 MB in the branch's example), but ensembles over many dates add up. Worth a size report and a clear command, not a quota. |
| **Cache hides fetch failures** | A stale entry keeps a run working after the source breaks. The provenance line should say the data came from the cache and when it was fetched. |
| **Three-way `cli.py` conflict** | Predictable and mechanical, but do it deliberately in the order above rather than resolving twice. |

## 8. Decisions needed

1. Sibling GeoTIFFs (`main`) or one multi-band file (my branch) as the export contract? I suggest sibling files, and that we drop my duplicate.
2. Should `fetch` also be exposed as an agent tool (`prepare_area`), or stay a human-run command?
3. Default cache location and whether it is gitignored (suggest `<repo>/cache`, ignored).
4. Who updates the cache branch for LANDFIRE — its author, or us as part of the merge?
5. Do we want `--no-cache` and a cache-version field from the start? (Recommended.)
