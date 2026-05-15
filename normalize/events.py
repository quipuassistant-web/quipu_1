import logging
import re
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical event map
# ---------------------------------------------------------------------------

CANONICAL_EVENTS = {
    "pga_championship_2026": {
        "id": "pga_championship_2026",
        "display_name": "PGA Championship",
        "dates": "May 15-18, 2026",
        "start_date": "2026-05-15",
        "venue": "Quail Hollow Club",
        "location": "Charlotte, NC",
        "purse": 15000000,
        "type": "major",
        "espn_event_id": "401609513",
        "datagolf_id": "pga-championship",
    },
    "memorial_2026": {
        "id": "memorial_2026",
        "display_name": "Memorial Tournament",
        "dates": "May 29 – Jun 1, 2026",
        "start_date": "2026-05-29",
        "venue": "Muirfield Village",
        "location": "Dublin, OH",
        "purse": 12000000,
        "type": "signature",
        "espn_event_id": None,
        "datagolf_id": "memorial",
    },
    "us_open_2026": {
        "id": "us_open_2026",
        "display_name": "U.S. Open",
        "dates": "Jun 5-8, 2026",
        "start_date": "2026-06-05",
        "venue": "Oakmont Country Club",
        "location": "Oakmont, PA",
        "purse": 12000000,
        "type": "major",
        "espn_event_id": None,
        "datagolf_id": "us-open",
    },
    "rbc_canadian_open_2026": {
        "id": "rbc_canadian_open_2026",
        "display_name": "RBC Canadian Open",
        "dates": "Jun 12-15, 2026",
        "start_date": "2026-06-12",
        "venue": "TBD",
        "location": "Canada",
        "purse": 9000000,
        "type": "regular",
        "espn_event_id": None,
        "datagolf_id": "rbc-canadian-open",
    },
    "travelers_2026": {
        "id": "travelers_2026",
        "display_name": "Travelers Championship",
        "dates": "Jun 19-22, 2026",
        "start_date": "2026-06-19",
        "venue": "TPC River Highlands",
        "location": "Cromwell, CT",
        "purse": 8000000,
        "type": "regular",
        "espn_event_id": None,
        "datagolf_id": "travelers",
    },
    "the_open_2026": {
        "id": "the_open_2026",
        "display_name": "The Open Championship",
        "dates": "Jul 16-20, 2026",
        "start_date": "2026-07-16",
        "venue": "Royal Portrush",
        "location": "Portrush, Northern Ireland",
        "purse": 11000000,
        "type": "major",
        "espn_event_id": None,
        "datagolf_id": "the-open",
    },
    # Past events (for ledger parsing)
    "truist_2026": {
        "id": "truist_2026",
        "display_name": "Truist Championship",
        "dates": "May 5-11, 2026",
        "start_date": "2026-05-05",
        "venue": "TPC Craig Woods",
        "location": "Philadelphia, PA",
        "purse": 1900000,
        "type": "signature",
        "espn_event_id": "401609512",
        "datagolf_id": "truist",
    },
    "masters_2026": {
        "id": "masters_2026",
        "display_name": "The Masters",
        "dates": "Apr 10-13, 2026",
        "start_date": "2026-04-10",
        "venue": "Augusta National",
        "location": "Augusta, GA",
        "purse": 15000000,
        "type": "major",
        "espn_event_id": "401609510",
        "datagolf_id": "masters",
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NON_ALPHA_RE = re.compile(r"[^a-z]")


def _event_key(name: str, year: int) -> str:
    """Build a canonical event key from name + year."""
    normalized = name.lower().strip()
    # Remove punctuation, collapse spaces
    normalized = "_".join(normalized.split())
    return f"{normalized}_{year}"


def _parse_date(date_str: str) -> Optional[date]:
    """Parse 'YYYY-MM-DD' string to date object."""
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _date_in_range(target: date, start: date, end: date) -> bool:
    """Check if target date falls within [start, end] inclusive."""
    return start <= target <= end


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

def canonicalize_event(name: str, year: int = 2026) -> str:
    """Normalize event name + year to canonical ID."""
    return _event_key(name, year)


def get_upcoming(n: int = 6) -> list[dict]:
    """Return next n upcoming events sorted by start_date, past events excluded."""
    today = date.today()
    upcoming = []
    for event in CANONICAL_EVENTS.values():
        start = _parse_date(event["start_date"])
        if start is None:
            continue
        if start >= today:
            upcoming.append(event)
    upcoming.sort(key=lambda e: e["start_date"])
    return upcoming[:n]


def get_event_by_date(date_str: str) -> Optional[dict]:
    """Return the event that starts on or contains date_str."""
    target = _parse_date(date_str)
    if target is None:
        return None
    # Find event whose start_date is on or before target
    best = None
    for event in CANONICAL_EVENTS.values():
        start = _parse_date(event["start_date"])
        if start is None:
            continue
        if start <= target:
            if best is None or start > _parse_date(best["start_date"]):
                best = event
    return best


def get_current_event() -> Optional[dict]:
    """Return the currently active tournament (if any)."""
    today = date.today()
    current = None
    for event in CANONICAL_EVENTS.values():
        start = _parse_date(event["start_date"])
        if start is None:
            continue
        # Approximate 7-day event window
        from datetime import timedelta
        end = start + timedelta(days=7)
        if start <= today <= end:
            current = event
            break
    return current