"""Kelly sizing + momentum signal helpers for strategies/kelly_momentum.yaml.

STDLIB ONLY (sqlite3 + math). No MT5 import — safe under native Linux AND Wine.
Does NOT touch live routing: main.py/run.py still use strategies/default.yaml
until the user explicitly switches.

Kelly: f* = (p*(b+1) - 1) / b, b=3 (RR 1:3). Half-Kelly, floored at 0,
capped at max_risk_per_trade (1% equity).
p = recent closed-trade win-rate per symbol (fallback 0.55).
Momentum signal: EMA(3) vs EMA(8) + burst + Donchian breakout (gamble_multi_v3).
"""

import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "trading_bot.db"

# ---- Kelly ----

def kelly_f_star(p: float, b: float = 3.0) -> float:
    """Full-Kelly fraction. f*<0 means p below breakeven 1/(b+1) -> no trade."""
    return (p * (b + 1.0) - 1.0) / b


def kelly_risk_usd(equity: float, p: float, b: float = 3.0,
                   half: bool = True, max_risk_pct: float = 0.01) -> dict:
    """Return {f_star, f_bet, risk_pct, risk_usd, trade_allowed}."""
    f_star = kelly_f_star(p, b)
    if f_star <= 0:
        return {"f_star": round(f_star, 4), "f_bet": 0.0,
                "risk_pct": 0.0, "risk_usd": 0.0, "trade_allowed": False}
    f_bet = f_star / 2.0 if half else f_star
    risk_pct = min(f_bet, max_risk_pct)
    risk_usd = equity * risk_pct
    return {"f_star": round(f_star, 4), "f_bet": round(f_bet, 4),
            "risk_pct": round(risk_pct, 4), "risk_usd": round(risk_usd, 2),
            "trade_allowed": True}


def symbol_win_rate(symbol: str, db_path=None, strategy: str = "kelly_momentum",
                    lookback: int = 50, fallback: float = 0.55,
                    min_history: int = 10) -> dict:
    """Win-rate over last N CLOSED trades for symbol (+strategy if column exists).

    Win = pnl > 0 (breakeven/zero counts as loss — conservative).
    Returns {p, n, wins, used_fallback}. Missing DB/table -> fallback.
    """
    db = str(db_path or DEFAULT_DB)
    try:
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "trades" not in tables:
            c.close()
            return {"p": fallback, "n": 0, "wins": 0, "used_fallback": True}
        cols = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
        q = "SELECT pnl FROM trades WHERE status='CLOSED' AND symbol=? "
        args: list = [symbol]
        if strategy and "strategy_name" in cols:
            q += "AND strategy_name=? "
            args.append(strategy)
        elif strategy and "bot_tag" in cols:
            q += "AND bot_tag=? "
            args.append(strategy)
        q += "ORDER BY id DESC LIMIT ?"
        args.append(lookback)
        rows = c.execute(q, args).fetchall()
        c.close()
    except Exception:
        return {"p": fallback, "n": 0, "wins": 0, "used_fallback": True}
    n = len(rows)
    if n < min_history:
        return {"p": fallback, "n": n, "wins": 0, "used_fallback": True}
    wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    return {"p": round(wins / n, 4), "n": n, "wins": wins,
            "used_fallback": False}


def kelly_lot_size(risk_usd: float, sl_move: float, contract: float = 1.0,
                   vol_min: float = 0.01, vol_step: float = 0.01,
                   vol_max: float = 100.0, max_lot: float = 1.0) -> float:
    """Convert risk USD to broker-clamped volume. Mirrors gamble_multi_v3 sizing."""
    if sl_move <= 0 or contract <= 0 or risk_usd <= 0:
        return 0.0
    vol = risk_usd / (sl_move * contract)
    vol = max(vol_min, min(max_lot, vol_max, vol))
    vol = round(vol / vol_step) * vol_step
    return round(vol, 2)


# ---- Momentum signal (pure function, no MT5) ----

def momentum_signal(fast: float, slow: float, mid: float, mid_old,
                    hi: float, lo: float, spread: float,
                    min_sep_bps: float = 0.3, mom_pct: float = 0.00005,
                    spread_cap_pct: float = 0.00015) -> dict:
    """BUY/SELL/HOLD momentum signal. Returns {direction, sep_bps, mom, mom_min, ok_spread}."""
    if slow is None or fast is None or slow == 0 or mid_old is None:
        return {"direction": None, "reason": "warming_up"}
    sep_bps = (fast - slow) / slow * 10000.0
    mom = mid - mid_old
    mom_min = mid * mom_pct
    ok_spread = spread <= mid * spread_cap_pct
    ema_bull = fast > slow and sep_bps >= min_sep_bps
    ema_bear = fast < slow and sep_bps <= -min_sep_bps
    if ema_bull and mom >= mom_min and mid >= hi and ok_spread:
        direction = "BUY"
    elif ema_bear and mom <= -mom_min and mid <= lo and ok_spread:
        direction = "SELL"
    else:
        direction = None
    return {"direction": direction, "sep_bps": round(sep_bps, 2),
            "mom": mom, "mom_min": mom_min, "ok_spread": ok_spread}


# ---- Timeout / breakeven guard (recovery tweak v2, backtested 2026-09-21) ----
#
# BACKTEST on 212 CLOSED trades (data/trading_bot.db):
#   TIME exits: n=56, net -32.72 (23 wins +20.49 / 33 losses -53.21, median -0.45)
#   TRAIL exits: n=143, net +263.10, 96.5% win-rate — the engine that pays.
#   Flooring TIME exits at breakeven (-0.10 slip): TIME net -32.72 -> +17.19,
#   lift +49.91 (+29.5% on closed total 169.43 -> 219.34). Risk/RR untouched.
# RULE: at max_hold timeout, CLOSE only if signed PnL >= breakeven buffer;
#   otherwise EXTEND once (timeout_extend_sec) and arm a breakeven stop so the
#   trailing engine — not the clock — decides the exit.

def timeout_exit_decision(signed: float, sl_move: float,
                          be_buffer_pct: float = 0.00002,
                          mid: float = 0.0,
                          extensions_used: int = 0,
                          max_extensions: int = 1) -> dict:
    """Decide a max_hold timeout exit. Returns {action, reason}.

    action: "CLOSE" (at/above breakeven — take it) or "EXTEND" (underwater —
      grant one extension with a breakeven stop armed; trail/SL decides).
    """
    be_buffer = (mid * be_buffer_pct) if mid else 0.0
    if signed >= be_buffer:
        return {"action": "CLOSE", "reason": "TIME_BE",
                "note": "timeout at/above breakeven — bank it, no market giveback"}
    if extensions_used < max_extensions:
        return {"action": "EXTEND", "reason": "TIME_EXTEND",
                "note": "timeout underwater — extend once, breakeven stop armed"}
    return {"action": "CLOSE", "reason": "TIME",
            "note": "timeout after extension — accept market price"}


def breakeven_stop_price(direction: str, entry: float, mid: float,
                         be_buffer_pct: float = 0.00002) -> float:
    """Stop price that locks breakeven (+buffer for BUY, -buffer for SELL)."""
    buf = (mid or entry) * be_buffer_pct
    return entry + buf if direction == "BUY" else entry - buf


if __name__ == "__main__":
    # Sample sizing: equity $10k, b=3, half-Kelly, 1% cap
    for p in (0.55, 0.40, 0.30, 0.25):
        print(f"p={p}: {kelly_risk_usd(10_000, p)}")
    # Sample lots (assumed mids/contracts — broker specs vary, illustrative only)
    sl = 110_000 * 0.0006
    print("BTCUSD lot:", kelly_lot_size(100.0, sl, 1.0))
    sl = 3_800 * 0.0006
    print("XAUUSD lot:", kelly_lot_size(100.0, sl, 100.0))
    sl = 65.0 * 0.0006
    print("USOIL  lot:", kelly_lot_size(100.0, sl, 1000.0))
