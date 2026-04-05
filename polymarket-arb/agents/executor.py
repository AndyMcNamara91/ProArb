"""
agents/executor.py — Agent 3: Executor

Responsibilities:
  1. Receive validated TradeDecision from Analyst queue
  2. Check STOP file before every order (kill switch)
  3. Place limit order on Polymarket CLOB via py-clob-client
  4. Record trade in BotState ledger
  5. Monitor fill status and log P&L

DEMO MODE: if DEMO_MODE=true (default), logs what it WOULD do without executing.
Set DEMO_MODE=false and provide real credentials to go live.
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path

from agents.analyst import TradeDecision
from core.risk import RiskManager
from core.state import BotState, Trade

log = logging.getLogger("executor")

KILL_FILE  = Path("STOP")
DEMO_MODE  = os.getenv("DEMO_MODE", "true").lower() == "true"

# Polymarket CLOB credentials — set in .env
POLY_PRIVATE_KEY    = os.getenv("POLY_PRIVATE_KEY", "")
POLY_FUNDER_ADDRESS = os.getenv("POLY_FUNDER_ADDRESS", "")
POLY_CHAIN_ID       = int(os.getenv("POLY_CHAIN_ID", 137))  # 137 = Polygon mainnet

# Slippage tolerance — how far from quoted price we'll accept a fill
SLIPPAGE_TOLERANCE = float(os.getenv("SLIPPAGE_TOLERANCE", 0.02))  # 2%


class ExecutorAgent:
    def __init__(self, state: BotState, risk: RiskManager):
        self.state  = state
        self.risk   = risk
        self._client = None  # lazy-init so import errors don't crash at startup

    async def execute(self, decision: TradeDecision) -> None:
        """
        Main entry point — called by main.py event loop for each TradeDecision.
        """

        # ── Kill switch ──────────────────────────────────────────────────────
        if KILL_FILE.exists():
            log.warning("STOP file present — executor refusing to place order")
            return

        opp = decision.opportunity

        # ── Dedup: skip if we already have a pending bet on this market ──────
        for existing in self.state._open_trades.values():
            if existing.market_id == opp.market_id and existing.status == "pending_outcome":
                log.debug(f"Already have pending bet on {opp.event_name} — skipping")
                return

        # ── Re-validate age ──────────────────────────────────────────────────
        latency = time.time() - decision.decision_ts
        if latency > 8.0:
            log.warning(f"Decision too old ({latency:.1f}s) — skipping {opp.event_name}")
            self.state.skip_trade(f"{opp.event_name}: executor latency {latency:.1f}s")
            return

        # ── Demo mode ────────────────────────────────────────────────────────
        if DEMO_MODE:
            log.info(
                f"[DEMO] PLACED: {opp.event_name} | {opp.our_side} "
                f"@ {opp.poly_price:.4f} | size ${decision.stake_usdc:.2f} "
                f"| edge {opp.raw_edge*100:.1f}% | EV ${decision.expected_val:.2f}"
            )
            # Record trade — will be resolved by outcome_checker when game finishes
            # Parse team info from source_detail (e.g. "ESPN live | Boston Celtics 24-Toronto Raptors 20")
            home_team, away_team, bet_team = self._parse_teams(opp)
            trade = Trade(
                timestamp     = time.time(),
                market_id     = opp.market_id,
                event_name    = opp.event_name,
                side          = opp.our_side,
                token_id      = opp.token_id,
                entry_price   = opp.poly_price,
                size_usdc     = decision.stake_usdc,
                edge_at_entry = opp.raw_edge,
                sports_prob   = opp.our_prob,
                poly_prob     = opp.poly_price,
                status        = "pending_outcome",
                order_id      = f"demo-{int(time.time() * 1000)}",
                home_team     = home_team,
                away_team     = away_team,
                sport         = opp.sport,
                bet_team      = bet_team,
            )
            self.state.record_trade(trade)
            return

        # ── Live execution ───────────────────────────────────────────────────
        try:
            client = self._get_client()
            order_id = await self._place_order(client, decision)
            if order_id:
                trade = Trade(
                    timestamp     = time.time(),
                    market_id     = opp.market_id,
                    event_name    = opp.event_name,
                    side          = opp.our_side,
                    token_id      = opp.token_id,
                    entry_price   = opp.poly_price,
                    size_usdc     = decision.stake_usdc,
                    edge_at_entry = opp.raw_edge,
                    sports_prob   = opp.our_prob,
                    poly_prob     = opp.poly_price,
                    status        = "open",
                    order_id      = order_id,
                )
                self.state.record_trade(trade)
        except Exception as e:
            log.error(f"Order placement failed for {opp.event_name}: {e}")

    async def _place_order(self, client, decision: TradeDecision) -> str:
        """
        Place a limit order on Polymarket CLOB.
        Uses GTC (good-till-cancelled) marketable limit to get immediate fill
        at best available price with slippage cap.
        
        Returns order_id string if successful, raises on failure.
        """
        opp = decision.opportunity

        # Price with slippage tolerance applied
        # Buying YES: we're willing to pay up to (price + tolerance)
        limit_price = round(
            opp.poly_price + SLIPPAGE_TOLERANCE
            if opp.our_side == "YES"
            else opp.poly_price - SLIPPAGE_TOLERANCE,
            4
        )
        limit_price = max(0.01, min(0.99, limit_price))

        # Size in number of shares (1 share = $1 at resolution)
        size_shares = decision.stake_usdc / opp.poly_price

        log.info(
            f"Placing order: {opp.event_name} | {opp.our_side} "
            f"limit={limit_price:.4f} size={size_shares:.2f} shares"
        )

        # py-clob-client order placement
        # See: https://github.com/Polymarket/py-clob-client
        from py_clob_client.clob_types import OrderArgs, BUY, SELL

        order_args = OrderArgs(
            token_id = opp.token_id,
            price    = limit_price,
            size     = size_shares,
            side     = BUY if opp.our_side == "YES" else SELL,
        )

        resp = client.create_and_post_order(order_args)

        if resp and resp.get("orderID"):
            order_id = resp["orderID"]
            log.info(f"Order placed: {order_id}")
            return order_id
        else:
            raise RuntimeError(f"Unexpected order response: {resp}")

    def _parse_teams(self, opp) -> tuple:
        """Extract home_team, away_team, and which team we're betting on from opportunity."""
        source = opp.source_detail
        home_team = away_team = bet_team = ""

        # Source detail format: "ESPN live | Home Team 24-Away Team 20"
        # or from Odds API: "{N} bookmakers, {sport}"
        if "ESPN live" in source:
            try:
                parts = source.split(" | ", 1)[1] if " | " in source else source
                # "Boston Celtics 24-Toronto Raptors 20" or "Boston Celtics 24-20 Toronto Raptors"
                # Try splitting on score pattern
                import re
                m = re.match(r"(.+?)\s+\d+[-–]\d+\s+(.+)", parts)
                if m:
                    home_team = m.group(1).strip()
                    away_team = m.group(2).strip()
            except Exception:
                pass

        # From event name: "Team A @ Team B" (Odds API) or "Team A vs. Team B" (Polymarket)
        if not home_team:
            name = opp.event_name
            if " @ " in name:
                away_team, home_team = name.split(" @ ", 1)
            elif " vs. " in name:
                parts = name.split(" vs. ", 1)
                home_team = parts[1] if len(parts) > 1 else ""
                away_team = parts[0]
            elif " vs " in name:
                parts = name.split(" vs ", 1)
                home_team = parts[1] if len(parts) > 1 else ""
                away_team = parts[0]

        # Determine which team we're betting on
        # YES on "Team A vs Team B" = Team A (first team / away in @ format)
        # For Polymarket "X vs Y" markets, YES typically = first team listed
        if opp.our_side == "YES":
            bet_team = away_team if " @ " in opp.event_name else home_team or away_team
        else:
            bet_team = home_team if " @ " in opp.event_name else away_team or home_team

        return home_team.strip(), away_team.strip(), bet_team.strip()

    def _get_client(self):
        """Lazy-init the py-clob-client. Raises on missing credentials."""
        if self._client:
            return self._client

        if not POLY_PRIVATE_KEY or not POLY_FUNDER_ADDRESS:
            raise RuntimeError(
                "POLY_PRIVATE_KEY and POLY_FUNDER_ADDRESS must be set in .env "
                "to run in live mode. Set DEMO_MODE=true to test without credentials."
            )

        try:
            from py_clob_client.client import ClobClient
            self._client = ClobClient(
                host       = "https://clob.polymarket.com",
                chain_id   = POLY_CHAIN_ID,
                key        = POLY_PRIVATE_KEY,
                signature_type = 1,  # EOA
                funder     = POLY_FUNDER_ADDRESS,
            )
            log.info("Polymarket CLOB client initialised (LIVE MODE)")
        except ImportError:
            raise RuntimeError(
                "py-clob-client not installed. Run: pip install py-clob-client"
            )

        return self._client
