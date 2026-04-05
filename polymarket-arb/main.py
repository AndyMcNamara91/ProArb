"""
Polymarket Sports Arbitrage Bot
================================
Three-agent pipeline:
  Agent 1 — Scanner:  polls live sports odds, detects gaps vs Polymarket
  Agent 2 — Analyst:  validates edge, calculates Kelly stake
  Agent 3 — Executor: places orders via Polymarket CLOB API

Requirements:
  pip install py-clob-client requests websockets python-dotenv colorlog

Config: copy .env.example → .env and fill in your keys.
Kill switch: touch STOP in this directory to halt all new trades instantly.
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import colorlog
from dotenv import load_dotenv

from agents.scanner import ScannerAgent
from agents.analyst import AnalystAgent
from agents.executor import ExecutorAgent
from core.state import BotState
from core.risk import RiskManager

load_dotenv()

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


async def main():
    log.info("=" * 60)
    log.info("Polymarket Sports Arb Bot — starting up")
    log.info("Kill switch: touch ./STOP to halt")
    log.info("=" * 60)

    state = BotState()
    risk  = RiskManager(
        daily_loss_limit=float(os.getenv("DAILY_LOSS_LIMIT_USD", 50)),
        max_position_pct=float(os.getenv("MAX_POSITION_PCT", 0.05)),
    )

    scanner  = ScannerAgent(state)
    analyst  = AnalystAgent(state, risk)
    executor = ExecutorAgent(state, risk)

    # Shared queue between agents
    opportunity_queue: asyncio.Queue = asyncio.Queue()
    order_queue:       asyncio.Queue = asyncio.Queue()

    async def run_scanner():
        while not KILL_FILE.exists():
            await scanner.scan(opportunity_queue)
            await asyncio.sleep(float(os.getenv("SCAN_INTERVAL_SEC", 2)))
        log.warning("STOP file detected — scanner halting")

    async def run_analyst():
        while not KILL_FILE.exists():
            try:
                opp = await asyncio.wait_for(opportunity_queue.get(), timeout=5)
                decision = await analyst.evaluate(opp)
                if decision:
                    await order_queue.put(decision)
            except asyncio.TimeoutError:
                pass
        log.warning("STOP file detected — analyst halting")

    async def run_executor():
        while not KILL_FILE.exists():
            try:
                order = await asyncio.wait_for(order_queue.get(), timeout=5)
                await executor.execute(order)
            except asyncio.TimeoutError:
                pass
        log.warning("STOP file detected — executor halting")

    async def run_risk_monitor():
        while not KILL_FILE.exists():
            if risk.daily_loss_breached():
                log.critical("Daily loss limit breached — creating STOP file")
                KILL_FILE.touch()
                break
            await asyncio.sleep(10)

    try:
        await asyncio.gather(
            run_scanner(),
            run_analyst(),
            run_executor(),
            run_risk_monitor(),
        )
    except KeyboardInterrupt:
        log.info("Keyboard interrupt — shutting down")
    finally:
        log.info(f"Session P&L: ${state.session_pnl:+.2f}")
        log.info(f"Trades placed: {state.trades_placed}")
        log.info(f"Trades skipped: {state.trades_skipped}")


if __name__ == "__main__":
    asyncio.run(main())
