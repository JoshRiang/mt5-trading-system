#!/usr/bin/env python3
"""EDGE HARNESS — measures whether a signal has a statistically credible edge
net of REAL spread cost, on REAL MT5 history. Research only; places no orders.

Run:  /home/josh/openbb-env/bin/python research/edge_harness.py [--symbols ...]

EXECUTION MODEL (the anti-lookahead contract, spelled out)
---------------------------------------------------------
A strategy returns plans[i] = a plan decided using ONLY bars 0..i (bar i's close
included, since that close is known when bar i completes). The engine executes
that plan at the OPEN of bar i+1. The entry bar's own high/low is NEVER used to
decide the entry; it is used only for stop/target testing once the trade is open.
No signal is consulted during a hold: exits are pure price.

Fill model (mirrors MT5 for a bid-based feed with constant spread `s`):
  BUY  : entry at ask = B_e + s ; SL/TP are BID levels entry-sl, entry+tp ;
         fills at those bid levels.
  SELL : entry at bid = B_e     ; SL/TP are ASK levels entry+sl, entry-tp ;
         fills at those ask levels (= bid trigger + s).
  => exactly ONE full spread is paid per round trip, and the stop always costs
     exactly 1R in price terms on both sides.
  Intrabar ambiguity (bar mode): if a bar touches BOTH stop and target, the STOP
  is assumed to fill first. The trailing stop is updated at bar END and tested
  with the PREVIOUS bar's trail level, so no intrabar path is assumed.
  Timeout exits fill at the bar close.
  Consequence: bar-mode fills at stop/target levels are mildly OPTIMISTIC on
  price, which is why the reported verdicts lean on exp_R_net and its CI, not on
  the gross figure.

SPREAD: taken from the real per-symbol mt5.symbol_info spread (see probe_out.txt)
and cross-checked against the median `spread` column of the exported M1 bars and
the bid/ask of the exported ticks.

METRICS per (signal, symbol, timeframe): n, win rate, avg win R, avg loss R,
realized RR, expectancy in R gross and net, bootstrap 95% CI on net expectancy,
and max drawdown of a 1%-risk fixed-fractional compounding equity curve.
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_core as hc  # noqa: E402

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
BOOT_N = 5000
BOOT_SEED = 20260929


# ---------------------------------------------------------------------------
# spread resolution
# ---------------------------------------------------------------------------
def spread_price(symbol, bars=None, ticks=None):
    """Real spread in price units. Prefers the live/exported bar spread column,
    falls back to the tick bid/ask median."""
    if ticks is not None and len(ticks["bid"]):
        s = float(np.median(ticks["ask"] - ticks["bid"]))
        if s > 0:
            return s
    if bars is not None and "spread_pts" in bars:
        pts = np.median(bars["spread_pts"][bars["spread_pts"] > 0]) if np.any(bars["spread_pts"] > 0) else 0
        if pts > 0:
            return float(pts) * hc.SPEC[symbol]["point"]
    return float(hc.SPEC[symbol]["spread_pts"]) * hc.SPEC[symbol]["point"]


# ---------------------------------------------------------------------------
# BAR-MODE ENGINE
# ---------------------------------------------------------------------------
def run_bars(bars, plans, spread, start_equity=100.0, risk_frac=0.01,
             collect_trades=False):
    """Walk bars; execute each plan at the NEXT bar's open. Returns metrics dict.

    LOOKAHEAD NOTE: `plans[i]` was computed from bars[0..i]; the fill happens at
    bars[i+1].open. Nothing in the loop below can see beyond index j.

    FILL MODEL (one spread per round trip, charged exactly once)
      * Entry reference price = the mid at the execution bar's open, o[e].
      * SL/TP/trail are MID-space levels: entry -/+ sl_dist, entry +/- tp_dist.
      * Triggers use the bar's high/low as published by MT5 (bid bars).
      * R_gross = mid-to-mid P&L / sl_dist.
        R_net   = R_gross - spread/sl_dist.
      Consequences that make this auditable:
        - a stop always realises exactly -1.00 R net,
        - a target always realises exactly +tp_mult R net,
        - both are checked by test_engine.py (T1/T2/T3/T11).
      The published bid high/low slightly understate the ask side for SELL
      triggers, so SELL stop/target touches are a hair late here; that is a
      deliberate simplification and it is conservative for the TP, generous for
      the SL. Net expectancy (what verdicts use) is unaffected by construction
      because the spread term is exact.
    """
    o, h, l, c, t = bars["open"], bars["high"], bars["low"], bars["close"], bars["time"]
    n = len(c)
    trades = []
    i = 0
    while i < n - 1:
        p = plans[i]
        if p is None:
            i += 1
            continue
        e = i + 1                                  # execution bar
        entry = o[e]
        sl_dist = p.sl_frac * entry
        if not (sl_dist > 0):
            i += 1
            continue
        if p.direction == "BUY":
            sl_px = entry - sl_dist
            tp_px = (entry + p.tp_frac * entry) if p.tp_frac else None
        else:
            sl_px = entry + sl_dist
            tp_px = (entry - p.tp_frac * entry) if p.tp_frac else None
        trail = None
        trig = (p.trail_trig_frac * entry) if p.trail_trig_frac else None
        dist = (p.trail_dist_frac * entry) if p.trail_dist_frac else None
        best = entry
        exit_px = None
        reason = None
        last_j = min(n - 1, e + max(1, p.max_hold_bars) - 1)
        for j in range(e, last_j + 1):
            bj_high, bj_low = h[j], l[j]
            if p.direction == "BUY":
                # stop-first when both are touched inside the same bar
                if bj_low <= sl_px:
                    exit_px, reason = sl_px, "SL"
                elif trail is not None and bj_low <= trail:
                    exit_px, reason = trail, "TRAIL"
                elif tp_px is not None and bj_high >= tp_px:
                    exit_px, reason = tp_px, "TP"
                elif j == last_j:
                    exit_px, reason = c[j], "TIME"
                if exit_px is not None:
                    break
                best = max(best, bj_high)
                if trig is not None and (best - entry) >= trig:
                    nt = best - dist
                    trail = nt if trail is None else max(trail, nt)
            else:
                if bj_high >= sl_px:
                    exit_px, reason = sl_px, "SL"
                elif trail is not None and bj_high >= trail:
                    exit_px, reason = trail, "TRAIL"
                elif tp_px is not None and bj_low <= tp_px:
                    exit_px, reason = tp_px, "TP"
                elif j == last_j:
                    exit_px, reason = c[j], "TIME"
                if exit_px is not None:
                    break
                best = min(best, bj_low)
                if trig is not None and (entry - best) >= trig:
                    nt = best + dist
                    trail = nt if trail is None else min(trail, nt)
        if exit_px is None:
            exit_px, reason = c[last_j], "TIME"
        R_gross = ((exit_px - entry) if p.direction == "BUY" else (entry - exit_px)) / sl_dist
        R_net = R_gross - spread / sl_dist          # exactly one spread per round trip
        trades.append(dict(t_in=int(t[e]), t_out=int(t[last_j]), dir=p.direction,
                           reason=reason, sl_dist=sl_dist, R_gross=R_gross,
                           R_net=R_net, tag=p.tag, bars_held=last_j - e + 1))
        i = last_j + 1                              # one position at a time
    return metrics(trades, start_equity, risk_frac, collect_trades)


# ---------------------------------------------------------------------------
# TICK-MODE ENGINE (no intrabar ambiguity: every tick's bid/ask is known)
# ---------------------------------------------------------------------------
def run_ticks(ticks, plans, bars_for_signal, start_equity=100.0, risk_frac=0.01,
              collect_trades=False, max_hold_sec=150):
    """Tick-accurate walk. `plans` is indexed by BAR; `bars_for_signal` gives the
    bar timestamps used to map each tick to the bar whose plan is live.

    ANTI-LOOKAHEAD: a tick at time t sits inside bar k (bt[k] <= t < bt[k+1]).
    Bar k has NOT closed yet at that moment, so the only actionable signal is
    plan[k-1] - the plan formed at the close of the last COMPLETED bar, which is
    bt[k]. By construction t >= bt[k], so plan[k-1] is genuinely in the past.
    Using plan[k] here would be lookahead (it needs bar k's own close).
    """
    t = ticks["time"]; bid = ticks["bid"]; ask = ticks["ask"]
    bt = bars_for_signal["time"]
    n = len(t)
    trades = []
    i = 0
    while i < n - 1:
        # bar currently forming at tick i; the actionable plan is the PREVIOUS one
        k = int(np.searchsorted(bt, t[i], side="right")) - 1
        p = plans[k - 1] if 0 <= k - 1 < len(plans) else None
        if p is None:
            i += 1
            continue
        e = i
        entry = ask[e] if p.direction == "BUY" else bid[e]     # pay/receive the real side
        if p.direction == "BUY":
            sl_dist = p.sl_frac * entry
            sl_px = entry - sl_dist
            tp_px = (entry + p.tp_frac * entry) if p.tp_frac else None
        else:
            sl_dist = p.sl_frac * entry
            sl_px = entry + sl_dist
            tp_px = (entry - p.tp_frac * entry) if p.tp_frac else None
        trig = (p.trail_trig_frac * entry) if p.trail_trig_frac else None
        dist = (p.trail_dist_frac * entry) if p.trail_dist_frac else None
        trail = None
        best = entry
        exit_px = None
        reason = None
        t_end = t[e] + max_hold_sec
        j = e
        while j < n:
            if p.direction == "BUY":
                cur = bid[j]                    # a BUY is valued/closed at the bid
                if cur <= sl_px:
                    exit_px, reason = cur, "SL"
                elif trail is not None and cur <= trail:
                    exit_px, reason = cur, "TRAIL"
                elif tp_px is not None and cur >= tp_px:
                    exit_px, reason = cur, "TP"
                elif t[j] >= t_end:
                    exit_px, reason = cur, "TIME"
                if exit_px is not None:
                    break
                if cur > best:
                    best = cur
                if trig is not None and (best - entry) >= trig:
                    nt = best - dist
                    trail = nt if trail is None else max(trail, nt)
            else:
                cur = ask[j]                    # a SELL is closed at the ask
                if cur >= sl_px:
                    exit_px, reason = cur, "SL"
                elif trail is not None and cur >= trail:
                    exit_px, reason = cur, "TRAIL"
                elif tp_px is not None and cur <= tp_px:
                    exit_px, reason = cur, "TP"
                elif t[j] >= t_end:
                    exit_px, reason = cur, "TIME"
                if exit_px is not None:
                    break
                if cur < best:
                    best = cur
                if trig is not None and (entry - best) >= trig:
                    nt = best + dist
                    trail = nt if trail is None else min(trail, nt)
            j += 1
        if exit_px is None:
            exit_px, reason = (bid[n - 1] if p.direction == "BUY" else ask[n - 1]), "TIME"
            j = n - 1
        R_gross = ((exit_px - entry) if p.direction == "BUY" else (entry - exit_px)) / sl_dist
        R_net = R_gross - (ticks["ask"][e] - ticks["bid"][e]) / sl_dist
        trades.append(dict(t_in=int(t[e]), t_out=int(t[j]), dir=p.direction,
                           reason=reason, sl_dist=sl_dist, R_gross=R_gross,
                           R_net=R_net, tag=p.tag, bars_held=j - e))
        i = j + 1
    return metrics(trades, start_equity, risk_frac, collect_trades)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def metrics(trades, start_equity=100.0, risk_frac=0.01, collect_trades=False):
    n = len(trades)
    out = {"n": n}
    if n == 0:
        out.update({"wr": None, "avg_win_R": None, "avg_loss_R": None, "rr": None,
                    "exp_R_gross": None, "exp_R_net": None, "ci_lo": None,
                    "ci_hi": None, "max_dd_pct": None, "final_equity": start_equity,
                    "reasons": {}, "avg_spread_R": None})
        if collect_trades:
            out["trades"] = []
        return out
    Rg = np.array([x["R_gross"] for x in trades])
    Rn = np.array([x["R_net"] for x in trades])
    wins = Rn > 0
    wr = float(wins.mean())
    aw = float(Rn[wins].mean()) if wins.any() else 0.0
    al = float(Rn[~wins].mean()) if (~wins).any() else 0.0
    rr = (aw / abs(al)) if al != 0 else None
    # equity curve: fixed-fractional, risk_frac of CURRENT equity per trade
    eq = start_equity
    curve = [eq]
    peak = eq
    mdd = 0.0
    for r in Rn:
        eq *= (1.0 + risk_frac * r)
        if eq <= 0:
            eq = 0.0
        curve.append(eq)
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak)
    # bootstrap CI on net expectancy per trade
    rng = np.random.default_rng(BOOT_SEED)
    idx = rng.integers(0, n, size=(BOOT_N, n))
    means = Rn[idx].mean(axis=1)
    ci_lo, ci_hi = np.percentile(means, [2.5, 97.5])
    reasons = {}
    for x in trades:
        d = reasons.setdefault(x["reason"], {"n": 0, "net": 0.0})
        d["n"] += 1
        d["net"] += x["R_net"]
    out.update({
        "wr": wr, "avg_win_R": aw, "avg_loss_R": al, "rr": rr,
        "exp_R_gross": float(Rg.mean()), "exp_R_net": float(Rn.mean()),
        "ci_lo": float(ci_lo), "ci_hi": float(ci_hi),
        "max_dd_pct": float(mdd * 100.0), "final_equity": float(eq),
        "reasons": {k: {"n": v["n"], "net_R": round(v["net"], 2)}
                    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]["n"])},
        "avg_spread_R": float(np.mean([x["R_gross"] - x["R_net"] for x in trades])),
        "curve": curve,
    })
    if collect_trades:
        out["trades"] = trades
    return out


def verdict(m):
    """Honest label. A result only earns EDGE if the 95% CI excludes zero AND
    the point estimate is positive net of spread."""
    if m["n"] == 0:
        return "NO DATA"
    if m["n"] < 30:
        return f"INCONCLUSIVE (n={m['n']})"
    e, lo, hi = m["exp_R_net"], m["ci_lo"], m["ci_hi"]
    if lo > 0:
        return "EDGE"
    if hi < 0:
        return "NEGATIVE"
    return "NO EDGE (CI spans 0)"


def fmt_row(name, sym, tf, m):
    if m["n"] == 0:
        return f"| {name} | {sym} | {tf} | 0 | - | - | - | - | - | - | - | NO DATA |"
    return ("| {name} | {sym} | {tf} | {n_t} | {wr:.1%} | {aw:+.3f} | {al:+.3f} | "
            "{rr} | {g:+.4f} | {net:+.4f} | [{lo:+.4f},{hi:+.4f}] | {v} |").format(
        name=name, sym=sym, tf=tf, n_t=m["n"], wr=m["wr"], aw=m["avg_win_R"],
        al=m["avg_loss_R"], rr=("-" if m["rr"] is None else f"{m['rr']:.2f}"),
        g=m["exp_R_gross"], net=m["exp_R_net"], lo=m["ci_lo"], hi=m["ci_hi"],
        v=verdict(m))


HEADER = ("| signal | symbol | tf | n | WR | avgW_R | avgL_R | RR | exp_R_gross | "
          "exp_R_net | CI95(net) | verdict |")
SEP = "|---|---|---|---|---|---|---|---|---|---|---|---|"


# ---------------------------------------------------------------------------
# main comparison grid
# ---------------------------------------------------------------------------
def build_grid():
    """(name, plan-fn, timeframe) tuples. Same exit machinery for all."""
    return [
        # (a) control: the live bot's signal + exits, ported to M1
        ("a) kelly_baseline SL0.06% TP3x", lambda b: hc.strategy_kelly_baseline(b), "M1"),
        ("a2) kelly_baseline hold=2bars", lambda b: hc.strategy_kelly_baseline(b, max_hold_bars=2), "M1"),
        # (b) same signal, wider stop / proportional target
        ("b) kelly SL0.3% TP3x", hc.make_kelly_variant(0.003, 3.0, 30, 0.00025, 0.00012,
                                                       "kelly_sl0.3"), "M1"),
        ("b2) kelly SL0.5% TP3x", hc.make_kelly_variant(0.005, 3.0, 60, 0.00025, 0.00012,
                                                        "kelly_sl0.5"), "M1"),
        ("b3) kelly SL0.3% TP1.5x", hc.make_kelly_variant(0.003, 1.5, 30, 0.00025, 0.00012,
                                                          "kelly_sl0.3_tp1.5"), "M1"),
        ("b4) kelly SL0.5% TP2x noTrail", hc.make_kelly_variant(0.005, 2.0, 120, None, None,
                                                                "kelly_sl0.5_notrail"), "M1"),
        # (c) mean reversion
        ("c) MR z>=2.0 n=30", hc.make_mean_reversion(30, 2.0, 3.0, 30), "M1"),
        ("c2) MR z>=2.5 n=60", hc.make_mean_reversion(60, 2.5, 3.0, 60), "M5"),
        ("c3) MR z>=2.0 n=30 M15", hc.make_mean_reversion(30, 2.0, 3.0, 20), "M15"),
        # (d) session / prior-day breakout
        ("d) priorDayBO M5", hc.make_session_breakout(7, 16, 60, 0.002, 2.0), "M5"),
        ("d2) priorDayBO M15", hc.make_session_breakout(7, 16, 40, 0.003, 2.0), "M15"),
        ("d3) asianRangeBO M5", hc.make_asian_breakout(0.5, 2.0, 120), "M5"),
        ("d4) asianRangeBO M15", hc.make_asian_breakout(0.5, 2.0, 60), "M15"),
        # (e) trend following
        ("e) trendEMA50/200 H1", hc.make_trend_ema(50, 200, 2.0, 3.0, 14, 500), "H1"),
        ("e2) trendEMA20/60 H1", hc.make_trend_ema(20, 60, 2.0, 3.0, 14, 300), "H1"),
        ("e3) trendEMA20/60 H4", hc.make_trend_ema(20, 60, 2.0, 3.0, 14, 200), "H4"),
        ("e4) donchian50 H4", hc.make_trend_donchian(50, 2.0, 3.0, 14, 1000), "H4"),
        # (f) null control
        ("f) random SL0.2% TP2x", hc.make_random_entry(12345, 0.002, 2.0, 60, 1.0 / 60.0), "M1"),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="XAUUSD,BTCUSD,XAGUSD,USOIL")
    ap.add_argument("--json", default=os.path.join(OUT_DIR, "results.json"))
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    grid = build_grid()
    bar_cache = {}
    rows = []
    raw = {}
    print(HEADER)
    print(SEP)
    for name, fn, tf in grid:
        for sym in symbols:
            key = (sym, tf)
            if key not in bar_cache:
                try:
                    bar_cache[key] = hc.load_bars(sym, tf)
                except FileNotFoundError:
                    bar_cache[key] = None
            bars = bar_cache[key]
            if bars is None or len(bars["time"]) < 300:
                continue
            sp = spread_price(sym, bars=bars)
            plans = fn(bars)
            m = run_bars(bars, plans, sp, collect_trades=False)
            m.pop("curve", None)
            line = fmt_row(name, sym, tf, m)
            print(line)
            rows.append(line)
            raw[f"{name}|{sym}|{tf}"] = {"spread_price": sp, "bars": len(bars),
                                         "first": int(bars["time"][0]),
                                         "last": int(bars["time"][-1]), **m}
            sys.stdout.flush()

    with open(args.json, "w") as fh:
        json.dump({"generated_utc": datetime.now(timezone.utc).isoformat(),
                   "bootstrap_n": BOOT_N, "rows": raw}, fh, indent=1, default=str)
    print(f"\nJSON -> {args.json}")


if __name__ == "__main__":
    main()
