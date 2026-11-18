"""Orchestrator: wires the six agents and runs scan + monitor loops."""
from __future__ import annotations

import json
import os
import threading
import time
import traceback

from .agents.analyst import Analyst
from .agents.executor import Executor
from .agents.filter import FilterAgent
from .agents.logger import LoggerAgent
from .agents.risk import Risk
from .agents.scout import Scout
from .brokers.paper import Brokers, PaperBroker
from .config import LIVE_CONFIRM_PHRASE, ROOT, Config, env
from .sources.alpaca import AlpacaData
from .sources.binance import BinanceData
from .sources.news import CryptoNews, Finnhub, NewsAck
from .store import Store

VENUES = ("binance_spot", "binance_futures", "alpaca")


class InstanceLock:
    """One running desk per data folder: a second process on the same book would
    sell the same positions twice. OS-level lock, released when the process dies."""

    def __init__(self, path):
        self.path, self._fh = path, None

    def acquire(self) -> bool:
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self):
        if self._fh:
            self._fh.close()
            self._fh = None


def perps_off() -> bool:
    """DESK_PERPS_OFF=1 in .env: this machine cannot reach Binance futures (e.g. HTTP 451 geo-block)."""
    return (env("DESK_PERPS_OFF") or "").strip().lower() in ("1", "true", "yes", "on")


def apply_machine_overrides(universe: dict) -> list[str]:
    """Per-machine switches from .env (not config.json, so every machine keeps the same rules).
    Narrows the universe only; risk rules and books are untouched. Returns notes for the start line."""
    notes = []
    spot = env("DESK_BINANCE_SPOT_DATA")
    if spot:
        notes.append(f"spot data via {spot.split('//')[-1].rstrip('/')}")
    if perps_off():
        universe["include_perps"] = False
        universe["commodities"] = {}  # gold/silver are perps on the same venue
        notes.append("perps and gold/silver off (DESK_PERPS_OFF)")
    return notes


class Desk:
    def __init__(self, cfg: Config, console: bool = True):
        self.cfg = cfg
        self.mode = cfg.mode
        self.store = Store(cfg.data_dir / "desk.db")
        self.log = LoggerAgent(self.store, cfg.log_dir, self.mode, console)
        self.bn = BinanceData(env("DESK_BINANCE_SPOT_DATA"), perps=not perps_off())
        self.machine_notes = apply_machine_overrides(cfg["universe"])
        f = cfg.section("filter")
        self.news_acks = NewsAck(f.get("news_acknowledged"))
        self.cnews = CryptoNews(f["crypto_news_feeds"], f["news_risk_keywords"], f.get("venue_news_names", {}),
                                self.news_acks)
        self.finnhub = Finnhub(env("FINNHUB_API_KEY"), f["news_risk_keywords"], self.news_acks)
        ak, asec = env("ALPACA_KEY_ID"), env("ALPACA_SECRET_KEY")
        trading_base = cfg["venues"]["alpaca_trading"]["live" if self.mode == "live" else "testnet"]
        self.alpaca = AlpacaData(ak, asec, trading_base) if ak and asec else None

        self.brokers = Brokers(self._make_brokers(), self.store)
        self.scout = Scout(cfg, self.bn, self.alpaca, self.log)
        self.filter = FilterAgent(cfg, self.bn, self.alpaca, self.cnews, self.finnhub, self.log, self.news_acks)
        self.analyst = Analyst(cfg, self.bn, self.alpaca, self.log)
        self.executor = Executor(cfg, self.brokers, self.store, self.log)
        self.risk = Risk(cfg, self.store, self.brokers, self.quotes, self.alpaca, self.log, ROOT)
        self.executor.risk = self.risk
        self._stop = threading.Event()
        self._lock = None

    # ------------------------------------------------------------ brokers
    def _info(self, symbol: str) -> dict:
        return self.bn.fut_info().get(symbol) or self.bn.spot_info().get(symbol) or {}

    def _make_brokers(self) -> dict:
        if self.mode == "paper":
            pb = PaperBroker(self.cfg, self.store, self.fresh_quote, self.funding)
            return {v: pb for v in VENUES}
        if self.mode == "live" and env("DESK_LIVE_CONFIRM") != LIVE_CONFIRM_PHRASE:
            raise SystemExit(f"LIVE mode refused: set DESK_LIVE_CONFIRM={LIVE_CONFIRM_PHRASE} in .env yourself.")
        if self.mode == "live":
            ok = ROOT / "data" / "testnet" / "preflight_ok.json"
            if not ok.exists():
                raise SystemExit("LIVE mode refused: run `python -m desk preflight` in testnet mode first.")
        from .brokers.alpaca_live import AlpacaBroker
        from .brokers.binance_live import BinanceFuturesBroker, BinanceSpotBroker
        v = self.cfg["venues"]
        out = {}
        sfx = "_TESTNET" if self.mode == "testnet" else ""
        bk, bs = env(f"BINANCE_API_KEY{sfx}"), env(f"BINANCE_API_SECRET{sfx}")
        if bk and bs:
            out["binance_spot"] = BinanceSpotBroker(v["binance_spot"][self.mode], bk, bs,
                                                    lambda s: self.bn.spot_info().get(s, {}))
        fk, fs = env(f"BINANCE_FUTURES_API_KEY{sfx}") or bk, env(f"BINANCE_FUTURES_API_SECRET{sfx}") or bs
        if fk and fs:
            out["binance_futures"] = BinanceFuturesBroker(v["binance_futures"][self.mode], fk, fs,
                                                          lambda s: self.bn.fut_info().get(s, {}))
        ak, asec = env("ALPACA_KEY_ID"), env("ALPACA_SECRET_KEY")
        if ak and asec:
            out["alpaca"] = AlpacaBroker(v["alpaca_trading"][self.mode], ak, asec)
        if not out:
            raise SystemExit(f"{self.mode} mode: no API keys found in .env")
        missing = [x for x in VENUES if x not in out]
        u = self.cfg["universe"]
        if "binance_spot" in missing:
            u["include_spot"] = False
        if "binance_futures" in missing:
            u["include_perps"] = False
            u["commodities"] = {}  # gold/silver are perps on the same venue
        if "alpaca" in missing:
            u["stocks_enabled"] = False
        return out

    # ------------------------------------------------------------ market data helpers
    def fresh_quote(self, market: str, symbol: str) -> tuple[float, float]:
        if market == "stock":
            s = self.alpaca.snapshots([symbol]).get(symbol, {}).get("latestQuote", {})
            return float(s.get("bp") or 0), float(s.get("ap") or 0)
        return self.bn.books(market, symbol)[symbol]

    def funding(self, symbol: str):
        return self.bn.premium_index(symbol).get(symbol)

    def quotes(self, positions) -> dict:
        out = {}
        by: dict[str, list[str]] = {}
        for p in positions:
            by.setdefault(p.market, []).append(p.symbol)
        for market, syms in by.items():
            if market == "stock":
                if self.alpaca:
                    for s, snap in self.alpaca.snapshots(syms).items():
                        q = snap.get("latestQuote") or {}
                        out[s] = (float(q.get("bp") or 0), float(q.get("ap") or 0))
            else:
                out.update(self.bn.quotes(market, syms))
        return out

    # ------------------------------------------------------------ cycles
    def scan_cycle(self):
        self.log.cycle += 1
        t0 = time.time()
        halted = self.risk.halted()
        if halted:
            self.log.event("risk", "halt", f"no new entries this cycle: {halted}", level="warn")
        cands = self.scout.scan()
        survivors = self.filter.run(cands)
        signals = self.analyst.run(survivors)
        for sig in sorted(signals, key=lambda s: s.score, reverse=True):
            try:
                ticket = self.executor.propose(sig)
                if not ticket:
                    continue
                approved = self.risk.approve(ticket)
                if not approved:
                    continue
                self.executor.execute(approved)
            except Exception as e:  # noqa: BLE001
                self.log.kill("executor", sig.candidate.symbol, sig.candidate.market,
                              f"order error: {type(e).__name__}: {str(e)[:150]}")
        self.log.event("desk", "cycle", f"cycle {self.log.cycle} done in {time.time() - t0:.0f}s | "
                       f"{len(cands)} scouted, {len(survivors)} passed filter, {len(signals)} scored >= "
                       f"{self.cfg['analyst']['min_score']} | equity ${self.brokers.total_equity():.2f}")

    def snapshot_equity(self):
        eq = self.brokers.total_equity()
        cash = sum(b.cash() for b in {id(b): b for b in self.brokers.mapping.values()}.values())
        self.store.add_equity(eq, cash, len(self.store.open_positions()))
        (self.cfg.data_dir / "heartbeat.json").write_text(json.dumps(
            {"ts": time.time(), "equity": eq, "cash": cash, "cycle": self.log.cycle}), encoding="utf-8")

    def _monitor_loop(self):
        interval = self.cfg["loop"]["monitor_interval_sec"]
        last_rec = 0.0
        while not self._stop.is_set():
            try:
                self.risk.monitor()
                if time.time() - last_rec > 60:
                    self.risk.reconcile()
                    last_rec = time.time()
            except Exception as e:  # noqa: BLE001
                self.log.event("risk", "monitor_error", f"{type(e).__name__}: {e}", level="error")
            self._stop.wait(interval)

    def claim(self):
        """Take this desk's book for this process (see InstanceLock)."""
        self._lock = InstanceLock(self.cfg.data_dir / "desk.lock")
        if not self._lock.acquire():
            raise SystemExit(f"Desk '{self.cfg.desk_name}' is already running on this book "
                             f"({self.cfg.data_dir}). Stop that process first.")

    @staticmethod
    def _prevent_idle_sleep():
        """Ask Windows not to idle-sleep while this process runs (released on exit;
        changes no settings). A closed laptop lid still sleeps."""
        import sys
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)

    def run(self):
        self.claim()
        L = self.cfg["loop"]
        self._prevent_idle_sleep()
        self.log.event("desk", "start", f"desk started in {self.mode.upper()} mode [{self.cfg.desk_name}] | equity "
                       f"${self.brokers.total_equity():.2f} | rules: {self.risk.rules_summary()} | "
                       f"stocks {'on' if self.alpaca else 'off (no keys)'} | "
                       f"stocks news/earnings {'on' if self.finnhub.available else 'off (no FINNHUB key -> fail-closed)'}"
                       + "".join(f" | {n}" for n in self.machine_notes))
        mon = threading.Thread(target=self._monitor_loop, name="risk-monitor", daemon=True)
        mon.start()
        last_scan = last_eq = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                if now - last_scan >= L["scan_interval_sec"]:
                    last_scan = now
                    try:
                        self.scan_cycle()
                    except Exception as e:  # noqa: BLE001
                        self.log.event("desk", "cycle_error", f"{type(e).__name__}: {e}", level="error",
                                       tb=traceback.format_exc()[-1500:])
                if now - last_eq >= L["equity_snapshot_sec"]:
                    last_eq = now
                    try:
                        self.snapshot_equity()
                    except Exception as e:  # noqa: BLE001
                        self.log.event("desk", "equity_error", str(e), level="warn")
                self._stop.wait(2)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            self.log.event("desk", "stop", "desk stopped (open positions stay open; stops resume on restart)")
