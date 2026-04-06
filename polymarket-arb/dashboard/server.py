"""
dashboard/server.py -- FastAPI server for live monitoring

Endpoints:
  GET /           -- serves the dashboard HTML
  GET /api/status -- bot running status, session P&L, open positions, uptime
  GET /api/trades -- reads trades.jsonl, returns trade list with stats
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

app = FastAPI(title="ProArb V2 Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LEDGER_FILE = Path("trades.jsonl")
KILL_FILE = Path("STOP")
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
DASHBOARD_DIR = Path(__file__).parent


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the dashboard HTML page."""
    html_path = DASHBOARD_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
    return HTMLResponse(html_path.read_text())


@app.get("/api/status")
async def get_status():
    """Return bot status from shared state (if available) or from ledger."""
    state = getattr(app.state, "bot_state", None)

    if state:
        return {
            "running": True,
            "demo_mode": DEMO_MODE,
            "session_pnl": round(state.session_pnl, 2),
            "bankroll": round(state.bankroll, 2),
            "trades_placed": state.trades_placed,
            "trades_skipped": state.trades_skipped,
            "daily_trades": state.daily_trades,
            "open_positions": state.open_positions,
            "uptime_seconds": round(state.uptime_seconds, 1),
            "kill_switch_active": KILL_FILE.exists(),
            "daily_loss_pct": round(state.daily_loss_pct * 100, 2),
            "win_rate": round(state.win_rate * 100, 1) if state.win_rate is not None else None,
            "average_clv": round(state.average_clv * 100, 2) if state.average_clv is not None else None,
            "total_volume": round(state.total_volume, 2),
        }

    # Fallback: derive from ledger file
    trades = _read_ledger()
    resolved = [t for t in trades if t.get("outcome") is not None]
    wins = sum(1 for t in resolved if t.get("outcome") == "won")
    total_pnl = sum(t.get("pnl", 0) or 0 for t in resolved)

    return {
        "running": False,
        "demo_mode": DEMO_MODE,
        "session_pnl": round(total_pnl, 2),
        "bankroll": None,
        "trades_placed": len(trades),
        "trades_skipped": 0,
        "daily_trades": 0,
        "open_positions": sum(1 for t in trades if t.get("status") in ("open", "demo")),
        "uptime_seconds": 0,
        "kill_switch_active": KILL_FILE.exists(),
        "daily_loss_pct": 0,
        "win_rate": round(wins / len(resolved) * 100, 1) if resolved else None,
        "average_clv": None,
        "total_volume": sum(t.get("size_usdc", 0) or 0 for t in resolved),
    }


@app.get("/api/trades")
async def get_trades(
    limit: int = Query(50, ge=1, le=500),
    status: Optional[str] = Query(None, pattern="^(all|open|demo|resolved|cancelled)$"),
):
    """Return trades from the ledger file."""
    trades = _read_ledger()

    if status and status != "all":
        trades = [t for t in trades if t.get("status") == status]

    # Most recent first
    trades.sort(key=lambda t: t.get("entry_time", 0), reverse=True)
    trades = trades[:limit]

    # Compute summary stats
    resolved = [t for t in _read_ledger() if t.get("outcome") is not None]
    wins = sum(1 for t in resolved if t.get("outcome") == "won")
    total_pnl = sum(t.get("pnl", 0) or 0 for t in resolved)
    total_volume = sum(t.get("size_usdc", 0) or 0 for t in resolved)

    return {
        "trades": trades,
        "total": len(trades),
        "session_pnl": round(total_pnl, 2),
        "win_rate": round(wins / len(resolved) * 100, 1) if resolved else None,
        "total_volume": round(total_volume, 2),
    }


@app.get("/api/games")
async def get_games(
    limit: int = Query(100, ge=1, le=500),
    sport: Optional[str] = Query(None),
):
    """Return all scanned games from the scanner's evaluation log."""
    state = getattr(app.state, "bot_state", None)
    if not state:
        return {"games": [], "total": 0, "last_scan": None, "sports": {}}

    results = state.scan_results

    # Filter by sport if provided
    if sport:
        results = [r for r in results if r.sport == sport]

    # Most recent first
    results.sort(key=lambda r: r.timestamp, reverse=True)
    results = results[:limit]

    # Sport breakdown
    all_results = state.scan_results
    sport_counts: dict[str, dict] = {}
    for r in all_results:
        if r.sport not in sport_counts:
            sport_counts[r.sport] = {"total": 0, "matched": 0, "with_edge": 0}
        sport_counts[r.sport]["total"] += 1
        if r.poly_matched:
            sport_counts[r.sport]["matched"] += 1
        if r.edge is not None and r.edge >= 0.08:
            sport_counts[r.sport]["with_edge"] += 1

    games = []
    for r in results:
        games.append({
            "timestamp": r.timestamp,
            "event_name": r.event_name,
            "sport": r.sport,
            "home_team": r.home_team,
            "away_team": r.away_team,
            "pinnacle_home_odds": round(r.pinnacle_home_odds, 3),
            "pinnacle_away_odds": round(r.pinnacle_away_odds, 3),
            "pinnacle_home_prob": round(r.pinnacle_home_prob, 4),
            "pinnacle_away_prob": round(r.pinnacle_away_prob, 4),
            "polymarket_price": round(r.polymarket_price, 4) if r.polymarket_price is not None else None,
            "poly_matched": r.poly_matched,
            "our_side": r.our_side,
            "edge": round(r.edge, 4) if r.edge is not None else None,
            "edge_pct": round(r.edge * 100, 1) if r.edge is not None else None,
            "best_bid": round(r.best_bid, 4),
            "best_ask": round(r.best_ask, 4),
            "spread": round(r.spread, 4),
            "liquidity": round(r.liquidity, 0),
            "action": r.action,
        })

    return {
        "games": games,
        "total": len(games),
        "last_scan": state.last_scan_time,
        "sports": sport_counts,
    }


def _read_ledger() -> list[dict]:
    """Read trades.jsonl and return list of trade dicts.

    Deduplicates by trade_id, keeping the latest entry (resolved > open).
    """
    if not LEDGER_FILE.exists():
        return []

    trades_by_id: dict[str, dict] = {}
    try:
        with LEDGER_FILE.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trade = json.loads(line)
                    tid = trade.get("trade_id", trade.get("order_id", ""))
                    if tid:
                        trades_by_id[tid] = trade  # last write wins
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []

    return list(trades_by_id.values())
