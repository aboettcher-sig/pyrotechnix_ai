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
from .model import fetch_layers, run_simulation
from .cache import DataStore
from . import raster, viz

__all__ = [
    "SimulationConfig",
    "run",
    "prepare_data",
    "viz",
    "raster",
    "DataStore",
    "initialize_ee",
    "run_simulation",
]


def _store_for(config: SimulationConfig):
    return DataStore(config.cache_dir) if config.cache_dir else None


def run(config: SimulationConfig):
    """Initialize Earth Engine and run one simulation end to end (using the cache if set)."""
    initialize_ee(config.ee_project)
    return run_simulation(config, _store_for(config))


def prepare_data(config: SimulationConfig):
    """Fetch and cache the static + weather layers for an AOI/date without running the engine."""
    if not config.cache_dir:
        raise ValueError("prepare_data requires config.cache_dir to be set.")
    initialize_ee(config.ee_project)
    fetch_layers(config, _store_for(config))
    return config.cache_dir
