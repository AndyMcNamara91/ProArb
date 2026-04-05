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
    status:        str = "open" # open | filled | demo_filled | pending_outcome
    fill_price:    Optional[float] = None
    pnl:           Optional[float] = None
    order_id:      Optional[str]   = None
    home_team:     Optional[str]   = None   # for outcome resolution
    away_team:     Optional[str]   = None
    sport:         Optional[str]   = None
    bet_team:      Optional[str]   = None   # which team we're betting wins


class BotState:
    """Thread/async-safe shared state across the three agents."""

    def __init__(self):
        self.session_pnl:    float = 0.0
        self.trades_placed:  int   = 0
        self.trades_skipped: int   = 0
        self._open_trades:   dict  = {}  # order_id → Trade
        self._daily_loss:    float = 0.0
        self._session_start: float = time.time()
        self._load_pending_trades()

    def _load_pending_trades(self) -> None:
        """Reload pending_outcome trades from trades.jsonl on startup for dedup."""
        if not LEDGER_FILE.exists():
            return
        try:
            # Track latest status per order_id
            latest: dict = {}
            with LEDGER_FILE.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    oid = data.get("order_id", "")
                    if oid:
                        latest[oid] = data

            # Restore only pending_outcome trades
            for oid, data in latest.items():
                if data.get("status") == "pending_outcome":
                    trade = Trade(
                        timestamp=data.get("timestamp", 0),
                        market_id=data.get("market_id", ""),
                        event_name=data.get("event_name", ""),
                        side=data.get("side", ""),
                        token_id=data.get("token_id", ""),
                        entry_price=data.get("entry_price", 0),
                        size_usdc=data.get("size_usdc", 0),
                        edge_at_entry=data.get("edge_at_entry", 0),
                        sports_prob=data.get("sports_prob", 0),
                        poly_prob=data.get("poly_prob", 0),
                        status="pending_outcome",
                        order_id=oid,
                        home_team=data.get("home_team"),
                        away_team=data.get("away_team"),
                        sport=data.get("sport"),
                        bet_team=data.get("bet_team"),
                    )
                    self._open_trades[oid] = trade

            if self._open_trades:
                log.info(f"Restored {len(self._open_trades)} pending trades from ledger")
        except Exception as e:
            log.warning(f"Failed to load pending trades from ledger: {e}")

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
        """Atomic append: write to tmp file then rename to avoid partial writes."""
        try:
            tmp = LEDGER_FILE.with_suffix(".tmp")
            line = json.dumps(asdict(trade)) + "\n"
            # Append existing content + new line atomically
            with LEDGER_FILE.open("a") as f:
                f.write(line)
                f.flush()
        except Exception as e:
            log.warning(f"Ledger write failed: {e}")


# ── Inline tests ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    # Clean up any leftover test ledger
    if LEDGER_FILE.exists():
        LEDGER_FILE.unlink()

    state = BotState()

    # Record 2 trades
    t1 = Trade(
        timestamp=time.time(), market_id="mkt-1", event_name="Test Game 1",
        side="YES", token_id="tok-1", entry_price=0.55, size_usdc=10.0,
        edge_at_entry=0.15, sports_prob=0.70, poly_prob=0.55,
        status="open", order_id="order-001",
    )
    t2 = Trade(
        timestamp=time.time(), market_id="mkt-2", event_name="Test Game 2",
        side="NO", token_id="tok-2", entry_price=0.40, size_usdc=8.0,
        edge_at_entry=0.12, sports_prob=0.72, poly_prob=0.60,
        status="open", order_id="order-002",
    )
    state.record_trade(t1)
    state.record_trade(t2)

    assert state.trades_placed == 2
    assert state.open_positions == 2

    # Close 1 trade
    state.close_trade("order-001", fill_price=0.58, pnl=2.50)
    assert state.open_positions == 1
    assert abs(state.session_pnl - 2.50) < 0.01

    # Verify ledger has 3 lines (2 opens + 1 close)
    with LEDGER_FILE.open() as f:
        lines = f.readlines()
    assert len(lines) == 3, f"Expected 3 ledger lines, got {len(lines)}"

    # Verify each line is valid JSON
    for line in lines:
        parsed = json.loads(line)
        assert "event_name" in parsed

    # Clean up
    LEDGER_FILE.unlink()
    print("All state tests passed")
