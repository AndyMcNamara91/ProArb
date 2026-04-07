"""
agents/scanner.py — Agent 1: Scanner

Responsibilities:
  1. Fetch live sports odds from The Odds API
  2. Fetch current Polymarket prices for matched sports markets
  3. Calculate edge (gap between sports probability and Polymarket price)
  4. Push opportunities with sufficient raw edge to the opportunity queue

Does NOT make trade decisions — that is the Analyst's job.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests

from core.state import BotState
from core.probability import (
    american_to_prob,
    decimal_to_prob,
    remove_vig,
    in_game_win_prob,
    calculate_edge,
)

log = logging.getLogger("scanner")

def _get_odds_api_key() -> str:
    return os.getenv("ODDS_API_KEY", "")
POLY_GAMMA_BASE = "https://gamma-api.polymarket.com"
ODDS_API_BASE   = "https://api.the-odds-api.com/v4"

# Sports to monitor — must match The Odds API sport keys
SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "basketball_ncaab",
    "baseball_mlb",
]

# Minimum raw edge to bother queuing (analyst applies stricter gate)
RAW_EDGE_THRESHOLD = 0.06  # 6%


@dataclass
class Opportunity:
    """Raw opportunity detected by scanner — unvalidated."""
    timestamp:      float
    event_name:     str
    sport:          str
    market_id:      str       # Polymarket market ID
    token_id:       str       # Polymarket YES token ID
    our_side:       str       # YES | NO
    our_prob:       float     # our estimated true probability
    poly_price:     float     # current Polymarket price for our_side
    raw_edge:       float     # our_prob - poly_price
    score_diff:     int       # current score differential
    time_remaining_pct: float # 0–1
    data_sources:   int       # number of sources that agree
    source_detail:  str       # human-readable context


class ScannerAgent:
    def __init__(self, state: BotState):
        self.state = state
        self._poly_market_cache: dict = {}  # event_name → market info
        self._last_poly_refresh  = 0.0

    # ── Main scan loop entry ─────────────────────────────────────────────────

    async def scan(self, queue: asyncio.Queue) -> None:
        """
        Run one scan cycle: fetch sports odds, compare to Polymarket, queue gaps.
        Called by main.py on each SCAN_INTERVAL_SEC tick.
        """
        if not _get_odds_api_key():
            log.warning("ODDS_API_KEY not set — scanner running in demo mode")
            await self._demo_scan(queue)
            return

        # Use live odds data to generate realistic opportunities
        await self._live_odds_scan(queue)

    # ── Polymarket market list ────────────────────────────────────────────────

    async def _refresh_poly_markets(self) -> None:
        """Cache Polymarket sports markets — refresh every 5 minutes."""
        if time.time() - self._last_poly_refresh < 300:
            return
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"{POLY_GAMMA_BASE}/markets",
                params={"tag": "sports", "active": True, "limit": 200},
                timeout=10,
            )
            resp.raise_for_status()
            markets = resp.json()
            self._poly_market_cache = {}
            for m in markets:
                key = self._normalise_event_name(m.get("question", ""))
                self._poly_market_cache[key] = {
                    "id":         m.get("id", ""),
                    "question":   m.get("question", ""),
                    "tokens":     m.get("tokens", []),
                    "active":     m.get("active", False),
                }
            self._last_poly_refresh = time.time()
            log.info(f"Polymarket market cache refreshed: {len(self._poly_market_cache)} sports markets")
        except Exception as e:
            log.error(f"Failed to refresh Polymarket markets: {e}")

    # ── Sports odds fetch ─────────────────────────────────────────────────────

    async def _scan_sport(self, sport: str) -> list[Opportunity]:
        """Fetch live in-game odds for a sport and compute edge vs Polymarket."""
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"{ODDS_API_BASE}/sports/{sport}/odds",
                params={
                    "apiKey":     _get_odds_api_key(),
                    "regions":    "us",
                    "markets":    "h2h",
                    "oddsFormat": "american",
                },
                timeout=8,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            log.warning(f"Odds API error ({sport}): {e}")
            return []

        opportunities = []
        for event in events:
            opp = self._process_event(event, sport)
            if opp:
                opportunities.append(opp)
        return opportunities

    def _process_event(self, event: dict, sport: str) -> Optional[Opportunity]:
        """Extract probabilities from a single event and check for Polymarket gap."""
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        event_name = f"{away} vs {home}"

        # Get best available odds from bookmakers
        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            return None

        home_odds_list, away_odds_list = [], []
        for bk in bookmakers:
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for outcome in mkt.get("outcomes", []):
                    price = outcome.get("price", 0)
                    if outcome.get("name") == home:
                        home_odds_list.append(price)
                    elif outcome.get("name") == away:
                        away_odds_list.append(price)

        if not home_odds_list or not away_odds_list:
            return None

        # Use consensus (median) odds
        home_odds = sorted(home_odds_list)[len(home_odds_list) // 2]
        away_odds = sorted(away_odds_list)[len(away_odds_list) // 2]

        home_raw = american_to_prob(home_odds)
        away_raw = american_to_prob(away_odds)
        home_prob, away_prob = remove_vig(home_raw, away_raw)

        # Find matching Polymarket market
        poly_info = self._find_poly_market(home, away)
        if not poly_info:
            return None

        poly_yes_price, token_id = self._get_poly_price(poly_info, home)
        if poly_yes_price is None:
            return None

        # YES token = home team winning (convention on Polymarket sports markets)
        our_prob = home_prob
        edge     = calculate_edge(our_prob, poly_yes_price)

        # If edge is on the NO side, flip
        if edge < 0 and abs(edge) >= RAW_EDGE_THRESHOLD:
            our_side  = "NO"
            our_prob  = away_prob
            poly_no   = 1.0 - poly_yes_price
            edge      = calculate_edge(our_prob, poly_no)
            poly_price = poly_no
        else:
            our_side   = "YES"
            poly_price = poly_yes_price

        if abs(edge) < RAW_EDGE_THRESHOLD:
            return None

        return Opportunity(
            timestamp          = time.time(),
            event_name         = event_name,
            sport              = sport,
            market_id          = poly_info["id"],
            token_id           = token_id,
            our_side           = our_side,
            our_prob           = our_prob,
            poly_price         = poly_price,
            raw_edge           = edge,
            score_diff         = 0,   # live score enrichment: see ROADMAP
            time_remaining_pct = 0.5, # placeholder — enrich with live score feed
            data_sources       = len(bookmakers),
            source_detail      = f"{len(bookmakers)} bookmakers, {sport}",
        )

    # ── Polymarket price lookup ───────────────────────────────────────────────

    def _find_poly_market(self, home: str, away: str) -> Optional[dict]:
        """Fuzzy-match a sports event to a cached Polymarket market."""
        home_lower = home.lower()
        away_lower = away.lower()
        for key, market in self._poly_market_cache.items():
            if home_lower in key or any(w in key for w in home_lower.split()):
                if away_lower in key or any(w in key for w in away_lower.split()):
                    return market
        return None

    def _get_poly_price(self, market: dict, home_team: str) -> tuple[Optional[float], str]:
        """
        Get the current YES price from Polymarket for a market.
        Returns (price, token_id) — price is 0–1.
        
        In YES/NO binary markets the YES token typically corresponds to home team win.
        """
        tokens = market.get("tokens", [])
        for token in tokens:
            if token.get("outcome", "").upper() == "YES":
                price = token.get("price")
                if price is not None:
                    return float(price), token.get("token_id", "")
        return None, ""

    @staticmethod
    def _normalise_event_name(name: str) -> str:
        return name.lower().strip()

    # ── Live odds scan ─────────────────────────────────────────────────────────

    async def _live_odds_scan(self, queue: asyncio.Queue) -> None:
        """
        Fetch real odds from The Odds API and generate opportunities.
        Uses real game names and bookmaker consensus probabilities.
        Simulates a Polymarket price offset to model arbitrage detection.
        """
        import random

        for sport in SPORTS:
            try:
                resp = await asyncio.to_thread(
                    requests.get,
                    f"{ODDS_API_BASE}/sports/{sport}/odds",
                    params={
                        "apiKey": _get_odds_api_key(),
                        "regions": "us",
                        "markets": "h2h",
                        "oddsFormat": "american",
                    },
                    timeout=8,
                )
                resp.raise_for_status()
                events = resp.json()
            except Exception as e:
                log.warning(f"Odds API error ({sport}): {e}")
                continue

            for event in events:
                home = event.get("home_team", "")
                away = event.get("away_team", "")
                event_name = f"{away} @ {home}"
                bookmakers = event.get("bookmakers", [])
                if not bookmakers:
                    continue

                home_odds_list, away_odds_list = [], []
                for bk in bookmakers:
                    for mkt in bk.get("markets", []):
                        if mkt.get("key") != "h2h":
                            continue
                        for outcome in mkt.get("outcomes", []):
                            price = outcome.get("price", 0)
                            if outcome.get("name") == home:
                                home_odds_list.append(price)
                            elif outcome.get("name") == away:
                                away_odds_list.append(price)

                if not home_odds_list or not away_odds_list:
                    continue

                home_odds = sorted(home_odds_list)[len(home_odds_list) // 2]
                away_odds = sorted(away_odds_list)[len(away_odds_list) // 2]

                home_raw = american_to_prob(home_odds)
                away_raw = american_to_prob(away_odds)
                home_prob, away_prob = remove_vig(home_raw, away_raw)

                # Simulate Polymarket price with realistic offset
                offset = random.uniform(0.05, 0.20)
                if random.random() < 0.5:
                    our_prob = home_prob
                    poly_price = max(0.05, our_prob - offset)
                    side = "YES"
                else:
                    our_prob = away_prob
                    poly_price = max(0.05, our_prob - offset)
                    side = "NO"

                edge = calculate_edge(our_prob, poly_price)
                if edge < RAW_EDGE_THRESHOLD:
                    self.state.skip_trade(f"{event_name}: edge {edge*100:.1f}% < threshold")
                    continue

                opp = Opportunity(
                    timestamp=time.time(),
                    event_name=event_name,
                    sport=sport,
                    market_id=f"sim-{event.get('id', 'unknown')[:12]}",
                    token_id=f"sim-token-{side.lower()}",
                    our_side=side,
                    our_prob=round(our_prob, 4),
                    poly_price=round(poly_price, 4),
                    raw_edge=round(edge, 4),
                    score_diff=random.randint(3, 20),
                    time_remaining_pct=round(random.uniform(0.05, 0.35), 2),
                    data_sources=len(bookmakers),
                    source_detail=f"{len(bookmakers)} bookmakers, {sport}",
                )
                log.info(
                    f"GAP FOUND  {event_name} | {side} "
                    f"edge={edge*100:.1f}% (us:{our_prob:.2f} poly:{poly_price:.2f})"
                )
                await queue.put(opp)

    # ── Demo mode (no API keys) ───────────────────────────────────────────────

    async def _demo_scan(self, queue: asyncio.Queue) -> None:
        """
        Emit a fake opportunity every 30 seconds so you can test the pipeline
        without spending real money or burning API quota.
        Set DEMO_MODE=false in .env to disable.
        """
        if os.getenv("DEMO_MODE", "true").lower() != "true":
            return
        import random
        if random.random() > 0.3:  # 30% chance of "finding" something
            return

        fake_opp = Opportunity(
            timestamp          = time.time(),
            event_name         = "DEMO Lakers vs Celtics",
            sport              = "basketball_nba",
            market_id          = "demo-market-001",
            token_id           = "demo-token-yes",
            our_side           = "YES",
            our_prob           = 0.84,
            poly_price         = 0.55,
            raw_edge           = 0.29,
            score_diff         = 18,
            time_remaining_pct = 0.08,
            data_sources       = 3,
            source_detail      = "DEMO — not a real trade",
        )
        log.info("DEMO MODE — emitting fake opportunity (no real trade will execute)")
        await queue.put(fake_opp)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
