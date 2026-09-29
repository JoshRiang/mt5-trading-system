"""Trading ERP — positions ledger + order blotter + risk limits + equity curve + reporting.

SQLite-backed, STDLIB ONLY (sqlite3 + datetime + pathlib) so it imports both
under native Linux AND under Wine Windows-Python (gamble bots wire fills here).

Design vs FINAL_BUILD_PLAN.md audit:
  - Plan assumed Exness native REST/WS API. Reality (verified 2026-09-20):
    execution is official MetaTrader5 package via Wine Windows-Python.
  - Plan's storage.py already has: asset_state, trades, news_log, daily_risk.
    This ERP module REUSES those tables and ADDS the missing ERP pieces:
      order_blotter  — every order_send attempt (fills AND rejects)
      equity_curve   — periodic equity/balance snapshots per bot
      risk_events    — floor/target/circuit/reject-guard events
    plus ALTERs trades with ticket/bot_tag/exit_reason columns if absent.

Tables:
  trades (extended): ticket, bot_tag, exit_reason
  order_blotter(id, ts, ticket, deal, symbol, direction, volume, price,
                sl, tp, retcode, comment, bot_tag)
  equity_curve(id, ts, equity, balance, profit, open_positions, bot_tag)
  risk_events(id, ts, event_type, detail, bot_tag)
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "trading_bot.db"


def _conn(db_path=None):
    p = str(db_path or DEFAULT_DB)
    c = sqlite3.connect(p)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_erp(db_path=None):
    """Create ERP tables; extend trades table if needed. Idempotent."""
    c = _conn(db_path)
    cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS order_blotter (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, ticket INTEGER, deal INTEGER,
        symbol TEXT NOT NULL, direction TEXT NOT NULL,
        volume REAL NOT NULL, price REAL NOT NULL,
        sl REAL, tp REAL, retcode INTEGER,
        comment TEXT, bot_tag TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS equity_curve (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, equity REAL NOT NULL, balance REAL,
        profit REAL, open_positions INTEGER, bot_tag TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS risk_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, event_type TEXT NOT NULL,
        detail TEXT, bot_tag TEXT)""")
    # extend legacy trades table (audit: it lacked ticket/bot linkage)
    cols = {r[1] for r in cur.execute("PRAGMA table_info(trades)").fetchall()}
    for col in ("ticket INTEGER", "bot_tag TEXT", "exit_reason TEXT"):
        name = col.split()[0]
        if name not in cols:
            cur.execute(f"ALTER TABLE trades ADD COLUMN {col}")
    c.commit()
    c.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def record_fill(db_or_path, symbol, direction, volume, price, sl, tp,
                ticket=None, deal=None, retcode=None, comment="",
                bot_tag="", risk_usd=0.0):
    """Log a fill to order_blotter + open row in trades ledger. Returns trade id."""
    init_erp(db_or_path)
    c = _conn(db_or_path)
    cur = c.cursor()
    ts = _now()
    cur.execute("""INSERT INTO order_blotter
        (ts, ticket, deal, symbol, direction, volume, price, sl, tp,
         retcode, comment, bot_tag)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts, ticket, deal, symbol, direction, volume, price, sl, tp,
         retcode, comment, bot_tag))
    cur.execute("""INSERT INTO trades
        (timestamp, symbol, direction, lot_size, entry_price, stop_loss,
         take_profit, risk_amount, status, strategy_name, notes, ticket, bot_tag)
        VALUES (?,?,?,?,?,?,?,?, 'OPEN',?,?,?,?)""",
        (ts, symbol, direction, volume, price, sl, tp, risk_usd,
         bot_tag, comment, ticket, bot_tag))
    tid = cur.lastrowid
    c.commit()
    c.close()
    return tid


def record_reject(db_or_path, symbol, direction, volume, retcode, comment, bot_tag=""):
    init_erp(db_or_path)
    c = _conn(db_or_path)
    c.execute("""INSERT INTO order_blotter
        (ts, symbol, direction, volume, price, retcode, comment, bot_tag)
        VALUES (?,?,?,?,0.0,?,?,?)""",
        (_now(), symbol, direction, volume, retcode, comment, bot_tag))
    c.commit()
    c.close()


def record_exit(db_or_path, ticket, exit_price, pnl, reason="", bot_tag="", symbol=""):
    """Close trades-ledger row(s) for ticket; log exit into blotter."""
    init_erp(db_or_path)
    c = _conn(db_or_path)
    cur = c.cursor()
    cur.execute("""UPDATE trades SET exit_price=?, pnl=?, status='CLOSED',
                   exit_reason=? WHERE ticket=? AND status='OPEN'""",
                (exit_price, pnl, reason, ticket))
    cur.execute("""INSERT INTO order_blotter
        (ts, ticket, symbol, direction, volume, price, retcode, comment, bot_tag)
        VALUES (?,?,?,?,?,?,?,?,?)""",
                (_now(), ticket, symbol, "EXIT", 0.0, exit_price, 0, reason, bot_tag))
    c.commit()
    c.close()


def record_equity(db_or_path, equity, balance=None, profit=None,
                  open_positions=0, bot_tag=""):
    init_erp(db_or_path)
    c = _conn(db_or_path)
    c.execute("""INSERT INTO equity_curve
        (ts, equity, balance, profit, open_positions, bot_tag)
        VALUES (?,?,?,?,?,?)""",
        (_now(), equity, balance, profit, open_positions, bot_tag))
    c.commit()
    c.close()


def risk_event(db_or_path, event_type, detail="", bot_tag=""):
    init_erp(db_or_path)
    c = _conn(db_or_path)
    c.execute("INSERT INTO risk_events (ts, event_type, detail, bot_tag)"
              " VALUES (?,?,?,?)", (_now(), event_type, detail, bot_tag))
    c.commit()
    c.close()


def report(db_path=None, bot_tag=None, limit=200):
    """ERP summary for dashboard/reporting. Returns dict of rows + totals."""
    init_erp(db_path)
    c = _conn(db_path)
    c.row_factory = sqlite3.Row
    tag = f"AND bot_tag='{bot_tag}'" if bot_tag else ""
    blotter = c.execute(
        f"SELECT * FROM order_blotter {'WHERE 1=1 ' + tag if tag else ''}"
        f" ORDER BY id DESC LIMIT {int(limit)}").fetchall()
    equity = c.execute(
        f"SELECT * FROM equity_curve {'WHERE 1=1 ' + tag if tag else ''}"
        f" ORDER BY id DESC LIMIT {int(limit)}").fetchall()
    events = c.execute(
        f"SELECT * FROM risk_events {'WHERE 1=1 ' + tag if tag else ''}"
        f" ORDER BY id DESC LIMIT 50").fetchall()
    closed = c.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(pnl),0) s FROM trades "
        f"WHERE status='CLOSED' {tag}").fetchone()
    op = c.execute(
        f"SELECT COUNT(*) n FROM trades WHERE status='OPEN' {tag}").fetchone()
    c.close()
    return {
        "blotter": [dict(r) for r in blotter],
        "equity_curve": [dict(r) for r in equity],
        "risk_events": [dict(r) for r in events],
        "closed_trades": closed["n"], "realized_pnl": closed["s"],
        "open_trades": op["n"],
    }


if __name__ == "__main__":
    init_erp()
    print("ERP tables ready at:", DEFAULT_DB)
    print(report(limit=3))
