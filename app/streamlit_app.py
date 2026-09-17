"""pyroSim map + chat: a Streamlit front end for the pyroSim agent.

Map on the left, chat on the right, one shared state: runs the agent makes appear on the map,
and a click on the map is attached to your next message as the ignition point.

Run from the repo root:
    streamlit run app/streamlit_app.py
"""

import asyncio
import os
import queue
import sys
import threading
from pathlib import Path

import folium
import streamlit as st
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
}

st.set_page_config(page_title="pyroSim", page_icon="🔥", layout="wide")


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


def chat_turn(prompt: str):
    text = prompt
    if st.session_state.picked and st.session_state.get("attach_click", True):
        lat, lon = st.session_state.picked
        text = (f"{prompt}\n\n[Map click: ignition point lon={lon:.5f}, lat={lat:.5f}. Use it as the "
                "ignition if the question needs one; propose a bounding box around it if none is given.]")
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

        with st.expander("Run without the agent"):
            areas = tools.list_example_areas()["areas"]
            with st.form("manual_run"):
                area_key = st.selectbox("Example area", list(areas), format_func=lambda k: areas[k]["description"])
                picked = st.session_state.picked
                use_click = st.checkbox("Use map click as ignition", value=bool(picked), disabled=not picked)
                date = st.text_input("Ignition date", "2024-08-15")
                days = st.number_input("Projection days", min_value=1, max_value=15, value=2)
                weather = st.selectbox("Weather", ["gridmet", "weathernext"])
                label = st.text_input("Label", "manual run")
                confirm = st.checkbox("Confirm large request")
                if st.form_submit_button("Run", use_container_width=True):
                    area = areas[area_key]
                    lon, lat = area["ignition_lonlat"]
                    if use_click and picked:
                        lat, lon = picked
                    with st.spinner("Running pyroSim…"):
                        result = tools.run_simulation(*area["bounds"], lon, lat, date, int(days), weather,
                                                      label, confirm)
                    if result["status"] == "done":
                        set_map_runs(st.session_state.map_runs + [result["run_id"]])
                        st.rerun()
                    else:
                        st.error(result.get("error") or result.get("reason"))


def base_map():
    area = tools.list_example_areas()["areas"]["sierra_example"]
    west, south, east, north = area["bounds"]
    fmap = folium.Map(location=[(south + north) / 2, (west + east) / 2], zoom_start=11, tiles=None,
                      control_scale=True)
    folium.TileLayer(maps.ESRI_IMAGERY, attr=maps.ESRI_ATTRIBUTION, name="Aerial imagery").add_to(fmap)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)
    return fmap


def map_panel():
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
        fmap = maps.build_folium_map(records, max_hours, until_hours=until, show_all=show_all)
    else:
        fmap = base_map()
    if st.session_state.picked:
        lat, lon = st.session_state.picked
        folium.Marker([lat, lon], tooltip="Picked ignition", icon=folium.Icon(color="orange", icon="crosshairs",
                                                                           prefix="fa")).add_to(fmap)

    clicked = st_folium(fmap, height=560, use_container_width=True, returned_objects=["last_clicked"],
                        key="map")
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
            metrics = st.columns(4)
            metrics[0].metric("Burned", f"{summary['burned_hectares']:,.0f} ha", f"{summary['burned_acres']:,.0f} ac",
                              delta_color="off")
            metrics[1].metric("By 24 h", f"{growth_24['burned_hectares']:,.0f} ha" if growth_24 else "—")
            metrics[2].metric("Max flame length", f"{summary['max_flame_length_m']:.1f} m")
            metrics[3].metric("Cell size", f"{summary['cell_size_m']:.0f} m")
            scenario = record["scenario"]
            st.caption(f"{scenario['ignition_date']} · {scenario['projection_days']} d · "
                       f"weather {record.get('weather_source_used')} · {record['run_id']}")


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


def main():
    init_state()
    sidebar()
    map_column, chat_column = st.columns([3, 2], gap="medium")
    with map_column:
        map_panel()
    with chat_column:
        chat_panel()


main()
