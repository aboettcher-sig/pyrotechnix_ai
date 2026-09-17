"""Weather backends: GRIDMET (default) and WeatherNext 3, behind one interface.

Each backend returns a `WeatherStack` whose cubes are already in pyretechnics units and
whose temporal resolution (daily for GRIDMET, hourly for WeatherNext) is reported explicitly.
"""

from dataclasses import dataclass

from . import gee, physics


class WeatherNextUnavailable(Exception):
    """Raised when no WeatherNext init run covers the requested window."""


@dataclass
class WeatherStack:
    cubes: dict            # name -> (bands, rows, cols) in pyretechnics units
    band_duration_min: float
    start_minutes: float   # simulation start offset within the cube
    source: str


def _dead_weather_cubes(wind_speed, upwind_dir, temp_c, dead_1hr, dead_10hr, dead_100hr):
    return {
        "wind_speed_10m": wind_speed,
        "upwind_direction": upwind_dir,
        "temperature": temp_c,
        "fuel_moisture_dead_1hr": dead_1hr,
        "fuel_moisture_dead_10hr": dead_10hr,
        "fuel_moisture_dead_100hr": dead_100hr,
    }


def _gridmet_stack(config, region, scale):
    stack = gee.fetch_gridmet_stack(config.ignition_date, config.projection_days + 1, region, scale)
    wind_speed, upwind_dir, temp_c, rh, fm100 = physics.gridmet_to_weather(stack)
    dead = physics.dead_fuel_moisture(temp_c, rh, fm100)
    cubes = _dead_weather_cubes(wind_speed, upwind_dir, temp_c, *dead)
    return WeatherStack(cubes, 1440.0, config.start_hour * 60.0, "gridmet")


def _weathernext_stack(config, region, scale):
    horizon_hours = config.start_hour + config.projection_days * 24 + 1
    init_time = config.weathernext_init_time or gee.select_weathernext_init(
        config.ignition_date, config.start_hour, horizon_hours, region
    )
    if init_time is None:
        raise WeatherNextUnavailable(
            f"No WeatherNext run covers {config.ignition_date} + {config.projection_days}d."
        )
    raw = gee.fetch_weathernext_stack(
        init_time, config.ignition_date, config.start_hour, horizon_hours,
        config.weathernext_stat, config.weathernext_step_hours, region, scale,
    )
    temp_c = raw["t2m"] - 273.15
    dewpoint_c = raw["dew"] - 273.15
    rh = physics.relative_humidity_from_dewpoint(temp_c, dewpoint_c)
    dead = physics.dead_fuel_moisture_from_emc(temp_c, rh)
    cubes = _dead_weather_cubes(
        physics.wind_uv_to_speed_kmh(raw["u"], raw["v"]),
        physics.wind_uv_to_upwind_direction(raw["u"], raw["v"]),
        temp_c, *dead,
    )
    return WeatherStack(cubes, config.weathernext_step_hours * 60.0, 0.0, "weathernext")


def get_weather(config, region, scale):
    """Return a WeatherStack from the configured source, falling back to GRIDMET if allowed."""
    if config.weather_source == "weathernext":
        try:
            return _weathernext_stack(config, region, scale)
        except WeatherNextUnavailable:
            if not config.allow_gridmet_fallback:
                raise
            print("WeatherNext has no coverage for this date; falling back to GRIDMET.")
    return _gridmet_stack(config, region, scale)
