"""Agent 3 - Analyst. Scores survivors 0..1 from 1h candles + filter metrics.
Score < min_score (0.6) dies. This is a transparent heuristic (trend + momentum +
participation + book quality), NOT a backtested edge - judge it on paper results."""
from __future__ import annotations

from ..models import Signal, Survivor
from ..util import atr_pct, ema, lerp_score, rsi


class Analyst:
    name = "analyst"

    def __init__(self, cfg, binance, alpaca, log):
        self.a = cfg.section("analyst")
        self.f = cfg.section("filter")
        self.bn = binance
        self.alpaca = alpaca
        self.log = log

    def run(self, survivors: list[Survivor]) -> list[Signal]:
        out = []
        for s in survivors:
            if not isinstance(s, Survivor):
                raise TypeError("Analyst only accepts Survivors from Filter")
            c = s.candidate
            try:
                feats = self._features(s)
            except Exception as e:  # noqa: BLE001
                self.log.kill("analyst", c.symbol, c.market, f"scoring failed: {type(e).__name__}: {str(e)[:100]}")
                continue
            if feats is None:
                continue
            w = self.a["weights"]
            used = {k: v for k, v in feats.items() if k in w}
            score = sum(w[k] * v for k, v in used.items()) / sum(w[k] for k in used)
            breakdown = {k: round(v, 3) for k, v in feats.items()}
            if score < self.a["min_score"]:
                self.log.kill("analyst", c.symbol, c.market,
                              f"score {score:.2f} < {self.a['min_score']:.2f}", score=score, breakdown=breakdown)
                continue
            self.log.event("analyst", "signal", f"{c.key} scored {score:.2f}", score=score, breakdown=breakdown)
            out.append(Signal(s, round(score, 4), breakdown))
        return out

    def _candles(self, c):
        if c.market in ("spot", "perp"):
            k = self.bn.klines(c.market, c.symbol, "1h", 120)
            closed = k[:-1]  # drop the forming candle
            return [x["h"] for x in closed], [x["l"] for x in closed], [x["c"] for x in closed]
        bars = self.alpaca.bars(c.symbol, "1Hour", 20)
        return [b["h"] for b in bars], [b["l"] for b in bars], [b["c"] for b in bars]

    def _features(self, s: Survivor) -> dict | None:
        c, chk = s.candidate, s.checks
        highs, lows, closes = self._candles(c)
        if len(closes) < 55:
            self.log.kill("analyst", c.symbol, c.market, f"insufficient history: {len(closes)} 1h bars < 55")
            return None
        px = c.mid
        e20, e50 = ema(closes[-100:], 20), ema(closes[-100:], 50)
        trend = 1.0 if px > e20 > e50 else 0.5 if px > e50 else 0.0

        r4 = (px / closes[-4] - 1) * 100  # ~4h return
        if r4 <= 0:
            momentum = 0.0
        elif r4 <= 3:
            momentum = r4 / 3
        elif r4 <= 8:
            momentum = 1.0
        else:
            momentum = max(0.0, 1 - (r4 - 8) / 8)  # overextended

        r = rsi(closes[-100:] + [px], 14)
        if r < 50:
            rsi_s = lerp_score(r, 45, 50)
        elif r <= 68:
            rsi_s = 1.0
        else:
            rsi_s = lerp_score(r, 78, 68)

        a = atr_pct(highs[-60:], lows[-60:], closes[-60:], 14)
        feats = {
            "trend": trend,
            "momentum": momentum,
            "rsi": rsi_s,
            "rel_volume": lerp_score(chk.get("vol_ratio", 0), 1.0, 2.0),
            "volatility_fit": lerp_score(a, 4.0, 1.5),  # 1h ATR vs an 8% stop
            "spread": max(0.0, 1 - chk["spread_pct"] / self.f["max_spread_pct"][c.market]),
            "book_imbalance": lerp_score(chk.get("book_imbalance", 0.5), 0.4, 0.6),
        }
        if c.market == "perp" and "funding_pct" in chk:
            feats["funding"] = lerp_score(chk["funding_pct"], self.f["max_abs_funding_pct"], 0.0)
        feats["_raw_r4_pct"] = round(r4, 2)
        feats["_raw_rsi"] = round(r, 1)
        feats["_raw_atr_pct"] = round(a, 2)
        return feats
