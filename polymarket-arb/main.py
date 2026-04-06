"""
ProArb V2 -- Value Betting Engine
==================================
4-module pipeline:
  Module 1 -- Scanner:    Pinnacle de-vigged odds vs Polymarket prices
  Module 2 -- Gatekeeper: 4 gates (quality, edge, bounds, Kelly sizing)
  Module 3 -- Executor:   postOnly limit orders (maker, 0% fee)
  Module 4 -- Resolver:   ESPN outcome resolution + CLV tracking

Plus a continuous Risk Monitor that enforces daily loss limits,
correlation caps, and the STOP file kill switch.

Config: copy .env.example -> .env and fill in your keys.
Kill switch: touch STOP in this directory to halt all new trades.
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import colorlog
from dotenv import load_dotenv

from core.models import RiskLimits
from core.risk import RiskManager
from core.state import BotState
from modules.scanner import ScannerModule, ODDS_API_POLL_INTERVAL
from modules.gatekeeper import GatekeeperModule
from modules.executor import ExecutorModule
from modules.resolver import ResolverModule, ESPN_POLL_INTERVAL

load_dotenv()

# -- Logging ----------------------------------------------------------------

handler = colorlog.StreamHandler()
handler.setFormatter(colorlog.ColoredFormatter(
    "%(log_color)s%(asctime)s [%(name)-12s] %(message)s",
    datefmt="%H:%M:%S",
    log_colors={
        "DEBUG": "cyan", "INFO": "green",
        "WARNING": "yellow", "ERROR": "red", "CRITICAL": "bold_red",
    }
))
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    handlers=[handler],
)
log = logging.getLogger("main")

KILL_FILE = Path("STOP")
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"

# Scanner poll interval (how often to check for new opportunities)
SCAN_INTERVAL_SEC = int(os.getenv("ODDS_API_POLL_INTERVAL", 600))


async def main():
    log.info("=" * 60)
    log.info("ProArb V2 -- Value Betting Engine")
    log.info(f"Mode: {'DEMO' if DEMO_MODE else 'LIVE'}")
    log.info("Kill switch: touch ./STOP to halt")
    log.info("=" * 60)

    # -- Initialise state and risk ---------------------------------------
    bankroll = float(os.getenv("BANKROLL_USDC", 500))

    limits = RiskLimits(
        daily_loss_limit_pct=float(os.getenv("DAILY_LOSS_LIMIT_PCT", 0.20)),
        max_position_pct=float(os.getenv("MAX_POSITION_PCT", 0.05)),
        max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", 10)),
        max_daily_trades=int(os.getenv("MAX_DAILY_TRADES", 15)),
        max_correlated_exposure_pct=float(os.getenv("MAX_SPORT_EXPOSURE_PCT", 0.15)),
        min_edge=float(os.getenv("MIN_EDGE", 0.10)),
        min_bankroll=float(os.getenv("MIN_BANKROLL", 20.0)),
        kelly_fraction=float(os.getenv("KELLY_FRACTION", 0.25)),
        min_stake=float(os.getenv("MIN_STAKE", 5.0)),
        min_liquidity=float(os.getenv("MIN_LIQUIDITY", 5000)),
        max_spread=float(os.getenv("MAX_SPREAD", 0.04)),
    )

    state = BotState(bankroll=bankroll)
    risk = RiskManager(limits=limits)

    # -- Initialise modules ----------------------------------------------
    scanner = ScannerModule(state)
    gatekeeper = GatekeeperModule(state, risk)
    executor = ExecutorModule(state)
    resolver = ResolverModule(state)

    # Shared queues
    opportunity_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    decision_queue: asyncio.Queue = asyncio.Queue(maxsize=50)

    log.info(f"Bankroll: ${bankroll:.2f}")
    log.info(f"Min edge: {limits.min_edge * 100:.0f}%")
    log.info(f"Kelly fraction: {limits.kelly_fraction * 100:.0f}%")
    log.info(f"Max positions: {limits.max_open_positions}")
    log.info(f"Max daily trades: {limits.max_daily_trades}")

    # -- Start dashboard server ------------------------------------------
    dashboard_port = int(os.getenv("DASHBOARD_PORT", 8080))

    async def run_dashboard():
        try:
            import uvicorn
            from dashboard.server import app as dashboard_app
            dashboard_app.state.bot_state = state
            config = uvicorn.Config(
                dashboard_app,
                host="0.0.0.0",
                port=dashboard_port,
                log_level="warning",
            )
            server = uvicorn.Server(config)
            log.info(f"Dashboard: http://localhost:{dashboard_port}")
            await server.serve()
        except ImportError:
            log.warning("uvicorn/fastapi not installed -- dashboard disabled")
        except Exception as e:
            log.warning(f"Dashboard failed to start: {e}")

    # -- Scanner loop ----------------------------------------------------
    async def run_scanner():
        # In demo mode, scan more frequently for testing
        interval = 10 if DEMO_MODE else SCAN_INTERVAL_SEC
        while not KILL_FILE.exists():
            try:
                await scanner.scan(opportunity_queue)
            except Exception as e:
                log.error(f"Scanner error: {e}")
            await asyncio.sleep(interval)
        log.warning("STOP file detected -- scanner halting")

    # -- Gatekeeper loop -------------------------------------------------
    async def run_gatekeeper():
        while not KILL_FILE.exists():
            try:
                opp = await asyncio.wait_for(opportunity_queue.get(), timeout=5)
                decision = await gatekeeper.evaluate(opp)
                if decision:
                    await decision_queue.put(decision)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                log.error(f"Gatekeeper error: {e}")
        log.warning("STOP file detected -- gatekeeper halting")

    # -- Executor loop ---------------------------------------------------
    async def run_executor():
        while not KILL_FILE.exists():
            try:
                decision = await asyncio.wait_for(decision_queue.get(), timeout=5)
                await executor.execute(decision)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                log.error(f"Executor error: {e}")
        log.warning("STOP file detected -- executor halting")
        await executor.cleanup()

    # -- Resolver loop ---------------------------------------------------
    async def run_resolver():
        while not KILL_FILE.exists():
            try:
                if DEMO_MODE:
                    await resolver.demo_resolve()
                else:
                    await resolver.check_outcomes()
            except Exception as e:
                log.error(f"Resolver error: {e}")
            await asyncio.sleep(ESPN_POLL_INTERVAL if not DEMO_MODE else 15)
        log.warning("STOP file detected -- resolver halting")

    # -- Risk Monitor (continuous) ---------------------------------------
    async def run_risk_monitor():
        while not KILL_FILE.exists():
            # Check daily loss limit
            if risk.daily_loss_breached(state.daily_loss_pct):
                log.critical(
                    f"Daily loss limit breached ({state.daily_loss_pct * 100:.1f}%) "
                    f"-- creating STOP file"
                )
                KILL_FILE.touch()
                break

            # Check minimum bankroll
            if state.bankroll < limits.min_bankroll:
                log.critical(
                    f"Bankroll ${state.bankroll:.2f} below minimum "
                    f"${limits.min_bankroll:.2f} -- creating STOP file"
                )
                KILL_FILE.touch()
                break

            # Log status every 60 seconds
            if int(time.time()) % 60 == 0:
                log.info(
                    f"STATUS | bankroll=${state.bankroll:.2f} "
                    f"| P&L=${state.session_pnl:+.2f} "
                    f"| open={state.open_positions} "
                    f"| trades={state.trades_placed} "
                    f"| daily_loss={state.daily_loss_pct * 100:.1f}%"
                )

            await asyncio.sleep(10)
        log.warning("Risk monitor halting")

    # -- Run all modules -------------------------------------------------
    try:
        await asyncio.gather(
            run_scanner(),
            run_gatekeeper(),
            run_executor(),
            run_resolver(),
            run_risk_monitor(),
            run_dashboard(),
        )
    except KeyboardInterrupt:
        log.info("Keyboard interrupt -- shutting down")
    finally:
        await scanner.close()
        await resolver.close()
        await executor.cleanup()

        log.info("=" * 60)
        log.info("ProArb V2 -- Session Summary")
        log.info(f"  Uptime:         {state.uptime_seconds / 60:.1f} minutes")
        log.info(f"  Session P&L:    ${state.session_pnl:+.2f}")
        log.info(f"  Final bankroll: ${state.bankroll:.2f}")
        log.info(f"  Trades placed:  {state.trades_placed}")
        log.info(f"  Trades skipped: {state.trades_skipped}")
        log.info(f"  Win rate:       {state.win_rate * 100:.1f}%" if state.win_rate is not None else "  Win rate:       N/A")
        if state.average_clv is not None:
            log.info(f"  Average CLV:    {state.average_clv * 100:.2f}%")
        log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
