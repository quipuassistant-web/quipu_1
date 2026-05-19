"""
players.py — canonical player ID crosswalk for the golf pick agent.

Resolves player names from multiple data sources (ESPN, DataGolf, OWGR,
PGA Tour) to a single canonical ID, and routes ambiguous cases to a
manual review queue.

Design principles:
  - Append-only: never auto-merge or auto-delete canonical records.
  - Source-id lookup wins: if you have an ESPN athlete_id and we've seen
    it before, that's the answer. Names are only a fallback.
  - High-confidence-only auto-resolve: anything below the threshold goes
    to the review queue. False positives propagate silently for weeks;
    false negatives just sit in a list waiting for you.
  - Birth year is the disambiguator of last resort (Tom Kim vs Si Woo Kim,
    Charlie Woods vs Tiger Woods, etc.).
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover
    fuzz = None  # we'll fall back to a naive similarity if rapidfuzz isn't installed


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    canonical_id   TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    normalized     TEXT NOT NULL,
    country        TEXT,
    birth_year     INTEGER,
    is_active      INTEGER DEFAULT 1,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS player_aliases (
    canonical_id   TEXT NOT NULL,
    alias          TEXT NOT NULL,        -- normalized form
    raw_form       TEXT,                 -- original string as seen
    source         TEXT,
    added_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (canonical_id, alias),
    FOREIGN KEY (canonical_id) REFERENCES players(canonical_id)
);

CREATE TABLE IF NOT EXISTS player_source_ids (
    canonical_id   TEXT NOT NULL,
    source         TEXT NOT NULL,        -- 'espn', 'datagolf', 'owgr', 'pgatour'
    source_id      TEXT NOT NULL,
    added_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (canonical_id, source),
    UNIQUE (source, source_id),
    FOREIGN KEY (canonical_id) REFERENCES players(canonical_id)
);

CREATE TABLE IF NOT EXISTS players_unresolved (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    source_id       TEXT,
    raw_name        TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    candidates_json TEXT,
    seen_count      INTEGER DEFAULT 1,
    first_seen_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_to     TEXT,
    status          TEXT DEFAULT 'pending'  -- 'pending', 'resolved', 'rejected'
);

CREATE INDEX IF NOT EXISTS idx_aliases_alias ON player_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_unresolved_status ON players_unresolved(status);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Name normalization
# ─────────────────────────────────────────────────────────────────────────────

# Common nicknames → expansion. Extend over time as you discover edge cases.
NICKNAME_MAP = {
    "cam": "cameron",
    "matt": "matthew",
    "jon": "jonathan",
    "tom": "thomas",
    "ben": "benjamin",
    "dan": "daniel",
    "danny": "daniel",
    "rob": "robert",
    "bob": "robert",
    "rick": "richard",
    "ricky": "richard",
    "will": "william",
    "billy": "william",
    "tony": "anthony",
    "chris": "christopher",
    "nick": "nicholas",
    "nicolas": "nico",         # Nico Echavarria
    "alex": "alexander",
    "sam": "samuel",
    "joe": "joseph",
    "mike": "michael",
}

_SUFFIX_PATTERN = re.compile(r"\s+(jr|sr|ii|iii|iv|v)$", re.IGNORECASE)
_PUNCT_KEEP = re.compile(r"[^\w\s'\-]")


# Nordic/Germanic letters that NFKD won't decompose — they're atomic codepoints,
# not letter+combining-mark pairs. Must be replaced explicitly.
_ATOMIC_SUBSTITUTIONS = {
    "ø": "o", "Ø": "O",
    "æ": "ae", "Æ": "Ae",
    "œ": "oe", "Œ": "Oe",
    "ß": "ss",
    "đ": "d",  "Đ": "D",
    "ł": "l",  "Ł": "L",
    "þ": "th", "Þ": "Th",
    "ð": "d",  "Ð": "D",
    "ı": "i",                # Turkish dotless i
}


def strip_diacritics(s: str) -> str:
    """José María → Jose Maria, Højgaard → Hojgaard, Åberg → Aberg."""
    # Step 1: atomic substitutions for codepoints that don't decompose
    for src, dst in _ATOMIC_SUBSTITUTIONS.items():
        if src in s:
            s = s.replace(src, dst)
    # Step 2: NFKD strips combining marks from decomposable letters
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_name(raw: str, *, expand_nicknames: bool = False) -> str:
    """
    Canonical normalized form. Designed to be lossy in helpful ways:
      - lowercase
      - diacritics stripped
      - 'Last, First' → 'first last'
      - initials separated: 'J.T.' → 'j t'
      - generation suffixes stripped: 'Frankie Capan III' → 'frankie capan'
      - punctuation removed except internal apostrophes/hyphens
      - whitespace collapsed

    Set expand_nicknames=True only when comparing — don't store nickname-expanded
    forms as aliases, or 'Cam' and 'Cameron' both become 'cameron' and you lose
    the ability to keep them as distinct registered spellings.
    """
    if not raw:
        return ""

    s = raw.strip()

    # "Last, First" → "First Last" (DataGolf displays this way)
    if "," in s and s.count(",") == 1:
        last, first = s.split(",", 1)
        s = f"{first.strip()} {last.strip()}"

    s = strip_diacritics(s).lower()

    # Initials: 'j.t.' or 'j. t.' → 'j t '. Trailing dots: 'jr.' → 'jr '.
    s = re.sub(r"\.\s*", " ", s)

    # Strip generation suffixes
    s = _SUFFIX_PATTERN.sub("", s)

    # Remove most punctuation; keep apostrophes (D'Antonio) and hyphens (Aphibarnrat)
    s = _PUNCT_KEEP.sub("", s)

    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()

    if expand_nicknames and s:
        tokens = s.split()
        tokens = [NICKNAME_MAP.get(t, t) for t in tokens]
        s = " ".join(tokens)

    return s


# ─────────────────────────────────────────────────────────────────────────────
# Similarity scoring
# ─────────────────────────────────────────────────────────────────────────────

def _token_set_ratio(a: str, b: str) -> float:
    """Returns similarity in [0, 1]. Uses rapidfuzz if available."""
    if not a or not b:
        return 0.0
    if fuzz is not None:
        return fuzz.token_set_ratio(a, b) / 100.0
    # Fallback: simple token-set Jaccard
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def similarity(name_a: str, name_b: str) -> float:
    """
    Composite similarity used by the resolver.
    Weights:
      - 0.55 token-set (handles word order, middle names, initials)
      - 0.30 last-name exact (last name is the strongest single signal in golf)
      - 0.15 nickname-expanded token-set (catches 'Cam' vs 'Cameron')
    """
    a_norm = normalize_name(name_a)
    b_norm = normalize_name(name_b)
    a_exp = normalize_name(name_a, expand_nicknames=True)
    b_exp = normalize_name(name_b, expand_nicknames=True)

    token = _token_set_ratio(a_norm, b_norm)
    token_exp = _token_set_ratio(a_exp, b_exp)

    a_last = a_norm.split()[-1] if a_norm.split() else ""
    b_last = b_norm.split()[-1] if b_norm.split() else ""
    last_match = 1.0 if a_last and a_last == b_last else 0.0

    base = 0.55 * token + 0.30 * last_match + 0.15 * token_exp

    # Strong boost when the nickname-expanded forms are identical AND the last
    # names match. This catches 'Cam Young' / 'Cameron Young' confidently
    # without lowering the floor for other fuzzy matches.
    if a_exp and a_exp == b_exp and last_match == 1.0:
        return max(base, 0.97)

    return base


# ─────────────────────────────────────────────────────────────────────────────
# Core resolver
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    canonical_id: str
    display_name: str
    alias_matched: str
    score: float

    def to_dict(self) -> dict:
        return {
            "canonical_id": self.canonical_id,
            "display_name": self.display_name,
            "alias_matched": self.alias_matched,
            "score": round(self.score, 4),
        }


@dataclass
class ResolveResult:
    canonical_id: Optional[str]
    confidence: float
    method: str  # 'source_id', 'alias_exact', 'fuzzy_high', 'queued', 'created'
    candidates: list[Candidate]


class PlayerCrosswalk:
    """
    SQLite-backed canonical player crosswalk.

    Typical usage from a fetcher:
        xwalk = PlayerCrosswalk("data/golf.db")
        result = xwalk.resolve("Olazábal, José María", source="datagolf", source_id="dg_4321")
        if result.canonical_id is None:
            # queued for manual review; skip this row for now
            continue
        # use result.canonical_id to write the row to your unified store
    """

    # Auto-resolution thresholds. Conservative by design.
    AUTO_RESOLVE_MIN = 0.95     # below this, never auto-resolve
    QUEUE_MIN = 0.70            # below this, don't even keep as a candidate

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ── canonical record management ──────────────────────────────────────

    def _next_canonical_id(self) -> str:
        row = self.conn.execute(
            "SELECT canonical_id FROM players ORDER BY canonical_id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "p_00001"
        last_num = int(row["canonical_id"].split("_")[1])
        return f"p_{last_num + 1:05d}"

    def create_player(
        self,
        display_name: str,
        *,
        country: Optional[str] = None,
        birth_year: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> str:
        """
        Create a new canonical player record. Returns the new canonical_id.
        Does not check for duplicates — caller is responsible for resolving first.
        """
        canonical_id = self._next_canonical_id()
        normalized = normalize_name(display_name)
        self.conn.execute(
            """
            INSERT INTO players (canonical_id, display_name, normalized, country, birth_year, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (canonical_id, display_name, normalized, country, birth_year, notes),
        )
        # Register the display name itself as an alias
        self.conn.execute(
            """
            INSERT OR IGNORE INTO player_aliases (canonical_id, alias, raw_form, source)
            VALUES (?, ?, ?, ?)
            """,
            (canonical_id, normalized, display_name, "manual"),
        )
        self.conn.commit()
        return canonical_id

    def add_alias(
        self,
        canonical_id: str,
        raw_form: str,
        source: str,
    ) -> None:
        """Register `raw_form` as an alias for `canonical_id`. No-op if the
        same canonical_id already has this alias. Logs a warning + skips if
        the alias is already mapped to a DIFFERENT canonical_id — that's an
        ambiguity the resolver should handle via the unresolved queue, not
        silently overwrite."""
        normalized = normalize_name(raw_form)
        if not normalized:
            return
        existing = self.conn.execute(
            "SELECT canonical_id FROM player_aliases WHERE alias = ? LIMIT 1",
            (normalized,),
        ).fetchone()
        if existing and existing["canonical_id"] != canonical_id:
            import logging
            logging.getLogger(__name__).warning(
                "alias collision: %r already maps to %s; refusing to add to %s",
                normalized, existing["canonical_id"], canonical_id,
            )
            return
        self.conn.execute(
            """
            INSERT OR IGNORE INTO player_aliases (canonical_id, alias, raw_form, source)
            VALUES (?, ?, ?, ?)
            """,
            (canonical_id, normalized, raw_form, source),
        )
        self.conn.commit()

    def add_source_id(self, canonical_id: str, source: str, source_id: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO player_source_ids (canonical_id, source, source_id)
            VALUES (?, ?, ?)
            """,
            (canonical_id, source, source_id),
        )
        self.conn.commit()

    # ── lookup primitives ────────────────────────────────────────────────

    def _lookup_by_source_id(self, source: str, source_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT canonical_id FROM player_source_ids WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()
        return row["canonical_id"] if row else None

    def _lookup_by_alias_exact(self, normalized: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT canonical_id FROM player_aliases WHERE alias = ? LIMIT 1",
            (normalized,),
        ).fetchone()
        return row["canonical_id"] if row else None

    def _all_aliases(self) -> Iterable[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT a.canonical_id, a.alias, p.display_name
            FROM player_aliases a
            JOIN players p ON p.canonical_id = a.canonical_id
            """
        ).fetchall()

    def _fuzzy_candidates(self, raw_name: str, top_k: int = 5) -> list[Candidate]:
        """
        Scores every known alias against the input and returns top-k above QUEUE_MIN.
        For O(thousands) of aliases this is fine; if you ever scale to O(100k)
        names, build a tri-gram index or use rapidfuzz.process.extract directly.
        """
        scored: list[Candidate] = []
        for row in self._all_aliases():
            score = similarity(raw_name, row["alias"])
            if score >= self.QUEUE_MIN:
                scored.append(
                    Candidate(
                        canonical_id=row["canonical_id"],
                        display_name=row["display_name"],
                        alias_matched=row["alias"],
                        score=score,
                    )
                )
        scored.sort(key=lambda c: c.score, reverse=True)

        # If two candidates from DIFFERENT canonical IDs both score very high,
        # that's a red flag — collapse only one-per-canonical and let the queue
        # surface the ambiguity.
        seen = set()
        deduped = []
        for c in scored:
            if c.canonical_id in seen:
                continue
            seen.add(c.canonical_id)
            deduped.append(c)
            if len(deduped) >= top_k:
                break
        return deduped

    # ── main entry point ─────────────────────────────────────────────────

    def resolve(
        self,
        raw_name: str,
        *,
        source: str,
        source_id: Optional[str] = None,
        queue_unresolved: bool = True,
    ) -> ResolveResult:
        """
        Resolve a raw name (from a fetcher) to a canonical_id.

        Order of operations:
          1. If source_id is provided and we've seen it → return immediately.
          2. Exact normalized-alias match → register source_id, return.
          3. Fuzzy match. If single candidate ≥ AUTO_RESOLVE_MIN with no close
             runner-up → register alias and source_id, return.
          4. Otherwise → log to unresolved queue, return None.

        Pass queue_unresolved=False for read-only lookups (e.g. the compat
        `lookup()` helper) so the unresolved table doesn't accumulate noise
        from queries that weren't supposed to add anything.
        """
        # 1. Source-ID fast path
        if source_id:
            existing = self._lookup_by_source_id(source, source_id)
            if existing:
                return ResolveResult(
                    canonical_id=existing,
                    confidence=1.0,
                    method="source_id",
                    candidates=[],
                )

        normalized = normalize_name(raw_name)
        if not normalized:
            return ResolveResult(None, 0.0, "queued", [])

        # 2. Exact alias match
        exact = self._lookup_by_alias_exact(normalized)
        if exact:
            if source_id:
                self.add_source_id(exact, source, source_id)
            return ResolveResult(
                canonical_id=exact,
                confidence=1.0,
                method="alias_exact",
                candidates=[],
            )

        # 3. Fuzzy
        candidates = self._fuzzy_candidates(raw_name)

        if candidates:
            top = candidates[0]
            runner_up = candidates[1].score if len(candidates) > 1 else 0.0
            # Require a clear winner: top is high AND well separated from #2
            if top.score >= self.AUTO_RESOLVE_MIN and (top.score - runner_up) >= 0.05:
                self.add_alias(top.canonical_id, raw_name, source)
                if source_id:
                    self.add_source_id(top.canonical_id, source, source_id)
                return ResolveResult(
                    canonical_id=top.canonical_id,
                    confidence=top.score,
                    method="fuzzy_high",
                    candidates=candidates,
                )

        # 4. Queue for manual review (skip when caller asked for read-only)
        if queue_unresolved:
            self._queue_unresolved(raw_name, normalized, source, source_id, candidates)
        return ResolveResult(
            canonical_id=None,
            confidence=candidates[0].score if candidates else 0.0,
            method="queued",
            candidates=candidates,
        )

    def _queue_unresolved(
        self,
        raw_name: str,
        normalized: str,
        source: str,
        source_id: Optional[str],
        candidates: list[Candidate],
    ) -> None:
        # If the same raw_name + source pair is already queued, bump the counter
        existing = self.conn.execute(
            """
            SELECT id, seen_count FROM players_unresolved
            WHERE source = ? AND raw_name = ? AND status = 'pending'
            """,
            (source, raw_name),
        ).fetchone()
        if existing:
            self.conn.execute(
                """
                UPDATE players_unresolved
                SET seen_count = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (existing["seen_count"] + 1, datetime.now(timezone.utc).isoformat(), existing["id"]),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO players_unresolved
                (source, source_id, raw_name, normalized_name, candidates_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    source,
                    source_id,
                    raw_name,
                    normalized,
                    json.dumps([c.to_dict() for c in candidates]),
                ),
            )
        self.conn.commit()

    # ── manual resolution workflow ───────────────────────────────────────

    def pending_unresolved(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM players_unresolved
            WHERE status = 'pending'
            ORDER BY seen_count DESC, last_seen_at DESC
            """
        ).fetchall()

    def confirm_unresolved(self, unresolved_id: int, canonical_id: str) -> None:
        row = self.conn.execute(
            "SELECT * FROM players_unresolved WHERE id = ?", (unresolved_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"No unresolved entry with id {unresolved_id}")
        self.add_alias(canonical_id, row["raw_name"], row["source"])
        if row["source_id"]:
            self.add_source_id(canonical_id, row["source"], row["source_id"])
        self.conn.execute(
            "UPDATE players_unresolved SET status = 'resolved', resolved_to = ? WHERE id = ?",
            (canonical_id, unresolved_id),
        )
        self.conn.commit()

    def reject_unresolved(self, unresolved_id: int, reason: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE players_unresolved SET status = 'rejected' WHERE id = ?",
            (unresolved_id,),
        )
        self.conn.commit()

    # ── inspection helpers ───────────────────────────────────────────────

    def get_player(self, canonical_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM players WHERE canonical_id = ?", (canonical_id,)
        ).fetchone()

    def stats(self) -> dict:
        def count(sql: str) -> int:
            return self.conn.execute(sql).fetchone()[0]
        return {
            "players": count("SELECT COUNT(*) FROM players"),
            "aliases": count("SELECT COUNT(*) FROM player_aliases"),
            "source_ids": count("SELECT COUNT(*) FROM player_source_ids"),
            "unresolved_pending": count(
                "SELECT COUNT(*) FROM players_unresolved WHERE status = 'pending'"
            ),
        }

    def close(self) -> None:
        self.conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Demo / smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile

    db = Path(tempfile.mkdtemp()) / "test_golf.db"
    xw = PlayerCrosswalk(db)

    # Seed canonical players
    scheffler = xw.create_player("Scottie Scheffler", country="USA", birth_year=1996)
    olazabal = xw.create_player("José María Olazábal", country="ESP", birth_year=1966)
    poston = xw.create_player("J.T. Poston", country="USA", birth_year=1993)
    capan = xw.create_player("Frankie Capan III", country="USA", birth_year=2000)
    tom_kim = xw.create_player("Tom Kim", country="KOR", birth_year=2002)
    si_woo_kim = xw.create_player("Si Woo Kim", country="KOR", birth_year=1995)
    cam_young = xw.create_player("Cameron Young", country="USA", birth_year=1997)
    # Adversarial: register a different "Cam" who shouldn't collide
    cam_smith = xw.create_player("Cameron Smith", country="AUS", birth_year=1993)

    xw.add_source_id(scheffler, "espn", "9478")
    xw.add_source_id(poston, "datagolf", "dg_1234")

    # Test cases: (input_name, source, source_id, expected_canonical_id, comment)
    tests = [
        ("Scottie Scheffler",       "espn",      "9478",     scheffler,  "source_id fast path"),
        ("S. Scheffler",            "datagolf",  None,       None,       "single initial → should QUEUE (ambiguous)"),
        ("Scheffler, Scottie",      "datagolf",  None,       scheffler,  "last-first comma form"),
        ("Jose Maria Olazabal",     "owgr",      None,       olazabal,   "diacritics stripped"),
        ("Olazábal, José María",    "datagolf",  None,       olazabal,   "diacritics + comma form"),
        ("J. T. Poston",            "owgr",      None,       poston,     "spaced initials"),
        ("JT Poston",               "espn",      None,       poston,     "joined initials"),
        ("Poston, J.T.",            "datagolf",  "dg_1234",  poston,     "source_id even with weird name"),
        ("Frankie Capan",           "espn",      None,       capan,      "suffix dropped"),
        ("Frankie Capan III",       "datagolf",  None,       capan,      "with suffix"),
        ("Cam Young",               "espn",      None,       cam_young,  "nickname expansion"),
        ("Cameron Young",           "datagolf",  None,       cam_young,  "full first name"),
        ("Tom Kim",                 "espn",      None,       tom_kim,    "ambiguous Korean name 1"),
        ("Si Woo Kim",              "espn",      None,       si_woo_kim, "ambiguous Korean name 2"),
        ("Joohyung Kim",            "owgr",      None,       None,       "Tom Kim's legal name — should QUEUE"),
        ("Tiger Woods",             "espn",      None,       None,       "unseen player — should QUEUE"),
    ]

    print(f"{'Input':<30} {'Method':<14} {'Score':<7} {'Match':<28} {'Pass?'}")
    print("─" * 92)
    passed = 0
    for raw, src, sid, expected, comment in tests:
        r = xw.resolve(raw, source=src, source_id=sid)
        match_name = xw.get_player(r.canonical_id)["display_name"] if r.canonical_id else "(queued)"
        ok = (r.canonical_id == expected)
        passed += int(ok)
        flag = "✓" if ok else "✗"
        print(f"{raw:<30} {r.method:<14} {r.confidence:<7.3f} {match_name:<28} {flag}  {comment}")

    print("─" * 92)
    print(f"Passed: {passed}/{len(tests)}")
    print()
    print("Stats:", xw.stats())
    print()
    print("Unresolved queue (would go to manual review):")
    for row in xw.pending_unresolved():
        cands = json.loads(row["candidates_json"]) if row["candidates_json"] else []
        top = cands[0] if cands else None
        print(f"  • {row['raw_name']:<25} (source={row['source']}, "
              f"top candidate={top['display_name'] if top else '—'} "
              f"@ {top['score'] if top else 0:.3f})")

    xw.close()


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers — backward-compatible API + derived data
#
# Burned-state, OWGR rank, and tier are NOT stored on the players table. They
# are derived: burned-state from the picks table (via Ledger), OWGR rank from
# seed_owgr.OWGR_TOP_200 + the live owgr_rankings table joined to
# player_source_ids, tier from rank.
# ─────────────────────────────────────────────────────────────────────────────

import os
from datetime import date


def _default_db_path() -> str:
    """Resolve the DB path. Env var wins; otherwise data/golf.db relative to cwd."""
    env = os.environ.get("QUIPU_DB")
    if env:
        return os.path.expanduser(env)
    return "data/golf.db"


_crosswalk: Optional["PlayerCrosswalk"] = None

def _get_crosswalk() -> "PlayerCrosswalk":
    global _crosswalk
    if _crosswalk is None:
        _crosswalk = PlayerCrosswalk(_default_db_path())
        _seed_if_empty()
    return _crosswalk

def _seed_if_empty():
    xw = _crosswalk
    if xw is None or xw.stats()["players"] > 0:
        return
    try:
        from normalize.seed import seed
    except ImportError:
        # No seed module — leave empty; caller can populate by hand.
        return
    try:
        seed(str(xw.db_path))
    except sqlite3.Error as e:
        # DB-level failure — log and leave empty rather than crashing import.
        # Anything else (programming errors in seed) should propagate.
        import logging
        logging.getLogger(__name__).warning("seed failed: %s", e)


# ── derived data: OWGR rank, tier, burned ───────────────────────────────────

_owgr_rank_cache: Optional[dict[str, int]] = None  # canonical_id → OWGR rank

def _owgr_rank_map() -> dict[str, int]:
    """{canonical_id: OWGR rank}. Prefers live data from owgr_rankings
    (imported via normalize.owgr_live from weekly PDFs); falls back to the
    static seed_owgr.py snapshot when no live data is loaded yet.
    Live data overlays seed — if a player appears in both, live wins."""
    global _owgr_rank_cache
    if _owgr_rank_cache is not None:
        return _owgr_rank_cache

    # Seed first (static snapshot, joined by ESPN id)
    seed_map: dict[str, int] = {}
    try:
        from normalize.seed_owgr import OWGR_TOP_200
    except ImportError:
        OWGR_TOP_200 = []
    if OWGR_TOP_200:
        espn_id_to_rank = {espn_id: rank for rank, _name, espn_id in OWGR_TOP_200}
        try:
            xw = _get_crosswalk()
            rows = xw.conn.execute(
                "SELECT canonical_id, source_id FROM player_source_ids WHERE source = 'espn'"
            ).fetchall()
            seed_map = {
                r["canonical_id"]: espn_id_to_rank[r["source_id"]]
                for r in rows
                if r["source_id"] in espn_id_to_rank
            }
        except sqlite3.Error:
            seed_map = {}

    # Overlay live data (more recent, resolved through the crosswalk)
    live: dict[str, int] = {}
    try:
        from normalize.owgr_live import live_rank_map
    except ImportError:
        pass
    else:
        try:
            xw = _get_crosswalk()
            live = live_rank_map(str(xw.db_path))
        except sqlite3.Error:
            live = {}

    merged = {**seed_map, **live}
    _owgr_rank_cache = merged
    return merged


def _tier_from_owgr(rank: Optional[int]) -> str:
    """elite ≤ 15, star ≤ 50, mid otherwise."""
    if rank is None or rank <= 0:
        return "mid"
    if rank <= 15:
        return "elite"
    if rank <= 50:
        return "star"
    return "mid"


def _burned_canonical_ids(season: Optional[int] = None) -> set[str]:
    """Active (non-voided) picks for the given season. Defaults to current year.

    Returns empty set if the picks table doesn't exist yet (Ledger schema not
    initialized) — equivalent to 'no picks recorded, nothing burned'.
    """
    if season is None:
        season = date.today().year
    xw = _get_crosswalk()
    try:
        rows = xw.conn.execute(
            "SELECT DISTINCT canonical_player_id FROM picks "
            "WHERE season = ? AND voided = 0",
            (season,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return set()
        raise
    return {r["canonical_player_id"] for r in rows}


# ── readable-id map (_readable_id_map dict) ────────────────────────────────

def _build_readable_map() -> dict:
    xw = _get_crosswalk()
    rows = xw.conn.execute("SELECT canonical_id, display_name FROM players").fetchall()
    result = {}
    for row in rows:
        key = re.sub(r'[^a-z0-9]', '_', row["display_name"].lower())
        key = re.sub(r'_+', '_', key).strip('_')
        result[key] = {"id": row["canonical_id"], "display_name": row["display_name"]}
    return result

_readable_id_map: dict = {}
_ensure_done = False

def _ensure_readable_map():
    global _readable_id_map, _ensure_done
    if not _ensure_done:
        try:
            _readable_id_map.update(_build_readable_map())
        except sqlite3.Error:
            # Players table doesn't exist yet (fresh DB pre-seed). Leave empty;
            # callers degrade gracefully via the canonical_id fallback.
            pass
        _ensure_done = True

def get_readable_id(db_canonical_id: str) -> str:
    _ensure_readable_map()
    for rid, pinfo in _readable_id_map.items():
        if pinfo["id"] == db_canonical_id:
            return rid
    return db_canonical_id

def canonicalize(name: str) -> str:
    return normalize_name(name)

def lookup(name: str) -> Optional[dict]:
    """Read-only name lookup. Does NOT queue unresolved entries —
    failing to find a player here shouldn't pollute the review queue."""
    xw = _get_crosswalk()
    result = xw.resolve(name, source="compat", source_id=None,
                        queue_unresolved=False)
    if result.canonical_id is None:
        return None
    row = xw.get_player(result.canonical_id)
    if row is None:
        return None
    return {"id": get_readable_id(row["canonical_id"]), "display_name": row["display_name"]}

def by_espn_id(espn_id: str, *, season: Optional[int] = None) -> Optional[dict]:
    xw = _get_crosswalk()
    cid = xw._lookup_by_source_id("espn", espn_id)
    if cid is None:
        return None
    row = xw.get_player(cid)
    if row is None:
        return None
    rank = _owgr_rank_map().get(cid)
    return {
        "id": get_readable_id(row["canonical_id"]),
        "display_name": row["display_name"],
        "owgr": rank if rank is not None else 0,
        "tier": _tier_from_owgr(rank),
        "burned": cid in _burned_canonical_ids(season),
    }

def get_all_available(*, season: Optional[int] = None) -> list[dict]:
    """All active, non-burned players. Burned-state comes from picks, not players."""
    xw = _get_crosswalk()
    burned = _burned_canonical_ids(season)
    ranks = _owgr_rank_map()
    rows = xw.conn.execute(
        "SELECT canonical_id, display_name FROM players WHERE is_active = 1"
    ).fetchall()
    _ensure_readable_map()
    n2r = {v["display_name"]: k for k, v in _readable_id_map.items()}
    out = []
    for r in rows:
        cid = r["canonical_id"]
        if cid in burned:
            continue
        rank = ranks.get(cid)
        out.append({
            "id": n2r.get(r["display_name"], cid),
            "display_name": r["display_name"],
            "owgr": rank if rank is not None else 0,
            "tier": _tier_from_owgr(rank),
            "burned": False,
        })
    return out

def get_burned_players(*, season: Optional[int] = None) -> set[str]:
    """Display names of players burned this season."""
    xw = _get_crosswalk()
    burned = _burned_canonical_ids(season)
    if not burned:
        return set()
    placeholders = ",".join("?" * len(burned))
    rows = xw.conn.execute(
        f"SELECT display_name FROM players WHERE canonical_id IN ({placeholders})",
        tuple(burned),
    ).fetchall()
    return {r["display_name"] for r in rows}

def add_player(canonical_id: str, player_data: dict):
    xw = _get_crosswalk()
    if xw.get_player(canonical_id) is None:
        xw.create_player(player_data["display_name"],
                         country=player_data.get("country"),
                         birth_year=player_data.get("birth_year"))

def resolve(raw_name: str, source: str = "agent", source_id: Optional[str] = None) -> "ResolveResult":
    return _get_crosswalk().resolve(raw_name, source=source, source_id=source_id)


def get_display_name(canonical_id: str) -> str:
    """Resolve canonical_id → display name; falls back to the id if unknown."""
    row = _get_crosswalk().get_player(canonical_id)
    return row["display_name"] if row else canonical_id


def get_owgr_rank_map() -> dict[str, int]:
    """{canonical_id: OWGR rank} for players in the seed top-200 with an ESPN id."""
    return _owgr_rank_map()


def ensure_seeded(crosswalk: "PlayerCrosswalk") -> bool:
    """Seed the crosswalk from OWGR top 200 + supplemental if it has no players.
    Returns True if a seed ran, False if the crosswalk was already populated.
    Safe to call repeatedly — checks count first."""
    if crosswalk.stats()["players"] > 0:
        return False
    from normalize.seed import seed
    seed(str(crosswalk.db_path))
    return True


def clear_owgr_cache() -> None:
    """Invalidate the in-process OWGR rank cache. Call this after importing
    a fresh OWGR PDF (owgr_live.import_pdf does this automatically) so
    long-running processes (e.g. the Flask dashboard) pick up new ranks
    without a restart."""
    global _owgr_rank_cache
    _owgr_rank_cache = None


# Don't eagerly init at import time. The crosswalk needs a DB to exist, and
# importing this module from a script that hasn't set up data/golf.db should
# not crash. Callers trigger initialization by calling any of the helpers above.
