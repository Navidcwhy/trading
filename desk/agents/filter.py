"""Agent 2 - Filter. Kills anything that fails a hard check. Fail-closed: if the data
for a check can't be fetched, the candidate dies (logged as such).

Checks, cheapest first:
  1 spread too wide or stale quote (stocks)   2 funding overheated (perps)   3 earnings today/tomorrow (stocks)
  4 volume too far under the 30-day avg (min_volume_ratio_vs_30d; stocks: against what a normal day
    has traded by this time of the session)    5 thin order book    6 open news risk
Gold/silver get the checks of the market they trade on (perp, or spot for a gold token); their news
check reads general market news (Finnhub) for the metal plus crypto news for the token and the venue.
Follow-up headlines about a known, handled event (config news_acknowledged) don't block,
but any other risk word in them still does; each excused headline is logged once.
"""
from __future__ import annotations

import datetime as dt

from ..models import Candidate, Survivor
from ..util import us_eastern


class FilterAgent:
    name = "filter"

    def __init__(self, cfg, binance, alpaca, crypto_news, finnhub, log, news_acks=None):
        self.f = cfg.section("filter")
        self.bn = binance
        self.alpaca = alpaca
        self.cnews = crypto_news
        self.fh = finnhub
        self.log = log
        self.acks = news_acks
        for e in news_acks.active() if news_acks else []:
            log.event("filter", "news_ack", f"known event, its follow-up headlines don't block "
                      f"through {e['until_str']}: {e['note']}")

    def run(self, cands: list[Candidate]) -> list[Survivor]:
        out = []
        for c in cands:
            if not isinstance(c, Candidate):
                raise TypeError("Filter only accepts Candidates from Scout")
            try:
                checks = self._check(c)
            except Exception as e:  # noqa: BLE001 - any data failure kills the candidate
                self._kill(c, f"check failed, fail-closed: {type(e).__name__}: {str(e)[:120]}")
                continue
            if checks is not None:
                out.append(Survivor(c, checks))
        for a in self.acks.take_new() if self.acks else []:
            words = "/".join(a["ignored"])
            self.log.event("filter", "news_ack", f"not blocking on '{words}', known event ({a['note']}): "
                           f"\"{a['title'][:110]}\"")
        self.log.event("filter", "summary", f"{len(out)}/{len(cands)} survived: "
                       + ", ".join(s.candidate.key for s in out))
        return out

    def _kill(self, c: Candidate, reason: str, **detail):
        self.log.kill("filter", c.symbol, c.market, reason, **detail)
        return None

    def _unavailable(self, c, what):
        if self.f.get("fail_closed", True):
            return self._kill(c, f"{what} unavailable (fail-closed)")
        return "skip"

    def _check(self, c: Candidate) -> dict | None:
        chk: dict = {}
        crypto = c.market in ("spot", "perp")

        # 1. spread
        if c.bid <= 0 or c.ask <= 0 or c.ask < c.bid:
            return self._kill(c, "no valid bid/ask")
        # A quote nobody has updated is no price at all: the free IEX feed often has none after
        # the close, so the last one can be an hour old. Fail-closed, like a missing quote.
        max_age = (self.f.get("max_quote_age_sec") or {}).get(c.market)
        if max_age:
            age = c.meta.get("quote_age_sec")
            if age is None:
                return self._kill(c, "quote time unavailable (fail-closed)")
            if age > max_age:
                return self._kill(c, f"stale quote: {age / 60:.0f} min old > {max_age / 60:g} min",
                                  quote_age_sec=round(age), max=max_age)
            chk["quote_age_sec"] = round(age)
        sp, mx = c.spread_pct, self.f["max_spread_pct"][c.market]
        if sp > mx:
            return self._kill(c, f"spread too wide: {sp:.3f}% > {mx}%", spread_pct=sp, max=mx)
        chk["spread_pct"] = sp

        # 2. funding
        if c.market == "perp":
            fr = c.meta.get("funding_rate")
            if fr is None:
                if self._unavailable(c, "funding rate") is None:
                    return None
            else:
                frp, mx = fr * 100, self.f["max_abs_funding_pct"]
                if abs(frp) > mx:
                    return self._kill(c, f"funding overheated: {frp:+.4f}%/period > +/-{mx}%",
                                      funding_pct=frp, max=mx)
                chk["funding_pct"] = frp

        # 3. earnings
        if c.market == "stock":
            today = us_eastern().date()
            horizon = today + dt.timedelta(days=self.f.get("earnings_kill_days_ahead", 1))
            cal = self.fh.earnings_dates(today, horizon)
            if cal is None:
                if self._unavailable(c, "earnings calendar (FINNHUB_API_KEY)") is None:
                    return None
            else:
                hits = [e for e in cal.get(c.symbol, []) if e[0] and str(today) <= e[0] <= str(horizon)]
                if hits:
                    return self._kill(c, f"earnings {hits[0][0]} ({hits[0][1] or 'time n/a'})", earnings=hits)

        # 4. volume vs 30-day average
        if crypto:
            avg = self.bn.avg_daily_quote_volume(c.market, c.symbol, 30)
            ratio = c.volume_24h_usd / avg if avg else 0
        else:
            frac = c.meta.get("expected_volume_frac") or 1.0
            avg = self.alpaca.avg_daily_volume(c.symbol, 30) * frac
            ratio = c.meta.get("today_volume", 0) / avg if avg else 0
        mn = self.f["min_volume_ratio_vs_30d"]
        if ratio < mn:
            when = " for this time of day" if not crypto and c.meta.get("expected_volume_frac", 1) < 1 else ""
            return self._kill(c, f"volume under 30d avg{when}: {ratio:.2f}x < {mn}x", vol_ratio=ratio)
        chk["vol_ratio"] = ratio

        # 5. order book depth
        if crypto:
            band = self.f["book_band_pct"] / 100
            d = self.bn.depth(c.market, c.symbol, 100)
            mid = c.mid
            bid_usd = sum(p * q for p, q in d["bids"] if p >= mid * (1 - band))
            ask_usd = sum(p * q for p, q in d["asks"] if p <= mid * (1 + band))
        else:
            bid_usd = c.meta.get("bid_size", 0) * c.bid
            ask_usd = c.meta.get("ask_size", 0) * c.ask
        mn = self.f["min_book_depth_usd"][c.market]
        thin = min(bid_usd, ask_usd)
        if thin < mn:
            side = "bid" if bid_usd < ask_usd else "ask"
            return self._kill(c, f"thin order book: {side} ${thin:,.0f} < ${mn:,.0f}",
                              bid_usd=bid_usd, ask_usd=ask_usd)
        chk.update(bid_depth_usd=bid_usd, ask_depth_usd=ask_usd,
                   book_imbalance=bid_usd / (bid_usd + ask_usd) if bid_usd + ask_usd else 0.5)

        # 6. open news risk
        if c.meta.get("asset_class") == "commodity":
            # gold/silver: the metal in general market news (Finnhub) + the token/venue in crypto news
            lb = self.f["news_lookback_hours"].get("commodity", 12)
            metal = self.fh.market_news_risk(c.meta.get("news_names", []), lb) if self.fh else None
            venue = self.cnews.risk_for(c.meta.get("token") or c.base, c.venue, lb)
            hits = None if metal is None or venue is None else metal + venue
        elif crypto:
            hits = self.cnews.risk_for(c.base, c.venue, self.f["news_lookback_hours"]["crypto"])
        else:
            hits = self.fh.news_risk(c.symbol, self.f["news_lookback_hours"]["stock"])
        if hits is None:
            if self._unavailable(c, "news feed") is None:
                return None
        elif hits:
            h = hits[0]
            return self._kill(c, f"open news risk ({h['scope'] if 'scope' in h else 'asset'}): "
                                 f"'{h['keyword']}' in \"{h['title'][:90]}\"", headlines=hits[:3])
        chk["news_hits"] = 0
        return chk
