# quipu_1 — agent notes

PGA Tour pick-one-and-done optimizer for Chad's 12-entry pool. This file is for you (the agent), not for end users. Read the README for the user-facing pitch; read this for the wiring, the rough edges, and the design decisions that aren't obvious from the code.

## Run model

- Python 3.10+. `requirements.txt` lists only `Flask`, `requests`, `beautifulsoup4`. The crosswalk uses `rapidfuzz` if installed and falls back to a Jaccard token-set if not — install `rapidfuzz` for real use.
- Run from the repo root. Internal imports are bare (`from normalize.players import ...`, `from ledger.ledger import ...`, `from fetchers.espn import ...`), so `cwd == /Users/chad/Projects/quipu_1` and that path must be on `sys.path`. There is a top-level `__init__.py` but the package is NOT pip-installable — bare imports won't work as `quipu_1.normalize.players`.
- Default SQLite DB: `data/golf.db` (relative — created on first run). All tables (`players`, `player_aliases`, `player_source_ids`, `players_unresolved`, `events`, `picks`, `skins`, `event_field`, `season_results`, `event_odds`, `http_cache`) live in this single file by design.
- ESPN, DataGolf, and the sportsbook scrapers all need a real network — the sandbox can't reach them. Backfills and live pipeline runs go on the user's machine.

## Layout (actual, vs. the README's aspirational tree)

```
quipu_1/                       <- repo root; cwd for every script
├── normalize/                 player crosswalk + canonical events
│   ├── players.py             PlayerCrosswalk (sqlite-backed) + compat shim
│   ├── seed.py                seed from OWGR_TOP_200 + SUPPLEMENTAL
│   ├── seed_owgr.py           static OWGR top-200 list w/ ESPN ids
│   ├── events.py              hardcoded CANONICAL_EVENTS dict (2026 schedule)
│   └── join.py
├── fetchers/
│   ├── espn.py                primary live source — scoreboard, eventlog
│   ├── datagolf.py            DISABLED (ToS); needs paid API key to revive
│   ├── odds.py                DISABLED (anti-bot); use add_odds.py paste-in
│   ├── weather.py
│   └── normalize/players.db   STRAY DB — compat shim writes here (see bug #4)
├── ledger/ledger.py           Ledger class — picks, events, skins, field, results
├── scoring/
│   ├── inputs.py              build_event_inputs — hydrates PlayerInputs from ledger
│   ├── multi_objective.py     score_field — probability cascade + composite
│   ├── skins_ev.py            skins EV math + re-exported odds helpers
│   ├── allocation.py          multi-event allocation planner
│   └── *.bak4 / *.bak5 / *.bak6   stale, ignored by .gitignore but on disk
├── pipeline/
│   ├── monday_open.py         resolve field, persist event_field, surface unresolved
│   ├── thursday_lock.py       final rec print (has bugs — see #2)
│   └── sunday_close.py        resolve picks, update skins pot, write results
├── cache/cache.py             SQLite TTL cache (Cache class + module-level helpers)
├── backfill.py                week-by-week ESPN walk → season_results
├── multi_season_backfill.py   wrapper around backfill_range for 3-4 seasons
├── course_history.py          per-player per-venue shrinkage estimator
├── add_odds.py                paste-in Vegas odds → event_odds table
├── import_picks.py            bootstrap ledger from CHAD_PICKS_2026 (hardcoded)
├── record_pick.py             Thursday CLI: lookup → confirm → insert into picks
├── golf_dashboard.py          Flask UI on :7071 (broken — see #1, #2)
└── test_*.py                  smoke tests, not pytest-discovered
```

## Where the source of truth lives

- **Who has been picked**: `picks` table. The partial unique index `uq_picks_player_season_active` enforces one-and-done only for `voided = 0` rows. A WD-restored player can be repicked because their voided row doesn't trip the constraint.
- **What the field is**: `event_field` table, written by `monday_open.run_monday_open`. Anything not in `event_field` is invisible to the scorer.
- **Per-player season form**: `season_results`, written by `sunday_close` and `backfill`. `scoring/inputs._hydrate_season_form` reads from here. Mid-season starts need `backfill.py` first or the scorer falls through to the flat prior.
- **Vegas odds**: `event_odds` (de-vigged at insert time by `add_odds.py`). Scorer auto-loads via `load_odds_for_event` if caller doesn't pass `vegas_odds=` explicitly.
- **Skins pot state**: `skins` table. `Ledger.current_skins_pot` computes `weekly_contribution × (1 + rolled_events)`.

## Design decisions worth keeping in mind

### Crosswalk

- **Append-only.** `players.py` never auto-merges or auto-deletes canonical records. Wrong merges propagate silently for weeks; manual review of the unresolved queue is the safety valve.
- **Resolve order**: source_id → exact normalized alias → fuzzy ≥ 0.95 with ≥ 0.05 gap to runner-up → queue. `AUTO_RESOLVE_MIN = 0.95`, `QUEUE_MIN = 0.70`. The 0.05 gap rule is what prevents Cameron Young / Cameron Smith from collapsing.
- **Name normalization is deliberately lossy in helpful ways**: diacritics stripped via NFKD + explicit substitutions for atomic codepoints (`ø→o`, `æ→ae`, `ß→ss`, `ł→l`, etc. — NFKD doesn't decompose those), `Last, First` flipped, initials separated, generation suffixes (`Jr`/`III`) stripped.
- **Nickname expansion is comparison-only.** Never store nickname-expanded aliases — keep "Cam" and "Cameron" distinct, or the system loses its ability to detect their collision later. The boost to 0.97 when expanded forms match AND last names match is what makes "Cam Young" resolve to "Cameron Young" without lowering the floor for other fuzzies.
- **Birth year is the disambiguator of last resort** (Tom Kim vs Si Woo Kim, Charlie vs Tiger). Stored on the canonical row; not yet used in `similarity()` — TODO for harder ambiguous cases.

### Scoring

- **Probability cascade** in `_base_finish_probs`: DataGolf finish distribution → Vegas implied (de-vigged) → ESPN season form (only if `season_starts ≥ 3`) → flat prior (1/field_size). Each step records a `source` + `confidence` so the composite gets discounted appropriately.
- **Venue lift is multiplicative on top of the base**, applied to win/top5/top10/top20 in tandem (correlated). Cut prob gets a softer adjustment (cut-making is less venue-dependent). Lift is capped at [0.5, 2.0] and scaled by evidence: 0 starts → no adjustment, 5+ starts → full lift, linear in between.
- **Composite is normalized within field, then discounted by source confidence.** A flat-prior player can't out-score a Vegas-anchored player by virtue of normalization alone — `composite *= probs.confidence` is doing the work.
- **Majors weight redistributes to season earnings when `can_win_majors_bucket = False`** (you joined mid-season and missed a major; the majors bucket is unreachable so its weight goes back into the pile).
- **Skins EV scales co-pickers by relative chalk** (`probs.win / avg_win`, capped 5x). Chalk plays share the pot more than longshots do. Coarse — a better model would weight by Vegas-implied directly.
- **Course history shrinkage**: `SHRINKAGE_ALPHA = 4` pseudo-starts of baseline blended before trusting the player's venue rate. Baselines: top-10 = 12%, top-20 = 20%, cut = 70% (weighted across field sizes; PGA Tour pools span 70–156).

### Pipelines

- Three discrete scripts, one per day, no daemon. **monday_open** is the only one that mutates `event_field`. **thursday_lock** is read-only — it scores and prints, doesn't write to `picks` (that's `record_pick.py`). **sunday_close** is idempotent — safe to re-run.
- ESPN is the primary live source. DataGolf and sportsbook scrapers are intentionally disabled (see "Known bugs", #5/#6). The paste-in `add_odds.py` flow exists because direct scraping was too brittle/ToS-iffy.
- HTTP cache TTLs are tuned per state: 5 min during live play, 4 h during the current week with no play, 30 d for completed events. `backfill.py` against a cold cache is 5–15 minutes for 4 seasons; re-runs are cheap.

### Ledger

- **One DB by design** — `data/golf.db` holds the crosswalk, the ledger, the HTTP cache, and the odds table. Backups are simpler; cross-table joins are SQL-local. The compat shim in `players.py` violates this (see bug #4).
- **Voided picks are kept, not deleted.** The audit trail matters and the partial unique index handles the constraint logic. Use `Ledger.void_pick()` after the fact, or `record_pick(voided=True)` if you knew at pick time.

## Known bugs / inconsistencies

These are real, present in current `main` (`a00c94d`). Not all are blockers — but don't assume the code on disk works end-to-end.

1. **`golf_dashboard.py` imports a stale package path.** Top of file inserts `~/AI_HOME/TOOLS` on `sys.path` and imports `from golf_agent.normalize.players import ...`. There is no `golf_agent/` directory in this repo. Every data function in the dashboard will `ModuleNotFoundError` at import time. The dashboard will only render with stubbed/empty data (the `try/except Exception: return [...]` fallbacks).

2. **Dashboard + `thursday_lock.py` call functions that don't exist:**
   - `scoring.multi_objective.get_recommendations_for_event` — the real entry point is `score_field(event_inputs, weights)`. No wrapper exists.
   - `ledger.ledger.get_season_summary` — it's `Ledger.season_summary(season)` (instance method).
   - `ledger.ledger.get_major_tracker` — doesn't exist at all.
   - `fetchers.espn.get_current_tournament` — it's `ESPNFetcher.current_tournament()` (instance method).
   Wiring the dashboard requires writing these adapter functions or rewriting the dashboard to instantiate `Ledger` / `ESPNFetcher` / `build_event_inputs` directly.

3. **`normalize/players.py` compat shim queries columns that don't exist.** `get_all_available()` and `by_espn_id()` `SELECT * FROM players` then read `r["owgr"]`, `r["tier"]`, `r["burned"]`. The `players` table schema only has `canonical_id, display_name, normalized, country, birth_year, is_active, created_at, notes`. These functions raise `IndexError` / `KeyError` the first time they're called. Burned-state should come from `picks` (via `Ledger.burned_player_ids`), not from a column on `players`.

4. **Compat shim hardcodes a different DB path from everything else.** `_get_crosswalk()` opens `~/AI_HOME/TOOLS/golf_agent/normalize/players.db`, but every pipeline opens `data/golf.db`. The stray file at `fetchers/normalize/players.db` (committed via LFS or just sitting on disk) is the symptom. Anything that goes through the shim sees a different world. Fix: make the shim accept an explicit path and route through the same DB as the rest of the system, or remove the shim and update callers.

5. **Sportsbook odds scrapers are deliberately disabled.** `fetchers/__init__.py` comments out `get_draftkings_odds` and friends; `odds.py` is still on disk and importable but flagged as broken (anti-bot triggers). The dashboard's direct `from golf_agent.fetchers.odds import get_draftkings_odds` is doubly broken (wrong path AND disabled module). Use `add_odds.py` paste-in for fresh lines.

6. **DataGolf scraper is deliberately disabled.** Same `__init__.py` block. `datagolf.py` scrapes the consumer site, which is a ToS violation. To wire it up properly, subscribe to `feeds.datagolf.com` (~$270/yr) and rewrite to use the API key — the scorer already has a slot for `datagolf_finish_distribution` in `PlayerInputs`.

7. **README says Streamlit; it's actually Flask.** Line 65 of README claims `golf_dashboard.py # Streamlit web UI`. It's a Flask app on port 7071 (`golf_dashboard.py:469`).

8. **Two ID schemes for events that never align.** `normalize/events.py` keys events by string slugs (`"pga_championship_2026"`). The ledger uses `e_<espn_event_id>` (`"e_401609513"`). The dashboard does `event_id = upcoming[0]["id"]` (slug form) and passes it to a function that expects ledger canonical ids. No translator exists.

9. **`record_pick._find_upcoming_event` orders by `start_date DESC LIMIT 1`.** That picks the *latest*-dated scheduled event, not the nearest upcoming. Mid-season, if multiple events are scheduled in the ledger, this picks wrong. Want `ORDER BY start_date ASC` plus `WHERE start_date >= date('now')`.

10. **Stale backup files in `scoring/`.** `allocation.py.bak4`, `inputs.py.{bak5,bak6}`, `multi_objective.py.{bak4,bak6}`. Ignored by `.gitignore` but still on disk — confusing when grepping. Safe to delete.

11. **`CANONICAL_EVENTS["truist_2026"]` has venue `"TPC Craig Woods"`.** Truist 2026 was at Philadelphia Cricket Club (Wissahickon). That entry was hand-typed and is wrong.

12. **`Ledger.MAJOR_NAME_FRAGMENTS` matches by substring.** "u.s. open" matches "The U.S. Open" but also any future event containing those words. Brittle if ESPN renames anything. Major flag is also computed only at insert — re-running `upsert_event` keeps it.

## Things to NOT do

- Don't auto-resolve crosswalk entries below the 0.95 + gap thresholds. A wrong merge means a player gets double-burned silently. The unresolved queue is the deliberate friction.
- Don't write nickname-expanded names into `player_aliases`. Comparison-time only.
- Don't add new event IDs to `CANONICAL_EVENTS` and assume the scorer will use them — the scorer reads from the `events` table (ledger), not from `CANONICAL_EVENTS`. That dict is for the dashboard's "upcoming" view and for `import_picks.py` fuzzy matching.
- Don't reintroduce sportsbook scrapers without re-checking ToS and anti-bot — the previous attempts were brittle. Paste-in via `add_odds.py` is the supported path.
- Don't delete voided pick rows. The audit trail and the partial-unique-index contract both depend on them sticking around.
- Don't widen the dashboard's `except Exception: return []` swallows. They mask the import bugs above and make it impossible to see what's actually failing. Better to let it crash visibly.

## Useful entry points when working on this

- Want to reason about a player ID issue? Start at `normalize/players.py:430` (`PlayerCrosswalk.resolve`) and look at the test cases in `__main__` (line 624+).
- Want to change scoring? `scoring/multi_objective.py:341` (`score_field`) is the only public entry. Probability cascade is `_base_finish_probs` at line 124.
- Want to add a real data source? Hydrate `PlayerInputs.datagolf_finish_distribution` (best signal) or `vegas_win_implied_pct` (next best) in `scoring/inputs.py:140`, and the cascade picks it up automatically.
- Want to debug a pipeline run end-to-end? `python pipeline/monday_open.py --demo` runs a self-contained mock against a fresh DB. Good sanity check after any change to `players.py` or `ledger.py`.

## Environment

`.env.example` lists `DATAGOLF_API_KEY`, `ESPN_API_KEY`, `ODDSTRADER_API_KEY`, `GMAIL_APP_PASSWORD`, `GMAIL_FROM`. In practice ESPN doesn't need a key (the endpoints we use are unauthenticated), DataGolf and OddsTrader integrations are disabled, and the Gmail credentials are unused in current code. The `.env` file is real config only for whatever the user wires up next.
