"""
core/state.py — Shared bot state, trade ledger, session P&L tracking
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger("state")

LEDGER_FILE = Path("trades.jsonl")


@dataclass
class Trade:
    timestamp:     float
    market_id:     str
    event_name:    str
    side:          str          # YES | NO
    token_id:      str
    entry_price:   float        # 0–1
    size_usdc:     float
    edge_at_entry: float        # e.g. 0.14 = 14%
    sports_prob:   float        # our estimated true probability
    poly_prob:     float        # polymarket price at entry
    status:        str = "open" # open | filled | failed
    fill_price:    Optional[float] = None
    pnl:           Optional[float] = None
    order_id:      Optional[str]   = None


class BotState:
    """Thread/async-safe shared state across the three agents."""

    def __init__(self):
        self.session_pnl:    float = 0.0
        self.trades_placed:  int   = 0
        self.trades_skipped: int   = 0
        self._open_trades:   dict  = {}  # order_id → Trade
        self._daily_loss:    float = 0.0
        self._session_start: float = time.time()

    # ── Trade lifecycle ──────────────────────────────────────────────────────

    def record_trade(self, trade: Trade) -> None:
        if trade.order_id:
            self._open_trades[trade.order_id] = trade
        self.trades_placed += 1
        self._append_ledger(trade)
        log.info(
            f"TRADE  {trade.event_name} | {trade.side} "
            f"@ {trade.entry_price:.3f} | size ${trade.size_usdc:.2f} "
            f"| edge {trade.edge_at_entry*100:.1f}%"
        )

    def close_trade(self, order_id: str, fill_price: float, pnl: float) -> None:
        trade = self._open_trades.pop(order_id, None)
        if trade:
            trade.status     = "filled"
            trade.fill_price = fill_price
            trade.pnl        = pnl
            self.session_pnl    += pnl
            self._daily_loss    += min(pnl, 0)  # only track losses
            self._append_ledger(trade)
            log.info(
                f"CLOSED {trade.event_name} | P&L ${pnl:+.2f} "
                f"| session total ${self.session_pnl:+.2f}"
            )

    def skip_trade(self, reason: str) -> None:
        self.trades_skipped += 1
        log.debug(f"SKIP   {reason}")

    # ── Accessors ────────────────────────────────────────────────────────────

    @property
    def daily_loss(self) -> float:
        return abs(self._daily_loss)

    @property
    def open_positions(self) -> int:
        return len(self._open_trades)

    # ── Persistence ──────────────────────────────────────────────────────────

    def _append_ledger(self, trade: Trade) -> None:
        try:
            with LEDGER_FILE.open("a") as f:
                f.write(json.dumps(asdict(trade)) + "\n")
        except Exception as e:
            log.warning(f"Ledger write failed: {e}")
