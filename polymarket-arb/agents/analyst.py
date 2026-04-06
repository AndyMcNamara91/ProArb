"""
agents/analyst.py — Agent 2: Analyst

Responsibilities:
  1. Receive raw opportunities from Scanner queue
  2. Apply strict edge, confidence, and risk gates
  3. Calculate Kelly-sized stake
  4. Enrich with expected value and slippage estimate
  5. Either push a validated TradeDecision to executor queue, or log a skip

This is the brain of the pipeline. It never touches the Polymarket API.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from agents.scanner import Opportunity
from core.probability import confidence_score, expected_value
from core.risk import RiskManager
from core.state import BotState

log = logging.getLogger("analyst")

BANKROLL_USDC = float(os.getenv("BANKROLL_USDC", 500))

# Maximum age of an opportunity before we discard it (scanner → analyst latency)
MAX_OPP_AGE_SEC = 10.0

# Minimum expected value (in USDC) for a trade to be worth executing
MIN_EV_USDC = 0.50


@dataclass
class TradeDecision:
    """Validated, sized trade ready for the executor."""
    opportunity:  Opportunity
    stake_usdc:   float
    confidence:   float
    expected_val: float    # EV in USDC
    decision_ts:  float    # when analyst made the decision


class AnalystAgent:
    def __init__(self, state: BotState, risk: RiskManager):
        self.state = state
        self.risk  = risk

    async def evaluate(self, opp: Opportunity) -> Optional[TradeDecision]:
        """
        Evaluate a raw opportunity. Returns a TradeDecision if tradeable,
        None if skipped (reason logged to state).
        """

        # ── 1. Staleness check ──────────────────────────────────────────────
        age = time.time() - opp.timestamp
        if age > MAX_OPP_AGE_SEC:
            self.state.skip_trade(f"{opp.event_name}: opportunity stale ({age:.1f}s)")
            return None

        # ── 2. Confidence scoring ───────────────────────────────────────────
        confidence = confidence_score(
            data_sources         = opp.data_sources,
            time_remaining_pct   = opp.time_remaining_pct,
            score_diff_magnitude = abs(opp.score_diff),
        )

        # ── 3. Risk gate ────────────────────────────────────────────────────
        tradeable, reason = self.risk.is_tradeable(
            edge           = opp.raw_edge,
            confidence     = confidence,
            open_positions = self.state.open_positions,
        )
        if not tradeable:
            self.state.skip_trade(f"{opp.event_name}: {reason}")
            return None

        # ── 4. Kelly sizing ─────────────────────────────────────────────────
        stake = self.risk.kelly_stake(
            bankroll     = BANKROLL_USDC,
            our_prob     = opp.our_prob,
            market_price = opp.poly_price,
        )
        if stake < 1.0:
            self.state.skip_trade(f"{opp.event_name}: Kelly stake too small (${stake:.2f})")
            return None

        # ── 5. Expected value check ─────────────────────────────────────────
        ev = expected_value(
            our_prob   = opp.our_prob,
            poly_price = opp.poly_price,
            stake      = stake,
        )
        if ev < MIN_EV_USDC:
            self.state.skip_trade(
                f"{opp.event_name}: EV ${ev:.2f} < min ${MIN_EV_USDC:.2f}"
            )
            return None

        # ── 6. Slippage sanity check ─────────────────────────────────────────
        # If our stake is large relative to typical Polymarket liquidity,
        # we may move the price. Warn if stake > 2% of typical market depth.
        # TODO: enrich with live orderbook depth from CLOB WebSocket.
        estimated_depth_usdc = 5000  # conservative estimate for mid-size sports market
        if stake > estimated_depth_usdc * 0.02:
            log.warning(
                f"{opp.event_name}: stake ${stake:.2f} may cause slippage "
                f"(est. depth ${estimated_depth_usdc})"
            )

        decision = TradeDecision(
            opportunity  = opp,
            stake_usdc   = stake,
            confidence   = confidence,
            expected_val = ev,
            decision_ts  = time.time(),
        )

        log.info(
            f"TRADE OK  {opp.event_name} | {opp.our_side} "
            f"| edge={opp.raw_edge*100:.1f}% conf={confidence:.2f} "
            f"| stake=${stake:.2f} EV=${ev:.2f}"
        )
        return decision
