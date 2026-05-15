"""
Weather fetcher — National Weather Service forecasts for tournament venues.

Uses the NWS API (api.weather.gov) — free, no API key required.

Pre-seeded venue coordinates for known PGA venues.
For each venue/date combo: resolve lat+lon → gridpoint → forecast.

Cache TTL: 6h (forecasts evolve but not minute-by-minute).
"""

import logging
import random
import time
import requests

logger = logging.getLogger(__name__)

NWS_BASE = "https://api.weather.gov"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Pre-seeded venue coordinates (lat, lon)
# ---------------------------------------------------------------------------

VENUE_COORDS = {
    "quail_hollow": (35.1147, -80.8526),         # Charlotte, NC
    "quail hollow": (35.1147, -80.8526),
    "oakmont": (40.5356, -79.8678),               # Oakmont, PA (US Open)
    "oakmont country club": (40.5356, -79.8678),
    "royal_portrush": (55.2086, -6.6563),        # Portrush, NI (The Open)
    "royal portrush": (55.2086, -6.6563),
    "muirfield_village": (40.1397, -82.9118),     # Dublin, OH (Memorial)
    "muirfield": (40.1397, -82.9118),
    "tpc_river_highlands": (41.5789, -72.6700),  # Cromwell, CT (Travelers)
    "tpc craig woods": (39.9500, -75.1700),       # Philadelphia, PA (Truist)
    "tpc_craig_woods": (39.9500, -75.1700),
    "augusta": (33.5031, -82.3922),              # Augusta, GA (Masters)
    "augusta national": (33.5031, -82.3922),
    "pebble_beach": (36.5714, -121.9497),        # Pebble Beach, CA (AT&T)
    "pebble_creek": (36.5714, -121.9497),
    "torrey_pines": (32.9002, -117.2524),        # La Jolla, CA (Farmers)
    "torrey pines": (32.9002, -117.2524),
}

# Quick lookup by canonical event ID
_EVENT_COORDS = {
    "pga_championship_2026": (35.1147, -80.8526),
    "memorial_2026": (40.1397, -82.9118),
    "us_open_2026": (40.5356, -79.8678),
    "the_open_2026": (55.2086, -6.6563),
    "travelers_2026": (41.5789, -72.6700),
    "truist_2026": (39.9500, -75.1700),
    "masters_2026": (33.5031, -82.3922),
}


def get_venue_coordinates(venue_name: str) -> tuple[float, float]:
    """
    Return (lat, lon) for a known venue.

    Supports partial name matching.
    Returns (None, None) if venue not found.
    """
    if not venue_name:
        return None, None

    # Normalize key
    key = venue_name.lower().strip().replace(" ", "_").replace("'", "")

    # Direct lookup
    if key in VENUE_COORDS:
        return VENUE_COORDS[key]

    # Partial match
    for vk, coords in VENUE_COORDS.items():
        if vk.replace("_", " ") in key.replace("_", " ") or key in vk.replace("_", " "):
            return coords

    logger.warning(f"Unknown venue: {venue_name!r}")
    return None, None


def get_tournament_weather(venue_name: str, target_date: str) -> dict:
    """
    Fetch NWS forecast for venue around target date.

    target_date: "YYYY-MM-DD" format (e.g., "2026-05-15")

    Returns: {
        "venue": "Quail Hollow",
        "date": "2026-05-15",
        "high_f": 82,
        "low_f": 64,
        "wind_mph": 12,
        "wind_dir": "SW",
        "conditions": "partly cloudy",
        "rain_chance": 20,
        "forecast_url": "https://...",
        "gridpoint": "RAH/106,74",
    }

    NWS API flow:
      1. GET https://api.weather.gov/points/{lat},{lon} → gives gridpoint URL
      2. GET gridpoint URL/forecast → gives daily + hourly forecast
      3. Find the day matching target_date and extract data

    Cache TTL: 6h.
    """
    from cache.cache import cache_get_or_fetch, cache_get, cache_set

    cache_key = f"weather:{venue_name}:{target_date}"

    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    lat, lon = get_venue_coordinates(venue_name)
    if lat is None or lon is None:
        logger.warning(f"No coordinates for venue: {venue_name}")
        return {"venue": venue_name, "date": target_date, "error": "venue not found"}

    try:
        # Step 1: resolve lat/lon to gridpoint
        import time as _time

        points_url = f"{NWS_BASE}/points/{lat},{lon}"
        headers = {"User-Agent": USER_AGENT}
        resp = requests.get(points_url, headers=headers, timeout=15)
        resp.raise_for_status()
        _time.sleep(random.uniform(1.0, 2.0))
        points_data = resp.json()

        gridpoint_url = points_data.get("properties", {}).get("forecast", "")
        gridpoint = points_data.get("properties", {}).get("gridId", "") + "/" + str(
            points_data.get("properties", {}).get("gridX", "")
        ) + "," + str(points_data.get("properties", {}).get("gridY", ""))

        if not gridpoint_url:
            raise ValueError("No forecast URL in NWS points response")

        # Step 2: get forecast
        forecast_resp = requests.get(gridpoint_url, headers=headers, timeout=15)
        forecast_resp.raise_for_status()
        _time.sleep(random.uniform(1.0, 2.0))
        forecast_data = forecast_resp.json()

        # Navigate periods
        periods = forecast_data.get("properties", {}).get("periods", [])
        target_day = None
        for period in periods:
            valid_start = period.get("startTime", "")
            # valid_start looks like "2026-05-15T07:00:00Z"
            if valid_start.startswith(target_date):
                target_day = period
                break

        if target_day is None:
            # Fall back: return first period's date + note
            first = periods[0] if periods else {}
            logger.warning(
                f"NWS: no forecast for {target_date} at {venue_name}, "
                f"returning first available: {first.get('startTime')}"
            )
            target_day = first

        if not target_day:
            return {"venue": venue_name, "date": target_date, "error": "no forecast data"}

        # Parse wind direction
        wind_raw = target_day.get("windSpeed", "")
        wind_mph = 0
        wind_dir = ""
        if wind_raw:
            # "12 mph" or "12 to 18 mph" or "15 mph SW"
            m = __import__("re").search(r"(\d+)\s*mph", wind_raw)
            if m:
                wind_mph = int(m.group(1))
            # Direction words
            for word in ["N", "NE", "E", "SE", "S", "SW", "W", "NW", "North", "South", "East", "West"]:
                if word in wind_raw:
                    wind_dir = word[:2] if len(word) > 1 else word
                    break

        result = {
            "venue": venue_name,
            "date": target_date,
            "high_f": target_day.get("temperature"),
            "low_f": target_day.get("temperatureLow"),
            "wind_mph": wind_mph,
            "wind_dir": wind_dir,
            "conditions": target_day.get("shortDescription", target_day.get("detailedForecast", "")),
            "rain_chance": target_day.get("probabilityOfPrecipitation", {}).get("value", 0),
            "forecast_url": gridpoint_url,
            "gridpoint": gridpoint,
        }

        cache_set(cache_key, result, ttl=21600)  # 6h
        return result

    except Exception as e:
        logger.error(f"Weather fetch failed for {venue_name} on {target_date}: {e}")
        return {"venue": venue_name, "date": target_date, "error": str(e)}