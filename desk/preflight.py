"""Preflight checks before trusting an order adapter.

testnet: exercises signing, balances, and place+cancel of a far-from-market limit
         order on each venue's TEST environment (no real money).
live:    read-only - balances readable, Binance key has withdrawals DISABLED.
"""
from __future__ import annotations

import json
import time

from .util import http_json


def run_preflight(desk) -> bool:
    mode = desk.mode
    results, ok = {}, True
    if mode == "paper":
        print("Paper mode needs no preflight.")
        return True
    for venue, b in desk.brokers.mapping.items():
        try:
            cash = b.cash()
            info = {"cash_usd": round(cash, 2)}
            if mode == "testnet":
                info.update(_order_roundtrip(venue, b))
            else:
                info.update(_live_checks(venue, b))
            results[venue] = {"ok": True, **info}
            print(f"[OK]   {venue}: {info}")
        except Exception as e:  # noqa: BLE001
            ok = False
            results[venue] = {"ok": False, "error": str(e)[:300]}
            print(f"[FAIL] {venue}: {e}")
    if ok:
        (desk.cfg.data_dir / "preflight_ok.json").write_text(
            json.dumps({"ts": time.time(), "mode": mode, "results": results}, indent=2), encoding="utf-8")
        print(f"Preflight passed -> {desk.cfg.data_dir / 'preflight_ok.json'}")
    else:
        print("Preflight FAILED - fix the errors above before going further.")
    return ok


def _order_roundtrip(venue, b) -> dict:
    if venue == "binance_spot":
        bid = float(http_json(f"{b.api.base}/api/v3/ticker/bookTicker", {"symbol": "BTCUSDT"})["bidPrice"])
        px = round(bid * 0.5, 2)
        o = b.api.call("POST", "/api/v3/order", {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT",
                                                 "timeInForce": "GTC", "quantity": f"{max(0.0002, 12 / px):.5f}",
                                                 "price": f"{px:.2f}"})
        b.api.call("DELETE", "/api/v3/order", {"symbol": "BTCUSDT", "orderId": o["orderId"]})
        return {"order_roundtrip": "BTCUSDT limit placed+cancelled"}
    if venue == "binance_futures":
        b._prepare("ETHUSDT")
        bid = float(http_json(f"{b.api.base}/fapi/v1/ticker/bookTicker", {"symbol": "ETHUSDT"})["bidPrice"])
        px = round(bid * 0.7, 2)
        o = b.api.call("POST", "/fapi/v1/order", {"symbol": "ETHUSDT", "side": "BUY", "type": "LIMIT",
                                                  "timeInForce": "GTC", "quantity": f"{max(0.01, 25 / px):.3f}",
                                                  "price": f"{px:.2f}"})
        b.api.call("DELETE", "/fapi/v1/order", {"symbol": "ETHUSDT", "orderId": o["orderId"]})
        return {"order_roundtrip": "ETHUSDT limit placed+cancelled, 1x isolated set"}
    if venue == "alpaca":
        o = b._call("POST", "/v2/orders", body={"symbol": "SPY", "qty": "1", "side": "buy", "type": "limit",
                                                "limit_price": "1.00", "time_in_force": "day",
                                                "extended_hours": True})
        b._call("DELETE", f"/v2/orders/{o['id']}")
        return {"order_roundtrip": "SPY ext-hours limit placed+cancelled"}
    return {}


def _live_checks(venue, b) -> dict:
    if venue in ("binance_spot", "binance_futures"):
        base = "https://api.binance.com"
        from .brokers.binance_live import _Signed
        r = _Signed(base, b.api.key, b.api.secret.decode()).call("GET", "/sapi/v1/account/apiRestrictions")
        if r.get("enableWithdrawals"):
            raise RuntimeError("API key has WITHDRAWALS ENABLED - create a trade-only key")
        return {"withdrawals": "disabled", "ip_restricted": r.get("ipRestrict")}
    if venue == "alpaca":
        a = b._call("GET", "/v2/account")
        if a.get("status") != "ACTIVE":
            raise RuntimeError(f"account status {a.get('status')}")
        return {"account": "ACTIVE", "pattern_day_trader": a.get("pattern_day_trader")}
    return {}
