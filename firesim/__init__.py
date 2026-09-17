"""firesim: US fire-spread simulation with pyretechnics + Earth Engine.

Typical use (from the notebook)::

    from firesim import SimulationConfig, run, viz

    config = SimulationConfig(aoi_bounds=..., ignition_lonlat=..., ignition_date=..., projection_days=...)
    results = run(config)
    viz.build_map(config, results)
    viz.plot_charts(config, results)
"""

from .cache import DataStore
from .config import SimulationConfig
from .gee import initialize_ee
from .model import fetch_layers, run_simulation
from . import observed, raster, severity, viz

__all__ = ["SimulationConfig", "run", "prepare_data", "viz", "raster", "observed", "severity",
           "DataStore", "initialize_ee", "run_simulation"]


def _store_for(config: SimulationConfig):
    return DataStore(config.cache_dir) if config.cache_dir else None


def run(config: SimulationConfig):
    """Run one simulation end to end, reusing cached layers when `cache_dir` is set.

    Earth Engine is initialized lazily inside the fetch path, so a fully cached area and date
    runs with no auth and no network call.
    """
    return run_simulation(config, _store_for(config))


def prepare_data(config: SimulationConfig) -> dict:
    """Fetch and cache an area's layers without running the engine. Returns the cache info."""
    if not config.cache_dir:
        raise ValueError("prepare_data requires config.cache_dir to be set.")
    _, _, info = fetch_layers(config, _store_for(config))
    return info
