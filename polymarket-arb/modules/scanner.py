"""
modules/scanner.py -- Module 1: "Find the Gap"

Responsibilities:
  1. Fetch Pinnacle odds via The Odds API (EU region, bookmakers=pinnacle)
  2. De-vig using Power method to get fair probabilities
  3. Match to Polymarket markets via Gamma API (fuzzy team name matching)
  4. Stream Polymarket prices via CLOB WebSocket for real-time updates
  5. Calculate edge = pinnacle_fair_prob - polymarket_price
  6. Queue opportunities where edge > threshold

Data sources:
  - The Odds API (EU region, bookmakers=pinnacle): true probability
  - Polymarket Gamma API: market discovery, token IDs
  - Polymarket CLOB WebSocket: real-time order book prices
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

import httpx

from core.devig import american_to_decimal, devig_power
from core.models import Opportunity, ScanResult
from core.state import BotState

log = logging.getLogger("scanner")

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
POLY_GAMMA_BASE = "https://gamma-api.polymarket.com"
POLY_CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLY_CLOB_REST = "https://clob.polymarket.com"

# Sports to monitor -- must match The Odds API sport keys
ACTIVE_SPORTS = os.getenv(
    "ACTIVE_SPORTS",
    "basketball_nba,americanfootball_nfl,baseball_mlb,soccer_epl"
).split(",")

ODDS_API_POLL_INTERVAL = int(os.getenv("ODDS_API_POLL_INTERVAL", 600))  # 10 min

# Pre-scan edge threshold (gatekeeper applies stricter 10% gate)
RAW_EDGE_THRESHOLD = 0.08

# Team name aliases for fuzzy matching (kept from V1, works well)
TEAM_NICKNAMES: dict[str, list[str]] = {
    # NBA
    "los angeles lakers": ["lakers", "la lakers"],
    "boston celtics": ["celtics"],
    "golden state warriors": ["warriors", "gsw"],
    "brooklyn nets": ["nets", "brooklyn"],
    "charlotte hornets": ["hornets", "charlotte"],
    "new york knicks": ["knicks", "ny knicks"],
    "los angeles clippers": ["clippers", "la clippers"],
    "oklahoma city thunder": ["thunder", "okc"],
    "san antonio spurs": ["spurs"],
    "minnesota timberwolves": ["timberwolves", "wolves"],
    "portland trail blazers": ["trail blazers", "blazers"],
    "new orleans pelicans": ["pelicans"],
    "sacramento kings": ["kings"],
    "toronto raptors": ["raptors"],
    "milwaukee bucks": ["bucks"],
    "philadelphia 76ers": ["76ers", "sixers"],
    "miami heat": ["heat"],
    "denver nuggets": ["nuggets"],
    "phoenix suns": ["suns"],
    "dallas mavericks": ["mavericks", "mavs"],
    "memphis grizzlies": ["grizzlies"],
    "cleveland cavaliers": ["cavaliers", "cavs"],
    "atlanta hawks": ["hawks"],
    "chicago bulls": ["bulls"],
    "indiana pacers": ["pacers"],
    "detroit pistons": ["pistons"],
    "orlando magic": ["magic"],
    "washington wizards": ["wizards"],
    "houston rockets": ["rockets"],
    "utah jazz": ["jazz"],
    # NFL
    "kansas city chiefs": ["chiefs", "kc chiefs"],
    "san francisco 49ers": ["49ers", "niners"],
    "buffalo bills": ["bills"],
    "philadelphia eagles": ["eagles"],
    "dallas cowboys": ["cowboys"],
    "green bay packers": ["packers"],
    "baltimore ravens": ["ravens"],
    "detroit lions": ["lions"],
    "miami dolphins": ["dolphins"],
    "cincinnati bengals": ["bengals"],
    # MLB
    "new york yankees": ["yankees", "ny yankees"],
    "los angeles dodgers": ["dodgers", "la dodgers"],
    "houston astros": ["astros"],
    "atlanta braves": ["braves"],
    "boston red sox": ["red sox"],
    "chicago cubs": ["cubs"],
    # EPL
    "manchester city": ["man city"],
    "manchester united": ["man united", "man utd"],
    "arsenal": ["arsenal fc"],
    "liverpool": ["liverpool fc"],
    "tottenham hotspur": ["tottenham", "spurs"],
    "chelsea": ["chelsea fc"],
}


class ScannerModule:
    def __init__(self, state: BotState):
        self.state = state
        self._poly_market_cache: dict[str, dict] = {}
        self._last_poly_refresh = 0.0
        self._ws_prices: dict[str, dict] = {}  # token_id -> {price, bid, ask, ...}
        self._ws_task: Optional[asyncio.Task] = None
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def close(self) -> None:
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    # -- Main scan cycle -------------------------------------------------

    async def scan(self, queue: asyncio.Queue) -> None:
        """Run one scan cycle: fetch Pinnacle odds, compare to Polymarket, queue gaps."""
        if not ODDS_API_KEY:
            log.warning("ODDS_API_KEY not set -- running demo scan")
            await self._demo_scan(queue)
            return

        await self._refresh_poly_markets()

        # Auto-start WebSocket streaming for all known token IDs
        if self._ws_task is None and self._poly_market_cache:
            token_ids = []
            for market in self._poly_market_cache.values():
                for token in market.get("tokens", []):
                    tid = token.get("token_id", "")
                    if tid:
                        token_ids.append(tid)
            if token_ids:
                await self.start_ws_stream(token_ids)

        for sport in ACTIVE_SPORTS:
            sport = sport.strip()
            if not sport:
                continue
            try:
                opportunities = await self._scan_sport(sport)
                for opp in opportunities:
                    if self.state.already_traded(opp.event_name):
                        self.state.skip_trade(f"{opp.event_name}: already traded")
                        continue
                    if opp.edge >= RAW_EDGE_THRESHOLD:
                        log.info(
                            f"GAP FOUND  {opp.event_name} | "
                            f"{opp.our_side} edge={opp.edge * 100:.1f}% "
                            f"(pinnacle:{opp.pinnacle_fair_prob:.3f} poly:{opp.polymarket_price:.3f})"
                        )
                        await queue.put(opp)
                    else:
                        self.state.skip_trade(
                            f"{opp.event_name}: edge {opp.edge * 100:.1f}% < {RAW_EDGE_THRESHOLD * 100}%"
                        )
            except Exception as e:
                log.error(f"Scan error for {sport}: {e}")

    # -- Fetch Pinnacle odds ---------------------------------------------

    async def _scan_sport(self, sport: str) -> list[Opportunity]:
        """Fetch Pinnacle odds for one sport via The Odds API, compute edges."""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{ODDS_API_BASE}/sports/{sport}/odds",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": "eu",
                    "bookmakers": "pinnacle",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            log.warning(f"Odds API error ({sport}): {e}")
            return []

        opportunities = []
        for event in events:
            opp, scan_result = self._process_event(event, sport)

            if opp:
                # Enrich with order book data for real markets
                if not opp.token_id.startswith("demo-"):
                    book = await self.fetch_order_book(opp.token_id)
                    opp.best_bid = book["best_bid"]
                    opp.best_ask = book["best_ask"]
                    opp.bid_ask_spread = book["spread"]
                    opp.order_book_depth = book["depth_at_mid"]
                    opp.polymarket_liquidity = book["total_bid_depth"] + book["total_ask_depth"]

                    # Update price from live book if available
                    if opp.our_side == "YES" and book["best_ask"] > 0:
                        opp.polymarket_price = book["best_ask"]
                        opp.edge = opp.pinnacle_fair_prob - opp.polymarket_price

                    # Update scan result with book data
                    if scan_result:
                        scan_result.best_bid = opp.best_bid
                        scan_result.best_ask = opp.best_ask
                        scan_result.spread = opp.bid_ask_spread
                        scan_result.liquidity = opp.polymarket_liquidity
                        scan_result.edge = opp.edge

                opportunities.append(opp)

            # Record scan result for every game evaluated
            if scan_result:
                self.state.record_scan(scan_result)

        return opportunities

    def _process_event(self, event: dict, sport: str) -> tuple[Optional[Opportunity], Optional[ScanResult]]:
        """Extract Pinnacle odds from event, de-vig, compare to Polymarket.

        Returns (opportunity_or_None, scan_result) -- scan_result is always
        produced for every Pinnacle event so the dashboard can show all games.
        """
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        event_name = f"{away} @ {home}"

        # Get Pinnacle odds (should be only bookmaker since we filtered)
        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            return None, None

        home_odds = None
        away_odds = None
        for bk in bookmakers:
            if bk.get("key") != "pinnacle":
                continue
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for outcome in mkt.get("outcomes", []):
                    if outcome.get("name") == home:
                        home_odds = outcome.get("price")
                    elif outcome.get("name") == away:
                        away_odds = outcome.get("price")

        if home_odds is None or away_odds is None:
            return None, None

        # De-vig using Power method
        home_fair, away_fair = devig_power(home_odds, away_odds)

        # Base scan result (always created for dashboard)
        scan = ScanResult(
            timestamp=time.time(),
            event_name=event_name,
            sport=sport,
            home_team=home,
            away_team=away,
            pinnacle_home_odds=home_odds,
            pinnacle_away_odds=away_odds,
            pinnacle_home_prob=home_fair,
            pinnacle_away_prob=away_fair,
        )

        # Find matching Polymarket market
        poly_info = self._find_poly_market(home, away)
        if not poly_info:
            scan.action = "no_match"
            return None, scan

        poly_yes_price, yes_token_id, poly_no_price, no_token_id = self._get_poly_prices(poly_info)
        if poly_yes_price is None:
            scan.action = "no_match"
            return None, scan

        scan.poly_matched = True
        scan.polymarket_price = poly_yes_price

        # Check WebSocket prices if available (more up-to-date)
        ws_data = self._ws_prices.get(yes_token_id)
        if ws_data:
            poly_yes_price = ws_data.get("price", poly_yes_price)
            scan.polymarket_price = poly_yes_price

        # Determine which side has edge
        # YES token = home team win on most Polymarket sports markets
        home_edge = home_fair - poly_yes_price
        away_edge = away_fair - (1.0 - poly_yes_price)
        best_edge = max(home_edge, away_edge)
        scan.edge = best_edge
        scan.our_side = "YES" if home_edge >= away_edge else "NO"

        if home_edge >= away_edge and home_edge >= RAW_EDGE_THRESHOLD:
            scan.action = "queued"
            opp = Opportunity(
                timestamp=time.time(),
                event_name=event_name,
                sport=sport,
                home_team=home,
                away_team=away,
                market_id=poly_info.get("id", ""),
                token_id=yes_token_id,
                our_side="YES",
                pinnacle_fair_prob=home_fair,
                polymarket_price=poly_yes_price,
                edge=home_edge,
                pinnacle_home_prob=home_fair,
                pinnacle_away_prob=away_fair,
                best_bid=ws_data.get("best_bid", 0.0) if ws_data else 0.0,
                best_ask=ws_data.get("best_ask", 0.0) if ws_data else 0.0,
                bid_ask_spread=ws_data.get("spread", 0.0) if ws_data else 0.0,
            )
            return opp, scan
        elif away_edge >= RAW_EDGE_THRESHOLD:
            scan.action = "queued"
            poly_no = 1.0 - poly_yes_price
            opp = Opportunity(
                timestamp=time.time(),
                event_name=event_name,
                sport=sport,
                home_team=home,
                away_team=away,
                market_id=poly_info.get("id", ""),
                token_id=no_token_id if no_token_id else yes_token_id,
                our_side="NO",
                pinnacle_fair_prob=away_fair,
                polymarket_price=poly_no,
                edge=away_edge,
                pinnacle_home_prob=home_fair,
                pinnacle_away_prob=away_fair,
                best_bid=0.0,
                best_ask=0.0,
                bid_ask_spread=0.0,
            )
            return opp, scan

        scan.action = "skipped"
        return None, scan

    # -- Polymarket market discovery (Gamma API) -------------------------

    async def _refresh_poly_markets(self) -> None:
        """Cache Polymarket sports markets from Gamma API. Refresh every 5 minutes."""
        if time.time() - self._last_poly_refresh < 300:
            return
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{POLY_GAMMA_BASE}/markets",
                params={"tag": "sports", "active": "true", "limit": 200},
            )
            resp.raise_for_status()
            markets = resp.json()
            self._poly_market_cache = {}
            for m in markets:
                key = self._normalise_name(m.get("question", ""))
                self._poly_market_cache[key] = {
                    "id": m.get("id", ""),
                    "question": m.get("question", ""),
                    "tokens": m.get("tokens", []),
                    "active": m.get("active", False),
                    "volume": float(m.get("volume", 0) or 0),
                }
            self._last_poly_refresh = time.time()
            log.info(f"Polymarket cache refreshed: {len(self._poly_market_cache)} sports markets")
        except Exception as e:
            log.error(f"Failed to refresh Polymarket markets: {e}")

    def _find_poly_market(self, home: str, away: str) -> Optional[dict]:
        """Fuzzy-match a sports event to a cached Polymarket market.

        Uses whole-word boundary matching to avoid false positives
        (e.g. "nets" inside "hornets").
        """
        home_lower = home.lower()
        away_lower = away.lower()

        # Build search terms including nicknames
        home_terms = [home_lower] + self._get_nicknames(home_lower)
        away_terms = [away_lower] + self._get_nicknames(away_lower)

        for key, market in self._poly_market_cache.items():
            home_match = any(self._whole_word_match(t, key) for t in home_terms)
            away_match = any(self._whole_word_match(t, key) for t in away_terms)
            if home_match and away_match:
                return market

        return None

    @staticmethod
    def _whole_word_match(term: str, text: str) -> bool:
        """Match term as a whole word in text (avoids 'nets' matching 'hornets')."""
        return bool(re.search(r'\b' + re.escape(term) + r'\b', text))

    @staticmethod
    def _get_nicknames(team_name: str) -> list[str]:
        """Get alternative names for a team."""
        for full_name, aliases in TEAM_NICKNAMES.items():
            if team_name in full_name or full_name in team_name:
                return aliases
            if any(team_name in a or a in team_name for a in aliases):
                return [full_name] + aliases
        return []

    def _get_poly_prices(self, market: dict) -> tuple[Optional[float], str, Optional[float], str]:
        """Get YES and NO prices and token IDs from a Polymarket market.

        Returns (yes_price, yes_token_id, no_price, no_token_id).
        """
        tokens = market.get("tokens", [])
        yes_price, yes_token = None, ""
        no_price, no_token = None, ""

        for token in tokens:
            outcome = token.get("outcome", "").upper()
            price = token.get("price")
            tid = token.get("token_id", "")
            if outcome == "YES" and price is not None:
                yes_price = float(price)
                yes_token = tid
            elif outcome == "NO" and price is not None:
                no_price = float(price)
                no_token = tid

        return yes_price, yes_token, no_price, no_token

    @staticmethod
    def _normalise_name(name: str) -> str:
        return name.lower().strip()

    # -- Order book depth (Polymarket CLOB REST) -------------------------

    async def fetch_order_book(self, token_id: str) -> dict:
        """Fetch order book for a token from Polymarket CLOB REST API.

        Returns dict with keys: best_bid, best_ask, spread, depth_at_mid,
        total_bid_depth, total_ask_depth.
        """
        if token_id.startswith("demo-"):
            return {"best_bid": 0.0, "best_ask": 0.0, "spread": 0.0,
                    "depth_at_mid": 0.0, "total_bid_depth": 0.0, "total_ask_depth": 0.0}

        client = await self._get_client()
        try:
            resp = await client.get(
                f"{POLY_CLOB_REST}/book",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            book = resp.json()

            bids = book.get("bids", [])
            asks = book.get("asks", [])

            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(asks[0]["price"]) if asks else 0.0
            spread = best_ask - best_bid if best_bid > 0 and best_ask > 0 else 0.0

            # Calculate depth: total size available on each side
            total_bid_depth = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids)
            total_ask_depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks)

            # Depth at mid: how much liquidity within 2 cents of best bid/ask
            mid = (best_bid + best_ask) / 2 if spread > 0 else 0.0
            depth_near_mid = (
                sum(float(b.get("size", 0)) * float(b.get("price", 0))
                    for b in bids if float(b.get("price", 0)) >= mid - 0.02)
                + sum(float(a.get("size", 0)) * float(a.get("price", 0))
                      for a in asks if float(a.get("price", 0)) <= mid + 0.02)
            )

            result = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": round(spread, 4),
                "depth_at_mid": round(depth_near_mid, 2),
                "total_bid_depth": round(total_bid_depth, 2),
                "total_ask_depth": round(total_ask_depth, 2),
            }

            # Cache in ws_prices for consistency
            self._ws_prices[token_id] = {
                "price": (best_bid + best_ask) / 2 if spread > 0 else best_bid or best_ask,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "depth": depth_near_mid,
            }

            return result

        except Exception as e:
            log.debug(f"Order book fetch failed for {token_id}: {e}")
            return {"best_bid": 0.0, "best_ask": 0.0, "spread": 0.0,
                    "depth_at_mid": 0.0, "total_bid_depth": 0.0, "total_ask_depth": 0.0}

    # -- WebSocket price streaming (Polymarket CLOB) ---------------------

    async def start_ws_stream(self, token_ids: list[str]) -> None:
        """Start a WebSocket connection to stream real-time Polymarket prices."""
        if not token_ids:
            return
        self._ws_task = asyncio.create_task(self._ws_loop(token_ids))

    async def _ws_loop(self, token_ids: list[str]) -> None:
        """WebSocket streaming loop. Reconnects on failure."""
        try:
            import websockets
        except ImportError:
            log.warning("websockets not installed -- skipping real-time price stream")
            return

        while True:
            try:
                async with websockets.connect(POLY_CLOB_WS) as ws:
                    # Subscribe to markets
                    for tid in token_ids:
                        sub_msg = json.dumps({
                            "type": "subscribe",
                            "channel": "market",
                            "market": tid,
                        })
                        await ws.send(sub_msg)

                    log.info(f"WebSocket connected, streaming {len(token_ids)} markets")

                    async for message in ws:
                        try:
                            data = json.loads(message)
                            self._handle_ws_message(data)
                        except json.JSONDecodeError:
                            continue

            except Exception as e:
                log.warning(f"WebSocket error: {e} -- reconnecting in 5s")
                await asyncio.sleep(5)

    def _handle_ws_message(self, data: dict) -> None:
        """Process incoming WebSocket price/book update."""
        # Handle different message types from Polymarket WS
        msg_type = data.get("type", "")

        if msg_type == "book":
            # Full book snapshot
            token_id = data.get("asset_id") or data.get("market")
            if not token_id:
                return
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            entry = self._ws_prices.setdefault(token_id, {})
            if bids:
                entry["best_bid"] = float(bids[0].get("price", 0))
            if asks:
                entry["best_ask"] = float(asks[0].get("price", 0))
            if "best_bid" in entry and "best_ask" in entry:
                entry["spread"] = entry["best_ask"] - entry["best_bid"]
                entry["price"] = (entry["best_bid"] + entry["best_ask"]) / 2
            # Calculate depth
            entry["depth"] = (
                sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids[:10])
                + sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks[:10])
            )
            return

        # Handle price tick updates
        token_id = data.get("asset_id") or data.get("market")
        if not token_id:
            return

        entry = self._ws_prices.setdefault(token_id, {})
        price = data.get("price")
        best_bid = data.get("best_bid")
        best_ask = data.get("best_ask")

        if price is not None:
            entry["price"] = float(price)
        if best_bid is not None:
            entry["best_bid"] = float(best_bid)
        if best_ask is not None:
            entry["best_ask"] = float(best_ask)
        if "best_bid" in entry and "best_ask" in entry:
            entry["spread"] = entry["best_ask"] - entry["best_bid"]

    # -- Demo mode -------------------------------------------------------

    async def _demo_scan(self, queue: asyncio.Queue) -> None:
        """Emit fake opportunities for pipeline testing without API keys."""
        if os.getenv("DEMO_MODE", "true").lower() != "true":
            return

        import random

        # Always generate scan results for the dashboard, even when no edge found
        fake_scenarios = [
            {
                "event": "Toronto Raptors @ Boston Celtics",
                "sport": "basketball_nba",
                "home": "Boston Celtics",
                "away": "Toronto Raptors",
                "home_fair": 0.755,
                "away_fair": 0.245,
                "home_odds": 1.32,
                "away_odds": 4.08,
                "poly_price": 0.63,
                "side": "YES",
            },
            {
                "event": "Houston Astros @ New York Yankees",
                "sport": "baseball_mlb",
                "home": "New York Yankees",
                "away": "Houston Astros",
                "home_fair": 0.62,
                "away_fair": 0.38,
                "home_odds": 1.61,
                "away_odds": 2.63,
                "poly_price": 0.48,
                "side": "YES",
            },
            {
                "event": "Buffalo Bills @ Kansas City Chiefs",
                "sport": "americanfootball_nfl",
                "home": "Kansas City Chiefs",
                "away": "Buffalo Bills",
                "home_fair": 0.68,
                "away_fair": 0.32,
                "home_odds": 1.47,
                "away_odds": 3.13,
                "poly_price": 0.55,
                "side": "YES",
            },
        ]

        # Also emit "no edge" games for realism in the scan log
        no_edge_games = [
            {
                "event": "Miami Heat @ Milwaukee Bucks",
                "sport": "basketball_nba",
                "home": "Milwaukee Bucks",
                "away": "Miami Heat",
                "home_fair": 0.61,
                "away_fair": 0.39,
                "home_odds": 1.64,
                "away_odds": 2.56,
                "poly_price": 0.60,
            },
            {
                "event": "Chicago Cubs @ Atlanta Braves",
                "sport": "baseball_mlb",
                "home": "Atlanta Braves",
                "away": "Chicago Cubs",
                "home_fair": 0.58,
                "away_fair": 0.42,
                "home_odds": 1.72,
                "away_odds": 2.38,
                "poly_price": 0.57,
            },
            {
                "event": "Arsenal @ Liverpool",
                "sport": "soccer_epl",
                "home": "Liverpool",
                "away": "Arsenal",
                "home_fair": 0.45,
                "away_fair": 0.55,
                "home_odds": 2.22,
                "away_odds": 1.82,
                "poly_price": 0.44,
            },
        ]

        # Record all "no edge" games in scan log
        for game in no_edge_games:
            edge = game["home_fair"] - game["poly_price"]
            scan = ScanResult(
                timestamp=time.time(),
                event_name=game["event"],
                sport=game["sport"],
                home_team=game["home"],
                away_team=game["away"],
                pinnacle_home_odds=game["home_odds"],
                pinnacle_away_odds=game["away_odds"],
                pinnacle_home_prob=game["home_fair"],
                pinnacle_away_prob=game["away_fair"],
                polymarket_price=game["poly_price"],
                poly_matched=True,
                our_side="YES" if edge > 0 else "NO",
                edge=edge,
                action="skipped",
            )
            self.state.record_scan(scan)

        if random.random() > 0.3:  # 30% chance of finding something with edge
            return

        scenario = random.choice(fake_scenarios)
        fair_prob = scenario["home_fair"] if scenario["side"] == "YES" else scenario["away_fair"]
        edge = fair_prob - scenario["poly_price"]

        # Record in scan log as "queued"
        scan = ScanResult(
            timestamp=time.time(),
            event_name=f"DEMO {scenario['event']}",
            sport=scenario["sport"],
            home_team=scenario["home"],
            away_team=scenario["away"],
            pinnacle_home_odds=scenario["home_odds"],
            pinnacle_away_odds=scenario["away_odds"],
            pinnacle_home_prob=scenario["home_fair"],
            pinnacle_away_prob=scenario["away_fair"],
            polymarket_price=scenario["poly_price"],
            poly_matched=True,
            our_side=scenario["side"],
            edge=edge,
            action="queued",
        )
        self.state.record_scan(scan)

        opp = Opportunity(
            timestamp=time.time(),
            event_name=f"DEMO {scenario['event']}",
            sport=scenario["sport"],
            home_team=scenario["home"],
            away_team=scenario["away"],
            market_id=f"demo-market-{int(time.time())}",
            token_id=f"demo-token-{int(time.time())}",
            our_side=scenario["side"],
            pinnacle_fair_prob=fair_prob,
            polymarket_price=scenario["poly_price"],
            edge=edge,
            pinnacle_home_prob=scenario["home_fair"],
            pinnacle_away_prob=scenario["away_fair"],
            polymarket_liquidity=25000.0,
            bid_ask_spread=0.02,
            best_bid=scenario["poly_price"] - 0.01,
            best_ask=scenario["poly_price"] + 0.01,
            order_book_depth=10000.0,
        )
        log.info(f"DEMO -- emitting fake opportunity: {opp.event_name} edge={opp.edge * 100:.1f}%")
        await queue.put(opp)
