"""
import_picks.py — bootstrap the ledger from your existing pick history.

Use this for mid-season onboarding: you've already made N picks this season
and need to record them so the burn list, season earnings, and majors tracker
are correct.

Workflow:
  1. seed.py  (canonical players)
  2. backfill.py --from 2026-01-01  (populates events + season_results)
  3. import_picks.py  (this — matches your picks to events, records them)
  4. business as usual: monday_open, record_pick, sunday_close

Tournament names are fuzzy-matched against the events table by case-insensitive
substring. If a name doesn't match (or matches multiple events), the row is
skipped and printed for manual handling with --event-id override.

Voided picks (--league rule restored the player after a WD) are recorded with
voided=1 and don't count toward one-and-done.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from normalize.players import PlayerCrosswalk, ensure_seeded
from ledger.ledger import Ledger

logger = logging.getLogger("import_picks")


# ─────────────────────────────────────────────────────────────────────────────
# Chad's actual 2026 pick history
# ─────────────────────────────────────────────────────────────────────────────

# Each row: (tourney_alias, player_name, position, earnings, score_to_par,
#            fedex_points, voided=False, voided_reason=None)
#
# Notes:
#   - "The Classic" disambiguated to Cognizant Classic (Lowry P2 ~$726K)
#   - "Miami" left as-is; importer will fuzzy-match. Adjust manually if needed.
#   - "AT&T" → Pebble Beach Pro-Am; "RBC" → RBC Heritage; "Truist" → Truist Championship
#   - Diacritics: "Aberg" matches "Åberg" via the crosswalk normalizer.
#   - "Nicolas Echavarria" matches "Nico Echavarria" via fuzzy + nickname expansion.

CHAD_PICKS_2026 = [
    {"tourney": "Sony",          "player": "Nicolas Echavarria",
     "position": "111", "earnings": 0,       "score_to_par": 18,  "fedex": 0},
    {"tourney": "American Express", "player": "Patrick Cantlay",
     "position": "13",  "earnings": 169_740, "score_to_par": -20, "fedex": 55},
    {"tourney": "Farmers",       "player": "Cameron Young",
     "position": "22",  "earnings": 92_640,  "score_to_par": -10, "fedex": 37},
    {"tourney": "Phoenix",       "player": "Maverick McNealy",
     "position": "13",  "earnings": 188_000, "score_to_par": -11, "fedex": 57},
    {"tourney": "Pebble",        "player": "Si Woo Kim",
     "position": "45",  "earnings": 57_000,  "score_to_par": -10, "fedex": 16},
    {"tourney": "Genesis",       "player": "Hideki Matsuyama",
     "position": "28",  "earnings": 136_500, "score_to_par": -6,  "fedex": 32},
    {"tourney": "Cognizant",     "player": "Shane Lowry",
     "position": "2",   "earnings": 726_400, "score_to_par": -15, "fedex": 208},
    {"tourney": "Arnold Palmer", "player": "Rory McIlroy",
     "position": "71",  "earnings": 0,       "score_to_par": 6,   "fedex": 0,
     "voided": True, "voided_reason": "WD before tournament — league rule restored"},
    {"tourney": "Players",       "player": "Collin Morikawa",
     "position": "123", "earnings": 0,       "score_to_par": 40,  "fedex": 0,
     "voided": True, "voided_reason": "WD before tournament — league rule restored"},
    {"tourney": "Valspar",       "player": "Jacob Bridgeman",
     "position": "14",  "earnings": 161_525, "score_to_par": -5,  "fedex": 4},
    {"tourney": "Houston",       "player": "Brooks Koepka",
     "position": "116", "earnings": 0,       "score_to_par": 16,  "fedex": 0},
    {"tourney": "Valero",        "player": "Russell Henley",
     "position": "71",  "earnings": 0,       "score_to_par": 10,  "fedex": 0},
    {"tourney": "Masters",       "player": "Bryson DeChambeau",
     "position": "60",  "earnings": 0,       "score_to_par": 20,  "fedex": 0},
    {"tourney": "RBC Heritage",  "player": "Christopher Gotterup",
     "position": "25",  "earnings": 142_750, "score_to_par": -8,  "fedex": 35},
    {"tourney": "Miami",         "player": "Jake Knapp",
     "position": None, "earnings": 0,        "score_to_par": 24,  "fedex": 0,
     "voided": True, "voided_reason": "WD before tournament — league rule restored"},
    {"tourney": "Truist",        "player": "Ludvig Aberg",
     "position": "8",   "earnings": 600_000, "score_to_par": -10, "fedex": 188},
]


# ─────────────────────────────────────────────────────────────────────────────
# Fuzzy event matching
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EventMatch:
    canonical_event_id: str
    event_name: str
    method: str   # 'exact_substr' | 'token_overlap' | 'multiple' | 'none'
    candidates: list[tuple[str, str]]  # (id, name) for ambiguous matches


def _normalize_tourney(s: str) -> str:
    """Lowercase, strip punctuation, condense whitespace."""
    return " ".join(
        ch for ch in s.lower().replace("&", "and").replace(".", " ").split()
    )


def match_event(ledger: Ledger, tourney_alias: str, season: int) -> EventMatch:
    """
    Find the canonical event matching this alias. Order of attempts:
      1. Substring match of normalized alias against normalized event name
      2. Token-overlap (any non-stopword token in alias appears in event name)
    """
    alias_norm = _normalize_tourney(tourney_alias)
    if not alias_norm:
        return EventMatch("", "", "none", [])

    events = ledger.conn.execute(
        "SELECT canonical_event_id, name FROM events WHERE season = ?",
        (season,),
    ).fetchall()

    # Pass 1: alias is a substring of event name
    hits = []
    for ev in events:
        ev_norm = _normalize_tourney(ev["name"])
        if alias_norm in ev_norm:
            hits.append((ev["canonical_event_id"], ev["name"]))

    if len(hits) == 1:
        return EventMatch(hits[0][0], hits[0][1], "exact_substr", [])
    if len(hits) > 1:
        return EventMatch("", "", "multiple", hits)

    # Pass 2: token overlap — at least one significant alias token in event name
    STOPWORDS = {"the", "of", "tournament", "championship", "open", "invitational",
                 "classic", "presented", "by", "in", "at", "and"}
    alias_tokens = [t for t in alias_norm.split() if t not in STOPWORDS and len(t) > 2]
    if not alias_tokens:
        # All tokens were stopwords — fall back to ANY token match
        alias_tokens = alias_norm.split()

    for ev in events:
        ev_norm = _normalize_tourney(ev["name"])
        if any(t in ev_norm.split() for t in alias_tokens):
            hits.append((ev["canonical_event_id"], ev["name"]))

    if len(hits) == 1:
        return EventMatch(hits[0][0], hits[0][1], "token_overlap", [])
    if len(hits) > 1:
        return EventMatch("", "", "multiple", hits)

    return EventMatch("", "", "none", [])


# ─────────────────────────────────────────────────────────────────────────────
# Importer
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    tourney: str
    player_name: str
    voided: bool
    status: str   # 'ok' | 'skipped_no_event' | 'skipped_ambiguous_event' |
                  # 'skipped_unresolved_player' | 'skipped_burned' |
                  # 'skipped_event_taken' | 'skipped_already_imported'
    event_name: Optional[str] = None
    canonical_event_id: Optional[str] = None
    canonical_player_id: Optional[str] = None
    detail: Optional[str] = None


def import_pick_row(
    *,
    ledger: Ledger,
    xwalk: PlayerCrosswalk,
    season: int,
    row: dict,
    override_event_id: Optional[str] = None,
    dry_run: bool = False,
) -> ImportResult:
    """Import a single row into the ledger."""
    tourney = row["tourney"]
    player_name = row["player"]
    voided = bool(row.get("voided", False))

    # Resolve event
    if override_event_id:
        ev = ledger.get_event(override_event_id)
        if not ev:
            return ImportResult(tourney, player_name, voided,
                                "skipped_no_event",
                                detail=f"override event id {override_event_id} not found")
        event_id = override_event_id
        event_name = ev["name"]
    else:
        match = match_event(ledger, tourney, season)
        if match.method == "none":
            return ImportResult(tourney, player_name, voided,
                                "skipped_no_event",
                                detail="no event in DB matched alias; "
                                "did you run backfill?")
        if match.method == "multiple":
            cands = ", ".join(f"{n} ({eid})" for eid, n in match.candidates)
            return ImportResult(tourney, player_name, voided,
                                "skipped_ambiguous_event",
                                detail=f"matched: {cands}")
        event_id = match.canonical_event_id
        event_name = match.event_name

    # Resolve player
    pr = xwalk.resolve(player_name, source="import")
    if pr.canonical_id is None:
        top = pr.candidates[0].display_name if pr.candidates else "—"
        return ImportResult(tourney, player_name, voided,
                            "skipped_unresolved_player",
                            event_name=event_name, canonical_event_id=event_id,
                            detail=f"top candidate: {top} (conf {pr.confidence:.2f})")
    player_id = pr.canonical_id

    # Idempotency: if a row (active OR voided) already exists for this
    # (event, player) pair, this exact pick was already imported. Skip
    # rather than appending a duplicate audit row. Applied before the
    # dry-run short-circuit so the dry-run report reflects what a real
    # run would actually write.
    existing_for_pair = ledger.conn.execute(
        "SELECT 1 FROM picks WHERE season = ? AND canonical_event_id = ? "
        "AND canonical_player_id = ? LIMIT 1",
        (season, event_id, player_id),
    ).fetchone()
    if existing_for_pair:
        return ImportResult(tourney, player_name, voided,
                            "skipped_already_imported",
                            event_name=event_name,
                            canonical_event_id=event_id,
                            canonical_player_id=player_id,
                            detail="pick for this (event, player) already in DB")

    if dry_run:
        return ImportResult(tourney, player_name, voided, "ok",
                            event_name=event_name,
                            canonical_event_id=event_id,
                            canonical_player_id=player_id,
                            detail="dry-run, not written")

    # Check if player is already burned by a non-voided pick this season —
    # if so, this import would either fail or create an inconsistency
    if not voided:
        burned = ledger.burned_player_ids(season)
        if player_id in burned:
            return ImportResult(tourney, player_name, voided,
                                "skipped_burned",
                                event_name=event_name,
                                canonical_event_id=event_id,
                                detail=f"player already burned by an earlier pick")

    # Record pick. Post burned-check, the only remaining integrity violation
    # is the partial unique on canonical_event_id (an active pick already
    # exists for this event from an earlier import or run).
    try:
        ledger.record_pick(
            season=season,
            canonical_event_id=event_id,
            canonical_player_id=player_id,
            voided=voided,
            voided_reason=row.get("voided_reason"),
        )
    except sqlite3.IntegrityError as e:
        msg = str(e)
        if "canonical_player_id" in msg:
            status = "skipped_burned"
        elif "canonical_event_id" in msg:
            status = "skipped_event_taken"
        else:
            status = "skipped_burned"  # fallback
        return ImportResult(tourney, player_name, voided, status,
                            event_name=event_name,
                            canonical_event_id=event_id,
                            detail=f"integrity: {e}")

    # Resolve pick with the supplied finish data
    made_cut: Optional[bool]
    position = row.get("position")
    if position is None or (isinstance(position, str) and position.upper() in ("WD", "DQ")):
        made_cut = None
    elif isinstance(position, str) and position.upper() == "CUT":
        made_cut = False
    else:
        made_cut = True  # has a numeric position → played the weekend

    ledger.resolve_pick(
        canonical_event_id=event_id,
        position=str(position) if position else None,
        score_to_par=row.get("score_to_par"),
        earnings=row.get("earnings"),
        made_cut=made_cut,
        fedex_points=row.get("fedex"),
    )

    return ImportResult(tourney, player_name, voided, "ok",
                        event_name=event_name,
                        canonical_event_id=event_id,
                        canonical_player_id=player_id)


def import_all(
    db_path: str | Path,
    rows: list[dict],
    season: int,
    *,
    overrides: Optional[dict[str, str]] = None,
    dry_run: bool = False,
) -> list[ImportResult]:
    """
    Import a list of pick rows. `overrides` is a {tourney_alias: event_id} map
    you can use to manually pin tournaments the fuzzy matcher couldn't resolve.
    """
    overrides = overrides or {}
    ledger = Ledger(db_path)
    xwalk = PlayerCrosswalk(db_path)
    ensure_seeded(xwalk)
    results: list[ImportResult] = []
    try:
        for row in rows:
            override = overrides.get(row["tourney"])
            r = import_pick_row(
                ledger=ledger, xwalk=xwalk, season=season,
                row=row, override_event_id=override, dry_run=dry_run,
            )
            results.append(r)
        return results
    finally:
        ledger.close()
        xwalk.close()


# ─────────────────────────────────────────────────────────────────────────────
# Pretty-print
# ─────────────────────────────────────────────────────────────────────────────

def print_results(results: list[ImportResult]) -> None:
    print()
    print(f"  {'Tourney':<16} {'Player':<22} {'Status':<28} {'Event matched':<30}")
    print("  " + "─" * 96)
    for r in results:
        status_str = r.status + ("  (voided)" if r.voided and r.status == "ok" else "")
        ev = r.event_name or "—"
        print(f"  {r.tourney[:16]:<16} {r.player_name[:22]:<22} "
              f"{status_str[:28]:<28} {ev[:30]:<30}")
        if r.detail:
            print(f"    └─ {r.detail}")
    print()
    ok = sum(1 for r in results if r.status == "ok")
    voided_count = sum(1 for r in results if r.voided and r.status == "ok")
    skipped = len(results) - ok
    print(f"  {ok} imported ({voided_count} voided / non-burning), {skipped} skipped")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Import past picks from CHAD_PICKS_2026.")
    parser.add_argument("--db", default=os.environ.get("QUIPU_DB", "data/golf.db"))
    parser.add_argument("--season", type=int,
                        default=int(os.environ.get("QUIPU_SEASON", date.today().year)))
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and match, don't write to DB")
    parser.add_argument("--override", action="append", default=[],
                        help="Manually pin a tournament: --override 'Miami=e_401580369'")
    parser.add_argument("--demo", action="store_true",
                        help="Run offline self-test against mock events")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.demo:
        _demo(Path(args.db))
        return 0

    overrides: dict[str, str] = {}
    for ov in args.override:
        if "=" not in ov:
            print(f"ERROR: --override must be 'alias=event_id'", file=sys.stderr)
            return 1
        k, v = ov.split("=", 1)
        overrides[k.strip()] = v.strip()

    print(f"Importing {len(CHAD_PICKS_2026)} picks into {args.db}")
    if args.dry_run:
        print("(DRY RUN — no writes)")
    results = import_all(args.db, CHAD_PICKS_2026, args.season,
                          overrides=overrides, dry_run=args.dry_run)
    print_results(results)

    # Surface unmatched tourneys clearly so user can supply --override
    unmatched = [r for r in results
                 if r.status in ("skipped_no_event", "skipped_ambiguous_event")]
    if unmatched:
        print(f"  {len(unmatched)} tournaments need manual --override:")
        for r in unmatched:
            print(f"    --override '{r.tourney}=e_<espn_event_id>'   "
                  f"# {r.detail}")
    return 0 if not unmatched else 2


# ─────────────────────────────────────────────────────────────────────────────
# Offline demo
# ─────────────────────────────────────────────────────────────────────────────

def _demo(db_path: Path) -> None:
    """Build mock events covering Chad's pick aliases, run the import, verify."""
    from normalize.seed import seed

    print("=" * 78)
    print(" IMPORT PICKS — Offline Demo")
    print("=" * 78)

    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print("\n[1] Seeding crosswalk...")
    seed(db_path)

    # Build mock events that the fuzzy matcher should find for each Chad alias.
    # Real ESPN names approximate.
    print("\n[2] Creating mock 2026 events covering Chad's pick history...")
    ledger = Ledger(db_path)
    mock_events = [
        ("401580351", "Sony Open in Hawaii",                       "2026-01-15"),
        ("401580352", "The American Express",                       "2026-01-22"),
        ("401580353", "Farmers Insurance Open",                     "2026-01-29"),
        ("401580354", "WM Phoenix Open",                            "2026-02-05"),
        ("401580355", "AT&T Pebble Beach Pro-Am",                   "2026-02-12"),
        ("401580356", "The Genesis Invitational",                   "2026-02-19"),
        ("401580357", "Cognizant Classic in The Palm Beaches",      "2026-02-26"),
        ("401580358", "Arnold Palmer Invitational presented by Mastercard", "2026-03-05"),
        ("401580359", "THE PLAYERS Championship",                   "2026-03-12"),
        ("401580360", "Valspar Championship",                       "2026-03-19"),
        ("401580361", "Texas Children's Houston Open",              "2026-03-26"),
        ("401580362", "Valero Texas Open",                          "2026-04-02"),
        ("401580363", "Masters Tournament",                         "2026-04-09"),
        ("401580364", "RBC Heritage",                               "2026-04-16"),
        ("401580365", "Corales Puntacana Championship",             "2026-04-23"),   # Miami fuzzy fallback
        ("401580366", "Truist Championship",                        "2026-05-07"),
    ]
    for eid, name, date in mock_events:
        ledger.upsert_event(
            espn_event_id=eid, name=name, season=2026,
            start_date=date, status="post",
        )
    ledger.close()
    print(f"    Created {len(mock_events)} mock events")

    print("\n[3] Running importer (dry run first)...")
    dry = import_all(db_path, CHAD_PICKS_2026, 2026, dry_run=True)
    print_results(dry)

    print("\n[4] Inspecting dry-run results for issues...")
    unmatched = [r for r in dry if r.status != "ok"]
    if unmatched:
        print(f"    {len(unmatched)} rows need attention:")
        for r in unmatched:
            print(f"      • {r.tourney}: {r.status}  ({r.detail})")

    # Miami likely doesn't match Corales Puntacana — demonstrate override
    miami_unmatched = any(r.tourney == "Miami" and r.status != "ok" for r in dry)
    if miami_unmatched:
        print(f"\n    Miami needs override. Pinning to e_401580365 "
              f"(Corales Puntacana)...")
        overrides = {"Miami": "e_401580365"}
    else:
        overrides = {}

    print("\n[5] Real import...")
    results = import_all(db_path, CHAD_PICKS_2026, 2026, overrides=overrides)
    print_results(results)

    # ── Validate the resulting ledger state ─────────────────────────────
    print("\n[6] Validating ledger state...")
    ledger = Ledger(db_path)

    summary = ledger.season_summary(2026)
    print(f"\n    Picks made:       {summary['picks_made']}")
    print(f"    Cuts made:        {summary['cuts_made']}")
    print(f"    Cuts missed:      {summary['cuts_missed']}")
    print(f"    Total earnings:   ${summary['total_earnings']:,.0f}")

    burned = ledger.burned_player_ids(2026)
    print(f"\n    Burned (counts toward one-and-done): {len(burned)}")

    # Verify the 3 WD picks did NOT burn their players
    xw = PlayerCrosswalk(db_path)
    mcilroy = xw.resolve("Rory McIlroy", source="t").canonical_id
    morikawa = xw.resolve("Collin Morikawa", source="t").canonical_id
    knapp = xw.resolve("Jake Knapp", source="t").canonical_id
    xw.close()

    print("\n[7] Sanity assertions...")
    # Total picks should be 16
    assert summary["picks_made"] == 16, f"Expected 16 picks, got {summary['picks_made']}"
    print(f"    ✓ All 16 picks recorded")

    # 3 voided picks → 13 burned players
    assert len(burned) == 13, f"Expected 13 burned, got {len(burned)}"
    print(f"    ✓ 13 active (burning) picks, 3 voided picks correctly excluded")

    # The three voided players should remain pickable
    assert mcilroy not in burned, "McIlroy should not be burned (voided)"
    assert morikawa not in burned, "Morikawa should not be burned (voided)"
    assert knapp not in burned, "Knapp should not be burned (voided)"
    print(f"    ✓ McIlroy, Morikawa, Knapp all still pickable")

    # Total earnings should match the sum of non-voided picks with earnings
    expected_earnings = sum(
        r["earnings"] for r in CHAD_PICKS_2026
        if r.get("earnings") and not r.get("voided")
    )
    # Voided picks may also contribute 0 earnings — they won't change the sum
    expected_earnings += sum(
        r["earnings"] for r in CHAD_PICKS_2026
        if r.get("earnings") and r.get("voided")
    )
    assert abs(summary["total_earnings"] - expected_earnings) < 1, \
        f"Earnings mismatch: {summary['total_earnings']} vs {expected_earnings}"
    print(f"    ✓ Total earnings ${summary['total_earnings']:,.0f} matches input")

    # Lowry's Cognizant 2nd should be highest single earning
    cognizant_pick = ledger.conn.execute(
        """
        SELECT p.earnings, p.position, p.canonical_player_id
        FROM picks p JOIN events e ON e.canonical_event_id = p.canonical_event_id
        WHERE e.name LIKE '%Cognizant%' AND p.season = 2026
        """,
    ).fetchone()
    assert cognizant_pick is not None
    assert cognizant_pick["position"] == "2"
    assert cognizant_pick["earnings"] == 726_400
    print(f"    ✓ Cognizant pick: pos {cognizant_pick['position']}, "
          f"${cognizant_pick['earnings']:,.0f}")

    # Can we re-pick McIlroy at a future event? (Should NOT raise.)
    print("\n[8] Verifying voided players are still pickable...")
    pga_id = ledger.upsert_event(
        espn_event_id="401580370", name="PGA Championship",
        season=2026, status="scheduled",
    )
    try:
        ledger.record_pick(
            season=2026, canonical_event_id=pga_id,
            canonical_player_id=mcilroy,
        )
        print(f"    ✓ McIlroy successfully picked for PGA Championship "
              f"(unique constraint correctly allowed re-pick)")
    except sqlite3.IntegrityError as e:
        print(f"    ✗ FAIL: re-pick of McIlroy blocked: {e}")
        ledger.close()
        sys.exit(1)

    # And trying to pick someone burned (e.g. Cantlay) SHOULD fail
    print("\n[9] Verifying one-and-done still works for non-voided burns...")
    xw = PlayerCrosswalk(db_path)
    cantlay = xw.resolve("Patrick Cantlay", source="t").canonical_id
    xw.close()
    cantlay_burned = cantlay in ledger.burned_player_ids(2026)
    assert cantlay_burned, "Cantlay should be burned (he was a non-voided AmEx pick)"
    print(f"    ✓ Cantlay correctly burned from AmEx pick")

    ledger.close()

    print("\n" + "=" * 78)
    print(" ✓ IMPORTER WORKING — ALL CHAD'S PICKS LOADED CORRECTLY")
    print("=" * 78)


if __name__ == "__main__":
    sys.exit(main())
