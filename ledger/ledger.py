"""
ledger.py — season ledger for picks, events, and pool state.

Three tables:
  events  — canonical event records (one per tournament-week)
  picks   — your one-and-done picks, with results filled in post-tournament
  skins   — weekly skins pot state (rollovers, winners)

The ledger is the source of truth for "what golfers have I used", "what's
my current pot exposure", and "what's my season total". The dashboard
reads from here; the pipelines write here.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ledger")


LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    canonical_event_id TEXT PRIMARY KEY,
    espn_event_id      TEXT UNIQUE,
    name               TEXT NOT NULL,
    short_name         TEXT,
    season             INTEGER NOT NULL,
    start_date         TEXT,
    end_date           TEXT,
    venue              TEXT,
    course_name        TEXT,
    purse              REAL,
    is_major           INTEGER DEFAULT 0,
    status             TEXT,             -- 'scheduled' | 'in' | 'post'
    winner_canonical_id TEXT,            -- set when status = 'post'
    updated_at         TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS picks (
    pick_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    season             INTEGER NOT NULL,
    canonical_event_id TEXT NOT NULL,
    canonical_player_id TEXT NOT NULL,
    picked_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    agent_confidence   TEXT,             -- 'HIGH' | 'MED' | 'LOW' | NULL
    agent_rationale    TEXT,             -- optional free-text
    -- Filled in post-tournament:
    position           TEXT,
    score_to_par       INTEGER,
    earnings           REAL,
    made_cut           INTEGER,
    fedex_points       REAL,
    resolved_at        TEXT,
    -- Voided picks (replacements, post-pick WDs). Row preserved for audit;
    -- partial unique indexes below ignore voided rows so a void frees the
    -- event-slot AND the player for re-use.
    voided             INTEGER DEFAULT 0,
    voided_reason      TEXT,
    FOREIGN KEY (canonical_event_id) REFERENCES events(canonical_event_id)
);

-- Partial unique indexes enforce one-active-pick-per-event AND one-and-done
-- only against voided=0 rows. Voided rows stay for audit but don't block.
CREATE UNIQUE INDEX IF NOT EXISTS uq_picks_player_season_active
    ON picks(season, canonical_player_id) WHERE voided = 0;
CREATE UNIQUE INDEX IF NOT EXISTS uq_picks_event_active
    ON picks(canonical_event_id) WHERE voided = 0;

CREATE TABLE IF NOT EXISTS skins (
    season              INTEGER NOT NULL,
    canonical_event_id  TEXT NOT NULL,
    pot_size            REAL,             -- $ in pot going into this event
    winners_picked_count INTEGER,         -- # pool entries who picked the winner
    payout_per_winner   REAL,             -- pot_size / winners_picked_count if >0
    rolled_over         INTEGER DEFAULT 0,
    updated_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (season, canonical_event_id),
    FOREIGN KEY (canonical_event_id) REFERENCES events(canonical_event_id)
);

CREATE TABLE IF NOT EXISTS event_field (
    canonical_event_id  TEXT NOT NULL,
    canonical_player_id TEXT NOT NULL,
    raw_name            TEXT,           -- raw name from ESPN, for audit
    espn_athlete_id     TEXT,
    added_at            TEXT DEFAULT CURRENT_TIMESTAMP,
    withdrawn           INTEGER DEFAULT 0,   -- set 1 when player WDs pre-event
    PRIMARY KEY (canonical_event_id, canonical_player_id),
    FOREIGN KEY (canonical_event_id) REFERENCES events(canonical_event_id)
);

CREATE TABLE IF NOT EXISTS season_results (
    canonical_event_id  TEXT NOT NULL,
    canonical_player_id TEXT NOT NULL,
    season              INTEGER NOT NULL,
    position            TEXT,        -- '1', 'T5', 'CUT', 'WD', 'DQ'
    score_to_par        INTEGER,
    earnings            REAL,
    made_cut            INTEGER,     -- 1=true, 0=false, NULL=unknown
    fedex_points        REAL,
    added_at            TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (canonical_event_id, canonical_player_id),
    FOREIGN KEY (canonical_event_id) REFERENCES events(canonical_event_id)
);

CREATE INDEX IF NOT EXISTS idx_picks_season ON picks(season);
CREATE INDEX IF NOT EXISTS idx_picks_player ON picks(canonical_player_id);
CREATE INDEX IF NOT EXISTS idx_field_event ON event_field(canonical_event_id);
CREATE INDEX IF NOT EXISTS idx_field_player ON event_field(canonical_player_id);
CREATE INDEX IF NOT EXISTS idx_results_season_player ON season_results(season, canonical_player_id);
CREATE INDEX IF NOT EXISTS idx_results_event ON season_results(canonical_event_id);
"""


@dataclass
class EventRow:
    canonical_event_id: str
    espn_event_id: str
    name: str
    short_name: Optional[str]
    season: int
    start_date: Optional[str]
    end_date: Optional[str]
    venue: Optional[str]
    course_name: Optional[str]
    purse: Optional[float]
    is_major: bool
    status: str
    winner_canonical_id: Optional[str] = None


@dataclass
class PickRow:
    pick_id: Optional[int]
    season: int
    canonical_event_id: str
    canonical_player_id: str
    picked_at: str
    agent_confidence: Optional[str]
    agent_rationale: Optional[str]
    position: Optional[str] = None
    score_to_par: Optional[int] = None
    earnings: Optional[float] = None
    made_cut: Optional[bool] = None
    fedex_points: Optional[float] = None
    resolved_at: Optional[str] = None


class Ledger:
    """
    Season ledger. Pass the same SQLite path as the crosswalk to keep everything
    in one DB (recommended for v1), or split if you want different backup cadences.
    """

    # Canonical IDs for the four men's majors. Configure these once you've
    # seen the actual ESPN event names/IDs for the season — for now we match
    # by name fragments.
    MAJOR_NAME_FRAGMENTS = ("masters", "pga championship", "u.s. open", "us open",
                            "the open championship", "british open", "open championship")

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(LEDGER_SCHEMA)
        self.conn.commit()
        self._migrate_picks_unique_constraint()

    def _migrate_picks_unique_constraint(self) -> None:
        """Old schema had table-level UNIQUE(canonical_event_id), which made
        the void-and-replace flow impossible (voided rows still occupied the
        event slot). Detect the old constraint and rebuild the table with a
        partial unique index instead. Idempotent; no-op on fresh DBs."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='picks'"
        ).fetchone()
        if not row or "UNIQUE (canonical_event_id)" not in (row["sql"] or ""):
            return  # already on new schema
        logger.info("Migrating picks table: dropping unconditional UNIQUE on "
                    "canonical_event_id in favour of partial index.")
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE picks_new (
                    pick_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    season             INTEGER NOT NULL,
                    canonical_event_id TEXT NOT NULL,
                    canonical_player_id TEXT NOT NULL,
                    picked_at          TEXT DEFAULT CURRENT_TIMESTAMP,
                    agent_confidence   TEXT,
                    agent_rationale    TEXT,
                    position           TEXT,
                    score_to_par       INTEGER,
                    earnings           REAL,
                    made_cut           INTEGER,
                    fedex_points       REAL,
                    resolved_at        TEXT,
                    voided             INTEGER DEFAULT 0,
                    voided_reason      TEXT,
                    FOREIGN KEY (canonical_event_id) REFERENCES events(canonical_event_id)
                );
                INSERT INTO picks_new
                  SELECT pick_id, season, canonical_event_id, canonical_player_id,
                         picked_at, agent_confidence, agent_rationale,
                         position, score_to_par, earnings, made_cut, fedex_points,
                         resolved_at, voided, voided_reason
                  FROM picks;
                DROP TABLE picks;
                ALTER TABLE picks_new RENAME TO picks;
                CREATE UNIQUE INDEX uq_picks_player_season_active
                    ON picks(season, canonical_player_id) WHERE voided = 0;
                CREATE UNIQUE INDEX uq_picks_event_active
                    ON picks(canonical_event_id) WHERE voided = 0;
                CREATE INDEX idx_picks_season ON picks(season);
                CREATE INDEX idx_picks_player ON picks(canonical_player_id);
            """)

    # ── events ──────────────────────────────────────────────────────────

    @classmethod
    def _looks_like_major(cls, name: str) -> bool:
        n = (name or "").lower()
        return any(frag in n for frag in cls.MAJOR_NAME_FRAGMENTS)

    def upsert_event(
        self,
        *,
        espn_event_id: str,
        name: str,
        season: int,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        venue: Optional[str] = None,
        course_name: Optional[str] = None,
        purse: Optional[float] = None,
        status: str = "scheduled",
        short_name: Optional[str] = None,
        winner_canonical_id: Optional[str] = None,
    ) -> str:
        """
        Insert or update an event by ESPN id. Returns canonical_event_id.
        The canonical id is just 'e_<espn_id>' for v1 — simple and stable.
        """
        canonical_id = f"e_{espn_event_id}"
        is_major = 1 if self._looks_like_major(name) else 0

        self.conn.execute(
            """
            INSERT INTO events (
                canonical_event_id, espn_event_id, name, short_name, season,
                start_date, end_date, venue, course_name, purse, is_major,
                status, winner_canonical_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_event_id) DO UPDATE SET
                name=excluded.name,
                short_name=excluded.short_name,
                start_date=COALESCE(excluded.start_date, events.start_date),
                end_date=COALESCE(excluded.end_date, events.end_date),
                venue=COALESCE(excluded.venue, events.venue),
                course_name=COALESCE(excluded.course_name, events.course_name),
                purse=COALESCE(excluded.purse, events.purse),
                status=excluded.status,
                winner_canonical_id=COALESCE(excluded.winner_canonical_id, events.winner_canonical_id),
                updated_at=CURRENT_TIMESTAMP
            """,
            (canonical_id, espn_event_id, name, short_name, season,
             start_date, end_date, venue, course_name, purse, is_major,
             status, winner_canonical_id, datetime.utcnow().isoformat()),
        )
        self.conn.commit()
        return canonical_id

    def get_event(self, canonical_event_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE canonical_event_id = ?",
            (canonical_event_id,),
        ).fetchone()

    # ── picks ───────────────────────────────────────────────────────────

    def record_pick(
        self,
        *,
        season: int,
        canonical_event_id: str,
        canonical_player_id: str,
        agent_confidence: Optional[str] = None,
        agent_rationale: Optional[str] = None,
        voided: bool = False,
        voided_reason: Optional[str] = None,
    ) -> int:
        """
        Pre-tournament: record your pick. Raises if player already used this season.

        If voided=True, the pick is recorded for audit but does NOT count toward
        one-and-done — the player remains pickable. Use this for league-WD-rule
        situations where a player withdrew before tee time and your league
        restores your ability to pick them.
        """
        cur = self.conn.execute(
            """
            INSERT INTO picks (
                season, canonical_event_id, canonical_player_id,
                agent_confidence, agent_rationale, voided, voided_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (season, canonical_event_id, canonical_player_id,
             agent_confidence, agent_rationale,
             1 if voided else 0, voided_reason),
        )
        self.conn.commit()
        return cur.lastrowid

    def void_pick(
        self,
        canonical_event_id: str,
        reason: Optional[str] = None,
    ) -> bool:
        """
        Mark the ACTIVE pick for this event as voided. Frees the player for
        future picks AND frees the event slot for a replacement. Voided rows
        stay for audit. Returns False if no active pick exists for the event.
        """
        cur = self.conn.execute(
            """
            UPDATE picks SET voided = 1, voided_reason = ?
            WHERE canonical_event_id = ? AND voided = 0
            """,
            (reason, canonical_event_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def replace_pick(
        self,
        *,
        season: int,
        canonical_event_id: str,
        canonical_player_id: str,
        agent_confidence: Optional[str] = None,
        agent_rationale: Optional[str] = None,
        reason: str = "replaced",
    ) -> int:
        """Atomically void the active pick for this event and insert a new one.
        Returns the new pick_id. Both rows persist for audit.
        """
        with self.conn:
            self.conn.execute(
                "UPDATE picks SET voided = 1, voided_reason = ? "
                "WHERE canonical_event_id = ? AND voided = 0",
                (reason, canonical_event_id),
            )
            cur = self.conn.execute(
                """
                INSERT INTO picks (
                    season, canonical_event_id, canonical_player_id,
                    agent_confidence, agent_rationale
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (season, canonical_event_id, canonical_player_id,
                 agent_confidence, agent_rationale),
            )
            return cur.lastrowid

    def resolve_pick(
        self,
        *,
        canonical_event_id: str,
        position: Optional[str],
        score_to_par: Optional[int],
        earnings: Optional[float],
        made_cut: Optional[bool],
        fedex_points: Optional[float] = None,
    ) -> bool:
        """Post-tournament: fill in the result for an existing pick. Returns False if no pick exists."""
        row = self.conn.execute(
            "SELECT pick_id FROM picks WHERE canonical_event_id = ?",
            (canonical_event_id,),
        ).fetchone()
        if not row:
            return False
        self.conn.execute(
            """
            UPDATE picks
            SET position = ?, score_to_par = ?, earnings = ?, made_cut = ?,
                fedex_points = ?, resolved_at = ?
            WHERE pick_id = ?
            """,
            (position, score_to_par, earnings,
             1 if made_cut else (0 if made_cut is False else None),
             fedex_points, datetime.utcnow().isoformat(), row["pick_id"]),
        )
        self.conn.commit()
        return True

    def burned_player_ids(self, season: int) -> set[str]:
        """
        Return canonical IDs of golfers already used (and not voided) this season.
        Voided picks (e.g. WD-restored by league rules) do NOT burn the player.
        """
        return {
            r["canonical_player_id"]
            for r in self.conn.execute(
                "SELECT canonical_player_id FROM picks "
                "WHERE season = ? AND voided = 0",
                (season,),
            )
        }

    def get_pick_for_event(
        self,
        canonical_event_id: str,
        *,
        include_voided: bool = False,
    ) -> Optional[sqlite3.Row]:
        """The active pick for this event. After a replace there can be
        multiple voided rows + one active row; only the active one represents
        the user's current decision. Pass include_voided=True for audit-style
        access (returns the most recent row regardless of voided status)."""
        if include_voided:
            return self.conn.execute(
                "SELECT * FROM picks WHERE canonical_event_id = ? "
                "ORDER BY pick_id DESC LIMIT 1",
                (canonical_event_id,),
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM picks WHERE canonical_event_id = ? AND voided = 0",
            (canonical_event_id,),
        ).fetchone()

    # ── skins ───────────────────────────────────────────────────────────

    def update_skins(
        self,
        *,
        season: int,
        canonical_event_id: str,
        pot_size: float,
        winners_picked_count: int,
    ) -> None:
        """
        Record the skins result for an event. If winners_picked_count == 0,
        mark as rolled over (pot carries forward to next event).
        """
        rolled = 1 if winners_picked_count == 0 else 0
        payout = (pot_size / winners_picked_count) if winners_picked_count > 0 else None
        self.conn.execute(
            """
            INSERT OR REPLACE INTO skins
            (season, canonical_event_id, pot_size, winners_picked_count,
             payout_per_winner, rolled_over, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (season, canonical_event_id, pot_size, winners_picked_count,
             payout, rolled, datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def current_skins_pot(self, season: int, *, weekly_contribution: float) -> float:
        """
        Compute the pot size for the *next* event, given accumulated rollovers.
        weekly_contribution = $ per pool entry per week earmarked for skins
                              × number of pool entries.
        """
        rolled_events = self.conn.execute(
            "SELECT COUNT(*) FROM skins WHERE season = ? AND rolled_over = 1",
            (season,),
        ).fetchone()[0]
        # Each rollover adds another week of contribution to the pot.
        return weekly_contribution * (1 + rolled_events)

    # ── summary / dashboard ─────────────────────────────────────────────

    def season_summary(self, season: int) -> dict:
        row = self.conn.execute(
            """
            SELECT
              COUNT(*) AS picks_made,
              SUM(CASE WHEN earnings IS NOT NULL THEN earnings ELSE 0 END) AS total_earnings,
              SUM(CASE WHEN made_cut = 1 THEN 1 ELSE 0 END) AS cuts_made,
              SUM(CASE WHEN made_cut = 0 THEN 1 ELSE 0 END) AS cuts_missed
            FROM picks WHERE season = ?
            """,
            (season,),
        ).fetchone()
        events_completed = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE season = ? AND status = 'post'",
            (season,),
        ).fetchone()[0]
        return {
            "picks_made": row["picks_made"] or 0,
            "total_earnings": row["total_earnings"] or 0.0,
            "cuts_made": row["cuts_made"] or 0,
            "cuts_missed": row["cuts_missed"] or 0,
            "events_completed": events_completed,
        }

    def majors_picked(self, season: int) -> list[sqlite3.Row]:
        """Picks for major events this season. Includes voided WD picks (so
        the UI can show 'WD' for league-restored picks) but excludes rows
        voided via replacement — those are pure audit."""
        return self.conn.execute(
            """
            SELECT e.name, e.start_date,
                   p.canonical_player_id, p.position, p.score_to_par,
                   p.made_cut, p.earnings, p.voided, p.voided_reason,
                   pl.display_name
            FROM picks p
            JOIN events e ON e.canonical_event_id = p.canonical_event_id
            LEFT JOIN players pl ON pl.canonical_id = p.canonical_player_id
            WHERE p.season = ? AND e.is_major = 1
              AND (p.voided = 0
                   OR COALESCE(p.voided_reason, '') NOT LIKE '%replaced%')
            ORDER BY e.start_date
            """,
            (season,),
        ).fetchall()

    # ── field management ────────────────────────────────────────────────

    def set_event_field(
        self,
        canonical_event_id: str,
        members: list[dict],
        *,
        replace: bool = True,
    ) -> int:
        """
        Replace (or append to) the field for an event.

        Each member dict needs: canonical_player_id, raw_name, espn_athlete_id.
        Members with canonical_player_id=None are skipped — unresolved players
        can't yet be referenced, but show up in the unresolved queue elsewhere.

        Returns count of rows written.
        """
        if replace:
            self.conn.execute(
                "DELETE FROM event_field WHERE canonical_event_id = ?",
                (canonical_event_id,),
            )
        written = 0
        for m in members:
            if not m.get("canonical_player_id"):
                continue
            self.conn.execute(
                """
                INSERT OR REPLACE INTO event_field
                (canonical_event_id, canonical_player_id, raw_name, espn_athlete_id)
                VALUES (?, ?, ?, ?)
                """,
                (canonical_event_id, m["canonical_player_id"],
                 m.get("raw_name"), m.get("espn_athlete_id")),
            )
            written += 1
        self.conn.commit()
        return written

    def event_field(self, canonical_event_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT canonical_player_id, raw_name, espn_athlete_id, withdrawn
            FROM event_field
            WHERE canonical_event_id = ?
            """,
            (canonical_event_id,),
        ).fetchall()

    def is_in_field(self, canonical_event_id: str, canonical_player_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM event_field
            WHERE canonical_event_id = ? AND canonical_player_id = ?
              AND withdrawn = 0
            """,
            (canonical_event_id, canonical_player_id),
        ).fetchone()
        return row is not None

    def mark_withdrawn(self, canonical_event_id: str, canonical_player_id: str) -> bool:
        cur = self.conn.execute(
            """
            UPDATE event_field SET withdrawn = 1
            WHERE canonical_event_id = ? AND canonical_player_id = ?
            """,
            (canonical_event_id, canonical_player_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ── season results (all players, all events) ─────────────────────────

    def upsert_results(
        self,
        canonical_event_id: str,
        season: int,
        results: list[dict],
    ) -> int:
        """
        Write per-player results for an event. Each result dict needs:
          canonical_player_id, position, score_to_par, earnings,
          made_cut (bool), fedex_points (optional).

        Idempotent — re-running just refreshes existing rows. Skips
        rows where canonical_player_id is None (unresolved players).
        """
        written = 0
        for r in results:
            if not r.get("canonical_player_id"):
                continue
            mc = r.get("made_cut")
            mc_int = 1 if mc is True else (0 if mc is False else None)
            self.conn.execute(
                """
                INSERT OR REPLACE INTO season_results
                (canonical_event_id, canonical_player_id, season,
                 position, score_to_par, earnings, made_cut, fedex_points)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (canonical_event_id, r["canonical_player_id"], season,
                 r.get("position"), r.get("score_to_par"), r.get("earnings"),
                 mc_int, r.get("fedex_points")),
            )
            written += 1
        self.conn.commit()
        return written

    def player_season_results(
        self,
        canonical_player_id: str,
        season: int,
    ) -> list[sqlite3.Row]:
        """All resolved results for one player this season, oldest-first."""
        return self.conn.execute(
            """
            SELECT r.*, e.name AS event_name, e.start_date, e.is_major
            FROM season_results r
            JOIN events e ON e.canonical_event_id = r.canonical_event_id
            WHERE r.canonical_player_id = ? AND r.season = ?
            ORDER BY e.start_date ASC
            """,
            (canonical_player_id, season),
        ).fetchall()

    def close(self) -> None:
        self.conn.close()
