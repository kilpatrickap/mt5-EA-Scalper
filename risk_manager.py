# risk_manager.py
import math
import MetaTrader5 as mt5
from logger_setup import log


class RiskManager:
    # ... (init, calculate_sl_tp, validate_and_adjust_sl methods are unchanged) ...
    def __init__(self, symbol: str, stop_loss_pips: int, risk_reward_ratio: float, point: float, stops_level: int):
        self.symbol, self.risk_reward_ratio, self.point, self.stops_level = symbol, risk_reward_ratio, point, stops_level
        self.stop_loss_pips = stop_loss_pips
        if 'e' in str(self.point).lower():
            self.price_decimals = int(str(self.point).split('-')[-1])
        else:
            self.price_decimals = len(str(self.point).split('.')[-1])

    def calculate_sl_tp(self, order_type: str, current_ask: float, current_bid: float):
        pass  # Unchanged

    def validate_and_adjust_sl(self, sl_price: float, current_ask: float, current_bid: float, order_type: str):
        pass  # Unchanged

    def calculate_volume(self, account_balance: float, risk_percent: float, stop_loss_in_points: float) -> float | None:
        """
        Calculates the trade volume. The stop loss is expected in POINTS.
        """
        if not all([account_balance, risk_percent]) or stop_loss_in_points <= 0:
            log.error(f"Invalid inputs for volume calculation. SL points was: {stop_loss_in_points}")
            return None

        symbol_info = mt5.symbol_info(self.symbol)
        account_info = mt5.account_info()
        if not symbol_info or not account_info:
            log.error(f"Could not get symbol or account info for {self.symbol}.");
            return None

        risk_amount = account_balance * (risk_percent / 100.0)
        loss_per_lot = stop_loss_in_points * symbol_info.trade_tick_value
        if loss_per_lot <= 0:
            log.error(f"Cannot calculate volume. Loss per lot is {loss_per_lot}.");
            return None

        volume = risk_amount / loss_per_lot

        volume_step, min_volume, max_volume = symbol_info.volume_step, symbol_info.volume_min, symbol_info.volume_max
        volume = math.floor(volume / volume_step) * volume_step
        volume = round(volume, 2)

        if volume < min_volume:
            log.warning(f"Calculated volume {volume:.2f} is < min {min_volume}. Rounding up. Risk will be higher.");
            volume = min_volume
        if volume > max_volume:
            log.warning(f"Calculated volume {volume:.2f} is > max {max_volume}. Capping at max.");
            volume = max_volume

        # === THIS IS THE UPDATED LOGIC ===
        # Convert points to pips for display (assuming 1 pip = 10 points)
        # This is for logging only; the calculation uses the more precise points value.
        sl_in_pips_for_log = stop_loss_in_points / 10

        # Updated log message to match the desired format
        log.info(
            f"Dynamic Volume Calculated: {volume:.2f} lots for {self.symbol} (Risk: {risk_percent}%, SL: {sl_in_pips_for_log:.1f} pips, Account: {account_balance:.2f})")
        # ================================
        return volume
