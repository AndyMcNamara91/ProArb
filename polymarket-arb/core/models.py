"""
core/models.py -- Data models for ProArb V2

All shared data structures used across modules. Built on Pydantic for
validation and serialisation.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Opportunity(BaseModel):
    """Raw opportunity detected by Scanner -- Pinnacle vs Polymarket gap."""
    timestamp: float
    event_name: str
    sport: str
    home_team: str
    away_team: str
    market_id: str                          # Polymarket condition ID
    token_id: str                           # Polymarket YES/NO token ID
    our_side: str                           # "YES" | "NO"
    pinnacle_fair_prob: float               # de-vigged Pinnacle probability for our side
    polymarket_price: float                 # current Polymarket price for our side
    edge: float                             # pinnacle_fair_prob - polymarket_price
    pinnacle_home_prob: float               # de-vigged home win prob (for CLV tracking)
    pinnacle_away_prob: float               # de-vigged away win prob
    polymarket_liquidity: float = 0.0       # estimated market volume
    bid_ask_spread: float = 0.0             # best_ask - best_bid
    best_bid: float = 0.0
    best_ask: float = 0.0
    order_book_depth: float = 0.0           # depth at target price


class TradeDecision(BaseModel):
    """Validated, sized trade approved by Gatekeeper."""
    opportunity: Opportunity
    stake_usdc: float
    kelly_fraction_used: float
    max_entry_price: float                  # limit price for the order
    current_best_ask: float
    decision_ts: float


class TradeRecord(BaseModel):
    """Full lifecycle record of a trade -- entry through resolution."""
    # Identity
    trade_id: str
    market_id: str
    token_id: str
    event_name: str
    sport: str
    team: str                               # team name we're betting on
    side: str                               # YES | NO

    # Entry data
    entry_time: float
    entry_price: float                      # what we paid on Polymarket
    size_usdc: float
    shares: float
    pinnacle_prob_at_entry: float           # Pinnacle fair prob when we traded
    edge_at_entry: float

    # Order tracking
    order_id: Optional[str] = None
    order_type: str = "limit"               # "limit" | "demo"
    status: str = "open"                    # open | filled | cancelled | resolved | demo

    # Closing data (captured at game start)
    pinnacle_prob_at_close: Optional[float] = None
    polymarket_price_at_close: Optional[float] = None

    # Resolution
    outcome: Optional[str] = None           # "won" | "lost" | None
    pnl: Optional[float] = None
    resolved_at: Optional[float] = None

    @property
    def clv(self) -> Optional[float]:
        """Closing Line Value. Positive = we got a better price than closing."""
        if self.pinnacle_prob_at_close is None:
            return None
        return self.pinnacle_prob_at_close - self.entry_price

    @property
    def edge_at_close(self) -> Optional[float]:
        if self.pinnacle_prob_at_close is None:
            return None
        return self.pinnacle_prob_at_close - self.entry_price


class RiskLimits(BaseModel):
    """Risk parameters -- loaded from environment."""
    daily_loss_limit_pct: float = 0.20          # 20% of starting daily bankroll
    max_position_pct: float = 0.05              # 5% per trade
    max_open_positions: int = 10
    max_daily_trades: int = 15
    max_correlated_exposure_pct: float = 0.15   # 15% in same sport
    min_edge: float = 0.10                      # 10% minimum edge
    min_bankroll: float = 20.0                  # stop trading below $20
    kelly_fraction: float = 0.25                # quarter Kelly
    min_stake: float = 5.0                      # $5 minimum per trade
    min_liquidity: float = 5000.0               # $5k minimum market volume
    max_spread: float = 0.04                    # 4 cent max spread
    prob_lower_bound: float = 0.25              # skip below 25%
    prob_upper_bound: float = 0.90              # skip above 90%
    min_poly_price: float = 0.15                # skip very cheap contracts
