import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _implied_win_pct(odds_str: str) -> Optional[float]:
    """Convert Vegas odds string to implied win probability (decimal)."""
    if odds_str is None:
        return None
    s = str(odds_str).strip()
    if not s:
        return None
    try:
        if s.startswith("+"):
            decimal = int(s[1:]) / 100
        elif s.startswith("-"):
            decimal = 100 / int(s[1:])
        else:
            decimal = float(s)
        # Cap at reasonable range
        if decimal <= 0 or decimal > 1:
            return None
        return round(decimal, 4)
    except (ValueError, ZeroDivisionError):
        return None


def build_player_event_record(
    player_id: str,
    event_id: str,
    odds_data: Optional[dict] = None,
    dg_data: Optional[dict] = None,
) -> dict:
    """
    Build a unified record for one player at one event.

    Assembles data from the canonical player map and any supplied
    odds / datagolf dicts. Returns a flat record dict.
    """
    # Import late to avoid circular issues
    from normalize.players import CANONICAL_PLAYERS
    from normalize.events import CANONICAL_EVENTS

    base = CANONICAL_PLAYERS.get(player_id, {})
    event = CANONICAL_EVENTS.get(event_id, {})

    vegas_odds = None
    if odds_data and isinstance(odds_data, dict):
        vegas_odds = odds_data.get("odds")

    implied = None
    if vegas_odds:
        implied = _implied_win_pct(vegas_odds)

    return {
        "player_id": player_id,
        "event_id": event_id,
        "display_name": base.get("display_name", player_id),
        "owgr": base.get("owgr"),
        "tier": base.get("tier"),
        "burned": base.get("burned", False),
        "vegas_odds": vegas_odds,
        "implied_win_pct": implied,
        "datagolf_pred": dg_data.get("pred") if dg_data else None,
        "datagolf_t5": dg_data.get("t5") if dg_data else None,
        "datagolf_t10": dg_data.get("t10") if dg_data else None,
        "datagolf_t20": dg_data.get("t20") if dg_data else None,
        "make_cut_pct": dg_data.get("make_cut_pct") if dg_data else None,
        "course_fit_score": dg_data.get("course_fit_score") if dg_data else None,
        "recent_form_score": dg_data.get("recent_form_score") if dg_data else None,
        "course_history_score": dg_data.get("course_history_score") if dg_data else None,
        "field_strength_score": dg_data.get("field_strength_score") if dg_data else None,
        "situational_score": dg_data.get("situational_score") if dg_data else None,
        # Derived for convenience
        "event_type": event.get("type"),
        "event_venue": event.get("venue"),
    }


def build_all_player_records(
    event_id: str,
    odds_data: Optional[list] = None,
    dg_data: Optional[list] = None,
) -> list[dict]:
    """Build records for all players in the field for an event."""
    from normalize.players import CANONICAL_PLAYERS

    odds_map = {}
    if odds_data:
        for entry in odds_data:
            pid = entry.get("player_id") or entry.get("id")
            if pid:
                odds_map[pid] = entry

    dg_map = {}
    if dg_data:
        for entry in dg_data:
            pid = entry.get("player_id") or entry.get("id")
            if pid:
                dg_map[pid] = entry

    records = []
    for pid, pinfo in CANONICAL_PLAYERS.items():
        record = build_player_event_record(
            pid,
            event_id,
            odds_data=odds_map.get(pid),
            dg_data=dg_map.get(pid),
        )
        records.append(record)

    return records