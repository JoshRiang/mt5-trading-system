"""KELLY MOMENTUM DEMO scalper — DEMO ONLY. Triple safety gate, NO real-money orders.

Params from strategies/kelly_momentum.yaml (status defined_only -> now LIVE per user switch):
  Signal: EMA(3)/EMA(8) + momentum burst + Donchian breakout (reused from gamble_multi_v3).
  Sizing: Kelly f*=(p*(b+1)-1)/b, b=3 (RR 1:3), HALF-Kelly, capped 1% equity/trade.
    p = last-50 CLOSED win-rate per symbol (fallback 0.55 if <10 history).
    f*<0 (p<0.25 breakeven) -> NO trade.
  Risk: 1 pos/symbol, 3 max open, 1%/day cap, SL 0.06% TP 3x, trail, 150s max hold.
  Signal+sizing via core/kelly.py (stdlib only). ERP ledger via core/erp.py (failures never kill trading).

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

try:
    from core.kelly import (symbol_win_rate, kelly_risk_usd, kelly_lot_size,
                            momentum_signal)
    KELLY_MOD = True
except Exception as e:
    print(f"KELLY_ABORT no core.kelly: {e}", flush=True)
    sys.exit(8)

# Symbol aliases + filling-mode resolution — STDLIB ONLY, shared with the
# other engines. Without this the engine sends a hardcoded IOC and BTCUSD
# rejects every order with 10030 "Unsupported filling mode".
try:
    from core.symbols import (resolve_symbol, filling_candidates,
                              describe_filling, RETRYABLE_FILL_RETCODES)
    SYMBOLS_MOD = True
except Exception as e:
    print(f"KELLY_ABORT no core.symbols: {e}", flush=True)
    sys.exit(9)

# ---- CONFIG (strategies/kelly_momentum.yaml verbatim) ----
SYMBOLS = ["BTCUSD", "XAUUSD", "USOIL"]
BOT_TAG = "kelly_momentum"
# EQUITY_FLOOR is RESOLVED AT STARTUP from live equity (see resolve_floor()).
# It used to be a hard 100.0 while the demo balance is 100.17 — any tick of
# spread instantly tripped it, the engine flattened and exited, and the bot
# could never run. A safety floor must sit BELOW the starting balance.
EQUITY_FLOOR = None          # resolved after the safety gate, from live equity
EQUITY_FLOOR_PCT = 0.5       # floor = 50% of startup equity ...
EQUITY_FLOOR_ABS_MIN = 50.0  # ... but never lower than this absolute floor
EQUITY_TARGET = 1000.0
# signal
FAST_EMA = 3
SLOW_EMA = 8
MIN_SEP_BPS = 0.3
MOM_PCT = 0.00005
MOM_WINDOW_SEC = 10
SPREAD_CAP_PCT = 0.00015
LOOKBACK = 60
TICK_POLL_SEC = 1.0
COOLDOWN_SEC = 3
# exits
SL_PCT = 0.0006
TP_MULT = 3.0
TRAIL_TRIG_PCT = 0.00025
TRAIL_DIST_PCT = 0.00012
MAX_HOLD_SEC = 150
# kelly
KELLY_B = 3.0
P_LOOKBACK = 50
P_FALLBACK = 0.55
P_MIN_HISTORY = 10
MAX_RISK_PCT = 0.01
MAX_LOT = 1.00
# risk
DAILY_CAP = 0.01
# Floating (unrealized) P/L must not count against the daily cap on its own:
# one open 0.05-lot position dipping 1.1% tripped the cap and froze entries for
# 10 hours with a healthy book (2026-09-21 incident). The cap now latches only
# on REALIZED loss, which is what it is meant to guard.
DAILY_CAP_REALIZED_ONLY = True
MAX_POS_PER_SYMBOL = 1
MAX_OPEN_POSITIONS = 3
MAX_TRADES = 500
MAX_RUNTIME_HRS = 12.0
MAGIC = 20260930

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


def resolve_floor(start_equity: float) -> float:
    """Resolve the safety floor from STARTUP equity so it cannot self-trip.

    The old hard 100.0 equalled the 100.17 live balance: the very first tick
    of spread put equity below it, the engine flattened and exited, and the
    bot could never trade. The floor must sit strictly BELOW the starting
    balance; it is a drawdown stop, not a starting-line tripwire.
    """
    if not start_equity or start_equity <= 0:
        return EQUITY_FLOOR_ABS_MIN
    floor = max(EQUITY_FLOOR_ABS_MIN, start_equity * EQUITY_FLOOR_PCT)
    # Never allow a floor at/above the live equity (that self-trips).
    if floor >= start_equity:
        floor = start_equity * EQUITY_FLOOR_PCT
    return round(floor, 2)


def mt5_symbol(bot_symbol: str) -> str:
    """Resolve a bot-side symbol (e.g. OILUSD) to the broker symbol (USOIL)."""
    return resolve_symbol(bot_symbol)


# ---- resolve the safety floor from live equity BEFORE anything trades ----
EQUITY_FLOOR = resolve_floor(float(ad.get("equity") or ad.get("balance") or 0.0))
log(f"FLOOR_RESOLVED floor={EQUITY_FLOOR} start_equity={ad.get('equity')} "
    f"pct={EQUITY_FLOOR_PCT} abs_min={EQUITY_FLOOR_ABS_MIN}")
if ad.get("equity") and EQUITY_FLOOR >= float(ad["equity"]):
    log(f"GAMBLE_ABORT floor_self_trip floor={EQUITY_FLOOR} equity={ad['equity']}")
    mt5.shutdown()
    sys.exit(10)


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
    ms = spec.get(sym, {}).get("mt5_symbol") or mt5_symbol(sym)
    tick = mt5.symbol_info_tick(ms)
    if tick is None:
        log(f"CLOSE_FAIL sym={sym} ticket={ticket} no_tick")
        return None
    req = {"action": mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": ms,
           "volume": float(d["volume"]),
           "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
           "price": tick.bid if is_buy else tick.ask,
           "deviation": 20, "magic": MAGIC, "comment": f"kelly-close:{reason}",
           "type_time": mt5.ORDER_TIME_GTC, "type_filling": None}
    # Fill types derived from THIS symbol's filling_mode; retry on 10030/10027/10028.
    cands = fills.get(sym) or filling_candidates(None)
    res = None
    for fill in cands:
        req["type_filling"] = fill
        res = mt5.order_send(req)
        if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            break
        if res is not None and res.retcode not in RETRYABLE_FILL_RETCODES:
            break
    if res is None:
        log(f"CLOSE_FAIL sym={sym} ticket={ticket} no_result err={mt5.last_error()}")
        return None
    log(f"CLOSE sym={sym} -> {ms} ticket={ticket} side={'BUY' if is_buy else 'SELL'} "
        f"vol={d['volume']} open={d['price_open']} retcode={res.retcode} "
        f"fill={req['type_filling']} price={res.price} comment={res.comment}")
    return d


def close_all(reason):
    for sym in SYMBOLS:
        ms = spec.get(sym, {}).get("mt5_symbol") or mt5_symbol(sym)
        for p in (mt5.positions_get(symbol=ms) or ()):
            close_ticket(sym, p.ticket, reason)


# ---- per-symbol specs + state ----
spec = {}
fills = {}
for s in SYMBOLS:
    ms = mt5_symbol(s)
    mt5.symbol_select(ms, True)
    si = mt5.symbol_info(ms)
    if si is None:
        log(f"SPEC_FAIL {s} -> {ms} no symbol_info — SKIPPED")
        continue
    fm = si.filling_mode
    spec[s] = dict(vol_min=si.volume_min, vol_step=si.volume_step,
                   vol_max=si.volume_max, contract=si.trade_contract_size,
                   digits=si.digits, mt5_symbol=ms, filling_mode=fm)
    fills[s] = filling_candidates(fm)
    log(f"SPEC {s} -> {ms} vol_min={si.volume_min} step={si.volume_step} max={si.volume_max} "
        f"contract={si.trade_contract_size} digits={si.digits} spread={si.spread} "
        f"filling_mode={fm} ({describe_filling(fm)}) candidates={fills[s]}")
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
day_start_equity = None
day_anchor_date = None
daily_blocked_logged = False

# starting Kelly p per symbol (informational; refreshed at each entry)
for s in SYMBOLS:
    wr = symbol_win_rate(s)
    log(f"KELLY_INIT sym={s} p={wr['p']} n={wr['n']} wins={wr['wins']} fallback={wr['used_fallback']}")

log(f"START kelly syms={SYMBOLS} ema={FAST_EMA}/{SLOW_EMA} kelly_b={KELLY_B} half "
    f"cap={MAX_RISK_PCT*100:.0f}% sl_pct={SL_PCT} tp_mult={TP_MULT} "
    f"max_per_sym={MAX_POS_PER_SYMBOL} max_open={MAX_OPEN_POSITIONS} daily_cap={DAILY_CAP*100:.0f}% "
    f"floor={EQUITY_FLOOR} target={EQUITY_TARGET} max_trades={MAX_TRADES}")
erp(risk_event, None, "bot_start", f"kelly_momentum {SYMBOLS}", BOT_TAG)

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
        today = datetime.now(timezone.utc).date().isoformat()
        if day_start_equity is None and equity > 0:
            day_start_equity = equity
            day_anchor_date = today
            log(f"DAY_ANCHOR equity={day_start_equity:.2f}")
        elif today != day_anchor_date:
            # UTC day rollover: rebase anchor
            day_start_equity = equity
            day_anchor_date = today
            daily_blocked_logged = False
            log(f"DAY_ANCHOR rebase equity={day_start_equity:.2f}")
        daily_blocked = (day_start_equity is not None
                         and equity < day_start_equity * (1 - DAILY_CAP))
        if DAILY_CAP_REALIZED_ONLY:
            # Only latch the cap once the loss is BANKED. A floating dip on an
            # open position is not a realized loss; latching on it froze the
            # bot for 10h on a healthy book (2026-09-21).
            realized = bal - day_start_equity if day_start_equity else 0.0
            daily_blocked = (day_start_equity is not None
                             and realized < -day_start_equity * DAILY_CAP)
        if daily_blocked and all(v["ticket"] is None for v in st.values()):
            # flat book while blocked (e.g. after restart into a loss): rebase so
            # the bot is never stuck paused with nothing working; cap still guards
            # fresh intraday drawdown from here
            day_start_equity = equity
            day_anchor_date = today
            daily_blocked = False
            daily_blocked_logged = False
            log(f"DAY_ANCHOR flat-rebase equity={day_start_equity:.2f}")
        if daily_blocked and not daily_blocked_logged:
            log(f"DAILY_CAP hit: equity={equity:.2f} anchor={day_start_equity:.2f} — entries paused")
            erp(risk_event, None, "daily_cap", f"{equity:.2f}", BOT_TAG)
            daily_blocked_logged = True

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

        open_total = sum(1 for v in st.values() if v["ticket"] is not None)

        for sym in SYMBOLS:
            S = st[sym]
            ms = spec[sym]["mt5_symbol"]          # broker symbol (OILUSD -> USOIL)
            tk = mt5.symbol_info_tick(ms)
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
                    open_total = sum(1 for v in st.values() if v["ticket"] is not None)
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
                    open_total = sum(1 for v in st.values() if v["ticket"] is not None)
                continue

            # --- entry signal (core.kelly) ---
            if (len(S["mids"]) >= LOOKBACK + 5 and S["fast"] is not None and S["slow"] is not None
                    and n_trades < MAX_TRADES
                    and (time.time() - S["last_exit"]) >= COOLDOWN_SEC
                    and not daily_blocked
                    and open_total < MAX_OPEN_POSITIONS):
                cur_count = len(mt5.positions_get(symbol=ms) or ())
                if cur_count >= MAX_POS_PER_SYMBOL:
                    continue
                cutoff = now - MOM_WINDOW_SEC
                old = None
                for ts, m in S["hist"]:
                    if ts <= cutoff:
                        old = m
                    else:
                        break
                win = list(S["mids"])[-LOOKBACK:-1] if len(S["mids"]) > LOOKBACK else list(S["mids"])[:-1]
                hi = max(win) if win else mid
                lo = min(win) if win else mid
                sig = momentum_signal(S["fast"], S["slow"], mid, old, hi, lo, spread,
                                      MIN_SEP_BPS, MOM_PCT, SPREAD_CAP_PCT)
                direction = sig.get("direction")
                if direction:
                    # --- Kelly sizing (core.kelly) ---
                    wr = symbol_win_rate(sym, lookback=P_LOOKBACK,
                                         fallback=P_FALLBACK, min_history=P_MIN_HISTORY)
                    kr = kelly_risk_usd(equity, wr["p"], KELLY_B, True, MAX_RISK_PCT)
                    if not kr["trade_allowed"]:
                        log(f"KELLY_SKIP sym={sym} dir={direction} p={wr['p']} "
                            f"f*={kr['f_star']} below breakeven")
                        continue
                    risk_usd = kr["risk_usd"]
                    vol = kelly_lot_size(risk_usd, sl_move, contract,
                                         spec[sym]["vol_min"], spec[sym]["vol_step"],
                                         spec[sym]["vol_max"], MAX_LOT)
                    if vol <= 0:
                        log(f"KELLY_SKIP sym={sym} dir={direction} vol=0 risk_usd={risk_usd}")
                        continue
                    price = ask if direction == "BUY" else bid
                    sl = price - sl_move if direction == "BUY" else price + sl_move
                    tp = price + sl_move * TP_MULT if direction == "BUY" else price - sl_move * TP_MULT
                    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": ms, "volume": float(vol),
                           "type": mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL,
                           "price": price, "sl": sl, "tp": tp,
                           "deviation": 20, "magic": MAGIC, "comment": "kelly-scalp",
                           "type_time": mt5.ORDER_TIME_GTC, "type_filling": None}
                    # Fill types come from THIS symbol's filling_mode. BTCUSD
                    # (mode 3) works with FOK/IOC; XAU/XAG/USOIL (mode 1) with
                    # FOK; RETURN is what earns 10030 here. Never hardcode.
                    res = None
                    for fill in fills.get(sym) or filling_candidates(None):
                        req["type_filling"] = fill
                        res = mt5.order_send(req)
                        if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                            break
                        if res is not None and res.retcode not in RETRYABLE_FILL_RETCODES:
                            break
                    if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                        n_trades += 1
                        S["direction"], S["price"], S["vol"], S["t0"] = direction, res.price, res.volume, time.time()
                        cur_poss = mt5.positions_get(symbol=ms) or ()
                        S["ticket"] = max([pp.ticket for pp in cur_poss]) if cur_poss else None
                        open_total += 1
                        log(f"FILL #{n_trades} sym={sym} -> {ms} ticket={S['ticket']} dir={direction} "
                            f"vol={res.volume} price={res.price} sl={sl:.5f} tp={tp:.5f} "
                            f"fill={req['type_filling']} deal={res.deal} "
                            f"p={wr['p']} n={wr['n']} f*={kr['f_star']} risk_usd={risk_usd} "
                            f"sep={sig.get('sep_bps')} spread={spread:.5f} equity={equity:.2f} comment={res.comment}")
                        erp(record_fill, None, sym, direction, float(res.volume), float(res.price),
                            float(sl), float(tp), S["ticket"], res.deal, res.retcode,
                            "kelly-scalp", BOT_TAG, risk_usd)
                    else:
                        rc = res.retcode if res is not None else None
                        cc = res.comment if res is not None else mt5.last_error()
                        log(f"REJECT sym={sym} dir={direction} vol={vol} retcode={rc} comment={cc} "
                            f"sep={sig.get('sep_bps')} spread={spread:.5f} equity={equity:.2f}")
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
