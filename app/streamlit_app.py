"""pyroSim map + chat: a Streamlit front end for the pyroSim agent.

Map on the left, chat on the right, one shared state: runs the agent makes appear on the map,
and a click on the map is attached to your next message as the ignition point.

Run from the repo root:
    streamlit run app/streamlit_app.py
"""

import asyncio
import math
import os
import queue
import shutil
import sys
import threading
from pathlib import Path

import folium
import streamlit as st
from folium.plugins import Draw
from dotenv import load_dotenv
from streamlit_folium import st_folium

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "agents" / "pyrosim_agent"
sys.path.insert(0, str(REPO_ROOT / "agents"))
load_dotenv(AGENT_DIR / ".env")

from google.genai import types  # noqa: E402

from pyrosim_agent import maps, tools  # noqa: E402

APP_NAME = "pyrosim"
USER_ID = "local-user"
MAX_MAP_RUNS = 4
TOOL_LABELS = {
    "list_example_areas": "Looking up example areas",
    "run_simulation": "Running simulation",
    "get_run": "Reading run",
    "list_runs": "Listing runs",
    "compare_runs": "Comparing runs",
    "show_map": "Drawing map",
    "prepare_area": "Preparing the area (downloading layers)",
    "cache_status": "Checking the cache",
}

st.set_page_config(page_title="pyroSim", page_icon="🔥", layout="wide",
                   initial_sidebar_state="collapsed")


# --- Agent runtime: one background event loop and runner shared by the server ---

@st.cache_resource
def agent_runtime():
    from google.adk.runners import InMemoryRunner

    from pyrosim_agent.agent import root_agent

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop, InMemoryRunner(agent=root_agent, app_name=APP_NAME)


def run_on_agent_loop(coro):
    loop, _ = agent_runtime()
    return asyncio.run_coroutine_threadsafe(coro, loop)


def ensure_session() -> str:
    if "session_id" not in st.session_state:
        _, runner = agent_runtime()
        session = run_on_agent_loop(
            runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        ).result(timeout=30)
        st.session_state.session_id = session.id
    return st.session_state.session_id


def stream_agent(text: str) -> queue.Queue:
    """Start one agent turn; events (then None) arrive on the returned queue."""
    events: queue.Queue = queue.Queue()
    _, runner = agent_runtime()
    session_id = ensure_session()
    message = types.Content(role="user", parts=[types.Part(text=text)])

    async def turn():
        try:
            async for event in runner.run_async(user_id=USER_ID, session_id=session_id, new_message=message):
                events.put(event)
        except Exception as error:  # surfaced in the chat, never swallowed
            events.put(error)
        finally:
            events.put(None)

    run_on_agent_loop(turn())
    return events


# --- State ---

def init_state():
    defaults = {"messages": [], "map_runs": [], "picked": None, "mode": os.environ.get("PYROSIM_MODE", "mock")}
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    os.environ["PYROSIM_MODE"] = st.session_state.mode


def set_map_runs(run_ids):
    known = [r for r in dict.fromkeys(run_ids) if (tools.load_run_record(r) or {}).get("status") == "done"]
    st.session_state.map_runs = known[-MAX_MAP_RUNS:]
    st.session_state.pending_map_select = True  # applied before the selector is drawn next run


def drawing_bounds(drawing) -> list[float] | None:
    """(west, south, east, north) of a drawn rectangle or polygon, or None."""
    geometry = (drawing or {}).get("geometry") or {}
    if geometry.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    rings = geometry["coordinates"] if geometry["type"] == "Polygon" else [
        ring for polygon in geometry["coordinates"] for ring in polygon]
    points = [point for ring in rings for point in ring]
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return [round(min(lons), 5), round(min(lats), 5), round(max(lons), 5), round(max(lats), 5)]


def bounds_km(bounds) -> tuple[float, float]:
    west, south, east, north = bounds
    mid = math.radians((south + north) / 2)
    return ((east - west) * 111.32 * math.cos(mid), (north - south) * 110.54)


def grow_bounds(bounds, factor):
    """Scale an area about its centre (used by the grow buttons)."""
    west, south, east, north = bounds
    cx, cy = (west + east) / 2, (south + north) / 2
    half_x, half_y = (east - west) / 2 * factor, (north - south) / 2 * factor
    return [round(cx - half_x, 5), round(cy - half_y, 5), round(cx + half_x, 5), round(cy + half_y, 5)]


def finished_runs():
    return [r for r in tools.list_runs(limit=50)["runs"] if r["status"] == "done"]


def run_option_label(run_id, runs_by_id):
    run = runs_by_id.get(run_id, {})
    label = run.get("label") or run_id
    hectares = run.get("burned_hectares") or 0
    return f"{label} · {hectares:,.0f} ha · {run.get('mode')} · …{run_id[-6:]}"


# --- Chat turn handling ---

def handle_event(event, activity, status, reply_parts):
    """Update activity lines, map selection and reply text from one ADK event."""
    for call in event.get_function_calls():
        args = call.args or {}
        detail = f" “{args['label']}”" if args.get("label") else ""
        line = f"{TOOL_LABELS.get(call.name, call.name)}{detail}…"
        activity.append(line)
        status.update(label=line)
        status.write(line)
    for response in event.get_function_responses():
        result = response.response or {}
        if result.get("status") == "error":
            line = f"⚠️ {response.name}: {result.get('error')}"
            activity.append(line)
            status.write(line)
        elif response.name == "run_simulation" and result.get("status") == "done":
            set_map_runs(st.session_state.map_runs + [result["run_id"]])
            summary = result.get("summary", {})
            line = f"✓ {result.get('label') or result['run_id']}: {summary.get('burned_hectares', 0):,.0f} ha"
            activity.append(line)
            status.write(line)
        elif response.name in ("compare_runs", "show_map") and result.get("status", "done") == "done":
            set_map_runs([run["run_id"] for run in result.get("runs", [])])
        elif result.get("status") == "needs_confirmation":
            activity.append("Waiting for your confirmation (large request).")
    if event.is_final_response() and event.content and event.content.parts:
        reply_parts.extend(part.text for part in event.content.parts if part.text)


def ui_context_blocks() -> list[str]:
    """What the user picked in the UI, as instructions prepended to their message.

    Order matters: a drawn area overrides an example fire's default area, and a clicked point
    overrides its suggested ignition. Every block is combined — never replace one with another.
    """
    blocks = []
    fire = st.session_state.get("example_fire")
    aoi = st.session_state.get("drawn_aoi")
    picked = st.session_state.picked if st.session_state.get("attach_click", True) else None

    if fire and fire != "—":
        scenario = tools.list_example_fires()["fires"][fire]
        west, south, east, north = scenario["aoi_bounds"]
        lon, lat = scenario["ignition_lonlat"]
        area = "" if aoi else (f"area west={west}, south={south}, east={east}, north={north}; ")
        ignition = "" if picked else f"ignition_lon={lon}, ignition_lat={lat}; "
        blocks.append(
            f"[Example fire selected in the UI: {fire} ({scenario['incident_name']}). Use {area}"
            f"{ignition}ignition_date={scenario['ignition_date']}. Pass observed_fire=\"{fire}\" to "
            f"show_map so the real perimeters are drawn. Observed growth "
            f"{scenario['first_acres']:,} -> {scenario['final_acres']:,} acres over "
            f"{scenario['observed_days']} days; compare the simulation with that.]"
        )
    if aoi:
        width_km, height_km = bounds_km(aoi)
        blocks.append(
            f"[Area of interest drawn on the map: west={aoi[0]}, south={aoi[1]}, east={aoi[2]}, "
            f"north={aoi[3]} ({width_km:.0f} x {height_km:.0f} km). Use exactly these bounds, "
            f"overriding any other area, and do not ask to confirm them.]"
        )
    if picked:
        lat, lon = picked
        extra = "" if aoi else " If no area is given, choose one generously around it."
        blocks.append(f"[Ignition point clicked on the map: ignition_lon={lon:.5f}, "
                      f"ignition_lat={lat:.5f}. Use it as the ignition.{extra}]")
    return blocks


def chat_turn(prompt: str):
    blocks = ui_context_blocks()
    text = "\n".join(blocks + ["", prompt]) if blocks else prompt
    if st.session_state.get("attach_click", True):
        st.session_state.picked = None
    st.session_state.messages.append({"role": "user", "content": prompt})

    activity, reply_parts = [], []
    with st.chat_message("assistant"):
        with st.status("Thinking…", expanded=True) as status:
            try:
                events = stream_agent(text)
                while (event := events.get()) is not None:
                    if isinstance(event, Exception):
                        raise event
                    handle_event(event, activity, status, reply_parts)
                status.update(label="Done", state="complete", expanded=False)
            except Exception as error:
                status.update(label="Agent error", state="error", expanded=False)
                reply_parts = [
                    f"**Agent error:** `{type(error).__name__}: {str(error)[:400]}`\n\n"
                    "If this is a 403 or authentication error, check Gemini access for the project in "
                    "`agents/pyrosim_agent/.env` (see README). You can still run simulations from the "
                    "sidebar."
                ]
    st.session_state.messages.append(
        {"role": "assistant", "content": "\n\n".join(reply_parts) or "_(no reply)_", "activity": activity}
    )
    st.rerun()


# --- UI pieces ---

def sidebar():
    with st.sidebar:
        st.header("🔥 pyroSim")
        mode = st.radio("Simulation mode", ["mock", "real"], index=["mock", "real"].index(st.session_state.mode),
                        horizontal=True,
                        help="mock: synthetic spread in seconds. real: Earth Engine + pyretechnics.")
        if mode != st.session_state.mode:
            st.session_state.mode = mode
            os.environ["PYROSIM_MODE"] = mode
        if mode == "mock":
            st.caption("Mock numbers are synthetic test output, not fire behavior.")

        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        st.caption(f"Agent model: `{os.environ.get('PYROSIM_AGENT_MODEL', 'gemini-flash-latest')}` · "
                   f"project: `{project or 'not set'}`")
        if st.button("New conversation", use_container_width=True):
            for key in ("messages", "session_id"):
                st.session_state.pop(key, None)
            st.rerun()

        with st.expander("Data cache"):
            usage = tools.cache_status()
            st.caption(f"{usage['static']} area(s) + {usage['weather']} weather set(s) · "
                       f"{usage['megabytes']} MB")
            st.text_input("Prepare: date", "2024-07-03", key="prepare_date")
            st.number_input("Prepare: days", 1, 15, 5, key="prepare_days")
            if st.button("Clear cache", use_container_width=True):
                shutil.rmtree(tools.CACHE_DIR, ignore_errors=True)
                st.rerun()

        with st.expander("Run without the agent"):
            areas = tools.list_example_areas()["areas"]
            with st.form("manual_run"):
                area_key = st.selectbox("Example area", list(areas), format_func=lambda k: areas[k]["description"])
                picked = st.session_state.picked
                use_click = st.checkbox("Use map click as ignition", value=bool(picked), disabled=not picked)
                date = st.text_input("Ignition date", "2024-08-15")
                days = st.number_input("Projection days", min_value=1, max_value=15, value=2)
                weather = st.selectbox("Weather", ["gridmet", "weathernext"])
                fuels = st.selectbox("Fuels", ["landfire", "nlcd"],
                                     help="landfire: LANDFIRE 2023 fuels + canopy (CONUS). nlcd: old crosswalk.")
                crown = st.checkbox("Crown fire", value=True)
                label = st.text_input("Label", "manual run")
                confirm = st.checkbox("Confirm large request")
                if st.form_submit_button("Run", use_container_width=True):
                    area = areas[area_key]
                    lon, lat = area["ignition_lonlat"]
                    if st.session_state.get("drawn_aoi"):
                        area = {**area, "bounds": st.session_state.drawn_aoi}
                    if use_click and picked:
                        lat, lon = picked
                    with st.spinner("Running pyroSim…"):
                        result = tools.run_simulation(*area["bounds"], lon, lat, date, int(days), weather,
                                                      fuels, crown, label, confirm)
                    if result["status"] == "done":
                        set_map_runs(st.session_state.map_runs + [result["run_id"]])
                        st.rerun()
                    else:
                        st.error(result.get("error") or result.get("reason"))


@st.cache_data(show_spinner="Building the full map…", max_entries=8)
def full_map_html(run_ids: tuple, fire: str, max_hours: int) -> str:
    """Self-contained map HTML (embedded imagery, daily fronts, severity, summary panel).

    Cached on the run ids, since it refetches basemap imagery each time it is built.
    """
    records = [tools.load_run_record(r) for r in run_ids]
    observed = maps.observed_features(fire) if fire else None
    return maps.build_folium_map(records, max_hours, observed=observed,
                                 standalone=True).get_root().render()


def base_map(bounds=None, observed=None, show_bounds=False):
    """Imagery map framed on an area.

    show_bounds stays False for an example fire: drawing its extent would look like an area of
    interest is already set, when the user still has to draw one (or let the agent choose).
    """
    if bounds is None:
        bounds = tools.list_example_areas()["areas"]["sierra_example"]["bounds"]
    west, south, east, north = bounds
    fmap = folium.Map(location=[(south + north) / 2, (west + east) / 2], tiles=None, control_scale=True)
    folium.TileLayer(maps.ESRI_IMAGERY, attr=maps.ESRI_ATTRIBUTION, name="Aerial imagery").add_to(fmap)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(fmap)
    if show_bounds:
        folium.Rectangle([[south, west], [north, east]], color="#3388ff", fill=False, weight=2,
                         tooltip="Area of interest").add_to(fmap)
    maps.add_observed_layer(fmap, observed)
    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.fit_bounds([[south, west], [north, east]])
    return fmap


def example_fire_panel():
    """Pick a real 2024 fire: frames the map on it, suggests its ignition point, offers the overlay."""
    fires = tools.list_example_fires()["fires"]
    names = ["—"] + list(fires)
    chosen = st.selectbox(
        "Example fire (real 2024 growth data)", names, key="example_fire",
        format_func=lambda n: "—" if n == "—" else
        f"{fires[n]['incident_name'].title()} · {fires[n]['ignition_date']} · "
        f"{fires[n]['first_acres']:,} → {fires[n]['final_acres']:,} ac",
    )
    if chosen == "—":
        return None, None

    fire = fires[chosen]
    if st.session_state.get("framed_fire") != chosen:
        st.session_state.framed_fire = chosen
        st.session_state.picked = tuple(reversed(fire["ignition_lonlat"]))  # (lat, lon)
        st.rerun()

    cols = st.columns([3, 2])
    cols[0].caption(
        f"Observed {fire['first_acres']:,} → {fire['final_acres']:,} ac over {fire['observed_days']} days "
        f"({fire['first_observation'][:10]} → {fire['last_observation'][:10]}). "
        f"Suggested ignition {fire['ignition_lonlat'][1]:.4f}, {fire['ignition_lonlat'][0]:.4f}."
    )
    overlay = cols[1].checkbox("Overlay observed perimeters", value=True, key="overlay_observed")
    if fire["first_acres"] > 1000:
        st.caption(f"⚠️ The first mapped perimeter is already {fire['first_acres']:,} ac, so a run from a "
                   "single ignition point is not directly comparable.")
    st.caption("Now ask in the chat, e.g. *“Run this fire for 10 days with gridmet.”*")
    return chosen, (maps.observed_features(chosen) if overlay else None)


def map_panel():
    fire, observed_features = example_fire_panel()
    runs = finished_runs()
    runs_by_id = {r["run_id"]: r for r in runs}
    if st.session_state.pop("pending_map_select", False) or "map_select" not in st.session_state:
        st.session_state.map_select = st.session_state.map_runs
    st.session_state.map_select = [r for r in st.session_state.map_select if r in runs_by_id]
    selected = st.multiselect(
        "Runs on the map", options=list(runs_by_id), key="map_select", max_selections=MAX_MAP_RUNS,
        format_func=lambda r: run_option_label(r, runs_by_id), placeholder="Ask the agent to run a simulation…",
    )
    records = [tools.load_run_record(r) for r in selected]

    if records:
        max_hours = max(r["scenario"]["projection_days"] for r in records) * 24
        controls = st.columns([4, 1])
        until = controls[0].slider("Hours after ignition", 0, max_hours, max_hours, step=1)
        show_all = controls[1].checkbox("All layers", value=False, help="Show every run at once")
        fmap = maps.build_folium_map(records, max_hours, until_hours=until, show_all=show_all,
                                     observed=observed_features)
    else:
        bounds = tools.list_example_fires()["fires"][fire]["aoi_bounds"] if fire else None
        fmap = base_map(bounds, observed_features)
    if st.session_state.picked:
        lat, lon = st.session_state.picked
        folium.Marker([lat, lon], tooltip="Picked ignition", icon=folium.Icon(color="orange", icon="crosshairs",
                                                                           prefix="fa")).add_to(fmap)

    drawn = st.session_state.get("drawn_aoi")
    if drawn:
        folium.Rectangle([[drawn[1], drawn[0]], [drawn[3], drawn[2]]], color="#ffb300", weight=3,
                         fill=False, tooltip="Drawn area of interest").add_to(fmap)
    Draw(export=False, position="topleft",
         draw_options={"rectangle": {"shapeOptions": {"color": "#ffb300"}}, "polygon": False,
                       "polyline": False, "circle": False, "marker": False, "circlemarker": False},
         edit_options={"edit": False}).add_to(fmap)

    live_tab, full_tab = st.tabs(["Live map", "Full map (all layers)"])
    with full_tab:
        if records:
            fire_key = fire if (fire and st.session_state.get("overlay_observed", True)) else ""
            html = full_map_html(tuple(selected), fire_key, max_hours)
            st.iframe(html, height=620)  # our own HTML, built from run outputs
            st.download_button("Download this map (HTML)", html, file_name=f"{selected[0]}_map.html",
                               mime="text/html", use_container_width=True,
                               help="Self-contained: imagery is embedded, so it opens offline.")
            st.caption("Layers: arrival time, burned by each day, flame-length bands, observed "
                       "perimeters by mapping. Same file the agent writes to runs/<run_id>/map.html.")
        else:
            st.info("Run a simulation to see the full map with day-by-day and severity layers.")

    with live_tab:
        clicked = st_folium(fmap, height=560, use_container_width=True,
                            returned_objects=["last_clicked", "last_active_drawing"], key="map")
    drawing = drawing_bounds((clicked or {}).get("last_active_drawing"))
    if drawing and drawing != st.session_state.get("drawn_aoi"):
        st.session_state.drawn_aoi = drawing
        st.rerun()

    if st.session_state.get("drawn_aoi"):
        aoi = st.session_state.drawn_aoi
        width_km, height_km = bounds_km(aoi)
        cols = st.columns([3, 1, 1, 1, 1])
        cols[0].caption(f"▭ Drawn area: {width_km:.0f} × {height_km:.0f} km "
                        f"({aoi[0]:.3f}, {aoi[1]:.3f}) → ({aoi[2]:.3f}, {aoi[3]:.3f}) — the agent uses this")
        if cols[4].button("Prepare", use_container_width=True,
                          help="Download this area's layers once so later runs take seconds"):
            with st.spinner("Downloading layers for this area…"):
                result = tools.prepare_area(*aoi, st.session_state.get("prepare_date", "2024-07-03"),
                                            int(st.session_state.get("prepare_days", 5)))
            if result["status"] == "done":
                st.success(f"Area prepared in {result.get('seconds', 0)} s — runs here are now fast."
                           if "seconds" in result else result.get("note", "Nothing to prepare."))
            else:
                st.error(result["error"])
        if cols[1].button("Grow 1.5×", use_container_width=True):
            st.session_state.drawn_aoi = grow_bounds(aoi, 1.5)
            st.rerun()
        if cols[2].button("Grow 2×", use_container_width=True):
            st.session_state.drawn_aoi = grow_bounds(aoi, 2.0)
            st.rerun()
        if cols[3].button("Clear area", use_container_width=True):
            st.session_state.drawn_aoi = None
            st.rerun()

    point = (clicked or {}).get("last_clicked")
    if point and st.session_state.get("last_click_seen") != point:
        st.session_state.last_click_seen = point
        st.session_state.picked = (point["lat"], point["lng"])
        st.rerun()

    if st.session_state.picked:
        lat, lon = st.session_state.picked
        cols = st.columns([3, 2, 1])
        cols[0].caption(f"📍 Picked ignition: {lat:.4f}, {lon:.4f}")
        cols[1].checkbox("Attach to next message", value=True, key="attach_click")
        if cols[2].button("Clear"):
            st.session_state.picked = None
            st.rerun()

    for record in records:
        summary = record["summary"]
        with st.container(border=True):
            title = record.get("label") or record["run_id"]
            st.markdown(f"**{title}**" + ("  ·  :orange[MOCK — synthetic]" if record["mode"] == "mock" else ""))
            growth_24 = next((g for g in record["growth"] if g["hours_after_ignition"] == 24), None)
            crown = summary.get("passive_crown_cells", 0) + summary.get("active_crown_cells", 0)
            metrics = st.columns(5)
            metrics[0].metric("Burned", f"{summary['burned_hectares']:,.0f} ha", f"{summary['burned_acres']:,.0f} ac",
                              delta_color="off")
            metrics[1].metric("By 24 h", f"{growth_24['burned_hectares']:,.0f} ha" if growth_24 else "—")
            metrics[2].metric("Max flame length", f"{summary['max_flame_length_m']:.1f} m")
            metrics[3].metric("Crown fire", f"{crown:,} cells" if crown else "none",
                              f"{summary.get('active_crown_cells', 0):,} active" if crown else None,
                              delta_color="off")
            metrics[4].metric("Cell size", f"{summary['cell_size_m']:.0f} m")
            cache = record.get("cache") or {}
            if cache.get("cache_dir"):
                hits = [group for group in ("static", "weather") if cache.get(group) == "hit"]
                st.caption(("⚡ " + " + ".join(hits) + " reused from cache · " if hits else "") +
                           f"fetch {cache.get('fetch_seconds', 0)}s · engine {cache.get('engine_seconds', 0)}s")
            if (record.get("edge") or {}).get("reached_area_edge"):
                st.warning("Fire reached the edge of the area — burned area is an underestimate. "
                           "Draw a bigger area, or use Grow 2×, and rerun.", icon="⚠️")
            severity = summary.get("severity")
            if severity:
                bands = severity["flame_length_bands"]
                st.caption("Flame length: " + " · ".join(
                    f"{b['band']} {b['fraction_of_burned']:.0%}" for b in bands if b["fraction_of_burned"]))
                st.progress(min(sum(b["fraction_of_burned"] for b in bands if b["lower_ft"] >= 8.0), 1.0),
                            text=f"{sum(b['fraction_of_burned'] for b in bands if b['lower_ft'] >= 8.0):.0%} "
                                 "of burned area at 8 ft+ (no direct attack at the head)")
            scenario = record["scenario"]
            st.caption(f"{scenario['ignition_date']} · {scenario['projection_days']} d · "
                       f"weather {record.get('weather_source_used')} · "
                       f"fuels {record.get('fuel_source_used', '—')} · {record['run_id']}")


def chat_panel():
    history = st.container(height=640)
    with history:
        if not st.session_state.messages:
            st.markdown(
                "Ask me to run fire-spread simulations, for example:\n\n"
                "- *Run the Sierra example from 2024-08-15 for 2 days.*\n"
                "- *Compare that with a 4-day horizon and a start two weeks later.*\n"
                "- *Click the map, then: start a fire here on 2024-08-15 for 3 days.*"
            )
        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                if message.get("activity"):
                    with st.expander(f"{len(message['activity'])} tool steps", expanded=False):
                        st.markdown("\n".join(f"- {line}" for line in message["activity"]))
                st.markdown(message["content"])
    prompt = st.chat_input("Ask about a fire scenario…")
    if prompt:
        with history:
            with st.chat_message("user"):
                st.markdown(prompt)
            chat_turn(prompt)


LAYOUTS = {"Split": [3, 2], "Map focus": [1, 0], "Chat focus": [2, 3]}


def main():
    init_state()
    sidebar()
    header = st.columns([2, 3])
    layout = header[0].segmented_control("Layout", list(LAYOUTS), default="Split", key="layout",
                                         help="Map focus hides the chat; the sidebar collapses with «")
    header[1].caption("Sidebar « top-left: mode, runs without the agent. Chat keeps its history "
                      "when you switch layout.")
    ratios = LAYOUTS.get(layout or "Split")

    if ratios[1] == 0:
        map_panel()
        return
    map_column, chat_column = st.columns(ratios, gap="medium")
    with map_column:
        map_panel()
    with chat_column:
        chat_panel()


main()
