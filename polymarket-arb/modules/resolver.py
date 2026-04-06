"""
modules/resolver.py -- Module 4: "Did We Win?"

Kept from V1: ESPN outcome resolution (the strongest part of the original design).
New in V2: CLV capture at game time, Polymarket resolution cross-check.

ESPN is used ONLY for outcome resolution -- never for probability estimation.
Polls every 60 seconds for finished games, matches to pending trades, resolves P&L.
"""

import logging
import os
import re
import time
from typing import Optional

import httpx

from core.devig import devig_power
from core.models import TradeRecord
from core.state import BotState

log = logging.getLogger("resolver")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_POLL_INTERVAL = int(os.getenv("ESPN_POLL_INTERVAL", 60))

# ESPN sport/league endpoint mapping
ESPN_ENDPOINTS: dict[str, str] = {
    "basketball_nba": "basketball/nba",
    "americanfootball_nfl": "football/nfl",
    "americanfootball_ncaaf": "football/college-football",
    "basketball_ncaab": "basketball/mens-college-basketball",
    "baseball_mlb": "baseball/mlb",
    "soccer_epl": "soccer/eng.1",
}

# Team nickname mapping for matching ESPN names to trade records.
# Uses whole-word boundary matching to avoid "nets" in "hornets" etc.
TEAM_NICKNAMES: dict[str, list[str]] = {
    "lakers": ["los angeles lakers", "la lakers"],
    "celtics": ["boston celtics"],
    "warriors": ["golden state warriors", "gsw"],
    "nets": ["brooklyn nets"],
    "hornets": ["charlotte hornets"],
    "knicks": ["new york knicks"],
    "clippers": ["los angeles clippers", "la clippers"],
    "thunder": ["oklahoma city thunder", "okc"],
    "spurs": ["san antonio spurs"],
    "timberwolves": ["minnesota timberwolves"],
    "trail blazers": ["portland trail blazers", "blazers"],
    "pelicans": ["new orleans pelicans"],
    "kings": ["sacramento kings"],
    "raptors": ["toronto raptors"],
    "bucks": ["milwaukee bucks"],
    "76ers": ["philadelphia 76ers", "sixers"],
    "heat": ["miami heat"],
    "nuggets": ["denver nuggets"],
    "suns": ["phoenix suns"],
    "mavericks": ["dallas mavericks", "mavs"],
    "grizzlies": ["memphis grizzlies"],
    "cavaliers": ["cleveland cavaliers", "cavs"],
    "hawks": ["atlanta hawks"],
    "bulls": ["chicago bulls"],
    "pacers": ["indiana pacers"],
    "pistons": ["detroit pistons"],
    "magic": ["orlando magic"],
    "wizards": ["washington wizards"],
    "rockets": ["houston rockets"],
    "jazz": ["utah jazz"],
    "chiefs": ["kansas city chiefs"],
    "49ers": ["san francisco 49ers", "niners"],
    "bills": ["buffalo bills"],
    "eagles": ["philadelphia eagles"],
    "cowboys": ["dallas cowboys"],
    "packers": ["green bay packers"],
    "ravens": ["baltimore ravens"],
    "lions": ["detroit lions"],
    "dolphins": ["miami dolphins"],
    "bengals": ["cincinnati bengals"],
    "yankees": ["new york yankees"],
    "dodgers": ["los angeles dodgers"],
    "astros": ["houston astros"],
    "braves": ["atlanta braves"],
    "red sox": ["boston red sox"],
    "cubs": ["chicago cubs"],
}


ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"


class ResolverModule:
    def __init__(self, state: BotState):
        self.state = state
        self._http_client: Optional[httpx.AsyncClient] = None
        self._closing_lines_captured: set[str] = set()  # trade_ids already captured

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def close(self) -> None:
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def check_outcomes(self) -> None:
        """Poll ESPN for finished games and resolve matching trades.

        Also captures closing Pinnacle lines for CLV tracking on
        games that are currently in progress.
        """
        open_trades = self.state.open_trades
        if not open_trades:
            return

        # Determine which leagues to check based on open trades
        active_sports = set(t.sport for t in open_trades)

        for sport in active_sports:
            espn_path = ESPN_ENDPOINTS.get(sport)
            if not espn_path:
                continue

            try:
                games = await self._fetch_espn_scoreboard(espn_path)
                for game in games:
                    matching_trades = self._match_trades_to_game(game, open_trades)
                    if not matching_trades:
                        continue

                    # Capture closing lines for games that just started (in progress)
                    if self._is_game_in_progress(game):
                        await self._capture_closing_lines(sport, matching_trades)

                    # Resolve finished games
                    if self._is_game_finished(game):
                        winner = self._determine_winner(game)
                        if not winner:
                            continue
                        for trade in matching_trades:
                            self._resolve_trade(trade, winner)

            except Exception as e:
                log.error(f"ESPN resolution error for {sport}: {e}")

    # -- Closing line capture (CLV tracking) -----------------------------

    async def _capture_closing_lines(self, sport: str, trades: list[TradeRecord]) -> None:
        """Fetch current Pinnacle odds and store as closing line for CLV.

        Called when ESPN shows a game is in progress. We capture the
        Pinnacle line at game start (or close to it) to measure CLV.
        """
        if not ODDS_API_KEY:
            return

        # Skip trades we've already captured closing lines for
        uncaptured = [t for t in trades if t.trade_id not in self._closing_lines_captured
                      and t.pinnacle_prob_at_close is None]
        if not uncaptured:
            return

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
            log.warning(f"Failed to fetch closing lines for {sport}: {e}")
            return

        for trade in uncaptured:
            for event in events:
                home = event.get("home_team", "").lower()
                away = event.get("away_team", "").lower()

                # Match event to trade
                trade_event = trade.event_name.lower()
                if not (_whole_word_in(home, trade_event) or _whole_word_in(away, trade_event)):
                    continue

                # Extract Pinnacle odds
                home_odds, away_odds = self._extract_pinnacle_odds(event)
                if home_odds is None:
                    continue

                # De-vig to get fair closing probability
                home_fair, away_fair = devig_power(home_odds, away_odds)

                # Set closing line based on which side we bet
                if trade.side == "YES":
                    trade.pinnacle_prob_at_close = home_fair
                else:
                    trade.pinnacle_prob_at_close = away_fair

                self._closing_lines_captured.add(trade.trade_id)
                clv = trade.clv
                log.info(
                    f"CLV captured {trade.event_name} | "
                    f"close={trade.pinnacle_prob_at_close:.3f} "
                    f"entry={trade.entry_price:.3f} "
                    f"CLV={clv * 100:+.1f}%" if clv is not None else ""
                )
                break

    @staticmethod
    def _extract_pinnacle_odds(event: dict) -> tuple[Optional[float], Optional[float]]:
        """Extract Pinnacle home/away decimal odds from an Odds API event."""
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        home_odds = None
        away_odds = None

        for bk in event.get("bookmakers", []):
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

        return home_odds, away_odds

    async def _fetch_espn_scoreboard(self, espn_path: str) -> list[dict]:
        """Fetch today's scoreboard from ESPN."""
        client = await self._get_client()
        try:
            resp = await client.get(f"{ESPN_BASE}/{espn_path}/scoreboard")
            resp.raise_for_status()
            data = resp.json()
            return data.get("events", [])
        except Exception as e:
            log.warning(f"ESPN fetch error ({espn_path}): {e}")
            return []

    def _is_game_in_progress(self, game: dict) -> bool:
        """Check if an ESPN game event is currently in progress."""
        status = game.get("status", {})
        state = status.get("type", {}).get("state", "")
        return state == "in"

    def _is_game_finished(self, game: dict) -> bool:
        """Check if an ESPN game event is finished."""
        status = game.get("status", {})
        state = status.get("type", {}).get("state", "")
        return state == "post"

    def _determine_winner(self, game: dict) -> Optional[str]:
        """Determine the winning team from an ESPN game event.

        Returns the full team name of the winner, or None if can't determine.
        """
        competitions = game.get("competitions", [])
        if not competitions:
            return None

        competitors = competitions[0].get("competitors", [])
        if len(competitors) < 2:
            return None

        # ESPN returns competitors with "winner" field
        for comp in competitors:
            if comp.get("winner"):
                team = comp.get("team", {})
                return team.get("displayName", "").lower()

        # Fallback: compare scores
        scores = []
        for comp in competitors:
            team_name = comp.get("team", {}).get("displayName", "").lower()
            score = int(comp.get("score", 0))
            scores.append((team_name, score))

        if len(scores) == 2 and scores[0][1] != scores[1][1]:
            return max(scores, key=lambda x: x[1])[0]

        return None

    def _match_trades_to_game(self, game: dict, trades: list[TradeRecord]) -> list[TradeRecord]:
        """Match a finished ESPN game to open trades using team name matching."""
        competitions = game.get("competitions", [])
        if not competitions:
            return []

        competitors = competitions[0].get("competitors", [])
        game_teams = set()
        for comp in competitors:
            team_name = comp.get("team", {}).get("displayName", "").lower()
            if team_name:
                game_teams.add(team_name)
                # Add nicknames
                for nick, aliases in TEAM_NICKNAMES.items():
                    if team_name in aliases or nick in team_name:
                        game_teams.add(nick)
                        game_teams.update(a.lower() for a in aliases)

        matched = []
        for trade in trades:
            trade_event = trade.event_name.lower()
            # Check if both teams from the game appear in the trade event name
            team_matches = sum(
                1 for t in game_teams
                if _whole_word_in(t, trade_event)
            )
            # Need at least 2 matches (both teams)
            if team_matches >= 2:
                matched.append(trade)

        return matched

    def _resolve_trade(self, trade: TradeRecord, winner: str) -> None:
        """Resolve a trade based on the game winner."""
        trade_team = trade.team.lower()

        # Check if our team won using fuzzy matching
        our_team_won = _teams_match(trade_team, winner)

        if our_team_won:
            # We hold shares that pay $1 each
            profit = (1.0 - trade.entry_price) * trade.shares
            outcome = "won"
        else:
            # We lose our entry cost
            profit = -(trade.entry_price * trade.shares)
            outcome = "lost"

        key = trade.order_id or trade.trade_id
        self.state.resolve_trade(key, outcome, round(profit, 2))

    # -- Demo resolution -------------------------------------------------

    async def demo_resolve(self) -> None:
        """In demo mode, auto-resolve trades after a delay based on probability."""
        import random

        open_trades = self.state.open_trades
        for trade in open_trades:
            if trade.status != "demo":
                continue

            # Resolve after ~60 seconds
            age = time.time() - trade.entry_time
            if age < 60:
                continue

            # Win based on Pinnacle probability
            won = random.random() < trade.pinnacle_prob_at_entry
            if won:
                pnl = round((1.0 - trade.entry_price) * trade.shares, 2)
                outcome = "won"
            else:
                pnl = round(-(trade.entry_price * trade.shares), 2)
                outcome = "lost"

            key = trade.order_id or trade.trade_id
            self.state.resolve_trade(key, outcome, pnl)
            log.info(f"[DEMO] Resolved {trade.event_name}: {outcome} P&L ${pnl:+.2f}")


def _whole_word_in(term: str, text: str) -> bool:
    """Match term as whole word in text."""
    return bool(re.search(r'\b' + re.escape(term) + r'\b', text))


def _teams_match(team_a: str, team_b: str) -> bool:
    """Check if two team names refer to the same team."""
    if team_a in team_b or team_b in team_a:
        return True

    # Check via nicknames
    a_names = {team_a}
    b_names = {team_b}

    for nick, aliases in TEAM_NICKNAMES.items():
        all_names = [nick] + [a.lower() for a in aliases]
        if any(n in team_a or team_a in n for n in all_names):
            a_names.update(all_names)
        if any(n in team_b or team_b in n for n in all_names):
            b_names.update(all_names)

    return bool(a_names & b_names)
