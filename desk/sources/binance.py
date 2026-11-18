"""Binance public market data (no keys): spot + USD-M perpetuals."""
from __future__ import annotations

import re
import time

from ..util import http_json

SPOT = "https://api.binance.com"
FUT = "https://fapi.binance.com"
# Binance's public asset list. exchangeInfo has no marker for tokenized stocks/ETFs
# (e.g. MUB = Micron, SOXLB = 3x semiconductor ETF), but this list tags them.
ASSET_LIST = "https://www.binance.com/bapi/asset/v2/public/asset/asset/get-all-asset"
TOKENIZED_STOCK_TAGS = {"bStocks"}


def underlying(base: str) -> str:
    """1000PEPE -> PEPE, 1000000MOG -> MOG (futures multiplier prefixes)."""
    return re.sub(r"^1(000)+", "", base)


def _parse_filters(filters: list[dict]) -> dict:
    out = {"step": 0.0, "min_qty": 0.0, "min_notional": 0.0, "tick": 0.0}
    for f in filters:
        t = f["filterType"]
        if t == "LOT_SIZE":
            out["step"], out["min_qty"] = float(f["stepSize"]), float(f["minQty"])
        elif t == "MARKET_LOT_SIZE" and float(f.get("stepSize", 0)) > 0:
            out["market_step"], out["market_min_qty"] = float(f["stepSize"]), float(f["minQty"])
        elif t == "NOTIONAL":
            out["min_notional"] = float(f.get("minNotional", 0))
        elif t == "MIN_NOTIONAL":
            out["min_notional"] = float(f.get("minNotional", f.get("notional", 0)))
        elif t == "PRICE_FILTER":
            out["tick"] = float(f["tickSize"])
    return out


class BinanceData:
    def __init__(self):
        self._cache: dict[str, tuple[float, object]] = {}

    def _cached(self, key, ttl, fn):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (time.time(), val)
        return val

    @staticmethod
    def base_url(market: str) -> str:
        return FUT if market == "perp" else SPOT

    # ------------------------------------------------------------ metadata
    def spot_info(self) -> dict:
        def load():
            data = http_json(f"{SPOT}/api/v3/exchangeInfo", {"permissions": "SPOT"}, timeout=30)
            out = {}
            for s in data["symbols"]:
                if s["status"] != "TRADING" or s["quoteAsset"] != "USDT":
                    continue
                out[s["symbol"]] = {"base": s["baseAsset"], "order_types": s.get("orderTypes", []),
                                    **_parse_filters(s["filters"])}
            return out
        return self._cached("spot_info", 3600, load)

    def fut_info(self) -> dict:
        """All tradable USDT perpetuals, crypto and TradFi (gold, silver, stocks...), tagged with
        contract_type and underlying_type (COIN, COMMODITY, EQUITY, INDEX...). Scout picks."""
        def load():
            data = http_json(f"{FUT}/fapi/v1/exchangeInfo", timeout=30)
            out = {}
            for s in data["symbols"]:
                if (s.get("contractType") not in ("PERPETUAL", "TRADIFI_PERPETUAL")
                        or s["status"] != "TRADING" or s["quoteAsset"] != "USDT"):
                    continue
                out[s["symbol"]] = {"base": s["baseAsset"], "order_types": s.get("orderTypes", []),
                                    "contract_type": s["contractType"],
                                    "underlying_type": s.get("underlyingType", "COIN"),
                                    **_parse_filters(s["filters"])}
            return out
        return self._cached("fut_info", 3600, load)

    def tokenized_stock_bases(self) -> set[str]:
        """Spot base assets Binance tags as tokenized stocks/ETFs. Raises if the list is unavailable."""
        def load():
            data = (http_json(ASSET_LIST, timeout=15, retries=2) or {}).get("data")
            if not data:
                raise ValueError("empty asset list")
            return {a["assetCode"] for a in data if TOKENIZED_STOCK_TAGS & set(a.get("tags") or [])}
        return self._cached("tokenized_stocks", 6 * 3600, load)

    # ------------------------------------------------------------ snapshots
    def tickers_24h(self, market: str) -> dict:
        path = "/fapi/v1/ticker/24hr" if market == "perp" else "/api/v3/ticker/24hr"
        rows = http_json(self.base_url(market) + path, timeout=30)
        return {r["symbol"]: r for r in rows}

    def books(self, market: str, symbol: str | None = None) -> dict:
        path = "/fapi/v1/ticker/bookTicker" if market == "perp" else "/api/v3/ticker/bookTicker"
        params = {"symbol": symbol} if symbol else None
        rows = http_json(self.base_url(market) + path, params)
        rows = rows if isinstance(rows, list) else [rows]
        return {r["symbol"]: (float(r["bidPrice"]), float(r["askPrice"])) for r in rows}

    def premium_index(self, symbol: str | None = None) -> dict:
        rows = http_json(f"{FUT}/fapi/v1/premiumIndex", {"symbol": symbol} if symbol else None)
        rows = rows if isinstance(rows, list) else [rows]
        return {r["symbol"]: {"funding_rate": float(r["lastFundingRate"] or 0),
                              "mark": float(r["markPrice"]),
                              "next_funding_ms": int(r["nextFundingTime"] or 0)} for r in rows}

    # ------------------------------------------------------------ per-symbol
    def depth(self, market: str, symbol: str, limit: int = 100) -> dict:
        path = "/fapi/v1/depth" if market == "perp" else "/api/v3/depth"
        d = http_json(self.base_url(market) + path, {"symbol": symbol, "limit": limit})
        return {"bids": [(float(p), float(q)) for p, q in d["bids"]],
                "asks": [(float(p), float(q)) for p, q in d["asks"]]}

    def klines(self, market: str, symbol: str, interval: str, limit: int) -> list[dict]:
        path = "/fapi/v1/klines" if market == "perp" else "/api/v3/klines"
        rows = http_json(self.base_url(market) + path, {"symbol": symbol, "interval": interval, "limit": limit})
        return [{"t": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                 "v": float(r[5]), "qv": float(r[7]), "close_t": r[6]} for r in rows]

    def avg_daily_quote_volume(self, market: str, symbol: str, days: int = 30) -> float:
        def load():
            rows = self.klines(market, symbol, "1d", days + 1)
            closed = [r for r in rows if r["close_t"] < time.time() * 1000][-days:]
            if len(closed) < days * 0.8:
                raise ValueError(f"only {len(closed)} days of history")
            return sum(r["qv"] for r in closed) / len(closed)
        return self._cached(f"advq:{market}:{symbol}", 1800, load)

    def quotes(self, market: str, symbols: list[str]) -> dict:
        if len(symbols) <= 5:
            out = {}
            for s in symbols:
                out.update(self.books(market, s))
            return out
        allq = self.books(market)
        return {s: allq[s] for s in symbols if s in allq}
