"""
espn.py — fetcher for ESPN's unofficial PGA golf endpoints.

ESPN exposes a handful of JSON endpoints that aren't officially documented but
have been stable for years. We use:

  1. /apis/site/v2/sports/golf/pga/scoreboard
     Current week's tournament + leaderboard. Add `?dates=YYYYMMDD-YYYYMMDD`
     for historical results.

  2. /apis/common/v3/sports/golf/athletes/{athlete_id}/stats?season=YYYY
     Single player's season stats.

  3. /sports/golf/leagues/pga/seasons/{year}/athletes/{id}/eventlog
     Per-player event log — every start and finish.

Design notes:
  - All network access goes through one `_get()` helper with caching, retry,
    and a single User-Agent string. Easy to swap for httpx/async later.
  - Parsers are *defensive*: ESPN reshuffles fields between event states
    (scheduled / in-progress / completed) and occasionally introduces new
    statuses. We log unexpected shapes rather than crashing.
  - Player names go through the crosswalk; unresolved players still get
    returned (with `canonical_id=None`) so callers can decide whether to
    skip or queue them.
  - Returns dataclass records. JSON in, typed Python out.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from normalize.players import PlayerCrosswalk

logger = logging.getLogger("espn_fetcher")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

BASE_SITE = "https://site.api.espn.com/apis/site/v2/sports/golf"
BASE_COMMON = "https://site.web.api.espn.com/apis/common/v3/sports/golf"
BASE_CORE = "https://sports.core.api.espn.com/v2/sports/golf"

# Identify ourselves honestly. ESPN's endpoints don't require auth but we are
# a polite client, not a stealth scraper.
USER_AGENT = "golf-pick-agent/0.1 (personal fantasy use; contact via owner)"

# Cache TTLs (seconds)
TTL_LIVE = 5 * 60          # 5 min during active play
TTL_RECENT = 4 * 3600      # 4 hours for the current week, no live play
TTL_HISTORICAL = 30 * 86400  # 30 days for completed events
TTL_PLAYER_STATS = 24 * 3600


# ─────────────────────────────────────────────────────────────────────────────
# Cache (minimal SQLite-backed key-value with TTL)
# ─────────────────────────────────────────────────────────────────────────────

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key         TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    body        TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    http_status INTEGER NOT NULL
);
"""


class HTTPCache:
    """Tiny TTL cache. Real impl would be shared across fetchers in cache/cache.py."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(_CACHE_SCHEMA)
        self.conn.commit()

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    def get(self, url: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT body, fetched_at, ttl_seconds FROM http_cache WHERE key = ?",
            (self._key(url),),
        ).fetchone()
        if not row:
            return None
        body, fetched_at, ttl = row
        if time.time() - fetched_at > ttl:
            return None
        return json.loads(body)

    def put(self, url: str, body: dict, ttl: int, http_status: int = 200) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO http_cache (key, url, body, fetched_at, ttl_seconds, http_status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (self._key(url), url, json.dumps(body), time.time(), ttl, http_status),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Records
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TournamentRecord:
    """A tournament/event from the scoreboard."""
    espn_event_id: str
    name: str
    short_name: Optional[str]
    start_date: str           # ISO 8601
    end_date: Optional[str]
    status: str               # 'scheduled' | 'in' | 'post' | 'unknown'
    is_completed: bool
    venue_name: Optional[str]
    venue_city: Optional[str]
    course_name: Optional[str]
    purse: Optional[float]
    leaderboard: list["LeaderboardRow"] = field(default_factory=list)
    raw_status_type: Optional[str] = None  # for debugging unexpected states


@dataclass
class LeaderboardRow:
    """One player's line on a tournament leaderboard."""
    espn_athlete_id: Optional[str]
    canonical_id: Optional[str]   # populated via crosswalk
    raw_name: str
    position: Optional[str]       # '1', 'T5', 'CUT', 'WD', 'DQ'
    score_to_par: Optional[int]   # under par negative, e.g. -12
    total_strokes: Optional[int]
    made_cut: Optional[bool]
    earnings: Optional[float]
    fedex_points: Optional[float]
    crosswalk_method: str         # how we resolved the canonical_id
    crosswalk_confidence: float


@dataclass
class PlayerEventLogEntry:
    """One event from a player's season eventlog."""
    season: int
    espn_event_id: str
    event_name: Optional[str]
    start_date: Optional[str]
    position: Optional[str]
    score_to_par: Optional[int]
    earnings: Optional[float]
    made_cut: Optional[bool]


# ─────────────────────────────────────────────────────────────────────────────
# HTTP layer
# ─────────────────────────────────────────────────────────────────────────────

class ESPNError(Exception):
    pass


class ESPNFetcher:
    """
    ESPN golf API client.

    Construct once per pipeline run, pass to whatever needs it.

        cache = HTTPCache("data/golf.db")
        xwalk = PlayerCrosswalk("data/golf.db")
        espn = ESPNFetcher(cache, xwalk)

        current = espn.current_tournament()
        for row in current.leaderboard:
            ...
    """

    def __init__(
        self,
        cache: HTTPCache,
        crosswalk: PlayerCrosswalk,
        *,
        max_retries: int = 3,
        retry_backoff: float = 1.5,
        request_timeout: float = 15.0,
    ):
        self.cache = cache
        self.xwalk = crosswalk
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.request_timeout = request_timeout

    # ── HTTP primitive ───────────────────────────────────────────────────

    def _get(self, url: str, *, ttl: int) -> dict:
        cached = self.cache.get(url)
        if cached is not None:
            logger.debug("cache hit: %s", url)
            return cached

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with urlopen(req, timeout=self.request_timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    body = json.loads(raw)
                self.cache.put(url, body, ttl)
                return body
            except HTTPError as e:
                last_err = e
                # 404 isn't worth retrying
                if e.code == 404:
                    raise ESPNError(f"404 from {url}") from e
                # 429/5xx: backoff and retry
                if e.code in (429, 500, 502, 503, 504):
                    sleep_s = self.retry_backoff ** attempt
                    logger.warning("HTTP %s from %s, retry in %.1fs", e.code, url, sleep_s)
                    time.sleep(sleep_s)
                    continue
                raise ESPNError(f"HTTP {e.code} from {url}") from e
            except (URLError, json.JSONDecodeError, TimeoutError) as e:
                last_err = e
                sleep_s = self.retry_backoff ** attempt
                logger.warning("transient error from %s (%s), retry in %.1fs", url, e, sleep_s)
                time.sleep(sleep_s)

        raise ESPNError(f"exhausted retries for {url}") from last_err

    # ── Defensive helpers ────────────────────────────────────────────────

    @staticmethod
    def _safe_get(obj: Any, *path: str, default: Any = None) -> Any:
        """Walk a nested dict/list path, returning default if anything's missing."""
        cur = obj
        for key in path:
            if cur is None:
                return default
            if isinstance(cur, dict):
                cur = cur.get(key)
            elif isinstance(cur, list):
                try:
                    cur = cur[int(key)]
                except (ValueError, IndexError):
                    return default
            else:
                return default
        return cur if cur is not None else default

    @staticmethod
    def _parse_int(v: Any) -> Optional[int]:
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            try:
                return int(float(v))
            except (ValueError, TypeError):
                return None

    @staticmethod
    def _parse_float(v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            # ESPN sometimes returns "$1,234,567" — strip and try again
            cleaned = v.replace("$", "").replace(",", "").strip()
            if cleaned == "":
                return None
            try:
                return float(cleaned)
            except ValueError:
                return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_score_to_par(s: Any) -> Optional[int]:
        """ESPN gives strings like 'E', '-12', '+3'. Returns 0 for E."""
        if s is None:
            return None
        if isinstance(s, (int, float)):
            return int(s)
        s = str(s).strip()
        if s in ("E", "e", "0", ""):
            return 0 if s in ("E", "e", "0") else None
        # Already signed
        try:
            return int(s)
        except ValueError:
            return None

    # ── Public: scoreboard / tournaments ─────────────────────────────────

    def current_tournament(self) -> Optional[TournamentRecord]:
        """Returns this week's tournament with leaderboard, or None if none scheduled."""
        url = f"{BASE_SITE}/pga/scoreboard"
        data = self._get(url, ttl=TTL_LIVE)
        events = data.get("events", []) or []
        if not events:
            return None
        return self._parse_event(events[0])

    def tournaments_in_range(self, start: datetime, end: datetime) -> list[TournamentRecord]:
        """Historical tournaments. Pass UTC datetimes."""
        date_range = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
        url = f"{BASE_SITE}/pga/scoreboard?dates={date_range}"
        data = self._get(url, ttl=TTL_HISTORICAL)
        events = data.get("events", []) or []
        return [self._parse_event(e) for e in events]

    def event_by_id(self, espn_event_id: str) -> Optional[TournamentRecord]:
        """
        Fetch a single event by its ESPN ID via the summary endpoint.
        More reliable than the scoreboard's date-range filter for historical
        events, which sometimes omits the competitors array.
        """
        url = f"{BASE_SITE}/pga/summary?event={espn_event_id}"
        try:
            data = self._get(url, ttl=TTL_HISTORICAL)
        except ESPNError as e:
            logger.warning("summary fetch failed for %s: %s", espn_event_id, e)
            return None
        # The summary endpoint wraps the event differently — `header` has the
        # event metadata and `competitions[0].competitors` has the leaderboard.
        header = data.get("header") or {}
        comps = header.get("competitions") or []
        if not comps:
            logger.warning("summary for %s has no competitions", espn_event_id)
            return None
        # Reshape into the same structure scoreboard returns, then reuse _parse_event
        event_shaped = {
            "id": header.get("id") or espn_event_id,
            "name": header.get("name") or "",
            "shortName": header.get("shortName"),
            "date": comps[0].get("date") or "",
            "endDate": comps[0].get("endDate"),
            "status": comps[0].get("status") or {},
            "competitions": comps,
        }
        return self._parse_event(event_shaped)

    def _parse_event(self, event: dict) -> TournamentRecord:
        status_obj = self._safe_get(event, "status", "type") or {}
        raw_status = status_obj.get("name") or status_obj.get("state") or "unknown"
        # ESPN uses 'STATUS_SCHEDULED', 'STATUS_IN_PROGRESS', 'STATUS_FINAL', etc.
        # Normalize to scheduled/in/post.
        status_map = {
            "STATUS_SCHEDULED": "scheduled",
            "STATUS_IN_PROGRESS": "in",
            "STATUS_FINAL": "post",
            "STATUS_PLAY_COMPLETE": "post",
            "STATUS_END_PERIOD": "in",
        }
        status = status_map.get(raw_status, raw_status.lower())
        if status not in ("scheduled", "in", "post"):
            logger.info("unexpected status type '%s' on event %s — treating as %s",
                        raw_status, event.get("id"), status)

        comp = self._safe_get(event, "competitions", "0") or {}
        venue = comp.get("venue") or {}
        course_name = self._safe_get(comp, "course", "name") or self._safe_get(comp, "courses", "0", "name")
        purse = self._parse_float(comp.get("purse"))

        record = TournamentRecord(
            espn_event_id=str(event.get("id", "")),
            name=event.get("name", ""),
            short_name=event.get("shortName"),
            start_date=event.get("date", ""),
            end_date=event.get("endDate"),
            status=status,
            is_completed=(status == "post"),
            venue_name=venue.get("fullName"),
            venue_city=self._safe_get(venue, "address", "city"),
            course_name=course_name,
            purse=purse,
            raw_status_type=raw_status,
        )

        record.leaderboard = self._parse_leaderboard(comp.get("competitors") or [])
        return record

    def _parse_leaderboard(self, competitors: list[dict]) -> list[LeaderboardRow]:
        rows: list[LeaderboardRow] = []
        for c in competitors:
            athlete = c.get("athlete") or {}
            espn_id = str(athlete.get("id", "")) or None
            raw_name = athlete.get("displayName") or athlete.get("fullName") or ""
            if not raw_name:
                logger.warning("competitor without name: keys=%s", list(c.keys()))
                continue

            # Resolve through crosswalk
            xw_result = self.xwalk.resolve(raw_name, source="espn", source_id=espn_id)

            # Score: ESPN typically nests under linescores or has top-level fields.
            # Try several locations.
            score_to_par = self._parse_score_to_par(c.get("score"))
            if score_to_par is None:
                # Sometimes hidden under statistics
                for stat in c.get("statistics") or []:
                    if stat.get("name") in ("scoreToPar", "toPar"):
                        score_to_par = self._parse_score_to_par(stat.get("displayValue"))
                        break

            total_strokes = self._parse_int(c.get("totalStrokes"))
            position_obj = c.get("status", {}).get("position") or {}
            position = position_obj.get("displayName") or position_obj.get("id")
            if position is None:
                position = self._safe_get(c, "status", "displayValue")

            # Cut / WD / DQ flags
            type_name = self._safe_get(c, "status", "type", "name") or ""
            made_cut: Optional[bool] = None
            if "CUT" in type_name.upper() or (isinstance(position, str) and position.upper() == "CUT"):
                made_cut = False
            elif "WITHDRAW" in type_name.upper() or position == "WD":
                made_cut = None
            elif position and position not in ("CUT", "WD", "DQ"):
                made_cut = True

            earnings = self._parse_float(c.get("earnings"))
            if earnings is None:
                for stat in c.get("statistics") or []:
                    if stat.get("name") in ("earnings", "money"):
                        earnings = self._parse_float(stat.get("displayValue"))
                        break

            fedex_points = None
            for stat in c.get("statistics") or []:
                if "fedex" in (stat.get("name") or "").lower():
                    fedex_points = self._parse_float(stat.get("displayValue"))
                    break

            rows.append(LeaderboardRow(
                espn_athlete_id=espn_id,
                canonical_id=xw_result.canonical_id,
                raw_name=raw_name,
                position=str(position) if position else None,
                score_to_par=score_to_par,
                total_strokes=total_strokes,
                made_cut=made_cut,
                earnings=earnings,
                fedex_points=fedex_points,
                crosswalk_method=xw_result.method,
                crosswalk_confidence=xw_result.confidence,
            ))
        return rows

    # ── Public: player eventlog ──────────────────────────────────────────

    def player_eventlog(self, espn_athlete_id: str, season: int) -> list[PlayerEventLogEntry]:
        url = (f"{BASE_CORE}/leagues/pga/seasons/{season}/athletes/"
               f"{espn_athlete_id}/eventlog?lang=en&region=us")
        try:
            data = self._get(url, ttl=TTL_PLAYER_STATS)
        except ESPNError as e:
            logger.warning("eventlog fetch failed for %s/%s: %s", espn_athlete_id, season, e)
            return []

        entries: list[PlayerEventLogEntry] = []
        # ESPN eventlog typically has events.items as list of {event: $ref, ...}
        items = self._safe_get(data, "events", "items") or []
        for item in items:
            # Each item may have a $ref needing another fetch, or inline fields.
            # In practice the eventlog gives us minimal data inline; deeper detail
            # requires following refs. For now, capture what's there.
            event_ref = item.get("event")
            event_id = None
            event_name = None
            start_date = None
            if isinstance(event_ref, dict):
                # Inline event
                event_id = str(event_ref.get("id", "")) or None
                event_name = event_ref.get("name")
                start_date = event_ref.get("date")
            elif isinstance(event_ref, str):
                # $ref URL — extract ID from path
                event_id = event_ref.rstrip("/").split("/")[-1].split("?")[0]

            entries.append(PlayerEventLogEntry(
                season=season,
                espn_event_id=event_id or "",
                event_name=event_name,
                start_date=start_date,
                position=self._safe_get(item, "competitor", "position"),
                score_to_par=self._parse_score_to_par(self._safe_get(item, "competitor", "score")),
                earnings=self._parse_float(self._safe_get(item, "competitor", "earnings")),
                made_cut=None,  # not always present; derive from position if needed
            ))
        return entries


# ─────────────────────────────────────────────────────────────────────────────
# Self-test (mock-driven — no network required)
# ─────────────────────────────────────────────────────────────────────────────

def _make_fake_scoreboard() -> dict:
    """Approximates ESPN's actual response shape for testing parsers offline."""
    return {
        "events": [{
            "id": "401580351",
            "name": "Sony Open in Hawaii",
            "shortName": "Sony Open",
            "date": "2026-01-15T19:00Z",
            "endDate": "2026-01-18T23:00Z",
            "status": {"type": {"name": "STATUS_FINAL", "completed": True}},
            "competitions": [{
                "venue": {"fullName": "Waialae Country Club",
                          "address": {"city": "Honolulu", "state": "HI"}},
                "course": {"name": "Waialae Country Club"},
                "purse": 8700000,
                "competitors": [
                    {
                        "athlete": {"id": "9478", "displayName": "Scottie Scheffler"},
                        "score": "-23",
                        "totalStrokes": 257,
                        "earnings": 1566000,
                        "status": {
                            "position": {"id": "1", "displayName": "1"},
                            "type": {"name": "STATUS_FINAL"},
                        },
                        "statistics": [
                            {"name": "fedexCupPoints", "displayValue": "500"},
                        ],
                    },
                    {
                        "athlete": {"id": "11119", "displayName": "Cam Young"},
                        "score": "-19",
                        "totalStrokes": 261,
                        "earnings": "$948,800",  # exercise the string parser
                        "status": {
                            "position": {"id": "2", "displayName": "T2"},
                            "type": {"name": "STATUS_FINAL"},
                        },
                    },
                    {
                        "athlete": {"id": "99999", "displayName": "José María Olazábal"},
                        "score": "+8",
                        "earnings": 0,
                        "status": {
                            "position": {"displayName": "CUT"},
                            "type": {"name": "STATUS_CUT"},
                        },
                    },
                    {
                        "athlete": {"id": "88888", "displayName": "Joohyung Kim"},
                        "score": "E",
                        "earnings": 25000,
                        "status": {
                            "position": {"displayName": "T45"},
                            "type": {"name": "STATUS_FINAL"},
                        },
                    },
                ],
            }],
        }],
    }


def _self_test() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    cache = HTTPCache(tmp / "cache.db")
    xwalk = PlayerCrosswalk(tmp / "xwalk.db")

    # Seed crosswalk so resolution works
    xwalk.create_player("Scottie Scheffler", country="USA", birth_year=1996)
    xwalk.create_player("Cameron Young", country="USA", birth_year=1997)
    xwalk.create_player("José María Olazábal", country="ESP", birth_year=1966)
    # Don't seed Joohyung Kim — exercise the unresolved queue path

    espn = ESPNFetcher(cache, xwalk)

    # Inject the fake response straight into cache to bypass network
    url = f"{BASE_SITE}/pga/scoreboard"
    cache.put(url, _make_fake_scoreboard(), ttl=TTL_LIVE)

    tournament = espn.current_tournament()
    assert tournament is not None
    print(f"Tournament: {tournament.name}")
    print(f"  ESPN id:    {tournament.espn_event_id}")
    print(f"  Status:     {tournament.status} (raw: {tournament.raw_status_type})")
    print(f"  Completed:  {tournament.is_completed}")
    print(f"  Venue:      {tournament.venue_name} ({tournament.venue_city})")
    print(f"  Course:     {tournament.course_name}")
    print(f"  Purse:      ${tournament.purse:,.0f}" if tournament.purse else "  Purse:  ?")
    print()
    print(f"  Leaderboard ({len(tournament.leaderboard)} rows):")
    print(f"  {'Pos':<5} {'Player':<28} {'ToPar':<6} {'Earnings':<12} {'Cut?':<6} {'Resolve':<14} {'Conf'}")
    print(f"  {'─'*5} {'─'*28} {'─'*6} {'─'*12} {'─'*6} {'─'*14} {'─'*5}")
    for row in tournament.leaderboard:
        earnings_str = f"${row.earnings:,.0f}" if row.earnings is not None else "?"
        cut_str = {True: "made", False: "cut", None: "—"}[row.made_cut]
        topar_str = f"{row.score_to_par:+d}" if row.score_to_par is not None else "?"
        if topar_str == "+0":
            topar_str = "E"
        print(f"  {row.position or '?':<5} {row.raw_name:<28} {topar_str:<6} "
              f"{earnings_str:<12} {cut_str:<6} {row.crosswalk_method:<14} {row.crosswalk_confidence:.3f}")

    print()
    print("Crosswalk stats:", xwalk.stats())
    print()

    # Sanity assertions
    assert tournament.is_completed
    assert tournament.purse == 8700000
    scheffler_row = next(r for r in tournament.leaderboard if "Scheffler" in r.raw_name)
    assert scheffler_row.canonical_id is not None, "Scheffler should resolve"
    assert scheffler_row.earnings == 1566000
    assert scheffler_row.score_to_par == -23
    assert scheffler_row.made_cut is True

    cam_row = next(r for r in tournament.leaderboard if "Young" in r.raw_name)
    assert cam_row.earnings == 948800, f"Expected 948800, got {cam_row.earnings}"
    assert cam_row.canonical_id is not None, "'Cam Young' should resolve to Cameron Young"

    olaz_row = next(r for r in tournament.leaderboard if "Olaz" in r.raw_name)
    assert olaz_row.made_cut is False, "Olazábal was CUT"

    kim_row = next(r for r in tournament.leaderboard if "Kim" in r.raw_name)
    assert kim_row.canonical_id is None, "Joohyung Kim should be queued (not seeded)"
    assert kim_row.crosswalk_method == "queued"
    assert kim_row.score_to_par == 0, "'E' should parse to 0"

    print("All assertions passed.")
    cache.close()
    xwalk.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _self_test()
