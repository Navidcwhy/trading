"""Binance spot + USD-M futures order adapters for TESTNET and LIVE modes.

UNVERIFIED against a real account: run `python -m desk preflight` in testnet mode
first; it exercises signing, balances and order placement/cancel on the exchange's
test environment. Orders are never retried automatically (no duplicate orders).
"""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse

from ..models import Fill
from ..util import HttpError, floor_step, http_json


def _round_tick(x: float, tick: float) -> str:
    if tick <= 0:
        return f"{x:.8f}".rstrip("0").rstrip(".")
    v = floor_step(x, tick)
    decimals = max(0, len(f"{tick:.10f}".rstrip("0").split(".")[1])) if tick < 1 else 0
    return f"{v:.{decimals}f}"


def _qty(x: float, step: float) -> str:
    return _round_tick(x, step)


class _Signed:
    def __init__(self, base: str, key: str, secret: str):
        self.base, self.key, self.secret = base.rstrip("/"), key, secret.encode()

    def call(self, method: str, path: str, params: dict | None = None):
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 5000
        qs = urllib.parse.urlencode(p)
        qs += "&signature=" + hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
        h = {"X-MBX-APIKEY": self.key}
        if method in ("GET", "DELETE"):
            return http_json(f"{self.base}{path}?{qs}", headers=h, method=method,
                             retries=3 if method == "GET" else 1)
        return http_json(f"{self.base}{path}", headers=h, method=method, form=qs, retries=1)


class BinanceSpotBroker:
    name = "binance_spot"

    def __init__(self, base: str, key: str, secret: str, info_fn):
        self.api = _Signed(base, key, secret)
        self.info_fn = info_fn  # symbol -> filters dict
        self._cash = (0.0, 0.0)

    def _balances(self) -> dict:
        acct = self.api.call("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        return {b["asset"]: float(b["free"]) for b in acct["balances"]}

    def cash(self) -> float:
        if time.time() - self._cash[0] > 10:
            self._cash = (time.time(), self._balances().get("USDT", 0.0))
        return self._cash[1]

    def _fill_from(self, r: dict, base_asset: str, price_hint: float) -> Fill:
        qty = float(r.get("executedQty") or 0)
        quote = float(r.get("cummulativeQuoteQty") or 0)
        price = quote / qty if qty else price_hint
        fee_usd, fee_base = 0.0, 0.0
        for f in r.get("fills", []):
            c, a = float(f["commission"]), f["commissionAsset"]
            if a == "USDT":
                fee_usd += c
            elif a == base_asset:
                fee_base += c
                fee_usd += c * price
            else:
                fee_usd += float(f["qty"]) * float(f["price"]) * 0.001  # e.g. BNB: approximate
        return Fill(price=price, qty=qty - fee_base, fee=fee_usd, order_id=str(r.get("orderId")))

    def buy(self, c, qty, max_price) -> Fill | None:
        self._cash = (0.0, 0.0)
        r = self.api.call("POST", "/api/v3/order", {
            "symbol": c.symbol, "side": "BUY", "type": "MARKET",
            "quoteOrderQty": f"{qty * c.ask:.2f}", "newOrderRespType": "FULL"})
        f = self._fill_from(r, c.meta["base"], c.ask)
        f.qty = floor_step(f.qty, c.meta.get("step", 0))
        return f if f.qty > 0 else None

    def sell(self, p, quote) -> Fill | None:
        self._cash = (0.0, 0.0)
        info = self.info_fn(p.symbol)
        base = info["base"]
        free = self._balances().get(base, 0.0)
        qty = floor_step(min(p.qty, free), info.get("step", 0))
        if qty <= 0:
            return None
        r = self.api.call("POST", "/api/v3/order", {
            "symbol": p.symbol, "side": "SELL", "type": "MARKET", "quantity": _qty(qty, info.get("step", 0)),
            "newOrderRespType": "FULL"})
        return self._fill_from(r, base, quote[0])

    def place_stop(self, p) -> str:
        info = self.info_fn(p.symbol)
        params = {"symbol": p.symbol, "side": "SELL", "quantity": _qty(p.qty, info.get("step", 0)),
                  "stopPrice": _round_tick(p.stop_price, info.get("tick", 0))}
        if "STOP_LOSS" in info.get("order_types", []):
            params["type"] = "STOP_LOSS"
        else:
            params.update(type="STOP_LOSS_LIMIT", timeInForce="GTC",
                          price=_round_tick(p.stop_price * 0.99, info.get("tick", 0)))
        return str(self.api.call("POST", "/api/v3/order", params)["orderId"])

    def stop_filled(self, p) -> Fill | None:
        if not p.stop_order_id:
            return None
        o = self.api.call("GET", "/api/v3/order", {"symbol": p.symbol, "orderId": p.stop_order_id})
        if o.get("status") == "FILLED":
            qty = float(o["executedQty"])
            px = float(o["cummulativeQuoteQty"]) / qty if qty else p.stop_price
            return Fill(price=px, qty=qty, fee=qty * px * 0.001, order_id=str(o["orderId"]))
        return None

    def cancel_stop(self, p) -> Fill | None:
        filled = self.stop_filled(p)
        if filled:
            return filled
        try:
            self.api.call("DELETE", "/api/v3/order", {"symbol": p.symbol, "orderId": p.stop_order_id})
        except HttpError as e:
            if "Unknown order" not in str(e):
                raise
        return None

    def replace_stop(self, p) -> str:
        self.cancel_stop(p)
        return self.place_stop(p)


class BinanceFuturesBroker:
    name = "binance_futures"

    def __init__(self, base: str, key: str, secret: str, info_fn):
        self.api = _Signed(base, key, secret)
        self.info_fn = info_fn
        self._setup: set[str] = set()
        self._cash = (0.0, 0.0)

    def cash(self) -> float:
        if time.time() - self._cash[0] > 10:
            bals = self.api.call("GET", "/fapi/v2/balance")
            usdt = next((float(b["availableBalance"]) for b in bals if b["asset"] == "USDT"), 0.0)
            self._cash = (time.time(), usdt)
        return self._cash[1]

    def _prepare(self, symbol: str):
        if symbol in self._setup:
            return
        try:
            self.api.call("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
        except HttpError as e:
            if "-4046" not in str(e):  # "No need to change margin type"
                raise
        self.api.call("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": 1})
        self._setup.add(symbol)

    def buy(self, c, qty, max_price) -> Fill | None:
        self._prepare(c.symbol)
        self._cash = (0.0, 0.0)
        step = c.meta.get("market_step") or c.meta.get("step", 0)
        r = self.api.call("POST", "/fapi/v1/order", {
            "symbol": c.symbol, "side": "BUY", "type": "MARKET", "quantity": _qty(qty, step),
            "newOrderRespType": "RESULT"})
        q = float(r.get("executedQty") or 0)
        px = float(r.get("avgPrice") or 0) or c.ask
        return Fill(price=px, qty=q, fee=q * px * 0.0005, order_id=str(r.get("orderId"))) if q > 0 else None

    def sell(self, p, quote) -> Fill | None:
        self._cash = (0.0, 0.0)
        info = self.info_fn(p.symbol)
        step = info.get("market_step") or info.get("step", 0)
        r = self.api.call("POST", "/fapi/v1/order", {
            "symbol": p.symbol, "side": "SELL", "type": "MARKET", "quantity": _qty(p.qty, step),
            "reduceOnly": "true", "newOrderRespType": "RESULT"})
        q = float(r.get("executedQty") or 0)
        px = float(r.get("avgPrice") or 0) or quote[0]
        return Fill(price=px, qty=q, fee=q * px * 0.0005, order_id=str(r.get("orderId"))) if q > 0 else None

    def place_stop(self, p) -> str:
        info = self.info_fn(p.symbol)
        r = self.api.call("POST", "/fapi/v1/order", {
            "symbol": p.symbol, "side": "SELL", "type": "STOP_MARKET",
            "stopPrice": _round_tick(p.stop_price, info.get("tick", 0)),
            "closePosition": "true", "workingType": "MARK_PRICE"})
        return str(r["orderId"])

    def stop_filled(self, p) -> Fill | None:
        if not p.stop_order_id:
            return None
        o = self.api.call("GET", "/fapi/v1/order", {"symbol": p.symbol, "orderId": p.stop_order_id})
        if o.get("status") == "FILLED":
            q, px = float(o["executedQty"]), float(o.get("avgPrice") or p.stop_price)
            return Fill(price=px, qty=q, fee=q * px * 0.0005, order_id=str(o["orderId"]))
        return None

    def cancel_stop(self, p) -> Fill | None:
        filled = self.stop_filled(p)
        if filled:
            return filled
        try:
            self.api.call("DELETE", "/fapi/v1/order", {"symbol": p.symbol, "orderId": p.stop_order_id})
        except HttpError as e:
            if "-2011" not in str(e):  # unknown order
                raise
        return None

    def replace_stop(self, p) -> str:
        self.cancel_stop(p)
        return self.place_stop(p)

    def funding_info(self, symbol):
        return None  # the exchange settles funding itself in live/testnet
