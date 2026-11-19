"""Agent 1 - Scout. Watches BTC, ETH, top alts (spot + USD-M perps) and gold/silver 24/7, and US
stocks and ETFs during the US regular session (or after-hours, if configured).
Gold/silver are Binance TradFi perps; where perps are off, gold is a gold token on Binance spot
(XAUT/PAXG, one token = one troy ounce). The stock watchlist (Nasdaq-100, gold, silver and Sweden
ETFs) is scouted every cycle of the session, next to the most active stocks and top gainers.
Emits Candidates; decides nothing."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..models import Candidate
from ..sources.binance import BinanceData, underlying
from ..util import HttpError, age_sec, expected_volume_fraction, us_eastern

LEVERAGED = re.compile(r"(UP|DOWN|BULL|BEAR)$")
TOKENIZED_RETRY_SEC = 600
SESSION_LABEL = {"regular": "regular session", "after_hours": "after-hours session"}


class Scout:
    name = "scout"

    def __init__(self, cfg, binance: BinanceData, alpaca, log, tokenized_cache: Path | None = None):
        self.u = cfg.section("universe")
        self.bn = binance
        self.alpaca = alpaca
        self.log = log
        self._state: dict[str, str] = {}
        self._tok_cache = tokenized_cache or (cfg.data_dir / "tokenized_stocks.json")
        self._tok_saved: set[str] | None = None
        self._tok_retry_at = 0.0
        self._perp_cache = None
        self._spot_cache = None

    def scan(self) -> list[Candidate]:
        self._perp_cache = self._spot_cache = None  # fetch market data at most once per scan
        out: list[Candidate] = []
        if self.u.get("include_spot") or self.u.get("include_perps"):
            out += self._crypto()
        if self.u.get("commodities"):
            out += self._commodities()
        if self.u.get("stocks_enabled"):
            out += self._stocks()
        by = {}
        for c in out:
            k = c.meta.get("asset_class") or c.market
            by[k] = by.get(k, 0) + 1
        self.log.event("scout", "scan", f"{len(out)} candidates " + str(by))
        return out

    def _status(self, topic: str, state: str, msg: str, level: str = "info"):
        """Log a state message only when it changes, not every cycle."""
        if self._state.get(topic) != state:
            self._state[topic] = state
            self.log.event("scout", topic, msg, level=level)

    # ------------------------------------------------------------------ crypto
    def _tokenized_stocks(self) -> set[str] | None:
        """Binance tokenized stocks/ETFs (bStocks) are kept out of the crypto universe: as
        'crypto' they would skip the stock rules (earnings, stock news). If Binance's list is
        down, use the last saved copy; with neither, return None (caller goes fail-closed)."""
        if time.time() >= self._tok_retry_at:
            try:
                tok = self.bn.tokenized_stock_bases()
                if tok != self._tok_saved:
                    self._tok_cache.parent.mkdir(parents=True, exist_ok=True)
                    self._tok_cache.write_text(json.dumps(sorted(tok)), encoding="utf-8")
                    self._tok_saved = tok
                self._status("tokenized", "live", f"tokenized-stock list loaded ({len(tok)} assets excluded)")
                return tok
            except Exception as e:  # noqa: BLE001
                self._tok_retry_at = time.time() + TOKENIZED_RETRY_SEC
                err = f"{type(e).__name__}: {str(e)[:120]}"
        else:
            err = "retry pending"
        try:
            tok = set(json.loads(self._tok_cache.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            self._status("tokenized", "none", f"tokenized-stock list unavailable ({err}) and no saved "
                         "copy: scouting BTC/ETH only (fail-closed)", level="warn")
            return None
        self._status("tokenized", "cached", f"tokenized-stock list unavailable ({err}); using saved "
                     f"copy ({len(tok)} assets)", level="warn")
        return tok

    def _pick_bases(self, info: dict, tickers: dict, tokenized: set[str] = frozenset()) -> list[str]:
        excl = set(self.u.get("crypto_exclude", []))
        bases = list(self.u.get("crypto_core", ["BTC", "ETH"]))
        ranked = sorted((t for s, t in tickers.items() if s in info),
                        key=lambda t: float(t["quoteVolume"]), reverse=True)
        skipped_tok = []
        for t in ranked:
            if len(bases) - len(self.u.get("crypto_core", [])) >= self.u.get("crypto_top_alts", 15):
                break
            b = info[t["symbol"]]["base"]
            last, hi, lo = float(t["lastPrice"]), float(t["highPrice"]), float(t["lowPrice"])
            stable_like = last > 0 and abs(last - 1) < 0.02 and (hi - lo) / last < 0.01
            if b in tokenized:
                skipped_tok.append(b)
                continue
            if b in excl or b in bases or LEVERAGED.search(b) or stable_like:
                continue
            bases.append(b)
        if skipped_tok:
            self._status("tokenized_skipped", ",".join(sorted(skipped_tok)),
                         "tokenized stocks kept out of top alts: " + ", ".join(skipped_tok))
        return bases

    def _spot_data(self):
        """(exchange info, 24h tickers, book tickers) for all spot pairs, once per scan."""
        if self._spot_cache is None:
            self._spot_cache = (self.bn.spot_info(), self.bn.tickers_24h("spot"), self.bn.books("spot"))
        return self._spot_cache

    def _crypto(self) -> list[Candidate]:
        out = []
        sinfo, stick, books = self._spot_data()
        tokenized = self._tokenized_stocks()
        if tokenized is None:
            bases = [b for b in self.u.get("crypto_core", ["BTC", "ETH"]) if f"{b}USDT" in sinfo]
        else:
            bases = self._pick_bases(sinfo, stick, tokenized)
        if self.u.get("include_spot"):
            for b in bases:
                sym = f"{b}USDT"
                if sym not in sinfo or sym not in stick or sym not in books:
                    continue
                t, (bid, ask) = stick[sym], books[sym]
                out.append(Candidate(
                    symbol=sym, base=b, market="spot", venue="binance_spot", bid=bid, ask=ask,
                    last=float(t["lastPrice"]), volume_24h_usd=float(t["quoteVolume"]),
                    meta={**sinfo[sym], "change_24h_pct": float(t["priceChangePercent"])}))
        if self.u.get("include_perps"):
            finfo, ftick, fbooks, prem = self._perp_data()
            by_under = {}
            for sym, i in finfo.items():
                # crypto perps only here: TradFi perps (stocks, gold...) never stand in for an alt
                if i.get("contract_type") == "PERPETUAL" and i.get("underlying_type") == "COIN":
                    by_under.setdefault(underlying(i["base"]), sym)
            for b in bases:
                sym = by_under.get(b)
                c = sym and self._perp_candidate(sym, b, finfo[sym], ftick, fbooks, prem)
                if c:
                    out.append(c)
        return out

    def _perp_data(self):
        if self._perp_cache is None:
            self._perp_cache = (self.bn.fut_info(), self.bn.tickers_24h("perp"),
                                self.bn.books("perp"), self.bn.premium_index())
        return self._perp_cache

    @staticmethod
    def _perp_candidate(sym, base, info, ftick, fbooks, prem, **extra) -> Candidate | None:
        if sym not in ftick or sym not in fbooks:
            return None
        t, (bid, ask) = ftick[sym], fbooks[sym]
        p = prem.get(sym, {})
        return Candidate(
            symbol=sym, base=base, market="perp", venue="binance_futures", bid=bid, ask=ask,
            last=float(t["lastPrice"]), volume_24h_usd=float(t["quoteVolume"]),
            meta={**info, "funding_rate": p.get("funding_rate"), "mark": p.get("mark"),
                  "next_funding_ms": p.get("next_funding_ms"),
                  "change_24h_pct": float(t["priceChangePercent"]), **extra})

    # ------------------------------------------------------------------ commodities
    def _commodities(self) -> list[Candidate]:
        """Gold, silver etc. 24/7: a Binance USDT-margined TradFi perpetual where perps are on, else a
        token on Binance spot that holds the metal (universe.commodity_spot_tokens, e.g. XAUT/PAXG
        for gold). Either way the candidate's base is the metal, so the desk never holds gold twice,
        and Filter checks general market news for the metal instead of crypto news for the coin."""
        out = []
        use_perps = self.u.get("include_perps")      # off where Binance futures are geo-blocked
        tokens = self.u.get("commodity_spot_tokens") or {}
        for base, names in self.u["commodities"].items():
            c = how = None
            if use_perps:
                finfo, ftick, fbooks, prem = self._perp_data()
                sym = f"{base}USDT"
                info = finfo.get(sym, {})
                if info.get("underlying_type") == "COMMODITY":
                    c = self._perp_candidate(sym, base, info, ftick, fbooks, prem,
                                             asset_class="commodity", news_names=list(names))
                    how = f"{sym} (commodity perp, 24/7)"
            if c is None and tokens.get(base):
                sinfo, stick, books = self._spot_data()
                for tok in tokens[base]:
                    sym = f"{tok}USDT"
                    if sym in sinfo and sym in stick and sym in books:
                        t, (bid, ask) = stick[sym], books[sym]
                        c = Candidate(
                            symbol=sym, base=base, market="spot", venue="binance_spot", bid=bid, ask=ask,
                            last=float(t["lastPrice"]), volume_24h_usd=float(t["quoteVolume"]),
                            meta={**sinfo[sym], "change_24h_pct": float(t["priceChangePercent"]),
                                  "asset_class": "commodity", "news_names": list(names), "token": tok})
                        how = f"{sym} ({tok} token = 1 oz of {base}, spot, 24/7)"
                        break
            if c is None:
                why = "no commodity perp" + (" or token" if tokens.get(base) else "") + " on Binance"
                if not use_perps:
                    why = "perps are off and " + ("no token is listed" if tokens.get(base) else "it has no spot token")
                self._status(f"commodity:{base}", "missing", f"{base}: not scouted 24/7 ({why})", level="warn")
                continue
            self._status(f"commodity:{base}", how, f"{base}: scouting {how}")
            out.append(c)
        return out

    # ------------------------------------------------------------------ stocks
    def _stock_status(self, state: str, msg: str):
        self._status("stocks", state, msg)

    def _stocks(self) -> list[Candidate]:
        if not self.alpaca:
            self._stock_status("nokeys", "US stocks off: no ALPACA_KEY_ID/ALPACA_SECRET_KEY in .env")
            return []
        session = self.u.get("stocks_session", "regular")
        label = SESSION_LABEL[session]
        try:
            window = self.alpaca.session_window(session)
        except HttpError as e:
            self._stock_status("err", f"US stocks: calendar unavailable ({e})")
            return []
        now = us_eastern()
        if not window or not (window[0] <= now < window[1]):
            self._stock_status("closed", f"US stocks: outside the {label}, scouting crypto only")
            return []
        self._stock_status("open", f"US stocks: {label} open until {window[1]:%H:%M} ET")
        watch = self.u.get("stocks_watchlist") or {}
        syms = list(dict.fromkeys(list(watch)
                                  + self.alpaca.most_actives(self.u.get("stocks_most_actives", 20))
                                  + self.alpaca.top_gainers(self.u.get("stocks_top_gainers", 10))))
        snaps = self.alpaca.snapshots(syms)
        # today's volume is compared with what a normal day has traded by this time of the session
        frac = 1.0
        if session == "regular":
            frac = expected_volume_fraction((now - window[0]).total_seconds() / 60,
                                            (window[1] - window[0]).total_seconds() / 60)
        today = now.date().isoformat()
        out = []
        for sym, s in snaps.items():
            q, t, d = s.get("latestQuote") or {}, s.get("latestTrade") or {}, s.get("dailyBar") or {}
            last = float(t.get("p") or 0)
            if last < self.u.get("stocks_min_price", 2.0):
                self.log.kill("scout", sym, "stock", f"price ${last:.2f} below universe minimum")
                continue
            # before today's first trade the snapshot still shows yesterday's bar
            vol = float(d.get("v") or 0) if str(d.get("t") or "").startswith(today) else 0.0
            meta = {"bid_size": float(q.get("bs") or 0), "ask_size": float(q.get("as") or 0),
                    "quote_age_sec": age_sec(q.get("t")),
                    "today_volume": vol, "expected_volume_frac": round(frac, 4),
                    "step": 0.0001, "min_qty": 0.0001, "min_notional": 1.0, "session": session,
                    "session_end_et": window[1].isoformat()}
            if sym in watch:
                meta["watchlist"] = True
            out.append(Candidate(
                symbol=sym, base=watch.get(sym) or sym, market="stock", venue="alpaca",
                bid=float(q.get("bp") or 0), ask=float(q.get("ap") or 0), last=last,
                volume_24h_usd=vol * last, meta=meta))
        return out
