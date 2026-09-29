"""EXPORT real MT5 history to CSV for the edge-research harness. READ-ONLY.

No orders. Never prints credentials. Writes only under research/data/.

What it exports (per symbol), and it PRINTS the actual rows + date range so the
sample size is auditable rather than assumed:
  * M1  bars, largest working count-based window (broker caps out before 120k)
  * M5  bars
  * M15 bars
  * H1  bars
  * H4  bars
  * ticks (COPY_TICKS_ALL) for the last N days, bid/ask preserved so the harness
    can charge the REAL spread on both legs instead of modelling it.

MT5 quirks learned from probing (do not "fix" these):
  - copy_rates_range with a window older than ~90 days returns (-2 Invalid params)
    -> the broker simply does not serve it. Older history is only reachable via
       the count-based copy_rates_from with a big count, and that also caps out.
  - A count above the terminal's cached depth returns (-2) rather than a short
    array, so we probe descending counts and take the first that works.
"""
import os
import sys
import csv
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5

SYMS = ["XAUUSD", "BTCUSD", "XAGUSD", "USOIL"]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

login = int(os.environ["MT5_LOGIN"])
password = os.environ["MT5_PASSWORD"]
server = os.environ["MT5_SERVER"]
path = os.environ.get("MT5_PATH", r"C:\Program Files\Exness MT5 6180\terminal64.exe")

if not mt5.initialize(path=path, login=login, password=password, server=server, timeout=60000):
    print("INIT_FAIL", mt5.last_error())
    sys.exit(3)

now = datetime.now(timezone.utc)
print(f"EXPORT_START now_utc={now.isoformat()}")
summary = []

TFS = [("M1", mt5.TIMEFRAME_M1), ("M5", mt5.TIMEFRAME_M5), ("M15", mt5.TIMEFRAME_M15),
       ("H1", mt5.TIMEFRAME_H1), ("H4", mt5.TIMEFRAME_H4)]
COUNTS = [60000, 30000, 10000, 5000, 2000, 1000]


def fetch_rates(sym, tf, counts=COUNTS):
    """Largest working count-based window, descending. Returns (name, arr, count)."""
    for c in counts:
        r = mt5.copy_rates_from(sym, tf, now, c)
        if r is not None and len(r) > 0:
            return c, r
    return None, None


for sym in SYMS:
    ok = mt5.symbol_select(sym, True)
    si = mt5.symbol_info(sym)
    if si is None:
        print(f"SKIP {sym} no symbol_info select={ok}")
        continue
    # live spread right now, for the record (used by the bar-mode spread model)
    tk = mt5.symbol_info_tick(sym)
    live_spread = (tk.ask - tk.bid) if tk is not None else float(si.spread) * si.point
    print(f"\n### {sym} digits={si.digits} point={si.point} contract={si.trade_contract_size} "
          f"tick_value={si.trade_tick_value} live_spread={live_spread:.6f} "
          f"live_bid={(tk.bid if tk else 0)} live_ask={(tk.ask if tk else 0)}")

    for tfname, tf in TFS:
        used, arr = fetch_rates(sym, tf)
        if arr is None:
            print(f"  {tfname}: NO DATA (all counts failed, last_err={mt5.last_error()})")
            continue
        fn = f"{OUT}\\{sym}_{tfname}.csv"
        with open(fn, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"])
            for r in arr:
                w.writerow([int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]),
                            float(r["close"]), int(r["tick_volume"]), int(r["spread"]),
                            int(r["real_volume"])])
        f = datetime.fromtimestamp(int(arr[0]["time"]), tz=timezone.utc)
        l = datetime.fromtimestamp(int(arr[-1]["time"]), tz=timezone.utc)
        print(f"  {tfname}: n={len(arr)} (requested {used}) {f.isoformat()} -> {l.isoformat()} -> {fn}")
        summary.append((sym, tfname, len(arr), f.isoformat(), l.isoformat()))

    # ---- ticks: 7 days, bid/ask preserved ----
    for days in (7, 3, 1):
        frm = now - timedelta(days=days)
        t = mt5.copy_ticks_range(sym, frm, now, mt5.COPY_TICKS_ALL)
        if t is not None and len(t) > 0:
            break
    if t is None or len(t) == 0:
        print(f"  TICKS: NO DATA err={mt5.last_error()}")
        continue
    fn = f"{OUT}\\{sym}_TICKS_{days}d.csv"
    with open(fn, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "bid", "ask", "last", "volume", "flags"])
        for r in t:
            w.writerow([int(r["time"]), float(r["bid"]), float(r["ask"]),
                        float(r["last"]), float(r["volume"]), int(r["flags"])])
    f = datetime.fromtimestamp(int(t[0]["time"]), tz=timezone.utc)
    l = datetime.fromtimestamp(int(t[-1]["time"]), tz=timezone.utc)
    print(f"  TICKS({days}d): n={len(t)} {f.isoformat()} -> {l.isoformat()} -> {fn}")
    summary.append((sym, f"TICKS_{days}d", len(t), f.isoformat(), l.isoformat()))

mt5.shutdown()

print("\n===== EXPORT SUMMARY =====")
for row in summary:
    print(f"{row[0]:8s} {row[1]:9s} n={row[2]:>9d}  {row[3]} -> {row[4]}")
with open(f"{OUT}\\EXPORT_MANIFEST.txt", "w") as fh:
    fh.write(f"exported_at_utc={now.isoformat()}\n")
    fh.write("symbol,tf,rows,first_utc,last_utc\n")
    for row in summary:
        fh.write(",".join(str(x) for x in row) + "\n")
print("EXPORT_DONE")
