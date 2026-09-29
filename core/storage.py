"""
Storage module for trading bot.
Handles SQLite persistence for:
- Asset state (6 variables per asset)
- Trade history
- News analysis log
- System logs
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trading_bot.db"


def get_connection() -> sqlite3.Connection:
    """Get SQLite connection with row factory."""
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize database schema."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Asset state table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS asset_state (
            symbol TEXT PRIMARY KEY,
            bullish_minutes REAL DEFAULT 0.0,
            bullish_hours REAL DEFAULT 0.0,
            bullish_daily REAL DEFAULT 0.0,
            bearish_minutes REAL DEFAULT 0.0,
            bearish_hours REAL DEFAULT 0.0,
            bearish_daily REAL DEFAULT 0.0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_news_impact REAL DEFAULT 0.0
        )
    """)
    
    # Trades table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            lot_size REAL NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL,
            stop_loss REAL,
            take_profit REAL,
            risk_amount REAL NOT NULL,
            pnl REAL DEFAULT 0.0,
            status TEXT DEFAULT 'OPEN',
            strategy_name TEXT,
            notes TEXT
        )
    """)
    
    # News analysis log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS news_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL,
            headline TEXT NOT NULL,
            symbol TEXT NOT NULL,
            impact_score REAL NOT NULL,
            variable_updates TEXT,
            analyzed_by TEXT DEFAULT 'hermes_agent'
        )
    """)
    
    # Daily risk tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_risk (
            date TEXT PRIMARY KEY,
            total_risk_used REAL DEFAULT 0.0,
            total_pnl REAL DEFAULT 0.0,
            trades_count INTEGER DEFAULT 0,
            stopped_early BOOLEAN DEFAULT 0
        )
    """)
    
    conn.commit()
    conn.close()


def upsert_asset_state(symbol: str, **kwargs):
    """Update or insert asset state."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Check if exists
    cursor.execute("SELECT symbol FROM asset_state WHERE symbol = ?", (symbol,))
    exists = cursor.fetchone()
    
    if exists:
        # Update
        set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [datetime.now().isoformat(), symbol]
        cursor.execute(f"""
            UPDATE asset_state SET {set_clause}, last_updated = ? WHERE symbol = ?
        """, values)
    else:
        # Insert
        columns = ["symbol"] + list(kwargs.keys()) + ["last_updated"]
        placeholders = ["?"] * len(columns)
        values = [symbol] + list(kwargs.values()) + [datetime.now().isoformat()]
        cursor.execute(f"""
            INSERT INTO asset_state ({", ".join(columns)}) VALUES ({", ".join(placeholders)})
        """, values)
    
    conn.commit()
    conn.close()


def get_asset_state(symbol: str) -> Optional[Dict[str, Any]]:
    """Get current state for an asset."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM asset_state WHERE symbol = ?", (symbol,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return dict(row)
    return None


def get_all_asset_states() -> Dict[str, Dict[str, Any]]:
    """Get state for all assets."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM asset_state")
    rows = cursor.fetchall()
    conn.close()
    
    return {row["symbol"]: dict(row) for row in rows}


def log_trade(symbol: str, direction: str, lot_size: float, entry_price: float,
              risk_amount: float, stop_loss: Optional[float] = None,
              take_profit: Optional[float] = None, strategy_name: Optional[str] = None,
              notes: Optional[str] = None) -> int:
    """Log a new trade."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO trades (symbol, direction, lot_size, entry_price, stop_loss, take_profit,
                           risk_amount, strategy_name, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, direction, lot_size, entry_price, stop_loss, take_profit,
          risk_amount, strategy_name, notes))
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def close_trade(trade_id: int, exit_price: float, pnl: float):
    """Close an open trade."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE trades SET exit_price = ?, pnl = ?, status = 'CLOSED' WHERE id = ?
    """, (exit_price, pnl, trade_id))
    conn.commit()
    conn.close()


def log_news_analysis(source: str, headline: str, symbol: str, impact_score: float,
                      variable_updates: Dict[str, float]):
    """Log news analysis result."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO news_log (source, headline, symbol, impact_score, variable_updates)
        VALUES (?, ?, ?, ?, ?)
    """, (source, headline, symbol, impact_score, json.dumps(variable_updates)))
    conn.commit()
    conn.close()


def get_daily_risk(date: str) -> Optional[Dict[str, Any]]:
    """Get daily risk usage for a specific date."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM daily_risk WHERE date = ?", (date,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return dict(row)
    return None


def update_daily_risk(date: str, risk_used: float, pnl: float, trades_count: int):
    """Update or insert daily risk record."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO daily_risk (date, total_risk_used, total_pnl, trades_count)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            total_risk_used = total_risk_used + ?,
            total_pnl = total_pnl + ?,
            trades_count = trades_count + ?
    """, (date, risk_used, pnl, trades_count, risk_used, pnl, trades_count))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    # Initialize database
    init_db()
    print("Database initialized at:", DB_PATH)
