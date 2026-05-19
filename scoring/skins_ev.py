"""
Skins expected-value calculator.

Pool-agnostic helpers; pass pot size + entry count in at call time. Chad's
defaults live in CLAUDE.md and the QUIPU_POOL_ENTRIES / QUIPU_WEEKLY_SKINS
env vars — don't bake them in here.

Core formula:
  EV = P(solo win) × (pot / n_pickers_for_winner)

Solo win = the player is one of n pool entries who picked them. If multiple
entries pick the same winner, the pot is split n ways. n_pickers can be
estimated from Vegas-implied win probabilities via estimate_pool_picks.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-export from fetchers.odds (makes this module self-contained)
# ---------------------------------------------------------------------------

def american_to_implied(odds_str: str) -> float:
    """
    Convert an odds string to implied win probability (percentage, 0–100).

    Accepts:
      '+650'  → 13.33  (American)
      '-200'  → 66.67  (American)
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
            # See fetchers/odds.py for the same fix — plain numeric is
            # either European decimal odds or an already-implied probability,
            # never a literal percentage.
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
    De-vig the odds: convert American → implied prob, normalize by total vig.

    The sum of all implied probs > 100% (that's the vig/bookmaker margin).
    De-vigged "fair" probabilities are each implied / sum_implied * 100.

    Returns same list with added "fair_win_pct" field (float, percentage).
    Keeps entries with implied=0 (no odds available).
    """
    if not odds_list:
        return odds_list

    implieds = []
    for entry in odds_list:
        odds_str = entry.get("odds") or entry.get("line")
        if odds_str:
            imp = american_to_implied(str(odds_str))
        else:
            imp = 0.0
        entry["implied_win_pct"] = round(imp, 4)
        implieds.append(imp)

    total_implied = sum(implieds)

    if total_implied <= 0:
        for entry in odds_list:
            entry["fair_win_pct"] = 0.0
        return odds_list

    for entry in odds_list:
        imp = entry.get("implied_win_pct", 0.0)
        entry["fair_win_pct"] = round((imp / total_implied) * 100, 4)

    return odds_list


# ---------------------------------------------------------------------------
# Solo win probability
# ---------------------------------------------------------------------------

def calc_solo_win_prob(odds_str: str) -> float:
    """
    Convert American odds string to solo win probability (decimal).
    '+650' → 0.133 (13.3%)
    '-200' → 0.667 (66.7%)
    """
    return american_to_implied(odds_str) / 100.0


# ---------------------------------------------------------------------------
# Pool pick distribution estimator
# ---------------------------------------------------------------------------

def estimate_pool_picks(odds_list: list[dict], n_entries: int = 12) -> dict[str, float]:
    """
    Estimate what fraction of the pool picks each player.

    Simple top-heavy model:
    - Top 5 players by vegas implied win% attract ~60% of picks
    - Top 2 of those attract ~36% and ~24% of that chunk
    - Remaining 40% spreads evenly across the rest of the field

    Returns: {"player_name": pick_share_float}
    e.g., {"Scottie Scheffler": 0.36, "Rory McIlroy": 0.24, ...}

    Args:
        odds_list: list of dicts with "player" and "implied_win_pct" keys
        n_entries: total number of entries in the pool (default 12)
    """
    if not odds_list:
        return {}

    # Sort by implied win%
    sorted_players = sorted(
        odds_list,
        key=lambda x: x.get("implied_win_pct", 0),
        reverse=True
    )

    n_top = min(5, len(sorted_players))
    top_picks = sorted_players[:n_top]

    # Top-heavy weights: 36%, 24%, 16%, 12%, 8% for top 5
    top_weights = [0.36, 0.24, 0.16, 0.12, 0.08][:n_top]
    total_top = sum(top_weights)
    # Normalize to sum to 0.6 (top 5 get 60% of pool)
    top_weights = [w / total_top * 0.6 for w in top_weights]

    result = {}
    for i, player in enumerate(top_picks):
        result[player["player"]] = top_weights[i]

    # Remaining 40% spread evenly among rest
    rest = sorted_players[n_top:]
    if rest:
        per_rest = 0.4 / len(rest)
        for player in rest:
            result[player["player"]] = per_rest
    else:
        # All picks went to top-5; scale them to 100%
        total = sum(top_weights)
        top_weights = [w / total for w in top_weights]
        for i, player in enumerate(top_picks):
            result[player["player"]] = top_weights[i]

    return result


def estimate_pool_size(n_entries: int = 12, entry_fee: float = 150.0) -> float:
    """
    Calculate total pool size.
    Chad's pool: 12 entries × $150 = $1800.
    Weekly skins portion: 12 × $31 = $372.
    """
    return n_entries * entry_fee


# ---------------------------------------------------------------------------
# Core skins EV
# ---------------------------------------------------------------------------

def skins_ev(
    player_odds: str,
    solo_prob: float,
    pot_size: float,
    n_pickers: int,
) -> float:
    """
    Calculate expected value for a skins pot pick.

    EV = P(solo win) × (pot / n_pickers_for_winner)

    Args:
        player_odds: American odds string (e.g., "+650")
        solo_prob: probability of winning outright (decimal, 0-1)
        pot_size: total skins pot in dollars
        n_pickers: how many people in the pool picked this player

    Returns:
        Expected value in dollars.
    """
    if n_pickers <= 0:
        n_pickers = 1

    ev = solo_prob * (pot_size / n_pickers)
    return round(ev, 2)


# ---------------------------------------------------------------------------
# Full weekly skins breakdown
# ---------------------------------------------------------------------------

def weekly_skins_breakdown(
    odds_list: list[dict],
    pot_size: float,
    n_entries: int = 12,
) -> list[dict]:
    """
    For each player in odds list, compute skins EV.

    Returns sorted by EV descending.
    Uses estimate_pool_picks() to get n_pickers per player,
    then computes solo_prob from odds and skins_ev.

    Args:
        odds_list: list of {"player": str, "odds": str, "implied_win_pct": float}
        pot_size: current skins pot in dollars (caller supplies — no default)
        n_entries: total entries in pool (default 12)
    """
    pool_picks = estimate_pool_picks(odds_list, n_entries)

    results = []
    for player_odds in odds_list:
        player_name = player_odds.get("player", "Unknown")
        odds_str = player_odds.get("odds", "+500")
        implied_pct = player_odds.get("implied_win_pct", 0.0)

        # n_pickers = pool_share × n_entries (rounded)
        pool_share = pool_picks.get(player_name, 0.05)
        n_pickers_in_pool = max(1, int(round(pool_share * n_entries)))

        # Solo probability from vegas implied
        solo_prob = calc_solo_win_prob(odds_str)

        # Skins EV
        ev = solo_prob * (pot_size / n_pickers_in_pool)

        results.append({
            "player": player_name,
            "odds": odds_str,
            "implied_win_pct": round(implied_pct, 2),
            "solo_prob": round(solo_prob, 3),
            "n_pickers": n_pickers_in_pool,
            "skins_ev": round(ev, 2),
            "pool_share": round(pool_share, 3),
        })

    results.sort(key=lambda x: x["skins_ev"], reverse=True)
    return results