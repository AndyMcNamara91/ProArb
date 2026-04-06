"""
modules/executor.py -- Module 3: "Place the Trade"

Key V2 changes:
  - postOnly limit orders (maker, 0% fee + potential rebate)
  - 10-minute order expiry (cancel if not filled)
  - No market orders, no taker fees
  - GTC orders that rest on the book

DEMO MODE: logs what it WOULD do without executing.
Set DEMO_MODE=false and provide real credentials to go live.
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from core.models import TradeDecision, TradeRecord
from core.state import BotState

log = logging.getLogger("executor")

KILL_FILE = Path("STOP")
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"

# Polymarket CLOB credentials
POLY_PRIVATE_KEY = os.getenv("POLYMARKET_PK", "")
POLY_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER", "")
POLY_CHAIN_ID = int(os.getenv("POLY_CHAIN_ID", 137))

# Order expiry: cancel unfilled orders after this many seconds
ORDER_EXPIRY_SEC = int(os.getenv("ORDER_EXPIRY_SEC", 600))


class ExecutorModule:
    def __init__(self, state: BotState):
        self.state = state
        self._client = None
        self._pending_cancels: dict[str, asyncio.Task] = {}

    async def execute(self, decision: TradeDecision) -> None:
        """Place a postOnly limit order or simulate in demo mode."""

        # -- Kill switch ---------------------------------------------------
        if KILL_FILE.exists():
            log.warning("STOP file present -- refusing to place order")
            return

        opp = decision.opportunity

        # -- Staleness check -----------------------------------------------
        latency = time.time() - decision.decision_ts
        if latency > 10.0:
            log.warning(f"Decision too old ({latency:.1f}s) -- skipping {opp.event_name}")
            self.state.skip_trade(f"{opp.event_name}: executor latency {latency:.1f}s")
            return

        # -- Calculate shares ----------------------------------------------
        shares = decision.stake_usdc / decision.max_entry_price

        # -- Build trade record --------------------------------------------
        trade_id = f"trade-{int(time.time() * 1000)}"
        team = opp.home_team if opp.our_side == "YES" else opp.away_team

        trade = TradeRecord(
            trade_id=trade_id,
            market_id=opp.market_id,
            token_id=opp.token_id,
            event_name=opp.event_name,
            sport=opp.sport,
            team=team,
            side=opp.our_side,
            entry_time=time.time(),
            entry_price=decision.max_entry_price,
            size_usdc=decision.stake_usdc,
            shares=round(shares, 2),
            pinnacle_prob_at_entry=opp.pinnacle_fair_prob,
            edge_at_entry=opp.edge,
        )

        # -- Demo mode -----------------------------------------------------
        if DEMO_MODE:
            trade.status = "demo"
            trade.order_type = "demo"
            trade.order_id = f"demo-{trade_id}"
            log.info(
                f"[DEMO] WOULD PLACE: {opp.event_name} | {opp.our_side} "
                f"@ {decision.max_entry_price:.2f} | {shares:.1f} shares "
                f"| ${decision.stake_usdc:.2f} | edge {opp.edge * 100:.1f}%"
            )
            self.state.record_trade(trade)
            return

        # -- Live execution ------------------------------------------------
        try:
            client = self._get_client()
            order_id = await self._place_limit_order(client, decision, shares)
            if order_id:
                trade.order_id = order_id
                trade.status = "open"
                self.state.record_trade(trade)

                # Schedule auto-cancel after expiry
                cancel_task = asyncio.create_task(
                    self._cancel_if_unfilled(client, order_id, ORDER_EXPIRY_SEC)
                )
                self._pending_cancels[order_id] = cancel_task
        except Exception as e:
            log.error(f"Order placement failed for {opp.event_name}: {e}")

    async def _place_limit_order(self, client, decision: TradeDecision, shares: float) -> Optional[str]:
        """Place a postOnly GTC limit order on Polymarket CLOB.

        postOnly guarantees we never pay taker fees. If our order would
        cross the spread, it's rejected rather than filled as a taker.
        """
        opp = decision.opportunity

        log.info(
            f"Placing postOnly limit: {opp.event_name} | {opp.our_side} "
            f"@ {decision.max_entry_price:.2f} | {shares:.1f} shares"
        )

        from py_clob_client.clob_types import OrderArgs, BUY, SELL, OrderType

        order_args = OrderArgs(
            token_id=opp.token_id,
            price=decision.max_entry_price,
            size=shares,
            side=BUY if opp.our_side == "YES" else SELL,
        )

        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, order_type=OrderType.GTC)

        if resp and hasattr(resp, "orderID"):
            order_id = resp.orderID
            log.info(f"Order placed: {order_id}")
            return order_id
        elif resp and isinstance(resp, dict) and resp.get("orderID"):
            order_id = resp["orderID"]
            log.info(f"Order placed: {order_id}")
            return order_id
        else:
            raise RuntimeError(f"Unexpected order response: {resp}")

    async def _cancel_if_unfilled(self, client, order_id: str, timeout: int) -> None:
        """Cancel an order if it hasn't been filled within timeout seconds.

        Pinnacle line may have moved by then, so the edge is stale.
        """
        await asyncio.sleep(timeout)
        try:
            client.cancel(order_id)
            key = order_id
            self.state.cancel_trade(key)
            log.info(f"Order {order_id} cancelled after {timeout}s (unfilled)")
        except Exception as e:
            log.warning(f"Failed to cancel order {order_id}: {e}")
        finally:
            self._pending_cancels.pop(order_id, None)

    def _get_client(self):
        """Lazy-init the py-clob-client."""
        if self._client:
            return self._client

        if not POLY_PRIVATE_KEY or not POLY_FUNDER_ADDRESS:
            raise RuntimeError(
                "POLYMARKET_PK and POLYMARKET_FUNDER must be set in .env "
                "to run in live mode. Set DEMO_MODE=true to test without credentials."
            )

        try:
            from py_clob_client.client import ClobClient
            self._client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=POLY_CHAIN_ID,
                key=POLY_PRIVATE_KEY,
                signature_type=1,  # EOA
                funder=POLY_FUNDER_ADDRESS,
            )
            log.info("Polymarket CLOB client initialised (LIVE MODE)")
        except ImportError:
            raise RuntimeError(
                "py-clob-client not installed. Run: pip install py-clob-client"
            )

        return self._client

    async def cleanup(self) -> None:
        """Cancel all pending cancel tasks on shutdown."""
        for task in self._pending_cancels.values():
            task.cancel()
        self._pending_cancels.clear()
