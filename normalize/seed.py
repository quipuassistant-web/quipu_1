"""
seed.py — load OWGR top 200 + supplemental players into the crosswalk.

Idempotent: rerunning won't create duplicates (the resolver dedupes by
ESPN source_id, then by exact normalized alias). Use this once at agent
bootstrap, and re-run annually after refreshing seed_owgr.py from ESPN.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from normalize.players import PlayerCrosswalk
from normalize.seed_owgr import OWGR_TOP_200, SUPPLEMENTAL

logger = logging.getLogger("seed")


@dataclass
class SeedReport:
    total_input: int
    created: int           # newly inserted canonical records
    matched_existing: int  # already in DB (by ESPN id or exact alias)
    skipped: int           # malformed rows
    name_collisions: list[tuple[str, str]]  # (input_name, matched_existing_name)


def seed(db_path: str | Path, *, also_supplemental: bool = True) -> SeedReport:
    """
    Load all seed entries into the crosswalk at db_path. Creates the DB if
    it doesn't exist. Returns a report you can print or log.
    """
    xwalk = PlayerCrosswalk(db_path)
    report = SeedReport(0, 0, 0, 0, [])

    rows: list[tuple[str, str]] = []
    for entry in OWGR_TOP_200:
        # (rank, name, espn_id) — ignore rank for seeding
        rows.append((entry[1], entry[2]))
    if also_supplemental:
        rows.extend(SUPPLEMENTAL)
    report.total_input = len(rows)

    for display_name, espn_id in rows:
        if not display_name or not espn_id:
            report.skipped += 1
            continue

        # First, ESPN id lookup. If we've seen this id before, that wins.
        result = xwalk.resolve(display_name, source="espn", source_id=espn_id)

        if result.method == "source_id":
            report.matched_existing += 1
            continue

        if result.method in ("alias_exact", "fuzzy_high"):
            # We found a match by name but didn't have the ESPN id yet.
            # Register the id so future ESPN feeds resolve via the fast path.
            xwalk.add_source_id(result.canonical_id, "espn", espn_id)
            existing = xwalk.get_player(result.canonical_id)
            existing_name = existing["display_name"] if existing else "?"
            if existing_name != display_name:
                report.name_collisions.append((display_name, existing_name))
            report.matched_existing += 1
            continue

        # method == "queued" — nothing close enough, create a new canonical
        canonical_id = xwalk.create_player(display_name)
        xwalk.add_source_id(canonical_id, "espn", espn_id)
        report.created += 1

    xwalk.close()
    return report


def print_report(report: SeedReport) -> None:
    print(f"Seed run complete:")
    print(f"  Input rows:        {report.total_input}")
    print(f"  Created new:       {report.created}")
    print(f"  Matched existing:  {report.matched_existing}")
    print(f"  Skipped (bad data):{report.skipped}")
    if report.name_collisions:
        print(f"  Name variants encountered for existing players:")
        for new, existing in report.name_collisions:
            print(f"    {new!r:35s} → already stored as {existing!r}")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    db = sys.argv[1] if len(sys.argv) > 1 else "data/golf.db"
    Path(db).parent.mkdir(parents=True, exist_ok=True)

    print(f"Seeding crosswalk at {db}")
    print()

    # First run
    report = seed(db)
    print_report(report)

    # Re-run to demonstrate idempotency
    print()
    print("Re-running to verify idempotency...")
    report2 = seed(db)
    print_report(report2)

    # Inspect final state
    xwalk = PlayerCrosswalk(db)
    print()
    print("Final crosswalk stats:", xwalk.stats())

    # Spot-check a few tricky names
    print()
    print("Spot-checking tricky lookups:")
    for query in [
        "Scottie Scheffler",
        "Ludvig Aberg",            # diacritic stripped on input
        "Rasmus Hojgaard",         # ø → o
        "Sami Valimaki",           # ä → a
        "JJ Spaun",                # initials concatenated
        "J. T. Poston",            # initials spaced
        "Joaquin Niemann",         # accent stripped
        "Cam Young",               # nickname → Cameron Young
        "C. Young",                # ambiguous: Cameron or Carson?
        "Adrien Dumont de Chassart",  # multi-word last
        "Tiger Woods",             # supplemental
        "Some Unknown Pro",        # should queue
    ]:
        r = xwalk.resolve(query, source="test")
        if r.canonical_id:
            name = xwalk.get_player(r.canonical_id)["display_name"]
            print(f"  {query:<32s} → {name:<32s} ({r.method}, {r.confidence:.3f})")
        else:
            top = r.candidates[0].display_name if r.candidates else "—"
            print(f"  {query:<32s} → QUEUED (top candidate: {top}, conf={r.confidence:.3f})")

    xwalk.close()
