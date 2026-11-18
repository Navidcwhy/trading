"""Handoff contracts between the six agents.

Scout --Candidate--> Filter --Survivor--> Analyst --Signal--> Executor
Executor --OrderTicket--> Risk --ApprovedOrder (sealed)--> Executor --> broker
Every agent --Kill / event--> Logger

Each object can only be built from the previous stage's object, so no agent can skip
another. ApprovedOrder additionally carries an HMAC seal that only Risk can produce;
the Executor refuses to place any order without a valid seal (and therefore without
a stop already attached).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candidate:  # Scout -> Filter
    symbol: str          # venue symbol, e.g. BTCUSDT, AAPL
    base: str            # underlying asset, e.g. BTC, AAPL (used for one-position-per-asset)
    market: str          # spot | perp | stock
    venue: str           # binance_spot | binance_futures | alpaca
    bid: float
    ask: float
    last: float
    volume_24h_usd: float
    meta: dict = field(default_factory=dict)   # filters, funding, etc.
    ts: float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.mid > 0 else float("inf")

    @property
    def key(self) -> str:
        return f"{self.market}:{self.symbol}"


@dataclass(frozen=True)
class Survivor:  # Filter -> Analyst
    candidate: Candidate
    checks: dict          # measured metrics from every filter that passed


@dataclass(frozen=True)
class Signal:  # Analyst -> Executor
    survivor: Survivor
    score: float
    breakdown: dict

    @property
    def candidate(self) -> Candidate:
        return self.survivor.candidate


@dataclass(frozen=True)
class OrderTicket:  # Executor -> Risk
    signal: Signal
    side: str             # buy (long-only desk)
    qty: float
    ref_price: float      # ask at proposal time
    notional: float
    ticket_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def candidate(self) -> Candidate:
        return self.signal.candidate


@dataclass(frozen=True)
class ApprovedOrder:  # Risk -> Executor
    ticket: OrderTicket
    stop_price: float
    stop_pct: float
    trail_activate_pct: float
    trail_pct: float
    seal: str


@dataclass
class Fill:
    price: float
    qty: float
    fee: float
    order_id: str = ""


@dataclass
class Position:
    id: int
    symbol: str
    base: str
    market: str
    venue: str
    qty: float
    entry_price: float
    entry_ts: float
    entry_fee: float
    stop_price: float
    initial_stop: float
    peak_price: float
    trail_active: bool
    last_price: float
    last_ts: float
    funding_paid: float
    last_funding_ts: float
    score: float
    stop_order_id: str
    status: str = "open"

    @property
    def notional(self) -> float:
        return self.qty * self.last_price

    @property
    def unrealized(self) -> float:
        return (self.last_price - self.entry_price) * self.qty
