# CLAUDE.md -- ProArb V2: Value Betting Engine

## PROJECT OVERVIEW

ProArb V2 is a value betting engine that exploits the gap between
Pinnacle's de-vigged closing lines (the sharpest probability source)
and Polymarket's retail-driven sports market prices.

This is NOT arbitrage (risk-free). It's directional value betting with
an institutional-grade edge signal.

## ARCHITECTURE

4-module pipeline (not 5 agents):

```
Scanner -> Gatekeeper -> Executor
   |                        |
   +-----> Resolver <-------+
   
   Risk Monitor (continuous)
```

- **Scanner**: Fetches Pinnacle odds via The Odds API (EU region),
  de-vigs using Power method, matches to Polymarket markets, calculates edge.
- **Gatekeeper**: 4 gates (market quality, edge threshold, probability bounds,
  Kelly sizing). No confidence scoring -- Pinnacle IS the confidence.
- **Executor**: postOnly limit orders (maker, 0% fee + rebate). Never taker.
  10-minute order expiry.
- **Resolver**: ESPN for outcome resolution only. Captures CLV at game time.
- **Risk Monitor**: Daily loss limit, min bankroll, correlation control.

## FILE STRUCTURE

```
polymarket-arb/
├── CLAUDE.md              <- this file
├── main.py                <- entry point, orchestrates all modules
├── requirements.txt
├── .env.example
├── .env                   <- not committed
├── STOP                   <- kill switch (touch to halt)
├── trades.jsonl           <- trade ledger (auto-created)
│
├── core/
│   ├── __init__.py
│   ├── models.py          <- Pydantic data models
│   ├── devig.py           <- Power method de-vigging
│   ├── state.py           <- BotState: shared state, ledger, P&L, CLV
│   └── risk.py            <- RiskManager: Kelly, correlation, gates
│
├── modules/
│   ├── __init__.py
│   ├── scanner.py         <- Pinnacle odds + Polymarket matching
│   ├── gatekeeper.py      <- 4 validation gates
│   ├── executor.py        <- postOnly limit order placement
│   └── resolver.py        <- ESPN outcome resolution
│
└── dashboard.html         <- optional dashboard UI
```

## TECH STACK

```
Python 3.11+
├── asyncio                # Concurrency
├── py-clob-client         # Polymarket CLOB API
├── websockets             # Polymarket real-time prices
├── httpx                  # Async HTTP (Odds API, ESPN, Gamma API)
├── pydantic               # Data models & validation
├── colorlog               # Coloured logging
└── python-dotenv          # Environment config

External APIs:
├── The Odds API (EU region, bookmakers=pinnacle)
├── Polymarket Gamma API (market discovery)
├── Polymarket CLOB WebSocket (real-time prices)
├── Polymarket CLOB REST (order placement)
└── ESPN Scoreboard API (outcome resolution only)
```

## KEY DESIGN DECISIONS

| Decision           | V1                     | V2                                      |
|--------------------|------------------------|-----------------------------------------|
| Probability source | ESPN logistic model    | Pinnacle de-vigged odds (Power method)  |
| Order type         | Market (taker, 0.75%)  | Limit postOnly (maker, 0% fee + rebate) |
| Kelly fraction     | 15%                    | 25% (quarter Kelly)                     |
| Minimum edge       | 6%                     | 10%                                     |
| Minimum stake      | ~$3.24                 | $5.00                                   |
| Max open positions | 20                     | 10                                      |
| Price data         | REST polling 30s       | WebSocket streaming                     |
| ESPN role          | Probability + outcomes | Outcomes only                           |
| Bankroll           | $108                   | $500 minimum                            |

## CODING STANDARDS

- Pydantic models for all data structures
- Async everywhere (httpx, asyncio.Queue)
- Errors: catch and log, never crash the main loop
- Logging: colorlog with module names
- Config: os.getenv() with safe defaults
- Demo mode: DEMO_MODE=true runs full pipeline without executing
- Kill switch: Path("STOP").exists() checked before every order

## ENV VARIABLES

See .env.example for full list with descriptions.

## RUNNING

```bash
pip install -r requirements.txt
cp .env.example .env  # edit with your keys
python main.py        # starts in DEMO_MODE by default
```

## KILL SWITCH

```bash
touch STOP   # halt all new trades immediately
rm STOP      # must restart bot manually
```
