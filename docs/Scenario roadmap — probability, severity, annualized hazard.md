# Scenario roadmap — burn probability, severity, annualized hazard

2026-09-17 · companion to [Fire weather agent — system design.md](Fire%20weather%20agent%20—%20system%20design.md), which covers the agent, the CLI contract and Phase 0.

## 1. What the discussion asked for

A ladder of questions the agent should answer for a point or region the user picks on the map. Each step reuses the machinery of the one before it.

| # | Question | What it needs |
| --- | --- | --- |
| 1 | Where does the fire front get to in the next ~10 days? | One deterministic run per scenario |
| 2 | What is the burn probability for this area over the next 10 days? | Many runs (Monte Carlo), stacked into a per-cell member fraction |
| 3 | What is the burn severity for both of those? | Fireline intensity / flame length per cell, classified into operational bands |
| 4 | What is the annualized hazard for this area? | Ignition-probability sampling over a fire-season window, many Monte Carlo runs, normalized to a year |
| 5 | How does the fire regime change with climate? | **Parked.** Everything except this is in scope for now |

Two sub-questions attach to *every* row above, and are owned by the effects workstream:

- **Good fire or bad fire?** — a filter over the predicted behavior.
- **What is the landscape readiness?** — a score for the area given that behavior.

### Terms we agreed to use carefully

- **Hazard, not risk.** Where the discussion said "annualized risk", we mean **hazard**: burn probability and intensity from the physics, with no values-at-risk in the calculation. Risk (hazard × exposure × response function) is the effects workstream's job. The agent must use the word "hazard" for question 4.
- **Severity is reported as intensity.** The team wants intensity values (fireline intensity, flame length) under the word "severity". In fire science, severity normally means the ecological effect on soil and vegetation, which we do not model. **Decision:** the agent computes and labels *fireline intensity and flame-length bands*, and says that explicitly, so nobody reads it as burn-severity mapping (BARC/dNBR).
- **Burn probability is a member fraction.** Cells burned ÷ members, the FSPro convention already recorded in the system design doc.

### Thresholds named in the discussion

These belong in one config file, not scattered in prompts, so the agent quotes numbers it was given rather than inventing them.

- **Flame length, suppression interpretation:** under 4 ft, hand crews and hand line can hold it; 4–8 ft, too intense for direct attack at the head, but dozers, engines and aircraft can work; above 8 ft (some use 10 ft), stay out of the head. These match the four bands already cited in the system design doc.
- **Burn probability:** "above about 0.05%" was named as the level that starts to matter. **Confirm before use:** 0.05% per year per cell, or per season? At what cell size? A probability threshold is meaningless without both.
- **Readiness scores** reuse these same numbers (Speaker 3 has used them before), so the thresholds file is shared with the effects workstream.

### Method agreed for annualized hazard

1. Take the **fire-season window** for the area, not the whole year, because ignitions under snow or rain are not how this is normally done. (Noted counterexample: Colorado's December fire. The window is a modeling choice we state, not a claim about when fires can happen.)
2. Take an **ignition probability map** for the area. We do not have one; Speaker 1 offered to supply one.
3. Draw a number of ignitions for the year — **20 to 1,000 in an area** was the range given — place them by that probability map, and draw weather and fuel-moisture conditions from within the window.
4. Run them all, stack the results, and that is the annual burn probability. Repeat the whole draw many times for a distribution.

### Validation

We have three 2024 California fires with mapped growth in `data/example_fires/`. Compare simulated spread against them, visually first, then with numbers.

## 2. What we have today

Working, on `feature/fire-agent-phase0` (includes the LANDFIRE merge):

- **`pyroSim run` CLI** — one deterministic run: area, ignition point, date, projection days, weather source, fuel source, crown fire on/off. Writes an hours-before-burn GeoTIFF (EPSG:4326) and a summary JSON.
- **Real inputs** — LANDFIRE 2023 fuels and canopy (30 m, CONUS), SRTM terrain, GRIDMET history or WeatherNext forecast, dead fuel moisture from weather. Crown fire works.
- **Agent** — ADK + Gemini with `run_simulation`, `compare_runs`, `show_map`, run store, cost gate. *Blocked on Vertex AI permission, tested with a stand-in model.*
- **Map + chat app** — Streamlit, time slider, run cards, click-to-pick ignition.
- **Mock mode** — same interface and grid, ~3 s, for building tooling.

Measured on the laptop: a **3-day real run over a 0.25° × 0.2° box at 138 m takes 12–17 s**, of which a large share is Earth Engine fetching. Mock is ~3 s.

Example fire data, all EPSG:3310, layers `source_perimeters`, `selected_footprints`, `monotonic_footprints` (cumulative) and `growth_increments` (new area per step), with `observation_time_utc` and areas in m²:

| Fire | Footprints | Window | First → final | Extent | Note |
| --- | --- | --- | --- | --- | --- |
| **Shelly** | 9 | 2024-07-03 15:00 → 07-16 03:23 UTC | 224 → 15,527 ac | 0.16° × 0.09° | **Start here.** First footprint is near ignition, so we can simulate the whole fire |
| **Airport** | 3 | 2024-09-11 19:10 → 09-13 03:52 UTC | 22,910 → 23,578 ac | 0.18° × 0.11° | Already 22.9k ac at first mapping; only usable as footprint-to-footprint growth |
| **Park** | 11 | 2024-07-26 18:45 → 08-12 02:51 UTC | 239,153 → 431,206 ac | 0.57° × 0.62° | Large; needs coarser cells or a pre-cached run |

Observation gaps are irregular (0.5 h to 165 h), so scoring has to compare *at observation times*, not at fixed 24 h steps.

## 3. Gaps, per question

| Question | Missing today |
| --- | --- |
| 1. Fire front, 10 days | Mostly there. Needs: ignition **regions** (not just points), a start hour, and a check that a 10-day horizon is affordable and that weather covers it |
| 2. Burn probability | Everything: no way to vary inputs, no batch runner, no stacking, no probability raster or thresholds |
| 3. Severity | Flame length and fireline intensity are computed by pyretechnics but **thrown away** — only summary stats survive; nothing is exported per cell, so no bands, no percentiles |
| 4. Annualized hazard | Ignition probability map (external), season window definition, condition sampling from history, normalization to a year, and far more compute than we do now |
| 5. Climate | Parked |
| Sub: good/bad fire, readiness | Consume products of 2 and 3; owned by the effects workstream; needs the thresholds file and the rasters |

**The single biggest blocker for questions 2 and 4 is not the physics — it is that every run re-fetches its static layers from Earth Engine.** Fuels, canopy and terrain are identical across members of an ensemble. Fetching them once per area and reusing them is what makes 20 members, or 1,000, practical.

## 4. Building blocks, in dependency order

### B1. Export the rasters we already compute
pyretechnics returns `time_of_arrival`, `fire_type`, `flame_length`, `spread_rate` and fireline intensity per cell. Today only arrival time is written. Write a multi-band GeoTIFF (or one file per variable) plus the existing summary. **Unblocks question 3 immediately** and costs nothing extra to compute.

### B2. Cache the static inputs per area
Fetch fuels, canopy and terrain once per (area, cell size, fuel source), store them locally keyed by a hash, and reuse across runs. Add a provenance record (LANDFIRE version, fetch date). Without this, ensembles are dominated by network time.

### B3. Scenario + ensemble runner
A scenario file describing one base case plus what varies, and a runner that executes members in parallel across cores, with a fixed seed per member so any member can be reproduced.

What to vary, and where the values come from:
- **Ignition location** — sampled inside a region (question 1 also wants regions), or from an ignition probability map (question 4).
- **Weather** — for a 10-day forecast, WeatherNext already exposes ensemble percentiles (`p10, p25, p50, p75, p90`) and the config already accepts them; that is the cheapest honest weather spread we can get. For annualized, sample historical GRIDMET days from within the season window.
- **Fuel moisture** — perturb the dead fuel moisture around the modeled value; live moisture constants are a tunable too.
- **Wind** — a multiplier and a direction offset, which also answers the "30% stronger wind" what-if from the demo script.

Expose it as `pyroSim ensemble` so the contract stays "the CLI is the contract", with a summary JSON listing members and a manifest of member rasters.

### B4. Burn probability products
Stack member arrival-time rasters: per-cell **member fraction** (the probability), plus arrival-time percentiles (p10/p50/p90) so the agent can say when, not just whether. Summary: area above each probability level, using the threshold file. The agent quotes "14 of 20 members", never an adjective.

### B5. Severity (intensity) products
Per-cell flame length and fireline intensity across members: p50 and p90, and the **fraction of predicted burned area in each operational band** (<4 ft, 4–8 ft, 8–11 ft, >11 ft). Report for both the single 10-day run and the probability ensemble, which is exactly what the discussion asked ("severity for both of those scenarios"). Crown-fire cells (passive/active) are already available and belong in the same summary.

### B6. Annualized hazard
Adds to B3:
- **Season window** per area — from a supplied definition, or derived from historical fire-weather data. Stated in every answer as an assumption.
- **Ignition probability map** — ingest whatever Speaker 1 supplies, resample to the run grid, and use it as the sampling weight.
- **Ignition count** — draw from the stated range (20–1,000 per year for the area), then repeat the yearly draw many times for a distribution rather than a single number.
- **Normalization** — express the result as annual burn probability per cell, and compare against the ~0.05% threshold once its basis is confirmed.

Compute is the constraint here, so plan for coarser cells (90–120 m), the static-layer cache, parallel members, and pre-caching the demo area.

### B7. Effects hooks (sub-questions)
Both sub-questions consume B4 and B5 outputs on the same grid: good fire / bad fire as a raster and a rollup verdict, and readiness for the area. Contract already sketched in the system design doc; the thresholds file is shared so both sides use the same numbers.

### B8. Validation harness
Turn `data/example_fires` into scored comparisons:
- Rasterize `monotonic_footprints` to the run grid, in EPSG:4326 to match our output.
- **Hindcast protocol:** start from footprint *N*, run to the time of footprint *N+1* (irregular gaps, so use the actual observation times), compare growth.
- **Metrics:** area ratio (simulated ÷ observed growth), Sørensen overlap of growth areas, arrival-time error at observed perimeters, and for ensembles, probability calibration (of cells predicted 60–80%, what fraction burned).
- **Visual:** observed perimeter on the map beside the simulated arrival-time layer, which is what "visually check" in the discussion asked for.
- **Expect overprediction** — no suppression is modeled and these fires were fought. Report the gap as a finding.
- **Order:** Shelly first (starts near ignition, small), then Airport (footprint-to-footprint only), then Park (coarse cells or pre-cached).

## 5. What the agent and UI gain

New tools, each thin over a CLI command, following the Phase 0 pattern:

| Tool | Answers |
| --- | --- |
| `run_ensemble` / `get_ensemble` | Questions 2 and 4 |
| `burn_probability_summary` | Area above probability thresholds, with member counts |
| `severity_summary` | Flame-length band fractions and crown-fire share, for a run or an ensemble |
| `annualized_hazard` | Question 4, with the season window and ignition assumptions echoed back |
| `compare_to_observed` | Validation against the example fires |

UI additions: a probability layer with its own legend, a severity-band layer, the observed perimeter overlay for hindcasts, and threshold toggles. The existing time slider already works for arrival-time percentiles.

Agent guardrails to extend: the cost gate needs a **member budget** (cells × members × days), and provenance must now include member count, seed, ignition-map source and season window.

## 6. Sequencing

| Milestone | Contents | Unblocks |
| --- | --- | --- |
| **M1 — severity now** | B1 + `severity_summary` + band legend in the UI | Question 3 for single runs, with no new physics |
| **M2 — make many runs cheap** | B2 static cache + B3 ensemble runner | Everything below |
| **M3 — 10-day probability** | B4 + probability layer + WeatherNext percentile members | Question 2, plus severity percentiles for question 3 |
| **M4 — validation** | B8 on Shelly, then Airport | Credibility for everything above |
| **M5 — annualized hazard** | B6, once the ignition map arrives | Question 4 |
| **M6 — effects** | B7 with the effects workstream | Both sub-questions |
| **Parked** | Climate adjustment | — |

M1 and M4 are the cheapest credibility per hour: one exports data we already compute, the other shows we are honest about accuracy.

## 7. Decisions needed from the team

1. **Ignition probability map** — who supplies it, in what format, at what resolution, and when? M5 cannot start without it.
2. **Burn probability threshold** — is "0.05%" annual, per cell, and at what cell size?
3. **Fire season window** — supplied per area, or derived? Which months for the demo areas?
4. **Ignitions per year** — what number do we use for the demo area, and do we report a distribution across yearly draws or a single stacked probability?
5. **Members per ensemble** — 20 gives 5% resolution and is cheap; annualized needs far more. What is the ceiling for a live demo versus pre-cached?
6. **Severity wording** — confirm we report "fireline intensity / flame-length bands" and avoid the term burn severity in user-facing text.
7. **Cell size policy** — 138 m today for a 0.25° box. Do we fix a cell size per question type (finer for question 1, coarser for question 4)?
8. **Ignition regions** — for "this area", do we sample ignitions uniformly in the drawn box, or always weight by the ignition map once we have it?

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| Ensembles too slow for a live demo | Static-layer cache, parallel members, capped members, coarser cells, pre-cached demo scenarios |
| Ignition map never arrives | M5 slips; fall back to uniform sampling in the drawn area and label it clearly as unweighted |
| "Severity" read as ecological burn severity | Fixed wording in the prompt and the UI legend; stated in every answer |
| Probability presented as certainty | Always show member fractions and the member count; never a single perimeter line |
| Validation looks bad (overprediction) | Expected, because suppression is not modeled; present the gap as a measure of suppression effect |
| Thresholds drift between agent, UI and readiness scores | One shared thresholds file, quoted with its source |
