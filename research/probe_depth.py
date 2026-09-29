"""PROBE 2 — how much history can we actually get? READ-ONLY.

M1 90d returned (-2, 'Terminal: Invalid params'). Before concluding the broker
only serves 30 days, try the other API shapes:
  * mt5.copy_rates_from(sym, tf, dt, count)   <- count-based, forces download
  * mt5.copy_rates_range on intermediate windows
  * H1 / H4 / D1 (needed for the trend-following candidate)
  * terminal_info().maxbars
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5

SYMS = ["XAUUSD", "BTCUSD", "XAGUSD", "USOIL", "EURUSD"]

login = int(os.environ["MT5_LOGIN"])
password = os.environ["MT5_PASSWORD"]
server = os.environ["MT5_SERVER"]
path = os.environ.get("MT5_PATH", r"C:\Program Files\Exness MT5 6180\terminal64.exe")

if not mt5.initialize(path=path, login=login, password=password, server=server, timeout=60000):
    print("INIT_FAIL", mt5.last_error())
    sys.exit(3)

ti = mt5.terminal_info()
print(f"TERMINAL maxbars={ti.maxbars} build={ti.build} connected={ti.connected} "
      f"path={ti.data_path}")

now = datetime.now(timezone.utc)
print(f"NOW_UTC {now.isoformat()}")

for s in SYMS:
    mt5.symbol_select(s, True)
    print(f"\n=== {s} ===")
    # count-based from now
    for cnt in (30000, 60000, 120000, 300000):
        r = mt5.copy_rates_from(s, mt5.TIMEFRAME_M1, now, cnt)
        n = 0 if r is None else len(r)
        if n:
            f = datetime.fromtimestamp(int(r[0]["time"]), tz=timezone.utc)
            l = datetime.fromtimestamp(int(r[-1]["time"]), tz=timezone.utc)
            print(f"  M1 from(now,{cnt}): n={n} first={f.isoformat()} last={l.isoformat()}")
        else:
            print(f"  M1 from(now,{cnt}): n=0 err={mt5.last_error()}")
    # range windows
    for d0, d1 in ((45, 30), (60, 30), (90, 60), (180, 90), (365, 180)):
        frm = now - timedelta(days=d0)
        to = now - timedelta(days=d1)
        r = mt5.copy_rates_range(s, mt5.TIMEFRAME_M1, frm, to)
        n = 0 if r is None else len(r)
        if n:
            f = datetime.fromtimestamp(int(r[0]["time"]), tz=timezone.utc)
            l = datetime.fromtimestamp(int(r[-1]["time"]), tz=timezone.utc)
            print(f"  M1 range[{d0}d ago -> {d1}d ago]: n={n} first={f.isoformat()} last={l.isoformat()}")
        else:
            print(f"  M1 range[{d0}d ago -> {d1}d ago]: n=0 err={mt5.last_error()}")
    # higher timeframes (count based)
    for tfname, tf, cnt in (("H1", mt5.TIMEFRAME_H1, 50000),
                            ("H4", mt5.TIMEFRAME_H4, 50000),
                            ("D1", mt5.TIMEFRAME_D1, 20000)):
        r = mt5.copy_rates_from(s, tf, now, cnt)
        n = 0 if r is None else len(r)
        if n:
            f = datetime.fromtimestamp(int(r[0]["time"]), tz=timezone.utc)
            l = datetime.fromtimestamp(int(r[-1]["time"]), tz=timezone.utc)
            print(f"  {tfname} from(now,{cnt}): n={n} first={f.isoformat()} last={l.isoformat()}")
        else:
            print(f"  {tfname} from(now,{cnt}): n=0 err={mt5.last_error()}")

mt5.shutdown()
print("\nPROBE2_DONE")
