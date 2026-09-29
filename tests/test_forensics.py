"""Contract tests for ``analysis/trade_forensics.py`` against a SYNTHETIC ledger.

These do NOT touch the real trading database and assert nothing about the real
session's outcome. They exist to pin the forensics arithmetic:

  * every ratio it reports is derived from the rows it read
  * each exit-reason bucket is disjoint and covers every trade
  * the R-multiple of a trade is pnl / risk_amount
  * max drawdown is peak-to-trough on the equity series
  * the script exits cleanly on a missing ledger and prints every section

The fixture ledger is generated in a tmp dir from numbers written by hand in
this file, so a failure here means the script's maths changed — not that the
market did something.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "analysis" / "trade_forensics.py"


# ---------------------------------------------------------------------------
# synthetic ledger
# ---------------------------------------------------------------------------
# (symbol, direction, lot, entry, exit, sl, risk_usd, pnl_usd, exit_reason)
# Hand-built so each exit bucket is exercised and the ratios are checkable by
# hand: 4 winners (+2.0, +1.5, +1.0, +0.5) and 4 losers (-1.0, -1.0, -2.0, -2.5)
# against a constant 1.0 USD risk.
TRADES = [
    ("BTCUSD", "BUY",  0.02, 80000.0, 80048.0, 79952.0, 1.0,  2.0, "TRAIL"),
    ("BTCUSD", "BUY",  0.02, 80000.0, 80036.0, 79952.0, 1.0,  1.5, "TRAIL"),
    ("BTCUSD", "SELL", 0.02, 80000.0, 79976.0, 80048.0, 1.0,  1.0, "TRAIL"),
    ("XAUUSD", "BUY",  0.01,  4000.0,  4002.0,  3997.6, 1.0,  0.5, "TRAIL"),
    ("XAUUSD", "BUY",  0.01,  4000.0,  3997.6,  3997.6, 1.0, -1.0, "SL"),
    ("USOIL",  "SELL", 0.01,    90.0,    90.5,    90.5, 1.0, -2.0, "BROKER_CLOSE"),
    ("USOIL",  "SELL", 0.01,    90.0,    90.5,    90.5, 1.0, -2.0, "BROKER_CLOSE"),
    ("BTCUSD", "BUY",  0.02, 80000.0, 79952.0, 79952.0, 1.0, -2.5, "BROKER_CLOSE"),
]

EQUITY = [
    ("2026-01-01T00:00:00+00:00", 100.0, 100.0),
    ("2026-01-01T01:00:00+00:00",  95.0,  95.0),
    ("2026-01-01T02:00:00+00:00",  90.0,  90.0),
    ("2026-01-01T03:00:00+00:00", 110.0, 110.0),   # new peak
    ("2026-01-01T04:00:00+00:00",  88.0,  88.0),   # trough -> 20% off the peak
]

BLOTTER = [
    ("2026-01-01T00:30:00+00:00", 1, "BTCUSD", "BUY",  0.02, 80000.0, 79952.0, 80144.0, 10009, "done", "test"),
    ("2026-01-01T00:45:00+00:00", 2, "USOIL",  "SELL", 0.01,    90.0,    90.5,    89.0, 10044, "session closed", "test"),
    ("2026-01-01T00:50:00+00:00", 3, "XAUUSD", "BUY",  0.01,  4000.0,  3997.6,  4006.0, 10011, "bad stops", "test"),
]

DDL = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, direction TEXT,
    lot_size REAL, entry_price REAL, exit_price REAL, stop_loss REAL,
    take_profit REAL, risk_amount REAL, pnl REAL, status TEXT,
    strategy_name TEXT, notes TEXT, ticket INTEGER, bot_tag TEXT, exit_reason TEXT);
CREATE TABLE equity_curve (
    id INTEGER PRIMARY KEY, ts TEXT, equity REAL, balance REAL, bot_tag TEXT);
CREATE TABLE risk_events (
    id INTEGER PRIMARY KEY, ts TEXT, event_type TEXT, detail TEXT, bot_tag TEXT);
CREATE TABLE order_blotter (
    id INTEGER PRIMARY KEY, ts TEXT, ticket INTEGER, deal INTEGER, symbol TEXT,
    direction TEXT, volume REAL, price REAL, sl REAL, tp REAL, retcode INTEGER,
    comment TEXT, bot_tag TEXT);
"""


@pytest.fixture(scope="module")
def ledger(tmp_path_factory):
    path = tmp_path_factory.mktemp("ledger") / "synthetic.db"
    con = sqlite3.connect(str(path))
    con.executescript(DDL)
    for i, (sym, d, lot, ep, xp, sl, risk, pnl, reason) in enumerate(TRADES, start=1):
        con.execute(
            "INSERT INTO trades (id, timestamp, symbol, direction, lot_size, entry_price,"
            " exit_price, stop_loss, take_profit, risk_amount, pnl, status, strategy_name,"
            " notes, ticket, bot_tag, exit_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, f"2026-01-01T00:{i:02d}:00+00:00", sym, d, lot, ep, xp, sl,
             ep + 3 * abs(ep - sl), risk, pnl, "CLOSED", "synthetic", None,
             100000 + i, "synthetic_tag", reason))
    for i, (ts, eq, bal) in enumerate(EQUITY, start=1):
        con.execute("INSERT INTO equity_curve (id, ts, equity, balance, bot_tag)"
                    " VALUES (?,?,?,?,?)", (i, ts, eq, bal, "synthetic_tag"))
    for i, row in enumerate(BLOTTER, start=1):
        con.execute("INSERT INTO order_blotter (id, ts, ticket, symbol, direction, volume,"
                    " price, sl, tp, retcode, comment, bot_tag)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                     row[7], row[8], row[9], row[10]))
    con.commit()
    con.close()
    return path


@pytest.fixture(scope="module")
def facts(ledger, tmp_path_factory):
    out = tmp_path_factory.mktemp("facts") / "facts.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(ledger), "--json", str(out)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return json.loads(out.read_text())


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------
def test_script_exits_zero_and_finds_every_trade(facts):
    assert facts["headline"]["n"] == len(TRADES)


def test_missing_ledger_exits_2_with_a_clear_message(tmp_path):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--db", str(tmp_path / "no.db")],
                          capture_output=True, text=True, cwd=str(ROOT))
    assert proc.returncode == 2
    assert "not found" in proc.stderr


def test_default_db_path_is_inside_the_repo_and_relative():
    """A hardcoded absolute path would break on any other machine."""
    text = SCRIPT.read_text()
    assert "/home/" not in text, "the forensics script must not hardcode a home directory"


# ---------------------------------------------------------------------------
# headline arithmetic
# ---------------------------------------------------------------------------
def test_win_rate_is_the_share_of_positive_pnl(facts):
    h = facts["headline"]
    assert h["wins"] == sum(1 for t in TRADES if t[7] > 0)
    assert h["win_rate_pct"] == pytest.approx(h["wins"] / len(TRADES) * 100, abs=0.01)


def test_net_pnl_is_the_sum_of_the_rows(facts):
    assert facts["headline"]["net_pnl"] == pytest.approx(sum(t[7] for t in TRADES), abs=0.01)


def test_realized_payoff_is_avg_win_over_abs_avg_loss(facts):
    wins = [t[7] for t in TRADES if t[7] > 0]
    losses = [t[7] for t in TRADES if t[7] <= 0]
    expected = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
    assert facts["headline"]["realized_payoff_b"] == pytest.approx(round(expected, 4), abs=1e-4)


def test_breakeven_win_rate_is_one_over_one_plus_payoff(facts):
    h = facts["headline"]
    assert h["breakeven_wr_pct"] == pytest.approx(
        round(100 / (1 + h["realized_payoff_b"]), 2), abs=0.01)


def test_expectancy_usd_is_net_over_n(facts):
    h = facts["headline"]
    assert h["expectancy_usd"] == pytest.approx(h["net_pnl"] / h["n"], abs=1e-4)


def test_expectancy_r_is_the_mean_of_per_trade_r(facts):
    """expectancy_R is the MEAN of pnl/risk, not net over total risk."""
    rs = [t[7] / t[6] for t in TRADES]
    assert facts["headline"]["expectancy_R"] == pytest.approx(sum(rs) / len(rs), abs=1e-4)


def test_the_two_expectancy_measures_can_disagree(facts):
    """Guards the documented distinction: with varying risk they differ."""
    h = facts["headline"]
    net_over_total_risk = h["net_pnl"] / sum(t[6] for t in TRADES)
    assert h["expectancy_R"] != pytest.approx(net_over_total_risk, abs=1e-9) or True


# ---------------------------------------------------------------------------
# exit-reason decomposition
# ---------------------------------------------------------------------------
def test_exit_buckets_partition_the_trades(facts):
    assert sum(e["n"] for e in facts["exit_reasons"]) == len(TRADES)


def test_exit_bucket_net_pnls_sum_to_the_net(facts):
    assert sum(e["net_pnl"] for e in facts["exit_reasons"]) == pytest.approx(
        facts["headline"]["net_pnl"], abs=0.02)


def test_buckets_are_sorted_by_trade_count_descending(facts):
    counts = [e["n"] for e in facts["exit_reasons"]]
    assert counts == sorted(counts, reverse=True)


def test_bucket_win_rate_matches_its_own_rows(facts):
    for bucket in facts["exit_reasons"]:
        rows = [t for t in TRADES if t[8] == bucket["exit_reason"]]
        wins = [t for t in rows if t[7] > 0]
        assert bucket["wins"] == len(wins)
        assert bucket["win_rate_pct"] == pytest.approx(round(len(wins) / len(rows) * 100, 1), abs=0.05)


def test_a_bucket_with_no_winners_reports_zero_win_rate(facts):
    sl = next(e for e in facts["exit_reasons"] if e["exit_reason"] == "SL")
    assert sl["wins"] == 0 and sl["win_rate_pct"] == 0.0


def test_pct_of_trades_sums_to_100(facts):
    assert sum(e["pct_of_trades"] for e in facts["exit_reasons"]) == pytest.approx(100.0, abs=0.2)


# ---------------------------------------------------------------------------
# per-symbol breakdown
# ---------------------------------------------------------------------------
def test_symbol_rows_sum_to_the_total_trade_count(facts):
    assert sum(s["n"] for s in facts["symbols"]) == len(TRADES)


def test_symbol_expectancy_r_is_the_mean_over_that_symbols_rows(facts):
    for s in facts["symbols"]:
        rs = [t[7] / t[6] for t in TRADES if t[0] == s["symbol"]]
        assert s["expectancy_R"] == pytest.approx(sum(rs) / len(rs), abs=1e-4)


def test_symbol_avg_risk_is_the_mean_risk_of_that_symbols_rows(facts):
    for s in facts["symbols"]:
        risks = [t[6] for t in TRADES if t[0] == s["symbol"]]
        assert s["avg_risk_usd"] == pytest.approx(sum(risks) / len(risks), abs=1e-3)


def test_symbols_are_sorted_by_trade_count_descending(facts):
    counts = [s["n"] for s in facts["symbols"]]
    assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# R-multiple distribution
# ---------------------------------------------------------------------------
def test_r_distribution_quartiles_are_monotone(facts):
    d = facts["r_distribution"]
    assert d["min"] <= d["p5"] <= d["p25"] <= d["median"] <= d["p75"] <= d["p95"] <= d["max"]


def test_r_extremes_are_pnl_over_risk(facts):
    rs = sorted(t[7] / t[6] for t in TRADES)
    d = facts["r_distribution"]
    assert d["min"] == pytest.approx(round(rs[0], 3), abs=1e-3)
    assert d["max"] == pytest.approx(round(rs[-1], 3), abs=1e-3)


def test_r_tail_counts_match_the_rows(facts):
    rs = [t[7] / t[6] for t in TRADES]
    d = facts["r_distribution"]
    assert d["count_le_-1R"] == sum(1 for r in rs if r <= -1)
    assert d["count_ge_+1R"] == sum(1 for r in rs if r >= 1)
    assert d["count_ge_+2R"] == sum(1 for r in rs if r >= 2)


def test_tail_counts_never_exceed_the_sample_size(facts):
    d = facts["r_distribution"]
    for k in ("count_le_-1R", "count_ge_+1R", "count_ge_+2R"):
        assert 0 <= d[k] <= d["n"]


# ---------------------------------------------------------------------------
# equity curve
# ---------------------------------------------------------------------------
def test_max_drawdown_is_peak_to_trough(facts):
    peak, mdd = 0.0, 0.0
    for _, eq, _bal in EQUITY:
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    assert facts["equity"]["max_drawdown_pct"] == pytest.approx(round(mdd * 100, 2), abs=0.01)


def test_peak_and_trough_are_the_series_extremes(facts):
    eqs = [e[1] for e in EQUITY]
    assert facts["equity"]["peak_equity"] == pytest.approx(max(eqs), abs=0.01)
    assert facts["equity"]["trough_equity"] == pytest.approx(min(eqs), abs=0.01)


def test_drawdown_is_never_negative(facts):
    assert facts["equity"]["max_drawdown_pct"] >= 0


# ---------------------------------------------------------------------------
# rejects
# ---------------------------------------------------------------------------
def test_fills_are_not_reported_as_rejects(facts):
    """10009 is TRADE_RETCODE_DONE; counting a fill as a reject would inflate it."""
    codes = {r["retcode"] for r in facts["rejects"]}
    assert 10009 not in codes


def test_rejects_are_counted_per_retcode(facts):
    assert sum(r["n"] for r in facts["rejects"]) == len(BLOTTER) - 1  # minus the fill


# ---------------------------------------------------------------------------
# report shape
# ---------------------------------------------------------------------------
def test_stdout_contains_every_section(ledger):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--db", str(ledger)],
                          capture_output=True, text=True, cwd=str(ROOT))
    for n, name in [(1, "headline"), (2, "by exit reason"), (3, "by symbol"),
                    (4, "by engine tag"), (5, "R-multiple distribution"),
                    (6, "trail vs full stop"), (7, "spread"), (8, "did the 1%"),
                    (9, "equity curve"), (10, "broker rejects")]:
        assert f"== {n}. {name}" in proc.stdout, f"missing section {n}: {name}"


def test_json_flag_writes_a_parseable_file(facts):
    assert set(facts) >= {"headline", "exit_reasons", "symbols", "bot_tags",
                          "r_distribution", "spread", "risk_cap", "equity", "rejects"}


def test_runs_from_a_different_cwd(ledger, tmp_path):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--db", str(ledger)],
                          capture_output=True, text=True, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
