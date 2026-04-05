"""
core/risk.py — Kelly Criterion sizing, daily loss limits, position caps
"""

import logging
import os

log = logging.getLogger("risk")


class RiskManager:
    """
    Centralised risk controls used by both Analyst and Executor.

    Key parameters (all configurable via .env):
      DAILY_LOSS_LIMIT_USD  — hard stop if daily losses exceed this (default $50)
      MAX_POSITION_PCT      — max % of bankroll per single trade (default 5%)
      MIN_EDGE              — minimum probability gap to trade (default 0.08 = 8%)
      MIN_CONFIDENCE        — minimum sports-prob confidence to trade (default 0.70)
      KELLY_FRACTION        — fractional Kelly to reduce variance (default 0.25)
      MAX_OPEN_POSITIONS    — cap simultaneous open trades (default 3)
    """

    def __init__(self, daily_loss_limit: float, max_position_pct: float):
        self.daily_loss_limit  = daily_loss_limit
        self.max_position_pct  = max_position_pct
        self.min_edge          = float(os.getenv("MIN_EDGE", 0.08))
        self.min_confidence    = float(os.getenv("MIN_CONFIDENCE", 0.70))
        self.kelly_fraction    = float(os.getenv("KELLY_FRACTION", 0.25))
        self.max_open          = int(os.getenv("MAX_OPEN_POSITIONS", 3))
        self._daily_loss_total = 0.0

    # ── Sizing ───────────────────────────────────────────────────────────────

    def kelly_stake(
        self,
        bankroll:    float,
        our_prob:    float,   # our estimated true probability (0–1)
        market_price: float,  # current polymarket price (0–1)
    ) -> float:
        """
        Full Kelly for a binary market where YES pays 1/price per dollar risked:
          f* = (p * b - q) / b      where b = (1/price) - 1
        
        We use fractional Kelly (default 25%) to reduce variance dramatically.
        Result is further capped at max_position_pct of bankroll.
        """
        if market_price <= 0 or market_price >= 1:
            return 0.0

        b = (1.0 / market_price) - 1.0   # net odds (profit per $1 wagered)
        q = 1.0 - our_prob
        full_kelly = (our_prob * b - q) / b

        if full_kelly <= 0:
            log.debug(f"Negative Kelly ({full_kelly:.3f}) — no edge, skipping")
            return 0.0

        frac_kelly = full_kelly * self.kelly_fraction
        capped     = min(frac_kelly, self.max_position_pct) * bankroll

        log.debug(
            f"Kelly: p={our_prob:.3f} b={b:.3f} "
            f"full={full_kelly:.3f} frac={frac_kelly:.3f} "
            f"stake=${capped:.2f}"
        )
        return max(capped, 0.0)

    # ── Gate checks ──────────────────────────────────────────────────────────

    def is_tradeable(
        self,
        edge:          float,   # gap between our_prob and market_price
        confidence:    float,   # 0–1, how sure we are about our_prob
        open_positions: int,
    ) -> tuple[bool, str]:
        """
        Returns (True, "") if trade passes all gates, else (False, reason).
        """
        if self._daily_loss_total >= self.daily_loss_limit:
            return False, f"Daily loss limit ${self.daily_loss_limit} reached"

        if edge < self.min_edge:
            return False, f"Edge {edge*100:.1f}% < min {self.min_edge*100:.1f}%"

        if confidence < self.min_confidence:
            return False, f"Confidence {confidence:.2f} < min {self.min_confidence:.2f}"

        if open_positions >= self.max_open:
            return False, f"Open positions {open_positions} >= max {self.max_open}"

        return True, ""

    # ── Loss tracking ────────────────────────────────────────────────────────

    def record_loss(self, amount: float) -> None:
        """Call with positive loss amount when a trade closes at a loss."""
        self._daily_loss_total += amount
        log.info(f"Daily loss tracker: ${self._daily_loss_total:.2f} / ${self.daily_loss_limit:.2f}")

    def daily_loss_breached(self) -> bool:
        return self._daily_loss_total >= self.daily_loss_limit

    def reset_daily(self) -> None:
        """Call at midnight to reset daily loss counter."""
        self._daily_loss_total = 0.0


# ── Inline tests ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    rm = RiskManager(daily_loss_limit=50.0, max_position_pct=0.05)

    # kelly_stake — strong edge should give reasonable stake
    stake = rm.kelly_stake(500, 0.87, 0.55)
    assert 5 <= stake <= 25, f"Expected $5-$25, got ${stake:.2f}"

    # kelly_stake — negative edge should return 0
    stake_neg = rm.kelly_stake(500, 0.45, 0.55)
    assert stake_neg == 0.0, f"Expected 0.0 for negative edge, got {stake_neg}"

    # is_tradeable — good trade
    ok, reason = rm.is_tradeable(0.12, 0.75, 1)
    assert ok is True and reason == "", f"Expected tradeable, got ({ok}, {reason})"

    # is_tradeable — edge too low
    ok, reason = rm.is_tradeable(0.04, 0.75, 1)
    assert ok is False and "edge" in reason.lower(), f"Expected edge rejection, got ({ok}, {reason})"

    # is_tradeable — confidence too low
    ok, reason = rm.is_tradeable(0.12, 0.50, 1)
    assert ok is False and "confidence" in reason.lower(), f"Expected confidence rejection, got ({ok}, {reason})"

    # is_tradeable — too many positions
    ok, reason = rm.is_tradeable(0.12, 0.75, 5)
    assert ok is False and "position" in reason.lower(), f"Expected position rejection, got ({ok}, {reason})"

    # daily loss tracking
    rm.record_loss(30.0)
    assert not rm.daily_loss_breached()
    rm.record_loss(25.0)
    assert rm.daily_loss_breached()
    rm.reset_daily()
    assert not rm.daily_loss_breached()

    print("All risk tests passed")
