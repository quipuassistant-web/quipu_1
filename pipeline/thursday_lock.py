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

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from ledger.ledger import Ledger
from normalize.players import PlayerCrosswalk, ensure_seeded
from scoring.inputs import build_event_inputs
from scoring.multi_objective import score_field

logger = logging.getLogger("thursday_lock")


def _confidence_label(c: float) -> str:
    if c >= 0.8:
        return "HIGH"
    if c >= 0.5:
        return "MED"
    return "LOW"


_EVENT_COLUMNS = "canonical_event_id, espn_event_id, name, course_name, is_major, start_date"


def _next_scheduled_event(ledger: Ledger, season: int):
    """Nearest future scheduled event in the ledger."""
    return ledger.conn.execute(
        f"""
        SELECT {_EVENT_COLUMNS}
        FROM events
        WHERE season = ? AND status = 'scheduled'
          AND (start_date IS NULL OR start_date >= date('now'))
        ORDER BY (start_date IS NULL), start_date ASC
        LIMIT 1
        """,
        (season,),
    ).fetchone()


def _event_by_espn_id(ledger: Ledger, espn_event_id: str):
    """Look up a specific event by ESPN id. Returns None if monday_open
    hasn't populated it yet."""
    return ledger.conn.execute(
        f"SELECT {_EVENT_COLUMNS} FROM events WHERE espn_event_id = ?",
        (espn_event_id,),
    ).fetchone()


def final_check(
    *,
    db_path: str | Path | None = None,
    season: int | None = None,
    weekly_skins_contribution: Optional[float] = None,
    pool_entries: Optional[int] = None,
    top_n: int = 5,
    espn_event_id: Optional[str] = None,
) -> list[dict]:
    """Re-score the upcoming event and print recommendations."""
    db_path = db_path or os.environ.get("QUIPU_DB", "data/golf.db")
    season = season or int(os.environ.get("QUIPU_SEASON", date.today().year))
    if weekly_skins_contribution is None:
        weekly_skins_contribution = float(os.environ.get("QUIPU_WEEKLY_SKINS", "372.0"))
    if pool_entries is None:
        pool_entries = int(os.environ.get("QUIPU_POOL_ENTRIES", "12"))

    ledger = Ledger(db_path)
    try:
        if espn_event_id:
            ev = _event_by_espn_id(ledger, espn_event_id)
            if ev is None:
                logger.error(
                    "ESPN event %s not found in ledger. Run monday_open with "
                    "--espn-event-id %s first to populate the field.",
                    espn_event_id, espn_event_id,
                )
                return []
        else:
            ev = _next_scheduled_event(ledger, season)
            if ev is None:
                logger.info("No scheduled event in the ledger. Run monday_open first.")
                return []
        canonical_event_id = ev["canonical_event_id"]
        event_espn_id = ev["espn_event_id"]
        skins_pot = ledger.current_skins_pot(season, weekly_contribution=weekly_skins_contribution)
        summary = ledger.season_summary(season)

        # Already-locked check — surface the pick instead of pretending we still
        # have a decision to make. Voided picks don't count (player got WD'd or
        # similar; the slot is open again).
        locked_pick = ledger.conn.execute(
            """
            SELECT p.canonical_player_id, pl.display_name
            FROM picks p
            LEFT JOIN players pl ON pl.canonical_id = p.canonical_player_id
            WHERE p.canonical_event_id = ? AND p.voided = 0
            """,
            (canonical_event_id,),
        ).fetchone()
    finally:
        ledger.close()

    if locked_pick is not None:
        name = locked_pick["display_name"] or locked_pick["canonical_player_id"]
        logger.warning(
            "Pick already locked for %s: %s. Skipping rec print — "
            "use record_pick.py --void to undo if you really need to re-score.",
            ev["name"], name,
        )
        return []

    # ESPN status check — best-effort; the script still works offline.
    if event_espn_id:
        try:
            from fetchers.espn import ESPNFetcher, HTTPCache
            cache = HTTPCache(db_path)
            xw = PlayerCrosswalk(db_path)
            ensure_seeded(xw)
            try:
                tournament = ESPNFetcher(cache, xw).event_by_id(event_espn_id)
                if tournament is not None:
                    logger.info(f"ESPN status for {tournament.name}: {tournament.status}")
            finally:
                cache.close()
                xw.close()
        except Exception as e:
            logger.warning(f"ESPN check skipped: {e}")

    logger.info("Reminder: paste fresh DraftKings odds via add_odds.py before locking.")

    # Re-score. Errors here are real bugs — let them propagate instead of
    # masking them with `return []`.
    inputs = build_event_inputs(
        db_path, canonical_event_id,
        season=season, skins_pot=skins_pot,
        expected_winner_pickers=max(pool_entries / 4.0, 1.0),
        pool_entries=pool_entries,
    )
    if not inputs.players:
        logger.warning(
            "Field for %s is empty (event_field has no rows). ESPN typically "
            "publishes the field Mon/Tue of event week — re-run monday_open "
            "closer to the event, then re-run thursday_lock.",
            ev["name"],
        )
        return []
    scored = score_field(inputs)[:top_n]

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Thursday-morning final-check pipeline.")
    parser.add_argument("--db", default=os.environ.get("QUIPU_DB", "data/golf.db"))
    parser.add_argument("--season", type=int,
                        default=int(os.environ.get("QUIPU_SEASON", date.today().year)))
    parser.add_argument("--espn-event-id",
                        help="Override the auto-detected upcoming event.")
    parser.add_argument("--entries", type=int,
                        default=int(os.environ.get("QUIPU_POOL_ENTRIES", "12")))
    parser.add_argument("--weekly-skins", type=float,
                        default=float(os.environ.get("QUIPU_WEEKLY_SKINS", "372.0")))
    parser.add_argument("--top-n", type=int, default=5)
    args = parser.parse_args()

    log_path = os.environ.get("QUIPU_LOG")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_path:
        handlers.append(logging.FileHandler(os.path.expanduser(log_path)))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    final_check(
        db_path=args.db,
        season=args.season,
        espn_event_id=args.espn_event_id,
        pool_entries=args.entries,
        weekly_skins_contribution=args.weekly_skins,
        top_n=args.top_n,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
