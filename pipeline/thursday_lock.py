"""
thursday_lock.py — Thursday morning final check.

Run after monday_open has populated the event and field; before record_pick.
Re-scores the field with fresh odds and prints the top recommendations.

Usage:
  python pipeline/thursday_lock.py
  QUIPU_DB=path/to/golf.db python pipeline/thursday_lock.py

Steps:
  1. ESPN field check — any last-minute WDs?
  2. Reminder to paste fresh DraftKings lines via add_odds.py
  3. Re-score the field via build_event_inputs + score_field
  4. Print ranked final recommendations and season summary

Does NOT lock the pick — that's record_pick.py.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

from ledger.ledger import Ledger
from normalize.players import PlayerCrosswalk
from scoring.inputs import build_event_inputs
from scoring.multi_objective import score_field

logger = logging.getLogger("thursday_lock")


def _confidence_label(c: float) -> str:
    if c >= 0.8:
        return "HIGH"
    if c >= 0.5:
        return "MED"
    return "LOW"


def _next_scheduled_event(ledger: Ledger, season: int):
    """Nearest future scheduled event in the ledger."""
    return ledger.conn.execute(
        """
        SELECT canonical_event_id, name, course_name, is_major, start_date
        FROM events
        WHERE season = ? AND status = 'scheduled'
          AND (start_date IS NULL OR start_date >= date('now'))
        ORDER BY (start_date IS NULL), start_date ASC
        LIMIT 1
        """,
        (season,),
    ).fetchone()


def final_check(
    *,
    db_path: str | Path | None = None,
    season: int | None = None,
    weekly_skins_contribution: float = 372.0,
    pool_entries: int = 12,
    top_n: int = 5,
) -> list[dict]:
    """Re-score the upcoming event and print recommendations."""
    db_path = db_path or os.environ.get("QUIPU_DB", "data/golf.db")
    season = season or int(os.environ.get("QUIPU_SEASON", date.today().year))

    ledger = Ledger(db_path)
    try:
        ev = _next_scheduled_event(ledger, season)
        if ev is None:
            logger.info("No scheduled event in the ledger. Run monday_open first.")
            return []
        canonical_event_id = ev["canonical_event_id"]
        skins_pot = ledger.current_skins_pot(season, weekly_contribution=weekly_skins_contribution)
        summary = ledger.season_summary(season)
    finally:
        ledger.close()

    # ESPN status check — best-effort; the script still works offline.
    try:
        from fetchers.espn import ESPNFetcher, HTTPCache
        cache = HTTPCache(db_path)
        xw = PlayerCrosswalk(db_path)
        try:
            tournament = ESPNFetcher(cache, xw).current_tournament()
            if tournament is not None:
                logger.info(f"ESPN status for {tournament.name}: {tournament.status}")
        finally:
            cache.close()
            xw.close()
    except Exception as e:
        logger.warning(f"ESPN check skipped: {e}")

    logger.info("Reminder: paste fresh DraftKings odds via add_odds.py before locking.")

    # Re-score
    try:
        inputs = build_event_inputs(
            db_path, canonical_event_id,
            season=season, skins_pot=skins_pot,
            expected_winner_pickers=max(pool_entries / 4.0, 1.0),
        )
        scored = score_field(inputs)[:top_n]
    except Exception as e:
        logger.error(f"Scoring failed: {e}")
        return []

    logger.info(f"=== FINAL RECS for {ev['name']} (THURSDAY) ===")
    out = []
    for i, r in enumerate(scored, start=1):
        conf = _confidence_label(r.finish_probs.confidence)
        logger.info(
            f"{i}. {r.display_name} | Score: {r.composite_score:.1f}/100 ({conf}) | "
            f"P(win)={r.finish_probs.win:.1%} | "
            f"E[$]=${r.expected_earnings:,.0f}"
        )
        out.append({
            "display_name": r.display_name,
            "canonical_id": r.canonical_id,
            "composite_score": r.composite_score,
            "confidence": conf,
            "finish_probs": r.finish_probs,
        })

    logger.info(
        f"Season: ${summary['total_earnings']:,.0f} earned | "
        f"{summary['events_completed']} events completed | "
        f"{summary['picks_made']} picks made"
    )
    return out


if __name__ == "__main__":
    log_path = os.environ.get("QUIPU_LOG")
    handlers = [logging.StreamHandler()]
    if log_path:
        handlers.append(logging.FileHandler(os.path.expanduser(log_path)))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    final_check()
