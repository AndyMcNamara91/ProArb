"""
core/risk.py -- Risk management with Kelly sizing, correlation control, trade caps

V2 changes: correlation control per sport, daily trade cap, quarter Kelly default,
10% minimum edge, $5 minimum stake.
"""

import logging

from core.models import RiskLimits

log = logging.getLogger("risk")


class RiskManager:
    """Centralised risk controls used by Gatekeeper and Risk Monitor."""

    def __init__(self, limits: RiskLimits):
        self.limits = limits

    # -- Kelly sizing ----------------------------------------------------

    def kelly_stake(self, bankroll: float, our_prob: float, market_price: float) -> float:
        """
        Quarter Kelly for a binary market.

        b = (1 - market_price) / market_price  (net odds)
        f* = (b * p - q) / b                   (full Kelly)
        stake = bankroll * f* * kelly_fraction

        Capped at max_position_pct of bankroll. Returns 0 if below min_stake.
        """
        if market_price <= 0 or market_price >= 1:
            return 0.0

        b = (1.0 - market_price) / market_price  # net odds
        q = 1.0 - our_prob
        full_kelly = (b * our_prob - q) / b

        if full_kelly <= 0:
            return 0.0

        frac_kelly = full_kelly * self.limits.kelly_fraction
        stake = bankroll * frac_kelly

        # Cap at max position size
        max_stake = bankroll * self.limits.max_position_pct
        stake = min(stake, max_stake)

        # Minimum viable stake
        if stake < self.limits.min_stake:
            return 0.0

        return round(stake, 2)

    # -- Gate checks (used by Gatekeeper) --------------------------------

    def check_edge(self, edge: float) -> tuple[bool, str]:
        if edge < self.limits.min_edge:
            return False, f"Edge {edge * 100:.1f}% < min {self.limits.min_edge * 100:.1f}%"
        return True, ""

    def check_probability_bounds(self, prob: float, poly_price: float) -> tuple[bool, str]:
        if prob < self.limits.prob_lower_bound or prob > self.limits.prob_upper_bound:
            return False, f"Prob {prob:.2f} outside [{self.limits.prob_lower_bound}, {self.limits.prob_upper_bound}]"
        if poly_price < self.limits.min_poly_price:
            return False, f"Poly price {poly_price:.2f} < min {self.limits.min_poly_price}"
        return True, ""

    def check_market_quality(
        self, liquidity: float, spread: float, depth_at_price: float, stake: float
    ) -> tuple[bool, str]:
        if liquidity < self.limits.min_liquidity:
            return False, f"Liquidity ${liquidity:.0f} < min ${self.limits.min_liquidity:.0f}"
        if spread > self.limits.max_spread:
            return False, f"Spread {spread:.3f} > max {self.limits.max_spread}"
        if depth_at_price < stake * 2:
            return False, f"Depth ${depth_at_price:.0f} < 2x stake ${stake:.0f}"
        return True, ""

    def check_position_limits(
        self,
        open_positions: int,
        daily_trades: int,
        daily_loss_pct: float,
        bankroll: float,
    ) -> tuple[bool, str]:
        if bankroll < self.limits.min_bankroll:
            return False, f"Bankroll ${bankroll:.2f} < min ${self.limits.min_bankroll:.2f}"
        if open_positions >= self.limits.max_open_positions:
            return False, f"Open positions {open_positions} >= max {self.limits.max_open_positions}"
        if daily_trades >= self.limits.max_daily_trades:
            return False, f"Daily trades {daily_trades} >= max {self.limits.max_daily_trades}"
        if daily_loss_pct >= self.limits.daily_loss_limit_pct:
            return False, f"Daily loss {daily_loss_pct * 100:.1f}% >= limit {self.limits.daily_loss_limit_pct * 100:.1f}%"
        return True, ""

    def check_correlation(
        self, sport: str, sport_exposure_pct: float
    ) -> tuple[bool, str]:
        if sport_exposure_pct >= self.limits.max_correlated_exposure_pct:
            return False, (
                f"Sport {sport} exposure {sport_exposure_pct * 100:.1f}% "
                f">= max {self.limits.max_correlated_exposure_pct * 100:.1f}%"
            )
        return True, ""

    def daily_loss_breached(self, daily_loss_pct: float) -> bool:
        return daily_loss_pct >= self.limits.daily_loss_limit_pct
