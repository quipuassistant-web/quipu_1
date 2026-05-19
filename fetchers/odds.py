"""
Odds fetcher — DraftKings primary, FanDuel fallback, and odds de-vigging.

Sources:
  - DraftKings: https://sportsbook.draftkings.com/sites/US/sports/golf/pga-tour
    (or API endpoint: /api/v4/sports/golf/leagues/pga/outrights)
  - FanDuel:    https://sportsbook.fanduel.com/api/sports/golf/leagues/pga/outrights

DraftKings uses heavy SPA rendering — data loads via API after JS executes.
Look for: window.__INITIAL_STATE__, window.__PRELOADED_STATE__, or __NEXT_DATA__.

Polite delays between requests.
"""

import logging
import random
import time
import re

import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Odds parsing helpers
# ---------------------------------------------------------------------------

def american_to_implied(odds_str: str) -> float:
    """
    Convert an odds string to implied win probability (percentage, 0–100).

    Accepts:
      '+650'  → 13.33  (American)
      '-110'  → 52.38  (American)
      'EV'    → 50.0   (American even money)
      '5.50'  → 18.18  (European decimal: 1/dec * 100)
      '0.18'  → 18.0   (already-implied probability 0–1)
    """
    if odds_str is None:
        return 0.0
    s = str(odds_str).strip()
    if not s:
        return 0.0

    if s.upper() in ("EV", "E", "-100", "+100"):
        return 50.0

    try:
        if s.startswith("+"):
            val = int(s[1:])
            implied = 100 / (val + 100) * 100
        elif s.startswith("-"):
            val = int(s[1:])
            implied = abs(val) / (abs(val) + 100) * 100
        else:
            # Plain numeric: either European decimal odds (≥1.0) or an
            # already-implied probability (<1.0). Treating it as a literal
            # percentage was the old bug — "5.5" came out as 5.5% when the
            # author meant European 5.5 odds (18.18%).
            dec = float(s)
            if dec <= 0:
                implied = 0.0
            elif dec < 1.0:
                implied = dec * 100
            else:
                implied = (1.0 / dec) * 100
    except (ValueError, ZeroDivisionError):
        implied = 0.0

    return round(implied, 4)


def devig_odds(odds_list: list[dict]) -> list[dict]:
    """
    De-vig the odds: convert American → implied prob, normalize by totalvig.

    The sum of all implied probs > 100% (that's the vig/bookmaker margin).
    De-vigged "fair" probabilities are each implied / sum_implied * 100.

    Returns same list with added "fair_win_pct" field (float, percentage).
    Keeps entries with implied=0 (no odds available).
    """
    if not odds_list:
        return odds_list

    # Collect all implied probs
    implieds = []
    for entry in odds_list:
        odds_str = entry.get("odds") or entry.get("line")
        if odds_str:
            imp = american_to_implied(odds_str)
        else:
            imp = 0.0
        entry["implied_win_pct"] = round(imp, 4)
        implieds.append(imp)

    total_implied = sum(implieds)

    # Avoid division by zero
    if total_implied <= 0:
        for entry in odds_list:
            entry["fair_win_pct"] = 0.0
        return odds_list

    # Normalize
    for entry in odds_list:
        imp = entry.get("implied_win_pct", 0.0)
        entry["fair_win_pct"] = round((imp / total_implied) * 100, 4)

    return odds_list


# ---------------------------------------------------------------------------
# DraftKings
# ---------------------------------------------------------------------------

def get_draftkings_odds(tournament: str = None) -> list[dict]:
    """
    Scrape DraftKings golf outright odds.

    Returns: [{
        "player": "Scottie Scheffler",
        "odds": "+650",
        "implied_win_pct": 13.33,
        "line": 650,
        "sportsbook": "draftkings",
    }, ...]

    Cache TTL: 4h.

    Approach:
      1. Look for JSON in page source (window.__INITIAL_STATE__ or similar)
      2. Try direct API: /api/v4/sports/golf/leagues/pga/outrights
      3. Fall back to HTML table parsing
      4. If all fail, return empty list + use cached data
    """
    cache_key = "odds:draftkings"

    from cache.cache import cache_get_or_fetch, cache_get

    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        url = "https://sportsbook.draftkings.com/sites/US/sports/golf/pga-tour"
        headers = {"User-Agent": USER_AGENT}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        time.sleep(random.uniform(1.5, 3.0))
        html = resp.text

        players = []

        # 1. Try window.__INITIAL_STATE__ or window.__PRELOADED_STATE__
        m = re.search(
            r'(?:window\.__INITIAL_STATE__|window\.__PRELOADED_STATE__)\s*=\s*(\{.*?\});',
            html,
            re.DOTALL,
        )
        if m:
            try:
                import json
                state = json.loads(m.group(1))
                # Navigate the state — golf outrights usually under:
                # state.routes.golf.outrights or similar
                # For now, do a shallow search for known player names
                text = m.group(1)
                # Look for patterns like "+650" near player names
                player_blocks = re.findall(
                    r'"displayName"\s*:\s*"([^"]+)"[^}]*\?"odds"\s*:\s*"([^"]+)"',
                    text,
                )
                for player_name, odds in player_blocks:
                    players.append({
                        "player": player_name,
                        "odds": odds,
                        "line": int(odds.replace("+", "").replace("-", "")) if odds else None,
                        "sportsbook": "draftkings",
                    })
            except Exception as e:
                logger.warning(f"Failed to parse __INITIAL_STATE__: {e}")

        if not players:
            # 2. Try API endpoint
            api_urls = [
                "https://sportsbook.draftkings.com/api/v4/sports/golf/leagues/pga/outrights",
                "https://sportsbook.draftkings.com/api/v4/sports/golf/outrights",
            ]
            for api_url in api_urls:
                try:
                    api_resp = requests.get(api_url, headers=headers, timeout=10)
                    if api_resp.status_code == 200:
                        import json
                        data = api_resp.json()
                        # Structure varies — look for offer outcomes
                        outcomes = data.get("outrights", []) or data.get("offers", []) or []
                        for offer in outcomes:
                            for outcome in offer.get("outcomes", []):
                                players.append({
                                    "player": outcome.get("participant", outcome.get("name", "")),
                                    "odds": outcome.get("odds"),
                                    "line": outcome.get("line"),
                                    "sportsbook": "draftkings",
                                })
                        if players:
                            break
                except Exception:
                    pass

        if not players:
            # 3. Fall back to HTML table parsing
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            # Look for table rows with player + odds
            rows = soup.find_all("tr")
            for row in rows:
                cols = row.find_all(["td", "th"])
                if len(cols) < 2:
                    continue
                cells = [c.get_text(strip=True) for c in cols]
                # Find cells with American odds pattern
                for i, cell in enumerate(cells):
                    if re.match(r"^[+-]\d+$", cell):
                        player_name = cells[i - 1] if i > 0 else ""
                        if player_name and len(player_name) > 1:
                            players.append({
                                "player": player_name,
                                "odds": cell,
                                "line": int(cell.replace("+", "").replace("-", "")),
                                "sportsbook": "draftkings",
                            })

        from cache.cache import cache_set
        cache_set(cache_key, players, ttl=14400)  # 4h
        return players

    except Exception as e:
        logger.error(f"DraftKings odds fetch failed: {e}")
        cached = cache_get(cache_key)
        return cached if cached is not None else []


# ---------------------------------------------------------------------------
# FanDuel
# ---------------------------------------------------------------------------

def get_fanduel_odds(tournament: str = None) -> list[dict]:
    """
    Scrape FanDuel golf outright odds — same interface as DraftKings.

    Returns: [{
        "player": "Scottie Scheffler",
        "odds": "+700",
        "implied_win_pct": 12.5,
        "line": 700,
        "sportsbook": "fanduel",
    }, ...]

    Cache TTL: 4h.
    """
    cache_key = "odds:fanduel"

    from cache.cache import cache_get_or_fetch, cache_get

    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        url = "https://sportsbook.fanduel.com/api/sports/golf/leagues/pga/outrights"
        headers = {"User-Agent": USER_AGENT}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        time.sleep(random.uniform(1.5, 3.0))

        import json
        data = resp.json()

        players = []
        # Navigate API response
        # FanDuel API: { offers: [{ outcomes: [{ participant, odds, line }] }] }
        offers = data.get("offers", []) or data.get("outrights", []) or []
        if isinstance(offers, dict):
            offers = [offers]

        for offer in offers:
            outcomes = offer.get("outcomes", []) or []
            for outcome in outcomes:
                participant = outcome.get("participant", outcome.get("name", ""))
                odds_str = outcome.get("odds", "")
                players.append({
                    "player": participant,
                    "odds": odds_str,
                    "line": int(odds_str.replace("+", "").replace("-", "")) if odds_str else None,
                    "sportsbook": "fanduel",
                })

        from cache.cache import cache_set
        cache_set(cache_key, players, ttl=14400)  # 4h
        return players

    except Exception as e:
        logger.error(f"FanDuel odds fetch failed: {e}")
        cached = cache_get(cache_key)
        return cached if cached is not None else []