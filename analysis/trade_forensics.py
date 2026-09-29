#!/usr/bin/env python3
"""trade_forensics.py — reproduce every performance number quoted in README.md.

Reads the committed SQLite ledger (data/trading_bot.db) and prints the same
tables the post-mortem quotes, so a reviewer can verify the README instead of
trusting it. STDLIB ONLY (sqlite3 + statistics) — no numpy, no MT5, no network:

    python3 analysis/trade_forensics.py
    python3 analysis/trade_forensics.py --all          # include the 2026-09-29 session
    python3 analysis/trade_forensics.py --json out.json

WHY THE DEFAULT WINDOW IS `timestamp < 2026-09-23`
--------------------------------------------------
The documented result is the 2026-09-21..22 demo session: 696 closed trades that
took the account from $565.34 to $100.17 and tripped the equity floor. The
ledger keeps accumulating afterwards (the engine was restarted on 2026-09-29 to
probe the Wine/MT5 bridge), so an unbounded query mixes two different regimes.
`--all` shows the whole ledger; the default is the session the README describes.

CONVENTIONS (stated so the numbers are checkable by hand)
  * "closed" = trades.status = 'CLOSED'.
  * win       = pnl > 0. A scratch trade (pnl == 0) counts as a loss, which is
                the conservative convention core/kelly.py uses for Kelly's p.
  * R-multiple = pnl / risk_amount. risk_amount is the risk the engine *intended*
                to take (1% of equity, before broker lot rounding), so R is a
                normalized P&L, not "how many stops I lost". The gap between
                intended and actual risk is measured in section 6.
  * realized payoff b = avg_win / |avg_loss|. The strategy config claims RR 1:3
    (b = 3). What the ledger shows is in section 1.
"""

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

DB_DEFAULT = Path(__file__).resolve().parent.parent / "data" / "trading_bot.db"
SESSION_END = "2026-09-23"          # exclusive upper bound on the documented window

# ---------------------------------------------------------------------------
# Real broker spreads, measured — NOT assumed. Two independent sources agree:
#   * mt5.symbol_info(sym).spread, live, 2026-09-29 (see research/EXPORT_MANIFEST.txt)
#   * median of the `spread` column of the exported M1 bars, which is the same
#     field MT5 stamps on every historical bar.
# Values are in POINTS; multiply by `point` for price units.
# EURUSD was never exported, so it has no measured spread here — left as None
# rather than guessed.
# ---------------------------------------------------------------------------
BROKER_SPREAD = {
    "XAUUSD": {"point": 0.001, "live_pts": 168, "median_m1_pts": 182, "live_mid": 4153.959},
    "BTCUSD": {"point": 0.01,  "live_pts": 700, "median_m1_pts": 700, "live_mid": 84273.86},
    "USOIL":  {"point": 0.001, "live_pts": 14,  "median_m1_pts": 14,  "live_mid": 89.152},
    "XAGUSD": {"point": 0.001, "live_pts": 21,  "median_m1_pts": 21,  "live_mid": 60.848},
}

# Broker contract sizes, read from mt5.symbol_info(sym).trade_contract_size.
CONTRACT_SIZE = {"XAUUSD": 100.0, "BTCUSD": 1.0, "USOIL": 1000.0,
                 "XAGUSD": 5000.0, "EURUSD": 100000.0}

STOP_FRACTION = 0.0006              # SL distance = 0.06% of mid, per the config
INTENDED_RR = 3.0                   # tp_mult in strategies/kelly_momentum.yaml


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def load_trades(db_path, until=None):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    q = "SELECT * FROM trades WHERE status='CLOSED'"
    args = []
    if until:
        q += " AND timestamp < ?"
        args.append(until)
    q += " ORDER BY timestamp, id"
    rows = [dict(r) for r in con.execute(q, args)]
    con.close()
    for r in rows:
        r["R"] = (r["pnl"] / r["risk_amount"]) if r["risk_amount"] else None
        r["sl_dist"] = (abs(r["entry_price"] - r["stop_loss"])
                        if r["stop_loss"] is not None else None)
    return rows


def load_equity(db_path, until=None):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    q = "SELECT ts, equity, balance, bot_tag FROM equity_curve"
    args = []
    if until:
        q += " WHERE ts < ?"
        args.append(until)
    q += " ORDER BY ts"
    rows = [dict(r) for r in con.execute(q, args)]
    con.close()
    return rows


def load_events(db_path):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT ts, event_type, detail, bot_tag FROM risk_events ORDER BY ts")]
    con.close()
    return rows


def load_blotter(db_path):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT ts, symbol, direction, volume, retcode, comment, bot_tag "
        "FROM order_blotter ORDER BY ts")]
    con.close()
    return rows


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def summarize(rows, label="ALL"):
    """Headline stats for a set of trades. Returns a dict (also printed)."""
    n = len(rows)
    if n == 0:
        return {"label": label, "n": 0}
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    net = sum(r["pnl"] for r in rows)
    avg_win = sum(r["pnl"] for r in wins) / len(wins) if wins else 0.0
    avg_loss = sum(r["pnl"] for r in losses) / len(losses) if losses else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss else None
    rs = [r["R"] for r in rows if r["R"] is not None]
    out = {
        "label": label,
        "n": n,
        "wins": len(wins),
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "realized_payoff_b": round(payoff, 4) if payoff else None,
        "breakeven_wr_pct": round(1.0 / (1.0 + payoff) * 100, 2) if payoff else None,
        "net_pnl": round(net, 2),
        "expectancy_usd": round(net / n, 4),
        "expectancy_R": round(sum(rs) / len(rs), 4) if rs else None,
        "first_ts": rows[0]["timestamp"],
        "last_ts": rows[-1]["timestamp"],
    }
    return out


def by_exit_reason(rows):
    out = []
    for reason in sorted({r["exit_reason"] for r in rows}):
        g = [r for r in rows if r["exit_reason"] == reason]
        w = [r for r in g if r["pnl"] > 0]
        out.append({
            "exit_reason": reason,
            "n": len(g),
            "pct_of_trades": round(len(g) / len(rows) * 100, 1),
            "wins": len(w),
            "win_rate_pct": round(len(w) / len(g) * 100, 1),
            "net_pnl": round(sum(r["pnl"] for r in g), 2),
            "avg_pnl": round(sum(r["pnl"] for r in g) / len(g), 3),
        })
    out.sort(key=lambda d: -d["n"])
    return out


def by_field(rows, field):
    out = []
    for key in sorted({r[field] for r in rows}):
        g = [r for r in rows if r[field] == key]
        w = [r for r in g if r["pnl"] > 0]
        rs = [r["R"] for r in g if r["R"] is not None]
        out.append({
            field: key,
            "n": len(g),
            "win_rate_pct": round(len(w) / len(g) * 100, 1),
            "net_pnl": round(sum(r["pnl"] for r in g), 2),
            "expectancy_R": round(sum(rs) / len(rs), 4) if rs else None,
            "avg_risk_usd": round(sum(r["risk_amount"] for r in g) / len(g), 3),
        })
    out.sort(key=lambda d: -d["n"])
    return out


def r_distribution(rows):
    """Where the R-multiples actually land. This is the inverted-RR evidence."""
    rs = [r["R"] for r in rows if r["R"] is not None]
    if not rs:
        return {}
    # method="inclusive" keeps the reported percentiles INSIDE [min, max]. The
    # default ("exclusive") extrapolates, so on a small ledger p5 could come out
    # below min and p95 above max — a percentile outside the data range is wrong
    # and breaks any monotonicity check on the distribution.
    q = statistics.quantiles(rs, n=100, method="inclusive") if len(rs) > 1 else None
    def pct(i):
        return round(q[i], 3) if q else round(rs[0], 3)
    return {
        "n": len(rs),
        "min": round(min(rs), 3),
        "p5": pct(4),
        "p25": pct(24),
        "median": round(statistics.median(rs), 3),
        "p75": pct(74),
        "p95": pct(94),
        "max": round(max(rs), 3),
        "count_ge_+1R": sum(1 for x in rs if x >= 1.0),
        "count_ge_+2R": sum(1 for x in rs if x >= 2.0),
        "count_le_-1R": sum(1 for x in rs if x <= -1.0),
    }


def stop_cluster(rows):
    """TRAIL (the winning engine) vs everything that is a full stop."""
    trail = [r for r in rows if r["exit_reason"] == "TRAIL"]
    stop = [r for r in rows if r["exit_reason"] in ("SL", "BROKER_CLOSE")]
    timeo = [r for r in rows if r["exit_reason"] == "TIME"]
    def block(g):
        # An empty bucket is legitimate (a session may have no SL exits at all).
        # It must report zeros, not None — None crashes the report formatter.
        n = len(g)
        net = sum(r["pnl"] for r in g)
        return {"n": n, "pct": round(n / len(rows) * 100, 1) if rows else 0.0,
                "net_pnl": round(net, 2),
                "avg_pnl": round(net / n, 3) if n else 0.0}
    return {"trail": block(trail), "full_stop": block(stop), "timeout": block(timeo)}


def spread_cost(rows):
    """Spread as a fraction of the 1R stop distance, and its total cost in USD.

    R distance is reconstructed per trade from the trade's own entry price
    (SL is a fixed 0.06% of mid), so this does not depend on the live quote.
    """
    out = []
    for r in rows:
        spec = BROKER_SPREAD.get(r["symbol"])
        if not spec or not r["entry_price"]:
            continue
        spread_price = spec["median_m1_pts"] * spec["point"]
        r_price = r["entry_price"] * STOP_FRACTION
        out.append({
            "symbol": r["symbol"],
            "spread_price": spread_price,
            "r_price": round(r_price, 6),
            "spread_pct_of_R": round(spread_price / r_price * 100, 2),
            "risk_usd": r["risk_amount"],
            "spread_usd": round(spread_price / r_price * r["risk_amount"], 3),
        })
    agg = {}
    for row in out:
        a = agg.setdefault(row["symbol"], {"symbol": row["symbol"], "n": 0,
                                           "spread_usd": 0.0, "pct_of_R": []})
        a["n"] += 1
        a["spread_usd"] += row["spread_usd"]
        a["pct_of_R"].append(row["spread_pct_of_R"])
    table = []
    for a in agg.values():
        table.append({
            "symbol": a["symbol"],
            "n": a["n"],
            "spread_pct_of_R_median": round(statistics.median(a["pct_of_R"]), 2),
            "spread_usd_total": round(a["spread_usd"], 2),
        })
    table.sort(key=lambda d: -d["n"])
    total_usd = sum(t["spread_usd_total"] for t in table)
    net = sum(r["pnl"] for r in rows)
    return {"per_symbol": table, "total_spread_usd": round(total_usd, 2),
            "net_pnl": round(net, 2),
            "spread_as_multiple_of_net_loss": round(abs(total_usd / net), 2) if net else None}


def risk_cap_check(rows, equity_rows):
    """Did the engine actually risk 1% per trade?

    Uses the most recent equity snapshot at or before each trade, then compares
    it against the risk the position REALLY carried:
        lot_size * contract_size * |entry - stop_loss|
    `risk_amount` is what the engine intended; this is what the broker's lot
    granularity delivered.
    """
    ts = [e["ts"] for e in equity_rows]
    eq = [e["equity"] for e in equity_rows]
    import bisect
    per_symbol, breaches, all_pcts = {}, [], []
    for r in rows:
        if not r["sl_dist"] or r["sl_dist"] <= 0:
            continue
        contract = CONTRACT_SIZE.get(r["symbol"])
        if not contract:
            continue
        i = bisect.bisect_left(ts, r["timestamp"]) - 1
        equity = eq[max(0, i)]
        if equity <= 0:
            continue
        actual = r["lot_size"] * contract * r["sl_dist"]
        pct = actual / equity * 100
        all_pcts.append(pct)
        per_symbol.setdefault(r["symbol"], []).append(pct)
        if pct > 1.0:
            breaches.append((r["symbol"], round(pct, 2), round(actual, 2), round(equity, 2)))
    sym_stats = []
    for sym, pcts in sorted(per_symbol.items(), key=lambda kv: -len(kv[1])):
        sym_stats.append({"symbol": sym, "n": len(pcts),
                          "min_pct": round(min(pcts), 2),
                          "median_pct": round(statistics.median(pcts), 2),
                          "max_pct": round(max(pcts), 2)})
    return {
        "trades_measured": len(all_pcts),
        "trades_over_1pct_cap": len(breaches),
        "pct_over_cap": round(len(breaches) / len(all_pcts) * 100, 1) if all_pcts else None,
        "median_risk_pct": round(statistics.median(all_pcts), 2) if all_pcts else None,
        "per_symbol": sym_stats,
        "worst": sorted(breaches, key=lambda b: -b[1])[:5],
    }


def equity_stats(equity_rows):
    if not equity_rows:
        return {}
    peak, mdd = 0.0, 0.0
    for e in equity_rows:
        peak = max(peak, e["equity"])
        if peak:
            mdd = max(mdd, (peak - e["equity"]) / peak)
    return {
        "snapshots": len(equity_rows),
        "first": equity_rows[0],
        "last": equity_rows[-1],
        "peak_equity": round(peak, 2),
        "trough_equity": round(min(e["equity"] for e in equity_rows), 2),
        "max_drawdown_pct": round(mdd * 100, 2),
    }


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def table(headers, rows, fmt=None):
    fmt = fmt or (lambda x: str(x))
    widths = [len(h) for h in headers]
    cells = [[fmt(c) for c in row] for row in rows]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--until", default=SESSION_END,
                    help="exclusive timestamp upper bound (default: %(default)s)")
    ap.add_argument("--all", action="store_true",
                    help="use the whole ledger (no upper bound)")
    ap.add_argument("--json", help="also write the full result set to this path")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"ledger not found: {db}", file=sys.stderr)
        return 2
    until = None if args.all else args.until

    trades = load_trades(db, until)
    if not trades:
        print("no closed trades in window", file=sys.stderr)
        return 2
    equity = load_equity(db, until)
    result = {}

    print(f"ledger : {db}")
    print(f"window : {'ALL' if until is None else f'closed before {until}'}")
    print(f"trades : {len(trades)} closed")
    print()

    head = summarize(trades)
    result["headline"] = head
    print("== 1. headline ==")
    table(["metric", "value"],
          [["closed trades", head["n"]],
           ["wins (pnl>0)", head["wins"]],
           ["win rate", f"{head['win_rate_pct']}%"],
           ["avg win", f"+{head['avg_win']}"],
           ["avg loss", f"{head['avg_loss']}"],
           ["realized payoff b (avg_win/|avg_loss|)", head["realized_payoff_b"]],
           ["config INTENDED RR", f"1:{INTENDED_RR:.0f}  (b={INTENDED_RR:.0f})"],
           ["breakeven WR at realized b", f"{head['breakeven_wr_pct']}%"],
           ["net P&L", f"{head['net_pnl']} USD"],
           ["expectancy per trade", f"{head['expectancy_usd']} USD / {head['expectancy_R']} R"],
           ["window", f"{head['first_ts']}  ->  {head['last_ts']}"]])
    print()

    exits = by_exit_reason(trades)
    result["exit_reasons"] = exits
    print("== 2. by exit reason ==")
    table(["exit_reason", "n", "% of trades", "wins", "win rate", "net USD", "avg USD"],
          [[e["exit_reason"], e["n"], f"{e['pct_of_trades']}%", e["wins"],
            f"{e['win_rate_pct']}%", f"{e['net_pnl']:+.2f}", f"{e['avg_pnl']:+.3f}"]
           for e in exits])
    print()

    print("== 3. by symbol ==")
    syms = by_field(trades, "symbol")
    result["symbols"] = syms
    table(["symbol", "n", "win rate", "net USD", "expectancy R", "avg risk USD"],
          [[s["symbol"], s["n"], f"{s['win_rate_pct']}%", f"{s['net_pnl']:+.2f}",
            f"{s['expectancy_R']:+.4f}", s["avg_risk_usd"]] for s in syms])
    print()

    print("== 4. by engine tag ==")
    tags = by_field(trades, "bot_tag")
    result["bot_tags"] = tags
    table(["bot_tag", "n", "win rate", "net USD", "expectancy R"],
          [[t["bot_tag"], t["n"], f"{t['win_rate_pct']}%", f"{t['net_pnl']:+.2f}",
            f"{t['expectancy_R']:+.4f}"] for t in tags])
    print()

    dist = r_distribution(trades)
    result["r_distribution"] = dist
    print("== 5. R-multiple distribution (pnl / intended risk) ==")
    table(["stat", "value"], [[k, v] for k, v in dist.items()])
    print()

    sc = stop_cluster(trades)
    result["stop_cluster"] = sc
    print("== 6. trail vs full stop ==")
    table(["bucket", "n", "% of trades", "net USD", "avg USD"],
          [[k, v["n"], f"{v['pct']}%", f"{v['net_pnl']:+.2f}", f"{v['avg_pnl']:+.3f}"]
           for k, v in sc.items()])
    print()

    sp = spread_cost(trades)
    result["spread"] = sp
    print("== 7. spread as a fraction of the 1R stop (measured broker spreads) ==")
    table(["symbol", "n", "median spread % of R", "spread USD paid (sum)"],
          [[s["symbol"], s["n"], f"{s['spread_pct_of_R_median']}%",
            f"{s['spread_usd_total']:+.2f}"] for s in sp["per_symbol"]])
    print(f"  total spread paid : {sp['total_spread_usd']:+.2f} USD")
    print(f"  net P&L           : {sp['net_pnl']:+.2f} USD")
    print(f"  => spread is {sp['spread_as_multiple_of_net_loss']}x the entire net loss")
    print()

    cap = risk_cap_check(trades, equity)
    result["risk_cap"] = cap
    print("== 8. did the 1%-per-trade cap actually hold? ==")
    table(["symbol", "n", "min risk %", "median risk %", "max risk %"],
          [[s["symbol"], s["n"], f"{s['min_pct']}%", f"{s['median_pct']}%",
            f"{s['max_pct']}%"] for s in cap["per_symbol"]])
    print(f"  trades risking MORE than 1% of live equity: "
          f"{cap['trades_over_1pct_cap']} / {cap['trades_measured']} "
          f"({cap['pct_over_cap']}%)")
    if cap["worst"]:
        print("  worst offenders (symbol, risk%, risk USD, equity at the time):")
        for w in cap["worst"]:
            print(f"    {w[0]:8} {w[1]:5}%  {w[2]:7} USD  on {w[3]} USD equity")
    print()

    eq = equity_stats(equity)
    result["equity"] = eq
    print("== 9. equity curve ==")
    if eq:
        table(["metric", "value"],
              [["snapshots", eq["snapshots"]],
               ["first", f"{eq['first']['ts']}  equity {eq['first']['equity']}"],
               ["last", f"{eq['last']['ts']}  equity {eq['last']['equity']}"],
               ["peak equity", eq["peak_equity"]],
               ["trough equity", eq["trough_equity"]],
               ["max drawdown", f"{eq['max_drawdown_pct']}%"]])
    print()

    print("== 10. broker rejects in the order blotter (all time) ==")
    blot = load_blotter(db)
    rej = {}
    for b in blot:
        if b["retcode"] not in (10009, 0):
            key = (b["retcode"], b["comment"])
            rej[key] = rej.get(key, 0) + 1
    result["rejects"] = [{"retcode": k[0], "comment": k[1], "n": v}
                         for k, v in sorted(rej.items(), key=lambda kv: -kv[1])]
    table(["retcode", "comment", "n"], [[k[0], k[1], v] for k, v in
                                        sorted(rej.items(), key=lambda kv: -kv[1])])
    print("  (10009 = TRADE_RETCODE_DONE; the fills, not errors)")

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1, default=str))
        print(f"\njson -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
