"""
State machine for 6 variables per asset.
Each asset has:
  bullish_minutes, bullish_hours, bullish_daily
  bearish_minutes, bearish_hours, bearish_daily
"""

from dataclasses import dataclass, field
from typing import Dict, Optional, Any
from datetime import datetime


@dataclass
class AssetState:
    """State for a single trading asset."""
    symbol: str
    bullish_minutes: float = 0.0
    bullish_hours: float = 0.0
    bullish_daily: float = 0.0
    bearish_minutes: float = 0.0
    bearish_hours: float = 0.0
    bearish_daily: float = 0.0
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())
    last_news_impact: float = 0.0

    @property
    def neutral_minutes(self) -> float:
        return max(0.0, 1.0 - self.bullish_minutes - self.bearish_minutes)

    @property
    def neutral_hours(self) -> float:
        return max(0.0, 1.0 - self.bullish_hours - self.bearish_hours)

    @property
    def neutral_daily(self) -> float:
        return max(0.0, 1.0 - self.bullish_daily - self.bearish_daily)

    def to_dict(self) -> Dict[str, float]:
        """Convert to dictionary."""
        return {
            "symbol": self.symbol,
            "bullish_minutes": self.bullish_minutes,
            "bullish_hours": self.bullish_hours,
            "bullish_daily": self.bullish_daily,
            "bearish_minutes": self.bearish_minutes,
            "bearish_hours": self.bearish_hours,
            "bearish_daily": self.bearish_daily,
            "neutral_minutes": self.neutral_minutes,
            "neutral_hours": self.neutral_hours,
            "neutral_daily": self.neutral_daily,
            "last_updated": self.last_updated,
            "last_news_impact": self.last_news_impact,
        }

    def apply_news_impact(self, impact: float, timeframe: str = "daily"):
        """
        Apply news impact to variables.
        
        Args:
            impact: -1.0 to 1.0
            timeframe: "minutes", "hours", or "daily"
        """
        if timeframe == "daily":
            if impact > 0:
                self.bullish_daily = min(1.0, self.bullish_daily + impact)
                self.bearish_daily = max(0.0, self.bearish_daily - impact * 0.5)
            else:
                self.bearish_daily = min(1.0, self.bearish_daily + abs(impact))
                self.bullish_daily = max(0.0, self.bullish_daily - abs(impact) * 0.5)
        elif timeframe == "hours":
            if impact > 0:
                self.bullish_hours = min(1.0, self.bullish_hours + impact)
                self.bearish_hours = max(0.0, self.bearish_hours - impact * 0.5)
            else:
                self.bearish_hours = min(1.0, self.bearish_hours + abs(impact))
                self.bullish_hours = max(0.0, self.bullish_hours - abs(impact) * 0.5)
        elif timeframe == "minutes":
            if impact > 0:
                self.bullish_minutes = min(1.0, self.bullish_minutes + impact)
                self.bearish_minutes = max(0.0, self.bearish_minutes - impact * 0.5)
            else:
                self.bearish_minutes = min(1.0, self.bearish_minutes + abs(impact))
                self.bullish_minutes = max(0.0, self.bullish_minutes - abs(impact) * 0.5)
        
        self.last_news_impact = impact
        self.last_updated = datetime.now().isoformat()

    def apply_decay(self, decay_minutes: float = 0.01, decay_hours: float = 0.05, decay_daily: float = 0.1):
        """
        Apply time decay to news impact.
        Impact fades over time.
        """
        # Bullish decay
        self.bullish_minutes = max(0.0, self.bullish_minutes - decay_minutes)
        self.bullish_hours = max(0.0, self.bullish_hours - decay_hours)
        self.bullish_daily = max(0.0, self.bullish_daily - decay_daily)
        
        # Bearish decay
        self.bearish_minutes = max(0.0, self.bearish_minutes - decay_minutes)
        self.bearish_hours = max(0.0, self.bearish_hours - decay_hours)
        self.bearish_daily = max(0.0, self.bearish_daily - decay_daily)
        
        self.last_updated = datetime.now().isoformat()

    def calculate_signal_strength(self, weights: Dict[str, float]) -> float:
        """
        Calculate weighted signal strength.
        
        Args:
            weights: {"bullish_minutes": 0.2, "bullish_hours": 0.3, ...}
        
        Returns:
            Signal strength: -1.0 to 1.0
        """
        bullish_score = (
            self.bullish_minutes * weights.get("bullish_minutes", 0.2) +
            self.bullish_hours * weights.get("bullish_hours", 0.3) +
            self.bullish_daily * weights.get("bullish_daily", 0.5)
        )
        
        bearish_score = (
            self.bearish_minutes * weights.get("bearish_minutes", 0.2) +
            self.bearish_hours * weights.get("bearish_hours", 0.3) +
            self.bearish_daily * weights.get("bearish_daily", 0.5)
        )
        
        # Time hierarchy: daily dominates
        if self.bullish_daily > 0.6 or self.bearish_daily > 0.6:
            if self.bullish_daily > 0.6 and self.bearish_hours > 0.6:
                # Conflict: dampen both
                bullish_score *= 0.7
                bearish_score *= 0.7
            elif self.bullish_daily > 0.6:
                bullish_score *= 1.2  # consistency bonus
            elif self.bearish_daily > 0.6:
                bearish_score *= 1.2
        
        return bullish_score - bearish_score


class StateMachine:
    """Manages state for all trading assets."""
    
    def __init__(self):
        self.states: Dict[str, AssetState] = {}
    
    def get_or_create(self, symbol: str) -> AssetState:
        """Get existing state or create new one."""
        if symbol not in self.states:
            self.states[symbol] = AssetState(symbol=symbol)
        return self.states[symbol]
    
    def update_from_news(self, symbol: str, impact: float, timeframe: str = "daily"):
        """Update asset state from news impact."""
        state = self.get_or_create(symbol)
        state.apply_news_impact(impact, timeframe)
        return state
    
    def apply_decay_all(self):
        """Apply time decay to all assets."""
        for state in self.states.values():
            state.apply_decay()
    
    def get_signal(self, symbol: str, weights: Dict[str, float]) -> Dict[str, Any]:
        """
        Get trading signal for an asset.
        
        Returns:
            {"signal": "BUY"|"SELL"|"HOLD", "strength": float, "state": AssetState}
        """
        state = self.get_or_create(symbol)
        strength = state.calculate_signal_strength(weights)
        
        if strength > 0.6:
            signal = "BUY"
        elif strength < -0.6:
            signal = "SELL"
        else:
            signal = "HOLD"
        
        return {
            "signal": signal,
            "strength": strength,
            "state": state.to_dict(),
        }
    
    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """Get all asset states."""
        return {symbol: state.to_dict() for symbol, state in self.states.items()}


if __name__ == "__main__":
    # Test
    sm = StateMachine()
    sm.update_from_news("BTCUSD", 0.8, "daily")
    sm.update_from_news("BTCUSD", -0.3, "hours")
    
    weights = {
        "bullish_minutes": 0.2,
        "bullish_hours": 0.3,
        "bullish_daily": 0.5,
        "bearish_minutes": 0.2,
        "bearish_hours": 0.3,
        "bearish_daily": 0.5,
    }
    
    signal = sm.get_signal("BTCUSD", weights)
    print(f"Signal: {signal['signal']}, Strength: {signal['strength']:.2f}")
    print(f"State: {signal['state']}")
