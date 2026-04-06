"""
dashboard/server.py — FastAPI server for the arb bot dashboard.

Serves the dashboard UI at GET / and exposes JSON API endpoints
for trades and bot status.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

log = logging.getLogger("dashboard")

LEDGER_FILE = Path("trades.jsonl")

app = FastAPI(title="Arb Bot Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the dashboard HTML page."""
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/api/trades")
async def get_trades(
    limit: int = Query(50, ge=1, le=500),
    status: str = Query("all"),
):
    """
    Read trades.jsonl and return trade history.

    Query params:
        limit  — max trades to return (default 50)
        status — filter: all | open | filled | failed
    """
    trades = _read_ledger()

    if status != "all":
        trades = [t for t in trades if t.get("status") == status]

    session_pnl = sum(t.get("pnl", 0) or 0 for t in trades if t.get("pnl") is not None)

    # Newest first
    trades.sort(key=lambda t: t.get("timestamp", 0), reverse=True)
    trades = trades[:limit]

    return {"trades": trades, "total": len(trades), "session_pnl": session_pnl}


@app.get("/api/status")
async def get_status():
    """Return bot status, P&L, and risk metrics."""
    state = getattr(app.state, "bot_state", None)
    risk = getattr(app.state, "risk_manager", None)

    if state is None:
        # Fallback: derive status from ledger file
        trades = _read_ledger()
        session_pnl = sum(t.get("pnl", 0) or 0 for t in trades if t.get("pnl") is not None)
        open_count = sum(1 for t in trades if t.get("status") == "open")
        return {
            "running": False,
            "demo_mode": os.getenv("DEMO_MODE", "true").lower() == "true",
            "session_pnl": session_pnl,
            "trades_placed": len(trades),
            "trades_skipped": 0,
            "open_positions": open_count,
            "uptime_seconds": 0,
            "kill_switch_active": Path("STOP").exists(),
            "daily_loss": 0.0,
            "daily_loss_limit": float(os.getenv("DAILY_LOSS_LIMIT_USD", 50)),
        }

    return {
        "running": True,
        "demo_mode": os.getenv("DEMO_MODE", "true").lower() == "true",
        "session_pnl": state.session_pnl,
        "trades_placed": state.trades_placed,
        "trades_skipped": state.trades_skipped,
        "open_positions": state.open_positions,
        "uptime_seconds": time.time() - state._session_start,
        "kill_switch_active": Path("STOP").exists(),
        "daily_loss": state.daily_loss,
        "daily_loss_limit": risk.daily_loss_limit if risk else float(os.getenv("DAILY_LOSS_LIMIT_USD", 50)),
    }


def _read_ledger() -> list[dict]:
    """Read trades.jsonl and return list of trade dicts."""
    if not LEDGER_FILE.exists():
        return []
    trades = []
    try:
        with LEDGER_FILE.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        log.warning(f"Failed to read ledger: {e}")
    return trades
