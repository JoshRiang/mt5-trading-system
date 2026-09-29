#!/usr/bin/env python3
"""Run the trading bot with dashboard."""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.storage import init_db
from core.state_machine import StateMachine
from core.strategy_engine import StrategyEngine, StrategyConfig
from core.risk_manager import RiskManager, RiskConfig
from core.news_analyzer import NewsAnalyzer
from core.alerts import send_trade_alert, send_daily_summary
# NOTE: streamlit is imported lazily inside run_dashboard() so that
# `run.py --mode bot|test` works on systems without the dashboard deps.


def run_bot():
    """Run the bot in headless mode."""
    from main import TradingBot
    bot = TradingBot()
    bot.run(news_interval=300, strategy_interval=60)


def run_dashboard():
    """Run the Streamlit dashboard."""
    import streamlit as st
    from dashboard.app import show_home, show_strategy_maker, show_news_effect, show_logs, show_settings
    
    st.set_page_config(
        page_title="Trading Bot Terminal",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    
    # ... dashboard code ...


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trading Bot")
    parser.add_argument("--mode", choices=["bot", "dashboard", "test"], default="test",
                       help="Run mode: bot (headless), dashboard (Streamlit), test")
    args = parser.parse_args()
    
    if args.mode == "test":
        from main import TradingBot
        bot = TradingBot()
        bot.run_news_cycle()
        bot.run_strategy_cycle()
    elif args.mode == "bot":
        run_bot()
    elif args.mode == "dashboard":
        run_dashboard()
