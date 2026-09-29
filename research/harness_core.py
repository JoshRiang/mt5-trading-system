"""Data loading, causal indicators, and the candidate-signal library.

Used by edge_harness.py. Everything here is unit-agnostic: a strategy emits a
per-bar PLAN (direction + exit distances as FRACTIONS of price), and the engine
decides when the trade actually opens and closes.

ANTI-LOOKAHEAD CONTRACT (every strategy obeys it)
  * Indicators at bar i are computed from bars 0..i inclusive (that bar's close
    is known at its close, which is when a bar-close signal is actionable).
  * A plan emitted at bar i executes at the OPEN of bar i+1. The engine enforces
    this; no strategy may reference future bars.
  * Every strategy here is written with explicit index windows so a reviewer can
    check causality by reading the loop bounds.

MT5 quirks encoded (learned by probing, do not "fix"):
  * copy_rates_range returns (-2 Invalid params) for windows older than the
    terminal's cached depth; only count-based copy_rates_from reaches back.
  * MT5 bars are BID-based. The engine therefore charges the spread explicitly
    on the BUY side (entry at bid+spread, exit at bid) and on the SELL side
    (entry at bid, exit at bid+spread) -> exactly one full spread per round trip.
"""

import csv
import math
import os

import numpy as np

# Bar/CSV exports live next to this file (research/data/*.csv). Those files are
# git-ignored (200 MB of M1/H1/tick history); regenerate them with
# research/export_data.py against a live terminal before running edge_harness.py.
# The engine's own self-test (research/test_engine.py) is synthetic and needs no CSVs.
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# From mt5.symbol_info() on 2026-09-29 (see research/probe_out.txt).
SPEC = {
    "XAUUSD": {"digits": 3, "point": 0.001, "contract": 100.0,  "spread_pts": 168, "tick_value": 0.1},
    "BTCUSD": {"digits": 2, "point": 0.01,  "contract": 1.0,    "spread_pts": 700, "tick_value": 0.01},
    "XAGUSD": {"digits": 3, "point": 0.001, "contract": 5000.0, "spread_pts": 21,  "tick_value": 5.0},
    "USOIL":  {"digits": 3, "point": 0.001, "contract": 1000.0, "spread_pts": 14,  "tick_value": 1.0},
}


def load_bars(symbol, tf):
    """Load <sym>_<tf>.csv -> dict of numpy arrays sorted ascending by time."""
    path = f"{DATA}/{symbol}_{tf}.csv"
    t, o, h, l, c, v, sp = [], [], [], [], [], [], []
    with open(path) as fh:
        r = csv.reader(fh)
        next(r)
        for row in r:
            t.append(int(row[0])); o.append(float(row[1])); h.append(float(row[2]))
            l.append(float(row[3])); c.append(float(row[4])); v.append(float(row[5]))
            sp.append(float(row[6]))
    d = {"time": np.array(t, dtype=np.int64), "open": np.array(o), "high": np.array(h),
         "low": np.array(l), "close": np.array(c), "tick_volume": np.array(v),
         "spread_pts": np.array(sp)}
    assert np.all(np.diff(d["time"]) > 0), f"{path} not strictly increasing"
    return d


def load_ticks(symbol):
    """Load <sym>_TICKS_7d.csv -> bid/ask/mid arrays (zero ticks dropped)."""
    path = f"{DATA}/{symbol}_TICKS_7d.csv"
    t, b, a = [], [], []
    with open(path) as fh:
        r = csv.reader(fh)
        next(r)
        for row in r:
            bt, ba = float(row[1]), float(row[2])
            if ba <= 0 or bt <= 0 or ba < bt:   # drop holiday/zero/garbled ticks
                continue
            t.append(int(row[0])); b.append(bt); a.append(ba)
    d = {"time": np.array(t, dtype=np.int64), "bid": np.array(b), "ask": np.array(a)}
    d["mid"] = (d["bid"] + d["ask"]) / 2.0
    assert np.all(np.diff(d["time"]) >= 0), f"{path} time went backwards"
    return d


# ---------------------------------------------------------------- indicators
def ema_series(x, n):
    """EMA, causal: out[i] uses x[0..i] only."""
    k = 2.0 / (n + 1.0)
    out = np.empty(len(x), dtype=float)
    acc = float(x[0])
    out[0] = acc
    for i in range(1, len(x)):
        acc = float(x[i]) * k + acc * (1 - k)
        out[i] = acc
    return out


def rolling_mean_std(x, n):
    """Causal rolling mean / population std, min_periods=n."""
    L = len(x)
    m = np.full(L, np.nan); s = np.full(L, np.nan)
    cs = np.concatenate([[0.0], np.cumsum(x, dtype=float)])
    cs2 = np.concatenate([[0.0], np.cumsum(np.asarray(x, dtype=float) ** 2)])
    for i in range(n - 1, L):
        w = cs[i + 1] - cs[i + 1 - n]
        w2 = cs2[i + 1] - cs2[i + 1 - n]
        mu = w / n
        m[i] = mu
        s[i] = math.sqrt(max(w2 / n - mu * mu, 0.0))
    return m, s


def rolling_max(x, n):
    """Causal rolling max over x[i-n+1..i]."""
    L = len(x)
    out = np.full(L, np.nan)
    for i in range(n - 1, L):
        out[i] = x[i - n + 1:i + 1].max()
    return out


def rolling_min(x, n):
    L = len(x)
    out = np.full(L, np.nan)
    for i in range(n - 1, L):
        out[i] = x[i - n + 1:i + 1].min()
    return out


def atr_series(high, low, close, n):
    """Causal ATR (Wilder via EMA)."""
    tr = np.empty(len(close))
    tr[0] = high[0] - low[0]
    tr[1:] = np.maximum(high[1:] - low[1:],
                        np.maximum(np.abs(high[1:] - close[:-1]),
                                   np.abs(low[1:] - close[:-1])))
    return ema_series(tr, n)


# ------------------------------------------------------------------ plan type
class Plan:
    """What a strategy wants done if a signal fires on this bar.

    sl_frac / tp_frac / trail_*_frac are FRACTIONS OF PRICE (e.g. 0.0006 = 0.06%).
    tp_frac None => no fixed target (exit on trail or timeout).
    """
    __slots__ = ("direction", "sl_frac", "tp_frac", "max_hold_bars",
                 "trail_trig_frac", "trail_dist_frac", "tag")

    def __init__(self, direction, sl_frac, tp_frac=None, max_hold_bars=60,
                 trail_trig_frac=None, trail_dist_frac=None, tag=""):
        self.direction = direction
        self.sl_frac = sl_frac
        self.tp_frac = tp_frac
        self.max_hold_bars = max_hold_bars
        self.trail_trig_frac = trail_trig_frac
        self.trail_dist_frac = trail_dist_frac
        self.tag = tag


# ============================================================================
# (a) BASELINE — gamble_kelly.py signal + exits, ported to bar granularity
# ============================================================================
# Live config (gamble_kelly.py, read 2026-09-29):
#   FAST_EMA=3 SLOW_EMA=8 MIN_SEP_BPS=0.3 MOM_PCT=0.00005 MOM_WINDOW_SEC=10
#   LOOKBACK=60 SPREAD_CAP_PCT=0.00015 TICK_POLL_SEC=1.0
#   SL_PCT=0.0006 TP_MULT=3.0 TRAIL_TRIG_PCT=0.00025 TRAIL_DIST_PCT=0.00012
#   MAX_HOLD_SEC=150
# Ports to M1 bars:
#   momentum(10s)        -> close[i]-close[i-1] on M1 (the closest bar analogue)
#   Donchian 60 samples  -> rolling max/min of the last `lookback` closes
#   SL 0.06%, TP 3x SL   -> sl_frac=0.0006, tp_frac=0.0018
#   trail trig 0.025%    -> trail_trig_frac=0.00025, dist 0.00012
#   MAX_HOLD_SEC=150     -> 3 M1 bars (<=180s; 2 bars <=120s is the sensitivity)
# The live SPREAD_CAP_PCT quote filter is deliberately NOT applied here (it is a
# broker-quote gate, not part of the price signal); its per-symbol pass/fail is
# reported separately in FINDINGS.md.
def strategy_kelly_baseline(bars, sl_frac=0.0006, tp_mult=3.0, max_hold_bars=3,
                            fast=3, slow=8, sep_bps=0.3, mom_pct=0.00005,
                            lookback=60, tag="kelly_baseline"):
    return _ema_donchian_plan(bars, fast, slow, sep_bps, mom_pct, lookback,
                              sl_frac, sl_frac * tp_mult, max_hold_bars,
                              0.00025, 0.00012, tag)


def make_kelly_variant(sl_frac, tp_mult, max_hold_bars, trail_trig_frac,
                       trail_dist_frac, tag):
    """Same EMA/Donchian signal, different risk geometry."""
    def f(bars):
        return _ema_donchian_plan(bars, 3, 8, 0.3, 0.00005, 60, sl_frac,
                                  sl_frac * tp_mult, max_hold_bars,
                                  trail_trig_frac, trail_dist_frac, tag)
    return f


def _ema_donchian_plan(bars, fast, slow, sep_bps, mom_pct, lookback, sl_frac,
                       tp_frac, max_hold_bars, trail_trig_frac, trail_dist_frac, tag):
    c = bars["close"]
    n = len(c)
    fe = ema_series(c, fast)
    se = ema_series(c, slow)
    plans = [None] * n
    start = max(lookback, slow) + 2
    for i in range(start, n):
        sep = (fe[i] - se[i]) / se[i] * 10000.0
        mom = c[i] - c[i - 1]
        mom_min = c[i] * mom_pct
        hi = c[i - lookback + 1:i + 1].max()     # window includes bar i only
        lo = c[i - lookback + 1:i + 1].min()
        d = None
        if fe[i] > se[i] and sep >= sep_bps and mom >= mom_min and c[i] >= hi:
            d = "BUY"
        elif fe[i] < se[i] and sep <= -sep_bps and mom <= -mom_min and c[i] <= lo:
            d = "SELL"
        if d:
            plans[i] = Plan(d, sl_frac, tp_frac, max_hold_bars,
                            trail_trig_frac, trail_dist_frac, tag)
    return plans


# ============================================================================
# (b) wider-SL variants are make_kelly_variant(...) — see edge_harness.py runs
# ============================================================================

# ============================================================================
# (c) MEAN REVERSION — z-score of close vs N-bar mean, exit on reversion
# ============================================================================
# Entry: |z| >= z_entry (z uses the CURRENT bar's close and the trailing window
#        ending at that bar). Direction is AGAINST the move.
# Exit:  target = the rolling mean AS OF THE SIGNAL BAR (frozen, so it is a real
#        reversion target and not a moving goalpost), plus a catastrophe stop at
#        stop_mult * sigma to define the risk unit.
def make_mean_reversion(n_win=30, z_entry=2.0, stop_mult=3.0, max_hold=30, tag=None):
    def f(bars):
        c = bars["close"]
        m, s = rolling_mean_std(c, n_win)
        plans = [None] * len(c)
        tg = tag or f"MR z>={z_entry} n={n_win}"
        for i in range(n_win, len(c)):
            if not np.isfinite(m[i]) or s[i] <= 0 or c[i] <= 0:
                continue
            z = (c[i] - m[i]) / s[i]
            if z <= -z_entry:
                plans[i] = Plan("BUY", stop_mult * s[i] / c[i],
                                abs(m[i] - c[i]) / c[i], max_hold, tag=tg)
            elif z >= z_entry:
                plans[i] = Plan("SELL", stop_mult * s[i] / c[i],
                                abs(m[i] - c[i]) / c[i], max_hold, tag=tg)
        return plans
    return f


# ============================================================================
# (d) SESSION / PRIOR-DAY BREAKOUT
# ============================================================================
# Prior CALENDAR (UTC) day high/low, built only from completed days. Entry on a
# close beyond it, restricted to the London-NY window 07:00-16:00 UTC.
def make_session_breakout(sess_start=7, sess_end=16, max_hold=60, sl_frac=0.002,
                          tp_mult=2.0, tag=None):
    def f(bars):
        t = bars["time"]; c = bars["close"]; h = bars["high"]; l = bars["low"]
        n = len(c)
        day = t // 86400
        pdh = np.full(n, np.nan); pdl = np.full(n, np.nan)
        cur = day[0]; dh = h[0]; dl = l[0]
        prev_h = prev_l = np.nan
        for i in range(1, n):
            if day[i] != cur:
                prev_h, prev_l = dh, dl
                cur = day[i]; dh = h[i]; dl = l[i]
            else:
                dh = max(dh, h[i]); dl = min(dl, l[i])
            pdh[i], pdl[i] = prev_h, prev_l
        plans = [None] * n
        tg = tag or "priorDayBO"
        for i in range(n):
            if not (np.isfinite(pdh[i]) and np.isfinite(pdl[i])):
                continue
            hr = (t[i] % 86400) // 3600
            if not (sess_start <= hr < sess_end):
                continue
            if c[i] > pdh[i]:
                plans[i] = Plan("BUY", sl_frac, sl_frac * tp_mult, max_hold, tag=tg)
            elif c[i] < pdl[i]:
                plans[i] = Plan("SELL", sl_frac, sl_frac * tp_mult, max_hold, tag=tg)
        return plans
    return f


# Asian-range (00:00-07:00 UTC) breakout traded in 07:00-16:00 UTC. The range is
# fully formed before the entry window opens -> causal by construction.
def make_asian_breakout(sl_frac_of_range=0.5, tp_mult=2.0, max_hold=120, tag=None):
    def f(bars):
        t = bars["time"]; c = bars["close"]; h = bars["high"]; l = bars["low"]
        n = len(c)
        day = t // 86400
        rh = np.full(n, np.nan); rl = np.full(n, np.nan)
        cur = day[0]; dh = h[0]; dl = l[0]
        for i in range(1, n):
            hr = (t[i] % 86400) // 3600
            if day[i] != cur:
                cur = day[i]; dh = h[i]; dl = l[i]
            if hr < 7:
                dh = max(dh, h[i]); dl = min(dl, l[i])
            if hr >= 7:
                if i > 0 and hr > 7 and np.isfinite(rh[i - 1]):
                    rh[i], rl[i] = rh[i - 1], rl[i - 1]
                else:
                    rh[i], rl[i] = dh, dl
        plans = [None] * n
        tg = tag or "asianRangeBO"
        for i in range(n):
            if not (np.isfinite(rh[i]) and np.isfinite(rl[i]) and c[i] > 0):
                continue
            hr = (t[i] % 86400) // 3600
            if not (7 <= hr < 16):
                continue
            rng = rh[i] - rl[i]
            if rng <= 0:
                continue
            sl = rng * sl_frac_of_range / c[i]
            if c[i] > rh[i]:
                plans[i] = Plan("BUY", sl, sl * tp_mult, max_hold, tag=tg)
            elif c[i] < rl[i]:
                plans[i] = Plan("SELL", sl, sl * tp_mult, max_hold, tag=tg)
        return plans
    return f


# ============================================================================
# (e) TREND FOLLOWING — longer horizon, ATR stop, trailing exit
# ============================================================================
def make_trend_ema(fast=50, slow=200, sl_atr=2.0, trail_atr=3.0, atr_win=14,
                   max_hold=500, tag=None):
    def f(bars):
        c = bars["close"]; h = bars["high"]; l = bars["low"]
        n = len(c)
        fe = ema_series(c, fast); se = ema_series(c, slow)
        atr = atr_series(h, l, c, atr_win)
        plans = [None] * n
        tg = tag or f"trendEMA{fast}/{slow}"
        for i in range(max(slow, atr_win) + 2, n):
            if atr[i] <= 0 or c[i] <= 0:
                continue
            up = fe[i] > se[i] and fe[i - 1] <= se[i - 1]
            dn = fe[i] < se[i] and fe[i - 1] >= se[i - 1]
            if up:
                plans[i] = Plan("BUY", sl_atr * atr[i] / c[i], None, max_hold,
                                trail_atr * atr[i] / c[i], trail_atr * atr[i] / c[i], tg)
            elif dn:
                plans[i] = Plan("SELL", sl_atr * atr[i] / c[i], None, max_hold,
                                trail_atr * atr[i] / c[i], trail_atr * atr[i] / c[i], tg)
        return plans
    return f


def make_trend_donchian(entry_n=50, sl_atr=2.0, trail_atr=3.0, atr_win=14,
                        max_hold=1000, tag=None):
    """Donchian(entry_n) breakout on the PRIOR bar's channel, ATR stop + trail."""
    def f(bars):
        c = bars["close"]; h = bars["high"]; l = bars["low"]
        n = len(c)
        hi = rolling_max(h, entry_n)
        lo = rolling_min(l, entry_n)
        atr = atr_series(h, l, c, atr_win)
        plans = [None] * n
        tg = tag or f"donchian{entry_n}"
        for i in range(entry_n + atr_win + 2, n):
            if atr[i] <= 0 or c[i] <= 0:
                continue
            if np.isfinite(hi[i - 1]) and c[i] > hi[i - 1]:
                plans[i] = Plan("BUY", sl_atr * atr[i] / c[i], None, max_hold,
                                trail_atr * atr[i] / c[i], trail_atr * atr[i] / c[i], tg)
            elif np.isfinite(lo[i - 1]) and c[i] < lo[i - 1]:
                plans[i] = Plan("SELL", sl_atr * atr[i] / c[i], None, max_hold,
                                trail_atr * atr[i] / c[i], trail_atr * atr[i] / c[i], tg)
        return plans
    return f


# ============================================================================
# (f) NULL CONTROL — random entries under the SAME exit rule
# ============================================================================
def make_random_entry(seed=12345, sl_frac=0.002, tp_mult=2.0, max_hold=60,
                      rate=1.0 / 60.0, tag="random"):
    """Random-direction entries at a given per-bar rate. Same exit machinery.
    If a 'signal' cannot beat this, its edge is zero."""
    def f(bars):
        rng = np.random.default_rng(seed)
        n = len(bars["close"])
        plans = [None] * n
        for i in range(n):
            if rng.random() < rate:
                plans[i] = Plan("BUY" if rng.random() < 0.5 else "SELL",
                                sl_frac, sl_frac * tp_mult, max_hold, tag=tag)
        return plans
    return f
