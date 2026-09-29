"""
Backtest engine.
Runs strategy against historical data.
"""

from typing import Dict, List, Any
from datetime import datetime
from core.state_machine import StateMachine
from core.strategy_engine import StrategyEngine, StrategyConfig
from core.risk_manager import RiskManager, RiskConfig


class BacktestResult:
    """Stores backtest results."""
    
    def __init__(self):
        self.trades: List[Dict] = []
        self.initial_balance: float = 10000.0
        self.final_balance: float = 10000.0
        self.win_count: int = 0
        self.loss_count: int = 0
        self.total_pnl: float = 0.0
        self.max_drawdown: float = 0.0
        self.start_date: Optional[str] = None
        self.end_date: Optional[str] = None
    
    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total if total > 0 else 0.0
    
    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t["pnl"] for t in self.trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in self.trades if t["pnl"] < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "initial_balance": self.initial_balance,
            "final_balance": self.final_balance,
            "total_pnl": self.total_pnl,
            "return_pct": (self.final_balance - self.initial_balance) / self.initial_balance * 100,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "max_drawdown": self.max_drawdown,
            "trade_count": len(self.trades),
            "start_date": self.start_date,
            "end_date": self.end_date,
        }


class BacktestEngine:
    """Backtests trading strategies."""
    
    def __init__(self, initial_balance: float = 10000.0):
        self.initial_balance = initial_balance
        self.state_machine = StateMachine()
        self.strategy_engine = StrategyEngine(self.state_machine)
        self.risk_manager = RiskManager(RiskConfig())
    
    def run(self, historical_data: Dict[str, List[Dict]], strategy: StrategyConfig) -> BacktestResult:
        """
        Run backtest on historical data.
        
        Args:
            historical_data: {symbol: [{timestamp, price, ...}, ...]}
            strategy: StrategyConfig
        
        Returns:
            BacktestResult
        """
        result = BacktestResult()
        result.start_date = datetime.now().isoformat()
        
        balance = self.initial_balance
        peak = balance
        
        # Simulate each timestep
        for symbol, data in historical_data.items():
            for i, candle in enumerate(data):
                # Update state from price action (simplified)
                price = candle.get("close", 0)
                if price <= 0:
                    continue
                
                # Simulate news impact (in real backtest, use actual news)
                # For now, use price momentum as proxy
                if i > 0:
                    prev_price = data[i-1].get("close", price)
                    price_change = (price - prev_price) / prev_price
                    
                    # Map price change to 6 variables
                    if price_change > 0.01:
                        self.state_machine.update_from_news(symbol, 0.5, "hours")
                    elif price_change < -0.01:
                        self.state_machine.update_from_news(symbol, -0.5, "hours")
                
                # Evaluate strategy
                signal = self.strategy_engine.evaluate(symbol, strategy)
                
                # Execute trade if signal
                if signal["signal"] in ["BUY", "SELL"] and self.risk_manager.open_positions < 4:
                    # Simplified position sizing
                    risk_amount = balance * 0.01 * signal["confidence"]
                    pnl = risk_amount * signal["strength"] * 0.3  # Simplified PnL
                    
                    trade = {
                        "timestamp": candle.get("timestamp", datetime.now().isoformat()),
                        "symbol": symbol,
                        "direction": signal["signal"],
                        "pnl": pnl,
                        "confidence": signal["confidence"],
                    }
                    result.trades.append(trade)
                    
                    balance += pnl
                    self.risk_manager.record_trade(0.01, pnl)
                    
                    if pnl > 0:
                        result.win_count += 1
                    else:
                        result.loss_count += 1
                    
                    # Track drawdown
                    if balance > peak:
                        peak = balance
                    drawdown = (peak - balance) / peak
                    if drawdown > result.max_drawdown:
                        result.max_drawdown = drawdown
        
        result.end_date = datetime.now().isoformat()
        result.final_balance = balance
        result.total_pnl = balance - self.initial_balance
        
        return result
    
    def run_walk_forward(self, historical_data: Dict[str, List[Dict]], 
                         strategy: StrategyConfig, window: int = 100) -> List[BacktestResult]:
        """
        Run walk-forward analysis.
        
        Args:
            historical_data: full dataset
            strategy: StrategyConfig
            window: rolling window size
        
        Returns:
            List of BacktestResult per window
        """
        results = []
        symbols = list(historical_data.keys())
        min_length = min(len(data) for data in historical_data.values())
        
        for i in range(0, min_length - window, window // 2):
            # Slice window
            window_data = {}
            for symbol, data in historical_data.items():
                window_data[symbol] = data[i:i+window]
            
            # Run backtest
            result = self.run(window_data, strategy)
            results.append(result)
        
        return results


if __name__ == "__main__":
    # Test with dummy data
    engine = BacktestEngine()
    
    dummy_data = {
        "BTCUSD": [
            {"timestamp": f"2026-09-0{i}T00:00:00", "close": 50000 + i * 100}
            for i in range(1, 31)
        ]
    }
    
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
    
    result = engine.run(dummy_data, strategy)
    print(f"Backtest result: {result.to_dict()}")
