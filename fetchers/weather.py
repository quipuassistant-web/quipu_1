"""
Weather fetcher — Open-Meteo for tournament venues.

Open-Meteo (https://open-meteo.com) — free, no API key, global coverage.
Two endpoints:
  - archive-api  for historical (events that have already happened)
  - api          for forecast (events <=16 days out)

We pick the right endpoint based on `start_date` vs today and return a
small per-day summary dict. Venue → (lat, lon) comes from VENUE_COORDS
first (curated, year-stable), then falls back to
normalize.course_metadata.venue_latlon if available.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

ARCHIVE_BASE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_BASE = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "quipu-golf-agent/0.1 (personal fantasy use)"

# Forecast endpoint reliably has ~16 days of data; switch to archive
# only once the event is firmly in the past.
FORECAST_HORIZON_DAYS = 16


# ---------------------------------------------------------------------------
# Venue → (lat, lon). Curated; prefer this over course_metadata.json for
# rotating events (PGA, U.S. Open, The Open) whose metadata is locked to
# a past year's venue.
# ---------------------------------------------------------------------------

VENUE_COORDS: dict[str, tuple[float, float]] = {
    # PGA Tour regulars (mostly stable)
    "augusta national": (33.5031, -82.0214),       # Masters
    "augusta": (33.5031, -82.0214),
    "quail hollow": (35.1147, -80.8526),           # Charlotte, NC
    "quail hollow club": (35.1147, -80.8526),
    "pebble beach": (36.5688, -121.9500),
    "pebble beach golf links": (36.5688, -121.9500),
    "tpc sawgrass": (30.1969, -81.3947),           # The Players
    "torrey pines": (32.9000, -117.2524),
    "muirfield village": (40.1397, -82.9118),
    "tpc river highlands": (41.5789, -72.6700),
    "tpc craig ranch": (33.1733, -96.6428),        # CJ Cup Byron Nelson
    "colonial country club": (32.7156, -97.3669),  # Charles Schwab
    "tpc southwind": (35.0631, -89.8033),          # FedEx St. Jude
    "east lake": (33.7390, -84.3247),              # Tour Championship
    "olympia fields": (41.5103, -87.6794),
    "waialae country club": (21.2683, -157.7869),  # Sony Open
    "pga west": (33.6961, -116.2811),              # American Express
    "pga national": (26.8337, -80.0888),           # Cognizant
    "bay hill": (28.4791, -81.5093),               # Arnold Palmer
    "innisbrook": (28.1180, -82.6917),             # Valspar
    "memorial park": (29.7700, -95.4244),          # Houston Open
    "tpc san antonio": (29.5961, -98.4506),        # Valero
    "harbour town": (32.1438, -80.8181),           # RBC Heritage
    "trinity forest": (32.7300, -96.7211),

    # 2026 rotating-major venues (override the gmalbert metadata's year-locked entries)
    "aronimink": (40.0078, -75.4078),              # 2026 PGA Championship
    "aronimink golf club": (40.0078, -75.4078),
    "oakmont": (40.5356, -79.8678),                # 2026 U.S. Open
    "oakmont country club": (40.5356, -79.8678),
    "royal birkdale": (53.6203, -3.0258),          # 2026 The Open
    "royal birkdale golf club": (53.6203, -3.0258),
}


def _norm_venue(v: str) -> str:
    return " ".join((v or "").lower().split())


def venue_coords(venue: Optional[str], event_name: Optional[str] = None
                 ) -> Optional[tuple[float, float]]:
    """Resolve a venue (or event-name fallback) to (lat, lon)."""
    if venue:
        key = _norm_venue(venue)
        if key in VENUE_COORDS:
            return VENUE_COORDS[key]
        for k, coords in VENUE_COORDS.items():
            if k in key or key in k:
                return coords
    if event_name:
        try:
            from normalize.course_metadata import venue_latlon
            return venue_latlon(event_name)
        except Exception:
            pass
    return None


def get_venue_coordinates(venue_name: str) -> Optional[tuple[float, float]]:
    """Back-compat alias of venue_coords."""
    return venue_coords(venue_name)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: float = 15.0) -> dict:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_tournament_weather(
    *,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    venue: Optional[str] = None,
    event_name: Optional[str] = None,
    start_date: str,    # YYYY-MM-DD
    end_date: Optional[str] = None,
) -> Optional[dict]:
    """Per-day weather summary for an event window.

    Returns None if we can't resolve a lat/lon or the API fails.
    On success: {
      'lat', 'lon',
      'days': [{'date', 'temp_c_max', 'temp_c_min', 'wind_max_kmh', 'precip_mm'}],
      'summary': {'avg_wind_max_kmh', 'max_wind_kmh', 'max_precip_mm'},
      'source': 'archive' | 'forecast',
    }
    """
    if lat is None or lon is None:
        coords = venue_coords(venue, event_name)
        if not coords:
            logger.info("no coordinates for venue=%r event=%r", venue, event_name)
            return None
        lat, lon = coords

    try:
        sd = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
    except ValueError:
        logger.warning("bad start_date %r", start_date)
        return None
    ed = (datetime.strptime(end_date[:10], "%Y-%m-%d").date()
          if end_date else sd + timedelta(days=3))

    today = date.today()
    source = "archive" if ed < today else "forecast"
    base = ARCHIVE_BASE if source == "archive" else FORECAST_BASE

    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "daily": "temperature_2m_max,temperature_2m_min,wind_speed_10m_max,precipitation_sum",
        "timezone": "auto",
    }
    if source == "archive":
        params["start_date"] = sd.isoformat()
        params["end_date"] = ed.isoformat()
    else:
        params["forecast_days"] = min(max((ed - today).days + 1, 1),
                                       FORECAST_HORIZON_DAYS)

    url = f"{base}?{urlencode(params)}"
    try:
        data = _http_get(url)
    except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as e:
        logger.warning("open-meteo fetch failed for (%s,%s): %s", lat, lon, e)
        return None

    daily = data.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        return None

    days = []
    for i, t in enumerate(times):
        try:
            d = datetime.strptime(t, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < sd or d > ed:
            continue
        days.append({
            "date": t,
            "temp_c_max": (daily.get("temperature_2m_max") or [None])[i],
            "temp_c_min": (daily.get("temperature_2m_min") or [None])[i],
            "wind_max_kmh": (daily.get("wind_speed_10m_max") or [None])[i],
            "precip_mm": (daily.get("precipitation_sum") or [None])[i],
        })
    if not days:
        return None

    winds = [d["wind_max_kmh"] for d in days if d["wind_max_kmh"] is not None]
    precs = [d["precip_mm"] for d in days if d["precip_mm"] is not None]
    return {
        "lat": lat,
        "lon": lon,
        "days": days,
        "summary": {
            "avg_wind_max_kmh": (sum(winds) / len(winds)) if winds else None,
            "max_wind_kmh": max(winds) if winds else None,
            "max_precip_mm": max(precs) if precs else None,
        },
        "source": source,
    }
