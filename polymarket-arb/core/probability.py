"""
core/probability.py — Convert sports odds formats to implied probability,
                       calculate gap vs Polymarket price, confidence scoring
"""

import math
import logging

log = logging.getLogger("probability")


# ── Odds conversion ──────────────────────────────────────────────────────────

def american_to_prob(odds: int) -> float:
    """
    Convert American moneyline odds to implied probability.
      +150  →  1 / (1 + 1.50)  = 0.400
      -200  →  2.0 / (1 + 2.0) = 0.667
    """
    if odds > 0:
        return 1.0 / (1.0 + odds / 100.0)
    else:
        magnitude = abs(odds) / 100.0
        return magnitude / (1.0 + magnitude)


def decimal_to_prob(odds: float) -> float:
    """Decimal odds (e.g. 2.50) → probability."""
    if odds <= 1.0:
        return 1.0
    return 1.0 / odds


def remove_vig(home_prob: float, away_prob: float) -> tuple[float, float]:
    """
    Remove bookmaker vig (overround) from two-sided market probabilities.
    Raw implied probs sum to > 1.0 due to vig — normalise to fair probs.
    """
    total = home_prob + away_prob
    if total <= 0:
        return 0.5, 0.5
    return home_prob / total, away_prob / total


# ── In-game momentum probability ─────────────────────────────────────────────

def in_game_win_prob(
    score_diff: int,        # positive = home team leading
    time_remaining_pct: float,  # 0.0 = game over, 1.0 = full game remaining
    sport: str = "basketball",
) -> float:
    """
    Simple logistic model for in-game win probability.
    
    Based on the insight that a team leading by a large margin late in a game
    has a near-certain win probability that Polymarket may not have repriced.
    
    Returns probability for the LEADING team (score_diff > 0 → home team).
    
    Note: For production, replace with a trained model or Sportradar live win prob.
    """
    if time_remaining_pct <= 0:
        return 1.0 if score_diff > 0 else 0.0

    # Sport-specific volatility (points-per-possession × pace)
    sport_k = {
        "basketball": 0.15,   # high-scoring, leads can evaporate
        "football":   0.25,   # 3-score game = very safe late
        "baseball":   0.20,
        "soccer":     0.30,   # low-scoring, even 1-goal leads can flip
    }.get(sport.lower(), 0.20)

    # Logistic: larger lead + less time = higher probability
    x = score_diff / (time_remaining_pct * 100 * sport_k + 0.01)
    prob = 1.0 / (1.0 + math.exp(-x))
    return max(0.01, min(0.99, prob))


# ── Gap calculation ──────────────────────────────────────────────────────────

def calculate_edge(our_prob: float, poly_price: float) -> float:
    """
    Edge = difference between our estimated probability and Polymarket price.
    Positive edge → market underpricing our side.
    
    e.g. our_prob=0.87, poly_price=0.55 → edge=0.32 (32%)
    """
    return our_prob - poly_price


def confidence_score(
    data_sources: int,         # how many independent sources agree
    time_remaining_pct: float, # less time = higher confidence
    score_diff_magnitude: int, # bigger lead = higher confidence
) -> float:
    """
    Composite confidence score (0–1) for our probability estimate.
    
    High confidence = multiple data sources agree + large lead + late in game.
    This gates whether we trade — low confidence = skip even with large edge.
    """
    source_score = min(data_sources / 3.0, 1.0)          # 3+ sources = full score
    time_score   = 1.0 - time_remaining_pct               # late game = high confidence
    lead_score   = min(abs(score_diff_magnitude) / 20.0, 1.0)  # 20pt lead = full

    # Weighted composite
    composite = (
        0.35 * source_score +
        0.40 * time_score   +
        0.25 * lead_score
    )
    return max(0.0, min(1.0, composite))


# ── Expected value ───────────────────────────────────────────────────────────

def expected_value(our_prob: float, poly_price: float, stake: float) -> float:
    """
    EV of buying YES at poly_price with our estimated true probability.
    Profit if correct = (1 - poly_price) × stake
    Loss if wrong     = poly_price × stake
    """
    profit_if_right = (1.0 - poly_price) / poly_price * stake
    ev = our_prob * profit_if_right - (1.0 - our_prob) * stake
    return ev
