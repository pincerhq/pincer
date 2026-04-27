"""
openweathermap_mcp — OpenWeatherMap MCP server built with fastmcp.

Transports:
    stdio (default):  python server.py --transport stdio
    HTTP:             python server.py --transport http [--host 0.0.0.0] [--port 8000]
        MCP endpoint → http://localhost:8000/mcp

Environment:
    API_KEY   Your OpenWeatherMap API key (required).
              Get a free key at https://openweathermap.org/api
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.cors import CORSMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
API_KEY = os.environ.get("API_KEY", "")
_OWM_BASE = "https://api.openweathermap.org/data/2.5"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="openweathermap_mcp",
    instructions=(
        "OpenWeatherMap MCP server. "
        "Use get_current_weather to fetch current conditions by city name, "
        "get_weather_by_coords to fetch by latitude/longitude, "
        "and get_forecast to fetch a multi-day hourly forecast."
    ),
)

_UNITS_LABEL = {"metric": "°C", "imperial": "°F", "standard": "K"}


def _require_api_key() -> None:
    if not API_KEY:
        raise ValueError(
            "API_KEY is not set. "
            "Get a free key at https://openweathermap.org/api, "
            "then export it: export API_KEY=your_key"
        )


def _handle_error(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return f"Configuration error: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        try:
            msg = exc.response.json().get("message", "")
        except Exception:
            msg = ""
        if code == 401:
            return "Error: Invalid API key – check your API_KEY environment variable."
        if code == 404:
            return "Error: Location not found."
        if code == 429:
            return "Error: Rate limit exceeded."
        return f"Error: OpenWeatherMap returned HTTP {code}. {msg}".strip()
    if isinstance(exc, httpx.TimeoutException):
        return "Error: request timed out – please try again"
    return f"Error: {type(exc).__name__}: {exc}"


def _format_weather(data: dict[str, Any], units: str) -> dict[str, Any]:
    """Extract the key fields from a /weather response."""
    unit = _UNITS_LABEL.get(units, "")
    main = data.get("main", {})
    wind = data.get("wind", {})
    weather = data.get("weather", [{}])[0]
    sys = data.get("sys", {})
    return {
        "city": data.get("name"),
        "country": sys.get("country"),
        "condition": weather.get("main"),
        "description": weather.get("description"),
        "temperature": f"{main.get('temp')} {unit}".strip(),
        "feels_like": f"{main.get('feels_like')} {unit}".strip(),
        "temp_min": f"{main.get('temp_min')} {unit}".strip(),
        "temp_max": f"{main.get('temp_max')} {unit}".strip(),
        "humidity_percent": main.get("humidity"),
        "pressure_hpa": main.get("pressure"),
        "wind_speed": wind.get("speed"),
        "wind_deg": wind.get("deg"),
        "visibility_m": data.get("visibility"),
        "clouds_percent": data.get("clouds", {}).get("all"),
        "units": units,
    }


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class WeatherByCityInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    city: str = Field(description="City name, optionally with country code, e.g. 'London' or 'Paris,FR'")
    units: str = Field(
        default="metric",
        description="Unit system: 'metric' (°C), 'imperial' (°F), or 'standard' (K). Default: metric.",
    )

    @field_validator("city")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("city cannot be empty")
        return v

    @field_validator("units")
    @classmethod
    def _valid_units(cls, v: str) -> str:
        if v not in ("metric", "imperial", "standard"):
            raise ValueError("units must be 'metric', 'imperial', or 'standard'")
        return v


class WeatherByCoordsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float = Field(description="Latitude, e.g. 51.5074", ge=-90.0, le=90.0)
    lon: float = Field(description="Longitude, e.g. -0.1278", ge=-180.0, le=180.0)
    units: str = Field(
        default="metric",
        description="Unit system: 'metric' (°C), 'imperial' (°F), or 'standard' (K). Default: metric.",
    )

    @field_validator("units")
    @classmethod
    def _valid_units(cls, v: str) -> str:
        if v not in ("metric", "imperial", "standard"):
            raise ValueError("units must be 'metric', 'imperial', or 'standard'")
        return v


class ForecastInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    city: str = Field(description="City name, optionally with country code, e.g. 'Tokyo' or 'Berlin,DE'")
    days: int = Field(
        default=3,
        description="Number of days to forecast (1–5). Each day returns up to 8 three-hour slots. Default: 3.",
        ge=1,
        le=5,
    )
    units: str = Field(
        default="metric",
        description="Unit system: 'metric' (°C), 'imperial' (°F), or 'standard' (K). Default: metric.",
    )

    @field_validator("city")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("city cannot be empty")
        return v

    @field_validator("units")
    @classmethod
    def _valid_units(cls, v: str) -> str:
        if v not in ("metric", "imperial", "standard"):
            raise ValueError("units must be 'metric', 'imperial', or 'standard'")
        return v


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "healthy",
            "service": "openweathermap-mcp",
            "version": "0.1.0",
            "transport": "http",
        }
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
async def get_current_weather(params: WeatherByCityInput) -> str:
    """Get current weather conditions for a city.

    Returns city, country, condition, description, temperature, feels_like,
    temp_min, temp_max, humidity_percent, pressure_hpa, wind_speed, wind_deg,
    visibility_m, clouds_percent, and units.
    """
    try:
        _require_api_key()
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{_OWM_BASE}/weather",
                params={"q": params.city, "appid": API_KEY, "units": params.units},
            )
            r.raise_for_status()
            data = r.json()
        return json.dumps(_format_weather(data, params.units))
    except Exception as exc:
        return json.dumps({"error": _handle_error(exc)})


@mcp.tool
async def get_weather_by_coords(params: WeatherByCoordsInput) -> str:
    """Get current weather conditions by geographic coordinates.

    Returns the same fields as get_current_weather.
    """
    try:
        _require_api_key()
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{_OWM_BASE}/weather",
                params={"lat": params.lat, "lon": params.lon, "appid": API_KEY, "units": params.units},
            )
            r.raise_for_status()
            data = r.json()
        return json.dumps(_format_weather(data, params.units))
    except Exception as exc:
        return json.dumps({"error": _handle_error(exc)})


@mcp.tool
async def get_forecast(params: ForecastInput) -> str:
    """Get a multi-day weather forecast for a city (3-hour slots, up to 5 days).

    Returns city, country, units, and a list of forecast slots each with:
    datetime, condition, description, temperature, feels_like, humidity_percent,
    wind_speed, and clouds_percent.
    """
    try:
        _require_api_key()
        cnt = params.days * 8  # 8 three-hour slots per day
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{_OWM_BASE}/forecast",
                params={"q": params.city, "appid": API_KEY, "units": params.units, "cnt": cnt},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return json.dumps({"error": _handle_error(exc)})

    unit = _UNITS_LABEL.get(params.units, "")
    city_info = data.get("city", {})
    slots = []
    for item in data.get("list", []):
        main = item.get("main", {})
        weather = item.get("weather", [{}])[0]
        slots.append(
            {
                "datetime": item.get("dt_txt"),
                "condition": weather.get("main"),
                "description": weather.get("description"),
                "temperature": f"{main.get('temp')} {unit}".strip(),
                "feels_like": f"{main.get('feels_like')} {unit}".strip(),
                "humidity_percent": main.get("humidity"),
                "wind_speed": item.get("wind", {}).get("speed"),
                "clouds_percent": item.get("clouds", {}).get("all"),
            }
        )
    return json.dumps(
        {
            "city": city_info.get("name"),
            "country": city_info.get("country"),
            "units": params.units,
            "forecast": slots,
        }
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenWeatherMap MCP server (fastmcp)")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=os.environ.get("TRANSPORT", "stdio"),
        help="Transport: 'http' (streamable HTTP, default) or 'stdio'",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Bind host for HTTP transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("PORT", 8000),
        help="Bind port for HTTP transport (default: 8000)",
    )
    args = parser.parse_args()

    if args.transport == "http":
        print(f"Starting openweathermap_mcp · HTTP transport · {args.host}:{args.port}/mcp")
        app = mcp.http_app()
        cors_app = CORSMiddleware(app=app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        uvicorn.run(cors_app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
