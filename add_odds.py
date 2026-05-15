"""
add_odds.py — record Vegas win odds for an upcoming event.

You scrape a sportsbook's outright market (DraftKings, BetMGM, etc.) and
paste odds in here. The tool:
  1. Parses American odds (±150) or implied probability (0.15)
  2. De-vigs the field (sportsbook overround typically 110-115%)
  3. Resolves each player through the crosswalk
  4. Writes to the event_odds table for the scorer to consume

The de-vig step matters: raw sportsbook implied probabilities sum to >100%
(that's the house edge). Dividing each by the field-wide sum normalizes
them to fair-win probabilities.

Two entry modes:

  Interactive (default) — paste a block of "Name [tab/comma/space] odds"
  lines, end with Ctrl-D. Mixed formats OK: '+650', '-150', '0.12'

      python add_odds.py --event-id e_401580351 --book draftkings
      Scottie Scheffler  +450
      Jon Rahm           +800
      Rory McIlroy       +900
      ...

  File mode — read from a CSV/TSV:

      python add_odds.py --event-id e_401580351 --book draftkings --file odds.csv

Usage:
  python add_odds.py --event-id <canonical_event_id> [--book BOOKNAME] [--file PATH]
  python add_odds.py --event-id <canonical_event_id> --list   # show stored odds
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from normalize.players import PlayerCrosswalk
from ledger.ledger import Ledger


ODDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_odds (
    canonical_event_id  TEXT NOT NULL,
    canonical_player_id TEXT NOT NULL,
    book                TEXT NOT NULL,         -- 'draftkings', 'betmgm', 'fanduel', etc.
    american_odds       INTEGER,
    raw_implied         REAL,                  -- pre-devig probability (0-1)
    fair_implied        REAL,                  -- post-devig probability (0-1)
    recorded_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (canonical_event_id, canonical_player_id, book),
    FOREIGN KEY (canonical_event_id) REFERENCES events(canonical_event_id)
);

CREATE INDEX IF NOT EXISTS idx_odds_event ON event_odds(canonical_event_id);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RawOddsRow:
    raw_name: str
    raw_odds: str
    american: Optional[int]
    raw_implied: Optional[float]
    error: Optional[str] = None


_LINE_SPLIT_RE = re.compile(r"[\t,]| {2,}")  # tab, comma, or 2+ spaces


def parse_odds_token(token: str) -> tuple[Optional[int], Optional[float], Optional[str]]:
    """
    Returns (american_odds, raw_implied_pct, error).
    Accepts American (+450, -150) or decimal implied (0.12, 12%).
    """
    t = token.strip().replace(",", "")
    if not t:
        return None, None, "empty"

    # Implied % form: "12%" or "0.12"
    if t.endswith("%"):
        try:
            v = float(t[:-1]) / 100
            return None, v, None
        except ValueError:
            return None, None, f"bad pct: {t}"
    if "." in t and not t.lstrip("-").startswith("0."):
        # Decimal European odds e.g. "5.50" — convert to American
        try:
            dec = float(t)
            if dec < 1.0:
                # treat as already-implied probability
                return None, dec, None
            am = round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))
            implied = 1 / dec
            return am, implied, None
        except ValueError:
            return None, None, f"bad decimal: {t}"

    # Bare small decimal: "0.12" = implied prob
    try:
        v = float(t)
        if -1 < v < 1:
            return None, v, None
    except ValueError:
        pass

    # American form: "+450" or "-150" (or unsigned positive ≥ 100)
    try:
        am = int(t)
        if am > 0:
            implied = 100 / (am + 100)
        elif am < 0:
            implied = (-am) / (-am + 100)
        else:
            return None, None, "zero odds"
        return am, implied, None
    except ValueError:
        return None, None, f"unparseable: {t}"


def parse_block(text: str) -> list[RawOddsRow]:
    """
    Parse a multi-line block. Each line: '<player name> <separator> <odds>'.
    Lines starting with '#' or empty lines are ignored.
    """
    rows: list[RawOddsRow] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _LINE_SPLIT_RE.split(line)
        if len(parts) < 2:
            # Single-space separator fallback — last whitespace-token is odds
            parts = line.rsplit(None, 1)
        if len(parts) != 2:
            rows.append(RawOddsRow(raw_name=line, raw_odds="",
                                   american=None, raw_implied=None,
                                   error="couldn't split name from odds"))
            continue
        name, odds_token = parts[0].strip(), parts[1].strip()
        am, implied, err = parse_odds_token(odds_token)
        rows.append(RawOddsRow(raw_name=name, raw_odds=odds_token,
                               american=am, raw_implied=implied, error=err))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# De-vigging
# ─────────────────────────────────────────────────────────────────────────────

def devig(implied_probs: list[float], *, default_hold: float = 0.10) -> list[float]:
    """
    Convert raw sportsbook implied probabilities to fair-win estimates.

    Two regimes:
      Full-field input (sum > 1.05): proportional normalization.
        The sum is >1 because of the book's vig. Dividing each by the sum
        gives fair probabilities. Standard for the full ~156-player field.

      Partial-field input (sum ≤ 1.05): fixed-hold reduction.
        You pasted only the favorites — the sum is <1 because the rest of
        the field isn't included. Renormalizing would inflate everyone.
        Instead, assume the book applies its standard hold (default 10%)
        and divide each by (1 + hold). Each player's estimate is now their
        fair "win-the-tournament" probability, and crucially they no longer
        artificially sum to 1.
    """
    total = sum(implied_probs)
    if total <= 0:
        return implied_probs
    if total > 1.05:
        # Real overround — proportional de-vig
        return [p / total for p in implied_probs]
    # Partial field — apply fixed-hold reduction
    factor = 1 + default_hold
    return [p / factor for p in implied_probs]


# ─────────────────────────────────────────────────────────────────────────────
# Main write path
# ─────────────────────────────────────────────────────────────────────────────

def store_odds(
    db_path: str | Path,
    canonical_event_id: str,
    book: str,
    parsed_rows: list[RawOddsRow],
    *,
    replace: bool = True,
    dry_run: bool = False,
) -> dict:
    """
    Resolve every parsed row through the crosswalk, de-vig the field, and
    write to event_odds. Returns a stats dict.
    """
    xwalk = PlayerCrosswalk(db_path)
    ledger = Ledger(db_path)
    # Add the odds table to the schema if it doesn't exist (ledger.py owns
    # primary schema; this is an additive extension)
    ledger.conn.executescript(ODDS_SCHEMA)

    stats = {
        "rows_input": len(parsed_rows),
        "rows_parsed_ok": 0,
        "rows_resolved": 0,
        "rows_queued": 0,
        "rows_written": 0,
        "raw_total_implied": 0.0,
        "vig_pct": 0.0,
        "errors": [],
    }
    try:
        # Filter to valid rows
        valid: list[tuple[RawOddsRow, str]] = []   # (row, canonical_id)
        for row in parsed_rows:
            if row.error or row.raw_implied is None:
                stats["errors"].append(f"{row.raw_name}: {row.error}")
                continue
            stats["rows_parsed_ok"] += 1
            r = xwalk.resolve(row.raw_name, source="odds")
            if r.canonical_id is None:
                stats["rows_queued"] += 1
                stats["errors"].append(
                    f"{row.raw_name}: unresolved (top candidate: "
                    f"{r.candidates[0].display_name if r.candidates else '—'})"
                )
                continue
            stats["rows_resolved"] += 1
            valid.append((row, r.canonical_id))

        if not valid:
            return stats

        # De-vig
        raw_probs = [row.raw_implied for row, _ in valid]
        stats["raw_total_implied"] = sum(raw_probs)
        stats["vig_pct"] = (stats["raw_total_implied"] - 1.0) * 100
        fair_probs = devig(raw_probs)

        if dry_run:
            return stats

        if replace:
            ledger.conn.execute(
                "DELETE FROM event_odds WHERE canonical_event_id = ? AND book = ?",
                (canonical_event_id, book),
            )

        for (row, cid), fair in zip(valid, fair_probs):
            ledger.conn.execute(
                """
                INSERT OR REPLACE INTO event_odds
                (canonical_event_id, canonical_player_id, book,
                 american_odds, raw_implied, fair_implied)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (canonical_event_id, cid, book,
                 row.american, row.raw_implied, fair),
            )
            stats["rows_written"] += 1
        ledger.conn.commit()
        return stats
    finally:
        xwalk.close()
        ledger.close()


def load_odds_for_event(
    db_path: str | Path,
    canonical_event_id: str,
    book: Optional[str] = None,
) -> dict[str, float]:
    """
    Read fair_implied probabilities back out as {canonical_id: fair_pct}.
    The scorer calls this and passes the result as vegas_odds=.

    If book is None, returns the most-recently-recorded book's odds.
    """
    ledger = Ledger(db_path)
    try:
        # Ensure schema exists (idempotent)
        ledger.conn.executescript(ODDS_SCHEMA)
        if book is None:
            row = ledger.conn.execute(
                """
                SELECT book FROM event_odds
                WHERE canonical_event_id = ?
                ORDER BY recorded_at DESC LIMIT 1
                """,
                (canonical_event_id,),
            ).fetchone()
            if not row:
                return {}
            book = row["book"]
        rows = ledger.conn.execute(
            """
            SELECT canonical_player_id, fair_implied
            FROM event_odds
            WHERE canonical_event_id = ? AND book = ?
            """,
            (canonical_event_id, book),
        ).fetchall()
        return {r["canonical_player_id"]: r["fair_implied"] for r in rows}
    finally:
        ledger.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Record Vegas win odds for an event.")
    parser.add_argument("--db", default="data/golf.db")
    parser.add_argument("--event-id", required=False,
                        help="Canonical event id (e.g. e_401580351)")
    parser.add_argument("--book", default="draftkings",
                        help="Sportsbook name (default: draftkings)")
    parser.add_argument("--file", help="Read odds from a file instead of stdin")
    parser.add_argument("--list", action="store_true",
                        help="List stored odds for the event")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and de-vig but don't write")
    parser.add_argument("--demo", action="store_true",
                        help="Run offline self-test")
    args = parser.parse_args()

    db = Path(args.db)

    if args.demo:
        _demo(db)
        return 0

    if not args.event_id:
        print("ERROR: --event-id is required (or use --demo)", file=sys.stderr)
        return 1

    if args.list:
        odds = load_odds_for_event(db, args.event_id, args.book if args.book else None)
        if not odds:
            print("(no odds stored for this event)")
            return 0
        xwalk = PlayerCrosswalk(db)
        rows = []
        for cid, fair in odds.items():
            p = xwalk.get_player(cid)
            rows.append((fair, p["display_name"] if p else cid))
        xwalk.close()
        rows.sort(reverse=True)
        print(f"  {'Player':<28}  {'Fair Win %':>10}")
        for fair, name in rows:
            print(f"  {name:<28}  {fair*100:>9.2f}%")
        return 0

    # Read odds block
    if args.file:
        text = Path(args.file).read_text()
    else:
        print(f"Paste odds for event {args.event_id} (book={args.book}).")
        print("Format: '<player name>  <american odds>' per line. Ctrl-D to finish.")
        print()
        text = sys.stdin.read()

    parsed = parse_block(text)
    if not parsed:
        print("No odds parsed.", file=sys.stderr)
        return 1

    stats = store_odds(db, args.event_id, args.book, parsed, dry_run=args.dry_run)

    is_partial = stats['raw_total_implied'] < 1.05
    regime = "partial-field (fixed 10% hold reduction)" if is_partial \
             else "full-field (proportional de-vig)"
    print()
    print(f"  Input rows:     {stats['rows_input']}")
    print(f"  Parsed OK:      {stats['rows_parsed_ok']}")
    print(f"  Resolved:       {stats['rows_resolved']}")
    print(f"  Queued/errors:  {stats['rows_queued']}")
    print(f"  Written:        {stats['rows_written']}")
    print(f"  Input sum:      {stats['raw_total_implied']:.3f}  → {regime}")
    if is_partial:
        print(f"  Note: input <1.0 means you pasted only the favorites. The")
        print(f"        remaining field gets the residual probability mass.")
    if stats["errors"]:
        print(f"  Errors / queued:")
        for e in stats["errors"][:10]:
            print(f"    • {e}")
        if len(stats["errors"]) > 10:
            print(f"    … {len(stats['errors']) - 10} more")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Demo
# ─────────────────────────────────────────────────────────────────────────────

def _demo(db_path: Path) -> None:
    """Offline demo: parses a real-looking DK paste, de-vigs, stores, reads back."""
    from seed import seed
    from ledger import Ledger as L

    print("=" * 72)
    print(" ADD ODDS — Offline Demo")
    print("=" * 72)

    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print("\n[1] Seeding crosswalk and creating a mock event...")
    seed(db_path)
    ledger = L(db_path)
    event_id = ledger.upsert_event(
        espn_event_id="401580370", name="PGA Championship",
        season=2026, status="scheduled",
        venue="Quail Hollow Club", purse=18_500_000,
    )
    ledger.close()
    print(f"    Event registered: {event_id}")

    # A realistic-looking paste mixing tabs, multiple spaces, and varied formats
    print("\n[2] Parsing odds block (realistic DK-style paste)...")
    odds_text = """
# DraftKings PGA Championship outrights, May 13 2026
Scottie Scheffler     +450
Rory McIlroy          +700
Xander Schauffele     +1400
Jon Rahm              +1600
Bryson DeChambeau     +1800
Collin Morikawa       +2200
Justin Thomas         +2500
Viktor Hovland        +2800
Hideki Matsuyama      +3000
Joaquín Niemann       +3500
Sam Burns             +4000
Tommy Fleetwood       +4500
Patrick Cantlay       +5000
Sahith Theegala       +6000
Cam Young             +6500
J.J. Spaun            +8000
"""
    parsed = parse_block(odds_text)
    print(f"    Parsed {len(parsed)} rows")
    for r in parsed[:3]:
        print(f"      {r.raw_name:<24} odds={r.raw_odds:<7}"
              f" → implied={r.raw_implied:.4f}")

    print("\n[3] Storing (de-vig applied)...")
    stats = store_odds(db_path, event_id, "draftkings", parsed)
    is_partial = stats["raw_total_implied"] < 1.05
    regime = "partial-field (fixed 10% hold)" if is_partial else "full-field (proportional)"
    print(f"    Input sum: {stats['raw_total_implied']:.3f}  → de-vig regime: {regime}")
    print(f"    Wrote {stats['rows_written']} rows")

    print("\n[4] Reading back fair probabilities...")
    odds = load_odds_for_event(db_path, event_id)
    xw = PlayerCrosswalk(db_path)
    rows = sorted(
        [(fair, xw.get_player(cid)["display_name"]) for cid, fair in odds.items()],
        reverse=True,
    )
    xw.close()
    print(f"    {'Player':<24}  {'Fair Win %':>10}")
    print(f"    {'─'*24}  {'─'*10}")
    for fair, name in rows[:8]:
        print(f"    {name:<24}  {fair*100:>9.2f}%")

    print("\n[5] Verifying de-vig math is realistic for partial-field input...")
    s = sum(odds.values())
    # Partial-field input should NOT sum to 1.0 — the remaining unentered
    # field gets the residual probability. With 16 favorites summing to ~77%
    # raw / 1.10 hold = ~70% fair, the remaining 30% is the rest of the field.
    assert s < 1.0, f"Partial-field de-vig should sum to <1.0, got {s}"
    assert s > 0.5, f"De-vig output unreasonably low: {s}"
    print(f"    ✓ Fair probs sum = {s:.4f}  (remaining ~{(1-s)*100:.0f}% = rest of field)")
    # Scheffler should land at a realistic ~16-17% (not the inflated ~24%)
    scheff_fair = next(fair for fair, name in rows if "Scheffler" in name)
    assert 0.15 < scheff_fair < 0.20, \
        f"Scheffler fair win should be ~16-17%, got {scheff_fair:.1%}"
    print(f"    ✓ Scheffler at {scheff_fair:.1%} — realistic for top favorite")

    print("\n[6] Scorer integration — confirm the scorer reads these odds...")
    from scoring.inputs import build_event_inputs
    # Quail Hollow PGA Championship — need a field. For the demo just use
    # everyone we just stored odds for.
    L_ = L(db_path)
    L_.set_event_field(event_id, [
        {"canonical_player_id": cid, "raw_name": "?", "espn_athlete_id": None}
        for cid in odds.keys()
    ])
    L_.close()

    inputs = build_event_inputs(
        db_path, event_id, season=2026, skins_pot=200.0,
        vegas_odds=odds,
    )
    top = next(p for p in inputs.players if "Scheffler" in p.display_name)
    assert top.vegas_win_implied_pct is not None, "Scheffler odds should be hydrated"
    assert 0.10 < top.vegas_win_implied_pct < 0.25, \
        f"Scheffler's fair win prob should be 10-25%, got {top.vegas_win_implied_pct}"
    print(f"    ✓ Scorer sees Scheffler's fair win = {top.vegas_win_implied_pct:.1%}")

    print("\n" + "=" * 72)
    print(" ✓ ODDS PIPELINE WORKING")
    print("=" * 72)


if __name__ == "__main__":
    sys.exit(main())
