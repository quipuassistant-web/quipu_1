"""
record_pick.py — Thursday-lock CLI to commit your pick for the upcoming event.

Usage:
  python record_pick.py --player "scottie scheffler"
  python record_pick.py --player "Scheffler" --confidence HIGH
  python record_pick.py --player "Spieth" --rationale "Course-horse at Colonial"
  python record_pick.py --event-id 401580352 --player "..."   # explicit event

Behavior:
  - Looks up the upcoming event automatically (the most recently scheduled
    one in the ledger). Override with --event-id for cross-week corrections.
  - Resolves --player through the crosswalk. Accepts partial / lower-case /
    diacritic-stripped input. If ambiguous, prints the candidates and exits.
  - Verifies the player is in the event's field (warns if not — sponsor
    invites can still resolve correctly but might not be playing).
  - Refuses to pick a player who has already been used this season.
  - Refuses to pick if a pick is already recorded for this event (use
    --replace to override).
  - Confirms with the user before writing (unless --yes).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from normalize.players import PlayerCrosswalk
from ledger.ledger import Ledger


@dataclass
class PickContext:
    canonical_event_id: str
    event_name: str
    event_start: Optional[str]
    season: int


def _find_upcoming_event(ledger: Ledger, season: int) -> Optional[PickContext]:
    """Pick the right scheduled event for record_pick.

    Order of preference:
      1. Nearest future-dated scheduled event (mid-season ahead-of-time pick).
      2. Most-recent past-dated scheduled event (the event is in progress or
         monday_open just opened it for a tournament happening this week and
         the date is already today/yesterday).
      3. Any scheduled event with NULL start_date (data gap).

    Prior behavior was a plain ORDER BY start_date DESC LIMIT 1 — that picked
    the latest-dated scheduled event regardless of whether it was in the past
    or future, which got the wrong row mid-season with multiple scheduled
    events ahead.
    """
    # 1. Nearest future-dated scheduled event
    row = ledger.conn.execute(
        """
        SELECT canonical_event_id, name, start_date, season FROM events
        WHERE season = ? AND status = 'scheduled' AND start_date >= date('now')
        ORDER BY start_date ASC LIMIT 1
        """,
        (season,),
    ).fetchone()
    if row is None:
        # 2. Most-recent past-dated scheduled event (event in progress or
        # just opened for a tournament happening now)
        row = ledger.conn.execute(
            """
            SELECT canonical_event_id, name, start_date, season FROM events
            WHERE season = ? AND status = 'scheduled' AND start_date < date('now')
            ORDER BY start_date DESC LIMIT 1
            """,
            (season,),
        ).fetchone()
    if row is None:
        # 3. NULL-date fallback
        row = ledger.conn.execute(
            """
            SELECT canonical_event_id, name, start_date, season FROM events
            WHERE season = ? AND status = 'scheduled' AND start_date IS NULL
            LIMIT 1
            """,
            (season,),
        ).fetchone()
    if not row:
        return None
    return PickContext(
        canonical_event_id=row["canonical_event_id"],
        event_name=row["name"],
        event_start=row["start_date"],
        season=row["season"],
    )


def _event_field_contains(ledger: Ledger, canonical_event_id: str,
                          canonical_player_id: str) -> Optional[bool]:
    """
    Check if monday_open recorded this player as part of the field.
    Returns True/False if the field is known, None if no field has been
    persisted for this event yet (in which case we can't say either way).
    """
    has_field = ledger.conn.execute(
        "SELECT 1 FROM event_field WHERE canonical_event_id = ? LIMIT 1",
        (canonical_event_id,),
    ).fetchone()
    if not has_field:
        return None
    return ledger.is_in_field(canonical_event_id, canonical_player_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Record your one-and-done pick.")
    parser.add_argument("--db", default="data/golf.db")
    parser.add_argument("--player", required=True,
                        help="Player name (partial, case-insensitive, diacritic-free OK)")
    parser.add_argument("--event-id",
                        help="Canonical event id (default: most recent scheduled)")
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--confidence", choices=["HIGH", "MED", "LOW"],
                        help="Agent confidence level (optional metadata)")
    parser.add_argument("--rationale", help="Free-text reason (optional)")
    parser.add_argument("--replace", action="store_true",
                        help="Replace existing pick for this event")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: {db} not found. Run seed.py first.", file=sys.stderr)
        return 1

    xwalk = PlayerCrosswalk(db)
    ledger = Ledger(db)
    try:
        # ── 1. Resolve the event ───────────────────────────────────────
        if args.event_id:
            ev = ledger.get_event(args.event_id)
            if not ev:
                print(f"ERROR: unknown event id {args.event_id}", file=sys.stderr)
                print("       Run monday_open.py to register it first.", file=sys.stderr)
                return 1
            ctx = PickContext(
                canonical_event_id=ev["canonical_event_id"],
                event_name=ev["name"],
                event_start=ev["start_date"],
                season=ev["season"],
            )
        else:
            ctx = _find_upcoming_event(ledger, args.season)
            if not ctx:
                print("ERROR: no scheduled event found. Run monday_open.py.",
                      file=sys.stderr)
                return 1

        # ── 2. Resolve the player ──────────────────────────────────────
        result = xwalk.resolve(args.player, source="cli")
        if result.canonical_id is None:
            print(f"Couldn't resolve '{args.player}' to a known player.")
            if result.candidates:
                print(f"Closest matches:")
                for c in result.candidates[:5]:
                    print(f"  • {c.display_name}  (score={c.score:.3f})")
                print("Try a more specific name, or address the unresolved queue.")
            else:
                print("No close candidates found. Is the player in the seeded list?")
            return 1
        player = xwalk.get_player(result.canonical_id)

        # ── 3. Check burned ────────────────────────────────────────────
        burned = ledger.burned_player_ids(ctx.season)
        if result.canonical_id in burned:
            # Find which event burned them
            row = ledger.conn.execute(
                """
                SELECT e.name, e.start_date
                FROM picks p
                JOIN events e ON e.canonical_event_id = p.canonical_event_id
                WHERE p.season = ? AND p.canonical_player_id = ?
                """,
                (ctx.season, result.canonical_id),
            ).fetchone()
            print(f"ERROR: {player['display_name']} was already used this season at "
                  f"{row['name']} ({row['start_date']}). One-and-done.",
                  file=sys.stderr)
            return 2

        # ── 4. Check existing pick for this event ──────────────────────
        existing = ledger.get_pick_for_event(ctx.canonical_event_id)
        if existing and not args.replace:
            existing_player = xwalk.get_player(existing["canonical_player_id"])
            print(f"ERROR: a pick already exists for {ctx.event_name}: "
                  f"{existing_player['display_name']}", file=sys.stderr)
            print("Use --replace to override.", file=sys.stderr)
            return 3

        # ── 5. Confirm ─────────────────────────────────────────────────
        print()
        print(f"  Event:     {ctx.event_name}  ({ctx.event_start})")
        print(f"  Pick:      {player['display_name']}  ({result.canonical_id})")
        print(f"  Method:    {result.method}  (confidence={result.confidence:.3f})")
        if args.confidence:
            print(f"  Agent confidence: {args.confidence}")
        if args.rationale:
            print(f"  Rationale: {args.rationale}")
        if existing:
            existing_player = xwalk.get_player(existing["canonical_player_id"])
            print(f"  REPLACING: {existing_player['display_name']}")

        in_field = _event_field_contains(ledger, ctx.canonical_event_id,
                                          result.canonical_id)
        if in_field is False:
            print()
            print(f"  ⚠  WARNING: {player['display_name']} is not in this event's field.")
            print(f"     They may have withdrawn or not be playing this week.")
            print(f"     Picking them anyway will result in $0 / no-finish.")
        elif in_field is None:
            print(f"  Field check: unknown (run monday_open.py to populate)")
        print()

        if not args.yes:
            answer = input("Confirm? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 4

        # ── 6. Write ───────────────────────────────────────────────────
        if existing and args.replace:
            ledger.conn.execute(
                "DELETE FROM picks WHERE canonical_event_id = ?",
                (ctx.canonical_event_id,),
            )
            ledger.conn.commit()
        ledger.record_pick(
            season=ctx.season,
            canonical_event_id=ctx.canonical_event_id,
            canonical_player_id=result.canonical_id,
            agent_confidence=args.confidence,
            agent_rationale=args.rationale,
        )
        print(f"✓ Pick recorded.")
        return 0
    finally:
        xwalk.close()
        ledger.close()


if __name__ == "__main__":
    sys.exit(main())
