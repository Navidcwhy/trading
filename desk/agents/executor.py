"""Agent 4 - Executor. Sizes trades (hard cap: max_position_pct of equity, default 10%),
hands an OrderTicket to Risk, and only places an order that comes back sealed by Risk
with the stop already attached. Long-only, perps at 1x."""
from __future__ import annotations

import time

from ..models import ApprovedOrder, OrderTicket, Signal
from ..util import floor_step


class Executor:
    name = "executor"

    def __init__(self, cfg, brokers, store, log):
        self.e = cfg.section("executor")
        self.r = cfg.section("risk")
        self.mode = cfg.mode
        self.brokers = brokers
        self.store = store
        self.log = log
        self.risk = None  # wired by Desk (needed to verify seals)

    def _kill(self, c, reason, **d):
        self.log.kill("executor", c.symbol, c.market, reason, **d)
        return None

    def propose(self, sig: Signal) -> OrderTicket | None:
        if not isinstance(sig, Signal):
            raise TypeError("Executor only accepts Signals from Analyst")
        c = sig.candidate
        broker = self.brokers.for_venue(c.venue)
        equity = self.brokers.total_equity()
        cash = broker.cash()
        cap = equity * self.e["max_position_pct"] / 100
        budget = min(cap, cash * (1 - self.e.get("cash_buffer_pct", 0.5) / 100))
        price = c.ask
        step = c.meta.get("market_step") or c.meta.get("step") or 0
        min_qty = c.meta.get("market_min_qty") or c.meta.get("min_qty") or 0
        min_notional = c.meta.get("min_notional") or 0
        qty = floor_step(budget / price, step) if price > 0 else 0
        notional = qty * price
        if qty <= 0 or qty < min_qty or notional < min_notional:
            return self._kill(c, f"size ${notional:.2f} below exchange minimum ${min_notional:.2f} "
                                 f"(10% cap ${cap:.2f}, cash ${cash:.2f})",
                              notional=notional, min_notional=min_notional, cap=cap, cash=cash)
        return OrderTicket(signal=sig, side="buy", qty=qty, ref_price=price, notional=notional)

    def execute(self, appr: ApprovedOrder):
        if not isinstance(appr, ApprovedOrder) or self.risk is None or not self.risk.verify_seal(appr):
            self.log.event("executor", "refused", "REFUSED order without a valid Risk seal", level="error")
            return None
        t = appr.ticket
        c = t.candidate
        broker = self.brokers.for_venue(c.venue)
        fill = broker.buy(c, t.qty, max_price=t.ref_price * 1.005)
        if fill is None:
            return self._kill(c, "not filled: price moved >0.5% or insufficient cash at fill time")
        stop = self.risk.anchor_stop(appr, fill.price)
        pid = self.store.open_position(
            symbol=c.symbol, base=c.base, market=c.market, venue=c.venue, qty=fill.qty,
            entry_price=fill.price, entry_ts=time.time(), entry_fee=fill.fee, stop_price=stop,
            initial_stop=stop, peak_price=fill.price, trail_active=0, last_price=fill.price,
            last_ts=time.time(), funding_paid=0.0,
            last_funding_ts=(c.meta.get("next_funding_ms") or 0) / 1000,  # = next funding time (paper)
            score=t.signal.score, stop_order_id="")
        self.log.event("executor", "entry",
                       f"BUY {c.key} qty {fill.qty:g} @ {fill.price:.6g} (${fill.qty * fill.price:.2f}, "
                       f"fee ${fill.fee:.4f}) stop {stop:.6g} (-{appr.stop_pct}%) score {t.signal.score:.2f}",
                       level="trade", position_id=pid, order_id=fill.order_id)
        if self.mode != "paper" and self.r.get("exchange_side_stops", True):
            self.risk.protect(pid)
        return pid
