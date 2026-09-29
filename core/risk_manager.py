"""
Risk manager.
Handles position sizing, daily risk cap, and circuit breaker.
"""

from typing import Dict, Optional, Any
from dataclasses import dataclass
from datetime import date


@dataclass
class RiskConfig:
    """Risk management configuration."""
    daily_risk_cap: float = 0.01  # 1% per day
    daily_profit_target: float = 0.03  # 3% per day
    max_open_positions: int = 4
    max_daily_loss: float = 0.01  # stop if loss > 1%
    margin_call_buffer: float = 0.5  # stop if margin < 50%
    rr_ratio_min: float = 3.0  # minimum risk:reward


class RiskManager:
    """Manages trading risk."""
    
    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        self.daily_risk_used = 0.0
        self.daily_pnl = 0.0
        self.today = date.today()
        self.open_positions = 0
    
    def check_daily_limit_reached(self) -> bool:
        """Check if daily risk limit reached."""
        if date.today() != self.today:
            # Reset daily counters
            self.today = date.today()
            self.daily_risk_used = 0.0
            self.daily_pnl = 0.0
            self.open_positions = 0
        
        return self.daily_risk_used >= self.config.daily_risk_cap
    
    def check_stop_trading(self, account_balance: float) -> bool:
        """Check if should stop trading for the day."""
        # Stop if daily loss exceeds limit
        if self.daily_pnl < -self.config.max_daily_loss * account_balance:
            return True
        
        # Stop if max positions reached
        if self.open_positions >= self.config.max_open_positions:
            return True
        
        return False
    
    def allocate_risk(self, signals: Dict[str, Dict[str, Any]], account_balance: float) -> Dict[str, float]:
        """
        Allocate daily risk cap across assets based on signal strength.
        
        Args:
            signals: {symbol: {"signal": "BUY", "strength": 0.8, ...}}
            account_balance: current account balance
        
        Returns:
            {symbol: risk_allocation (0.0 to 1.0)}
        """
        if self.check_daily_limit_reached():
            return {}
        
        # Filter only actionable signals
        actionable = {
            symbol: data for symbol, data in signals.items()
            if data["signal"] in ["BUY", "SELL"] and data["confidence"] > 0.0
        }
        
        if not actionable:
            return {}
        
        # Calculate total signal strength
        total_strength = sum(abs(data["strength"]) for data in actionable.values())
        if total_strength == 0:
            return {}
        
        # Allocate risk proportionally
        allocation = {}
        remaining_risk = self.config.daily_risk_cap - self.daily_risk_used
        
        for symbol, data in actionable.items():
            if remaining_risk <= 0:
                break
            
            proportion = abs(data["strength"]) / total_strength
            asset_risk = min(remaining_risk, self.config.daily_risk_cap * proportion)
            
            if asset_risk > 0:
                allocation[symbol] = asset_risk
                remaining_risk -= asset_risk
        
        return allocation
    
    def calculate_position_size(self, symbol: str, signal_strength: float,
                                entry_price: float, stop_loss: float,
                                account_balance: float, pip_value: float) -> float:
        """
        Calculate position size in lots.
        
        Args:
            symbol: trading symbol
            signal_strength: -1.0 to 1.0
            entry_price: entry price
            stop_loss: stop loss price
            account_balance: account balance in USD
            pip_value: value of 1 pip for 1 lot
        
        Returns:
            Lot size
        """
        # Risk amount in USD
        risk_amount = account_balance * self.config.daily_risk_cap * abs(signal_strength)
        
        # Stop loss distance in pips
        sl_distance_pips = abs(entry_price - stop_loss) / pip_value
        
        if sl_distance_pips == 0:
            return 0.0
        
        # Lot size = risk / (sl_distance * pip_value)
        lot_size = risk_amount / (sl_distance_pips * pip_value)
        
        # Clamp to reasonable range
        lot_size = max(0.01, min(lot_size, 1.0))
        
        return round(lot_size, 2)
    
    def record_trade(self, risk_used: float, pnl: float = 0.0):
        """Record trade execution."""
        self.daily_risk_used += risk_used
        self.daily_pnl += pnl
        self.open_positions += 1
    
    def record_close(self, pnl: float):
        """Record position close."""
        self.daily_pnl += pnl
        self.open_positions -= 1
    
    def get_status(self) -> Dict[str, Any]:
        """Get current risk status."""
        return {
            "daily_risk_used": self.daily_risk_used,
            "daily_risk_cap": self.config.daily_risk_cap,
            "risk_remaining": self.config.daily_risk_cap - self.daily_risk_used,
            "daily_pnl": self.daily_pnl,
            "daily_profit_target": self.config.daily_profit_target,
            "open_positions": self.open_positions,
            "max_positions": self.config.max_open_positions,
            "can_trade": not self.check_daily_limit_reached() and not self.check_stop_trading(10000),  # TODO: pass real balance
        }


if __name__ == "__main__":
    rm = RiskManager()
    print("Risk manager initialized")
    print(f"Status: {rm.get_status()}")
