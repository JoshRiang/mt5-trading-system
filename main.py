"""
Main entry point for trading bot.
Runs the core loop: news analysis → state update → strategy evaluation → execution.
"""

import time
import signal
import sys
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.state_machine import StateMachine
from core.strategy_engine import StrategyEngine, StrategyConfig
from core.risk_manager import RiskManager, RiskConfig
from core.news_analyzer import NewsAnalyzer
from core.storage import init_db, get_all_asset_states, log_news_analysis, log_trade
from core.alerts import send_trade_alert, send_error_alert
from core.mt5_client import MT5Client
# Bot-side symbol -> broker symbol (OILUSD -> USOIL). MT5Client applies this on
# every MT5 call, so the OILUSD keys below stay valid as internal identifiers
# while the broker always receives a symbol it actually knows.
from core.symbols import resolve_symbol
# Legacy fallback (Exness REST client, pre-MT5 bridge):
# from core.exness_client import ExnessClient


class TradingBot:
    """Main trading bot."""
    
    def __init__(self):
        self.state_machine = StateMachine()
        self.strategy_engine = StrategyEngine(self.state_machine)
        self.risk_manager = RiskManager()
        self.news_analyzer = NewsAnalyzer()
        self.mt5_client: Optional[MT5Client] = None
        # Legacy: self.exness_client (ExnessClient) — replaced by mt5_client.
        self.running = False
        self.live_trading = False  # MUST be explicitly enabled via enable_live_trading()

        # Initialize database
        init_db()

        # Load existing states from DB
        self._load_states()

        # Execution client (lazy: created on first execution cycle / enable_live_trading)
        self._client: Optional[MT5Client] = None

    def _get_client(self) -> MT5Client:
        """Lazy MT5Client (loads .env on first use)."""
        if self._client is None:
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).resolve().parent / ".env")
            self._client = MT5Client()
            self.mt5_client = self._client
        return self._client

    def enable_live_trading(self) -> None:
        """Explicit opt-in for LIVE orders. Without this, execution stays paper/dry-run."""
        self.live_trading = True

    # Keyed on the BOT-side symbol (what strategy signals / news carry).
    # MT5Client._mt5_symbol() maps these to broker symbols on every MT5 call
    # (notably OILUSD -> USOIL — there is no OILUSD on Exness).
    PIP_VALUE = {"BTCUSD": 1.0, "XAUUSD": 1.0, "OILUSD": 1.0, "XAGUSD": 1.0}
    SL_PIPS = {"BTCUSD": 500.0, "XAUUSD": 5.0, "OILUSD": 1.5, "XAGUSD": 0.5}

    def run_execution_cycle(self, signals: Dict[str, Dict]) -> Dict[str, Dict]:
        """Execute strategy signals via MT5: risk gate -> price -> size -> order -> log -> alert."""
        results: Dict[str, Dict] = {}
        client = self._get_client()

        # 1. Account / risk gates
        acct = client.get_account_info()
        if acct.get("paper"):
            print("  [exec] PAPER mode (no live MT5 connection) — dry-run only.")
        balance = float(acct.get("balance") or 0.0)
        if not acct.get("paper") and balance <= 0:
            msg = f"Refusing to trade: account balance={balance}"
            print(f"  [exec] {msg}")
            send_error_alert(msg)
            return results
        if self.risk_manager.check_daily_limit_reached():
            print("  [exec] Daily risk cap reached — no new orders.")
            return results
        if not acct.get("paper") and self.risk_manager.check_stop_trading(balance):
            print("  [exec] Stop-trading condition hit — no new orders.")
            return results

        # 2. Allocate risk across actionable signals
        allocation = self.risk_manager.allocate_risk(signals, balance or 10000.0)
        if not allocation:
            print("  [exec] No actionable allocation.")
            return results

        for symbol, risk_frac in allocation.items():
            sig = signals.get(symbol, {})
            direction = sig.get("signal")
            strength = float(sig.get("strength", 0.0))
            if direction not in ("BUY", "SELL"):
                continue

            # 3. Fresh price
            ticks = client.get_ticks(symbol, count=1)
            tick_list = ticks.get("ticks") or []
            if not tick_list:
                results[symbol] = {"error": "no price"}
                continue
            last = tick_list[-1]
            entry = float(last.get("bid") if direction == "SELL" else last.get("ask",
                          last.get("price", 0.0)) or last.get("price", 0.0))

            # 4. SL/TP from ATR-proxy pips, RR 1:3
            sl_dist = self.SL_PIPS.get(symbol, 1.0)
            sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
            tp = entry + 3 * sl_dist if direction == "BUY" else entry - 3 * sl_dist

            # 5. Size from risk manager
            lots = self.risk_manager.calculate_position_size(
                symbol, strength, entry, sl, balance or 10000.0,
                self.PIP_VALUE.get(symbol, 1.0))

            if not self.live_trading:
                print(f"  [exec] DRY-RUN {direction} {symbol} {lots} lots @ ~{entry} "
                      f"(SL {sl:.2f} / TP {tp:.2f}) — live_trading not enabled.")
                results[symbol] = {"dry_run": True, "direction": direction,
                                   "lot_size": lots, "entry": entry}
                continue

            # 6. LIVE order
            order = client.place_order(symbol, direction, lots, stop_loss=sl,
                                       take_profit=tp, comment="trading-bot")
            if order.get("error"):
                print(f"  [exec] ORDER FAILED {symbol}: {order['error']}")
                send_error_alert(f"Order failed {symbol} {direction}: {order['error']}")
                results[symbol] = order
                continue

            # 7. Log + risk bookkeeping + alert
            risk_usd = (balance or 0.0) * risk_frac
            try:
                log_trade(symbol, direction, lots, entry, risk_usd, sl, tp,
                          strategy_name="default", notes=f"strength={strength:+.2f}")
            except Exception as e:
                print(f"  [exec] log_trade failed: {e}")
            self.risk_manager.record_trade(risk_frac)
            try:
                send_trade_alert(symbol, direction, lots, entry, strength)
            except Exception as e:
                print(f"  [exec] alert failed: {e}")
            print(f"  [exec] LIVE {direction} {symbol} {lots} lots @ ~{entry} "
                  f"order={order.get('order_id')}")
            results[symbol] = order

        return results

    def _load_states(self):
        """Load existing states from database."""
        states = get_all_asset_states()
        for symbol, state_data in states.items():
            state = self.state_machine.get_or_create(symbol)
            state.bullish_minutes = state_data.get("bullish_minutes", 0.0)
            state.bullish_hours = state_data.get("bullish_hours", 0.0)
            state.bullish_daily = state_data.get("bullish_daily", 0.0)
            state.bearish_minutes = state_data.get("bearish_minutes", 0.0)
            state.bearish_hours = state_data.get("bearish_hours", 0.0)
            state.bearish_daily = state_data.get("bearish_daily", 0.0)
    
    def run_news_cycle(self):
        """Run one news analysis cycle."""
        print(f"\n[{datetime.now()}] Running news analysis cycle...")
        
        # Fetch news
        news_items = self.news_analyzer.fetch_all_news()
        print(f"  Fetched {len(news_items)} news items")
        
        # Analyze
        analysis = self.news_analyzer.analyze_batch(news_items)
        
        # Update state machine
        for symbol, data in analysis.items():
            if data["total_impact"] != 0:
                self.state_machine.update_from_news(symbol, data["total_impact"], "daily")
                print(f"  {symbol}: impact {data['total_impact']:+.2f} ({data['news_count']} news)")
                
                # Log to database
                for item in data["items"]:
                    log_news_analysis(
                        source=item["source"],
                        headline=item["headline"],
                        symbol=symbol,
                        impact_score=item["impact"],
                        variable_updates={"daily": item["impact"]},
                    )
        
        return analysis
    
    def run_strategy_cycle(self) -> Dict[str, Dict]:
        """Run one strategy evaluation cycle."""
        # Canonical bot-side names; MT5Client maps OILUSD -> USOIL at the
        # broker boundary (there is no OILUSD symbol on Exness).
        symbols = ["BTCUSD", "XAUUSD", "OILUSD", "XAGUSD"]
        
        strategy = StrategyConfig(
            weights={
                "bullish_minutes": 0.2,
                "bullish_hours": 0.3,
                "bullish_daily": 0.5,
                "bearish_minutes": 0.2,
                "bearish_hours": 0.3,
                "bearish_daily": 0.5,
            },
            buy_threshold=0.6,
            sell_threshold=0.6,
        )
        
        results = self.strategy_engine.evaluate_all(symbols, strategy)
        
        print("\nStrategy signals:")
        for symbol, result in results.items():
            print(f"  {symbol}: {result['signal']} (strength: {result['strength']:+.2f})")
        
        return results
    
    def run(self, news_interval: int = 300, strategy_interval: int = 60):
        """
        Main bot loop.
        
        Args:
            news_interval: seconds between news cycles
            strategy_interval: seconds between strategy cycles
        """
        self.running = True
        
        # Setup signal handlers
        def signal_handler(sig, frame):
            print("\n\nShutting down gracefully...")
            self.running = False
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        print("=" * 60)
        print("TRADING BOT STARTED")
        print("=" * 60)
        print(f"News cycle: every {news_interval}s")
        print(f"Strategy cycle: every {strategy_interval}s")
        print("Assets: BTCUSD, XAUUSD, OILUSD(->USOIL), XAGUSD")
        print("=" * 60)
        
        last_news = 0
        last_strategy = 0
        
        while self.running:
            now = time.time()
            
            # News cycle
            if now - last_news >= news_interval:
                self.run_news_cycle()
                last_news = now
            
            # Strategy cycle
            if now - last_strategy >= strategy_interval:
                self.run_strategy_cycle()
                last_strategy = now
            
            # Sleep
            time.sleep(1)
    
    def stop(self):
        """Stop the bot."""
        self.running = False


if __name__ == "__main__":
    bot = TradingBot()
    
    # Test mode: run one cycle
    print("Running test cycle...")
    bot.run_news_cycle()
    bot.run_strategy_cycle()
    
    print("\nTest complete. To run continuously: bot.run()")
