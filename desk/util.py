"""Shared helpers: HTTP with retries, time zones, indicators, rounding."""
from __future__ import annotations

import datetime as dt
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "desk/0.1 (personal paper-trading bot)"
UTC = dt.timezone.utc


class HttpError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _request(url, params=None, headers=None, method="GET", data=None, timeout=15, retries=3):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")[:300]
            # Never echo the URL: signed requests carry signatures / tokens.
            if e.code in (418, 429) or e.code >= 500:
                wait = int(e.headers.get("Retry-After") or (2 ** attempt) * 2)
                last = HttpError(f"HTTP {e.code}: {text}", e.code)
                time.sleep(min(wait, 60))
                continue
            raise HttpError(f"HTTP {e.code}: {text}", e.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = HttpError(f"network: {e}")
            time.sleep(2 ** attempt)
    raise last or HttpError("request failed")


def http_json(url, params=None, headers=None, method="GET", body=None, form=None, timeout=15, retries=3):
    data = None
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = form.encode() if isinstance(form, str) else urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    raw = _request(url, params, headers, method, data, timeout, retries)
    return json.loads(raw) if raw else None


def http_text(url, timeout=15, retries=2) -> str:
    return _request(url, timeout=timeout, retries=retries).decode("utf-8", errors="replace")


# ---------------------------------------------------------------- time

def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def age_sec(ts: str | None, now: dt.datetime | None = None) -> float | None:
    """Seconds since a UTC RFC 3339 timestamp such as Alpaca's '2026-11-18T21:01:00.287544391Z'
    (whole seconds are enough), or None if it is missing or unreadable."""
    try:
        t = dt.datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return max(0.0, ((now or now_utc()) - t).total_seconds())


# Share of a normal US regular session's volume that has traded, every 30 minutes from the 09:30 open
# to the 16:00 close: busy open, quiet lunch, heavy close (the closing auction is the last jump).
# A rough market-wide shape, not per stock; good enough to judge "is volume normal for this hour".
US_VOLUME_CURVE = (0.0, 0.13, 0.21, 0.28, 0.34, 0.40, 0.45, 0.50, 0.55, 0.60, 0.66, 0.73, 0.82, 1.0)


def expected_volume_fraction(minutes_since_open: float, session_minutes: float = 390) -> float:
    """Share of a full session's volume a normal day has traded `minutes_since_open` into the session
    (a half day is squeezed onto the same curve). Never below 0.02, so the first minutes don't divide
    by almost nothing."""
    curve = US_VOLUME_CURVE
    x = clamp(minutes_since_open / session_minutes if session_minutes > 0 else 1.0) * (len(curve) - 1)
    i = min(int(x), len(curve) - 2)
    return max(0.02, curve[i] + (x - i) * (curve[i + 1] - curve[i]))


def us_eastern(now: dt.datetime | None = None) -> dt.datetime:
    """Current US/Eastern wall time (naive). Implements the US DST rule directly so
    we don't depend on the tzdata package, which Windows Python lacks."""
    now = now or now_utc()
    y = now.year
    mar8 = dt.datetime(y, 3, 8, tzinfo=UTC)
    dst_start = (mar8 + dt.timedelta(days=(6 - mar8.weekday()) % 7)).replace(hour=7)
    nov1 = dt.datetime(y, 11, 1, tzinfo=UTC)
    dst_end = (nov1 + dt.timedelta(days=(6 - nov1.weekday()) % 7)).replace(hour=6)
    offset = -4 if dst_start <= now < dst_end else -5
    return (now + dt.timedelta(hours=offset)).replace(tzinfo=None)


# ---------------------------------------------------------------- math

def floor_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    n = math.floor(x / step + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, decimals)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def lerp_score(x: float, zero_at: float, one_at: float) -> float:
    """Linear 0..1 score: 0 at `zero_at`, 1 at `one_at` (either direction)."""
    if one_at == zero_at:
        return 1.0 if x >= one_at else 0.0
    return clamp((x - zero_at) / (one_at - zero_at))


def ema(values: list[float], n: int) -> float:
    if not values:
        return float("nan")
    k = 2 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: list[float], n: int = 14) -> float:
    if len(closes) < n + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr_pct(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float:
    if len(closes) < n + 1:
        return float("nan")
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a / closes[-1] * 100
