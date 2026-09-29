"""GAMBLE v3 MULTI-SYMBOL DEMO scalper — DEMO ONLY. Triple safety gate, NO real-money orders.

Expands v2 (BTCUSD-only) to all symbols with LIVE ticks: BTCUSD, XAUUSD,
ETHUSD, EURUSD, XAGUSD, USOIL (all verified ticking 2026-09-21).

Per-symbol strategy = v2 identical: seconds-EMA(3/8) + microstructure
(spread cap + momentum burst + structure break), generalized with
price-relative thresholds (SL_PCT of mid) so one param set fits all scales.
  - Sizing: 1% of equity risk per trade (RISK_PCT=0.01), TP=3x SL (1%:3%),
    vol = risk_usd/(SL_move*contract) clamped to broker [min,max]+MAX_LOT.
  - Per-symbol cap MAX_POS_PER_SYMBOL=1; shared equity floor 100 / target 1000.
  - Every fill/exit/reject/equity-HB also written to the ERP SQLite ledger
    (core/erp.py: order_blotter + trades + equity_curve + risk_events).
    ERP failures NEVER kill trading (try/except around each call).

Run under Wine Windows-Python with MT5_* env sourced from .env (chmod 600).
NEVER log credentials.
"""
import os
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from core.erp import (record_fill, record_exit, record_reject,
                          record_equity, risk_event)
    ERP = True
except Exception as e:
    print(f"ERP_IMPORT_FAIL {e} — continuing without ERP ledger", flush=True)
    ERP = False

# ---- CONFIG (v2 params, generalized price-relative) ----
SYMBOLS = ["BTCUSD", "XAUUSD", "ETHUSD", "EURUSD", "XAGUSD", "USOIL"]
BOT_TAG = "gamble_multi_v3"
EQUITY_FLOOR = 100.0
EQUITY_TARGET = 1000.0
RISK_PCT = 0.01          # 1% of equity per trade per symbol
SL_PCT = 0.0006          # SL distance = 0.06% of mid (≈BTC v2 50/81k)
TP_MULT = 3.0            # 1% risk : 3% reward
TRAIL_TRIG_PCT = 0.00025
TRAIL_DIST_PCT = 0.00012
MOM_PCT = 0.00005        # momentum burst threshold as fraction of mid
SPREAD_CAP_PCT = 0.00015 # microstructure spread cap as fraction of mid
MAX_HOLD_SEC = 150
MAX_TRADES = 500
MAX_RUNTIME_HRS = 12.0
FAST_EMA = 3
SLOW_EMA = 8
MIN_SEP_BPS = 0.3
TICK_POLL_SEC = 1.0
COOLDOWN_SEC = 3
MAX_POS_PER_SYMBOL = 1
MAX_LOT = 1.00
MOM_WINDOW_SEC = 10
LOOKBACK = 60
MAGIC = 20260923

# ---- SAFETY GATE (triple) ----
try:
    import MetaTrader5 as mt5
except ImportError as e:
    print(f"GAMBLE_ABORT paper_fallback no MT5 package: {e}", flush=True)
    sys.exit(2)

login = int(os.environ["MT5_LOGIN"])
password = os.environ["MT5_PASSWORD"]
server = os.environ["MT5_SERVER"]
path = os.environ.get("MT5_PATH", r"C:\Program Files\Exness MT5 6180\terminal64.exe")

DEMO_MODES = {getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0),
              getattr(mt5, "ACCOUNT_TRADE_MODE_CONTEST", 1)}
REAL_MODE = getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2)

ok = mt5.initialize(path=path, login=login, password=password, server=server, timeout=60000)
if not ok:
    print(f"GAMBLE_ABORT init_failed {mt5.last_error()}", flush=True)
    sys.exit(3)

info = mt5.account_info()
if info is None:
    print(f"GAMBLE_ABORT no_account_info {mt5.last_error()}", flush=True)
    sys.exit(4)
ad = info._asdict()
tm = ad.get("trade_mode")
print(f"GATE trade_mode={tm} login={ad.get('login')} server={ad.get('server')} "
      f"balance={ad.get('balance')} equity={ad.get('equity')}", flush=True)
if tm == REAL_MODE or tm not in DEMO_MODES:
    print(f"GAMBLE_ABORT NOT_DEMO trade_mode={tm} — refusing to trade.", flush=True)
    mt5.shutdown()
    sys.exit(5)
if "real" in str(server).lower() and "trial" not in str(server).lower() and "demo" not in str(server).lower():
    print(f"GAMBLE_ABORT server name looks REAL: {server} — refusing.", flush=True)
    mt5.shutdown()
    sys.exit(6)
print("GATE PASS: DEMO confirmed, proceeding.", flush=True)


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def ema(prev, price, n):
    k = 2.0 / (n + 1.0)
    return price * k + prev * (1 - k) if prev is not None else price


def erp(fn, *a, **k):
    if ERP:
        try:
            fn(*a, **k)
        except Exception as e:
            log(f"ERP_FAIL {fn.__name__} {e}")


def close_ticket(sym, ticket, reason):
    poss = mt5.positions_get(ticket=ticket)
    if not poss:
        return None
    d = poss[0]._asdict()
    is_buy = d["type"] == mt5.POSITION_TYPE_BUY
    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        log(f"CLOSE_FAIL sym={sym} ticket={ticket} no_tick")
        return None
    req = {"action": mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": sym,
           "volume": float(d["volume"]),
           "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
           "price": tick.bid if is_buy else tick.ask,
           "deviation": 20, "magic": MAGIC, "comment": f"gmulti-close:{reason}",
           "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
    res = None
    for fill in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        req["type_filling"] = fill
        res = mt5.order_send(req)
        if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            break
    if res is None:
        log(f"CLOSE_FAIL sym={sym} ticket={ticket} no_result err={mt5.last_error()}")
        return None
    log(f"CLOSE sym={sym} ticket={ticket} side={'BUY' if is_buy else 'SELL'} "
        f"vol={d['volume']} open={d['price_open']} retcode={res.retcode} "
        f"price={res.price} comment={res.comment}")
    return d


def close_all(reason):
    for sym in SYMBOLS:
        for p in (mt5.positions_get(symbol=sym) or ()):
            close_ticket(sym, p.ticket, reason)


# ---- per-symbol specs + state ----
spec = {}
for s in SYMBOLS:
    mt5.symbol_select(s, True)
    si = mt5.symbol_info(s)
    if si is None:
        log(f"SPEC_FAIL {s} no symbol_info — SKIPPED")
        continue
    spec[s] = dict(vol_min=si.volume_min, vol_step=si.volume_step,
                   vol_max=si.volume_max, contract=si.trade_contract_size,
                   digits=si.digits)
    log(f"SPEC {s} vol_min={si.volume_min} step={si.volume_step} max={si.volume_max} "
        f"contract={si.trade_contract_size} digits={si.digits} spread={si.spread}")
SYMBOLS = [s for s in SYMBOLS if s in spec]
if not SYMBOLS:
    log("GAMBLE_ABORT no symbols available")
    mt5.shutdown()
    sys.exit(7)

st = {s: dict(mids=deque(maxlen=400), hist=deque(maxlen=400), fast=None, slow=None,
              ticket=None, direction=None, price=0.0, vol=0.0, t0=0.0,
              trail=None, last_exit=0.0) for s in SYMBOLS}
n_trades = 0
wins = losses = 0
realized = 0.0
last_hb = 0.0
t_start = time.time()

log(f"START multi syms={SYMBOLS} ema={FAST_EMA}/{SLOW_EMA} risk={RISK_PCT*100:.0f}% "
    f"sl_pct={SL_PCT} tp_mult={TP_MULT} floor={EQUITY_FLOOR} target={EQUITY_TARGET} "
    f"max_per_sym={MAX_POS_PER_SYMBOL} max_trades={MAX_TRADES}")
erp(risk_event, None, "bot_start", f"v3 multi {SYMBOLS}", BOT_TAG)

try:
    while True:
        elapsed = time.time() - t_start
        if elapsed > MAX_RUNTIME_HRS * 3600:
            log("STOP runtime limit — flattening")
            erp(risk_event, None, "runtime_stop", "12h limit", BOT_TAG)
            close_all("timeout")
            break

        ai = mt5.account_info()
        equity = ai._asdict()["equity"] if ai else 0.0
        bal = ai._asdict()["balance"] if ai else 0.0
        now = time.time()
        if now - last_hb >= 60:
            last_hb = now
            opent = {s: v["ticket"] for s, v in st.items()}
            log(f"HB equity={equity:.2f} trades={n_trades} wins={wins} losses={losses} open={opent}")
            erp(record_equity, None, equity, bal, 0.0,
                sum(1 for v in st.values() if v["ticket"] is not None), BOT_TAG)
        if equity <= EQUITY_FLOOR:
            log(f"STOP equity floor hit: {equity:.2f} — flattening")
            erp(risk_event, None, "equity_floor", f"{equity:.2f}", BOT_TAG)
            close_all("equity_floor")
            break
        if equity >= EQUITY_TARGET:
            log(f"GOAL equity target hit: {equity:.2f} — flattening")
            erp(risk_event, None, "equity_target", f"{equity:.2f}", BOT_TAG)
            close_all("equity_target")
            break

        for sym in SYMBOLS:
            S = st[sym]
            tk = mt5.symbol_info_tick(sym)
            if tk is None:
                continue
            bid, ask = float(tk.bid), float(tk.ask)
            if bid <= 0 or ask <= 0:
                continue
            spread = ask - bid
            mid = (bid + ask) / 2.0
            S["mids"].append(mid)
            S["hist"].append((now, mid))
            S["fast"] = ema(S["fast"], mid, FAST_EMA)
            S["slow"] = ema(S["slow"], mid, SLOW_EMA)

            sl_move = mid * SL_PCT
            contract = spec[sym]["contract"] or 1.0

            # --- manage open position ---
            if S["ticket"] is not None:
                poss = mt5.positions_get(ticket=S["ticket"])
                if not poss:
                    log(f"EXIT sym={sym} ticket={S['ticket']} no_longer_open (SL/TP or manual)")
                    S["ticket"] = None
                    S["trail"] = None
                    S["last_exit"] = time.time()
                    continue
                p = poss[0]._asdict()
                cur = bid if S["direction"] == "BUY" else ask
                signed = (cur - S["price"]) if S["direction"] == "BUY" else (S["price"] - cur)
                pnl_est = signed * S["vol"] * contract
                sl_hit = signed <= -sl_move
                tp_hit = signed >= sl_move * TP_MULT
                timed_out = (time.time() - S["t0"]) > MAX_HOLD_SEC
                if signed >= mid * TRAIL_TRIG_PCT:
                    nt = (cur - mid * TRAIL_DIST_PCT) if S["direction"] == "BUY" else (cur + mid * TRAIL_DIST_PCT)
                    if S["trail"] is None or ((nt > S["trail"]) if S["direction"] == "BUY" else (nt < S["trail"])):
                        S["trail"] = nt
                trail_hit = S["trail"] is not None and (
                    (cur <= S["trail"]) if S["direction"] == "BUY" else (cur >= S["trail"]))
                if sl_hit or tp_hit or trail_hit or timed_out:
                    reason = "SL" if sl_hit else ("TP" if tp_hit else ("TRAIL" if trail_hit else "TIME"))
                    d = close_ticket(sym, S["ticket"], reason)
                    if d is not None and not mt5.positions_get(ticket=S["ticket"]):
                        prof = float(d.get("profit", 0.0))
                        realized += prof
                        if prof >= 0:
                            wins += 1
                        else:
                            losses += 1
                        log(f"EXIT sym={sym} ticket={S['ticket']} dir={S['direction']} reason={reason} "
                            f"est_pnl={pnl_est:.2f} broker_profit={prof:.2f} equity={equity:.2f}")
                        erp(record_exit, None, S["ticket"], cur, prof, reason, BOT_TAG, sym)
                    S["ticket"] = None
                    S["trail"] = None
                    S["last_exit"] = time.time()
                continue

            # --- entry signal ---
            if (len(S["mids"]) >= LOOKBACK + 5 and S["fast"] is not None and S["slow"] is not None
                    and n_trades < MAX_TRADES
                    and (time.time() - S["last_exit"]) >= COOLDOWN_SEC):
                cur_count = len(mt5.positions_get(symbol=sym) or ())
                if cur_count >= MAX_POS_PER_SYMBOL:
                    continue
                sep_bps = (S["fast"] - S["slow"]) / S["slow"] * 10000.0 if S["slow"] else 0.0
                cutoff = now - MOM_WINDOW_SEC
                old = None
                for ts, m in S["hist"]:
                    if ts <= cutoff:
                        old = m
                    else:
                        break
                mom = mid - old if old is not None else 0.0
                win = list(S["mids"])[-LOOKBACK:-1] if len(S["mids"]) > LOOKBACK else list(S["mids"])[:-1]
                hi = max(win) if win else mid
                lo = min(win) if win else mid
                mom_min = mid * MOM_PCT
                ok_spread = spread <= mid * SPREAD_CAP_PCT
                ema_bull = S["fast"] > S["slow"] and sep_bps >= MIN_SEP_BPS
                ema_bear = S["fast"] < S["slow"] and sep_bps <= -MIN_SEP_BPS
                direction, why = None, ""
                if ema_bull and mom >= mom_min and mid >= hi and ok_spread:
                    direction = "BUY"
                    why = f"sep={sep_bps:.1f} mom={mom:.5f} brk_hi={hi:.5f}"
                elif ema_bear and mom <= -mom_min and mid <= lo and ok_spread:
                    direction = "SELL"
                    why = f"sep={sep_bps:.1f} mom={mom:.5f} brk_lo={lo:.5f}"
                if direction:
                    risk_usd = equity * RISK_PCT
                    vol = risk_usd / (sl_move * contract)
                    vol = max(spec[sym]["vol_min"], min(MAX_LOT, spec[sym]["vol_max"], vol))
                    vol = round(vol / spec[sym]["vol_step"]) * spec[sym]["vol_step"]
                    vol = round(vol, 2)
                    price = ask if direction == "BUY" else bid
                    sl = price - sl_move if direction == "BUY" else price + sl_move
                    tp = price + sl_move * TP_MULT if direction == "BUY" else price - sl_move * TP_MULT
                    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": float(vol),
                           "type": mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL,
                           "price": price, "sl": sl, "tp": tp,
                           "deviation": 20, "magic": MAGIC, "comment": "gmulti-scalp",
                           "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
                    res = None
                    for fill in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
                        req["type_filling"] = fill
                        res = mt5.order_send(req)
                        if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                            break
                        if res is not None and res.retcode not in (10030, 10027, 10028):
                            break
                    if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                        n_trades += 1
                        S["direction"], S["price"], S["vol"], S["t0"] = direction, res.price, res.volume, time.time()
                        cur_poss = mt5.positions_get(symbol=sym) or ()
                        S["ticket"] = max([pp.ticket for pp in cur_poss]) if cur_poss else None
                        log(f"FILL #{n_trades} sym={sym} ticket={S['ticket']} dir={direction} vol={res.volume} "
                            f"price={res.price} sl={sl:.5f} tp={tp:.5f} deal={res.deal} "
                            f"{why} spread={spread:.5f} equity={equity:.2f} comment={res.comment}")
                        erp(record_fill, None, sym, direction, float(res.volume), float(res.price),
                            float(sl), float(tp), S["ticket"], res.deal, res.retcode,
                            "gmulti-scalp", BOT_TAG, risk_usd)
                    else:
                        rc = res.retcode if res is not None else None
                        cc = res.comment if res is not None else mt5.last_error()
                        log(f"REJECT sym={sym} dir={direction} vol={vol} retcode={rc} comment={cc} "
                            f"{why} spread={spread:.5f} equity={equity:.2f}")
                        erp(record_reject, None, sym, direction, float(vol), rc, str(cc), BOT_TAG)

        time.sleep(TICK_POLL_SEC)
finally:
    try:
        ai = mt5.account_info()
        if ai:
            d = ai._asdict()
            log(f"END balance={d.get('balance')} equity={d.get('equity')} profit={d.get('profit')} "
                f"trades={n_trades} wins={wins} losses={losses} realized_broker={realized:.2f}")
            erp(record_equity, None, d.get("equity"), d.get("balance"), d.get("profit"), 0, BOT_TAG)
    except Exception:
        traceback.print_exc()
    mt5.shutdown()
    log("SHUTDOWN")
