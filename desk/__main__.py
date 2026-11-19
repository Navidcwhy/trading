"""CLI:  python -m desk [--profile NAME] run | once | report [HOURS] | kills [N] | compare | preflight | reset-paper --yes

No --profile = the main desk (config.json). --profile daytrade = the desk in config.daytrade.json.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from collections import Counter

from .config import data_path, load_config, profile_names


def _fmt_ts(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "-"


def _reason_group(r: str) -> str:
    if r.startswith("score "):
        return "analyst score below min"
    if r.startswith("size $"):
        return "size below exchange minimum"
    return re.split(r"[:(]", r)[0].strip()


def report(cfg, since_h: float | None = None):
    from .store import Store
    st = Store(cfg.data_dir / "desk.db")
    since = (dt.datetime.now().timestamp() - since_h * 3600) if since_h else 0
    start = st.get("paper_start_equity", cfg["start_equity_usd"])
    eq_rows = st.q("SELECT * FROM equity ORDER BY ts DESC LIMIT 1")
    opens = st.open_positions()
    cash = st.get("paper_cash")
    print(f"=== DESK REPORT [{cfg.mode.upper()} / {cfg.desk_name}] {dt.datetime.now():%Y-%m-%d %H:%M} ===")
    if eq_rows:
        e = eq_rows[0]
        print(f"Equity ${e['equity']:.2f} (start ${start:.2f}, {(e['equity'] / start - 1) * 100:+.2f}%)  "
              f"cash ${e['cash']:.2f}  last snapshot {_fmt_ts(e['ts'])}")
    elif cash is not None:
        print(f"Cash ${cash:.2f}")
    print(f"\nOPEN POSITIONS ({len(opens)})")
    for p in opens:
        lev = f"  {p.leverage:g}x margin ${p.margin_used:.2f} ({p.unrealized / p.margin_used * 100:+.1f}% on margin)" \
            if p.leverage > 1 else ""
        print(f"  {p.market:5} {p.symbol:12} qty {p.qty:<12g} entry {p.entry_price:<10.6g} last {p.last_price:<10.6g}"
              f" {(p.last_price / p.entry_price - 1) * 100:+6.2f}%  stop {p.stop_price:.6g}"
              f"{' TRAILING' if p.trail_active else ''}  since {_fmt_ts(p.entry_ts)}{lev}")
    closed = st.closed_positions(since)
    wins = [c for c in closed if (c["pnl"] or 0) > 0]
    print(f"\nCLOSED TRADES ({len(closed)}, {len(wins)} winners, P&L ${sum(c['pnl'] or 0 for c in closed):+.4f})")
    for c in closed[-15:]:
        print(f"  {c['market']:5} {c['symbol']:12} {_fmt_ts(c['entry_ts'])} -> {_fmt_ts(c['exit_ts'])} "
              f"{c['entry_price']:.6g} -> {c['exit_price']:.6g}  ${c['pnl']:+.4f}  [{c['exit_reason']}]")
    kills = st.q("SELECT stage, reason FROM kills WHERE ts>=?", (since,))
    print(f"\nKILLS ({len(kills)})")
    by_stage = Counter(k["stage"] for k in kills)
    for stage in ("scout", "filter", "analyst", "executor", "risk"):
        if by_stage.get(stage):
            groups = Counter(_reason_group(k["reason"]) for k in kills if k["stage"] == stage)
            print(f"  {stage:9} {by_stage[stage]:5}  " + ", ".join(f"{g} x{n}" for g, n in groups.most_common(6)))
    errs = st.q("SELECT ts, stage, message FROM events WHERE level='error' AND ts>=? ORDER BY ts DESC LIMIT 5",
                (since,))
    if errs:
        print("\nRECENT ERRORS")
        for e in errs:
            print(f"  {_fmt_ts(e['ts'])} [{e['stage']}] {e['message'][:150]}")


def kills(cfg, n: int):
    from .store import Store
    st = Store(cfg.data_dir / "desk.db")
    for k in reversed(st.q("SELECT * FROM kills ORDER BY id DESC LIMIT ?", (n,))):
        print(f"{_fmt_ts(k['ts'])} c{k['cycle']:<4} {k['stage']:8} {k['market']:5} {k['symbol']:14} {k['reason']}")


def split_profile(argv):
    """Pull `--profile NAME` / `--profile=NAME` out of argv (it may stand anywhere)."""
    rest, profile, i = [], None, 0
    while i < len(argv):
        a = argv[i]
        if a == "--profile":
            if i + 1 >= len(argv):
                raise SystemExit("--profile needs a name, e.g. --profile daytrade")
            profile = argv[i + 1]
            i += 2
        elif a.startswith("--profile="):
            profile = a.split("=", 1)[1]
            i += 1
        else:
            rest.append(a)
            i += 1
    return rest, profile


def desk_stats(st, t0: float, start_equity: float) -> dict:
    """One desk's numbers since t0 (the start of the youngest desk), for `compare`."""
    row = st.q("SELECT equity FROM equity WHERE ts<=? ORDER BY ts DESC LIMIT 1", (t0,))
    eq0 = row[0]["equity"] if row else st.get("paper_start_equity", start_equity)
    last = st.q("SELECT * FROM equity ORDER BY ts DESC LIMIT 1")
    if last:
        eq_now, cash, eq_ts = last[0]["equity"], last[0]["cash"], last[0]["ts"]
    else:
        eq_now, cash, eq_ts = eq0, st.get("paper_cash", eq0), None
    closed = st.closed_positions(t0)
    ent = st.q("SELECT COUNT(*) AS n, COALESCE(SUM(entry_fee), 0) AS fee FROM positions WHERE entry_ts>=?", (t0,))[0]
    hold = [(c["exit_ts"] - c["entry_ts"]) / 3600 for c in closed if c["exit_ts"] and c["entry_ts"]]
    opens = st.open_positions()
    return {"eq0": eq0, "eq_now": eq_now, "eq_ts": eq_ts,
            "change_pct": (eq_now / eq0 - 1) * 100 if eq0 else 0.0,
            "open": len(opens),
            "invested_pct": (1 - cash / eq_now) * 100 if eq_now else 0.0,
            "exposure_pct": sum(p.notional for p in opens) / eq_now * 100 if eq_now else 0.0,
            "levered": sum(1 for p in opens if p.leverage > 1),
            "entries": ent["n"], "closed": len(closed),
            "winners": sum(1 for c in closed if (c["pnl"] or 0) > 0),
            "pnl": sum(c["pnl"] or 0 for c in closed),
            "avg_hold_h": sum(hold) / len(hold) if hold else None,
            "fees": ent["fee"] + sum(c["exit_fee"] or 0 for c in closed),
            "exits": Counter(_reason_group(c["exit_reason"] or "?") for c in closed)}


def compare(cfg):
    """All desks side by side, counted from the moment the youngest one started."""
    from .agents.risk import rules_summary
    from .store import Store
    desks = []
    for name in ["main"] + profile_names():
        prof = "" if name == "main" else name
        db = data_path(cfg.mode, prof) / "desk.db"
        if not db.exists():                     # Store() would create it
            continue
        st = Store(db)
        first = st.q("SELECT MIN(ts) AS t FROM events")[0]["t"]
        if first:
            desks.append((name, prof, st, first))
    if len(desks) < 2:
        print("Nothing to compare: fewer than two desks have data.")
        return
    t0 = max(d[3] for d in desks)
    now = dt.datetime.now()
    print(f"=== DESK COMPARISON [{cfg.mode.upper()}] {now:%Y-%m-%d %H:%M} ===")
    print(f"Counted from {_fmt_ts(t0)}, when the youngest desk started ({(now.timestamp() - t0) / 3600:.1f} h ago)\n")
    stats = []
    for name, prof, st, _ in desks:
        c = load_config(profile=prof)
        print(f"  {name + ':':<12}{rules_summary(c.section('risk'))}")
        stats.append(desk_stats(st, t0, c["start_equity_usd"]))
    rows = [("Equity at start", lambda d: f"${d['eq0']:.2f}"),
            ("Equity now", lambda d: f"${d['eq_now']:.2f}"),
            ("Change", lambda d: f"{d['change_pct']:+.2f}%"),
            ("Open positions", lambda d: str(d["open"])),
            ("Invested", lambda d: f"{d['invested_pct']:.0f}%"),
            ("Exposure", lambda d: f"{d['exposure_pct']:.0f}%" + (f" ({d['levered']} lev.)" if d["levered"] else "")),
            ("Entries", lambda d: str(d["entries"])),
            ("Closed trades", lambda d: str(d["closed"])),
            ("Winners", lambda d: f"{d['winners']}/{d['closed']}" if d["closed"] else "-"),
            ("Realised P&L", lambda d: f"${d['pnl']:+.2f}"),
            ("Avg hold", lambda d: f"{d['avg_hold_h']:.1f} h" if d["avg_hold_h"] is not None else "-"),
            ("Fees paid", lambda d: f"${d['fees']:.2f}")]
    print("\n" + " " * 18 + "".join(f"{d[0]:>14}" for d in desks))
    for label, fmt in rows:
        print(f"{label:<18}" + "".join(f"{fmt(d):>14}" for d in stats))
    print("\nExits by reason:")
    for (name, *_), d in zip(desks, stats):
        print(f"  {name + ':':<12}" + (", ".join(f"{r} x{n}" for r, n in d["exits"].most_common()) or "-"))
    print("\n'Change' is the fair number: equity now against equity when the youngest desk started.\n"
          "Realised P&L counts trades closed since then, also ones opened before it.\n"
          "'Invested' is the cash tied up (margin for leveraged trades), 'Exposure' the market value held.")


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    argv = argv if argv is not None else sys.argv[1:]
    argv, profile = split_profile(argv)
    cmd = argv[0] if argv else "help"
    cfg = load_config(profile=profile)
    if cmd == "run":
        from .desk import Desk
        Desk(cfg).run()
    elif cmd == "once":
        from .desk import Desk
        d = Desk(cfg)
        d.claim()
        d.scan_cycle()
        d.risk.monitor()
        d.snapshot_equity()
    elif cmd == "report":
        report(cfg, float(argv[1]) if len(argv) > 1 else None)
    elif cmd == "kills":
        kills(cfg, int(argv[1]) if len(argv) > 1 else 40)
    elif cmd == "compare":
        compare(cfg)
    elif cmd == "preflight":
        from .desk import Desk
        from .preflight import run_preflight
        sys.exit(0 if run_preflight(Desk(cfg)) else 1)
    elif cmd == "reset-paper":
        if cfg.mode != "paper" or "--yes" not in argv:
            print("Usage (paper mode only): python -m desk [--profile NAME] reset-paper --yes")
            return
        from .desk import InstanceLock
        lock = InstanceLock(cfg.data_dir / "desk.lock")
        if not lock.acquire():
            raise SystemExit(f"Desk '{cfg.desk_name}' is running. Stop it before resetting its paper state.")
        lock.release()
        db = cfg.data_dir / "desk.db"
        for f in (db, db.with_suffix(".db-wal"), db.with_suffix(".db-shm")):
            if f.exists():
                f.unlink()
        print(f"Paper state reset [{cfg.desk_name}].")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
