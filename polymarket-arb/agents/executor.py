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
import random
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

        # ── Re-validate age ──────────────────────────────────────────────────
        latency = time.time() - decision.decision_ts
        if latency > 8.0:
            log.warning(f"Decision too old ({latency:.1f}s) — skipping {opp.event_name}")
            self.state.skip_trade(f"{opp.event_name}: executor latency {latency:.1f}s")
            return

        # ── Demo mode ────────────────────────────────────────────────────────
        if DEMO_MODE:
            log.info(
                f"[DEMO] WOULD PLACE: {opp.event_name} | {opp.our_side} "
                f"@ {opp.poly_price:.4f} | size ${decision.stake_usdc:.2f} "
                f"| edge {opp.raw_edge*100:.1f}% | EV ${decision.expected_val:.2f}"
            )
            # Simulate a fill for state tracking
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
                status        = "demo_filled",
                order_id      = f"demo-{int(time.time())}",
            )
            self.state.record_trade(trade)
            # Auto-close demo trade after a short delay so positions cycle
            asyncio.create_task(self._demo_auto_close(trade))
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

    async def _demo_auto_close(self, trade: Trade) -> None:
        """Simulate closing a demo trade after 5-15s with random P&L."""
        delay = random.uniform(5, 15)
        await asyncio.sleep(delay)
        # Simulate price movement: win ~60% of the time in demo
        if random.random() < 0.6:
            pnl = round(random.uniform(0.50, trade.size_usdc * 0.3), 2)
        else:
            pnl = round(-random.uniform(0.50, trade.size_usdc * 0.2), 2)
        fill_price = round(trade.entry_price + random.uniform(-0.05, 0.05), 4)
        fill_price = max(0.01, min(0.99, fill_price))
        self.state.close_trade(trade.order_id, fill_price, pnl)
        log.info(f"[DEMO] Auto-closed {trade.event_name} | P&L ${pnl:+.2f}")

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
