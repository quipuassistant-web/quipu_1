"""
scoring/inputs.py — assemble the inputs the scorer needs.

For each available player in this week's field, produce a PlayerInputs
record with everything the scorer might look at:

  - canonical id + display name
  - prior season finishes this season (the ones we've already played)
  - recent form: P(make cut), P(top10), P(top20), expected earnings per start
  - manual overrides: vegas_win_implied_pct, datagolf_finish_distribution

For event-level context (EventInputs):
  - purse, major status, field strength estimate
  - skins pot (with rollover), expected concentration of pool picks

Everything is computed from the ledger (no network), so the scorer is
deterministic and fast.

When DataGolf or Vegas data is unavailable, the relevant fields are None
and the scorer falls back to ESPN-derived estimates with lower confidence.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ledger.ledger import Ledger


# ─────────────────────────────────────────────────────────────────────────────
# Records
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PlayerInputs:
    canonical_id: str
    display_name: str
    # Season-to-date form from the season_results table — full leaderboards
    # populated by backfill.py and (eventually) sunday_close. Falls back to
    # 0/0 stats for players with no season_results rows; the scorer in turn
    # falls back to vegas_win_implied_pct or the flat prior.
    season_starts: int = 0
    season_cuts_made: int = 0
    season_top_10s: int = 0
    season_top_20s: int = 0
    season_earnings: float = 0.0
    season_wins: int = 0
    # Optional external signals (filled in by callers from manual entry,
    # DataGolf scrape, or Vegas odds).
    vegas_win_implied_pct: Optional[float] = None      # 0–1 fair, de-vigged
    vegas_top10_implied_pct: Optional[float] = None
    datagolf_finish_distribution: Optional[dict] = None  # {'win':..., 't5':..., 't10':..., 't20':..., 'cut':...}
    # Course-fit / venue history — hydrated from course_history.get_course_history
    # when build_event_inputs is given include_course_history=True.
    venue_starts: int = 0
    venue_top_10s: int = 0
    venue_made_cuts: int = 0
    venue_avg_finish: Optional[float] = None
    venue_top10_rate_shrunk: Optional[float] = None     # shrunk top-10 rate
    venue_skill_lift: float = 1.0                       # 1.0 = neutral; >1 = better here
    venue_evidence_weight: float = 0.0                  # 0 = no history
    # OWGR — global skill rank from normalize.owgr_live (live PDF import)
    # falling back to seed_owgr.py snapshot. Lower is better.
    owgr_rank: Optional[int] = None


@dataclass
class EventInputs:
    canonical_event_id: str
    name: str
    is_major: bool
    purse: Optional[float]
    field_size: int
    season: int
    skins_pot: float                  # actual dollars this week, with rollover
    expected_winner_pickers: float    # legacy coarse estimate, kept for fallback
    pool_entries: int = 12            # total entries; drives per-player pool-share estimate
    # Field strength: mean OWGR rank of players with a known rank. Lower is
    # stronger. None when nobody in the field has an OWGR rank.
    field_strength: Optional[float] = None
    # Optional weather summary for the event window (from fetchers.weather).
    weather: Optional[dict] = None
    # All players in the field, with computed inputs.
    players: list[PlayerInputs] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────────────────

def build_event_inputs(
    db_path: str | Path,
    canonical_event_id: str,
    *,
    season: int,
    skins_pot: float,
    expected_winner_pickers: float = 3.0,
    pool_entries: int = 12,
    vegas_odds: Optional[dict[str, float]] = None,       # {canonical_id: implied_win_pct}
    vegas_top10: Optional[dict[str, float]] = None,
    datagolf_distributions: Optional[dict[str, dict]] = None,
    include_course_history: bool = True,
) -> EventInputs:
    """
    Build the EventInputs for the scorer. Pulls field from the ledger,
    per-player form from season_results, and merges optional external signals.

    When include_course_history is True (default), also hydrates per-player
    venue stats from any prior events at the same course_name.
    """
    ledger = Ledger(db_path)
    try:
        ev = ledger.get_event(canonical_event_id)
        if not ev:
            raise ValueError(f"Unknown event: {canonical_event_id}")

        course_name = ev["course_name"] if include_course_history else None

        # OWGR live → fallback to seed
        from normalize.players import _owgr_rank_map
        owgr_ranks = _owgr_rank_map()

        # Auto-load odds from event_odds table if caller didn't provide them.
        # add_odds is a sibling module at the repo root; only ImportError is
        # an expected failure (missing module = no odds yet entered).
        if vegas_odds is None:
            try:
                from add_odds import load_odds_for_event
            except ImportError:
                vegas_odds = {}
            else:
                vegas_odds = load_odds_for_event(db_path, canonical_event_id) or {}

        field_rows = ledger.event_field(canonical_event_id)
        burned = ledger.burned_player_ids(season)

        players: list[PlayerInputs] = []
        for row in field_rows:
            cid = row["canonical_player_id"]
            if cid in burned:
                continue  # not pickable — exclude from scoring entirely

            # Player display name — join through players table
            p = ledger.conn.execute(
                "SELECT display_name FROM players WHERE canonical_id = ?", (cid,),
            ).fetchone()
            display_name = p["display_name"] if p else row["raw_name"]

            inputs = PlayerInputs(canonical_id=cid, display_name=display_name)
            _hydrate_season_form(ledger, inputs, season)
            inputs.owgr_rank = owgr_ranks.get(cid)

            if vegas_odds and cid in vegas_odds:
                inputs.vegas_win_implied_pct = vegas_odds[cid]
            if vegas_top10 and cid in vegas_top10:
                inputs.vegas_top10_implied_pct = vegas_top10[cid]
            if datagolf_distributions and cid in datagolf_distributions:
                inputs.datagolf_finish_distribution = datagolf_distributions[cid]

            if course_name:
                _hydrate_course_history(
                    db_path, inputs, course_name, canonical_event_id,
                )

            players.append(inputs)

        # Field strength: mean OWGR rank of resolved-and-ranked players.
        ranked = [p.owgr_rank for p in players if p.owgr_rank]
        field_strength = (sum(ranked) / len(ranked)) if ranked else None

        # Weather — best-effort; absence is fine.
        weather = None
        try:
            from fetchers.weather import get_tournament_weather
            if ev["start_date"]:
                weather = get_tournament_weather(
                    venue=ev["venue"],
                    event_name=ev["name"],
                    start_date=ev["start_date"],
                    end_date=ev["end_date"],
                )
        except Exception as e:
            # Network / DNS / etc. — log and move on; weather is optional.
            import logging
            logging.getLogger(__name__).info("weather fetch skipped: %s", e)

        return EventInputs(
            canonical_event_id=canonical_event_id,
            name=ev["name"],
            is_major=bool(ev["is_major"]),
            purse=ev["purse"],
            field_size=len(field_rows),
            season=season,
            skins_pot=skins_pot,
            expected_winner_pickers=expected_winner_pickers,
            pool_entries=pool_entries,
            field_strength=field_strength,
            weather=weather,
            players=players,
        )
    finally:
        ledger.close()


def _hydrate_course_history(
    db_path: str | Path,
    inputs: PlayerInputs,
    course_name: str,
    canonical_event_id: str,
) -> None:
    """Pull venue history. Imports locally to avoid circular import."""
    from course_history import get_course_history, CourseHistory

    h = get_course_history(
        db_path, inputs.canonical_id, course_name,
        exclude_event_id=canonical_event_id,  # don't peek at the event we're scoring
    )
    if isinstance(h, CourseHistory):
        inputs.venue_starts = h.starts
        inputs.venue_top_10s = h.top_10s
        inputs.venue_made_cuts = h.made_cuts
        inputs.venue_avg_finish = h.avg_finish
        inputs.venue_top10_rate_shrunk = h.top10_rate_shrunk
        inputs.venue_skill_lift = h.venue_skill_lift
        inputs.venue_evidence_weight = h.evidence_weight


def _hydrate_season_form(ledger: Ledger, inputs: PlayerInputs, season: int) -> None:
    """
    Pull this player's season-to-date form from season_results.
    This table is populated by sunday_close (writing full leaderboards) and
    by backfill.py (catching up on historical events).
    """
    rows = ledger.conn.execute(
        """
        SELECT position, score_to_par, earnings, made_cut
        FROM season_results
        WHERE season = ? AND canonical_player_id = ?
        """,
        (season, inputs.canonical_id),
    ).fetchall()
    for r in rows:
        inputs.season_starts += 1
        if r["made_cut"] == 1:
            inputs.season_cuts_made += 1
        if r["earnings"]:
            inputs.season_earnings += r["earnings"]
        pos = (r["position"] or "").lstrip("T")
        try:
            pos_int = int(pos)
            if pos_int == 1:
                inputs.season_wins += 1
            if pos_int <= 10:
                inputs.season_top_10s += 1
            if pos_int <= 20:
                inputs.season_top_20s += 1
        except (ValueError, TypeError):
            pass
