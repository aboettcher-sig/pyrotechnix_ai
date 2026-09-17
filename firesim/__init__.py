"""firesim: US fire-spread simulation with pyretechnics + Earth Engine.

Typical use (from the notebook)::

    from firesim import SimulationConfig, run, viz

    config = SimulationConfig(aoi_bounds=..., ignition_lonlat=..., ignition_date=..., projection_days=...)
    results = run(config)
    viz.build_map(config, results)
    viz.plot_charts(config, results)
"""

from .config import SimulationConfig
from .gee import initialize_ee
from .model import run_simulation
from . import raster, viz

__all__ = ["SimulationConfig", "run", "viz", "raster", "initialize_ee", "run_simulation"]


def run(config: SimulationConfig):
    """Initialize Earth Engine and run one simulation end to end."""
    initialize_ee(config.ee_project)
    return run_simulation(config)
