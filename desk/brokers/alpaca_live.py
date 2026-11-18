"""Alpaca stock adapter for TESTNET (= Alpaca paper API) and LIVE modes.

Extended hours: Alpaca only accepts LIMIT orders, time_in_force=day, and has no stop
orders outside regular hours - so stops for stocks are client-side (Risk monitor)
and stocks are flattened before the after-hours session ends.
UNVERIFIED against a real account: run preflight in testnet mode first.
"""
from __future__ import annotations

import time

from ..models import Fill
from ..util import HttpError, http_json


class AlpacaBroker:
    name = "alpaca"

    def __init__(self, base: str, key: str, secret: str):
        self.base = base.rstrip("/")
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._cash = (0.0, 0.0)

    def _call(self, method, path, body=None, params=None):
        return http_json(f"{self.base}{path}", params, headers=self.h, method=method, body=body,
                         retries=3 if method == "GET" else 1)

    def cash(self) -> float:
        if time.time() - self._cash[0] > 10:
            a = self._call("GET", "/v2/account")
            self._cash = (time.time(), float(a.get("cash") or 0))
        return self._cash[1]

    def _limit(self, symbol, side, qty, price, wait_s=30) -> Fill | None:
        o = self._call("POST", "/v2/orders", body={
            "symbol": symbol, "qty": f"{qty:.4f}".rstrip("0").rstrip("."), "side": side, "type": "limit",
            "limit_price": f"{price:.2f}", "time_in_force": "day", "extended_hours": True})
        oid = o["id"]
        deadline = time.time() + wait_s
        while time.time() < deadline:
            o = self._call("GET", f"/v2/orders/{oid}")
            if o["status"] == "filled":
                break
            time.sleep(2)
        if o["status"] != "filled":
            try:
                self._call("DELETE", f"/v2/orders/{oid}")
            except HttpError:
                pass
            o = self._call("GET", f"/v2/orders/{oid}")
        q = float(o.get("filled_qty") or 0)
        if q <= 0:
            return None
        self._cash = (0.0, 0.0)
        return Fill(price=float(o["filled_avg_price"]), qty=q, fee=0.0, order_id=oid)

    def buy(self, c, qty, max_price) -> Fill | None:
        return self._limit(c.symbol, "buy", qty, max_price)

    def sell(self, p, quote) -> Fill | None:
        return self._limit(p.symbol, "sell", p.qty, quote[0] * 0.997)

    # no exchange-side stops in extended hours
    def place_stop(self, p):
        return ""

    def replace_stop(self, p):
        return ""

    def cancel_stop(self, p):
        return None

    def stop_filled(self, p):
        return None

    def funding_info(self, symbol):
        return None
