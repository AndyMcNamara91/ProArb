"""
modules/gatekeeper.py -- Module 2: "Is It Worth It?"

4 gates:
  1. Market Quality Filter (liquidity, spread, depth)
  2. Edge Threshold (10% minimum)
  3. Probability Bounds (0.25-0.90 range, min poly price 0.15)
  4. Kelly Sizing with fee adjustment (quarter Kelly, 5% position cap)

Also checks: position limits, daily trade cap, daily loss, correlation.
"""

import logging
import time
from typing import Optional

from core.models import Opportunity, TradeDecision, RiskLimits
from core.risk import RiskManager
from core.state import BotState

log = logging.getLogger("gatekeeper")

# Maximum age of an opportunity before we discard it (scanner -> gatekeeper latency)
MAX_OPP_AGE_SEC = 15.0


class GatekeeperModule:
    def __init__(self, state: BotState, risk: RiskManager):
        self.state = state
        self.risk = risk

    async def evaluate(self, opp: Opportunity) -> Optional[TradeDecision]:
        """Evaluate a raw opportunity through all gates. Returns TradeDecision or None."""

        # -- Staleness check -----------------------------------------------
        age = time.time() - opp.timestamp
        if age > MAX_OPP_AGE_SEC:
            self.state.skip_trade(f"{opp.event_name}: stale ({age:.1f}s)")
            return None

        # -- Dedup check ---------------------------------------------------
        if self.state.already_traded(opp.event_name):
            self.state.skip_trade(f"{opp.event_name}: already traded")
            return None

        # -- Gate 1: Market Quality ----------------------------------------
        ok, reason = self.risk.check_market_quality(
            liquidity=opp.polymarket_liquidity,
            spread=opp.bid_ask_spread,
            depth_at_price=opp.order_book_depth,
            stake=self.risk.limits.min_stake,  # use min stake for initial check
        )
        if not ok:
            # In demo mode, skip this gate (we don't have real market data)
            if not _is_demo(opp):
                self.state.skip_trade(f"{opp.event_name}: {reason}")
                return None

        # -- Gate 2: Edge Threshold ----------------------------------------
        ok, reason = self.risk.check_edge(opp.edge)
        if not ok:
            self.state.skip_trade(f"{opp.event_name}: {reason}")
            return None

        # -- Gate 3: Probability Bounds ------------------------------------
        ok, reason = self.risk.check_probability_bounds(
            opp.pinnacle_fair_prob, opp.polymarket_price
        )
        if not ok:
            self.state.skip_trade(f"{opp.event_name}: {reason}")
            return None

        # -- Position & daily limits ---------------------------------------
        ok, reason = self.risk.check_position_limits(
            open_positions=self.state.open_positions,
            daily_trades=self.state.daily_trades,
            daily_loss_pct=self.state.daily_loss_pct,
            bankroll=self.state.bankroll,
        )
        if not ok:
            self.state.skip_trade(f"{opp.event_name}: {reason}")
            return None

        # -- Correlation check ---------------------------------------------
        ok, reason = self.risk.check_correlation(
            sport=opp.sport,
            sport_exposure_pct=self.state.sport_exposure_pct(opp.sport),
        )
        if not ok:
            self.state.skip_trade(f"{opp.event_name}: {reason}")
            return None

        # -- Gate 4: Kelly Sizing ------------------------------------------
        stake = self.risk.kelly_stake(
            bankroll=self.state.bankroll,
            our_prob=opp.pinnacle_fair_prob,
            market_price=opp.polymarket_price,
        )
        if stake <= 0:
            self.state.skip_trade(f"{opp.event_name}: Kelly stake too small")
            return None

        # Re-check market quality with actual stake
        if not _is_demo(opp):
            ok, reason = self.risk.check_market_quality(
                liquidity=opp.polymarket_liquidity,
                spread=opp.bid_ask_spread,
                depth_at_price=opp.order_book_depth,
                stake=stake,
            )
            if not ok:
                self.state.skip_trade(f"{opp.event_name}: {reason}")
                return None

        # -- Calculate limit order price -----------------------------------
        # Price = Pinnacle fair prob - 5% edge buffer (keep minimum edge after fill)
        max_price = opp.pinnacle_fair_prob - 0.05

        # Don't pay more than current best ask (would be a taker order)
        current_ask = opp.best_ask if opp.best_ask > 0 else opp.polymarket_price + 0.01
        if max_price >= current_ask:
            max_price = current_ask - 0.01

        # Snap to tick size ($0.01)
        max_price = round(max_price, 2)
        max_price = max(0.01, min(0.99, max_price))

        decision = TradeDecision(
            opportunity=opp,
            stake_usdc=stake,
            kelly_fraction_used=self.risk.limits.kelly_fraction,
            max_entry_price=max_price,
            current_best_ask=current_ask,
            decision_ts=time.time(),
        )

        log.info(
            f"APPROVED {opp.event_name} | {opp.our_side} "
            f"| edge={opp.edge * 100:.1f}% | stake=${stake:.2f} "
            f"| limit={max_price:.2f}"
        )
        return decision


def _is_demo(opp: Opportunity) -> bool:
    """Check if this is a demo opportunity (relaxed validation)."""
    return opp.event_name.startswith("DEMO") or opp.market_id.startswith("demo-")
