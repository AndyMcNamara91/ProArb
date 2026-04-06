"""
core/state.py -- Shared bot state, trade ledger, session P&L, sport exposure tracking

Kept from V1: trades.jsonl append-only persistence, dedup logic, session counters.
New in V2: sport exposure tracking, CLV aggregation, daily trade counter.
"""

import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from core.models import ScanResult, TradeRecord

log = logging.getLogger("state")

LEDGER_FILE = Path("trades.jsonl")


class BotState:
    """Shared mutable state across all modules."""

    def __init__(self, bankroll: float = 500.0):
        self.bankroll: float = bankroll
        self.starting_daily_bankroll: float = bankroll
        self.session_pnl: float = 0.0
        self.trades_placed: int = 0
        self.trades_skipped: int = 0
        self.daily_trades: int = 0
        self._daily_loss: float = 0.0
        self._session_start: float = time.time()

        # Open trades keyed by order_id or trade_id
        self._open_trades: dict[str, TradeRecord] = {}

        # Dedup: set of event names we've already traded
        self._traded_events: set[str] = set()

        # Sport exposure: sport -> total USDC deployed
        self._sport_exposure: defaultdict[str, float] = defaultdict(float)

        # All resolved trades for CLV tracking
        self._resolved_trades: list[TradeRecord] = []

        # Scan log: every game checked (capped at last 500)
        self._scan_results: list[ScanResult] = []
        self._max_scan_results: int = 500

    # -- Trade lifecycle -------------------------------------------------

    def record_trade(self, trade: TradeRecord) -> None:
        key = trade.order_id or trade.trade_id
        self._open_trades[key] = trade
        self._traded_events.add(trade.event_name)
        self._sport_exposure[trade.sport] += trade.size_usdc
        self.trades_placed += 1
        self.daily_trades += 1
        self._append_ledger(trade)
        log.info(
            f"TRADE  {trade.event_name} | {trade.side} "
            f"@ {trade.entry_price:.3f} | ${trade.size_usdc:.2f} "
            f"| edge {trade.edge_at_entry * 100:.1f}%"
        )

    def resolve_trade(self, key: str, outcome: str, pnl: float) -> None:
        trade = self._open_trades.pop(key, None)
        if not trade:
            log.warning(f"Cannot resolve unknown trade: {key}")
            return

        trade.outcome = outcome
        trade.pnl = pnl
        trade.status = "resolved"
        trade.resolved_at = time.time()

        self.session_pnl += pnl
        self.bankroll += pnl
        if pnl < 0:
            self._daily_loss += abs(pnl)

        # Release sport exposure
        self._sport_exposure[trade.sport] = max(
            0, self._sport_exposure[trade.sport] - trade.size_usdc
        )

        self._resolved_trades.append(trade)
        self._append_ledger(trade)
        log.info(
            f"RESOLVED {trade.event_name} | {outcome} | P&L ${pnl:+.2f} "
            f"| session ${self.session_pnl:+.2f}"
        )

    def cancel_trade(self, key: str) -> None:
        trade = self._open_trades.pop(key, None)
        if trade:
            trade.status = "cancelled"
            self._sport_exposure[trade.sport] = max(
                0, self._sport_exposure[trade.sport] - trade.size_usdc
            )
            self._append_ledger(trade)
            log.info(f"CANCELLED {trade.event_name}")

    def skip_trade(self, reason: str) -> None:
        self.trades_skipped += 1
        log.debug(f"SKIP   {reason}")

    # -- Dedup -----------------------------------------------------------

    def already_traded(self, event_name: str) -> bool:
        return event_name in self._traded_events

    # -- Accessors -------------------------------------------------------

    @property
    def daily_loss(self) -> float:
        return self._daily_loss

    @property
    def daily_loss_pct(self) -> float:
        if self.starting_daily_bankroll <= 0:
            return 0.0
        return self._daily_loss / self.starting_daily_bankroll

    @property
    def open_positions(self) -> int:
        return len(self._open_trades)

    @property
    def open_trades(self) -> list[TradeRecord]:
        return list(self._open_trades.values())

    def sport_exposure(self, sport: str) -> float:
        return self._sport_exposure.get(sport, 0.0)

    def sport_exposure_pct(self, sport: str) -> float:
        if self.bankroll <= 0:
            return 0.0
        return self._sport_exposure.get(sport, 0.0) / self.bankroll

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._session_start

    # -- CLV metrics -----------------------------------------------------

    @property
    def average_clv(self) -> Optional[float]:
        clvs = [t.clv for t in self._resolved_trades if t.clv is not None]
        if not clvs:
            return None
        return sum(clvs) / len(clvs)

    @property
    def win_rate(self) -> Optional[float]:
        resolved = [t for t in self._resolved_trades if t.outcome is not None]
        if not resolved:
            return None
        wins = sum(1 for t in resolved if t.outcome == "won")
        return wins / len(resolved)

    @property
    def total_volume(self) -> float:
        return sum(t.size_usdc for t in self._resolved_trades)

    # -- Daily reset -----------------------------------------------------

    def reset_daily(self) -> None:
        self._daily_loss = 0.0
        self.daily_trades = 0
        self.starting_daily_bankroll = self.bankroll

    # -- Persistence (trades.jsonl) --------------------------------------

    def _append_ledger(self, trade: TradeRecord) -> None:
        try:
            with LEDGER_FILE.open("a") as f:
                f.write(trade.model_dump_json() + "\n")
        except Exception as e:
            log.warning(f"Ledger write failed: {e}")

    # -- Scan results log ------------------------------------------------

    def record_scan(self, result: ScanResult) -> None:
        self._scan_results.append(result)
        if len(self._scan_results) > self._max_scan_results:
            self._scan_results = self._scan_results[-self._max_scan_results:]

    @property
    def scan_results(self) -> list[ScanResult]:
        return list(self._scan_results)

    @property
    def last_scan_time(self) -> Optional[float]:
        if not self._scan_results:
            return None
        return self._scan_results[-1].timestamp

    def find_trades_for_event(self, event_name: str) -> list[TradeRecord]:
        """Find open trades matching an event name (for resolver)."""
        matches = []
        for trade in self._open_trades.values():
            if trade.event_name == event_name:
                matches.append(trade)
        return matches
