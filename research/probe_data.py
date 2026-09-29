"""PROBE MT5 data availability (research only, READ-ONLY, no orders).

Prints per-symbol spec (spread/digits/contract/tick value) and the ACTUAL
earliest/latest M1 bar and tick timestamps the broker will serve, so we can
choose an honest export window instead of assuming one.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5

SYMS = ["XAUUSD", "BTCUSD", "XAGUSD", "USOIL", "UKOIL", "EURUSD"]

login = int(os.environ["MT5_LOGIN"])
password = os.environ["MT5_PASSWORD"]
server = os.environ["MT5_SERVER"]
path = os.environ.get("MT5_PATH", r"C:\Program Files\Exness MT5 6180\terminal64.exe")

if not mt5.initialize(path=path, login=login, password=password, server=server, timeout=60000):
    print("INIT_FAIL", mt5.last_error())
    sys.exit(3)

ai = mt5.account_info()
print(f"ACCOUNT login=<demo-account> server={ai.server} trade_mode={ai.trade_mode} "
      f"currency={ai.currency} balance={ai.balance} equity={ai.equity}")

now = datetime.now(timezone.utc)
print(f"NOW_UTC {now.isoformat()}")

for s in SYMS:
    ok = mt5.symbol_select(s, True)
    si = mt5.symbol_info(s)
    if si is None:
        print(f"{s}: NO symbol_info (select={ok}) err={mt5.last_error()}")
        continue
    tk = mt5.symbol_info_tick(s)
    print(f"\n=== {s} ===")
    print(f"  digits={si.digits} spread={si.spread} point={si.point} "
          f"contract={si.trade_contract_size} tick_value={si.trade_tick_value} "
          f"tick_size={si.trade_tick_size} vol_min={si.volume_min} "
          f"vol_step={si.volume_step} stops_level={si.trade_stops_level} "
          f"freeze={si.trade_freeze_level} filling_mode={si.filling_mode}")
    if tk is not None:
        print(f"  tick bid={tk.bid} ask={tk.ask} spread_abs={tk.ask - tk.bid:.6f} time={tk.time}")
    # M1 availability: walk back in ~10 day chunks to find earliest served bar
    for days in (30, 90, 180, 365, 730):
        frm = now - timedelta(days=days)
        r = mt5.copy_rates_range(s, mt5.TIMEFRAME_M1, frm, now)
        n = 0 if r is None else len(r)
        if n:
            first = datetime.fromtimestamp(int(r[0]["time"]), tz=timezone.utc)
            last = datetime.fromtimestamp(int(r[-1]["time"]), tz=timezone.utc)
            print(f"  M1 {days}d: n={n} first={first.isoformat()} last={last.isoformat()}")
        else:
            print(f"  M1 {days}d: n=0 err={mt5.last_error()}")
    # ticks
    for days in (1, 3, 7):
        frm = now - timedelta(days=days)
        t = mt5.copy_ticks_range(s, frm, now, mt5.COPY_TICKS_ALL)
        n = 0 if t is None else len(t)
        if n:
            first = datetime.fromtimestamp(int(t[0]["time"]), tz=timezone.utc)
            last = datetime.fromtimestamp(int(t[-1]["time"]), tz=timezone.utc)
            print(f"  TICKS {days}d: n={n} first={first.isoformat()} last={last.isoformat()}")
        else:
            print(f"  TICKS {days}d: n=0 err={mt5.last_error()}")

mt5.shutdown()
print("\nPROBE_DONE")
