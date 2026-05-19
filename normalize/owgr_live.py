"""
OWGR live rankings — import from manually-downloaded PDFs.

owgr.com is JavaScript-heavy with no exposed JSON API; gmalbert's repo
solves this with Playwright (heavy chromium dep). We take the lighter
path: user downloads the latest weekly PDF from owgr.com/archive and
drops it into data/owgr/. This module imports it.

Workflow:
  1. Go to https://www.owgr.com/archive  (or https://www.owgr.com/current-world-ranking/)
  2. Download the weekly "world ranking" PDF (e.g. owgr20-2026.pdf)
  3. mv ~/Downloads/owgr20-2026.pdf data/owgr/
  4. python -m normalize.owgr_live --import data/owgr/owgr20-2026.pdf

Once imported, get_owgr_rank(canonical_id) returns the latest rank for
that player. Replaces the static seed_owgr.py snapshot (which doesn't
get updates without code edits).

Parser adapted from gmalbert/golf-predictions/scrapers/parse_owgr_pdfs_v3.py
(MIT — match against curated country list, longest-first, handle
spacing artefacts).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("owgr_live")


OWGR_SCHEMA = """
CREATE TABLE IF NOT EXISTS owgr_rankings (
    week_ending         TEXT NOT NULL,    -- YYYY-MM-DD; the Sunday the ranking covers
    rank                INTEGER NOT NULL,
    player_name         TEXT NOT NULL,    -- raw name from the PDF (pre-crosswalk)
    country             TEXT,
    avg_points          REAL,
    total_points        REAL,
    rank_last_week      TEXT,             -- raw '(N)' or 'T' or '-'
    events_played       INTEGER,
    source_pdf          TEXT NOT NULL,    -- filename for audit
    imported_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (week_ending, rank)
);

CREATE INDEX IF NOT EXISTS idx_owgr_player ON owgr_rankings(player_name);
CREATE INDEX IF NOT EXISTS idx_owgr_week ON owgr_rankings(week_ending);
"""


# Curated country list (longest-first for greedy backwards match). Borrowed
# from gmalbert's parse_owgr_pdfs_v3.py — covers every tour-eligible country.
GOLF_COUNTRIES = sorted([
    "United States", "Northern Ireland", "Chinese Taipei", "South Africa",
    "Korea; Republic of", "United Arab Emirates", "Puerto Rico", "Costa Rica",
    "Dominican Republic", "Czech Republic", "Saudi Arabia", "New Zealand",
    "South Korea", "England", "Australia", "Scotland", "Japan", "Sweden",
    "Thailand", "India", "China", "France", "Denmark", "Spain", "Germany",
    "Mexico", "Italy", "Canada", "Finland", "Ireland", "Argentina",
    "Netherlands", "Norway", "Austria", "Malaysia", "Colombia", "Switzerland",
    "Belgium", "Wales", "Philippines", "Chile", "Indonesia", "Brazil",
    "Portugal", "Singapore", "Vietnam", "Zimbabwe", "Kenya", "Nigeria",
    "Morocco", "Tunisia", "Turkey", "Peru", "Venezuela", "Paraguay",
    "Uruguay", "Ecuador", "Jamaica", "Russia", "Ukraine", "Poland",
    "Hong Kong", "Taiwan", "Israel", "Egypt",
], key=len, reverse=True)


@dataclass
class OWGRRow:
    rank: int
    player_name: str
    country: str
    avg_points: float
    total_points: float
    rank_last_week: str
    events_played: int


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(OWGR_SCHEMA)
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# PDF parsing
# ─────────────────────────────────────────────────────────────────────────────

def _split_name_country(text: str) -> tuple[str, str]:
    """'Scottie Scheffler United States' → ('Scottie Scheffler', 'United States').
    Tries each known country longest-first; falls back to last-token heuristics
    for the rare pdf rendering with missing spaces ('Tom KimKorea; Republic of')."""
    text = text.strip()
    for country in GOLF_COUNTRIES:
        if text.endswith(country):
            return text[:-len(country)].strip(), country
        nospace = country.replace(" ", "")
        if text.endswith(nospace):
            return text[:-len(nospace)].strip(), country
    words = text.split()
    if len(words) >= 2:
        if words[-2] in ("United", "Northern", "South", "New", "Saudi", "Chinese",
                         "Costa", "Puerto", "Czech", "Hong"):
            return " ".join(words[:-2]), " ".join(words[-2:])
        return " ".join(words[:-1]), words[-1]
    return text, "Unknown"


_RANK_LINE_RE = re.compile(r"^(\d+)\s+\(([^)]+)\)\s+<([^>]+)>\s+(.+)$")


def _parse_page_text(text: str) -> list[OWGRRow]:
    """Parse one PDF page's text into ranking rows."""
    lines = text.split("\n")
    header_idx = None
    for i, line in enumerate(lines):
        if "This" in line and "Last" in line and "Week" in line:
            header_idx = i
            break
    if header_idx is None:
        return []

    rows: list[OWGRRow] = []
    for line in lines[header_idx + 2:]:
        line = line.strip()
        if not line:
            continue
        if "Week" in line and ("Page" in line or "Footnotes" in line):
            break
        m = _RANK_LINE_RE.match(line)
        if not m:
            continue
        try:
            this_week = int(m.group(1))
            last_week = m.group(2).replace("T", "").replace("-", "0")
            remainder = m.group(4).strip()
            numbers = re.findall(r"-?[\d.]+", remainder)
            if len(numbers) < 6:
                continue
            avg_points = float(numbers[-6])
            total_points = float(numbers[-5])
            actual = int(float(numbers[-1]))
            first_num_pos = remainder.find(numbers[-6])
            name_country = remainder[:first_num_pos].strip()
            player_name, country = _split_name_country(name_country)
            rows.append(OWGRRow(
                rank=this_week,
                player_name=player_name,
                country=country,
                avg_points=avg_points,
                total_points=total_points,
                rank_last_week=last_week,
                events_played=actual,
            ))
        except (ValueError, IndexError) as e:
            logger.debug("skipping malformed line %r: %s", line[:80], e)
            continue
    return rows


_FILENAME_DATE_RE = re.compile(r"(?:ending|week)[ _-]*(\d+)[ _-]*(\w+)[ _-]*(\d{4})", re.IGNORECASE)
_FILENAME_WEEK_RE = re.compile(r"owgr(\d+)[-_](\d{4})", re.IGNORECASE)


def _infer_week_ending(pdf_path: Path) -> str:
    """Best-effort 'YYYY-MM-DD' from filename. Falls back to file mtime."""
    name = pdf_path.name
    m = _FILENAME_DATE_RE.search(name.replace("_", " "))
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y"
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = _FILENAME_WEEK_RE.search(name)
    if m:
        # ISO week N of year Y → Monday of that week (close enough for sort)
        week, year = int(m.group(1)), int(m.group(2))
        try:
            return datetime.fromisocalendar(year, week, 7).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return date.fromtimestamp(pdf_path.stat().st_mtime).isoformat()


def parse_owgr_pdf(pdf_path: str | Path) -> tuple[str, list[OWGRRow]]:
    """Parse a single OWGR PDF. Returns (week_ending_date_iso, rows)."""
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError(
            "pdfplumber required to import OWGR PDFs. pip install pdfplumber"
        ) from e

    pdf_path = Path(pdf_path)
    week_ending = _infer_week_ending(pdf_path)
    all_rows: list[OWGRRow] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_rows.extend(_parse_page_text(text))
    return week_ending, all_rows


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

def import_pdf(db_path: str | Path, pdf_path: str | Path) -> dict:
    """Parse a PDF and write its rows into owgr_rankings. Idempotent —
    re-importing the same PDF replaces its rows (keyed by week_ending+rank)."""
    pdf_path = Path(pdf_path)
    week_ending, rows = parse_owgr_pdf(pdf_path)
    if not rows:
        return {"imported": 0, "week_ending": week_ending, "source": pdf_path.name}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        # Clear any prior rows for this week_ending so re-imports don't accumulate.
        conn.execute("DELETE FROM owgr_rankings WHERE week_ending = ?", (week_ending,))
        conn.executemany(
            """
            INSERT INTO owgr_rankings
              (week_ending, rank, player_name, country, avg_points,
               total_points, rank_last_week, events_played, source_pdf)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(week_ending, r.rank, r.player_name, r.country,
              r.avg_points, r.total_points, r.rank_last_week,
              r.events_played, pdf_path.name) for r in rows],
        )
        conn.commit()
    finally:
        conn.close()
    return {"imported": len(rows), "week_ending": week_ending,
            "source": pdf_path.name}


def latest_week_ending(db_path: str | Path) -> Optional[str]:
    """ISO date of the most-recent imported ranking week."""
    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT MAX(week_ending) FROM owgr_rankings"
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def get_rank_by_name(db_path: str | Path, player_name: str) -> Optional[int]:
    """Lowest (best) rank for player_name across all imports. Useful for the
    crosswalk-bypass case where we only have the raw name."""
    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT rank FROM owgr_rankings
            WHERE player_name = ?
            ORDER BY week_ending DESC LIMIT 1
            """,
            (player_name,),
        ).fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def live_rank_map(db_path: str | Path) -> dict[str, int]:
    """{canonical_id: rank} for the most-recent week, resolved through the
    crosswalk. Players not in the crosswalk are skipped. Used by the
    dashboard / scorer in place of the static seed_owgr.py snapshot."""
    week = latest_week_ending(db_path)
    if not week:
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT rank, player_name FROM owgr_rankings WHERE week_ending = ?",
            (week,),
        ).fetchall()
    finally:
        conn.close()

    # Resolve through the crosswalk. Import locally to avoid module-init order.
    from normalize.players import PlayerCrosswalk, ensure_seeded
    xw = PlayerCrosswalk(db_path)
    ensure_seeded(xw)
    out: dict[str, int] = {}
    try:
        for r in rows:
            res = xw.resolve(r["player_name"], source="owgr")
            if res.canonical_id:
                out[res.canonical_id] = int(r["rank"])
    finally:
        xw.close()
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Import OWGR rankings from a PDF.")
    p.add_argument("--db", default=os.environ.get("QUIPU_DB", "data/golf.db"))
    p.add_argument("--import", dest="pdf_path",
                   help="Path to an OWGR weekly PDF (downloaded from owgr.com/archive).")
    p.add_argument("--show-latest", action="store_true",
                   help="Print the top 20 of the most-recently imported week.")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.pdf_path:
        result = import_pdf(args.db, args.pdf_path)
        print(f"Imported {result['imported']} rows from {result['source']} "
              f"for week ending {result['week_ending']}.")
        return 0

    if args.show_latest:
        week = latest_week_ending(args.db)
        if not week:
            print("No OWGR data imported yet. Run with --import path/to/owgr.pdf")
            return 1
        print(f"Latest week ending: {week}")
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute(
                "SELECT rank, player_name, country, avg_points "
                "FROM owgr_rankings WHERE week_ending = ? "
                "ORDER BY rank LIMIT 20",
                (week,),
            ):
                print(f"  {r['rank']:>3}. {r['player_name']:30} {r['country']:20} {r['avg_points']:.4f}")
        finally:
            conn.close()
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
