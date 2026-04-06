"""
agents/scanner.py — Agent 1: Scanner

Responsibilities:
  1. Fetch futures odds from The Odds API (championship winners)
  2. Fetch current Polymarket prices for matching futures markets
  3. Calculate edge (gap between bookmaker probability and Polymarket price)
  4. Push opportunities with sufficient raw edge to the opportunity queue

Does NOT make trade decisions — that is the Analyst's job.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests

from core.state import BotState
from core.probability import (
    american_to_prob,
    calculate_edge,
)

log = logging.getLogger("scanner")


def _get_odds_api_key() -> str:
    return os.getenv("ODDS_API_KEY", "")


POLY_GAMMA_BASE = "https://gamma-api.polymarket.com"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Futures markets to scan — these match Polymarket's sports futures
FUTURES_SPORTS = [
    {
        "odds_key": "basketball_nba_championship_winner",
        "poly_pattern": "win the 2026 nba finals",
        "label": "NBA Finals",
    },
    {
        "odds_key": "baseball_mlb_world_series_winner",
        "poly_pattern": "win the 2026 mlb world series",
        "label": "MLB World Series",
    },
    {
        "odds_key": "soccer_fifa_world_cup_winner",
        "poly_pattern": "win the 2026 fifa world cup",
        "label": "FIFA World Cup",
    },
    {
        "odds_key": "icehockey_nhl_championship_winner",
        "poly_pattern": "win the 2026 nhl stanley cup",
        "label": "NHL Stanley Cup",
    },
]

# Minimum raw edge to bother queuing (analyst applies stricter gate)
RAW_EDGE_THRESHOLD = 0.04  # 4% for futures (tighter than game lines)

# Team name aliases for matching Polymarket questions
TEAM_ALIASES = {
    # NBA
    "Oklahoma City Thunder": ["thunder", "oklahoma city"],
    "Boston Celtics": ["celtics", "boston"],
    "San Antonio Spurs": ["spurs", "san antonio"],
    "Denver Nuggets": ["nuggets", "denver"],
    "Cleveland Cavaliers": ["cavaliers", "cleveland"],
    "New York Knicks": ["knicks", "new york knicks"],
    "Detroit Pistons": ["pistons", "detroit"],
    "Minnesota Timberwolves": ["timberwolves", "minnesota"],
    "Houston Rockets": ["rockets", "houston"],
    "Philadelphia 76ers": ["76ers", "philadelphia"],
    "Milwaukee Bucks": ["bucks", "milwaukee"],
    "Golden State Warriors": ["warriors", "golden state"],
    "Los Angeles Lakers": ["lakers", "los angeles lakers"],
    "Los Angeles Clippers": ["clippers", "la clippers"],
    "Miami Heat": ["heat", "miami heat"],
    "Dallas Mavericks": ["mavericks", "dallas"],
    "Phoenix Suns": ["suns", "phoenix"],
    "Indiana Pacers": ["pacers", "indiana"],
    "Atlanta Hawks": ["hawks", "atlanta hawks"],
    "Orlando Magic": ["magic", "orlando"],
    "Memphis Grizzlies": ["grizzlies", "memphis"],
    "Sacramento Kings": ["kings", "sacramento"],
    "Portland Trail Blazers": ["trail blazers", "portland"],
    "New Orleans Pelicans": ["pelicans", "new orleans"],
    "Toronto Raptors": ["raptors", "toronto raptors"],
    "Chicago Bulls": ["bulls", "chicago bulls"],
    "Charlotte Hornets": ["hornets", "charlotte"],
    "Brooklyn Nets": ["nets", "brooklyn"],
    "Utah Jazz": ["jazz", "utah"],
    "Washington Wizards": ["wizards", "washington wizards"],
    # MLB
    "Los Angeles Dodgers": ["dodgers", "los angeles dodgers"],
    "New York Yankees": ["yankees", "new york yankees"],
    "New York Mets": ["mets", "new york mets"],
    "Atlanta Braves": ["braves", "atlanta braves"],
    "Houston Astros": ["astros", "houston astros"],
    "Philadelphia Phillies": ["phillies", "philadelphia phillies"],
    "Seattle Mariners": ["mariners", "seattle mariners"],
    "Toronto Blue Jays": ["blue jays", "toronto blue jays"],
    "Detroit Tigers": ["tigers", "detroit tigers"],
    "Boston Red Sox": ["red sox", "boston red sox"],
    "Chicago Cubs": ["cubs", "chicago cubs"],
    "Baltimore Orioles": ["orioles", "baltimore"],
    "San Diego Padres": ["padres", "san diego"],
    "Cleveland Guardians": ["guardians", "cleveland guardians"],
    "Minnesota Twins": ["twins", "minnesota twins"],
    "Tampa Bay Rays": ["rays", "tampa bay"],
    "Milwaukee Brewers": ["brewers", "milwaukee brewers"],
    "St. Louis Cardinals": ["cardinals", "st. louis"],
    "Texas Rangers": ["rangers", "texas rangers"],
    "Cincinnati Reds": ["reds", "cincinnati"],
    "Pittsburgh Pirates": ["pirates", "pittsburgh"],
    "San Francisco Giants": ["giants", "san francisco giants"],
    "Kansas City Royals": ["royals", "kansas city"],
    "Colorado Rockies": ["rockies", "colorado"],
    "Miami Marlins": ["marlins", "miami marlins"],
    "Washington Nationals": ["nationals", "washington nationals"],
    "Arizona Diamondbacks": ["diamondbacks", "arizona"],
    "Los Angeles Angels": ["angels", "los angeles angels"],
    "Chicago White Sox": ["white sox", "chicago white sox"],
    "Oakland Athletics": ["athletics", "oakland"],
    # FIFA
    "Spain": ["spain"], "France": ["france"], "England": ["england"],
    "Brazil": ["brazil"], "Argentina": ["argentina"], "Portugal": ["portugal"],
    "Germany": ["germany"], "Netherlands": ["netherlands"], "Norway": ["norway"],
    "Belgium": ["belgium"], "Italy": ["italy"], "Colombia": ["colombia"],
    "USA": ["united states", "usa"], "Mexico": ["mexico"], "Japan": ["japan"],
    "Australia": ["australia"], "Ecuador": ["ecuador"], "Morocco": ["morocco"],
    "Denmark": ["denmark"], "Uruguay": ["uruguay"], "Croatia": ["croatia"],
    "Switzerland": ["switzerland"], "Canada": ["canada"],
}


@dataclass
class Opportunity:
    """Raw opportunity detected by scanner — unvalidated."""
    timestamp:      float
    event_name:     str
    sport:          str
    market_id:      str       # Polymarket condition ID
    token_id:       str       # Polymarket CLOB token ID
    our_side:       str       # YES | NO
    our_prob:       float     # bookmaker consensus probability
    poly_price:     float     # current Polymarket price
    raw_edge:       float     # our_prob - poly_price
    score_diff:     int       # not used for futures (always 0)
    time_remaining_pct: float # not used for futures
    data_sources:   int       # number of bookmakers
    source_detail:  str       # human-readable context


class ScannerAgent:
    def __init__(self, state: BotState):
        self.state = state
        self._poly_market_cache: list[dict] = []
        self._last_poly_refresh = 0.0

    # ── Main scan loop entry ─────────────────────────────────────────────────

    async def scan(self, queue: asyncio.Queue) -> None:
        """
        Run one scan cycle: fetch futures odds, compare to Polymarket, queue gaps.
        Called by main.py on each SCAN_INTERVAL_SEC tick.
        """
        if not _get_odds_api_key():
            log.warning("ODDS_API_KEY not set — scanner running in demo mode")
            await self._demo_scan(queue)
            return

        await self._refresh_poly_markets()

        if not self._poly_market_cache:
            log.warning("No Polymarket sports markets cached — skipping scan")
            return

        for fut in FUTURES_SPORTS:
            try:
                await self._scan_futures(fut, queue)
            except Exception as e:
                log.error(f"Scan error for {fut['label']}: {e}")

    # ── Polymarket market cache ──────────────────────────────────────────────

    async def _refresh_poly_markets(self) -> None:
        """Fetch all active Polymarket sports markets from the Gamma events API."""
        if time.time() - self._last_poly_refresh < 300:
            return

        all_markets = []
        try:
            for offset in range(0, 3000, 100):
                resp = await asyncio.to_thread(
                    requests.get,
                    f"{POLY_GAMMA_BASE}/events",
                    params={
                        "tag": "sports",
                        "active": "true",
                        "closed": "false",
                        "limit": 100,
                        "offset": offset,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                events = resp.json()
                if not events:
                    break

                for event in events:
                    for m in event.get("markets", []):
                        question = m.get("question", "")
                        prices_raw = m.get("outcomePrices", "")
                        tokens_raw = m.get("clobTokenIds", "")

                        try:
                            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
                        except (json.JSONDecodeError, TypeError):
                            continue

                        if not prices or not tokens or len(prices) < 2 or len(tokens) < 2:
                            continue

                        yes_price = float(prices[0])
                        if yes_price <= 0.001 or yes_price >= 0.999:
                            continue

                        all_markets.append({
                            "question": question,
                            "question_lower": question.lower(),
                            "yes_price": yes_price,
                            "no_price": float(prices[1]),
                            "yes_token": tokens[0],
                            "no_token": tokens[1],
                            "condition_id": m.get("conditionId", ""),
                        })

            self._poly_market_cache = all_markets
            self._last_poly_refresh = time.time()
            log.info(f"Polymarket cache: {len(all_markets)} active sports markets")
        except Exception as e:
            log.error(f"Failed to refresh Polymarket markets: {e}")

    # ── Futures scanning ─────────────────────────────────────────────────────

    async def _scan_futures(self, fut: dict, queue: asyncio.Queue) -> None:
        """
        Fetch futures odds from bookmakers, compute consensus probability
        for each team, compare to Polymarket price for the same team.
        """
        odds_key = fut["odds_key"]
        poly_pattern = fut["poly_pattern"]
        label = fut["label"]

        # 1. Fetch bookmaker futures odds
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"{ODDS_API_BASE}/sports/{odds_key}/odds",
                params={
                    "apiKey": _get_odds_api_key(),
                    "regions": "us",
                    "markets": "outrights",
                    "oddsFormat": "american",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                log.warning(f"Odds API ({label}): {data.get('message', 'error')}")
                return
        except Exception as e:
            log.warning(f"Odds API error ({label}): {e}")
            return

        if not data:
            return

        # 2. Aggregate odds across bookmakers for each team
        team_odds: dict[str, list[int]] = {}
        num_bookmakers = 0
        for event in data:
            for bk in event.get("bookmakers", []):
                num_bookmakers += 1
                for mkt in bk.get("markets", []):
                    for outcome in mkt.get("outcomes", []):
                        name = outcome.get("name", "")
                        price = outcome.get("price", 0)
                        if name and price:
                            team_odds.setdefault(name, []).append(price)

        if not team_odds:
            return

        # 3. Find matching Polymarket markets
        poly_futures = [m for m in self._poly_market_cache if poly_pattern in m["question_lower"]]

        if not poly_futures:
            log.debug(f"No Polymarket markets matching '{poly_pattern}'")
            return

        matched = 0
        for team_name, odds_list in team_odds.items():
            # Consensus probability (median of bookmaker implied probs)
            probs = [american_to_prob(o) for o in odds_list]
            probs.sort()
            book_prob = probs[len(probs) // 2]

            # Find the Polymarket market for this team
            poly_market = self._find_poly_market_for_team(team_name, poly_futures)
            if not poly_market:
                continue

            matched += 1
            poly_yes = poly_market["yes_price"]
            poly_no = poly_market["no_price"]

            # Check YES edge: bookmaker says higher prob than Polymarket price
            edge_yes = calculate_edge(book_prob, poly_yes)
            # Check NO edge: bookmaker says lower prob than what Polymarket implies
            edge_no = calculate_edge(1.0 - book_prob, poly_no)

            for side, edge, poly_price, token_id, our_prob in [
                ("YES", edge_yes, poly_yes, poly_market["yes_token"], book_prob),
                ("NO", edge_no, poly_no, poly_market["no_token"], 1.0 - book_prob),
            ]:
                if edge >= RAW_EDGE_THRESHOLD:
                    opp = Opportunity(
                        timestamp=time.time(),
                        event_name=poly_market["question"],
                        sport=odds_key,
                        market_id=poly_market["condition_id"],
                        token_id=token_id,
                        our_side=side,
                        our_prob=round(our_prob, 4),
                        poly_price=round(poly_price, 4),
                        raw_edge=round(edge, 4),
                        score_diff=0,
                        time_remaining_pct=0.1,
                        data_sources=len(odds_list),
                        source_detail=f"{len(odds_list)} books, {label}",
                    )
                    log.info(
                        f"EDGE  {team_name} | {label} {side} "
                        f"edge={edge*100:.1f}% "
                        f"(book:{our_prob:.3f} poly:{poly_price:.3f})"
                    )
                    await queue.put(opp)
                else:
                    self.state.skip_trade(
                        f"{team_name} {label}: {side} edge {edge*100:.1f}% < {RAW_EDGE_THRESHOLD*100}%"
                    )

        log.info(f"{label}: matched {matched}/{len(team_odds)} teams to Polymarket")

    def _find_poly_market_for_team(self, team_name: str, poly_markets: list[dict]) -> Optional[dict]:
        """Find the Polymarket market that references this team."""
        team_lower = team_name.lower()

        # Try exact team name match first
        for pm in poly_markets:
            if team_lower in pm["question_lower"]:
                return pm

        # Try aliases
        aliases = TEAM_ALIASES.get(team_name, [])
        for alias in aliases:
            for pm in poly_markets:
                if alias in pm["question_lower"]:
                    return pm

        return None

    # ── Demo mode (no API keys) ───────────────────────────────────────────────

    async def _demo_scan(self, queue: asyncio.Queue) -> None:
        """Emit a fake opportunity for pipeline testing."""
        if os.getenv("DEMO_MODE", "true").lower() != "true":
            return
        import random
        if random.random() > 0.3:
            return

        fake_opp = Opportunity(
            timestamp=time.time(),
            event_name="DEMO Lakers vs Celtics",
            sport="basketball_nba",
            market_id="demo-market-001",
            token_id="demo-token-yes",
            our_side="YES",
            our_prob=0.84,
            poly_price=0.55,
            raw_edge=0.29,
            score_diff=18,
            time_remaining_pct=0.08,
            data_sources=3,
            source_detail="DEMO — not a real trade",
        )
        log.info("DEMO MODE — emitting fake opportunity")
        await queue.put(fake_opp)
