"""
Telegram alerts for trading bot.
Sends notifications for trades, signals, and errors.
"""

import requests
from typing import Optional
from pathlib import Path
import yaml

config_path = Path(__file__).resolve().parent.parent / "config" / "exness.yaml"
with open(config_path) as f:
    config = yaml.safe_load(f)

TELEGRAM_TOKEN = config.get("telegram", {}).get("bot_token", "")
TELEGRAM_CHAT_ID = config.get("telegram", {}).get("chat_id", "")


def send_telegram_message(message: str, parse_mode: str = "Markdown") -> bool:
    """Send Telegram message."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False


def send_trade_alert(symbol: str, direction: str, lot_size: float, 
                     entry_price: float, signal_strength: float):
    """Send trade execution alert."""
    emoji = "🟢" if direction == "BUY" else "🔴"
    message = (
        f"{emoji} **TRADE EXECUTED**\n"
        f"Symbol: {symbol}\n"
        f"Direction: {direction}\n"
        f"Lot Size: {lot_size}\n"
        f"Entry Price: {entry_price}\n"
        f"Signal Strength: {signal_strength:.2f}"
    )
    return send_telegram_message(message)


def send_signal_alert(symbol: str, signal: str, strength: float):
    """Send trading signal alert."""
    if signal == "BUY":
        emoji = "📈"
    elif signal == "SELL":
        emoji = "📉"
    else:
        emoji = "⏸️"
    
    message = (
        f"{emoji} **SIGNAL: {signal}**\n"
        f"Symbol: {symbol}\n"
        f"Strength: {strength:.2f}"
    )
    return send_telegram_message(message)


def send_error_alert(error_message: str):
    """Send error alert."""
    message = f"⚠️ **ERROR**\n{error_message}"
    return send_telegram_message(message)


def send_daily_summary(balance: float, pnl: float, trades: int, risk_used: float):
    """Send daily summary."""
    emoji = "✅" if pnl >= 0 else "❌"
    message = (
        f"📊 **DAILY SUMMARY**\n"
        f"Balance: ${balance:.2f}\n"
        f"P&L: ${pnl:+.2f}\n"
        f"Trades: {trades}\n"
        f"Risk Used: {risk_used*100:.1f}%"
    )
    return send_telegram_message(message)


if __name__ == "__main__":
    # Test
    print("Testing Telegram alerts...")
    send_trade_alert("BTCUSD", "BUY", 0.01, 50000.0, 0.75)
    send_signal_alert("XAUUSD", "SELL", -0.82)
    send_daily_summary(10050.0, 50.0, 3, 0.008)
