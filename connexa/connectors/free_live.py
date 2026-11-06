"""Free-live connectors: real provider calls that need no credentials, so the
agent lane has *real data* flowing out of the box (like the big platforms' no-auth connectors).
"""
from __future__ import annotations

import httpx
from pydantic import BaseModel

from .base import Connector, ToolSpec, ToolContext


class ForecastInput(BaseModel):
    latitude: float
    longitude: float


class CurrentWeatherInput(BaseModel):
    latitude: float
    longitude: float


class SearchInput(BaseModel):
    query: str


class SummaryInput(BaseModel):
    title: str


def _http_get(url: str, params: dict | None = None) -> dict:
    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()


class WeatherConnector(Connector):
    slug = "weather"
    name = "Weather"
    description = "Real live weather forecasts (Open-Meteo) — no credentials needed."
    auth_type = "none"
    real_lane_ready = True

    def build_tools(self) -> list[ToolSpec]:
        def get_forecast(ctx: ToolContext, inp: ForecastInput) -> dict:
            data = _http_get(
                "https://api.open-meteo.com/v1/forecast",
                {"latitude": inp.latitude, "longitude": inp.longitude,
                 "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode",
                 "forecast_days": 5},
            )
            daily = data.get("daily", {})
            periods = [
                {"day_index": i, "date": (daily.get("time") or [None] * 5)[i],
                 "temp_max_c": (daily.get("temperature_2m_max") or [None] * 5)[i],
                 "temp_min_c": (daily.get("temperature_2m_min") or [None] * 5)[i],
                 "precipitation_mm": (daily.get("precipitation_sum") or [None] * 5)[i],
                 "weathercode": (daily.get("weathercode") or [None] * 5)[i]}
                for i in range(len(daily.get("time", [])))
            ]
            return {"location": {"latitude": inp.latitude, "longitude": inp.longitude},
                    "elevation_value": data.get("elevation"), "periods": periods}

        def get_current(ctx: ToolContext, inp: CurrentWeatherInput) -> dict:
            data = _http_get("https://api.open-meteo.com/v1/forecast",
                             {"latitude": inp.latitude, "longitude": inp.longitude,
                              "current": "temperature_2m,wind_speed_10m,weathercode"})
            current = data.get("current", {})
            return {"location": {"latitude": inp.latitude, "longitude": inp.longitude},
                    "updated": current.get("time"), "temperature_c": current.get("temperature_2m"),
                    "wind_speed_kmh": current.get("wind_speed_10m"), "weathercode": current.get("weathercode")}

        return [
            ToolSpec(name="get_forecast", description="Get a 5-day weather forecast for a latitude/longitude (real live data).",
                     handler=get_forecast, input_model=ForecastInput, needs_credentials=False, credit_type="low"),
            ToolSpec(name="get_current_weather", description="Get current weather conditions for a latitude/longitude (real live data).",
                     handler=get_current, input_model=CurrentWeatherInput, needs_credentials=False, credit_type="low"),
        ]


class WikipediaConnector(Connector):
    slug = "wikipedia"
    name = "Wikipedia"
    description = "Search and summarize Wikipedia articles — real live data, no credentials."
    auth_type = "none"
    real_lane_ready = True

    def build_tools(self) -> list[ToolSpec]:
        def search(ctx: ToolContext, inp: SearchInput) -> dict:
            data = _http_get("https://en.wikipedia.org/w/api.php", {
                "action": "query", "list": "search", "srsearch": inp.query,
                "format": "json", "srlimit": 5, "origin": "*"})
            hits = [r.get("title") for r in (data.get("query", {}).get("search") or [])]
            return {"query": inp.query, "results": hits, "total": data.get("query", {}).get("searchinfo", {}).get("totalhits", len(hits))}

        def get_summary(ctx: ToolContext, inp: SummaryInput) -> dict:
            try:
                data = _http_get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{inp.title.replace(' ', '_')}")
                return {"title": data.get("title"), "extract": data.get("extract"),
                        "url": data.get("content_urls", {}).get("desktop", {}).get("page")}
            except Exception as error:  # noqa: BLE001
                return {"title": inp.title, "extract": None, "error": str(error)[:200]}

        return [
            ToolSpec(name="search", description="Search Wikipedia article titles for a query (real live data).",
                     handler=search, input_model=SearchInput, needs_credentials=False, credit_type="low"),
            ToolSpec(name="get_summary", description="Get the lead summary of a Wikipedia page by exact title (real live data).",
                     handler=get_summary, input_model=SummaryInput, needs_credentials=False, credit_type="low"),
        ]
