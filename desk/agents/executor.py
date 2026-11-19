"""Agent 4 - Executor. Sizes trades (hard cap: max_position_pct of equity, default 10%),
hands an OrderTicket to Risk, and only places an order that comes back sealed by Risk
with the stop already attached. Long-only, perps at 1x.

Leverage (paper only, risk.leverage): an instrument that moves little gets leverage, so its trade
can matter: L = target_atr_1h_pct / its 1h ATR, rounded down, at most max_x. A leveraged trade puts
up less cash (margin_pct of equity) for a position of margin * L; Risk then tightens its exits so
that it can never lose more than a normal 10% trade at its normal stop."""
from __future__ import annotations

import time

from ..models import ApprovedOrder, OrderTicket, Signal
from ..util import floor_step


def leverage_for(lev: dict, market: str, atr_1h_pct) -> int:
    """How much leverage the rules give a trade: 1 = none. See the module docstring."""
    mx = int((lev or {}).get("max_x") or 1)
    if mx <= 1 or market not in lev.get("markets", ()) or not atr_1h_pct or atr_1h_pct != atr_1h_pct:
        return 1
    return max(1, min(mx, int(lev["target_atr_1h_pct"] / atr_1h_pct)))


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
        lev = leverage_for(self.r.get("leverage"), c.market, sig.breakdown.get("_raw_atr_pct"))
        if self.mode != "paper":
            lev = 1                                    # leverage is simulated, paper only
        pct = self.r["leverage"]["margin_pct"] if lev > 1 else self.e["max_position_pct"]
        cap = equity * pct / 100                       # cash this trade may tie up
        budget = min(cap, cash * (1 - self.e.get("cash_buffer_pct", 0.5) / 100)) * lev
        price = c.ask
        step = c.meta.get("market_step") or c.meta.get("step") or 0
        min_qty = c.meta.get("market_min_qty") or c.meta.get("min_qty") or 0
        min_notional = c.meta.get("min_notional") or 0
        qty = floor_step(budget / price, step) if price > 0 else 0
        notional = qty * price
        if qty <= 0 or qty < min_qty or notional < min_notional:
            return self._kill(c, f"size ${notional:.2f} below exchange minimum ${min_notional:.2f} "
                                 f"({pct:g}% cap ${cap:.2f}{f' at {lev}x' if lev > 1 else ''}, cash ${cash:.2f})",
                              notional=notional, min_notional=min_notional, cap=cap, cash=cash, leverage=lev)
        return OrderTicket(signal=sig, side="buy", qty=qty, ref_price=price, notional=notional, leverage=lev)

    def execute(self, appr: ApprovedOrder):
        if not isinstance(appr, ApprovedOrder) or self.risk is None or not self.risk.verify_seal(appr):
            self.log.event("executor", "refused", "REFUSED order without a valid Risk seal", level="error")
            return None
        t = appr.ticket
        c = t.candidate
        broker = self.brokers.for_venue(c.venue)
        lev = t.leverage
        fill = broker.buy(c, t.qty, max_price=t.ref_price * 1.005, **({"leverage": lev} if lev > 1 else {}))
        if fill is None:
            return self._kill(c, "not filled: price moved >0.5% or insufficient cash at fill time")
        stop = self.risk.anchor_stop(appr, fill.price)
        now = time.time()
        margin = fill.qty * fill.price / lev if lev > 1 else None
        pid = self.store.open_position(
            symbol=c.symbol, base=c.base, market=c.market, venue=c.venue, qty=fill.qty,
            entry_price=fill.price, entry_ts=now, entry_fee=fill.fee, stop_price=stop,
            initial_stop=stop, peak_price=fill.price, trail_active=0, last_price=fill.price,
            last_ts=now, funding_paid=0.0,
            # perps: next funding time (paper); leveraged spot/stocks: last interest charge
            last_funding_ts=now if lev > 1 and c.market != "perp" else (c.meta.get("next_funding_ms") or 0) / 1000,
            score=t.signal.score, stop_order_id="", leverage=lev, margin=margin, exit_scale=appr.exit_scale)
        levered = f" at {lev}x, margin ${margin:.2f}" if lev > 1 else ""
        self.log.event("executor", "entry",
                       f"BUY {c.key} qty {fill.qty:g} @ {fill.price:.6g} (${fill.qty * fill.price:.2f}{levered}, "
                       f"fee ${fill.fee:.4f}) stop {stop:.6g} (-{appr.stop_pct}%) score {t.signal.score:.2f}",
                       level="trade", position_id=pid, order_id=fill.order_id)
        if self.mode != "paper" and self.r.get("exchange_side_stops", True):
            self.risk.protect(pid)
        return pid
