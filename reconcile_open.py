#!/usr/bin/env python3
"""reconcile_open.py — true-up stale OPEN rows in the trading ERP.

WHY THIS EXISTS
---------------
When the bot stops (equity floor, 12h limit, manual kill), positions closed by
that shutdown never get a `record_exit` call: `close_all()` flattens at the
broker but the ERP rows stay `status='OPEN'` forever. The dashboard then shows
phantom exposure (observed: 240 stale OPEN rows while real exposure was 0).

WHAT IT DOES
------------
Reads the broker's OWN deal history (the source of truth) and, for every ERP row
still marked OPEN whose ticket has a matching closing deal, writes the real exit
price / pnl / reason and flips the row to CLOSED. Rows with no closing deal are
reported as genuinely open and left untouched.

READ-ONLY on the broker: it only queries history. It never places or closes an
order. Safe to run while the bot is live.

USAGE
-----
    WINEPREFIX=/home/josh/.wine DISPLAY=:20 \
      /opt/wine-stable/bin/wine 'C:\\users\\josh\\AppData\\Local\\Programs\\Python\\Python311\\python.exe' \
      'Z:\\home\\josh\\trading_bot\\reconcile_open.py' [--apply]

Without --apply it only reports what it WOULD change (dry run).
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "trading_bot.db"


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def load_open_rows(conn):
    cur = conn.execute(
        "SELECT id, ticket, symbol, direction, lot_size, bot_tag "
        "FROM trades WHERE status='OPEN' AND ticket IS NOT NULL"
    )
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the corrections (default: dry run)")
    args = ap.parse_args()

    try:
        import MetaTrader5 as mt5
    except Exception as e:  # noqa: BLE001
        log(f"FATAL: MetaTrader5 module unavailable: {e}")
        return 2

    login = int(os.environ["MT5_LOGIN"])
    password = os.environ["MT5_PASSWORD"]
    server = os.environ["MT5_SERVER"]
    path = os.environ.get(
        "MT5_PATH", r"C:\Program Files\Exness MT5 6180\terminal64.exe")

    if not mt5.initialize(path=path, login=login, password=password,
                          server=server, timeout=60000):
        log(f"FATAL: mt5.initialize failed: {mt5.last_error()}")
        return 2

    ai = mt5.account_info()
    log(f"connected login={ai.login if ai else '?'} "
        f"server={ai.server if ai else '?'} "
        f"balance={ai.balance if ai else '?'}")

    conn = sqlite3.connect(DB)
    rows = load_open_rows(conn)
    log(f"ERP rows marked OPEN: {len(rows)}")

    # Index the broker's closing deals by position ticket.
    # NOTE: history_deals_get rejects an unbounded/very old start date and
    # returns None (observed: start=2020 -> None, start=now-7d -> 1544 deals).
    # Walk a bounded lookback and use the first window that yields data.
    now = datetime.now(timezone.utc)
    deals = None
    for days in (30, 14, 7, 3, 1):
        deals = mt5.history_deals_get(now - timedelta(days=days), now)
        if deals:
            log(f"history window: last {days}d -> {len(deals)} deals")
            break
    closers = {}
    if deals:
        for d in deals:
            # DEAL_ENTRY_OUT (1) closes an existing position.
            if getattr(d, "entry", None) == 1:
                closers.setdefault(d.position_id, []).append(d)
    log(f"broker closing deals indexed: {len(closers)} positions")

    fixed, still_open, no_match = 0, 0, 0
    for row_id, ticket, symbol, direction, lot, tag in rows:
        out = closers.get(ticket)
        if not out:
            # No closing deal: could be genuinely open, or history pruned.
            live = mt5.positions_get(ticket=ticket)
            if live:
                still_open += 1
            else:
                no_match += 1
            continue

        pnl = sum(getattr(d, "profit", 0.0) or 0.0 for d in out)
        pnl += sum(getattr(d, "swap", 0.0) or 0.0 for d in out)
        pnl += sum(getattr(d, "commission", 0.0) or 0.0 for d in out)
        exit_price = getattr(out[-1], "price", None)
        reason = "BROKER_CLOSE"

        if args.apply:
            conn.execute(
                "UPDATE trades SET status='CLOSED', pnl=?, exit_price=?, "
                "exit_reason=? WHERE id=?",
                (round(pnl, 2), exit_price, reason, row_id))
        fixed += 1

    if args.apply:
        conn.commit()

    log(f"RESULT: {'APPLIED' if args.apply else 'DRY RUN'} — "
        f"reconciled={fixed} genuinely_open={still_open} "
        f"no_deal_no_position={no_match}")
    if not args.apply:
        log("re-run with --apply to write these corrections")

    conn.close()
    mt5.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
