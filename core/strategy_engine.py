"""
Strategy engine.
Calculates weighted signal strength from 6 variables.
"""

from typing import Dict, Optional, Any
from dataclasses import dataclass
from .state_machine import StateMachine, AssetState


@dataclass
class StrategyConfig:
    """Strategy configuration."""
    weights: Dict[str, float]
    buy_threshold: float = 0.6
    sell_threshold: float = 0.6
    news_filter_min_impact: float = 0.3


class StrategyEngine:
    """Evaluates trading signals based on weighted 6-variable model."""
    
    def __init__(self, state_machine: StateMachine):
        self.state_machine = state_machine
        self.default_weights = {
            "bullish_minutes": 0.2,
            "bullish_hours": 0.3,
            "bullish_daily": 0.5,
            "bearish_minutes": 0.2,
            "bearish_hours": 0.3,
            "bearish_daily": 0.5,
        }
    
    def evaluate(self, symbol: str, strategy: Optional[StrategyConfig] = None) -> Dict[str, Any]:
        """
        Evaluate trading signal for an asset.
        
        Returns:
            {
                "signal": "BUY" | "SELL" | "HOLD",
                "strength": float (-1.0 to 1.0),
                "confidence": float (0.0 to 1.0),
                "state": AssetState.to_dict(),
                "reason": str
            }
        """
        if strategy is None:
            strategy = StrategyConfig(weights=self.default_weights)
        
        state = self.state_machine.get_or_create(symbol)
        strength = state.calculate_signal_strength(strategy.weights)
        
        # Determine signal
        if strength > strategy.buy_threshold:
            signal = "BUY"
            confidence = min(1.0, abs(strength))
            reason = f"Bullish score {strength:.2f} > threshold {strategy.buy_threshold}"
        elif strength < -strategy.sell_threshold:
            signal = "SELL"
            confidence = min(1.0, abs(strength))
            reason = f"Bearish score {abs(strength):.2f} > threshold {strategy.sell_threshold}"
        else:
            signal = "HOLD"
            confidence = 0.0
            reason = f"Score {strength:.2f} within neutral zone"
        
        return {
            "signal": signal,
            "strength": strength,
            "confidence": confidence,
            "state": state.to_dict(),
            "reason": reason,
        }
    
    def evaluate_all(self, symbols: list[str], strategy: Optional[StrategyConfig] = None) -> Dict[str, Dict[str, Any]]:
        """Evaluate all symbols."""
        results = {}
        for symbol in symbols:
            results[symbol] = self.evaluate(symbol, strategy)
        return results


if __name__ == "__main__":
    from state_machine import StateMachine
    
    sm = StateMachine()
    sm.update_from_news("BTCUSD", 0.8, "daily")
    sm.update_from_news("BTCUSD", -0.2, "hours")
    
    engine = StrategyEngine(sm)
    result = engine.evaluate("BTCUSD")
    print(f"Signal: {result['signal']}, Strength: {result['strength']:.2f}")
    print(f"Reason: {result['reason']}")
