"""
Polymarket Sports Arbitrage Bot
================================
Three-agent pipeline:
  Agent 1 — Scanner:  polls live sports odds, detects gaps vs Polymarket
  Agent 2 — Analyst:  validates edge, calculates Kelly stake
  Agent 3 — Executor: places orders via Polymarket CLOB API

A FastAPI dashboard serves live trade data at http://localhost:8080.

Requirements:
  pip install -r requirements.txt

Config: copy .env.example -> .env and fill in your keys.
Kill switch: touch STOP in this directory to halt all new trades instantly.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

import colorlog
from dotenv import load_dotenv

# Load .env BEFORE importing agents — they read env vars at module level
load_dotenv()

from agents.scanner import ScannerAgent
from agents.analyst import AnalystAgent
from agents.executor import ExecutorAgent
from core.state import BotState
from core.risk import RiskManager

# ── Logging ──────────────────────────────────────────────────────────────────
handler = colorlog.StreamHandler()
handler.setFormatter(colorlog.ColoredFormatter(
    "%(log_color)s%(asctime)s [%(name)-10s] %(message)s",
    datefmt="%H:%M:%S",
    log_colors={
        "DEBUG": "cyan", "INFO": "green",
        "WARNING": "yellow", "ERROR": "red", "CRITICAL": "bold_red",
    }
))
logging.basicConfig(level=logging.INFO, handlers=[handler])
log = logging.getLogger("main")

KILL_FILE = Path("STOP")
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", 2))


async def main():
    start_time = time.time()

    log.info("=" * 60)
    log.info("Polymarket Sports Arb Bot — starting up")
    log.info(f"DEMO_MODE={os.getenv('DEMO_MODE', 'true')}")
    log.info("Kill switch: touch ./STOP to halt")
    log.info("Dashboard: http://localhost:%s", os.getenv("DASHBOARD_PORT", "8080"))
    log.info("=" * 60)

    # Remove stale STOP file from previous run
    if KILL_FILE.exists():
        log.warning("Removing stale STOP file from previous run")
        KILL_FILE.unlink()

    state = BotState()
    risk = RiskManager(
        daily_loss_limit=float(os.getenv("DAILY_LOSS_LIMIT_USD", 50)),
        max_position_pct=float(os.getenv("MAX_POSITION_PCT", 0.05)),
    )

    scanner = ScannerAgent(state)
    analyst = AnalystAgent(state, risk)
    executor = ExecutorAgent(state, risk)

    # Bounded queues prevent unbounded memory growth
    opportunity_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    order_queue: asyncio.Queue = asyncio.Queue(maxsize=100)

    # Shared shutdown event for clean coordinated exit
    shutdown_event = asyncio.Event()

    async def run_scanner():
        """Poll sports odds and push opportunities to the queue."""
        while not KILL_FILE.exists() and not shutdown_event.is_set():
            try:
                await scanner.scan(opportunity_queue)
            except Exception as e:
                log.error(f"Scanner error: {e}")
            await asyncio.sleep(SCAN_INTERVAL_SEC)
        log.warning("Scanner halting")

    async def run_analyst():
        """Consume opportunities, validate, size, and push trade decisions."""
        while not KILL_FILE.exists() and not shutdown_event.is_set():
            try:
                opp = await asyncio.wait_for(opportunity_queue.get(), timeout=5)
                decision = await analyst.evaluate(opp)
                if decision:
                    await order_queue.put(decision)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                log.error(f"Analyst error: {e}")
        log.warning("Analyst halting")

    async def run_executor():
        """Consume trade decisions and execute (or demo-log) orders."""
        while not KILL_FILE.exists() and not shutdown_event.is_set():
            try:
                order = await asyncio.wait_for(order_queue.get(), timeout=5)
                await executor.execute(order)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                log.error(f"Executor error: {e}")
        log.warning("Executor halting")

    async def run_risk_monitor():
        """Watch for daily loss limit breach and trigger kill switch."""
        while not KILL_FILE.exists() and not shutdown_event.is_set():
            if risk.daily_loss_breached():
                log.critical("Daily loss limit breached — creating STOP file")
                KILL_FILE.touch()
                break
            await asyncio.sleep(10)

    async def run_dashboard(uv_server_holder):
        """Start the FastAPI dashboard server."""
        import uvicorn
        from dashboard.server import create_app

        app = create_app(state, risk, start_time)
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=int(os.getenv("DASHBOARD_PORT", 8080)),
            log_level="warning",
        )
        server = uvicorn.Server(config)
        uv_server_holder.append(server)
        await server.serve()

    async def run_stop_watcher(uv_server_holder):
        """Poll for STOP file and trigger clean shutdown of all coroutines."""
        while not shutdown_event.is_set():
            if KILL_FILE.exists():
                log.warning("STOP file detected — initiating shutdown")
                shutdown_event.set()
                # Signal uvicorn to stop so asyncio.gather can finish
                for srv in uv_server_holder:
                    srv.should_exit = True
                break
            await asyncio.sleep(1)

    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()
    uv_server_holder = []

    def signal_handler():
        log.info("Shutdown signal received")
        shutdown_event.set()
        for srv in uv_server_holder:
            srv.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await asyncio.gather(
            run_scanner(),
            run_analyst(),
            run_executor(),
            run_risk_monitor(),
            run_dashboard(uv_server_holder),
            run_stop_watcher(uv_server_holder),
        )
    finally:
        elapsed = time.time() - start_time
        log.info("=" * 60)
        log.info("SESSION SUMMARY")
        log.info(f"  Uptime:         {elapsed:.0f}s")
        log.info(f"  Session P&L:    ${state.session_pnl:+.2f}")
        log.info(f"  Trades placed:  {state.trades_placed}")
        log.info(f"  Trades skipped: {state.trades_skipped}")
        log.info(f"  Open positions: {state.open_positions}")
        log.info(f"  Daily loss:     ${state.daily_loss:.2f}")
        log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
