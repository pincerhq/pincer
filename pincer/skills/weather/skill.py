"""Weather skill: OpenWeatherMap current weather and forecast."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


def _geocode(city: str, api_key: str) -> dict | None:
    """Get lat/lon for city. Returns first result or None."""
    q = urllib.parse.quote(city)
    url = f"https://api.openweathermap.org/geo/1.0/direct?q={q}&limit=1&appid={api_key}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data and isinstance(data, list) and len(data) > 0:
                return {"lat": data[0]["lat"], "lon": data[0]["lon"], "name": data[0].get("name", city)}
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError):
        pass
    return None


def get_weather(city: str, units: str = "metric") -> dict:
    """Get current weather for a city."""
    api_key = os.environ.get("OPENWEATHER_API_KEY") or os.environ.get("PINCER_OPENWEATHERMAP_API_KEY")
    if not api_key:
        return {"error": "Missing API key: set OPENWEATHER_API_KEY or PINCER_OPENWEATHERMAP_API_KEY"}

    geo = _geocode(city, api_key)
    if not geo:
        return {"error": f"City not found: {city}"}

    lat, lon = geo["lat"], geo["lon"]
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&units={units}&appid={api_key}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            w = data.get("weather", [{}])[0]
            return {
                "city": geo["name"],
                "temp": data.get("main", {}).get("temp"),
                "description": w.get("description", ""),
                "humidity": data.get("main", {}).get("humidity"),
                "wind_speed": data.get("wind", {}).get("speed"),
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"API error: {e.code} {body[:200]}"}
    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
        return {"error": f"Network or parse error: {e}"}


def get_forecast(city: str, units: str = "metric", days: int = 3) -> dict:
    """Get multi-day forecast. Groups by date, returns most common description per day."""
    api_key = os.environ.get("OPENWEATHER_API_KEY") or os.environ.get("PINCER_OPENWEATHERMAP_API_KEY")
    if not api_key:
        return {"error": "Missing API key: set OPENWEATHER_API_KEY or PINCER_OPENWEATHERMAP_API_KEY"}

    geo = _geocode(city, api_key)
    if not geo:
        return {"error": f"City not found: {city}"}

    lat, lon = geo["lat"], geo["lon"]
    url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&units={units}&appid={api_key}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            items = data.get("list", [])

            by_date: dict[str, list] = {}
            for item in items:
                dt = item.get("dt_txt", "")
                date_part = dt.split()[0] if dt else ""
                if not date_part:
                    continue
                if date_part not in by_date:
                    by_date[date_part] = []
                by_date[date_part].append(item)

            forecast_list = []
            for date_part in sorted(by_date.keys())[:days]:
                day_items = by_date[date_part]
                temps = [
                    item.get("main", {}).get("temp")
                    for item in day_items
                    if item.get("main", {}).get("temp") is not None
                ]
                descs = [
                    item.get("weather", [{}])[0].get("description", "")
                    for item in day_items
                    if item.get("weather")
                ]
                most_common = max(set(descs), key=descs.count) if descs else ""
                forecast_list.append({
                    "date": date_part,
                    "temp_min": min(temps) if temps else None,
                    "temp_max": max(temps) if temps else None,
                    "description": most_common,
                })

            return {
                "city": geo["name"],
                "forecast": forecast_list,
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"API error: {e.code} {body[:200]}"}
    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
        return {"error": f"Network or parse error: {e}"}
