"""
agents/outcome_checker.py — Resolves pending trades against actual game outcomes

Polls ESPN for finished games ("post" state) and closes trades
based on which team actually won. No fake P&L — only real results.
"""

import asyncio
import logging
import time
from typing import Optional

import requests

from core.state import BotState, Trade

log = logging.getLogger("outcomes")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

ESPN_ENDPOINTS = {
    "basketball_nba":      "basketball/nba",
    "americanfootball_nfl": "football/nfl",
    "basketball_ncaab":    "basketball/mens-college-basketball",
    "baseball_mlb":        "baseball/mlb",
    "icehockey_nhl":       "hockey/nhl",
    "soccer_epl":          "soccer/eng.1",
    "soccer_usa_mls":      "soccer/usa.1",
    "soccer_spain_la_liga": "soccer/esp.1",
    "soccer_germany_bundesliga": "soccer/ger.1",
    "soccer_italy_serie_a": "soccer/ita.1",
    "soccer_france_ligue_one": "soccer/fra.1",
    "soccer_uefa_champs_league": "soccer/uefa.champions",
}


class OutcomeChecker:
    """Checks finished games and resolves pending trades with real P&L."""

    def __init__(self, state: BotState):
        self.state = state
        self._finished_games: dict = {}  # cache: "home|away" -> winner team name
        self._last_poll = 0.0

    async def check_outcomes(self) -> None:
        """Poll ESPN for finished games, resolve any matching pending trades."""
        # Poll ESPN every 30 seconds
        if time.time() - self._last_poll < 30:
            return
        self._last_poll = time.time()

        await self._refresh_finished_games()
        self._resolve_pending_trades()

    async def _refresh_finished_games(self) -> None:
        """Fetch all finished games from ESPN across all sports."""
        loop = asyncio.get_event_loop()

        for sport, endpoint in ESPN_ENDPOINTS.items():
            try:
                resp = await loop.run_in_executor(
                    None,
                    lambda ep=endpoint: requests.get(
                        f"{ESPN_BASE}/{ep}/scoreboard", timeout=10
                    )
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.debug(f"ESPN poll failed for {sport}: {e}")
                continue

            for event in data.get("events", []):
                status = event.get("status", {}).get("type", {})
                state = status.get("state", "")

                if state != "post":
                    continue

                comps = event.get("competitions", [])
                if not comps:
                    continue

                comp = comps[0]
                home_team = away_team = ""
                home_score = away_score = 0
                winner = ""

                for competitor in comp.get("competitors", []):
                    team_name = competitor.get("team", {}).get("displayName", "")
                    score = int(competitor.get("score", "0") or "0")
                    is_winner = competitor.get("winner", False)

                    if competitor.get("homeAway") == "home":
                        home_team = team_name
                        home_score = score
                    else:
                        away_team = team_name
                        away_score = score

                    if is_winner:
                        winner = team_name

                # Fallback: determine winner from score if "winner" field missing
                if not winner:
                    if home_score > away_score:
                        winner = home_team
                    elif away_score > home_score:
                        winner = away_team
                    else:
                        continue  # tie — skip

                if home_team and away_team and winner:
                    key = f"{home_team}|{away_team}"
                    if key not in self._finished_games:
                        log.info(
                            f"FINAL: {away_team} {away_score} @ {home_team} {home_score} "
                            f"— Winner: {winner}"
                        )
                    self._finished_games[key] = {
                        "home_team": home_team,
                        "away_team": away_team,
                        "home_score": home_score,
                        "away_score": away_score,
                        "winner": winner,
                        "sport": sport,
                    }

    def _resolve_pending_trades(self) -> None:
        """Match pending trades to finished games and calculate real P&L."""
        # Get list of pending trades (copy to avoid mutation during iteration)
        pending = {
            oid: trade for oid, trade in self.state._open_trades.items()
            if trade.status == "pending_outcome"
        }

        if not pending:
            return

        for order_id, trade in pending.items():
            result = self._match_trade_to_outcome(trade)
            if result is None:
                continue  # game not finished yet

            winner, game_info = result
            our_team_won = self._did_our_team_win(trade, winner, game_info)

            if our_team_won is None:
                continue  # couldn't determine

            # Calculate real P&L
            # If we bet YES at entry_price and our team won: profit = (1 - entry_price) / entry_price * stake
            # If we bet YES and lost: loss = -stake
            if our_team_won:
                pnl = round((1.0 - trade.entry_price) / trade.entry_price * trade.size_usdc, 2)
                fill_price = 1.0
            else:
                pnl = round(-trade.size_usdc, 2)
                fill_price = 0.0

            self.state.close_trade(order_id, fill_price, pnl)
            outcome = "WON" if our_team_won else "LOST"
            log.info(
                f"RESOLVED {trade.event_name} | {trade.side} on {trade.bet_team} "
                f"| {outcome} | Winner: {winner} | P&L ${pnl:+.2f}"
            )

    def _match_trade_to_outcome(self, trade: Trade) -> Optional[tuple]:
        """Try to find a finished game matching this trade. Returns (winner, game_info) or None."""
        # Try exact match on home/away teams stored in trade
        if trade.home_team and trade.away_team:
            key = f"{trade.home_team}|{trade.away_team}"
            if key in self._finished_games:
                g = self._finished_games[key]
                return g["winner"], g

        # Fuzzy match: use team nicknames from event name (whole word only)
        import re
        event_lower = trade.event_name.lower()
        # Split event into words for whole-word matching
        event_words = set(re.split(r'[\s.@]+', event_lower))

        for key, game in self._finished_games.items():
            home_nick = game["home_team"].split()[-1].lower()
            away_nick = game["away_team"].split()[-1].lower()

            # Require BOTH team nicknames to match (prevents "nets" matching "hornets")
            home_match = len(home_nick) > 3 and home_nick in event_words
            away_match = len(away_nick) > 3 and away_nick in event_words

            if home_match and away_match:
                return game["winner"], game
            # Single match only if the nickname is long enough to be unambiguous (>6 chars)
            if home_match and len(home_nick) > 6:
                return game["winner"], game
            if away_match and len(away_nick) > 6:
                return game["winner"], game

        return None

    def _did_our_team_win(self, trade: Trade, winner: str, game_info: dict) -> Optional[bool]:
        """Determine if the team we bet on actually won."""
        winner_lower = winner.lower()
        winner_nick = winner.split()[-1].lower()

        # If bet_team is stored, use it directly
        if trade.bet_team:
            bet_nick = trade.bet_team.split()[-1].lower()
            if len(bet_nick) > 3:
                return bet_nick == winner_nick

        # Fallback: infer from side + event name
        # For "Team A vs. Team B" — YES = Team A wins
        # For "Team A @ Team B" — YES = Team A wins (away team)
        event = trade.event_name
        first_team = ""
        second_team = ""

        if " @ " in event:
            first_team, second_team = event.split(" @ ", 1)
        elif " vs. " in event:
            first_team, second_team = event.split(" vs. ", 1)
        elif " vs " in event:
            first_team, second_team = event.split(" vs ", 1)

        if not first_team:
            return None

        first_nick = first_team.strip().split()[-1].lower()
        second_nick = second_team.strip().split()[-1].lower()

        if trade.side == "YES":
            # We bet on first_team winning
            return first_nick == winner_nick
        else:
            # We bet NO = second_team winning
            return second_nick == winner_nick
