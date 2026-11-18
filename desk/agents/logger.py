"""Agent 6 - Logger. Every other agent reports here. Nothing is dropped silently:
every kill (with the measured value vs the threshold) goes to SQLite, a daily JSONL
file and the console."""
from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import time

from ..store import Store


class LoggerAgent:
    name = "logger"

    def __init__(self, store: Store, log_dir, mode: str, console: bool = True):
        self.store = store
        self.log_dir = log_dir
        self.mode = mode
        self.console = console
        self.cycle = 0
        self._lock = threading.Lock()

    def _jsonl(self, rec: dict):
        day = dt.datetime.now().strftime("%Y%m%d")
        path = self.log_dir / f"desk-{self.mode}-{day}.jsonl"
        with self._lock, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def _print(self, line: str):
        if self.console:
            ts = dt.datetime.now().strftime("%H:%M:%S")
            print(f"{ts} {line}", flush=True, file=sys.stdout)

    def kill(self, stage: str, symbol: str, market: str, reason: str, **detail):
        self.store.add_kill(self.cycle, stage, symbol, market, reason, detail)
        self._jsonl({"ts": time.time(), "type": "kill", "cycle": self.cycle, "stage": stage,
                     "symbol": symbol, "market": market, "reason": reason, "detail": detail})
        self._print(f"KILL  [{stage:8}] {market:5} {symbol:14} {reason}")

    def event(self, stage: str, kind: str, message: str, level: str = "info", **detail):
        self.store.add_event(level, stage, kind, message, detail)
        self._jsonl({"ts": time.time(), "type": "event", "level": level, "stage": stage,
                     "kind": kind, "message": message, "detail": detail})
        tag = {"info": "INFO ", "warn": "WARN ", "error": "ERROR", "trade": "TRADE"}.get(level, level.upper())
        self._print(f"{tag} [{stage:8}] {message}")
