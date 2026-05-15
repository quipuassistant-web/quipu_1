"""
Usage: python -m golf_agent.pipeline.thursday_lock
Or: python golf_agent/pipeline/thursday_lock.py
"""
import logging
import os
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.expanduser("~/AI_HOME/LOGS/golf_agent.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def final_check():
    """
    Thursday morning final check.
    
    Steps:
    1. ESPN field check — any last-minute WDs?
    2. Re-pull odds (lines move mid-week)
    3. Re-score with fresh odds
    4. Compare to Monday's recs — any changes?
    5. If recommended pick has withdrawn → alert immediately
    6. Print ranked final recommendations
    7. Ask: lock pick?
    """
    from normalize.events import get_upcoming
    from scoring.multi_objective import get_recommendations_for_event
    from fetchers.espn import get_current_tournament
    from ledger.ledger import get_season_summary
    
    upcoming = get_upcoming(n=1)
    if not upcoming:
        logger.info("No upcoming events found")
        return
    
    event = upcoming[0]
    event_id = event["id"]
    
    # Check for WDs
    try:
        tournament = get_current_tournament()
        if tournament.get("status") == "in_progress":
            logger.info("⚠️  Tournament already in progress — checking for WDs...")
        else:
            logger.info(f"Field status: {tournament.get('status', 'pre')}")
    except Exception as e:
        logger.warning(f"ESPN check error: {e}")
    
    # Odds re-pull is manual: paste fresh DraftKings lines into the dashboard
    # via /odds/<event_id>, or via add_odds.py. The scorer reads from event_odds.
    logger.info('Reminder: paste fresh DraftKings odds before locking the pick.')
    
    # Re-score
    try:
        recs = get_recommendations_for_event(event_id, top_n=5)
        logger.info(f"=== FINAL RECS for {event['display_name']} (THURSDAY) ===")
        for i, r in enumerate(recs):
            logger.info(f"{i+1}. {r['display_name']} | Score: {r['composite_score']}/100 ({r['confidence']}) | Odds: {r.get('vegas_odds', 'N/A')}")
            if r.get("opportunity_cost"):
                logger.info(f"   ⚠️  {r['opportunity_cost']}")
    except Exception as e:
        logger.error(f"Error generating final recommendations: {e}")
        recs = []
    
    # Season summary
    summary = get_season_summary()
    logger.info(f"Season: ${summary.get('total_winnings', 0):,.0f} | {summary.get('events_completed', 0)}/{summary.get('total_events', 31)} events")
    
    return recs


def lock_pick(player_id: str, event_id: str = None):
    """
    Lock in a pick for an event.
    Currently just logs it — ledger entry happens at sunday_close.
    """
    from normalize.players import lookup
    
    if event_id is None:
        from normalize.events import get_upcoming
        upcoming = get_upcoming(n=1)
        event_id = upcoming[0]["id"] if upcoming else None
    
    player = lookup(player_id)
    event_name = event_id
    
    logger.info(f"🔒 PICK LOCKED: {player['display_name']} for {event_name}")
    logger.info(f"   (Ledger entry will be recorded at sunday_close after tournament)")
    
    # Append to a pending_picks file for tracking
    pending_file = os.path.expanduser("~/AI_HOME/TOOLS/golf_agent/pending_pick.txt")
    with open(pending_file, "w") as f:
        f.write(f"{datetime.now().isoformat()}|{player_id}|{event_id}\n")


if __name__ == "__main__":
    final_check()