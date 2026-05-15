"""
DataGolf fetcher — predictive model data, course fit scores, win probabilities.

Sources:
  - Rankings:       https://www.datagolf.com/datagolf-rankings
  - Tournament preds: https://www.datagolf.com/pre-tournament-predictions

DataGolf pages are server-rendered HTML — BeautifulSoup is sufficient.
User-Agent rotation + polite delays between requests.

NOTE: Before scraping, check robots.txt. If disallowed, fall back to ESPN+odds only.
Cache TTLs: 4h Mon-Wed, 24h Thu-Sun (rankings shift slowly).

Important: DataGolf is subscription-based for some data.
Free pages: rankings, some pre-tournament predictions.
"""

import logging
import random
import time
import re

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DG_BASE = "https://www.datagolf.com"


def _fetch(url: str, timeout: int = 15) -> BeautifulSoup:
    """Fetch and parse HTML from DataGolf. Returns BeautifulSoup or raises."""
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    time.sleep(random.uniform(2.0, 4.0))
    return BeautifulSoup(resp.text, "lxml")


def _check_robots(url: str) -> bool:
    """
    Check if scraping is allowed per robots.txt.
    Returns True if allowed, False if blocked.
    """
    try:
        robots_url = f"{DG_BASE}/robots.txt"
        resp = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=5)
        lines = resp.text.split("\n")
        for line in lines:
            if "disallow" in line.lower() and url in line:
                logger.warning(f"DataGolf robots.txt disallows {url}")
                return False
    except Exception as e:
        logger.warning(f"Could not check robots.txt: {e}")
    return True


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------

def get_datagolf_rankings() -> list[dict]:
    """
    Scrape datagolf.com/datagolf-rankings — top ~500 players.

    Returns: [{
        "player": "Scottie Scheffler",
        "rank": 1,
        "dg_id": 12345,
        "sg_total": 2.31,
        "sg_ott": 0.78,
        "sg_approach": 0.67,
        "sg_around_green": 0.41,
        "sg_putting": 0.44,
        "events": 24,
        "win_prob_adj": 0.042,
    }, ...]

    Cache: 24h (rankings shift slowly).
    """
    cache_key = "datagolf:rankings"

    from cache.cache import cache_get_or_fetch, cache_get

    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        # Check robots.txt first
        if not _check_robots("/datagolf-rankings"):
            logger.warning("DataGolf /datagolf-rankings blocked by robots.txt — skipping")
            return []

        soup = _fetch(f"{DG_BASE}/datagolf-rankings")

        # Data lives in a table with class "table-scroll" or similar
        # Or embedded as JSON in a <script> tag
        # Look for JSON data first
        script_data = None
        for script in soup.find_all("script"):
            text = script.string or ""
            if "datagolf_rankings" in text or "playerRankings" in text:
                script_data = text
                break

        players = []
        if script_data:
            # Extract JSON from script tag
            import json
            # Try to find JSON object in the script
            match = re.search(r'\{[^{}]*"player"[^{}]*\}', script_data)
            if match:
                try:
                    data = json.loads(match.group())
                    for entry in data.get("playerRankings", data.get("rankings", [])):
                        players.append({
                            "player": entry.get("player_name", ""),
                            "rank": entry.get("rank"),
                            "dg_id": entry.get("dg_id") or entry.get("id"),
                            "sg_total": entry.get("sg_total") or entry.get("strokes_gained_total"),
                            "sg_ott": entry.get("sg_ott") or entry.get("strokes_gained_ott"),
                            "sg_approach": entry.get("sg_approach"),
                            "sg_around_green": entry.get("sg_around_green"),
                            "sg_putting": entry.get("sg_putting"),
                            "events": entry.get("events"),
                            "win_prob_adj": entry.get("win_prob_adj"),
                        })
                except Exception:
                    pass

        if not players:
            # Fall back to HTML table parsing
            table = soup.find("table")
            if table:
                rows = table.find_all("tr")
                for row in rows[1:]:  # Skip header
                    cols = row.find_all(["td", "th"])
                    if len(cols) < 5:
                        continue
                    cells = [c.get_text(strip=True) for c in cols]
                    # Pattern: rank | player | sg_total | sg_ott | sg_approach | ...
                    try:
                        players.append({
                            "player": cells[1] if len(cells) > 1 else "",
                            "rank": int(cells[0]) if cells[0].isdigit() else None,
                            "dg_id": None,
                            "sg_total": float(cells[2]) if len(cells) > 2 and cells[2] else None,
                            "sg_ott": float(cells[3]) if len(cells) > 3 and cells[3] else None,
                            "sg_approach": float(cells[4]) if len(cells) > 4 and cells[4] else None,
                            "sg_around_green": None,
                            "sg_putting": None,
                            "events": None,
                            "win_prob_adj": None,
                        })
                    except (ValueError, IndexError):
                        continue

        from cache.cache import cache_set
        cache_set(cache_key, players, ttl=86400)  # 24h
        return players

    except Exception as e:
        logger.error(f"DataGolf rankings fetch failed: {e}")
        # Return empty list + cached data if available
        cached = cache_get(cache_key)
        return cached if cached is not None else []


# ---------------------------------------------------------------------------
# Pre-tournament predictions
# ---------------------------------------------------------------------------

def get_tournament_preds(event_slug: str = None) -> list[dict]:
    """
    Scrape datagolf.com/pre-tournament-predictions for current/upcoming event.

    event_slug: datagolf URL slug e.g. "pga-championship" or None for current.

    Returns: [{
        "player": "Scottie Scheffler",
        "dg_id": 12345,
        "win_prob": 0.123,
        "t5_prob": 0.445,
        "t10_prob": 0.622,
        "t20_prob": 0.778,
        "cut_prob": 0.891,
        "made_cut": True,
        "course_fit_score": 87,
        "sg_tee_to_green": 1.23,
        "sg_approach": 0.45,
        "sg_around_green": 0.12,
        "sg_putting": 0.33,
        "event": event_slug or "current",
    }, ...]

    Cache TTL: 4h Mon-Wed, 24h Thu-Sun.
    """
    from datetime import datetime, date
    today = date.today()
    weekday = today.weekday()  # 0=Mon ... 4=Fri, 5=Sat, 6=Sun
    if weekday < 4:  # Mon-Wed
        cache_ttl = 14400  # 4h
    else:  # Thu-Sun
        cache_ttl = 86400  # 24h

    cache_key = f"datagolf:tournament_preds:{event_slug or 'current'}"

    from cache.cache import cache_get_or_fetch, cache_get

    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        if not _check_robots("/pre-tournament-predictions"):
            logger.warning("DataGolf /pre-tournament-predictions blocked by robots.txt — skipping")
            return []

        url = f"{DG_BASE}/pre-tournament-predictions"
        if event_slug:
            url = f"{DG_BASE}/pre-tournament-predictions/{event_slug}"

        soup = _fetch(url)

        # Predictions are typically in a table or embedded JSON
        # Try JSON first
        script_data = None
        for script in soup.find_all("script"):
            text = script.string or ""
            if "tournament_predictions" in text or "preTournament" in text or "predictions" in text:
                script_data = text
                break

        players = []
        if script_data:
            import json
            # Extract JSON array of player predictions
            match = re.search(r'\[[\s\S]*?"player"[\s\S]*?\]', script_data)
            if match:
                try:
                    data = json.loads(match.group())
                    for entry in data:
                        players.append({
                            "player": entry.get("player_name", entry.get("player", "")),
                            "dg_id": entry.get("dg_id") or entry.get("id"),
                            "win_prob": entry.get("win_prob"),
                            "t5_prob": entry.get("top5_prob") or entry.get("t5_prob"),
                            "t10_prob": entry.get("top10_prob") or entry.get("t10_prob"),
                            "t20_prob": entry.get("top20_prob") or entry.get("t20_prob"),
                            "cut_prob": entry.get("cut_prob"),
                            "made_cut": entry.get("made_cut"),
                            "course_fit_score": entry.get("course_fit_score"),
                            "sg_tee_to_green": entry.get("sg_tee_to_green"),
                            "sg_approach": entry.get("sg_approach"),
                            "sg_around_green": entry.get("sg_around_green"),
                            "sg_putting": entry.get("sg_putting"),
                            "event": event_slug or "current",
                        })
                except Exception:
                    pass

        if not players:
            # Fall back to HTML table
            table = soup.find("table")
            if table:
                rows = table.find_all("tr")
                for row in rows[1:]:
                    cols = row.find_all(["td", "th"])
                    if len(cols) < 4:
                        continue
                    cells = [c.get_text(strip=True) for c in cols]
                    try:
                        players.append({
                            "player": cells[1] if len(cells) > 1 else "",
                            "dg_id": None,
                            "win_prob": float(cells[2]) if len(cells) > 2 and cells[2] else None,
                            "t5_prob": float(cells[3]) if len(cells) > 3 and cells[3] else None,
                            "t10_prob": None,
                            "t20_prob": None,
                            "cut_prob": None,
                            "made_cut": None,
                            "course_fit_score": None,
                            "sg_tee_to_green": None,
                            "sg_approach": None,
                            "sg_around_green": None,
                            "sg_putting": None,
                            "event": event_slug or "current",
                        })
                    except (ValueError, IndexError):
                        continue

        from cache.cache import cache_set
        cache_set(cache_key, players, ttl=cache_ttl)
        return players

    except Exception as e:
        logger.error(f"DataGolf tournament predictions fetch failed: {e}")
        cached = cache_get(cache_key)
        return cached if cached is not None else []