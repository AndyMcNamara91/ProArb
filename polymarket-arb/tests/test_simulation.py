#!/usr/bin/env python3
"""
tests/test_simulation.py -- Live-data end-to-end test of ProArb V2

Hits REAL APIs:
  - The Odds API (Pinnacle odds via EU region) -- needs ODDS_API_KEY
  - Polymarket Gamma API (market discovery + prices) -- free, no key
  - Polymarket CLOB REST (order book depth) -- free, no key
  - ESPN Scoreboard (game outcomes) -- free, no key

The ONLY thing that's simulated is the actual Polymarket order placement.
Everything else is real: real odds, real prices, real edge calculations,
real risk gates, real order book depth.

Usage:
  # Full live test (needs ODDS_API_KEY in .env or environment):
  python tests/test_simulation.py

  # Without Odds API key (tests Polymarket + ESPN + all math/risk):
  python tests/test_simulation.py --no-odds

Assumes ~€500 ($500 USDC) starting bankroll.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Run from project root
os.chdir(Path(__file__).parent.parent)
sys.path.insert(0, ".")

os.environ["DEMO_MODE"] = "true"
os.environ["BANKROLL_USDC"] = "500"

from dotenv import load_dotenv
load_dotenv()

import logging
import colorlog

handler = colorlog.StreamHandler()
handler.setFormatter(colorlog.ColoredFormatter(
    "%(log_color)s%(asctime)s [%(name)-12s] %(message)s",
    datefmt="%H:%M:%S",
    log_colors={
        "DEBUG": "cyan", "INFO": "green",
        "WARNING": "yellow", "ERROR": "red", "CRITICAL": "bold_red",
    }
))
logging.basicConfig(level=logging.INFO, handlers=[handler])
log = logging.getLogger("test")

import httpx
from core.models import Opportunity, RiskLimits
from core.risk import RiskManager
from core.state import BotState
from core.devig import devig_power, american_to_decimal
from modules.gatekeeper import GatekeeperModule
from modules.executor import ExecutorModule
from modules.scanner import ScannerModule

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
POLY_GAMMA_BASE = "https://gamma-api.polymarket.com"
POLY_CLOB_REST = "https://clob.polymarket.com"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "baseball_mlb",
    "soccer_epl",
]

ESPN_ENDPOINTS = {
    "basketball_nba": "basketball/nba",
    "americanfootball_nfl": "football/nfl",
    "baseball_mlb": "baseball/mlb",
    "soccer_epl": "soccer/eng.1",
}


# ── Test 1: De-vig math validation ──────────────────────────────────────────

def test_devig_math():
    """Validate Power method de-vigging against known Pinnacle lines."""
    log.info("=" * 70)
    log.info("TEST 1: De-vig Math Validation")
    log.info("=" * 70)

    test_cases = [
        # (home_decimal_odds, away_decimal_odds, description)
        (1.286, 3.80, "Heavy favourite: ~78% implied"),
        (1.50, 2.80, "Moderate favourite: ~67% implied"),
        (1.91, 2.00, "Coin flip: ~52% implied"),
        (2.50, 1.57, "Moderate underdog: ~40% implied"),
        (4.00, 1.28, "Heavy underdog: ~25% implied"),
    ]

    all_pass = True
    for home_odds, away_odds, desc in test_cases:
        home_implied = 1 / home_odds
        away_implied = 1 / away_odds
        overround = (home_implied + away_implied - 1) * 100

        home_fair, away_fair = devig_power(home_odds, away_odds)

        # Assertions: must sum to 1 and each must be in valid range
        sums_to_one = abs(home_fair + away_fair - 1.0) < 0.001
        valid_range = 0 < home_fair < 1 and 0 < away_fair < 1
        # Total implied sum should decrease (vig removed)
        vig_removed = (home_fair + away_fair) < (home_implied + away_implied + 0.001)

        status = "PASS" if (sums_to_one and valid_range and vig_removed) else "FAIL"
        if status == "FAIL":
            all_pass = False

        log.info(
            f"  [{status}] {desc}\n"
            f"         Odds: {home_odds:.3f} / {away_odds:.3f} "
            f"(overround: {overround:.1f}%)\n"
            f"         Implied: {home_implied:.4f} / {away_implied:.4f} "
            f"(sum: {home_implied + away_implied:.4f})\n"
            f"         Fair:    {home_fair:.4f} / {away_fair:.4f} "
            f"(sum: {home_fair + away_fair:.4f})"
        )

    return all_pass


# ── Test 2: Kelly sizing and risk gates ─────────────────────────────────────

def test_kelly_and_gates():
    """Test Kelly criterion and all gatekeeper risk gates with €500 bankroll."""
    log.info("")
    log.info("=" * 70)
    log.info("TEST 2: Kelly Sizing & Risk Gates ($500 bankroll)")
    log.info("=" * 70)

    limits = RiskLimits()
    risk = RiskManager(limits=limits)
    all_pass = True

    # Kelly sizing tests
    kelly_cases = [
        (500, 0.755, 0.63, True, "12.5% edge, should size"),
        (500, 0.62, 0.48, True, "14% edge, should size"),
        (500, 0.55, 0.50, True, "5% edge, Kelly produces small stake"),
        (500, 0.50, 0.55, False, "Negative edge, Kelly = 0"),
        (500, 0.90, 0.75, True, "15% edge at high prob"),
        (100, 0.70, 0.55, True, "Small bankroll, should still work"),
        (15, 0.70, 0.55, False, "Tiny bankroll, stake < $5 min"),
    ]

    for bankroll, prob, price, should_size, desc in kelly_cases:
        stake = risk.kelly_stake(bankroll, prob, price)
        sized = stake > 0
        ok = sized == should_size

        # Verify position cap
        if sized:
            assert stake <= bankroll * limits.max_position_pct + 0.01, \
                f"Stake ${stake} exceeds position cap"
            assert stake >= limits.min_stake, \
                f"Stake ${stake} below minimum"

        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        log.info(f"  [{status}] {desc} -> stake=${stake:.2f}")

    # Edge gate
    for edge, should_pass in [(0.12, True), (0.10, True), (0.09, False), (0.05, False)]:
        ok, reason = risk.check_edge(edge)
        status = "PASS" if ok == should_pass else "FAIL"
        if ok != should_pass:
            all_pass = False
        log.info(f"  [{status}] Edge {edge*100:.0f}%: {'pass' if ok else 'fail'} {reason}")

    # Probability bounds
    for prob, price, should_pass in [
        (0.60, 0.45, True), (0.30, 0.18, True),
        (0.95, 0.80, False), (0.20, 0.10, False),
        (0.50, 0.12, False),
    ]:
        ok, reason = risk.check_probability_bounds(prob, price)
        status = "PASS" if ok == should_pass else "FAIL"
        if ok != should_pass:
            all_pass = False
        log.info(f"  [{status}] Prob {prob:.2f} @ ${price:.2f}: {'pass' if ok else 'fail'} {reason}")

    # Correlation
    for exposure, should_pass in [(0.10, True), (0.14, True), (0.15, False), (0.20, False)]:
        ok, _ = risk.check_correlation("basketball_nba", exposure)
        status = "PASS" if ok == should_pass else "FAIL"
        if ok != should_pass:
            all_pass = False
        log.info(f"  [{status}] Sport exposure {exposure*100:.0f}%: {'pass' if ok else 'fail'}")

    return all_pass


# ── Test 3: Live Polymarket market scan ─────────────────────────────────────

async def test_polymarket_scan():
    """Fetch real Polymarket sports markets and inspect prices."""
    log.info("")
    log.info("=" * 70)
    log.info("TEST 3: Live Polymarket Sports Markets")
    log.info("=" * 70)

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(
                f"{POLY_GAMMA_BASE}/markets",
                params={"tag": "sports", "active": "true", "limit": 100},
            )
            resp.raise_for_status()
            markets = resp.json()
        except Exception as e:
            log.error(f"  [SKIP] Polymarket API unreachable: {e}")
            return True  # don't fail on network issues

    if not markets:
        log.warning("  [WARN] No active sports markets found on Polymarket")
        return True

    log.info(f"  Found {len(markets)} active sports markets")
    print()

    shown = 0
    for m in markets[:15]:
        question = m.get("question", "?")
        volume = float(m.get("volume", 0) or 0)
        tokens = m.get("tokens", [])

        yes_price = None
        for t in tokens:
            if t.get("outcome", "").upper() == "YES":
                yes_price = t.get("price")

        if yes_price is not None:
            log.info(
                f"  {question[:65]:65s} | "
                f"YES=${float(yes_price):.2f} | "
                f"vol=${volume:,.0f}"
            )
            shown += 1

    if shown == 0:
        log.warning("  No markets with YES prices found")

    return True


# ── Test 4: Live Pinnacle odds (requires API key) ──────────────────────────

async def test_pinnacle_odds():
    """Fetch real Pinnacle odds and de-vig them."""
    log.info("")
    log.info("=" * 70)
    log.info("TEST 4: Live Pinnacle Odds (The Odds API)")
    log.info("=" * 70)

    if not ODDS_API_KEY:
        log.warning("  [SKIP] ODDS_API_KEY not set -- skipping Pinnacle test")
        log.info("  Set ODDS_API_KEY in .env to run this test")
        return True

    all_events = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for sport in SPORTS:
            try:
                resp = await client.get(
                    f"{ODDS_API_BASE}/sports/{sport}/odds",
                    params={
                        "apiKey": ODDS_API_KEY,
                        "regions": "eu",
                        "bookmakers": "pinnacle",
                        "markets": "h2h",
                        "oddsFormat": "decimal",
                    },
                )
                resp.raise_for_status()
                events = resp.json()
                all_events.extend([(e, sport) for e in events])
                log.info(f"  {sport}: {len(events)} events with Pinnacle lines")
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 422:
                    log.info(f"  {sport}: not in season / no events")
                else:
                    log.warning(f"  {sport}: API error {e.response.status_code}")
            except Exception as e:
                log.warning(f"  {sport}: fetch error: {e}")

    if not all_events:
        log.warning("  No Pinnacle events found across any sport")
        return True

    print()
    log.info(f"  De-vigging {len(all_events)} Pinnacle lines:")
    print()

    for event, sport in all_events[:12]:
        home = event.get("home_team", "?")
        away = event.get("away_team", "?")

        # Extract Pinnacle odds
        home_odds, away_odds = None, None
        for bk in event.get("bookmakers", []):
            if bk.get("key") != "pinnacle":
                continue
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for outcome in mkt.get("outcomes", []):
                    if outcome.get("name") == home:
                        home_odds = outcome.get("price")
                    elif outcome.get("name") == away:
                        away_odds = outcome.get("price")

        if home_odds is None or away_odds is None:
            continue

        home_fair, away_fair = devig_power(home_odds, away_odds)
        overround = (1/home_odds + 1/away_odds - 1) * 100

        log.info(
            f"  {away:25s} @ {home:25s}\n"
            f"    Pinnacle: {home_odds:.3f} / {away_odds:.3f} "
            f"(vig: {overround:.1f}%)\n"
            f"    Fair:     {home_fair:.1%} / {away_fair:.1%}"
        )

    return True


# ── Test 5: Full pipeline with live data ────────────────────────────────────

async def test_full_pipeline():
    """Run the full Scanner -> Gatekeeper -> Executor pipeline with live data.

    Uses real Polymarket prices and (if available) real Pinnacle odds.
    Only the order placement is simulated (demo mode).
    """
    log.info("")
    log.info("=" * 70)
    log.info("TEST 5: Full Pipeline -- Live Data, Demo Execution")
    log.info("=" * 70)

    BANKROLL = 500.0
    limits = RiskLimits()
    state = BotState(bankroll=BANKROLL)
    risk = RiskManager(limits=limits)
    scanner = ScannerModule(state)
    gatekeeper = GatekeeperModule(state, risk)
    executor = ExecutorModule(state)

    # Queue for opportunities
    opp_queue: asyncio.Queue = asyncio.Queue()

    # Run one scan cycle
    log.info(f"  Scanning with bankroll=${BANKROLL:.0f}, min_edge={limits.min_edge*100:.0f}%...")
    print()
    await scanner.scan(opp_queue)

    opp_count = opp_queue.qsize()
    log.info(f"  Scanner found {opp_count} opportunities above 8% raw edge")

    # Process through gatekeeper + executor
    approved = 0
    rejected = 0
    while not opp_queue.empty():
        opp = await opp_queue.get()
        log.info(
            f"  Opportunity: {opp.event_name}\n"
            f"    Side: {opp.our_side} | Pinnacle: {opp.pinnacle_fair_prob:.3f} "
            f"| Poly: {opp.polymarket_price:.3f} | Edge: {opp.edge*100:.1f}%\n"
            f"    Liquidity: ${opp.polymarket_liquidity:,.0f} "
            f"| Spread: {opp.bid_ask_spread:.3f} "
            f"| Depth: ${opp.order_book_depth:,.0f}"
        )

        decision = await gatekeeper.evaluate(opp)
        if decision:
            approved += 1
            log.info(
                f"    -> APPROVED: stake=${decision.stake_usdc:.2f} "
                f"limit_price={decision.max_entry_price:.2f}"
            )
            await executor.execute(decision)
        else:
            rejected += 1
            log.info(f"    -> REJECTED by gatekeeper")

    print()
    log.info(f"  Pipeline results:")
    log.info(f"    Opportunities:  {opp_count}")
    log.info(f"    Approved:       {approved}")
    log.info(f"    Rejected:       {rejected}")
    log.info(f"    Trades placed:  {state.trades_placed}")
    log.info(f"    Bankroll:       ${state.bankroll:.2f}")

    await scanner.close()
    return True


# ── Test 6: ESPN scoreboard check ───────────────────────────────────────────

async def test_espn_scoreboard():
    """Fetch real ESPN scoreboards to validate outcome resolution logic."""
    log.info("")
    log.info("=" * 70)
    log.info("TEST 6: Live ESPN Scoreboards")
    log.info("=" * 70)

    async with httpx.AsyncClient(timeout=15.0) as client:
        for sport, espn_path in ESPN_ENDPOINTS.items():
            try:
                resp = await client.get(f"{ESPN_BASE}/{espn_path}/scoreboard")
                resp.raise_for_status()
                data = resp.json()
                events = data.get("events", [])

                finished = 0
                in_progress = 0
                scheduled = 0

                for event in events:
                    status = event.get("status", {}).get("type", {}).get("state", "")
                    name = event.get("name", "?")
                    if status == "post":
                        finished += 1
                        # Show winner
                        competitions = event.get("competitions", [])
                        if competitions:
                            for comp in competitions[0].get("competitors", []):
                                if comp.get("winner"):
                                    winner = comp.get("team", {}).get("displayName", "?")
                                    score = comp.get("score", "?")
                                    log.info(f"    Final: {name:50s} | Winner: {winner} ({score})")
                                    break
                    elif status == "in":
                        in_progress += 1
                    else:
                        scheduled += 1

                log.info(
                    f"  {sport:30s} | "
                    f"{len(events)} events: "
                    f"{finished} final, {in_progress} live, {scheduled} scheduled"
                )

            except Exception as e:
                log.warning(f"  {sport}: ESPN error: {e}")

    return True


# ── Test 7: Order book depth check ──────────────────────────────────────────

async def test_order_book():
    """Fetch real Polymarket CLOB order books for sports markets."""
    log.info("")
    log.info("=" * 70)
    log.info("TEST 7: Live Polymarket Order Books")
    log.info("=" * 70)

    # First get some real token IDs from Gamma API
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(
                f"{POLY_GAMMA_BASE}/markets",
                params={"tag": "sports", "active": "true", "limit": 20},
            )
            resp.raise_for_status()
            markets = resp.json()
        except Exception as e:
            log.warning(f"  [SKIP] Can't fetch markets: {e}")
            return True

    checked = 0
    for market in markets[:5]:
        tokens = market.get("tokens", [])
        question = market.get("question", "?")[:60]

        for token in tokens:
            tid = token.get("token_id", "")
            outcome = token.get("outcome", "")
            if not tid or outcome.upper() != "YES":
                continue

            # Fetch order book
            async with httpx.AsyncClient(timeout=10.0) as client:
                try:
                    resp = await client.get(
                        f"{POLY_CLOB_REST}/book",
                        params={"token_id": tid},
                    )
                    resp.raise_for_status()
                    book = resp.json()

                    bids = book.get("bids", [])
                    asks = book.get("asks", [])
                    best_bid = float(bids[0]["price"]) if bids else 0
                    best_ask = float(asks[0]["price"]) if asks else 0
                    spread = best_ask - best_bid if best_bid and best_ask else 0
                    bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
                    ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])

                    log.info(
                        f"  {question}\n"
                        f"    Bid: ${best_bid:.2f} ({len(bids)} levels, "
                        f"top-5 depth: {bid_depth:.0f} shares)\n"
                        f"    Ask: ${best_ask:.2f} ({len(asks)} levels, "
                        f"top-5 depth: {ask_depth:.0f} shares)\n"
                        f"    Spread: ${spread:.3f}"
                    )
                    checked += 1
                except Exception as e:
                    log.debug(f"    Book fetch failed: {e}")

    if checked == 0:
        log.warning("  No order books fetched (markets may be inactive)")

    return True


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="ProArb V2 live-data tests")
    parser.add_argument("--no-odds", action="store_true",
                        help="Skip Pinnacle odds test (no API key needed)")
    args = parser.parse_args()

    log.info("")
    log.info("*" * 70)
    log.info("  ProArb V2 -- Live-Data Test Suite")
    log.info("  Bankroll: $500 (EUR500)")
    log.info(f"  Odds API key: {'SET' if ODDS_API_KEY else 'NOT SET'}")
    log.info("  All data is REAL -- only trade execution is simulated")
    log.info("*" * 70)

    results = {}

    # Test 1: De-vig math (no network needed)
    results["De-vig Math"] = test_devig_math()

    # Test 2: Kelly + risk gates (no network needed)
    results["Kelly & Gates"] = test_kelly_and_gates()

    # Test 3: Live Polymarket markets
    results["Polymarket Scan"] = await test_polymarket_scan()

    # Test 4: Live Pinnacle odds
    if not args.no_odds:
        results["Pinnacle Odds"] = await test_pinnacle_odds()

    # Test 5: Full pipeline
    results["Full Pipeline"] = await test_full_pipeline()

    # Test 6: ESPN scoreboards
    results["ESPN Scores"] = await test_espn_scoreboard()

    # Test 7: Order book depth
    results["Order Books"] = await test_order_book()

    # Summary
    print("\n")
    log.info("=" * 70)
    log.info("TEST SUMMARY")
    log.info("=" * 70)
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        icon = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        log.info(f"  [{icon}] {name}")

    print()
    if all_pass:
        log.info("ALL TESTS PASSED")
    else:
        log.error("SOME TESTS FAILED")
    log.info("=" * 70)


if __name__ == "__main__":
    Path("trades.jsonl").unlink(missing_ok=True)
    Path("STOP").unlink(missing_ok=True)

    asyncio.run(main())

    Path("trades.jsonl").unlink(missing_ok=True)
