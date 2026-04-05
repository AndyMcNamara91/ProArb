# CLAUDE.md — Polymarket Sports Arbitrage Bot
# Build plan for Claude Code
# ============================================================
# HOW TO USE THIS FILE
# --------------------
# 1. Install Claude Code:  npm install -g @anthropic-ai/claude-code
# 2. Open your project:    cd polymarket-arb && claude
# 3. Say:                  "Follow CLAUDE.md and build Phase 1"
# 4. Claude Code will read this file and execute autonomously.
#
# Work through phases in order. Each phase is self-contained
# and ends with a working, testable deliverable.
# ============================================================

## PROJECT OVERVIEW

Polymarket sports arbitrage bot — detects pricing gaps between live
sports bookmaker odds and Polymarket prediction market contracts,
then trades the mispriced side before the market corrects.

Three Python agents run as async tasks in a single process:
  - Scanner  : polls sports odds APIs, detects edge vs Polymarket
  - Analyst  : validates edge, confidence, sizes Kelly stake
  - Executor : places orders on Polymarket CLOB

A FastAPI dashboard server serves a live HTML/JS dashboard that
reads trades.jsonl and shows P&L, win rate, open positions, trade log.

## TECH STACK

Backend : Python 3.11+, asyncio, FastAPI, py-clob-client, requests
Frontend: Single HTML file (vanilla JS, no framework, no bundler)
Storage : trades.jsonl (append-only ledger), .env for config
APIs    : The Odds API (sports), Polymarket Gamma API + CLOB API

## FILE STRUCTURE (target state)

```
polymarket-arb/
├── CLAUDE.md              ← this file
├── main.py                ← entry point, spins up all agents + dashboard
├── requirements.txt
├── .env.example
├── .env                   ← not committed, created by user
├── STOP                   ← kill switch (touch to halt, rm to resume)
├── trades.jsonl           ← trade ledger (auto-created)
│
├── agents/
│   ├── __init__.py
│   ├── scanner.py         ← Agent 1: sports odds → gap detection
│   ├── analyst.py         ← Agent 2: edge validation + Kelly sizing
│   └── executor.py        ← Agent 3: Polymarket CLOB order placement
│
├── core/
│   ├── __init__.py
│   ├── probability.py     ← odds conversion, edge calc, confidence score
│   ├── risk.py            ← RiskManager: Kelly, loss limits, gates
│   └── state.py           ← BotState: shared state, ledger, P&L
│
└── dashboard/
    ├── server.py          ← FastAPI server: serves dashboard + /api/trades
    └── index.html         ← Single-page dashboard UI
```

## CODING STANDARDS

- All Python files: type hints, docstrings on every class and public method
- Async everywhere: use asyncio.Queue between agents, never blocking calls
- Errors: catch and log, never crash the main loop
- Logging: colorlog, format = "HH:MM:SS [agent-name] message"
- Config: everything via os.getenv() with safe defaults
- Demo mode: DEMO_MODE=true (default) runs full pipeline, never executes orders
- Kill switch: check Path("STOP").exists() before every order

## ENV VARIABLES

```
DEMO_MODE=true
POLY_PRIVATE_KEY=
POLY_FUNDER_ADDRESS=
POLY_CHAIN_ID=137
ODDS_API_KEY=
BANKROLL_USDC=500
DAILY_LOSS_LIMIT_USD=50
MAX_POSITION_PCT=0.05
MAX_OPEN_POSITIONS=3
MIN_EDGE=0.08
MIN_CONFIDENCE=0.70
KELLY_FRACTION=0.25
SLIPPAGE_TOLERANCE=0.02
SCAN_INTERVAL_SEC=2
DASHBOARD_PORT=8080
```

---

# ═══════════════════════════════════════════════════════════════
# PHASE 0 — Project scaffold + dependency validation
# ═══════════════════════════════════════════════════════════════

## Goal
Create the full folder structure, requirements.txt, .env.example,
and verify that all dependencies install cleanly.

## Tasks

1. Create directory structure:
   ```
   mkdir -p agents core dashboard
   touch agents/__init__.py core/__init__.py
   ```

2. Write requirements.txt:
   ```
   py-clob-client>=0.14.0
   requests>=2.31.0
   websockets>=12.0
   python-dotenv>=1.0.0
   colorlog>=6.8.0
   fastapi>=0.110.0
   uvicorn>=0.27.0
   ```

3. Write .env.example with all variables listed above, commented.

4. Run: `pip install -r requirements.txt`
   Fix any version conflicts before proceeding.

5. Create a minimal main.py that just prints "Bot scaffold ready"
   and exits cleanly.

## Done when
`python main.py` runs without errors and all imports resolve.

---

# ═══════════════════════════════════════════════════════════════
# PHASE 1 — core/probability.py
# ═══════════════════════════════════════════════════════════════

## Goal
Implement all probability math. This module has zero external
dependencies — test it in isolation.

## Functions to implement

```python
def american_to_prob(odds: int) -> float:
    """American moneyline → implied probability.
    +150 → 0.400, -200 → 0.667"""

def decimal_to_prob(odds: float) -> float:
    """Decimal odds → probability. 2.50 → 0.400"""

def remove_vig(home_prob: float, away_prob: float) -> tuple[float, float]:
    """Normalise two raw implied probs to remove bookmaker overround.
    e.g. (0.55, 0.52) → (0.514, 0.486)"""

def in_game_win_prob(score_diff: int, time_remaining_pct: float,
                     sport: str = "basketball") -> float:
    """Logistic model for in-game win probability.
    score_diff > 0 = home leading. Returns probability for home team.
    Sport k values: basketball=0.15, football=0.25, baseball=0.20, soccer=0.30"""

def calculate_edge(our_prob: float, poly_price: float) -> float:
    """our_prob - poly_price. Positive = market underpricing our side."""

def confidence_score(data_sources: int, time_remaining_pct: float,
                     score_diff_magnitude: int) -> float:
    """Composite 0-1 confidence score.
    Weights: sources=0.35, time=0.40, lead=0.25"""

def expected_value(our_prob: float, poly_price: float, stake: float) -> float:
    """EV of buying YES at poly_price. 
    EV = our_prob * profit_if_right - (1-our_prob) * stake"""
```

## Tests to write inline (use assert statements at bottom of file)
- american_to_prob(+150) ≈ 0.400
- american_to_prob(-200) ≈ 0.667
- remove_vig(0.55, 0.52) → sum to 1.0
- calculate_edge(0.87, 0.55) == 0.32
- confidence_score(3, 0.05, 18) > 0.8  (late game, big lead, 3 sources)

## Done when
`python core/probability.py` runs all asserts without error.

---

# ═══════════════════════════════════════════════════════════════
# PHASE 2 — core/risk.py
# ═══════════════════════════════════════════════════════════════

## Goal
RiskManager class: Kelly sizing + all trade gates.

## Class: RiskManager

```python
class RiskManager:
    def __init__(self, daily_loss_limit: float, max_position_pct: float): ...

    def kelly_stake(self, bankroll: float, our_prob: float,
                    market_price: float) -> float:
        """Full Kelly × kelly_fraction, capped at max_position_pct × bankroll.
        Returns 0.0 if Kelly is negative (no edge)."""

    def is_tradeable(self, edge: float, confidence: float,
                     open_positions: int) -> tuple[bool, str]:
        """Gate check. Returns (True, "") or (False, reason_string).
        Gates: daily_loss_limit, min_edge, min_confidence, max_open_positions"""

    def record_loss(self, amount: float) -> None:
        """Accumulate daily loss total."""

    def daily_loss_breached(self) -> bool:
        """True if daily loss >= limit."""

    def reset_daily(self) -> None:
        """Reset daily loss counter (call at midnight)."""
```

## Kelly formula
```
b = (1 / market_price) - 1      # net odds
f* = (our_prob * b - (1-our_prob)) / b
stake = min(f* * kelly_fraction, max_position_pct) * bankroll
```

## Tests
- kelly_stake(500, 0.87, 0.55) → between $5 and $25 (reasonable range)
- kelly_stake(500, 0.45, 0.55) → 0.0 (negative edge)
- is_tradeable(0.12, 0.75, 1) → (True, "")
- is_tradeable(0.04, 0.75, 1) → (False, contains "edge")
- is_tradeable(0.12, 0.50, 1) → (False, contains "confidence")

## Done when
`python core/risk.py` runs all asserts without error.

---

# ═══════════════════════════════════════════════════════════════
# PHASE 3 — core/state.py
# ═══════════════════════════════════════════════════════════════

## Goal
BotState: shared mutable state and trade ledger across all agents.

## Dataclass: Trade

```python
@dataclass
class Trade:
    timestamp:      float
    market_id:      str
    event_name:     str
    side:           str          # YES | NO
    token_id:       str
    entry_price:    float
    size_usdc:      float
    edge_at_entry:  float
    sports_prob:    float
    poly_prob:      float
    status:         str = "open" # open | filled | demo_filled | failed
    fill_price:     Optional[float] = None
    pnl:            Optional[float] = None
    order_id:       Optional[str]   = None
```

## Class: BotState

```python
class BotState:
    session_pnl:    float   # running total
    trades_placed:  int
    trades_skipped: int

    def record_trade(self, trade: Trade) -> None:
        """Store in _open_trades dict, increment counter, append to trades.jsonl"""

    def close_trade(self, order_id: str, fill_price: float, pnl: float) -> None:
        """Move from open to closed, update session_pnl, append updated record"""

    def skip_trade(self, reason: str) -> None:
        """Increment skipped counter, debug log"""

    @property
    def daily_loss(self) -> float: ...

    @property
    def open_positions(self) -> int: ...
```

## Ledger format (trades.jsonl)
One JSON object per line. Each trade written twice:
once on open (status="open"), once on close (status="filled").
The dashboard reads this file.

## Done when
Manual test: create BotState, record 2 trades, close 1, verify
trades.jsonl has 3 lines (2 opens + 1 close update).

---

# ═══════════════════════════════════════════════════════════════
# PHASE 4 — agents/scanner.py
# ═══════════════════════════════════════════════════════════════

## Goal
Agent 1: poll The Odds API, match to Polymarket, detect gaps,
push Opportunity objects to asyncio.Queue.

## Dataclass: Opportunity

```python
@dataclass
class Opportunity:
    timestamp:           float
    event_name:          str
    sport:               str
    market_id:           str       # Polymarket market ID
    token_id:            str       # Polymarket YES token ID
    our_side:            str       # YES | NO
    our_prob:            float
    poly_price:          float
    raw_edge:            float
    score_diff:          int       # 0 if not available yet
    time_remaining_pct:  float     # 0.5 default until live score wired in
    data_sources:        int       # number of bookmakers agreeing
    source_detail:       str
```

## Class: ScannerAgent

```python
class ScannerAgent:
    def __init__(self, state: BotState): ...

    async def scan(self, queue: asyncio.Queue) -> None:
        """One scan cycle. Called every SCAN_INTERVAL_SEC.
        If ODDS_API_KEY not set: run _demo_scan() instead."""

    async def _refresh_poly_markets(self) -> None:
        """Cache Polymarket sports markets from gamma-api.polymarket.com/markets
        with tag=sports. Refresh every 5 minutes."""

    async def _scan_sport(self, sport: str) -> list[Opportunity]:
        """Fetch odds for one sport, find Polymarket matches, compute edges."""

    def _process_event(self, event: dict, sport: str) -> Optional[Opportunity]:
        """Single event → Opportunity or None.
        Steps:
        1. Extract home/away odds from all bookmakers
        2. Take median odds (consensus)
        3. Convert to probability, remove vig
        4. Find matching Polymarket market (fuzzy name match)
        5. Fetch YES price from market tokens
        6. Calculate edge on both YES and NO side
        7. Return whichever side has edge > RAW_EDGE_THRESHOLD (0.06)"""

    async def _demo_scan(self, queue: asyncio.Queue) -> None:
        """Emit a fake Opportunity 30% of the time for pipeline testing.
        event_name = 'DEMO Lakers vs Celtics'
        our_prob = 0.84, poly_price = 0.55, raw_edge = 0.29"""
```

## Sports to monitor
```python
SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "basketball_ncaab",
    "baseball_mlb",
]
```

## API endpoints
- Odds: GET https://api.the-odds-api.com/v4/sports/{sport}/odds
  params: apiKey, regions=us, markets=h2h, oddsFormat=american
- Poly markets: GET https://gamma-api.polymarket.com/markets
  params: tag=sports, active=true, limit=200

## Done when
With DEMO_MODE and no API keys: `python -c "
import asyncio, os
os.environ['DEMO_MODE'] = 'true'
from core.state import BotState
from agents.scanner import ScannerAgent
import asyncio

async def test():
    state = BotState()
    scanner = ScannerAgent(state)
    q = asyncio.Queue()
    for _ in range(10):
        await scanner.scan(q)
    print(f'Queue size: {q.qsize()} (should be > 0)')

asyncio.run(test())
"`

---

# ═══════════════════════════════════════════════════════════════
# PHASE 5 — agents/analyst.py
# ═══════════════════════════════════════════════════════════════

## Goal
Agent 2: receive Opportunity, apply all gates, size stake, emit TradeDecision.

## Dataclass: TradeDecision

```python
@dataclass
class TradeDecision:
    opportunity:   Opportunity
    stake_usdc:    float
    confidence:    float
    expected_val:  float
    decision_ts:   float
```

## Class: AnalystAgent

```python
class AnalystAgent:
    def __init__(self, state: BotState, risk: RiskManager): ...

    async def evaluate(self, opp: Opportunity) -> Optional[TradeDecision]:
        """
        Pipeline:
        1. Staleness check: discard if opp.timestamp > 10s ago
        2. confidence_score() from core.probability
        3. risk.is_tradeable() gate
        4. risk.kelly_stake() sizing
        5. If stake < $1.00: skip
        6. expected_value() check: skip if EV < $0.50
        7. Slippage warning if stake > 2% of estimated $5000 market depth
        8. Return TradeDecision or None
        """
```

## Constants
```python
MAX_OPP_AGE_SEC  = 10.0
MIN_EV_USDC      = 0.50
BANKROLL_USDC    = float(os.getenv("BANKROLL_USDC", 500))
```

## Done when
Unit test with a fake Opportunity:
- High edge (0.29), high confidence → TradeDecision returned
- Low edge (0.03) → None returned, skip logged
- Stale timestamp (20s ago) → None returned

---

# ═══════════════════════════════════════════════════════════════
# PHASE 6 — agents/executor.py
# ═══════════════════════════════════════════════════════════════

## Goal
Agent 3: receive TradeDecision, place order (or simulate in demo mode),
record in BotState.

## Class: ExecutorAgent

```python
class ExecutorAgent:
    def __init__(self, state: BotState, risk: RiskManager): ...

    async def execute(self, decision: TradeDecision) -> None:
        """
        1. Check STOP file — refuse if exists
        2. Check decision age — skip if > 8s old
        3. If DEMO_MODE: log "[DEMO] WOULD PLACE ...", record demo trade, return
        4. _get_client(): lazy-init py-clob-client with credentials
        5. _place_order(): limit order with slippage cap
        6. record_trade() in state on success
        """

    async def _place_order(self, client, decision: TradeDecision) -> str:
        """
        limit_price = poly_price + SLIPPAGE_TOLERANCE (for YES)
                    = poly_price - SLIPPAGE_TOLERANCE (for NO)
        size_shares = stake_usdc / poly_price
        Use OrderArgs(token_id, price, size, side=BUY|SELL)
        Call client.create_and_post_order(order_args)
        Return orderID string
        """

    def _get_client(self):
        """Lazy init ClobClient. Raise RuntimeError if credentials missing."""
```

## Done when
Demo mode: `python -c "
import asyncio
from agents.executor import ExecutorAgent
from core.state import BotState
from core.risk import RiskManager
# create a fake TradeDecision and call execute()
# verify trades.jsonl gets a line written
"`

---

# ═══════════════════════════════════════════════════════════════
# PHASE 7 — main.py (full async orchestration)
# ═══════════════════════════════════════════════════════════════

## Goal
Wire all agents together with asyncio.gather(), add risk monitor,
add STOP file watcher.

## Structure

```python
async def main():
    state = BotState()
    risk  = RiskManager(...)

    scanner  = ScannerAgent(state)
    analyst  = AnalystAgent(state, risk)
    executor = ExecutorAgent(state, risk)

    opportunity_queue: asyncio.Queue = asyncio.Queue()
    order_queue:       asyncio.Queue = asyncio.Queue()

    async def run_scanner():
        while not Path("STOP").exists():
            await scanner.scan(opportunity_queue)
            await asyncio.sleep(SCAN_INTERVAL_SEC)

    async def run_analyst():
        while not Path("STOP").exists():
            opp = await asyncio.wait_for(opportunity_queue.get(), timeout=5)
            decision = await analyst.evaluate(opp)
            if decision:
                await order_queue.put(decision)

    async def run_executor():
        while not Path("STOP").exists():
            order = await asyncio.wait_for(order_queue.get(), timeout=5)
            await executor.execute(order)

    async def run_risk_monitor():
        while not Path("STOP").exists():
            if risk.daily_loss_breached():
                Path("STOP").touch()
                break
            await asyncio.sleep(10)

    await asyncio.gather(
        run_scanner(),
        run_analyst(),
        run_executor(),
        run_risk_monitor(),
    )
```

## Done when
`python main.py` runs for 30 seconds in demo mode,
produces log output from all three agents,
writes to trades.jsonl,
exits cleanly on Ctrl+C with session summary logged.

---

# ═══════════════════════════════════════════════════════════════
# PHASE 8 — dashboard/server.py (FastAPI)
# ═══════════════════════════════════════════════════════════════

## Goal
Lightweight FastAPI server that:
1. Serves dashboard/index.html at GET /
2. Exposes GET /api/trades — reads trades.jsonl, returns JSON
3. Exposes GET /api/status — returns bot running status, session P&L,
   open positions count, uptime
4. Runs on DASHBOARD_PORT (default 8080)

## Endpoints

```python
GET /
    → serve dashboard/index.html as HTML response

GET /api/trades
    → read trades.jsonl line by line, parse JSON, return list
    → query params: ?limit=50&status=all|open|filled
    → response: { trades: [...], total: int, session_pnl: float }

GET /api/status
    → response: {
        running: bool,
        demo_mode: bool,
        session_pnl: float,
        trades_placed: int,
        trades_skipped: int,
        open_positions: int,
        uptime_seconds: float,
        kill_switch_active: bool,
        daily_loss: float,
        daily_loss_limit: float
      }
```

## CORS
Allow all origins (localhost only in practice).

## Integration with main.py
In main.py Phase 7, add a 5th coroutine:

```python
async def run_dashboard():
    import uvicorn
    config = uvicorn.Config(
        "dashboard.server:app",
        host="0.0.0.0",
        port=int(os.getenv("DASHBOARD_PORT", 8080)),
        log_level="warning",  # don't pollute bot logs
    )
    server = uvicorn.Server(config)
    await server.serve()
```

Pass the BotState instance to the FastAPI app via app.state.

## Done when
`python main.py` starts, open http://localhost:8080/api/trades
returns valid JSON (empty list if no trades yet).

---

# ═══════════════════════════════════════════════════════════════
# PHASE 9 — dashboard/index.html (live dashboard UI)
# ═══════════════════════════════════════════════════════════════

## Goal
Single HTML file, no build step, opens in browser at localhost:8080.
Auto-refreshes every 3 seconds via fetch() to /api/trades and /api/status.

## Design direction
Dark terminal aesthetic. Monospace numbers. Tight information density.
Feels like a trading terminal, not a consumer app.
Fonts: Syne (headings) + DM Mono (numbers/data) + DM Sans (body).
Colour palette: near-black bg (#080a0d), green (#23d18b) for profit/positive,
red (#f14c4c) for loss/negative, amber (#f0a500) for warnings,
blue (#4d9fff) for neutral data.

## Sections

### 1. Topbar (sticky)
- Left: brand mark + "ARB BOT" wordmark
- Centre: status pill — green "LIVE" / amber "DEMO" / red "STOPPED"
- Right: last-updated timestamp + auto-refresh countdown

### 2. KPI strip (4 cards in a row)
- Session P&L (large number, green/red)
- Win Rate % (closed trades only)
- Trades Today (placed / skipped)
- Open Positions (count / max)

### 3. Daily loss gauge
- Horizontal bar: current daily loss vs limit
- Colour shifts amber at 60%, red at 85%

### 4. Live trade feed (scrollable table)
Columns: Time | Event | Side | Entry | Edge | Stake | Status | P&L
- Status pill: OPEN (blue), FILLED (green), DEMO (amber), FAILED (red)
- Sort: newest first
- Limit: 50 rows

### 5. Stats sidebar (right column on wide screens)
- Best trade (highest P&L)
- Worst trade
- Average edge at entry
- Average confidence
- Total volume traded ($)

## Auto-refresh logic
```javascript
async function refresh() {
    const [status, trades] = await Promise.all([
        fetch('/api/status').then(r => r.json()),
        fetch('/api/trades?limit=50').then(r => r.json())
    ]);
    renderStatus(status);
    renderTrades(trades);
    updateTimestamp();
}
setInterval(refresh, 3000);
refresh(); // immediate first load
```

## Error state
If fetch fails (bot not running): show a banner
"Bot offline — start with: python main.py"

## Done when
Open http://localhost:8080 in browser.
Dashboard shows correct data from trades.jsonl.
P&L updates within 3 seconds of a new trade being written.
Works on mobile (responsive layout stacks to single column).

---

# ═══════════════════════════════════════════════════════════════
# PHASE 10 — End-to-end test + hardening
# ═══════════════════════════════════════════════════════════════

## Goal
Full integration test in demo mode. Fix any bugs found.

## Test sequence

1. Delete trades.jsonl if it exists
2. python main.py
3. Wait 60 seconds
4. Verify: trades.jsonl has entries (demo mode should generate some)
5. Open http://localhost:8080 — verify dashboard shows data
6. touch STOP — verify bot halts within 5 seconds
7. rm STOP — verify bot does NOT auto-restart (by design — user must restart)
8. Re-run, wait 10s, kill with Ctrl+C
9. Verify session summary is logged on exit

## Hardening checklist

- [ ] All API calls have try/except — one failure never kills the loop
- [ ] Queue has max size (maxsize=100) to prevent unbounded growth
- [ ] trades.jsonl write is atomic (write to tmp, rename)
- [ ] Bot handles missing ODDS_API_KEY gracefully (runs demo scan)
- [ ] Bot handles Polymarket API rate limit (429) with exponential backoff
- [ ] Dashboard handles empty trades.jsonl (shows zeros, not errors)
- [ ] All float values rounded to 4dp in logs and ledger
- [ ] .gitignore includes: .env, trades.jsonl, STOP, __pycache__

## Done when
All checklist items pass. Bot runs 5 minutes without any unhandled exceptions.

---

# ═══════════════════════════════════════════════════════════════
# PHASE 11 — Live score enrichment (STRETCH GOAL)
# ═══════════════════════════════════════════════════════════════

## Goal
Replace placeholder score_diff=0 and time_remaining_pct=0.5 in Scanner
with real live game data. This is what gives the bot its actual edge signal.

## Implementation

Add core/live_scores.py:

```python
class LiveScoreFeed:
    """
    Polls ESPN's unofficial scoreboard endpoint for live game data.
    No API key required. Rate limit: max 1 req/sport/30s.

    ESPN endpoint (unofficial, may break):
    https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard

    Returns dict keyed by normalised team names:
    {
        "lakers": {
            "home_score": 98,
            "away_score": 80,
            "time_remaining_pct": 0.08,
            "period": 4,
            "clock": "2:14"
        }
    }
    """

    async def get_game_state(self, home_team: str, away_team: str,
                             sport: str) -> Optional[dict]: ...
```

Wire into ScannerAgent._process_event():
- After computing edge, call live_scores.get_game_state()
- If game state found: update opportunity.score_diff and time_remaining_pct
- This feeds into AnalystAgent confidence_score() for much better sizing

## ESPN sport/league mapping
```python
ESPN_ENDPOINTS = {
    "basketball_nba":      "basketball/nba",
    "americanfootball_nfl": "football/nfl",
    "basketball_ncaab":    "basketball/mens-college-basketball",
    "baseball_mlb":        "baseball/mlb",
}
```

## Done when
Scanner logs real score_diff and time_remaining_pct values for active games.
Confidence scores are non-default (not 0.5) when games are live.

---

# ═══════════════════════════════════════════════════════════════
# NOTES FOR CLAUDE CODE
# ═══════════════════════════════════════════════════════════════

## Working style
- Complete each phase fully before starting the next
- Run the "Done when" test at end of each phase — fix before moving on
- Never leave TODOs or placeholder functions — implement everything
- If a phase's test fails, debug and fix within that phase
- Add inline comments for any non-obvious logic (odds math, Kelly formula)

## Do not
- Add any dependencies not in requirements.txt without asking
- Change the .env variable names (dashboard relies on them)
- Use threading — asyncio only
- Write any blocking I/O in the agent loops

## Phase execution order
0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → (11 optional)

Each phase builds on the previous. Do not skip.
