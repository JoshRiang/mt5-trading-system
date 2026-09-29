#!/usr/bin/env python3
"""Self-tests for the edge harness engine. Synthetic data with KNOWN answers,
so the engine is verified before any real-data conclusion is drawn.

Run: python3 research/test_engine.py   (needs numpy; no CSV data required)
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_core as hc  # noqa: E402
import edge_harness as eh  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        FAILS.append(name)


def mk(o, h, l, c, t0=1700000000):
    o = np.array(o, float); h = np.array(h, float)
    l = np.array(l, float); c = np.array(c, float)
    t = np.arange(len(c)) * 60 + t0
    return {"time": t, "open": o, "high": h, "low": l, "close": c,
            "tick_volume": np.ones(len(c)), "spread_pts": np.zeros(len(c))}


# ---------------------------------------------------------------- TEST 1
# A pure BUY with SL 1% and TP 3x, entry at bar1 open, price rises to hit TP.
# Hand-computed expectation:
#   bars: [100, 100, 103] ; entry at bar1 open = 100 ; spread 0
#   sl_dist = 1.0 ; tp = 103 -> bar2 high 103 hits TP -> R_gross = +3, R_net = +3
def test_tp_hit():
    b = mk(o=[100, 100, 100], h=[100, 101, 103], l=[100, 99, 100], c=[100, 100, 103])
    plans = [None, hc.Plan("BUY", 0.01, 0.03, 5), None]
    m = eh.run_bars(b, plans, spread=0.0, collect_trades=True)
    tr = m["trades"][0]
    check("T1 tp hit R_gross=+3", abs(tr["R_gross"] - 3.0) < 1e-9, f"got {tr['R_gross']}")
    check("T1 reason=TP", tr["reason"] == "TP", tr["reason"])


# ---------------------------------------------------------------- TEST 2
# Spread accounting: with sl_dist=1.0 and spread=1.0 the round trip must cost
# EXACTLY 1R. Checked on both a winning and a losing trade.
def test_spread_cost():
    b = mk(o=[100, 100, 100], h=[100, 101, 103], l=[100, 99, 100], c=[100, 100, 103])
    plans = [None, hc.Plan("BUY", 0.01, 0.03, 5), None]
    m0 = eh.run_bars(b, plans, spread=0.0)
    m1 = eh.run_bars(b, plans, spread=1.0)
    d = m0["exp_R_net"] - m1["exp_R_net"]
    check("T2 spread costs exactly 1R on a TP trade", abs(d - 1.0) < 1e-9, f"delta={d}")
    # losing trade: price falls through the stop. With spread=1.0 and sl_dist=1.0
    # the correct net is -2.00R: -1R of price plus 1R of spread.
    b2 = mk(o=[100, 100, 100], h=[100, 101, 101], l=[100, 98, 98], c=[100, 99, 99])
    plans2 = [None, hc.Plan("BUY", 0.01, 0.03, 5), None]
    m2 = eh.run_bars(b2, plans2, spread=0.0)
    m3 = eh.run_bars(b2, plans2, spread=1.0)
    check("T2 zero-spread stop realises exactly -1.00R", abs(m2["exp_R_net"] + 1.0) < 1e-9,
          f"got {m2['exp_R_net']}")
    check("T2 with 1R spread the stop realises exactly -2.00R", abs(m3["exp_R_net"] + 2.0) < 1e-9,
          f"got {m3['exp_R_net']}")
    check("T2 spread costs exactly 1R on a SL trade",
          abs((m2["exp_R_net"] - m3["exp_R_net"]) - 1.0) < 1e-9)


# ---------------------------------------------------------------- TEST 3
# SELL with SL: price rises through the stop. R_net must be exactly -1.
def test_sell_sl():
    b = mk(o=[100, 100, 100], h=[100, 101, 103], l=[100, 99, 100], c=[100, 100, 103])
    plans = [None, hc.Plan("SELL", 0.01, 0.03, 5), None]
    m = eh.run_bars(b, plans, spread=0.0, collect_trades=True)
    tr = m["trades"][0]
    check("T3 sell SL R_gross=-1", abs(tr["R_gross"] + 1.0) < 1e-9, f"got {tr['R_gross']}")
    check("T3 sell SL reason=SL", tr["reason"] == "SL", tr["reason"])


# ---------------------------------------------------------------- TEST 4
# ANTI-LOOKAHEAD: a plan at bar i must NOT be able to trade on bar i's own high.
# Craft bars where bar 0's close triggers a plan and bar 0's HIGH would already
# be at the TP, but the NEXT bar opens flat and then falls to the SL.
# If the engine (wrongly) executed on bar 0, it would book a TP win.
def test_no_lookahead():
    # plan at index 0; execution must be bar 1 open = 100
    b = mk(o=[100, 100, 100, 100], h=[110, 100, 100, 100], l=[100, 99, 98, 97],
           c=[100, 100, 98, 97])
    plans = [hc.Plan("BUY", 0.01, 0.03, 5), None, None, None]
    m = eh.run_bars(b, plans, spread=0.0, collect_trades=True)
    tr = m["trades"][0]
    # bar1 open=100 -> sl=99 -> bar1 low=99 touches SL -> -1R
    check("T4 executes at NEXT bar open (not the signal bar)", abs(tr["R_gross"] + 1.0) < 1e-9,
          f"got {tr['R_gross']} (a +3 would mean it used the signal bar's high)")


# ---------------------------------------------------------------- TEST 5
# One-position-at-a-time: consecutive plans must not overlap.
def test_no_overlap():
    n = 12
    o = np.full(n, 100.0); h = np.full(n, 100.5); l = np.full(n, 99.5); c = np.full(n, 100.0)
    b = mk(o, h, l, c)
    plans = [hc.Plan("BUY", 0.01, 0.03, 4) for _ in range(n)]
    m = eh.run_bars(b, plans, spread=0.0, collect_trades=True)
    spans = [(t["t_in"], t["t_out"]) for t in m["trades"]]
    ok = all(spans[i][1] < spans[i + 1][0] for i in range(len(spans) - 1))
    check("T5 no overlapping positions", ok, f"n={len(spans)}")


# ---------------------------------------------------------------- TEST 6
# Timeout exit fills at close and R is bounded by hold length.
def test_timeout():
    n = 6
    o = np.full(n, 100.0); h = np.full(n, 100.1); l = np.full(n, 99.9)
    c = np.array([100, 100, 100, 100, 100, 101.0])
    b = mk(o, h, l, c)
    plans = [None, hc.Plan("BUY", 0.01, None, 2), None, None, None, None]
    m = eh.run_bars(b, plans, spread=0.0, collect_trades=True)
    tr = m["trades"][0]
    # entry bar1 open=100, sl=99, hold 2 bars -> exit at bar2 close=100 -> 0R
    check("T6 timeout exits at close of last hold bar", tr["reason"] == "TIME", tr["reason"])
    check("T6 timeout R=0", abs(tr["R_gross"]) < 1e-9, f"got {tr['R_gross']}")


# ---------------------------------------------------------------- TEST 7
# Trailing stop: trail must never move against the position, and the trail
# level tested on bar j is the one set at bar j-1 (no intrabar path assumed).
def test_trail_monotone():
    o = np.array([100, 100, 100, 100, 100, 100], float)
    h = np.array([100, 102, 104, 105, 106, 106], float)
    l = np.array([100, 100, 103, 104, 105, 99], float)
    c = np.array([100, 101, 104, 105, 106, 99], float)
    b = mk(o, h, l, c)
    # trigger 0.5%, dist 0.25% ; sl 2% so only the trail can exit
    plans = [None, hc.Plan("BUY", 0.02, None, 10, 0.005, 0.0025), None, None, None, None]
    m = eh.run_bars(b, plans, spread=0.0, collect_trades=True)
    tr = m["trades"][0]
    check("T7 trail exit fires", tr["reason"] == "TRAIL", tr["reason"])
    check("T7 trail exit is profitable", tr["R_gross"] > 0, f"got {tr['R_gross']:.3f}")


# ---------------------------------------------------------------- TEST 8
# Bootstrap CI behaviour: must contain the sample mean, span zero for a
# zero-mean sample, and shrink as n grows.
def test_bootstrap():
    rng = np.random.default_rng(7)
    # EXACTLY zero-mean sample: centred so the "no edge" claim is about the
    # bootstrap, not about a lucky draw.
    z = rng.normal(0.0, 1.0, 300); z -= z.mean()
    noise = [dict(R_gross=float(x), R_net=float(x), reason="TP", tag="t") for x in z]
    mn = eh.metrics(noise)
    check("T8 zero-mean sample => CI spans 0", mn["ci_lo"] < 0 < mn["ci_hi"],
          f"[{mn['ci_lo']:.3f},{mn['ci_hi']:.3f}]")
    check("T8 zero-mean sample => verdict NO EDGE", "NO EDGE" in eh.verdict(mn), eh.verdict(mn))
    check("T8 CI brackets the sample mean",
          mn["ci_lo"] <= mn["exp_R_net"] <= mn["ci_hi"])
    # same distribution, more data => narrower CI
    z2 = rng.normal(0.0, 1.0, 6000); z2 -= z2.mean()
    m2 = eh.metrics([dict(R_gross=float(x), R_net=float(x), reason="TP", tag="t") for x in z2])
    w1 = mn["ci_hi"] - mn["ci_lo"]; w2 = m2["ci_hi"] - m2["ci_lo"]
    check("T8 CI shrinks with n", w2 < w1, f"n=300 w={w1:.3f}  n=6000 w={w2:.3f}")
    # a real positive edge with enough n must be labelled EDGE
    edge = [dict(R_gross=0.3 + float(x), R_net=0.3 + float(x), reason="TP", tag="t")
            for x in rng.normal(0, 1.0, 2000)]
    me = eh.metrics(edge)
    check("T8 genuine +0.3R edge with n=2000 => EDGE", eh.verdict(me) == "EDGE", eh.verdict(me))
    # n below the 30-trade floor must be refused a verdict, however pretty the mean
    tiny = edge[:20]
    mt = eh.metrics(tiny)
    check("T8 n=20 is refused an EDGE verdict", eh.verdict(mt).startswith("INCONCLUSIVE"),
          eh.verdict(mt))
    # a NEGATIVE edge must be caught as negative, not called 'no edge'
    neg = [dict(R_gross=-0.4 + float(x), R_net=-0.4 + float(x), reason="SL", tag="t")
           for x in rng.normal(0, 1.0, 2000)]
    check("T8 genuine -0.4R edge => NEGATIVE", eh.verdict(eh.metrics(neg)) == "NEGATIVE",
          eh.verdict(eh.metrics(neg)))


# ---------------------------------------------------------------- TEST 9
# Determinism: same input -> same output.
def test_determinism():
    rng = np.random.default_rng(3)
    c = 100 * np.cumprod(1 + rng.normal(0, 0.002, 500))
    b = mk(c * 0.9999, c * 1.001, c * 0.999, c)
    fn = hc.make_mean_reversion(30, 2.0, 3.0, 30)
    p1 = fn(b); p2 = fn(b)
    m1 = eh.run_bars(b, p1, 0.01); m2 = eh.run_bars(b, p2, 0.01)
    check("T9 deterministic", m1["exp_R_net"] == m2["exp_R_net"], f"{m1['exp_R_net']}")


# ---------------------------------------------------------------- TEST 10
# Random control: the NULL. In BAR mode the stop-first intrabar rule makes the
# engine deliberately CONSERVATIVE, so the random control's gross expectancy is
# slightly negative rather than exactly zero. The tests assert that property
# (bounded conservatism, never an invented edge) and that the spread is charged
# exactly once. The random control is then used as the zero line when judging
# real signals on the same timeframe, which is what makes the comparison honest.
def test_random_control():
    gross, net, spr = [], [], []
    for seed in range(40):
        rng = np.random.default_rng(1000 + seed)
        c = 100 * np.cumprod(1 + rng.normal(0, 0.001, 3000))
        b = mk(c * 0.9999, c * 1.001, c * 0.999, c)
        plans = hc.make_random_entry(seed, 0.002, 2.0, 60, 1 / 60.0)(b)
        m = eh.run_bars(b, plans, 0.02)     # 0.02 spread on a 0.2% stop = 0.1R/rt
        if m["n"] > 0:
            gross.append(m["exp_R_gross"]); net.append(m["exp_R_net"])
            spr.append(m["avg_spread_R"])
    g = np.array(gross); nn = np.array(net); s = np.array(spr)
    check("T10 random control gross is never POSITIVE (engine cannot invent edge)",
          g.mean() < 0, f"mean={g.mean():+.4f}")
    check("T10 random-control conservatism is bounded (|gross| < 0.25R)",
          abs(g.mean()) < 0.25, f"mean={g.mean():+.4f}")
    check("T10 random net is negative on average", nn.mean() < 0, f"net={nn.mean():+.4f}")
    check("T10 net == gross - spread_R exactly",
          np.allclose(nn, g - s, atol=1e-12), f"max|diff|={np.abs(nn - (g - s)).max():.2e}")


# ---------------------------------------------------------------- TEST 11
# Spread-in-R arithmetic reported by metrics equals spread/sl_dist exactly.
def test_spread_R_reported():
    b = mk(o=[100, 100, 100], h=[100, 100.1, 100.2], l=[100, 99.9, 100.0],
           c=[100, 100, 100])
    plans = [None, hc.Plan("BUY", 0.01, 0.03, 3), None]
    m = eh.run_bars(b, plans, spread=0.05, collect_trades=True)
    check("T11 avg_spread_R == spread/sl_dist", abs(m["avg_spread_R"] - 0.05 / 1.0) < 1e-12,
          f"got {m['avg_spread_R']}")


# ---------------------------------------------------------------- TEST 12
# TICK MODE: the spread must be paid for real on both legs, and the entry must
# be at the correct side (BUY pays ask, SELL receives bid).
def test_tick_mode():
    # bars open at 0 and 60. A tick at t=61 sits in bar 1, so the actionable
    # plan is plan[0] (formed at bar 0's close = t=60).
    bt = np.array([0, 60, 120], dtype=np.int64)
    bars = {"time": bt}
    # a BUY at ask=100.5, price rises so the bid crosses the TP at 100.5*1.02
    ticks = {"time": np.array([61, 62, 63, 64], dtype=np.int64),
             "bid": np.array([100.0, 101.5, 102.6, 103.0]),
             "ask": np.array([100.5, 102.0, 103.1, 103.5])}
    plans = [hc.Plan("BUY", 0.01, 0.02, 5), None, None]
    m = eh.run_ticks(ticks, plans, bars, collect_trades=True)
    tr = m["trades"][0]
    check("T12 tick entry pays the ASK (100.5)", abs(tr["sl_dist"] / 0.01 - 100.5) < 1e-9,
          f"entry={tr['sl_dist']/0.01}")
    check("T12 tick TP exit at the BID level",
          tr["reason"] == "TP" and tr["R_gross"] > 1.95,
          f"reason={tr['reason']} R_gross={tr['R_gross']:.4f}")
    # spread charged exactly once, from the real tick bid/ask
    check("T12 spread charged once = 0.5/1.005",
          abs(m["avg_spread_R"] - 0.5 / 1.005) < 1e-6, f"got {m['avg_spread_R']:.6f}")
    # SELL side: entry at bid, exit at ask
    ticks2 = {"time": np.array([61, 62, 63], dtype=np.int64),
              "bid": np.array([100.0, 99.0, 98.0]),
              "ask": np.array([100.5, 99.5, 98.5])}
    plans2 = [hc.Plan("SELL", 0.01, 0.02, 5), None, None]
    m2 = eh.run_ticks(ticks2, plans2, bars, collect_trades=True)
    tr2 = m2["trades"][0]
    check("T12 tick SELL entry receives the BID (100.0)",
          abs(tr2["sl_dist"] / 0.01 - 100.0) < 1e-9, f"entry={tr2['sl_dist']/0.01}")
    check("T12 tick SELL profits as price falls", tr2["R_gross"] > 1.0,
          f"R_gross={tr2['R_gross']:.4f}")


# ---------------------------------------------------------------- TEST 13
# TICK MODE anti-lookahead: a plan on bar 0 cannot be traded on a tick that
# occurs BEFORE bar 0 has closed.
def test_tick_no_lookahead():
    bt = np.array([0, 60, 120], dtype=np.int64)
    bars = {"time": bt}
    # tick at t=30 is INSIDE bar 0 -> plan[0] must not be tradeable there
    ticks = {"time": np.array([30, 61, 62], dtype=np.int64),
             "bid": np.array([100.0, 100.0, 100.0]),
             "ask": np.array([100.5, 100.5, 100.5])}
    plans = [hc.Plan("BUY", 0.01, 0.02, 5), None, None]
    m = eh.run_ticks(ticks, plans, bars, collect_trades=True)
    ok = m["n"] == 1 and m["trades"][0]["t_in"] == 61
    check("T13 tick mode will not trade inside the signal bar", ok,
          f"n={m['n']} t_in={m['trades'][0]['t_in'] if m['n'] else None}")


if __name__ == "__main__":
    for f in (test_tp_hit, test_spread_cost, test_sell_sl, test_no_lookahead,
              test_no_overlap, test_timeout, test_trail_monotone, test_bootstrap,
              test_determinism, test_random_control, test_spread_R_reported,
              test_tick_mode, test_tick_no_lookahead):
        f()
    print("\n" + ("ALL TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}"))
    sys.exit(1 if FAILS else 0)
