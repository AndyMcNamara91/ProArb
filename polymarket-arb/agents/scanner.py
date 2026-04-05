"""
agents/scanner.py — Agent 1: Scanner

Responsibilities:
  1. Fetch real Polymarket sports markets (Gamma API — free, no key)
  2. Fetch live game scores from ESPN (free, no key)
  3. Fetch bookmaker odds from The Odds API (if ODDS_API_KEY set)
  4. Compare implied probabilities to Polymarket prices to find edge
  5. Push opportunities with sufficient raw edge to the opportunity queue

Does NOT make trade decisions — that is the Analyst's job.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from statistics import median
from typing import Optional

import requests

from core.state import BotState
from core.probability import (
    american_to_prob,
    remove_vig,
    calculate_edge,
    in_game_win_prob,
)

log = logging.getLogger("scanner")

ODDS_API_KEY    = os.getenv("ODDS_API_KEY", "")
POLY_GAMMA_BASE = "https://gamma-api.polymarket.com"
ODDS_API_BASE   = "https://api.the-odds-api.com/v4"
ESPN_BASE       = "https://site.api.espn.com/apis/site/v2/sports"

# Sports to monitor — The Odds API sport keys
SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "basketball_ncaab",
    "baseball_mlb",
]

# ESPN endpoint mapping
ESPN_ENDPOINTS = {
    "basketball_nba":      "basketball/nba",
    "americanfootball_nfl": "football/nfl",
    "basketball_ncaab":    "basketball/mens-college-basketball",
    "baseball_mlb":        "baseball/mlb",
}

# Minimum raw edge to bother queuing (analyst applies stricter gate)
RAW_EDGE_THRESHOLD = 0.06  # 6%


@dataclass
class Opportunity:
    """Raw opportunity detected by scanner — unvalidated."""
    timestamp:          float
    event_name:         str
    sport:              str
    market_id:          str       # Polymarket market ID
    token_id:           str       # Polymarket YES token ID
    our_side:           str       # YES | NO
    our_prob:           float     # our estimated true probability
    poly_price:         float     # current Polymarket price for our_side
    raw_edge:           float     # our_prob - poly_price
    score_diff:         int       # current score differential
    time_remaining_pct: float     # 0-1
    data_sources:       int       # number of sources that agree
    source_detail:      str       # human-readable context


class ScannerAgent:
    def __init__(self, state: BotState):
        self.state = state
        self._poly_markets: list[dict] = []
        self._last_poly_refresh = 0.0
        self._espn_cache: dict = {}          # sport -> {team_name -> game_state}
        self._last_espn_refresh: dict = {}   # sport -> timestamp

    async def _async_get(self, url: str, params: dict = None) -> Optional[dict]:
        """Run requests.get in executor to avoid blocking the event loop."""
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None, lambda: requests.get(url, params=params, timeout=10)
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.debug(f"HTTP GET failed {url}: {e}")
            return None

    # ── Main scan loop entry ─────────────────────────────────────────────────

    async def scan(self, queue: asyncio.Queue) -> None:
        """Run one scan cycle: fetch real market data, find edges, queue gaps."""
        try:
            await self._refresh_poly_markets()
        except Exception as e:
            log.error(f"Failed to refresh Polymarket markets: {e}")

        if not self._poly_markets:
            log.warning("No Polymarket sports markets found — nothing to scan")
            return

        # Fetch ESPN scores for all sports
        for sport in SPORTS:
            try:
                await self._refresh_espn_scores(sport)
            except Exception as e:
                log.debug(f"ESPN refresh failed for {sport}: {e}")

        if ODDS_API_KEY:
            # Full mode: compare bookmaker odds to Polymarket prices
            await self._scan_with_odds_api(queue)
        else:
            # No odds API key: use ESPN scores + Polymarket prices
            await self._scan_with_espn(queue)

    # ── Polymarket market list (real API) ────────────────────────────────────

    async def _refresh_poly_markets(self) -> None:
        """Fetch active sports markets from Polymarket Gamma API. Refresh every 2 min."""
        if time.time() - self._last_poly_refresh < 120:
            return

        all_markets = []

        # Fetch today's game markets and futures
        params = {"active": "true", "closed": "false", "limit": "100",
                  "order": "volume24hr", "ascending": "false"}
        data = await self._async_get(f"{POLY_GAMMA_BASE}/markets", params)
        if data:
            all_markets.extend(data)

        # Filter to sports-related markets
        sports_keywords = [
            "win", "nba", "nfl", "mlb", "nhl", "ncaa", "premier league",
            "champions league", "la liga", "serie a", "bundesliga",
            "cricket", "ipl", "tennis", "ufc", "boxing", "soccer",
            "football", "basketball", "baseball", "finals", "world cup",
            "vs", "match", "game",
        ]
        political_keywords = [
            "president", "election", "nominee", "nomination", "congress",
            "senate", "governor", "democrat", "republican", "trump",
            "biden", "gta", "ceasefire", "iran", "china", "ukraine",
            "fed ", "interest rate", "tariff",
        ]

        self._poly_markets = []
        seen_ids = set()
        for m in all_markets:
            mid = m.get("id", "")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)

            q = m.get("question", "").lower()
            closed = m.get("closed", True)
            prices = m.get("outcomePrices", "[]")

            # Skip closed, no-price, or political markets
            if closed:
                continue
            if any(kw in q for kw in political_keywords):
                continue
            if not any(kw in q for kw in sports_keywords):
                continue

            # Parse prices
            try:
                if isinstance(prices, str):
                    import json
                    price_list = json.loads(prices)
                else:
                    price_list = prices
                yes_price = float(price_list[0]) if price_list else 0
                no_price = float(price_list[1]) if len(price_list) > 1 else 1 - yes_price
            except (ValueError, IndexError):
                continue

            # Skip resolved markets (price near 0 or 1)
            if yes_price < 0.02 or yes_price > 0.98:
                continue

            # Parse token IDs
            tokens_raw = m.get("clobTokenIds", "[]")
            try:
                if isinstance(tokens_raw, str):
                    import json
                    token_ids = json.loads(tokens_raw)
                else:
                    token_ids = tokens_raw
            except (ValueError, TypeError):
                token_ids = []

            self._poly_markets.append({
                "id": mid,
                "question": m.get("question", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "token_id_yes": token_ids[0] if token_ids else "",
                "token_id_no": token_ids[1] if len(token_ids) > 1 else "",
                "volume_24h": m.get("volume24hr", 0),
            })

        self._last_poly_refresh = time.time()
        log.info(f"Polymarket: {len(self._poly_markets)} active sports markets found")

    # ── ESPN live scores (real API, free) ────────────────────────────────────

    async def _refresh_espn_scores(self, sport: str) -> None:
        """Fetch live scores from ESPN. Rate limit: 1 req/sport/30s."""
        last = self._last_espn_refresh.get(sport, 0)
        if time.time() - last < 30:
            return

        endpoint = ESPN_ENDPOINTS.get(sport)
        if not endpoint:
            return

        data = await self._async_get(f"{ESPN_BASE}/{endpoint}/scoreboard")
        if not data:
            return

        games = {}
        for event in data.get("events", []):
            name = event.get("name", "")
            short = event.get("shortName", "")
            status_type = event.get("status", {}).get("type", {})
            state = status_type.get("state", "")  # pre, in, post

            competitions = event.get("competitions", [])
            if not competitions:
                continue
            comp = competitions[0]

            home_team = away_team = ""
            home_score = away_score = 0
            for competitor in comp.get("competitors", []):
                team_name = competitor.get("team", {}).get("displayName", "")
                score = int(competitor.get("score", "0") or "0")
                if competitor.get("homeAway") == "home":
                    home_team = team_name
                    home_score = score
                else:
                    away_team = team_name
                    away_score = score

            # Estimate time remaining
            status_detail = event.get("status", {}).get("displayClock", "0:00")
            period = int(event.get("status", {}).get("period", 1) or 1)
            time_pct = self._estimate_time_remaining(sport, period, status_detail, state)

            game_state = {
                "home_team": home_team,
                "away_team": away_team,
                "home_score": home_score,
                "away_score": away_score,
                "score_diff": home_score - away_score,
                "time_remaining_pct": time_pct,
                "state": state,
                "period": period,
                "event_name": name,
                "short_name": short,
            }

            # Index by multiple keys for fuzzy matching
            for key in [home_team.lower(), away_team.lower(), name.lower(), short.lower()]:
                games[key] = game_state

        self._espn_cache[sport] = games
        self._last_espn_refresh[sport] = time.time()
        live_count = sum(1 for g in set(id(v) for v in games.values()))
        log.info(f"ESPN {sport}: {len(data.get('events', []))} games ({state})")

    def _estimate_time_remaining(self, sport: str, period: int,
                                  clock: str, state: str) -> float:
        """Estimate fraction of game remaining (0=over, 1=full game)."""
        if state == "post":
            return 0.0
        if state == "pre":
            return 1.0

        # Parse clock "M:SS" or "H:MM:SS"
        parts = clock.replace(" ", "").split(":")
        try:
            if len(parts) == 2:
                minutes, seconds = int(parts[0]), int(parts[1])
            elif len(parts) == 3:
                minutes = int(parts[0]) * 60 + int(parts[1])
                seconds = int(parts[2])
            else:
                minutes, seconds = 0, 0
        except ValueError:
            minutes, seconds = 0, 0

        clock_seconds = minutes * 60 + seconds

        if "basketball" in sport:
            total_periods = 4
            period_length = 12 * 60  # 12 min quarters (NBA)
            if "ncaa" in sport:
                total_periods = 2
                period_length = 20 * 60  # 20 min halves
            total_seconds = total_periods * period_length
            elapsed = (period - 1) * period_length + (period_length - clock_seconds)
            return max(0.0, min(1.0, 1.0 - elapsed / total_seconds))

        elif "football" in sport:
            total_seconds = 4 * 15 * 60  # 4 x 15min quarters
            elapsed = (period - 1) * 15 * 60 + (15 * 60 - clock_seconds)
            return max(0.0, min(1.0, 1.0 - elapsed / total_seconds))

        elif "baseball" in sport:
            # Baseball: 9 innings, use period/9
            return max(0.0, min(1.0, 1.0 - period / 9.0))

        return 0.5  # fallback

    # ── Scan mode: ESPN scores + Polymarket ──────────────────────────────────

    async def _scan_with_espn(self, queue: asyncio.Queue) -> None:
        """
        No Odds API key: use ESPN live scores to compute win probability,
        then compare to Polymarket prices for edge detection.
        """
        for market in self._poly_markets:
            try:
                opp = self._match_espn_to_polymarket(market)
                if opp and opp.raw_edge >= RAW_EDGE_THRESHOLD:
                    log.info(
                        f"EDGE FOUND  {opp.event_name} | "
                        f"{opp.our_side} edge={opp.raw_edge*100:.1f}% "
                        f"(us:{opp.our_prob:.3f} poly:{opp.poly_price:.3f}) "
                        f"[score: {opp.score_diff}, time: {opp.time_remaining_pct:.0%}]"
                    )
                    await queue.put(opp)
                elif opp:
                    self.state.skip_trade(
                        f"{opp.event_name}: edge {opp.raw_edge*100:.1f}% < {RAW_EDGE_THRESHOLD*100}%"
                    )
            except Exception as e:
                log.debug(f"Match error for {market.get('question', '')}: {e}")

    def _match_espn_to_polymarket(self, market: dict) -> Optional[Opportunity]:
        """Try to match a Polymarket market to an ESPN game and compute edge."""
        question = market["question"]
        q_lower = question.lower()

        # Try to find matching ESPN game
        game_state = None
        matched_sport = ""

        for sport, games in self._espn_cache.items():
            for key, gs in games.items():
                # Check if any team name from ESPN appears in the Polymarket question
                home_lower = gs["home_team"].lower()
                away_lower = gs["away_team"].lower()

                # Match on team name fragments (e.g. "Lakers" in "Will the Los Angeles Lakers win...")
                # Skip very common words that cause false matches
                skip_words = {"will", "team", "city", "state", "west", "east", "north", "south", "york", "angeles", "diego", "francisco", "antonio", "orleans", "jose"}
                home_words = [w for w in home_lower.split() if len(w) > 3 and w not in skip_words]
                away_words = [w for w in away_lower.split() if len(w) > 3 and w not in skip_words]

                home_match = any(w in q_lower for w in home_words) if home_words else False
                away_match = any(w in q_lower for w in away_words) if away_words else False

                if home_match or away_match:
                    game_state = gs
                    matched_sport = sport
                    break
            if game_state:
                break

        if not game_state:
            return None

        # Only interested in live or recently started games
        if game_state["state"] not in ("in", "post"):
            return None

        # Compute win probability from score + time
        score_diff = game_state["score_diff"]
        time_pct = game_state["time_remaining_pct"]

        sport_type = "basketball"
        if "football" in matched_sport:
            sport_type = "football"
        elif "baseball" in matched_sport:
            sport_type = "baseball"

        # Determine which team the Polymarket YES corresponds to
        # "Will X win..." -> YES = X wins
        home_in_question = any(
            w in q_lower for w in game_state["home_team"].lower().split() if len(w) > 3
        )

        if home_in_question:
            # YES = home team wins
            our_prob = in_game_win_prob(score_diff, time_pct, sport_type)
        else:
            # YES = away team wins
            our_prob = in_game_win_prob(-score_diff, time_pct, sport_type)

        poly_yes = market["yes_price"]
        poly_no = market["no_price"]

        # Check edge on YES side
        edge_yes = calculate_edge(our_prob, poly_yes)
        # Check edge on NO side
        edge_no = calculate_edge(1.0 - our_prob, poly_no)

        if abs(edge_yes) >= abs(edge_no) and edge_yes > 0:
            our_side = "YES"
            poly_price = poly_yes
            edge = edge_yes
            token_id = market["token_id_yes"]
        elif edge_no > 0:
            our_side = "NO"
            our_prob = 1.0 - our_prob
            poly_price = poly_no
            edge = edge_no
            token_id = market["token_id_no"]
        else:
            return Opportunity(
                timestamp=time.time(), event_name=question, sport=matched_sport,
                market_id=market["id"], token_id=market["token_id_yes"],
                our_side="YES", our_prob=our_prob, poly_price=poly_yes,
                raw_edge=max(edge_yes, edge_no), score_diff=score_diff,
                time_remaining_pct=time_pct, data_sources=1,
                source_detail=f"ESPN live score | {game_state['home_team']} {game_state['home_score']}-{game_state['away_score']} {game_state['away_team']}",
            )

        return Opportunity(
            timestamp=time.time(),
            event_name=question,
            sport=matched_sport,
            market_id=market["id"],
            token_id=token_id,
            our_side=our_side,
            our_prob=round(our_prob, 4),
            poly_price=poly_price,
            raw_edge=round(edge, 4),
            score_diff=score_diff,
            time_remaining_pct=time_pct,
            data_sources=1,
            source_detail=f"ESPN live | {game_state['home_team']} {game_state['home_score']}-{game_state['away_score']} {game_state['away_team']}",
        )

    # ── Scan mode: The Odds API + Polymarket ─────────────────────────────────

    async def _scan_with_odds_api(self, queue: asyncio.Queue) -> None:
        """Full mode: compare bookmaker odds to Polymarket prices."""
        for sport in SPORTS:
            events = await self._async_get(
                f"{ODDS_API_BASE}/sports/{sport}/odds",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": "us",
                    "markets": "h2h",
                    "oddsFormat": "american",
                },
            )
            if not events:
                continue

            for event in events:
                try:
                    opp = self._process_odds_event(event, sport)
                    if opp and opp.raw_edge >= RAW_EDGE_THRESHOLD:
                        log.info(
                            f"EDGE FOUND  {opp.event_name} | "
                            f"{opp.our_side} edge={opp.raw_edge*100:.1f}% "
                            f"(us:{opp.our_prob:.3f} poly:{opp.poly_price:.3f}) "
                            f"[{opp.data_sources} bookmakers]"
                        )
                        await queue.put(opp)
                    elif opp:
                        self.state.skip_trade(
                            f"{opp.event_name}: edge {opp.raw_edge*100:.1f}% < threshold"
                        )
                except Exception as e:
                    log.debug(f"Process event error: {e}")

    def _process_odds_event(self, event: dict, sport: str) -> Optional[Opportunity]:
        """Match an Odds API event to a Polymarket market and compute edge."""
        home = event.get("home_team", "")
        away = event.get("away_team", "")

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

        # Consensus (median) odds
        home_odds = sorted(home_odds_list)[len(home_odds_list) // 2]
        away_odds = sorted(away_odds_list)[len(away_odds_list) // 2]

        home_raw = american_to_prob(home_odds)
        away_raw = american_to_prob(away_odds)
        home_prob, away_prob = remove_vig(home_raw, away_raw)

        # Find matching Polymarket market
        poly_market = self._find_poly_market_for_teams(home, away)
        if not poly_market:
            return None

        poly_yes = poly_market["yes_price"]

        # Enrich with ESPN live score if available
        score_diff = 0
        time_pct = 0.5
        for sport_key, games in self._espn_cache.items():
            for key, gs in games.items():
                if (home.lower() in key or away.lower() in key) and gs["state"] == "in":
                    score_diff = gs["score_diff"]
                    time_pct = gs["time_remaining_pct"]
                    break

        our_prob = home_prob
        edge = calculate_edge(our_prob, poly_yes)

        if edge < 0 and abs(edge) >= RAW_EDGE_THRESHOLD:
            our_side = "NO"
            our_prob = away_prob
            poly_price = poly_market["no_price"]
            edge = calculate_edge(our_prob, poly_price)
            token_id = poly_market["token_id_no"]
        else:
            our_side = "YES"
            poly_price = poly_yes
            token_id = poly_market["token_id_yes"]

        if edge < RAW_EDGE_THRESHOLD:
            return None

        return Opportunity(
            timestamp=time.time(),
            event_name=f"{away} @ {home}",
            sport=sport,
            market_id=poly_market["id"],
            token_id=token_id,
            our_side=our_side,
            our_prob=round(our_prob, 4),
            poly_price=poly_price,
            raw_edge=round(edge, 4),
            score_diff=score_diff,
            time_remaining_pct=time_pct,
            data_sources=len(bookmakers),
            source_detail=f"{len(bookmakers)} bookmakers, {sport}",
        )

    def _find_poly_market_for_teams(self, home: str, away: str) -> Optional[dict]:
        """Fuzzy match team names to a Polymarket market question."""
        home_words = [w.lower() for w in home.split() if len(w) > 3]
        away_words = [w.lower() for w in away.split() if len(w) > 3]

        for market in self._poly_markets:
            q = market["question"].lower()
            home_match = any(w in q for w in home_words) if home_words else False
            away_match = any(w in q for w in away_words) if away_words else False
            if home_match or away_match:
                return market
        return None
