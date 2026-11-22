"""Agent 5 - Risk.

Before entry: approves or kills each OrderTicket and attaches the stop (-8% default).
Only Risk can seal an ApprovedOrder; the Executor won't trade without the seal.
After entry: monitors every open position (default every 15 s):
  * exits at the stop (-8% from fill)
  * once a position has gained +15%, trails the stop at peak - 8% (stop only moves up)
  * flattens stocks shortly before their session ends (regular close, or after-hours close if
    stocks trade after hours): no stock positions overnight

Leverage (paper only, risk.leverage, sized by the Executor): a trade at L x puts up margin_pct of
equity for a position of margin * L. Risk tightens every price exit of such a trade (stop, take
profit, trail start and trail distance) by exit_scale = max_position_pct / (margin_pct * L), at most 1,
so it can never lose more than a normal max_position_pct trade at the normal stop. Risk also caps all
open positions together at max_gross_exposure_pct of equity, charges interest on the borrowed part
(borrow_apr_pct) and closes a position whose margin is nearly gone (margin_call_pct, a safety net
behind the stop).

Optional rules, off unless set in the risk section (used by the daytrade desk):
  * take_profit_pct: sell once the bid is that far above the entry price
  * max_hold_hours: sell after that long, whatever the price
  * reentry_cooldown_min: no new entry in an asset for that long after selling it

Slots kept for stocks (risk.reserve_slots_for_stocks, e.g. 3 of max_open_positions 10): positions
outside US stocks (crypto, gold tokens and perps) may fill at most the other slots, so 24/7 crypto
can't take every slot before the US session opens. Holdings above that cap (from before it was
set) are sold, weakest first.

Circuit breakers: STOP file (no new entries), FLATTEN file (close everything),
daily drawdown limit, equity floor, max open positions, one position per asset.
STOP / FLATTEN hit every desk; STOP.<desk> / FLATTEN.<desk> (e.g. STOP.daytrade) only one.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import time

from ..models import ApprovedOrder, OrderTicket
from ..util import HttpError, us_eastern

OPTIONAL_RULES = ("take_profit_pct", "max_hold_hours", "reentry_cooldown_min")


def rules_summary(r: dict) -> str:
    """A desk's exit rules in one line (start message and `compare`)."""
    on = lambda k: r.get(k) or None  # noqa: E731
    parts = [f"stop -{r['stop_loss_pct']:g}%"]
    tp = on("take_profit_pct")
    if tp:
        parts.append(f"take profit +{tp:g}%")
    if not tp or tp > r["trail_activate_pct"]:
        parts.append(f"trail {r['trail_pct']:g}% after +{r['trail_activate_pct']:g}%")
    max_h = on("max_hold_hours")
    if max_h:
        parts.append(f"max hold {max_h:g}h")
    cd = on("reentry_cooldown_min")
    if cd:
        parts.append(f"re-entry cooldown {cd:g} min")
    keep = on("reserve_slots_for_stocks")
    if keep:
        parts.append(f"crypto max {max(0, r['max_open_positions'] - keep)} of {r['max_open_positions']} slots")
    return ", ".join(parts)


class Risk:
    name = "risk"

    def __init__(self, cfg, store, brokers, quotes, alpaca, log, root):
        self.r = cfg.section("risk")
        self.lev = self.r.get("leverage") or {}
        self.max_position_pct = cfg.section("executor")["max_position_pct"]
        self.stock_session = (cfg.get("universe") or {}).get("stocks_session", "regular")
        self.mode = cfg.mode
        self.store = store
        self.brokers = brokers
        self.quotes = quotes          # callable(list[Position]) -> {symbol: (bid, ask)}
        self.alpaca = alpaca
        self.log = log
        self.root = root
        self._secret = os.urandom(32)
        self._stale: dict[int, float] = {}
        self.desk = getattr(cfg, "desk_name", "main")
        for k in OPTIONAL_RULES:
            v = self.r.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                                  or not (0 <= v < float("inf"))):
                raise ValueError(f"risk.{k} must be a number >= 0 (0 or absent = off), got {v!r}")
        keep = self.r.get("reserve_slots_for_stocks")
        if keep is not None and (isinstance(keep, bool) or not isinstance(keep, int)
                                 or not 0 <= keep <= self.r["max_open_positions"]):
            raise ValueError(f"risk.reserve_slots_for_stocks must be a whole number from 0 to "
                             f"max_open_positions ({self.r['max_open_positions']}), got {keep!r}")

    def _opt(self, key: str):
        """An optional rule's value, or None when it is off (absent or 0)."""
        v = self.r.get(key)
        return v if v else None

    def rules_summary(self) -> str:
        return rules_summary(self.r)

    def crypto_cap(self) -> int | None:
        """Most positions outside US stocks the desk may hold: max_open_positions minus
        reserve_slots_for_stocks. None when no slots are kept for stocks."""
        keep = self.r.get("reserve_slots_for_stocks")
        return max(0, self.r["max_open_positions"] - keep) if keep else None

    # ------------------------------------------------------------ seal
    def _seal(self, t: OrderTicket, stop: float) -> str:
        msg = f"{t.ticket_id}|{t.candidate.symbol}|{t.qty!r}|{stop!r}|{t.leverage!r}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def verify_seal(self, a: ApprovedOrder) -> bool:
        return hmac.compare_digest(a.seal, self._seal(a.ticket, a.stop_price))

    # ------------------------------------------------------------ switches / breakers
    def switch(self, name: str) -> bool:
        """Kill-switch file in the project folder: NAME hits every desk, NAME.<desk> only this
        one. A trailing .txt counts too (Windows editors add it and Explorer hides it)."""
        return any((self.root / f).exists()
                   for f in (name, f"{name}.txt", f"{name}.{self.desk}", f"{name}.{self.desk}.txt"))

    def halted(self) -> str | None:
        if self.switch("STOP"):
            return "STOP file present"
        if self.switch("FLATTEN"):
            return "FLATTEN file present"
        equity = self.brokers.total_equity()
        key = f"day_start_equity:{dt.datetime.now(dt.timezone.utc).date()}"
        start = self.store.get(key)
        if start is None:
            self.store.set(key, equity)
            start = equity
        dd = (equity / start - 1) * 100 if start else 0
        if dd <= -self.r["max_daily_drawdown_pct"]:
            return f"daily drawdown {dd:.1f}% hit limit -{self.r['max_daily_drawdown_pct']}%"
        if equity < self.r["halt_below_equity_usd"]:
            return f"equity ${equity:.2f} below floor ${self.r['halt_below_equity_usd']:.2f}"
        return None

    def _minutes_left_stock_session(self) -> float | None:
        if not self.alpaca:
            return None
        try:
            w = self.alpaca.session_window(self.stock_session)
        except HttpError:
            return None
        if not w:
            return None
        return (w[1] - us_eastern()).total_seconds() / 60

    # ------------------------------------------------------------ pre-entry
    def _kill(self, c, reason, **d):
        self.log.kill("risk", c.symbol, c.market, reason, **d)
        return None

    def approve(self, t: OrderTicket) -> ApprovedOrder | None:
        if not isinstance(t, OrderTicket):
            raise TypeError("Risk only accepts OrderTickets from Executor")
        c = t.candidate
        why = self.halted()
        if why:
            return self._kill(c, f"halted: {why}")
        open_pos = self.store.open_positions()
        if len(open_pos) >= self.r["max_open_positions"]:
            return self._kill(c, f"max open positions ({self.r['max_open_positions']}) reached")
        cap = self.crypto_cap()
        if cap is not None and c.market != "stock":
            held = sum(1 for p in open_pos if p.market != "stock")
            if held >= cap:
                return self._kill(c, f"crypto slots full: {held}/{cap} ({self.r['reserve_slots_for_stocks']} of "
                                     f"{self.r['max_open_positions']} slots kept for stocks)")
        if any(p.symbol == c.symbol and p.market == c.market for p in open_pos):
            return self._kill(c, "already holding this instrument")
        if self.r.get("one_position_per_asset", True):
            dup = next((p for p in open_pos if p.base == c.base), None)
            if dup:
                return self._kill(c, f"already exposed to {c.base} via {dup.market}:{dup.symbol}")
        cd = self._opt("reentry_cooldown_min")
        if cd:
            row = self.store.q("SELECT MAX(exit_ts) AS t FROM positions WHERE status='closed' AND base=?",
                               (c.base,))
            last = row[0]["t"] if row else None
            if last:
                mins = (time.time() - last) / 60
                if mins < cd:
                    return self._kill(c, f"re-entry cooldown: {c.base} sold {mins:.0f} min ago (< {cd:g} min)")
        if c.market == "stock":
            left = self._minutes_left_stock_session()
            need = self.r["no_stock_entries_min_before_session_end"]
            if left is None or left < need:
                return self._kill(c, f"too close to session end ({left and round(left)} min left < {need})")
        k = 1.0
        if t.leverage > 1:
            why = self._leverage_refusal(t, open_pos)
            if why:
                return self._kill(c, why)
            k = self.exit_scale(t.leverage)
        pct = self.r["stop_loss_pct"] if k == 1 else round(self.r["stop_loss_pct"] * k, 2)
        stop = t.ref_price * (1 - pct / 100)
        if not (0 < stop < t.ref_price):
            return self._kill(c, f"invalid stop {stop}")
        appr = ApprovedOrder(ticket=t, stop_price=stop, stop_pct=pct,
                             trail_activate_pct=self.r["trail_activate_pct"] * k, trail_pct=self.r["trail_pct"] * k,
                             seal=self._seal(t, stop), exit_scale=k)
        tp, max_h = self._opt("take_profit_pct"), self._opt("max_hold_hours")
        extra = (f", take profit +{self._pct(tp, k):g}%" if tp else "") + (f", max hold {max_h:g}h" if max_h else "")
        levered = f" at {t.leverage}x (margin ${t.notional / t.leverage:.2f})" if t.leverage > 1 else ""
        self.log.event("risk", "approved",
                       f"{c.key} approved: ${t.notional:.2f}{levered}, stop {stop:.6g} (-{pct}%), "
                       f"max loss ${t.notional * pct / 100:.2f}{extra}")
        return appr

    # ------------------------------------------------------------ leverage
    def exit_scale(self, leverage: float) -> float:
        """How much tighter a trade at `leverage` x exits: 1 = as usual. Keeps the most a trade can
        lose at its stop at or under that of a normal (max_position_pct) trade."""
        if leverage <= 1:
            return 1.0
        return min(1.0, self.max_position_pct / (self.lev["margin_pct"] * leverage))

    @staticmethod
    def _pct(v: float, k: float) -> float:
        return v if k == 1 else round(v * k, 2)

    def _leverage_refusal(self, t: OrderTicket, open_pos) -> str | None:
        c = t.candidate
        if self.mode != "paper":
            return "leverage is paper-only"
        mx = int(self.lev.get("max_x") or 1)
        if t.leverage > mx or c.market not in self.lev.get("markets", ()):
            return f"leverage {t.leverage}x not allowed for {c.market} (max {mx}x)"
        equity = self.brokers.total_equity()
        gross = sum(p.qty * p.last_price for p in open_pos) + t.notional
        cap = equity * self.lev.get("max_gross_exposure_pct", 100) / 100
        if gross > cap:
            return f"gross exposure ${gross:.2f} would pass {self.lev.get('max_gross_exposure_pct', 100):g}% of equity (${cap:.2f})"
        return None

    def _borrow(self, p, now: float | None = None):
        """Paper only: interest on the borrowed part of a leveraged spot/stock position, charged
        from cash every hour (and at exit) and counted in its funding_paid."""
        if self.mode != "paper" or p.leverage <= 1 or p.market == "perp":
            return
        apr = (self.lev.get("borrow_apr_pct") or {}).get(p.market, 0)
        now = now or time.time()
        borrowed = p.qty * p.entry_price - p.margin_used
        if apr <= 0 or borrowed <= 0 or not p.last_funding_ts:
            return
        cost = borrowed * apr / 100 * (now - p.last_funding_ts) / (365 * 86400)
        if cost <= 0:
            return
        self.brokers.for_venue(p.venue).adjust_cash(-cost)
        p.funding_paid += cost
        p.last_funding_ts = now
        self.store.update_position(p.id, funding_paid=p.funding_paid, last_funding_ts=now)

    def anchor_stop(self, appr: ApprovedOrder, fill_price: float) -> float:
        """Stop is defined as -stop_pct from the actual entry; never looser than pre-entry stop
        by more than the slippage guard."""
        return fill_price * (1 - appr.stop_pct / 100)

    # ------------------------------------------------------------ exchange-side stops (live/testnet)
    def protect(self, pid: int):
        p = next((x for x in self.store.open_positions() if x.id == pid), None)
        if not p:
            return
        broker = self.brokers.for_venue(p.venue)
        try:
            oid = broker.place_stop(p)
            self.store.update_position(pid, stop_order_id=oid or "")
            if oid:
                self.log.event("risk", "stop_placed", f"exchange stop placed for {p.symbol} @ {p.stop_price:.6g}")
        except Exception as e:  # noqa: BLE001
            self.log.event("risk", "stop_failed", f"could not place exchange stop for {p.symbol}: {e} "
                           "-> emergency exit", level="error")
            q = self.quotes([p]).get(p.symbol)
            if q:
                self.exit(p, "emergency: exchange stop placement failed", q)

    # ------------------------------------------------------------ monitor
    def monitor(self):
        positions = self.store.open_positions()
        if not positions:
            return
        try:
            quotes = self.quotes(positions)
        except Exception as e:  # noqa: BLE001
            self.log.event("risk", "quotes", f"quote fetch failed: {e}", level="warn")
            return
        flatten_all = self.switch("FLATTEN")
        tp, max_h = self._opt("take_profit_pct"), self._opt("max_hold_hours")
        stock_left = None
        if any(p.market == "stock" for p in positions):
            stock_left = self._minutes_left_stock_session()
        now = time.time()
        for p in positions:
            q = quotes.get(p.symbol)
            if not q or q[0] <= 0:
                first = self._stale.setdefault(p.id, now)
                if now - first > self.r["stale_quote_alert_sec"]:
                    self.log.event("risk", "stale", f"no quote for {p.symbol} for {now - first:.0f}s", level="warn")
                    self._stale[p.id] = now
                continue
            self._stale.pop(p.id, None)
            bid = q[0]
            peak = max(p.peak_price, bid)
            stop, trail, k = p.stop_price, p.trail_active, p.exit_scale
            trail_pct = self._pct(self.r["trail_pct"], k)
            if peak >= p.entry_price * (1 + self.r["trail_activate_pct"] * k / 100):
                if not trail:
                    self.log.event("risk", "trail_on", f"{p.symbol} +{(peak / p.entry_price - 1) * 100:.1f}%: "
                                   f"trailing stop activated ({trail_pct}% below peak)")
                trail = True
                stop = max(stop, peak * (1 - trail_pct / 100))
            raised = stop > p.stop_price * 1.000001
            self.store.update_position(p.id, last_price=bid, last_ts=now, peak_price=peak,
                                       stop_price=stop, trail_active=int(trail))
            p.last_price, p.peak_price, p.stop_price, p.trail_active = bid, peak, stop, trail
            if raised:
                self.log.event("risk", "stop_up", f"{p.symbol} stop raised to {stop:.6g} "
                               f"(locks {(stop / p.entry_price - 1) * 100:+.1f}%)")
                if self.mode != "paper" and p.stop_order_id:
                    try:
                        oid = self.brokers.for_venue(p.venue).replace_stop(p)
                        self.store.update_position(p.id, stop_order_id=oid or "")
                    except Exception as e:  # noqa: BLE001
                        self.log.event("risk", "stop_replace", f"{p.symbol}: stop replace failed: {e} "
                                       "(client-side stop still active)", level="error")
            self._funding(p)
            if p.leverage > 1 and now - (p.last_funding_ts or now) >= 3600:
                self._borrow(p, now)
            reason = None
            if bid <= stop:
                reason = "trailing stop" if trail else f"stop -{self._pct(self.r['stop_loss_pct'], k)}%"
            elif tp and bid >= p.entry_price * (1 + self._pct(tp, k) / 100):
                reason = f"take profit +{self._pct(tp, k):g}%"
            elif p.leverage > 1 and p.value - p.funding_paid <= p.margin_used * self.lev.get("margin_call_pct", 20) / 100:
                reason = "margin call"
            elif flatten_all:
                reason = "manual FLATTEN"
            elif p.market == "stock" and (stock_left is None
                                          or stock_left <= self.r["flatten_stocks_min_before_session_end"]):
                reason = ("flatten before after-hours close" if self.stock_session == "after_hours"
                          else "flatten before the close")
            elif max_h and now - p.entry_ts >= max_h * 3600:
                reason = f"time exit {max_h:g}h"
            if reason:
                self.exit(p, reason, q)
        self._trim_to_crypto_cap(quotes)

    def _trim_to_crypto_cap(self, quotes: dict):
        """Sells positions outside US stocks above crypto_cap (held from before slots were kept for
        stocks), weakest first, so the kept slots are free when the US session opens."""
        cap = self.crypto_cap()
        if cap is None:
            return
        held = [p for p in self.store.open_positions() if p.market != "stock"]
        if len(held) <= cap:
            return
        weakest = sorted(held, key=lambda p: p.last_price / p.entry_price if p.entry_price else 0)
        for p in weakest[:len(held) - cap]:
            q = quotes.get(p.symbol)
            if q and q[0] > 0:
                self.exit(p, f"slot reserve: crypto max {cap}", q)

    def _funding(self, p):
        """Paper only: charge/credit perp funding when a funding timestamp passes."""
        if self.mode != "paper" or p.market != "perp" or not p.last_funding_ts:
            return
        if time.time() < p.last_funding_ts:
            return
        broker = self.brokers.for_venue(p.venue)
        info = broker.funding_info(p.symbol)
        if not info:
            return
        cost = p.qty * info["mark"] * info["funding_rate"]  # longs pay positive funding
        broker.adjust_cash(-cost)
        self.store.update_position(p.id, funding_paid=p.funding_paid + cost,
                                   last_funding_ts=info["next_funding_ms"] / 1000)
        self.log.event("risk", "funding", f"{p.symbol} funding {'paid' if cost > 0 else 'received'} "
                       f"${abs(cost):.4f} (rate {info['funding_rate'] * 100:+.4f}%)")

    def reconcile(self):
        """Live/testnet: detect exchange-side stops that filled between monitor polls."""
        if self.mode == "paper":
            return
        for p in self.store.open_positions():
            if not p.stop_order_id:
                continue
            try:
                fill = self.brokers.for_venue(p.venue).stop_filled(p)
            except Exception as e:  # noqa: BLE001
                self.log.event("risk", "reconcile", f"{p.symbol}: stop status check failed: {e}", level="warn")
                continue
            if fill:
                self._close(p, fill, "exchange stop filled")

    def exit(self, p, reason: str, quote):
        broker = self.brokers.for_venue(p.venue)
        self._borrow(p)            # interest up to now (leveraged paper positions only)
        fill = None
        if self.mode != "paper" and p.stop_order_id:
            try:
                fill = broker.cancel_stop(p)  # returns a Fill if the exchange stop already executed
                if fill:
                    reason = "exchange stop filled"
            except Exception as e:  # noqa: BLE001
                self.log.event("risk", "cancel_stop", f"{p.symbol}: cancel stop failed: {e}", level="warn")
        if fill is None:
            try:
                fill = broker.sell(p, quote)
            except Exception as e:  # noqa: BLE001
                self.log.event("risk", "exit_failed", f"sell error {p.symbol}: {e}", level="error")
                fill = None
        if fill is None:
            self.log.event("risk", "exit_failed", f"EXIT FAILED {p.symbol} ({reason}) - will retry", level="error")
            return
        self._close(p, fill, reason)

    def _close(self, p, fill, reason: str):
        pnl = (fill.price - p.entry_price) * fill.qty - p.entry_fee - fill.fee - p.funding_paid
        self.store.update_position(p.id, status="closed", exit_price=fill.price, exit_ts=time.time(),
                                   exit_fee=fill.fee, exit_reason=reason, pnl=pnl)
        self.log.event("risk", "exit", f"SELL {p.market}:{p.symbol} @ {fill.price:.6g} [{reason}] "
                       f"P&L ${pnl:+.4f} ({(fill.price / p.entry_price - 1) * 100:+.2f}%)",
                       level="trade", position_id=p.id, pnl=pnl)
