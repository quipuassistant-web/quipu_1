"""
Per-event metadata (lat/lon, course type, grass, yardage, major/playoff flags).

Adapted from gmalbert/golf-predictions/data_files/course_metadata.json. Keyed
by lower-cased event name with aliases. Used to:

  - Detect majors / playoff events without relying on ESPN name substring
    matching (which was flagged as fragile in CLAUDE.md).
  - Provide venue lat/lon to the weather fetcher (fetchers/weather.py).
  - Surface course attributes (grass, yardage, course_type) for downstream
    fit features.

If you add new events, drop them into data/course_metadata.json directly —
this module just reads.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional


_METADATA_PATH = Path(__file__).parent.parent / "data" / "course_metadata.json"


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    """Load + cache. Strips `_comment` / `_schema` housekeeping keys."""
    if not _METADATA_PATH.exists():
        return {}
    raw = json.loads(_METADATA_PATH.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


_PUNCT_RE = re.compile(r"[^\w\s]")


def _normalize_event_name(name: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace. Mirrors how the
    metadata file keys are normalized — 'AT&T Pebble Beach Pro-Am' →
    'att pebble beach proam'."""
    s = (name or "").lower()
    s = s.replace("&", "and")
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(s.split())


def lookup(event_name: str) -> Optional[dict]:
    """Find metadata for an event name. Exact match first (lower-cased),
    then normalized match, then a single-substring fallback for variants
    like 'THE CJ CUP Byron Nelson' vs 'the cj cup byron nelson'."""
    if not event_name:
        return None
    data = _load()
    lower = event_name.lower()
    if lower in data:
        return data[lower]
    # Punctuation-stripped match against the same-normalized key set
    norm = _normalize_event_name(event_name)
    for k, v in data.items():
        if _normalize_event_name(k) == norm:
            return v
    # Single-hit substring fallback (only return if unambiguous)
    candidates = [v for k, v in data.items() if k in lower or lower in k]
    if len(candidates) == 1:
        return candidates[0]
    return None


def is_major(event_name: str) -> bool:
    m = lookup(event_name)
    return bool(m and m.get("is_major"))


def is_playoff(event_name: str) -> bool:
    m = lookup(event_name)
    return bool(m and m.get("is_playoff"))


def venue_latlon(event_name: str) -> Optional[tuple[float, float]]:
    m = lookup(event_name)
    if not m or "lat" not in m or "lon" not in m:
        return None
    return float(m["lat"]), float(m["lon"])
