"""Alpaca market data for US stocks (needs ALPACA_KEY_ID / ALPACA_SECRET_KEY; a free
paper account is enough). Free plan = IEX feed, which is thinner than the full tape."""
from __future__ import annotations

import datetime as dt
import time

from ..util import HttpError, http_json, us_eastern

DATA = "https://data.alpaca.markets"


class AlpacaData:
    def __init__(self, key: str, secret: str, trading_base: str, feed: str = "iex"):
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.trading_base = trading_base
        self.feed = feed
        self._cal: tuple[str, dict | None] | None = None
        self._daily: dict[str, tuple[float, float]] = {}

    def _get(self, url, params=None):
        return http_json(url, params, headers=self.h, timeout=20)

    # ------------------------------------------------------------ session
    def session_today(self) -> dict | None:
        """{'date', 'session_open', 'close', 'session_close'} in ET 'HH:MM', or None if no trading."""
        today = us_eastern().date().isoformat()
        if self._cal and self._cal[0] == today:
            return self._cal[1]
        rows = self._get(f"{self.trading_base}/v2/calendar", {"start": today, "end": today})
        day = None
        for r in rows or []:
            if r.get("date") == today:
                fmt = lambda s: f"{s[:2]}:{s[2:]}" if s and ":" not in s else s
                day = {"date": today, "open": fmt(r.get("open")), "close": fmt(r.get("close")),
                       "session_open": fmt(r.get("session_open")), "session_close": fmt(r.get("session_close"))}
        self._cal = (today, day)
        return day

    SESSIONS = {"regular": ("open", "close"), "after_hours": ("close", "session_close")}

    def session_window(self, kind: str = "regular") -> tuple[dt.datetime, dt.datetime] | None:
        """(start, end) of today's 'regular' session (09:30-16:00 ET, shorter on half days) or its
        'after_hours' session (16:00-20:00 ET), as ET naive datetimes; None when the market is closed."""
        start_key, end_key = self.SESSIONS[kind]
        day = self.session_today()
        if not day or not day.get(start_key) or not day.get(end_key):
            return None
        d = dt.date.fromisoformat(day["date"])
        hm = lambda s: dt.datetime.combine(d, dt.time(int(s[:2]), int(s[3:5])))
        return hm(day[start_key]), hm(day[end_key])

    def after_hours_window(self) -> tuple[dt.datetime, dt.datetime] | None:
        return self.session_window("after_hours")

    # ------------------------------------------------------------ universe
    def most_actives(self, top: int) -> list[str]:
        d = self._get(f"{DATA}/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": top})
        return [r["symbol"] for r in d.get("most_actives", [])]

    def top_gainers(self, top: int) -> list[str]:
        d = self._get(f"{DATA}/v1beta1/screener/stocks/movers", {"top": top})
        return [r["symbol"] for r in d.get("gainers", [])]

    def snapshots(self, symbols: list[str]) -> dict:
        if not symbols:
            return {}
        return self._get(f"{DATA}/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": self.feed}) or {}

    def bars(self, symbol: str, timeframe: str, days: int) -> list[dict]:
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out, token = [], None
        while True:
            p = {"timeframe": timeframe, "start": start, "limit": 1000, "feed": self.feed, "adjustment": "raw"}
            if token:
                p["page_token"] = token
            d = self._get(f"{DATA}/v2/stocks/{symbol}/bars", p)
            out += d.get("bars") or []
            token = d.get("next_page_token")
            if not token:
                return out

    def avg_daily_volume(self, symbol: str, days: int = 30) -> float:
        hit = self._daily.get(symbol)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        today = us_eastern().date().isoformat()
        bars = [b for b in self.bars(symbol, "1Day", days * 2) if not b["t"].startswith(today)][-days:]
        if len(bars) < days * 0.8:
            raise HttpError(f"only {len(bars)} daily bars")
        avg = sum(b["v"] for b in bars) / len(bars)
        self._daily[symbol] = (time.time(), avg)
        return avg
