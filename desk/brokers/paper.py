"""Paper broker: fills against live public quotes with fees + slippage, one shared
cash pool, enforces the same exchange minimums the live venues do."""
from __future__ import annotations

import uuid

from ..models import Candidate, Fill
from ..util import floor_step


class Brokers:
    """Routes venues to brokers and computes total equity."""

    def __init__(self, mapping: dict, store):
        self.mapping = mapping
        self.store = store

    def for_venue(self, venue: str):
        return self.mapping[venue]

    def total_equity(self) -> float:
        cash = sum(b.cash() for b in {id(b): b for b in self.mapping.values()}.values())
        return cash + sum(p.qty * p.last_price for p in self.store.open_positions())


class PaperBroker:
    name = "paper"

    def __init__(self, cfg, store, fresh_quote, funding_fn):
        self.p = cfg.section("paper")
        self.store = store
        self.fresh_quote = fresh_quote     # (market, symbol) -> (bid, ask)
        self.funding_fn = funding_fn       # symbol -> {funding_rate, mark, next_funding_ms}
        if store.get("paper_cash") is None:
            store.set("paper_cash", float(cfg["start_equity_usd"]))
            store.set("paper_start_equity", float(cfg["start_equity_usd"]))

    def cash(self) -> float:
        return float(self.store.get("paper_cash", 0.0))

    def adjust_cash(self, delta: float):
        with self.store.lock:
            self.store.set("paper_cash", self.cash() + delta)

    def _fee(self, market):
        return self.p["fee_pct"][market] / 100

    def buy(self, c: Candidate, qty: float, max_price: float) -> Fill | None:
        bid, ask = self.fresh_quote(c.market, c.symbol)
        if ask <= 0 or ask > max_price:
            return None
        price = ask * (1 + self.p["slippage_pct"] / 100)
        with self.store.lock:
            cash = self.cash()
            fee_r = self._fee(c.market)
            if qty * price * (1 + fee_r) > cash:
                step = c.meta.get("market_step") or c.meta.get("step") or 0
                qty = floor_step(cash / (price * (1 + fee_r)), step)
                if qty * price < (c.meta.get("min_notional") or 0) or qty <= 0:
                    return None
            notional = qty * price
            fee = notional * fee_r
            self.store.set("paper_cash", cash - notional - fee)
        return Fill(price=price, qty=qty, fee=fee, order_id="paper-" + uuid.uuid4().hex[:8])

    def sell(self, p, quote) -> Fill | None:
        bid = quote[0]
        if bid <= 0:
            return None
        price = bid * (1 - self.p["slippage_pct"] / 100)
        notional = p.qty * price
        fee = notional * self._fee(p.market)
        self.adjust_cash(notional - fee)
        return Fill(price=price, qty=p.qty, fee=fee, order_id="paper-" + uuid.uuid4().hex[:8])

    def funding_info(self, symbol):
        return self.funding_fn(symbol)

    # exchange-side stops don't exist in paper; Risk monitors client-side
    def place_stop(self, p):
        return ""

    def replace_stop(self, p):
        return ""

    def cancel_stop(self, p):
        return None
