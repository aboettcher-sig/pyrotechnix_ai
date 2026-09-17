"""pyroSim agent: run and compare fire-spread simulations from natural language.

Follows the Earth Engine community ADK agent layout (agent.py, prompts.py, tools.py, .env).
From the `agents/` folder run `adk web` (browser UI) or `adk run pyrosim_agent` (terminal).
Each simulation runs the pyroSim CLI in a subprocess, which initializes Earth Engine itself.
"""

import os

from google.adk.agents import llm_agent

from . import prompts
from . import tools

_MODEL = os.environ.get("PYROSIM_AGENT_MODEL", "gemini-flash-latest")

root_agent = llm_agent.Agent(
    name="pyrosim_agent",
    model=_MODEL,
    description="Runs and compares pyroSim wildfire-spread simulations from natural language.",
    instruction=prompts.root_prompt,
    tools=[
        tools.list_example_areas,
        tools.list_example_fires,
        tools.run_simulation,
        tools.prepare_area,
        tools.cache_status,
        tools.get_run,
        tools.list_runs,
        tools.compare_runs,
        tools.severity_summary,
        tools.show_map,
    ],
)
