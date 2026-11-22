"""Offline tests for the handoff chain and risk rules (no network).
Run: python -m unittest discover -s tests -v"""
import dataclasses
import datetime as dt
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from desk.agents.executor import Executor, leverage_for
from desk.agents.filter import FilterAgent
from desk.agents.logger import LoggerAgent
from desk.agents.risk import Risk, rules_summary
from desk.agents.scout import Scout
from desk.brokers.paper import Brokers, PaperBroker
from desk.config import ROOT, Config, _merge, load_config, profile_names
from desk.desk import InstanceLock, apply_machine_overrides
from desk.sources.binance import BinanceData
from desk.__main__ import desk_stats, split_profile
from desk.models import Candidate, Signal, Survivor
from desk.sources.news import CryptoNews, Finnhub, NewsAck, company_names
from desk.store import Store
from desk.util import HttpError, age_sec, expected_volume_fraction, us_eastern

CFG = Config(json.loads((ROOT / "config.json").read_text(encoding="utf-8")))


def cand(symbol="SOLUSDT", base="SOL", market="spot", bid=99.99, ask=100.0, **meta):
    m = {"step": 0.001, "min_qty": 0.001, "min_notional": 5.0, "base": base}
    m.update(meta)
    return Candidate(symbol=symbol, base=base, market=market, venue="binance_spot" if market == "spot"
                     else "binance_futures", bid=bid, ask=ask, last=ask, volume_24h_usd=1e9, meta=m)


def signal(c, score=0.75):
    return Signal(Survivor(c, {"spread_pct": c.spread_pct}), score, {})


class _Base(unittest.TestCase):
    """Executor + Risk on a paper book in a temp folder. Subclasses pick the desk."""
    START = 50.0     # start equity
    RISK = {}        # overrides for the risk section
    PROFILE = ""     # "" = the main desk

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp / "t.db")
        self.log = LoggerAgent(self.store, self.tmp, "paper", console=False)
        self.px = {}  # symbol -> (bid, ask)
        cfg = Config(json.loads(json.dumps(CFG)))
        cfg["start_equity_usd"] = self.START
        cfg["risk"]["halt_below_equity_usd"] = 40.0
        cfg["risk"].update(self.RISK)
        cfg["profile"] = self.PROFILE
        self.cfg = cfg
        self.paper = PaperBroker(cfg, self.store, lambda m, s: self.px[s], lambda s: None)
        self.brokers = Brokers({v: self.paper for v in ("binance_spot", "binance_futures", "alpaca")}, self.store)
        self.risk = Risk(cfg, self.store, self.brokers, lambda ps: {p.symbol: self.px[p.symbol] for p in ps},
                         None, self.log, self.tmp)
        self.ex = Executor(cfg, self.brokers, self.store, self.log)
        self.ex.risk = self.risk

    def kills(self, stage=None):
        rows = self.store.q("SELECT * FROM kills")
        return [r for r in rows if stage is None or r["stage"] == stage]

    def enter(self, c):
        self.px[c.symbol] = (c.bid, c.ask)
        t = self.ex.propose(signal(c))
        self.assertIsNotNone(t)
        a = self.risk.approve(t)
        self.assertIsNotNone(a)
        self.assertAlmostEqual(a.stop_price, t.ref_price * (1 - self.cfg["risk"]["stop_loss_pct"] / 100))
        return self.ex.execute(a)


class DeskTest(_Base):
    """The main desk's rules in the $50 scenario."""

    def test_sizing_is_10pct_and_stop_is_minus_8(self):
        pid = self.enter(cand())
        p = self.store.open_positions()[0]
        self.assertEqual(p.id, pid)
        self.assertAlmostEqual(p.qty, 0.05)                      # $5 of $50 at $100
        self.assertAlmostEqual(p.stop_price, p.entry_price * 0.92)

    def test_stop_loss_exit(self):
        self.enter(cand())
        p = self.store.open_positions()[0]
        self.px["SOLUSDT"] = (p.entry_price * 0.925, p.entry_price * 0.926)
        self.risk.monitor()
        self.assertEqual(len(self.store.open_positions()), 1)    # -7.5%: still open
        self.px["SOLUSDT"] = (p.entry_price * 0.919, p.entry_price * 0.92)
        self.risk.monitor()
        self.assertEqual(len(self.store.open_positions()), 0)
        c = self.store.closed_positions()[0]
        self.assertTrue(c["exit_reason"].startswith("stop -8"))
        self.assertLess(c["pnl"], 0)

    def test_trailing_after_plus_15(self):
        self.enter(cand())
        e = self.store.open_positions()[0].entry_price
        for mult in (1.10, 1.14):
            self.px["SOLUSDT"] = (e * mult, e * mult)
            self.risk.monitor()
        p = self.store.open_positions()[0]
        self.assertFalse(p.trail_active)
        self.assertAlmostEqual(p.stop_price, e * 0.92)            # untouched below +15%
        self.px["SOLUSDT"] = (e * 1.16, e * 1.16)
        self.risk.monitor()
        p = self.store.open_positions()[0]
        self.assertTrue(p.trail_active)
        self.assertAlmostEqual(p.stop_price, e * 1.16 * 0.92)
        self.px["SOLUSDT"] = (e * 1.25, e * 1.25)
        self.risk.monitor()
        self.px["SOLUSDT"] = (e * 1.20, e * 1.20)                 # pullback: stop must not drop
        self.risk.monitor()
        p = self.store.open_positions()[0]
        self.assertAlmostEqual(p.stop_price, e * 1.25 * 0.92)
        self.px["SOLUSDT"] = (e * 1.14, e * 1.14)                 # below 1.25*0.92 = 1.15
        self.risk.monitor()
        c = self.store.closed_positions()[0]
        self.assertEqual(c["exit_reason"], "trailing stop")
        self.assertGreater(c["pnl"], 0)

    def test_executor_refuses_unsealed_order(self):
        c = cand()
        self.px[c.symbol] = (c.bid, c.ask)
        a = self.risk.approve(self.ex.propose(signal(c)))
        forged = dataclasses.replace(a, stop_price=a.stop_price * 0.5)  # loosen the stop
        self.assertIsNone(self.ex.execute(forged))
        self.assertEqual(self.store.open_positions(), [])

    def test_below_exchange_minimum_is_killed(self):
        self.store.set("paper_cash", 49.0)                        # 10% = $4.90 < $5 min notional
        self.assertIsNone(self.ex.propose(signal(cand())))
        k = self.kills("executor")
        self.assertEqual(len(k), 1)
        self.assertIn("below exchange minimum", k[0]["reason"])

    def test_fees_from_first_trade_block_second_trade_at_50_usd(self):
        # Real constraint: after one $5 entry, fees push equity under $50, so 10% < $5 minimum.
        self.enter(cand())
        other = cand(symbol="XRPUSDT", base="XRP")
        self.px[other.symbol] = (other.bid, other.ask)
        self.assertIsNone(self.ex.propose(signal(other)))
        self.assertIn("below exchange minimum", self.kills("executor")[0]["reason"])

    def test_one_position_per_asset(self):
        self.store.set("paper_cash", 100.0)
        self.enter(cand())
        perp = cand(symbol="SOLUSDT_PERP", market="perp")
        self.px[perp.symbol] = (perp.bid, perp.ask)
        self.assertIsNone(self.risk.approve(self.ex.propose(signal(perp))))
        self.assertIn("already exposed to SOL", self.kills("risk")[0]["reason"])

    def test_stop_file_blocks_entries(self):
        (self.tmp / "STOP").write_text("")
        c = cand()
        self.px[c.symbol] = (c.bid, c.ask)
        self.assertIsNone(self.risk.approve(self.ex.propose(signal(c))))
        self.assertIn("STOP file", self.kills("risk")[0]["reason"])

    def test_filter_fails_closed_without_news(self):
        class FakeBn:
            def avg_daily_quote_volume(self, *a): return 5e8
            def depth(self, *a): return {"bids": [(99.9, 1e5)], "asks": [(100.1, 1e5)]}

        class NoNews:
            def risk_for(self, *a): return None

        f = FilterAgent(CFG, FakeBn(), None, NoNews(), None, self.log)
        self.assertEqual(f.run([cand()]), [])
        self.assertIn("news feed unavailable", self.kills("filter")[0]["reason"])

    def test_filter_kills_each_rule(self):
        class FakeBn:
            def __init__(s, avg=5e8, depth=1e5): s.avg, s.d = avg, depth
            def avg_daily_quote_volume(s, *a): return s.avg
            def depth(s, *a): return {"bids": [(99.9, s.d)], "asks": [(100.1, s.d)]}

        class News:
            def __init__(s, hits): s.hits = hits
            def risk_for(s, *a): return s.hits

        cases = [
            (cand(bid=99.0, ask=100.0), FakeBn(), News([]), "spread too wide"),
            (cand(market="perp", funding_rate=0.001), FakeBn(), News([]), "funding overheated"),
            (cand(), FakeBn(avg=2e9), News([]), "volume under 30d avg"),
            (cand(), FakeBn(depth=10), News([]), "thin order book"),
            (cand(), FakeBn(), News([{"keyword": "hack", "title": "Solana bridge hacked", "scope": "asset"}]),
             "open news risk"),
        ]
        for c, bn, news, expect in cases:
            f = FilterAgent(CFG, bn, None, news, None, self.log)
            self.assertEqual(f.run([c]), [], expect)
            self.assertIn(expect, self.kills("filter")[-1]["reason"])
        ok = FilterAgent(CFG, FakeBn(), None, News([]), None, self.log).run([cand()])
        self.assertEqual(len(ok), 1)

    def test_filter_kills_stale_or_undated_stock_quotes(self):
        f = FilterAgent(CFG, None, None, None, None, self.log)
        stock = lambda **m: cand(symbol="AES", base="AES", market="stock", bid=14.99, ask=15.03, **m)
        self.assertEqual(f.run([stock(quote_age_sec=90 * 60)]), [])
        self.assertIn("stale quote: 90 min old > 5 min", self.kills("filter")[-1]["reason"])
        self.assertEqual(f.run([stock()]), [])
        self.assertIn("quote time unavailable (fail-closed)", self.kills("filter")[-1]["reason"])
        # a fresh quote gets past the check (and dies later here, without earnings data)
        f.run([stock(quote_age_sec=20)])
        self.assertNotIn("quote", self.kills("filter")[-1]["reason"])

    def test_quote_age_from_alpaca_timestamps(self):
        now = dt.datetime(2026, 11, 18, 22, 31, tzinfo=dt.timezone.utc)
        self.assertEqual(age_sec("2026-11-18T21:01:00.287544391Z", now), 90 * 60)
        self.assertEqual(age_sec("2026-11-18T22:31:30Z", now), 0.0)
        for bad in (None, "", "garbage"):
            self.assertIsNone(age_sec(bad, now))

    def test_scout_keeps_tokenized_stocks_out(self):
        vols = {"BTC": 9e9, "ETH": 8e9, "MUB": 7e9, "SOL": 6e9, "XRP": 5e9, "ADA": 4e9}

        class FakeBn:
            def __init__(s, fail=False): s.fail = fail
            def spot_info(s): return {f"{b}USDT": {"base": b} for b in vols}
            def tickers_24h(s, m):
                return {f"{b}USDT": {"symbol": f"{b}USDT", "quoteVolume": v, "lastPrice": 100,
                                     "highPrice": 105, "lowPrice": 95, "priceChangePercent": 1}
                        for b, v in vols.items()}
            def books(s, m, sym=None): return {f"{b}USDT": (99.99, 100.0) for b in vols}
            def tokenized_stock_bases(s):
                if s.fail:
                    raise ConnectionError("asset list down")
                return {"MUB", "TSLAB"}

        cfg = Config(json.loads(json.dumps(CFG)))
        cfg["universe"].update(include_perps=False, stocks_enabled=False, crypto_top_alts=2, commodities={})
        cache = self.tmp / "tokenized.json"
        bases = lambda scout: [c.base for c in scout.scan()]

        # live list: MUB (Micron bStock) is skipped, the next real alts fill its slot
        self.assertEqual(bases(Scout(cfg, FakeBn(), None, self.log, cache)), ["BTC", "ETH", "SOL", "XRP"])
        # list down: the saved copy still keeps MUB out
        self.assertEqual(bases(Scout(cfg, FakeBn(fail=True), None, self.log, cache)), ["BTC", "ETH", "SOL", "XRP"])
        # list down and no saved copy: fail-closed to BTC/ETH only
        cache.unlink()
        self.assertEqual(bases(Scout(cfg, FakeBn(fail=True), None, self.log, cache)), ["BTC", "ETH"])
        warns = [e["message"] for e in self.store.q("SELECT * FROM events WHERE level = 'warn'")]
        self.assertTrue(any("fail-closed" in m for m in warns))

    def test_scout_adds_gold_and_silver_perps(self):
        perps = {"BTCUSDT": ("BTC", "PERPETUAL", "COIN"),
                 "XAUUSDT": ("XAU", "TRADIFI_PERPETUAL", "COMMODITY"),
                 "XAGUSDT": ("XAG", "TRADIFI_PERPETUAL", "COMMODITY"),
                 "TSLAUSDT": ("TSLA", "TRADIFI_PERPETUAL", "EQUITY")}

        class FakeBn:
            def spot_info(s): return {"BTCUSDT": {"base": "BTC"}, "ETHUSDT": {"base": "ETH"}}
            def tickers_24h(s, m):
                syms = ("BTCUSDT", "ETHUSDT") if m == "spot" else tuple(perps)
                return {x: {"symbol": x, "quoteVolume": 1e9, "lastPrice": 100, "highPrice": 105,
                            "lowPrice": 95, "priceChangePercent": 1} for x in syms}
            def books(s, m, sym=None): return {x: (99.99, 100.0) for x in ("BTCUSDT", "ETHUSDT", *perps)}
            def premium_index(s, sym=None): return {x: {"funding_rate": 0.0} for x in perps}
            def tokenized_stock_bases(s): return set()
            def fut_info(s):
                return {x: {"base": b, "contract_type": ct, "underlying_type": ut, "step": 0.001,
                            "min_qty": 0.001, "min_notional": 5.0} for x, (b, ct, ut) in perps.items()}

        cfg = Config(json.loads(json.dumps(CFG)))
        cfg["universe"].update(include_spot=False, include_perps=True, stocks_enabled=False,
                               commodities={"XAU": ["gold"], "XAG": ["silver"]})
        got = {c.symbol: c for c in Scout(cfg, FakeBn(), None, self.log, self.tmp / "tok.json").scan()}
        self.assertEqual(sorted(got), ["BTCUSDT", "XAGUSDT", "XAUUSDT"])   # no Tesla stock perp
        self.assertEqual(got["XAUUSDT"].meta["asset_class"], "commodity")
        self.assertEqual(got["XAGUSDT"].meta["news_names"], ["silver"])
        self.assertNotIn("asset_class", got["BTCUSDT"].meta)

    def test_filter_gold_reads_market_news_and_fails_closed(self):
        class FakeBn:
            def avg_daily_quote_volume(s, *a): return 5e8
            def depth(s, *a): return {"bids": [(99.9, 1e5)], "asks": [(100.1, 1e5)]}

        class CryptoNews:
            def risk_for(s, *a): return []

        class Fh:
            def __init__(s, hits): s.hits = hits
            def market_news_risk(s, names, lb): return s.hits

        gold = cand(symbol="XAUUSDT", base="XAU", market="perp", funding_rate=0.0,
                    asset_class="commodity", news_names=["gold"])
        run = lambda fh: FilterAgent(CFG, FakeBn(), None, CryptoNews(), fh, self.log).run([gold])
        self.assertEqual(len(run(Fh([]))), 1)
        self.assertEqual(run(Fh([{"title": "Gold trading halted", "keyword": "halted", "scope": "asset"}])), [])
        self.assertIn("open news risk", self.kills("filter")[-1]["reason"])
        for fh in (None, Fh(None)):                      # no Finnhub key / Finnhub down
            self.assertEqual(run(fh), [])
            self.assertIn("news feed unavailable", self.kills("filter")[-1]["reason"])

    def test_finnhub_market_news_matches_the_metal_only(self):
        f = Finnhub("test-key", CFG["filter"]["news_risk_keywords"])
        now = time.time()
        f._get = lambda path, params: [
            {"headline": "Gold trading halted in Shanghai", "datetime": now - 3600, "source": "x"},
            {"headline": "Goldman Sachs faces probe", "datetime": now - 3600, "source": "x"},  # not gold
            {"headline": "Gold vault hacked", "datetime": now - 48 * 3600, "source": "x"},     # too old
        ]
        self.assertEqual([h["keyword"] for h in f.market_news_risk(["gold"], 12)], ["halted"])

    def test_known_hack_follow_ups_dont_block_but_new_trouble_does(self):
        fc = CFG["filter"]
        entry, day = fc["news_acknowledged"][0], dt.timedelta(days=1)

        def news(titles, until=dt.date.today() + day):       # the real config entry, own end date
            n = CryptoNews([], fc["news_risk_keywords"], fc["venue_news_names"],
                           NewsAck([{**entry, "until": until.isoformat()}]))
            n._items = [{"title": t, "ts": time.time() - 600, "source": "x"} for t in titles]
            n._ok_feeds, n._fetched = 1, time.time()        # no network
            return n

        kw = lambda titles, **k: [h["keyword"] for h in news(titles, **k).risk_for("BTC", "binance_spot", 12)]
        follow_ups = ["Analysts see no broader 'contagion' from $6.3B Binance hack",
                      "North Korean hackers are creating memecoins to wash loot from $6.3 billion Binance hack"]
        self.assertEqual(kw(follow_ups), [])
        # naming "the Binance hack" is enough, the amount isn't needed
        self.assertEqual(kw(["Binance Hack Launderers Mint Elon Musk and Animal Meme Coins to Move Stolen Crypto"]), [])
        # the court follow-up of 19 Nov, which puts the hack at $6.5B
        self.assertEqual(kw(["Binance Wins $6.5B Freezing Order Over Hack That Can Be Served via the Blockchain",
                             "$6.5B Binance hack: court freezes stolen funds"]), [])
        self.assertEqual(kw(["Binance freezes withdrawals after hack"]), ["freezes withdrawals"])
        self.assertEqual(kw(["Binance suspends withdrawals despite freezing order over hack"]), ["suspends"])
        self.assertEqual(kw(["Court issues freezing order against Binance in fraud case"]), ["fraud"])
        # new trouble still blocks, also when the old hack is in the same headline
        self.assertEqual(kw(["Binance suspends withdrawals after $6.3B hack"]), ["suspends"])
        self.assertEqual(kw(["Binance pauses withdrawals again, weeks after $6.3 billion hack"]),
                         ["pauses withdrawals"])
        self.assertEqual(kw(["Binance hacked again, $400M drained"]), ["hacked"])
        self.assertEqual(kw(["Binance hacked for $400M in hot wallet exploit"]), ["hacked"])
        self.assertEqual(kw(["Another Binance hack: $400M drained"]), ["hack"])
        self.assertEqual(kw(["New $400M Binance hack hits hot wallet"]), ["hack"])
        self.assertEqual(kw(["Binance hacked again, weeks after $6.3B hack"]), ["hacked"])
        self.assertEqual(kw(["Bitcoin ETFs log $6.3B outflows as SEC probe widens"]), ["probe"])  # not the hack
        self.assertEqual(kw(["Bitcoin ETFs log $6.5B outflows as SEC probe widens"]), ["probe"])
        self.assertEqual(kw(follow_ups[:1], until=dt.date.today() - day), ["hack"])             # expired

        # through the Filter: the candidate passes, each excused headline is logged once
        class FakeBn:
            def avg_daily_quote_volume(s, *a): return 5e8
            def depth(s, *a): return {"bids": [(99.9, 1e5)], "asks": [(100.1, 1e5)]}

        n = news(follow_ups)
        f = FilterAgent(CFG, FakeBn(), None, n, None, self.log, n.acks)
        for _ in range(2):
            self.assertEqual(len(f.run([cand()])), 1)
        logged = [e["message"] for e in self.store.q("SELECT * FROM events WHERE kind = 'news_ack'")]
        self.assertEqual(sum("not blocking on 'hack'" in m for m in logged), 2)
        self.assertTrue(any("known event, its follow-up headlines" in m for m in logged))

    def test_mexc_cut_off_headlines_dont_block_but_trouble_at_binance_does(self):
        fc = CFG["filter"]
        entry = next(e for e in fc["news_acknowledged"] if "MEXC" in e["note"])
        day = dt.timedelta(days=1)

        def kw(titles, until=dt.date.today() + day):         # the real config entry, own end date
            n = CryptoNews([], fc["news_risk_keywords"], fc["venue_news_names"],
                           NewsAck([{**entry, "until": until.isoformat()}]))
            n._items = [{"title": t, "ts": time.time() - 600, "source": "x"} for t in titles]
            n._ok_feeds, n._fetched = 1, time.time()        # no network
            return [h["keyword"] for h in n.risk_for("BTC", "binance_spot", 12)]

        same_event = [
            "Binance Halts MEXC Transfers as FinCEN Cuts Exchange Off From US Financial System",  # the real one
            "Binance halted MEXC transfers after FinCEN order",
            "Binance suspends deposits and withdrawals to MEXC after FinCEN order",
            "Binance, Coinbase halt transfers to and from MEXC",
            "FinCEN bans MEXC, Binance suspends transfers",
            "US sanctions MEXC; Binance halts transfers to the exchange",
        ]
        for t in same_event:
            self.assertEqual(kw([t]), [], t)
        trouble = {                                          # still blocks, with this risk word
            "Binance halts withdrawals amid MEXC fallout": "halts",
            "Binance pauses withdrawals as MEXC fallout spreads": "pauses withdrawals",
            "US sanctions Binance over MEXC transfers": "sanctions",
            "Binance faces sanctions over MEXC flows": "sanctions",
            "Binance suspended in US after MEXC scandal": "suspended",
            "Binance under investigation over MEXC transfers": "investigation",
            "Binance halts transfers to sanctioned exchange": "halts",              # doesn't name MEXC
        }
        for t, word in trouble.items():
            self.assertEqual(kw([t]), [word], t)
        self.assertEqual(kw(same_event[:1], until=dt.date.today() - day), ["halts"])  # expired


DAYTRADE = {"stop_loss_pct": 3.0, "take_profit_pct": 4.5, "max_hold_hours": 8, "reentry_cooldown_min": 120}
MAIN_RULES = "stop -8%, trail 8% after +15%, crypto max 7 of 10 slots"
DAYTRADE_RULES = ("stop -3%, take profit +4.5%, max hold 8h, re-entry cooldown 120 min, "
                  "crypto max 7 of 10 slots")


class _RulesBase(_Base):
    """Helpers for the exit-rule tests, on a $200 book."""
    START = 200.0

    def held(self, c=None):
        self.enter(c or cand())
        return self.store.open_positions()[-1]

    def at(self, p, mult):
        """Quote p at entry * mult and let Risk look at it."""
        px = p.entry_price * mult
        self.px[p.symbol] = (px, px)
        self.risk.monitor()

    def age(self, p, hours):
        self.store.update_position(p.id, entry_ts=time.time() - hours * 3600)

    def exit_reason(self, base="SOL"):
        return self.store.q("SELECT exit_reason FROM positions WHERE base=? ORDER BY id DESC LIMIT 1",
                            (base,))[0]["exit_reason"]

    def refusal(self, c):
        """Risk's reason for refusing c, or None when it approves."""
        self.px[c.symbol] = (c.bid, c.ask)
        t = self.ex.propose(signal(c))
        self.assertIsNotNone(t)
        if self.risk.approve(t) is not None:
            return None
        return self.store.q("SELECT reason FROM kills WHERE stage='risk' ORDER BY id DESC LIMIT 1")[0]["reason"]

    def switch_cases(self, cases):
        for name, hit in cases:
            f = self.tmp / name
            f.write_text("")
            self.assertEqual(self.risk.halted() == "STOP file present", hit, name)
            f.unlink()
        self.assertIsNone(self.risk.halted())


class MainRulesTest(_RulesBase):
    """The optional rules are off unless a desk sets them: the main desk exits as it always did."""

    def test_no_take_profit_and_no_time_exit(self):
        p = self.held()
        self.age(p, 100)
        self.at(p, 1.10)
        self.assertEqual(len(self.store.open_positions()), 1)
        self.assertEqual((self.risk.desk, self.risk.rules_summary()), ("main", MAIN_RULES))
        msg = self.store.q("SELECT message FROM events WHERE kind='approved'")[0]["message"]
        self.assertTrue(msg.endswith("(-8.0%), max loss $1.60"), msg)

    def test_no_reentry_cooldown(self):
        p = self.held()
        self.at(p, 0.90)
        self.assertEqual(self.exit_reason(), "stop -8.0%")
        self.assertIsNone(self.refusal(cand(bid=89.99, ask=90.0)))

    def test_zero_means_off(self):
        cfg = Config(json.loads(json.dumps(self.cfg)))
        cfg["risk"].update(take_profit_pct=0, max_hold_hours=0, reentry_cooldown_min=0)
        r = Risk(cfg, self.store, self.brokers, None, None, self.log, self.tmp)
        self.assertEqual(r.rules_summary(), MAIN_RULES)
        self.assertEqual([r._opt(k) for k in ("take_profit_pct", "max_hold_hours", "reentry_cooldown_min")],
                         [None, None, None])

    def test_bad_rule_value_is_refused_at_start(self):
        for bad in (-1, "4.5", True, float("nan"), float("inf")):
            cfg = Config(json.loads(json.dumps(self.cfg)))
            cfg["risk"]["max_hold_hours"] = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                Risk(cfg, self.store, self.brokers, None, None, self.log, self.tmp)

    def test_switch_files_name_the_desk(self):
        self.switch_cases([("STOP.daytrade", False), ("STOP.daytrade.txt", False),
                           ("STOP.main", True), ("STOP.main.txt", True), ("STOP", True), ("STOP.txt", True)])


class DaytradeTest(_RulesBase):
    """The daytrade desk: stop -3%, take profit +4.5%, out after 8 h, 2 h before the same asset again."""
    RISK = DAYTRADE
    PROFILE = "daytrade"

    def test_rules_line(self):
        self.assertEqual((self.risk.desk, self.risk.rules_summary()), ("daytrade", DAYTRADE_RULES))

    def test_stop_is_minus_3(self):
        p = self.held()
        self.assertAlmostEqual(p.stop_price, p.entry_price * 0.97)
        self.at(p, 0.975)
        self.assertEqual(len(self.store.open_positions()), 1)
        self.at(p, 0.969)
        c = self.store.closed_positions()[0]
        self.assertEqual(c["exit_reason"], "stop -3.0%")
        self.assertAlmostEqual(c["pnl"] / (p.entry_price * p.qty) * 100, -3.3, delta=0.15)   # fees + slippage

    def test_take_profit_at_plus_4_5(self):
        p = self.held()
        self.at(p, 1.044)                                         # just short of it
        self.assertEqual(len(self.store.open_positions()), 1)
        self.at(p, 1.0451)
        self.assertEqual(self.store.open_positions(), [])
        c = self.store.closed_positions()[0]
        self.assertEqual(c["exit_reason"], "take profit +4.5%")
        self.assertAlmostEqual(c["pnl"] / (p.entry_price * p.qty) * 100, 4.3, delta=0.15)    # fees + slippage
        self.assertFalse(c["trail_active"])                       # the +15% trail never comes into play
        msg = self.store.q("SELECT message FROM events WHERE kind='approved'")[0]["message"]
        self.assertTrue(msg.endswith(", take profit +4.5%, max hold 8h"), msg)

    def test_time_exit_after_8_hours(self):
        p = self.held()
        self.age(p, 7.9)
        self.at(p, 1.01)
        self.assertEqual(len(self.store.open_positions()), 1)
        self.age(p, 8.01)
        self.at(p, 1.01)
        self.assertEqual(self.exit_reason(), "time exit 8h")

    def test_stop_and_take_profit_come_before_the_clock(self):
        p = self.held()
        self.age(p, 9)
        self.at(p, 0.96)
        self.assertEqual(self.exit_reason(), "stop -3.0%")
        q = self.held(cand(symbol="ETHUSDT", base="ETH"))
        self.age(q, 9)
        self.at(q, 1.05)
        self.assertEqual(self.exit_reason("ETH"), "take profit +4.5%")

    def test_reentry_cooldown(self):
        p = self.held()
        self.at(p, 1.05)
        self.assertEqual(self.exit_reason(), "take profit +4.5%")
        again = cand(bid=104.99, ask=105.0)
        self.assertEqual(self.refusal(again), "re-entry cooldown: SOL sold 0 min ago (< 120 min)")
        self.assertIn("re-entry cooldown", self.refusal(cand(market="perp", bid=104.99, ask=105.0)))  # same asset
        self.assertIsNone(self.refusal(cand(symbol="ETHUSDT", base="ETH")))                           # another is free
        self.store.x("UPDATE positions SET exit_ts=? WHERE base='SOL'", (time.time() - 119 * 60,))
        self.assertIn("sold 119 min ago", self.refusal(again))
        self.store.x("UPDATE positions SET exit_ts=? WHERE base='SOL'", (time.time() - 121 * 60,))
        self.assertIsNone(self.refusal(again))

    def test_cooldown_also_after_a_stop(self):
        p = self.held()
        self.at(p, 0.96)
        self.assertEqual(self.exit_reason(), "stop -3.0%")
        self.assertIn("re-entry cooldown", self.refusal(cand(bid=95.99, ask=96.0)))

    def test_switch_files_name_the_desk(self):
        self.switch_cases([("STOP.main", False), ("STOP.main.txt", False),
                           ("STOP.daytrade", True), ("STOP.daytrade.txt", True), ("STOP", True), ("STOP.txt", True)])

    def test_flatten_file_for_this_desk_only(self):
        p = self.held()
        (self.tmp / "FLATTEN.main").write_text("")
        self.at(p, 1.0)
        self.assertEqual(len(self.store.open_positions()), 1)
        (self.tmp / "FLATTEN.daytrade").write_text("")
        self.at(p, 1.0)
        self.assertEqual(self.exit_reason(), "manual FLATTEN")


class ProfileConfigTest(unittest.TestCase):
    """config.<name>.json laid over config.json, in a temp project folder."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        base = {"mode": "paper", "start_equity_usd": 200.0,
                "risk": {"stop_loss_pct": 8.0, "trail_activate_pct": 15.0, "trail_pct": 8.0, "max_open_positions": 10}}
        over = {"profile": "spoofed", "risk": {"stop_loss_pct": 3.0, "take_profit_pct": 4.5}}
        (self.root / "config.json").write_text(json.dumps(base), encoding="utf-8")
        (self.root / "config.daytrade.json").write_text(json.dumps(over), encoding="utf-8")
        clean = {k: v for k, v in os.environ.items() if k not in ("DESK_PROFILE", "DESK_MODE")}
        for p in (mock.patch("desk.config.ROOT", self.root), mock.patch.dict(os.environ, clean, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def test_main_desk_is_the_default(self):
        for prof in (None, "", "main", "MAIN"):
            c = load_config(profile=prof)
            self.assertEqual((c.profile, c.desk_name), ("", "main"), prof)
            self.assertEqual(c["risk"]["stop_loss_pct"], 8.0)
            self.assertNotIn("take_profit_pct", c["risk"])
            self.assertEqual(c.data_dir, self.root / "data" / "paper")
            self.assertEqual(c.log_dir, self.root / "logs")

    def test_profile_overrides_only_what_it_names(self):
        c = load_config(profile="daytrade")
        self.assertEqual(c.desk_name, "daytrade")               # the file cannot rename the desk
        self.assertEqual(c["risk"], {"stop_loss_pct": 3.0, "take_profit_pct": 4.5, "trail_activate_pct": 15.0,
                                     "trail_pct": 8.0, "max_open_positions": 10})
        self.assertEqual(c["start_equity_usd"], 200.0)
        self.assertEqual(c.data_dir, self.root / "data" / "paper-daytrade")   # its own book
        self.assertEqual(c.log_dir, self.root / "logs" / "daytrade")          # and its own logs
        self.assertEqual(profile_names(), ["daytrade"])

    def test_profile_from_environment(self):
        with mock.patch.dict(os.environ, {"DESK_PROFILE": "daytrade"}):
            self.assertEqual(load_config().desk_name, "daytrade")
            self.assertEqual(load_config(profile="main").desk_name, "main")   # an explicit choice wins

    def test_profiles_are_paper_only(self):
        with mock.patch.dict(os.environ, {"DESK_MODE": "testnet"}):
            with self.assertRaises(SystemExit):
                load_config(profile="daytrade")
            self.assertEqual(load_config().mode, "testnet")     # only the main desk may leave paper

    def test_unknown_or_invalid_profile(self):
        for bad in ("nosuch", "../daytrade", "day trade", "x" * 25):
            with self.assertRaises(SystemExit, msg=bad):
                load_config(profile=bad)


class ShippedDaytradeConfigTest(unittest.TestCase):
    def test_file_changes_the_exit_rules_and_nothing_else(self):
        over = json.loads((ROOT / "config.daytrade.json").read_text(encoding="utf-8"))
        merged = _merge(json.loads(json.dumps(CFG)), over)
        self.assertEqual({k: v for k, v in merged["risk"].items() if CFG["risk"].get(k) != v}, DAYTRADE)
        self.assertEqual({k for k in merged if merged[k] != CFG.get(k)}, {"risk", "_about"})
        self.assertEqual(rules_summary(merged["risk"]), DAYTRADE_RULES)
        self.assertEqual(rules_summary(CFG["risk"]), MAIN_RULES)


class CliHelpersTest(unittest.TestCase):
    def test_split_profile(self):
        self.assertEqual(split_profile(["run"]), (["run"], None))
        self.assertEqual(split_profile(["--profile", "daytrade", "run"]), (["run"], "daytrade"))
        self.assertEqual(split_profile(["report", "12", "--profile=daytrade"]), (["report", "12"], "daytrade"))
        with self.assertRaises(SystemExit):
            split_profile(["run", "--profile"])

    def test_rules_summary_keeps_the_trail_when_take_profit_is_above_it(self):
        self.assertEqual(rules_summary({**CFG["risk"], "take_profit_pct": 25}),
                         "stop -8%, take profit +25%, trail 8% after +15%, crypto max 7 of 10 slots")

    def test_desk_stats_counts_from_the_given_moment(self):
        st = Store(Path(tempfile.mkdtemp()) / "s.db")
        t0 = 1_000_000.0
        for ts, eq, cash, n in ((t0 - 600, 198.0, 10.0, 9), (t0 - 60, 196.0, 6.0, 10), (t0 + 3600, 199.92, 49.98, 8)):
            st.x("INSERT INTO equity(ts,equity,cash,open_positions) VALUES(?,?,?,?)", (ts, eq, cash, n))

        def pos(**f):
            row = dict(symbol="XUSDT", base="X", market="spot", venue="binance_spot", qty=1.0, entry_price=20.0,
                       entry_fee=0.02, stop_price=18.4, initial_stop=18.4, peak_price=20.0, trail_active=0,
                       last_price=20.0, last_ts=0.0, funding_paid=0.0, last_funding_ts=0.0, score=0.7,
                       stop_order_id="")
            row.update(f)
            st.open_position(**row)

        closed = dict(status="closed", exit_price=20.0, exit_fee=0.02)
        pos(entry_ts=t0 - 7200, exit_ts=t0 - 100, exit_reason="stop -8.0%", pnl=-1.7, **closed)       # before t0
        pos(entry_ts=t0 - 7200, exit_ts=t0 + 1800, exit_reason="take profit +4.5%", pnl=0.85, **closed)
        pos(entry_ts=t0 + 600, exit_ts=t0 + 7800, exit_reason="time exit 8h", pnl=-0.10, **closed)
        pos(entry_ts=t0 + 900)                                                                        # still open
        d = desk_stats(st, t0, 200.0)
        self.assertEqual((d["eq0"], d["eq_now"], d["eq_ts"]), (196.0, 199.92, t0 + 3600))   # last snapshot <= t0
        self.assertAlmostEqual(d["change_pct"], 2.0)
        self.assertAlmostEqual(d["invested_pct"], 75.0)
        self.assertEqual((d["open"], d["entries"], d["closed"], d["winners"]), (1, 2, 2, 1))
        self.assertAlmostEqual(d["pnl"], 0.75)
        self.assertAlmostEqual(d["avg_hold_h"], 2.25)
        self.assertAlmostEqual(d["fees"], 0.08)
        self.assertEqual(dict(d["exits"]), {"take profit +4.5%": 1, "time exit 8h": 1})

    def test_desk_stats_on_a_new_book(self):
        st = Store(Path(tempfile.mkdtemp()) / "s.db")
        st.set("paper_start_equity", 200.0)
        st.set("paper_cash", 200.0)
        d = desk_stats(st, 1_000_000.0, 200.0)
        self.assertEqual((d["eq0"], d["eq_now"], d["change_pct"], d["invested_pct"]), (200.0, 200.0, 0.0, 0.0))
        self.assertEqual((d["open"], d["entries"], d["closed"], d["avg_hold_h"], d["fees"]), (0, 0, 0, None, 0))


class InstanceLockTest(unittest.TestCase):
    def test_one_process_per_book(self):
        d = Path(tempfile.mkdtemp())
        first, second, other = (InstanceLock(d / n) for n in ("desk.lock", "desk.lock", "other.lock"))
        self.addCleanup(lambda: [x.release() for x in (first, second, other)])
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())      # the same book: refused
        self.assertTrue(other.acquire())        # another desk's book: fine
        first.release()
        self.assertTrue(second.acquire())       # free again once the holder is gone



class MachineOverridesTest(unittest.TestCase):
    """.env switches for a machine that cannot reach api.binance.com / fapi.binance.com (cloud, HTTP 451)."""

    def test_perps_off_and_spot_mirror(self):
        u = {"include_spot": True, "include_perps": True, "commodities": {"XAU": ["gold", "XAU"]}}
        env = {"DESK_PERPS_OFF": "1", "DESK_BINANCE_SPOT_DATA": "https://data-api.binance.vision/"}
        with mock.patch.dict(os.environ, env):
            notes = apply_machine_overrides(u)
        self.assertEqual(u, {"include_spot": True, "include_perps": False, "commodities": {}})
        self.assertEqual(notes, ["spot data via data-api.binance.vision", "perps off (DESK_PERPS_OFF), gold/silver off"])
        # with a gold token on spot, gold stays (as the token) and silver, which has none, goes
        u = {"include_perps": True, "commodities": {"XAU": ["gold"], "XAG": ["silver"]},
             "commodity_spot_tokens": {"XAU": ["XAUT", "PAXG"]}}
        with mock.patch.dict(os.environ, {"DESK_PERPS_OFF": "1"}):
            notes = apply_machine_overrides(u)
        self.assertEqual((u["include_perps"], u["commodities"]), (False, {"XAU": ["gold"]}))
        self.assertEqual(notes[-1], "perps off (DESK_PERPS_OFF), XAU via spot token")

    def test_without_switches_nothing_changes(self):
        u = {"include_spot": True, "include_perps": True, "commodities": {"XAU": ["gold", "XAU"]}}
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("DESK_PERPS_OFF", None)
            os.environ.pop("DESK_BINANCE_SPOT_DATA", None)
            self.assertEqual(apply_machine_overrides(u), [])
        self.assertEqual(u, {"include_spot": True, "include_perps": True, "commodities": {"XAU": ["gold", "XAU"]}})

    def test_binance_without_perps_never_calls_futures(self):
        bn = BinanceData("https://data-api.binance.vision/", perps=False)
        with mock.patch("desk.sources.binance.http_json", side_effect=AssertionError("network call")):
            self.assertEqual(bn.fut_info(), {})
            self.assertEqual(bn.premium_index("BTCUSDT"), {})
        self.assertEqual(bn.base_url("spot"), "https://data-api.binance.vision")
        self.assertEqual(BinanceData().base_url("spot"), "https://api.binance.com")
        self.assertEqual(BinanceData().base_url("perp"), "https://fapi.binance.com")


def copy_cfg(**universe):
    cfg = Config(json.loads(json.dumps(CFG)))
    cfg["universe"].update(universe)
    return cfg


class MarketsTest(_Base):
    """Filter floor, US regular session with the ETF watchlist, gold as a spot token."""

    def test_volume_floor_is_a_dead_market_check(self):
        class FakeBn:
            def __init__(s, ratio): s.avg = 1e9 / ratio          # cand() trades $1e9 in 24 h
            def avg_daily_quote_volume(s, *a): return s.avg
            def depth(s, *a): return {"bids": [(99.9, 1e5)], "asks": [(100.1, 1e5)]}

        class News:
            def risk_for(s, *a): return []

        run = lambda ratio: FilterAgent(CFG, FakeBn(ratio), None, News(), None, self.log).run([cand()])
        self.assertEqual(CFG["filter"]["min_volume_ratio_vs_30d"], 0.6)
        self.assertEqual(len(run(0.7)), 1)                       # a quiet day passes
        self.assertEqual(run(0.5), [])                           # a dead one doesn't
        self.assertIn("volume under 30d avg: 0.50x < 0.6x", self.kills("filter")[-1]["reason"])

    def test_expected_volume_fraction(self):
        self.assertEqual(expected_volume_fraction(0), 0.02)                  # floor at the open
        self.assertAlmostEqual(expected_volume_fraction(30), 0.13)
        self.assertAlmostEqual(expected_volume_fraction(195), 0.475)         # 12:45 ET
        self.assertAlmostEqual(expected_volume_fraction(105, 210), 0.475)    # half day, same point
        self.assertEqual(expected_volume_fraction(390), 1.0)
        self.assertEqual(expected_volume_fraction(500), 1.0)

    def test_stock_volume_is_judged_for_the_time_of_day(self):
        class Alpaca:
            def avg_daily_volume(s, sym, days): return 1000.0

        class Fh:
            def earnings_dates(s, a, b): return {}
            def news_risk(s, sym, lb, names=None): return []

        stock = lambda vol, frac: cand(symbol="QQQ", base="NDX", market="stock", bid=100.0, ask=100.02,
                                       quote_age_sec=5, bid_size=300, ask_size=300, today_volume=vol,
                                       expected_volume_frac=frac)
        f = FilterAgent(CFG, None, Alpaca(), None, Fh(), self.log)
        self.assertEqual(len(f.run([stock(400, 0.475)])), 1)       # 400 by 12:45 = 0.84x a normal day
        self.assertEqual(f.run([stock(400, 1.0)]), [])             # the same 400 at the close = 0.40x
        self.assertIn("volume under 30d avg: 0.40x < 0.6x", self.kills("filter")[-1]["reason"])
        self.assertEqual(f.run([stock(200, 0.475)]), [])
        self.assertIn("for this time of day: 0.42x", self.kills("filter")[-1]["reason"])
        # IEX shows 100 shares at the top of the book: $4,000 of SLV is enough for $20 orders
        thin = cand(symbol="SLV", base="XAG", market="stock", bid=40.0, ask=40.02, quote_age_sec=5,
                    bid_size=100, ask_size=100, today_volume=900, expected_volume_frac=1.0)
        self.assertEqual(len(f.run([thin])), 1)

    def test_scout_regular_session_with_watchlist(self):
        class Alpaca:
            def session_window(s, kind="regular"):
                s.kind = kind
                return dt.datetime(2026, 11, 19, 9, 30), dt.datetime(2026, 11, 19, 16, 0)
            def most_actives(s, n): return ["NVDA", "QQQ"]
            def top_gainers(s, n): return ["PENNY"]
            def snapshots(s, syms):
                s.syms = syms
                q = {"bp": 100.0, "ap": 100.02, "bs": 300, "as": 200, "t": "2026-11-19T17:44:50Z"}
                snap = lambda px, day, v: {"latestQuote": q, "latestTrade": {"p": px},
                                           "dailyBar": {"t": f"{day}T05:00:00Z", "v": v}}
                return {"QQQ": snap(100.01, "2026-11-19", 5000), "GLD": snap(100.01, "2026-11-18", 9000),
                        "NVDA": snap(100.01, "2026-11-19", 7000), "PENNY": snap(1.5, "2026-11-19", 1)}

        cfg = copy_cfg(include_spot=False, include_perps=False, commodities={},
                       stocks_watchlist={"QQQ": "NDX", "GLD": "XAU"})
        a = Alpaca()
        with mock.patch("desk.agents.scout.us_eastern", return_value=dt.datetime(2026, 11, 19, 12, 45)):
            got = {c.symbol: c for c in Scout(cfg, None, a, self.log, self.tmp / "tok.json").scan()}
        self.assertEqual(a.kind, "regular")
        self.assertEqual(a.syms[:2], ["QQQ", "GLD"])                # the watchlist comes every cycle
        self.assertEqual(sorted(got), ["GLD", "NVDA", "QQQ"])        # PENNY is under $2
        self.assertEqual([got[s].base for s in ("QQQ", "GLD", "NVDA")], ["NDX", "XAU", "NVDA"])
        self.assertTrue(got["QQQ"].meta["watchlist"])
        self.assertNotIn("watchlist", got["NVDA"].meta)
        self.assertEqual(got["GLD"].meta["today_volume"], 0.0)       # only yesterday's bar so far
        self.assertEqual(got["QQQ"].meta["expected_volume_frac"], 0.475)
        # after the close: no stocks, and the reason is logged once
        with mock.patch("desk.agents.scout.us_eastern", return_value=dt.datetime(2026, 11, 19, 16, 5)):
            self.assertEqual(Scout(cfg, None, a, self.log, self.tmp / "tok.json").scan(), [])
        msgs = [e["message"] for e in self.store.q("SELECT message FROM events WHERE stage='scout'")]
        self.assertIn("US stocks: outside the regular session, scouting crypto only", msgs)

    def test_gold_trades_as_a_spot_token_when_perps_are_off(self):
        pairs = {"BTCUSDT": "BTC", "XAUTUSDT": "XAUT", "PAXGUSDT": "PAXG"}

        class FakeBn:
            def spot_info(s): return {x: {"base": b, "step": 0.0001, "min_notional": 5.0} for x, b in pairs.items()}
            def tickers_24h(s, m):
                assert m == "spot", "perp data asked for with perps off"
                return {x: {"symbol": x, "quoteVolume": 2e7, "lastPrice": 3724.0, "highPrice": 3740,
                            "lowPrice": 3700, "priceChangePercent": 0.2} for x in pairs}
            def books(s, m, sym=None):
                assert m == "spot", "perp data asked for with perps off"
                return {x: (3723.9, 3724.0) for x in pairs}
            def tokenized_stock_bases(s): return set()

        cfg = copy_cfg(include_spot=True, include_perps=False, stocks_enabled=False, crypto_top_alts=0,
                       commodities={"XAU": ["gold"], "XAG": ["silver"]},
                       commodity_spot_tokens={"XAU": ["XAUT", "PAXG"]})
        got = {c.symbol: c for c in Scout(cfg, FakeBn(), None, self.log, self.tmp / "tok.json").scan()}
        self.assertEqual(sorted(got), ["BTCUSDT", "XAUTUSDT"])      # one gold token, not both
        g = got["XAUTUSDT"]
        self.assertEqual((g.base, g.market, g.meta["asset_class"], g.meta["token"]), ("XAU", "spot", "commodity", "XAUT"))
        warns = [e["message"] for e in self.store.q("SELECT message FROM events WHERE level='warn'")]
        self.assertIn("XAG: not scouted 24/7 (perps are off and it has no spot token)", warns)

        class Bn:
            def avg_daily_quote_volume(s, *a): return 2e7
            def depth(s, *a): return {"bids": [(3723.0, 100)], "asks": [(3725.0, 100)]}

        class CryptoNews:
            def risk_for(s, base, venue, lb):
                s.asked = (base, venue)
                return []

        class Fh:
            def market_news_risk(s, names, lb): return []

        cn = CryptoNews()
        self.assertEqual(len(FilterAgent(CFG, Bn(), None, cn, Fh(), self.log).run([g])), 1)
        self.assertEqual(cn.asked, ("XAUT", "binance_spot"))          # news about the token itself


class LeverageTest(_RulesBase):
    """Main desk: a quiet instrument gets up to 3x on 5% margin, with every exit two thirds as far."""
    QUIET = {"_raw_atr_pct": 0.25}        # 1h ATR 0.25% -> 1.5 / 0.25 = 6 -> capped at 3x

    def lev_enter(self, c=None, breakdown=None):
        c = c or cand()
        self.px[c.symbol] = (c.bid, c.ask)
        t = self.ex.propose(Signal(Survivor(c, {"spread_pct": c.spread_pct}), 0.75, breakdown or self.QUIET))
        a = self.risk.approve(t)
        if a is None:
            return t, None
        self.ex.execute(a)
        return t, self.store.open_positions()[-1]

    def test_leverage_comes_from_how_much_the_instrument_moves(self):
        lev = CFG["risk"]["leverage"]
        self.assertEqual(lev["target_atr_1h_pct"], 1.5)
        # gold ~0.3-0.5%, BTC ~0.6-0.8%, alts 1%+ an hour
        self.assertEqual([leverage_for(lev, "spot", a) for a in (0.25, 0.45, 0.5, 0.6, 0.75, 1.0, 2.0)],
                         [3, 3, 3, 2, 2, 1, 1])
        self.assertEqual(leverage_for(lev, "stock", 0.3), 3)
        self.assertEqual(leverage_for(lev, "perp", 0.25), 1)        # perps stay at 1x
        self.assertEqual([leverage_for(lev, "spot", a) for a in (None, float("nan"), 0)], [1, 1, 1])
        self.assertEqual(leverage_for({**lev, "max_x": 1}, "spot", 0.25), 1)
        self.assertEqual(leverage_for({}, "spot", 0.25), 1)

    def test_sizing_margin_and_tighter_stop(self):
        t, p = self.lev_enter()
        self.assertEqual(t.leverage, 3)
        self.assertAlmostEqual(t.notional, 30.0)                      # 5% margin ($10) x 3
        self.assertEqual((p.leverage, round(p.exit_scale, 4)), (3, 0.6667))
        self.assertAlmostEqual(p.margin, p.qty * p.entry_price / 3)
        self.assertAlmostEqual(p.stop_price, p.entry_price * (1 - 0.0533))
        fee = p.entry_fee
        self.assertAlmostEqual(self.paper.cash(), 200 - p.margin - fee)   # only the margin left cash
        self.assertAlmostEqual(self.brokers.total_equity(), 200 - fee)
        msg = self.store.q("SELECT message FROM events WHERE kind='approved'")[-1]["message"]
        self.assertIn("$30.00 at 3x (margin $10.00), stop 94.67 (-5.33%), max loss $1.60", msg)
        self.at(p, 0.95)                                               # -5%: inside the stop
        self.assertEqual(len(self.store.open_positions()), 1)
        self.assertAlmostEqual(self.brokers.total_equity(), 200 - fee + p.qty * (p.entry_price * 0.95 - p.entry_price))
        self.at(p, 0.946)
        c = self.store.closed_positions()[0]
        self.assertEqual(c["exit_reason"], "stop -5.33%")
        normal_max_loss = 200 * 0.10 * 0.08                             # a 10% trade at -8%
        self.assertLess(-c["pnl"], normal_max_loss * 1.1)               # same risk, plus fees and slippage
        self.assertAlmostEqual(self.paper.cash(), 200 + c["pnl"])       # margin back, result settled

    def test_trail_starts_and_follows_two_thirds_as_far(self):
        t, p = self.lev_enter()
        self.at(p, 1.099)
        self.assertFalse(self.store.open_positions()[0].trail_active)
        self.at(p, 1.101)                                               # +10% = 15% x 2/3
        q = self.store.open_positions()[0]
        self.assertTrue(q.trail_active)
        self.assertAlmostEqual(q.stop_price, p.entry_price * 1.101 * (1 - 0.0533))
        self.at(p, 1.04)
        c = self.store.closed_positions()[0]
        self.assertEqual(c["exit_reason"], "trailing stop")
        self.assertGreater(c["pnl"], 0)

    def test_moderate_mover_gets_2x_with_normal_exits(self):
        t, p = self.lev_enter(breakdown={"_raw_atr_pct": 0.6})
        self.assertEqual((t.leverage, p.exit_scale), (2, 1.0))
        self.assertAlmostEqual(t.notional, 20.0)                       # same size as a normal trade
        self.assertAlmostEqual(p.stop_price, p.entry_price * (1 - self.cfg["risk"]["stop_loss_pct"] / 100))
        self.assertAlmostEqual(self.paper.cash(), 200 - p.margin - p.entry_fee)   # same stop, half the cash

    def test_gross_exposure_cap(self):
        lev = self.cfg["risk"]["leverage"]
        lev["max_gross_exposure_pct"] = 20.0                           # $40 on a $200 book
        self.lev_enter()
        t, p = self.lev_enter(cand(symbol="XAUTUSDT", base="XAU"))
        self.assertIsNone(p)
        self.assertEqual(self.kills("risk")[-1]["reason"], "gross exposure $59.91 would pass 20% of equity ($39.99)")

    def test_interest_on_the_borrowed_part(self):
        t, p = self.lev_enter()
        borrowed = p.qty * p.entry_price - p.margin
        self.store.update_position(p.id, last_funding_ts=time.time() - 365 * 86400)
        cash = self.paper.cash()
        self.at(p, 1.0)
        q = self.store.open_positions()[0]
        self.assertAlmostEqual(q.funding_paid, borrowed * 0.10, places=3)   # 10% a year on spot
        self.assertAlmostEqual(self.paper.cash(), cash - q.funding_paid)
        self.at(p, 0.94)
        c = self.store.closed_positions()[0]
        gross = (c["exit_price"] - c["entry_price"]) * c["qty"]
        self.assertAlmostEqual(c["pnl"], gross - c["entry_fee"] - c["exit_fee"] - q.funding_paid, places=4)

    def test_leverage_is_paper_only_and_sealed(self):
        c = cand()
        self.px[c.symbol] = (c.bid, c.ask)
        sig = Signal(Survivor(c, {"spread_pct": c.spread_pct}), 0.75, self.QUIET)
        t = self.ex.propose(sig)
        a = self.risk.approve(t)
        self.assertFalse(self.risk.verify_seal(dataclasses.replace(a, ticket=dataclasses.replace(t, leverage=1))))
        self.risk.mode = "testnet"
        self.assertIsNone(self.risk.approve(dataclasses.replace(t, ticket_id="other")))
        self.assertEqual(self.kills("risk")[-1]["reason"], "leverage is paper-only")
        self.ex.mode = "testnet"
        self.assertEqual(self.ex.propose(sig).leverage, 1)

    def test_old_books_load_unlevered(self):
        path = self.tmp / "old.db"
        import sqlite3
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, symbol TEXT, base TEXT, market TEXT, "
                    "venue TEXT, qty REAL, entry_price REAL, entry_ts REAL, entry_fee REAL, stop_price REAL, "
                    "initial_stop REAL, peak_price REAL, trail_active INTEGER, last_price REAL, last_ts REAL, "
                    "funding_paid REAL, last_funding_ts REAL, score REAL, stop_order_id TEXT, status TEXT, "
                    "exit_price REAL, exit_ts REAL, exit_fee REAL, exit_reason TEXT, pnl REAL)")
        con.execute("INSERT INTO positions (symbol, base, market, venue, qty, entry_price, last_price, stop_price, "
                    "initial_stop, peak_price, trail_active, funding_paid, status) "
                    "VALUES ('SOLUSDT', 'SOL', 'spot', 'binance_spot', 0.268, 74.6, 75.0, 68.6, 68.6, 75, 0, 0, 'open')")
        con.commit()
        con.close()
        p = Store(path).open_positions()[0]
        self.assertEqual((p.leverage, p.margin, p.exit_scale), (1.0, None, 1.0))
        self.assertAlmostEqual(p.value, 0.268 * 75.0)

    def test_stocks_flatten_before_the_regular_close(self):
        class Alpaca:
            def __init__(s): s.left, s.kinds = 60, []
            def session_window(s, kind="regular"):
                s.kinds.append(kind)
                now = us_eastern()
                return now - dt.timedelta(hours=2), now + dt.timedelta(minutes=s.left)

        a = Alpaca()
        self.risk.alpaca = a
        p = self.held(cand(symbol="QQQ", base="NDX", market="stock"))
        self.at(p, 1.0)
        self.assertEqual(len(self.store.open_positions()), 1)
        a.left = 9                                                     # 9 min to the 16:00 close
        self.at(p, 1.0)
        self.assertEqual(self.exit_reason("NDX"), "flatten before the close")
        self.assertEqual(set(a.kinds), {"regular"})


class DaytradeLeverageTest(LeverageTest):
    """Daytrade desk at 3x: stop -2%, take profit +3% (two thirds of -3% / +4.5%)."""
    RISK = DAYTRADE

    def test_sizing_margin_and_tighter_stop(self):
        t, p = self.lev_enter()
        self.assertAlmostEqual(p.stop_price, p.entry_price * 0.98)
        msg = self.store.q("SELECT message FROM events WHERE kind='approved'")[-1]["message"]
        self.assertTrue(msg.endswith("(-2.0%), max loss $0.60, take profit +3%, max hold 8h"), msg)
        self.at(p, 1.029)
        self.assertEqual(len(self.store.open_positions()), 1)
        self.at(p, 1.0301)
        self.assertEqual(self.exit_reason(), "take profit +3%")

    def test_trail_starts_and_follows_two_thirds_as_far(self):
        t, p = self.lev_enter()
        self.at(p, 0.979)
        self.assertEqual(self.exit_reason(), "stop -2.0%")

    def test_interest_on_the_borrowed_part(self):
        pass                                                           # same as the main desk

    def test_gross_exposure_cap(self):
        pass                                                           # same as the main desk


class StockNewsRelevanceTest(_Base):
    """Stock news only blocks when the headline names the company. Real Finnhub articles from
    19-21 Nov 2026: a peer's downgrade and the Anthropic IPO were blocking NVDA, QQQ and INTC."""
    NOW = time.time()

    def finnhub(self, rows):
        f = Finnhub("test-key", CFG["filter"]["news_risk_keywords"],
                    headline_only=CFG["filter"]["news_headline_only_keywords"])

        def get(path, params):
            if path == "/calendar/earnings":
                return {"earningsCalendar": []}
            return [{"headline": h, "summary": s, "datetime": self.NOW - 3600, "source": "x"} for h, s in rows]
        f._get = get
        return f

    def test_company_names_from_the_listing(self):
        cases = {
            "NVIDIA Corporation Common Stock": ["NVIDIA"],
            "Advanced Micro Devices, Inc. Common Stock": ["Advanced Micro Devices", "Advanced Micro"],
            "CoreWeave, Inc. Class A Common Stock": ["CoreWeave"],
            "Invesco QQQ Trust, Series 1": ["Invesco QQQ"],
            "Space Exploration Technologies Corp. Class A Common Stock": ["Space Exploration Technologies",
                                                                          "Space Exploration"],
            "Palo Alto Networks, Inc. Common Stock": ["Palo Alto Networks", "Palo"],
            "Amazon.com, Inc. Common Stock": ["Amazon"],
            "Nebius Group N.V. Class A Ordinary Shares": ["Nebius"],
            "Direxion Daily Semiconductor Bear 3X ETF": ["Direxion Daily Semiconductor Bear 3X"],
            None: [], "": [],
        }
        for full, names in cases.items():
            self.assertEqual(company_names(full), names, full)

    def test_only_headlines_naming_the_company_count(self):
        f = self.finnhub([
            ("Nebius: Neocloud Bubble Burst Is Already Happening (Rating Downgrade)", ""),
            ("Premarket movers: Palo Alto soars, chip stocks rally ahead of Anthropic debut",
             "Equity futures jumped after Anthropic's initial public offering priced above expectations."),
            ("Nvidia stock extends $2.5 trillion rout as SEC probe reportedly investigates its accounting", ""),
            ("Nvidia Sinks 7% on New Probes as California AG Says Scandal Raises Bright Red Flags",
             "Nvidia shares fell 7% after California's attorney general subpoenaed the company and "
             "lawmakers opened a probe."),
        ])
        hits = f.news_risk("NVDA", 24, ["NVIDIA"])
        self.assertEqual([h["keyword"] for h in hits], ["probe", "probe"])
        other = f.take_other()
        self.assertEqual([(o["symbol"], o["keyword"]) for o in other], [("NVDA", "downgrade"), ("NVDA", "offering")])
        f.news_risk("NVDA", 24, ["NVIDIA"])
        self.assertEqual(f.take_other(), [])                     # each skipped headline is reported once
        # without the company's names every article filed under the ticker counts, as before
        # (the IPO one still doesn't: 'offering' only counts in a headline)
        self.assertEqual([h["keyword"] for h in f.news_risk("NVDA", 24)], ["downgrade", "probe", "probe"])

    def test_everyday_words_count_in_the_headline_only(self):
        f = self.finnhub([
            ("Intel's Turnaround Story Remains Firmly Intact, Buy The Panic",
             "Its forward P/E below 40x is offering an accumulation opportunity."),
            ("Not AMD. Not Intel. This Is the Chip Stock Wall Street Is Underestimating",
             "Broadcom's AI business is growing, but a fresh downgrade has investors focused on one risk."),
        ])
        self.assertEqual(f.news_risk("INTC", 24, ["Intel"]), [])
        f = self.finnhub([("Why Is CoreWeave (CRWV) Facing New Supply Chain Risk From A US Ban?", ""),
                          ("Intel Prices $2 Billion Public Offering of Common Stock", "")])
        self.assertEqual([h["keyword"] for h in f.news_risk("CRWV", 24, ["CoreWeave"])], ["ban"])
        self.assertEqual([h["keyword"] for h in f.news_risk("INTC", 24, ["Intel"])], ["offering"])

    def filter_run(self, alpaca, rows, symbol="NVDA"):
        stock = cand(symbol=symbol, base=symbol, market="stock", bid=100.0, ask=100.02, quote_age_sec=5,
                     bid_size=300, ask_size=300, today_volume=900, expected_volume_frac=1.0)
        return FilterAgent(CFG, None, alpaca, None, self.finnhub(rows), self.log).run([stock])

    def test_filter_uses_the_listing_name_and_aliases(self):
        class Alpaca:
            names = {"NVDA": "NVIDIA Corporation Common Stock",
                     "SPCX": "Space Exploration Technologies Corp. Class A Common Stock"}
            def avg_daily_volume(s, sym, days): return 1000.0
            def asset_name(s, sym): return s.names[sym]

        peer = ("Nebius: I Was Wrong, Growth Financing In Danger (Rating Downgrade)", "I downgrade NBIS stock.")
        self.assertEqual(len(self.filter_run(Alpaca(), [peer])), 1)
        logged = self.store.q("SELECT message FROM events WHERE kind='news_other'")
        self.assertEqual(len(logged), 1)
        self.assertIn("not blocking NVDA on 'downgrade'", logged[0]["message"])
        own = ("After Its Worst Week in 18 Years, Nvidia Now Faces an SEC Investigation", "")
        self.assertEqual(self.filter_run(Alpaca(), [peer, own]), [])
        self.assertIn("'investigation'", self.kills("filter")[-1]["reason"])
        # SpaceX is listed as Space Exploration Technologies: config stock_news_aliases names it
        merger = ("Gary Black Warns SpaceX-Tesla Merger Could Wipe Out 24% Premium in Weeks: "
                  "'Massive Dilution' Could Crush Shares", "")
        self.assertEqual(self.filter_run(Alpaca(), [merger], "SPCX"), [])
        self.assertIn("'dilution'", self.kills("filter")[-1]["reason"])

    def test_filter_without_a_name_counts_every_article(self):
        class Alpaca:
            def avg_daily_volume(s, sym, days): return 1000.0
            def asset_name(s, sym): raise HttpError("HTTP 500")

        peer = ("Nebius: Neocloud Bubble Burst Is Already Happening (Rating Downgrade)", "")
        self.assertEqual(self.filter_run(Alpaca(), [peer]), [])
        self.assertIn("'downgrade' in \"Nebius", self.kills("filter")[-1]["reason"])


class CryptoCapTest(_RulesBase):
    """3 of 10 slots stay free for stocks: crypto (and gold tokens) fill at most 7."""

    class Alpaca:
        def session_window(s, kind="regular"):
            now = us_eastern()
            return now - dt.timedelta(hours=2), now + dt.timedelta(hours=4)

    def coin(self, i, px=100.0):
        return cand(symbol=f"C{i}USDT", base=f"C{i}", bid=px - 0.01, ask=px)

    def test_eighth_crypto_is_refused_but_a_stock_gets_in(self):
        self.assertEqual(self.risk.crypto_cap(), 7)
        for i in range(7):
            self.enter(self.coin(i))
        self.assertEqual(self.refusal(self.coin(7)), "crypto slots full: 7/7 (3 of 10 slots kept for stocks)")
        self.risk.alpaca = self.Alpaca()
        self.assertIsNone(self.refusal(cand(symbol="QQQ", base="NDX", market="stock", bid=100.0, ask=100.02)))

    def test_holdings_above_the_cap_are_sold_weakest_first(self):
        self.risk.r["reserve_slots_for_stocks"] = 0              # as before the reserve existed
        for i in range(10):
            self.enter(self.coin(i))
        self.assertEqual(len(self.store.open_positions()), 10)
        self.risk.r["reserve_slots_for_stocks"] = 3
        for i, p in enumerate(self.store.open_positions()):    # C0 weakest ... C9 strongest
            px = p.entry_price * (0.97 + i * 0.01)
            self.px[p.symbol] = (px, px)
        self.risk.monitor()
        left = sorted(p.base for p in self.store.open_positions())
        self.assertEqual(left, [f"C{i}" for i in range(3, 10)])
        sold = self.store.q("SELECT base, exit_reason FROM positions WHERE status='closed' ORDER BY base")
        self.assertEqual([(r["base"], r["exit_reason"]) for r in sold],
                         [(f"C{i}", "slot reserve: crypto max 7") for i in range(3)])
        self.risk.monitor()                                      # at the cap: nothing more is sold
        self.assertEqual(len(self.store.open_positions()), 7)

    def test_zero_means_no_cap_and_bad_values_are_refused(self):
        cfg = Config(json.loads(json.dumps(self.cfg)))
        cfg["risk"]["reserve_slots_for_stocks"] = 0
        r = Risk(cfg, self.store, self.brokers, None, None, self.log, self.tmp)
        self.assertIsNone(r.crypto_cap())
        self.assertEqual(r.rules_summary(), "stop -8%, trail 8% after +15%")
        for bad in (-1, 11, 2.5, True, "3"):
            cfg["risk"]["reserve_slots_for_stocks"] = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                Risk(cfg, self.store, self.brokers, None, None, self.log, self.tmp)


if __name__ == "__main__":
    unittest.main()
