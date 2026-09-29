"""PROBE 3 — find working counts for M15/H1/H4 (trend-following candidates). READ-ONLY."""
import os
import sys
from datetime import datetime, timezone

import MetaTrader5 as mt5

SYMS = ["XAUUSD", "BTCUSD", "XAGUSD", "USOIL"]
login = int(os.environ["MT5_LOGIN"]); password = os.environ["MT5_PASSWORD"]
server = os.environ["MT5_SERVER"]
path = os.environ.get("MT5_PATH", r"C:\Program Files\Exness MT5 6180\terminal64.exe")
if not mt5.initialize(path=path, login=login, password=password, server=server, timeout=60000):
    print("INIT_FAIL", mt5.last_error()); sys.exit(3)
now = datetime.now(timezone.utc)
for s in SYMS:
    mt5.symbol_select(s, True)
    print(f"=== {s} ===")
    for tfname, tf in (("M5", mt5.TIMEFRAME_M5), ("M15", mt5.TIMEFRAME_M15),
                       ("H1", mt5.TIMEFRAME_H1), ("H4", mt5.TIMEFRAME_H4)):
        for cnt in (2000, 5000, 10000):
            r = mt5.copy_rates_from(s, tf, now, cnt)
            n = 0 if r is None else len(r)
            if n:
                f = datetime.fromtimestamp(int(r[0]["time"]), tz=timezone.utc)
                l = datetime.fromtimestamp(int(r[-1]["time"]), tz=timezone.utc)
                print(f"  {tfname} cnt={cnt}: n={n} {f.date()} -> {l.date()}")
                break
            else:
                print(f"  {tfname} cnt={cnt}: n=0 err={mt5.last_error()}")
mt5.shutdown(); print("PROBE3_DONE")
