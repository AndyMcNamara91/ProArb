"""
dashboard/server.py — FastAPI server for the live trading dashboard.

Endpoints:
  GET /           → serves dashboard/index.html
  GET /api/trades → reads trades.jsonl, returns JSON
  GET /api/status → returns bot running status, P&L, positions
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
KILL_FILE = Path("STOP")
DASHBOARD_DIR = Path(__file__).parent


def create_app(state, risk, start_time: float) -> FastAPI:
    """Factory that creates the FastAPI app with access to shared bot state."""

    app = FastAPI(title="Arb Bot Dashboard", docs_url=None, redoc_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        """Serve the dashboard HTML page."""
        html_path = DASHBOARD_DIR / "index.html"
        try:
            return html_path.read_text()
        except FileNotFoundError:
            return HTMLResponse(
                "<h1>Dashboard not found</h1><p>index.html missing from dashboard/</p>",
                status_code=404,
            )

    @app.get("/api/trades")
    async def get_trades(
        limit: int = Query(default=50, ge=1, le=500),
        status: str = Query(default="all"),
    ):
        """Read trades from the JSONL ledger and return as JSON."""
        trades = []
        try:
            if LEDGER_FILE.exists():
                with LEDGER_FILE.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            trade = json.loads(line)
                            if status != "all" and trade.get("status") != status:
                                continue
                            trades.append(trade)
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            log.error(f"Error reading ledger: {e}")

        # Newest first
        trades.sort(key=lambda t: t.get("timestamp", 0), reverse=True)
        trades = trades[:limit]

        # Calculate session P&L from ledger
        session_pnl = state.session_pnl if state else 0.0

        return {
            "trades": trades,
            "total": len(trades),
            "session_pnl": round(session_pnl, 4),
        }

    @app.get("/api/status")
    async def get_status():
        """Return current bot status."""
        demo_mode = os.getenv("DEMO_MODE", "true").lower() == "true"
        kill_active = KILL_FILE.exists()

        return {
            "running": not kill_active,
            "demo_mode": demo_mode,
            "session_pnl": round(state.session_pnl, 4) if state else 0.0,
            "trades_placed": state.trades_placed if state else 0,
            "trades_skipped": state.trades_skipped if state else 0,
            "open_positions": state.open_positions if state else 0,
            "uptime_seconds": round(time.time() - start_time, 1),
            "kill_switch_active": kill_active,
            "daily_loss": round(state.daily_loss, 4) if state else 0.0,
            "daily_loss_limit": risk.daily_loss_limit if risk else 50.0,
        }

    return app
