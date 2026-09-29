"""Kelly sizing math, lot conversion, and the signal/timeout decision functions.

These are the parts of the engine that are pure logic, so they are the parts that
can be pinned down exactly. Every expected value below is either derived by hand
in a comment or taken from the worked example in strategies/kelly_momentum.yaml.
"""
import math
import sqlite3

import pytest

from core.kelly import (breakeven_stop_price, kelly_f_star, kelly_lot_size,
                        kelly_risk_usd, momentum_signal, symbol_win_rate,
                        timeout_exit_decision)


# ---------------------------------------------------------------------------
# kelly_f_star: f* = (p*(b+1) - 1) / b
# ---------------------------------------------------------------------------
def test_f_star_matches_the_formula():
    # b=3 -> f* = (4p - 1)/3
    assert kelly_f_star(0.55, 3.0) == pytest.approx((0.55 * 4 - 1) / 3)
    assert kelly_f_star(0.55, 3.0) == pytest.approx(0.4)


def test_f_star_is_zero_at_the_breakeven_win_rate():
    """p0 = 1/(b+1). At b=3 that is 0.25; below it Kelly says do not bet."""
    for b in (0.5, 1.0, 2.0, 3.0, 5.0):
        p0 = 1.0 / (b + 1.0)
        assert kelly_f_star(p0, b) == pytest.approx(0.0, abs=1e-12)


def test_f_star_is_negative_below_breakeven():
    assert kelly_f_star(0.20, 3.0) < 0
    assert kelly_f_star(0.24, 3.0) < 0
    assert kelly_f_star(0.26, 3.0) > 0


def test_f_star_is_monotone_in_p():
    vals = [kelly_f_star(p, 3.0) for p in (0.3, 0.4, 0.5, 0.6, 0.7)]
    assert vals == sorted(vals)


# ---------------------------------------------------------------------------
# kelly_risk_usd: half-Kelly, floored at 0, capped at max_risk_pct
# ---------------------------------------------------------------------------
def test_worked_example_from_the_strategy_config():
    """kelly_momentum.yaml: equity 10k, p=0.55, b=3 -> f*=0.40, half=0.20,
    capped to 1% -> risk $100/trade."""
    r = kelly_risk_usd(10_000.0, 0.55, b=3.0, half=True, max_risk_pct=0.01)
    assert r["f_star"] == pytest.approx(0.4)
    assert r["f_bet"] == pytest.approx(0.2)
    assert r["risk_pct"] == pytest.approx(0.01)      # the cap binds
    assert r["risk_usd"] == pytest.approx(100.0)
    assert r["trade_allowed"] is True


def test_cap_binds_because_kelly_almost_always_exceeds_it():
    """With b=3 and any p above ~0.2575 half-Kelly already exceeds 1%."""
    for p in (0.30, 0.40, 0.50, 0.60):
        r = kelly_risk_usd(1_000.0, p)
        assert r["risk_pct"] == pytest.approx(0.01)
        assert r["risk_usd"] == pytest.approx(10.0)


def test_half_kelly_is_exactly_half_of_full_kelly():
    full = kelly_risk_usd(10_000.0, 0.55, half=False, max_risk_pct=1.0)
    half = kelly_risk_usd(10_000.0, 0.55, half=True, max_risk_pct=1.0)
    assert half["f_bet"] == pytest.approx(full["f_bet"] / 2)


def test_no_trade_when_f_star_is_not_positive():
    for p in (0.0, 0.10, 0.25, 0.2499):
        r = kelly_risk_usd(10_000.0, p)
        assert r["trade_allowed"] is False
        assert r["risk_usd"] == 0.0
        assert r["risk_pct"] == 0.0
        assert r["f_bet"] == 0.0


def test_risk_scales_with_equity_not_with_a_fixed_dollar_amount():
    a = kelly_risk_usd(100.0, 0.55)
    b = kelly_risk_usd(10_000.0, 0.55)
    assert a["risk_usd"] == pytest.approx(1.0)
    assert b["risk_usd"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# kelly_lot_size: risk USD -> broker-clamped volume
# ---------------------------------------------------------------------------
def test_lot_size_from_risk_and_stop_distance():
    # risk 100 USD, stop 60 price units, contract 1.0 -> 1.666 lots -> clamped to
    # max_lot 1.0 by the caller's default
    assert kelly_lot_size(100.0, 60.0, contract=1.0, max_lot=10.0) == pytest.approx(1.67)
    # risk 100, stop 2.5, contract 100 -> 0.4 lots
    assert kelly_lot_size(100.0, 2.5, contract=100.0) == pytest.approx(0.4)


def test_lot_size_respects_broker_minimum():
    """A tiny account can only trade the 0.01 minimum. This is the mechanism that
    silently breaks the 1% cap on a $100 balance."""
    lots = kelly_lot_size(0.5, 2.5, contract=100.0, vol_min=0.01)
    assert lots == pytest.approx(0.01)


def test_lot_size_respects_broker_maximum_and_step():
    assert kelly_lot_size(1e9, 1.0, contract=1.0, vol_max=50.0, max_lot=1e9) == 50.0
    # step rounding: 0.0173 -> 0.02
    assert kelly_lot_size(1.73, 100.0, contract=1.0) == pytest.approx(0.02)


def test_lot_size_refuses_degenerate_inputs():
    assert kelly_lot_size(0.0, 1.0, contract=1.0) == 0.0
    assert kelly_lot_size(-5.0, 1.0, contract=1.0) == 0.0
    assert kelly_lot_size(10.0, 0.0, contract=1.0) == 0.0
    assert kelly_lot_size(10.0, -1.0, contract=1.0) == 0.0
    assert kelly_lot_size(10.0, 1.0, contract=0.0) == 0.0


# ---------------------------------------------------------------------------
# momentum_signal: EMA separation + burst + Donchian break + spread gate
# ---------------------------------------------------------------------------
def test_buy_needs_all_four_conditions():
    # fast > slow with separation, momentum burst up, price at the window high,
    # spread inside the cap -> BUY
    sig = momentum_signal(fast=100.10, slow=100.00, mid=100.20, mid_old=100.10,
                          hi=100.15, lo=99.80, spread=0.01,
                          min_sep_bps=0.3, mom_pct=0.00005, spread_cap_pct=0.00015)
    assert sig["direction"] == "BUY"
    assert sig["sep_bps"] == pytest.approx(10.0, abs=0.01)
    assert sig["ok_spread"] is True


def test_sell_is_the_mirror():
    sig = momentum_signal(fast=99.90, slow=100.00, mid=99.80, mid_old=99.90,
                          hi=100.20, lo=99.85, spread=0.01)
    assert sig["direction"] == "SELL"
    assert sig["sep_bps"] < 0


def test_insufficient_ema_separation_blocks_the_signal():
    # fast just barely above slow: sep below min_sep_bps
    sig = momentum_signal(fast=100.001, slow=100.000, mid=100.20, mid_old=100.10,
                          hi=100.15, lo=99.80, spread=0.01, min_sep_bps=0.3)
    assert sig["direction"] is None


def test_no_burst_blocks_the_signal():
    sig = momentum_signal(fast=100.10, slow=100.00, mid=100.10, mid_old=100.10,
                          hi=100.15, lo=99.80, spread=0.01)
    assert sig["direction"] is None


def test_no_donchian_break_blocks_the_signal():
    # price below the lookback high -> no BUY even though everything else lines up
    sig = momentum_signal(fast=100.10, slow=100.00, mid=100.10, mid_old=100.00,
                          hi=100.15, lo=99.80, spread=0.01)
    assert sig["direction"] is None


def test_spread_cap_blocks_an_otherwise_valid_signal():
    sig = momentum_signal(fast=100.10, slow=100.00, mid=100.20, mid_old=100.10,
                          hi=100.15, lo=99.80, spread=0.02,   # 20 bps vs 1.5 bps cap
                          spread_cap_pct=0.00015)
    assert sig["ok_spread"] is False
    assert sig["direction"] is None


def test_warming_up_returns_no_direction():
    assert momentum_signal(None, None, 100.0, None, 100.0, 100.0, 0.01)["direction"] is None
    assert momentum_signal(100.0, None, 100.0, 100.0, 100.0, 100.0, 0.01)["direction"] is None
    assert momentum_signal(100.0, 0.0, 100.0, 100.0, 100.0, 100.0, 0.01)["direction"] is None


# ---------------------------------------------------------------------------
# timeout / breakeven guard
# ---------------------------------------------------------------------------
def test_timeout_above_breakeven_banks_the_trade():
    d = timeout_exit_decision(signed=0.5, sl_move=2.0, mid=100.0, extensions_used=0)
    assert d["action"] == "CLOSE"
    assert d["reason"] == "TIME_BE"


def test_timeout_underwater_extends_once_then_accepts_market():
    first = timeout_exit_decision(signed=-0.5, sl_move=2.0, mid=100.0, extensions_used=0)
    assert first["action"] == "EXTEND"
    second = timeout_exit_decision(signed=-0.5, sl_move=2.0, mid=100.0, extensions_used=1)
    assert second["action"] == "CLOSE"
    assert second["reason"] == "TIME"


def test_extension_budget_is_respected():
    d = timeout_exit_decision(signed=-1.0, sl_move=2.0, mid=100.0,
                              extensions_used=0, max_extensions=0)
    assert d["action"] == "CLOSE"


def test_breakeven_stop_sits_on_the_profitable_side():
    buy = breakeven_stop_price("BUY", 100.0, 100.0)
    sell = breakeven_stop_price("SELL", 100.0, 100.0)
    assert buy > 100.0
    assert sell < 100.0
    # buffer is proportional to mid, so a bigger mid gives a bigger cushion
    assert breakeven_stop_price("BUY", 100.0, 200.0) > buy


# ---------------------------------------------------------------------------
# symbol_win_rate against a synthetic ledger
# ---------------------------------------------------------------------------
def _ledger(tmp_path, pnls, symbol="BTCUSD", strategy="kelly_momentum",
            status="CLOSED"):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, pnl REAL,"
                " status TEXT, strategy_name TEXT)")
    for i, p in enumerate(pnls):
        con.execute("INSERT INTO trades (symbol, pnl, status, strategy_name)"
                    " VALUES (?,?,?,?)", (symbol, p, status, strategy))
    con.commit()
    con.close()
    return db


def test_win_rate_counts_scratch_trades_as_losses(tmp_path):
    db = _ledger(tmp_path, [1.0] * 6 + [0.0] + [-1.0] * 3)
    r = symbol_win_rate("BTCUSD", db_path=db, min_history=1, lookback=100)
    assert r["n"] == 10
    assert r["wins"] == 6
    assert r["p"] == pytest.approx(0.6)          # the 0.0 is not a win


def test_win_rate_falls_back_when_history_is_thin(tmp_path):
    db = _ledger(tmp_path, [1.0, 1.0, 1.0])
    r = symbol_win_rate("BTCUSD", db_path=db, min_history=10, fallback=0.55)
    assert r["used_fallback"] is True
    assert r["p"] == pytest.approx(0.55)


def test_win_rate_falls_back_when_the_db_is_missing(tmp_path):
    r = symbol_win_rate("BTCUSD", db_path=tmp_path / "nope.db", fallback=0.55)
    assert r["used_fallback"] is True
    assert r["p"] == pytest.approx(0.55)


def test_win_rate_ignores_other_symbols_and_open_trades(tmp_path):
    db = _ledger(tmp_path, [1.0] * 12)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO trades (symbol, pnl, status, strategy_name)"
                " VALUES ('XAUUSD', 5.0, 'CLOSED', 'kelly_momentum')")
    con.execute("INSERT INTO trades (symbol, pnl, status, strategy_name)"
                " VALUES ('BTCUSD', 5.0, 'OPEN', 'kelly_momentum')")
    con.commit()
    con.close()
    r = symbol_win_rate("BTCUSD", db_path=db, min_history=1, lookback=100)
    assert r["n"] == 12
    assert r["p"] == pytest.approx(1.0)


def test_win_rate_only_uses_the_most_recent_lookback(tmp_path):
    # 10 old losses then 10 recent wins; lookback=10 must see only the wins
    db = _ledger(tmp_path, [-1.0] * 10 + [1.0] * 10)
    r = symbol_win_rate("BTCUSD", db_path=db, lookback=10, min_history=1)
    assert r["n"] == 10
    assert r["p"] == pytest.approx(1.0)
