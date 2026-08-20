"""Example Weather Tool."""

from __future__ import annotations

from typing import Any

from framework.tool.function_tool import function_tool


@function_tool(
    name_override="example-weather",
    description_override="Return demo weather data for a city.",
)
async def get_weather(city: str, date: str | None = None) -> dict[str, Any]:
    """Return deterministic demo weather data.

    Args:
        city: City name to query.
        date: Optional date string for the forecast.
    """
    normalized_city = city.strip() or "UNKNOWN"
    return {
        "city": normalized_city,
        "date": date,
        "condition": "sunny",
        "temperature_c": 26,
        "humidity_percent": 58,
        "source": "example-static-weather",
    }
