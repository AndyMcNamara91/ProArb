"""
core/devig.py -- Pinnacle odds de-vigging using the Power method

The Power method is the most accurate de-vigging approach for handling
favourite-longshot bias. It finds exponent k such that
p_home^(1/k) + p_away^(1/k) = 1, producing fair probabilities.
"""

import logging

log = logging.getLogger("devig")


def devig_power(odds_home: float, odds_away: float) -> tuple[float, float]:
    """
    De-vig decimal odds using the Power method.

    Args:
        odds_home: Decimal odds for home team (e.g. 1.286 for -350)
        odds_away: Decimal odds for away team (e.g. 3.80 for +280)

    Returns:
        (fair_home_prob, fair_away_prob) summing to 1.0
    """
    if odds_home <= 1.0 or odds_away <= 1.0:
        log.warning(f"Invalid odds: home={odds_home}, away={odds_away}")
        return 0.5, 0.5

    p_home = 1.0 / odds_home  # implied prob with vig
    p_away = 1.0 / odds_away

    # Binary search for k where p_home^(1/k) + p_away^(1/k) = 1
    lo, hi = 0.01, 5.0
    for _ in range(100):
        k = (lo + hi) / 2.0
        total = p_home ** (1.0 / k) + p_away ** (1.0 / k)
        if total > 1.0:
            lo = k
        else:
            hi = k

    fair_home = p_home ** (1.0 / k)
    fair_away = p_away ** (1.0 / k)

    # Normalise to ensure they sum exactly to 1.0
    s = fair_home + fair_away
    fair_home /= s
    fair_away /= s

    return fair_home, fair_away


def american_to_decimal(odds: int) -> float:
    """Convert American moneyline odds to decimal odds.

    +150 -> 2.50, -200 -> 1.50
    """
    if odds > 0:
        return 1.0 + odds / 100.0
    else:
        return 1.0 + 100.0 / abs(odds)


def decimal_to_american(odds: float) -> int:
    """Convert decimal odds to American moneyline."""
    if odds >= 2.0:
        return round((odds - 1.0) * 100)
    else:
        return round(-100.0 / (odds - 1.0))
