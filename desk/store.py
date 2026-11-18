"""SQLite persistence: positions, kills, events, equity curve, key/value state."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .models import Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS kills (
  id INTEGER PRIMARY KEY, ts REAL, cycle INTEGER, stage TEXT, symbol TEXT, market TEXT,
  reason TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL, level TEXT, stage TEXT, kind TEXT, message TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY, symbol TEXT, base TEXT, market TEXT, venue TEXT, qty REAL,
  entry_price REAL, entry_ts REAL, entry_fee REAL, stop_price REAL, initial_stop REAL,
  peak_price REAL, trail_active INTEGER, last_price REAL, last_ts REAL, funding_paid REAL,
  last_funding_ts REAL, score REAL, stop_order_id TEXT, status TEXT,
  exit_price REAL, exit_ts REAL, exit_fee REAL, exit_reason TEXT, pnl REAL);
CREATE TABLE IF NOT EXISTS equity (ts REAL, equity REAL, cash REAL, open_positions INTEGER);
CREATE INDEX IF NOT EXISTS kills_ts ON kills(ts);
CREATE INDEX IF NOT EXISTS pos_status ON positions(status);
"""

POS_FIELDS = ["id", "symbol", "base", "market", "venue", "qty", "entry_price", "entry_ts", "entry_fee",
              "stop_price", "initial_stop", "peak_price", "trail_active", "last_price", "last_ts",
              "funding_paid", "last_funding_ts", "score", "stop_order_id", "status"]


class Store:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    # kv
    def get(self, key, default=None):
        rows = self.q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key, value):
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    # kills / events
    def add_kill(self, cycle, stage, symbol, market, reason, detail):
        self.x("INSERT INTO kills(ts,cycle,stage,symbol,market,reason,detail) VALUES(?,?,?,?,?,?,?)",
               (time.time(), cycle, stage, symbol, market, reason, json.dumps(detail, default=str)))

    def add_event(self, level, stage, kind, message, detail):
        self.x("INSERT INTO events(ts,level,stage,kind,message,detail) VALUES(?,?,?,?,?,?)",
               (time.time(), level, stage, kind, message, json.dumps(detail, default=str)))

    # positions
    def _pos(self, r) -> Position:
        d = {k: r[k] for k in POS_FIELDS}
        d["trail_active"] = bool(d["trail_active"])
        d["stop_order_id"] = d["stop_order_id"] or ""
        return Position(**d)

    def open_positions(self) -> list[Position]:
        return [self._pos(r) for r in self.q("SELECT * FROM positions WHERE status='open' ORDER BY id")]

    def open_position(self, **f) -> int:
        f.setdefault("status", "open")
        cols = ",".join(f)
        return self.x(f"INSERT INTO positions({cols}) VALUES({','.join('?' * len(f))})", tuple(f.values()))

    def update_position(self, pid: int, **f):
        sets = ",".join(f"{k}=?" for k in f)
        self.x(f"UPDATE positions SET {sets} WHERE id=?", (*f.values(), pid))

    def closed_positions(self, since: float = 0):
        return self.q("SELECT * FROM positions WHERE status='closed' AND exit_ts>=? ORDER BY exit_ts", (since,))

    def add_equity(self, equity, cash, n_open):
        self.x("INSERT INTO equity(ts,equity,cash,open_positions) VALUES(?,?,?,?)",
               (time.time(), equity, cash, n_open))
