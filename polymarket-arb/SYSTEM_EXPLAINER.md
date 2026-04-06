# ProArb — Polymarket Sports Arbitrage Bot

## The Core Idea

Bookmakers (DraftKings, FanDuel, etc.) price sports outcomes using professional odds models. Polymarket is a prediction market where prices are set by retail traders. Sometimes Polymarket misprices an outcome — e.g. bookmakers say a team has a 75% chance of winning, but Polymarket is only pricing it at 55%. That 20% gap is "edge." The bot finds and exploits these gaps.

## Architecture — 5 Agents in a Pipeline

The bot runs as a single Python process using `asyncio`. Seven coroutines run concurrently via `asyncio.gather()`:

```
Scanner → Analyst → Executor → Outcome Checker
                                    ↑
                         Risk Monitor (background)
                         Dashboard (web UI)
                         Stop Watcher (kill switch)
```

## Agent 1: Scanner (`agents/scanner.py`)

**Job:** Find pricing gaps between the real world and Polymarket.

Two data sources, run in parallel:

1. **ESPN Scoreboard API** (free, no key) — Polls 12 sports leagues every 30 seconds for live scores. Uses score + time remaining to compute a win probability via a logistic model. For example: team leading by 15 points with 2 minutes left in basketball = ~95% win probability.

2. **The Odds API** (key required, rate-limited to every 15 minutes) — Fetches consensus bookmaker odds across 6 sports. Takes the median moneyline from all bookmakers, strips the vig (house edge), and converts to a fair probability.

Then for each live game, it searches Polymarket's Gamma API for a matching market (using team nickname matching). If our estimated probability minus the Polymarket price > 6%, it queues an `Opportunity` for the Analyst.

**ESPN scans 12 leagues for free:** NBA, NFL, NCAAB, MLB, NHL, EPL, MLS, La Liga, Bundesliga, Serie A, Ligue 1, Champions League.

**Odds API queries 6 sports** (costs credits): NBA, NFL, NCAAB, MLB, NHL, EPL.

## Agent 2: Analyst (`agents/analyst.py`)

**Job:** Decide if an opportunity is actually worth trading.

Applies 5 gates in order:

1. **Longshot filter** — Skip if win probability < 40% or Polymarket price < $0.20. Prevents betting on unlikely outcomes where the model is unreliable.
2. **Staleness check** — Skip if the opportunity is more than 10 seconds old (prices move fast).
3. **Confidence scoring** — Composite score based on: how many data sources agree, how late in the game it is, how big the lead is. Late game + big lead + multiple sources = high confidence.
4. **Risk gate** — Checks edge > 8%, confidence > 30%, and open positions < 20.
5. **Kelly sizing** — Calculates optimal bet size using fractional Kelly Criterion (15% Kelly). With a $108 bankroll, this typically produces ~$3.24 stakes.
6. **Expected value check** — Skip if EV < $0.50.

If all gates pass, it creates a `TradeDecision` and queues it for the Executor.

## Agent 3: Executor (`agents/executor.py`)

**Job:** Place the bet (or simulate it in demo mode).

Before placing:
- Checks for the STOP kill switch file
- **Dedup check** — Extracts team nicknames from the event name. Checks against: (a) an in-memory set of nicknames already bet on this cycle, and (b) all pending trades in state. Prevents betting on the same game twice even if ESPN and Odds API both flag it.

In **demo mode** (current): Logs the trade as "pending_outcome" with all details (teams, sport, entry price, stake). No real money moves.

In **live mode**: Uses `py-clob-client` to place a limit order on Polymarket's CLOB (Central Limit Order Book) with a 2% slippage tolerance.

## Agent 4: Outcome Checker (`agents/outcome_checker.py`)

**Job:** Resolve pending bets against actual game results.

Polls ESPN every 30 seconds. When a game's state changes to "post" (finished), it:

1. Matches the finished game to pending trades using whole-word team nickname matching (prevents "nets" matching "hornets")
2. Determines the winner from ESPN's final score
3. Calculates real P&L:
   - **Win:** profit = (1 - entry_price) / entry_price x stake
   - **Loss:** loss = -stake
4. Updates the trade status to "won" or "lost" and writes to `trades.jsonl`

This is the key difference from fake backtesting — every trade resolves against an actual ESPN-confirmed game outcome.

## Agent 5: Risk Monitor (`core/risk.py`)

**Job:** Prevent catastrophic losses.

Runs continuously and enforces:
- Daily loss limit ($108 — the full bankroll)
- Maximum position size (3% of bankroll per trade)
- Maximum 20 open positions at once
- Minimum edge (8%) and confidence (30%) thresholds

## Dashboard (`dashboard/`)

FastAPI web server on port 8080. Shows running status, session P&L, trade count, open positions. Accessible at `localhost:8080`.

## Stop Watcher

Monitors for a file called `STOP` in the working directory. If it appears, the executor refuses all new orders. Simple kill switch.

## State Persistence (`core/state.py`)

All trades are appended to `trades.jsonl` as newline-delimited JSON. On restart, `BotState` reloads all pending_outcome trades so the dedup logic knows what's already been bet on.

## Data Flow Example

```
1. ESPN reports: Celtics 95, Raptors 78, 4th quarter, 3:20 left
2. Scanner computes: Celtics win prob = 96%
3. Scanner finds Polymarket market: "Will Celtics win?" priced at $0.72
4. Scanner: edge = 96% - 72% = 24% → queues opportunity
5. Analyst: prob 96% > 40% ✓, price $0.72 > $0.20 ✓, edge 24% > 8% ✓
6. Analyst: Kelly stake = $3.24, EV = $2.88 → queues trade
7. Executor: no existing Celtics bet → places demo trade, YES @ $0.72
8. [2 hours later] ESPN: game final, Celtics 112-98
9. Outcome Checker: Celtics won, we had YES → profit = (1-0.72)/0.72 × $3.24 = +$1.26
```

## Key Configuration (`.env`)

```
DEMO_MODE=true          # No real money
BANKROLL_USDC=108       # €100 = $108
KELLY_FRACTION=0.15     # Conservative 15% Kelly
MIN_EDGE=0.08           # 8% minimum edge
MAX_OPEN_POSITIONS=20   # Up to 20 concurrent bets
SCAN_INTERVAL_SEC=2     # Main loop every 2 seconds
```

## Credit Conservation

The Odds API free tier gives 500 requests/month. Each poll costs 1 request per sport. At 6 sports every 15 minutes, that's ~24 requests/hour. ESPN is completely free and unlimited, so it does the heavy lifting with 12 leagues polled every 30 seconds.
