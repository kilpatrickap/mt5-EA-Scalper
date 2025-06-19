# risk_manager.py
import math
import MetaTrader5 as mt5
from logger_setup import log


class RiskManager:
    """
    Manages risk-related calculations for trades, including dynamic position sizing.
    """

    def __init__(self, symbol: str, stop_loss_pips: int, risk_reward_ratio: float, point: float, stops_level: int):
        """
        Initializes the RiskManager with parameters for a specific symbol.
        """
        if not isinstance(stop_loss_pips, int) or stop_loss_pips <= 0:
            raise ValueError("stop_loss_pips must be a positive integer.")
        if not isinstance(risk_reward_ratio, (float, int)) or risk_reward_ratio <= 0:
            raise ValueError("risk_reward_ratio must be a positive number.")

        self.symbol = symbol
        self.stop_loss_pips = stop_loss_pips
        self.risk_reward_ratio = risk_reward_ratio
        self.point = point
        self.stops_level = stops_level

        if 'e' in str(self.point).lower():
            self.price_decimals = int(str(self.point).split('-')[-1])
        else:
            self.price_decimals = len(str(self.point).split('.')[-1])

    def calculate_sl_tp(self, order_type: str, current_ask: float, current_bid: float) -> tuple[
        float | None, float | None, int | None]:
        # This method is for legacy strategies and remains unchanged.
        if not all([order_type, current_ask, current_bid]):
            log.error("RiskManager received invalid inputs for SL/TP calculation.")
            return None, None, None
        spread_in_points = (current_ask - current_bid) / self.point
        min_stop_level_in_points = self.stops_level
        required_sl_distance_in_points = spread_in_points + min_stop_level_in_points
        sl_in_pips = self.stop_loss_pips
        if sl_in_pips < required_sl_distance_in_points:
            original_sl_pips = self.stop_loss_pips
            sl_in_pips = math.ceil(required_sl_distance_in_points)
            log.warning(
                f"Configured SL for {self.symbol} is too tight. "
                f"Original: {original_sl_pips} pips, Required & Adjusted to: {sl_in_pips} pips."
            )
        sl_in_price = sl_in_pips * self.point
        tp_in_price = sl_in_price * self.risk_reward_ratio
        if order_type.upper() == "BUY":
            entry_price = current_ask
            sl_price = entry_price - sl_in_price
            tp_price = entry_price + tp_in_price
        elif order_type.upper() == "SELL":
            entry_price = current_bid
            sl_price = entry_price + sl_in_price
            tp_price = entry_price - tp_in_price
        else:
            log.error(f"Invalid order_type '{order_type}' received in RiskManager.")
            return None, None, None
        return round(sl_price, self.price_decimals), round(tp_price, self.price_decimals), sl_in_pips

    def validate_and_adjust_sl(self, sl_price: float, entry_price: float, order_type: str) -> float:
        # This method is correct and remains unchanged.
        min_stop_distance_price = self.stops_level * self.point
        if order_type.upper() == "BUY":
            if (entry_price - sl_price) < min_stop_distance_price:
                adjusted_sl = entry_price - min_stop_distance_price
                log.warning(f"BUY SL for {self.symbol} too close. Original: {sl_price}, Adjusted: {adjusted_sl}")
                return round(adjusted_sl, self.price_decimals)
        elif order_type.upper() == "SELL":
            if (sl_price - entry_price) < min_stop_distance_price:
                adjusted_sl = entry_price + min_stop_distance_price
                log.warning(f"SELL SL for {self.symbol} too close. Original: {sl_price}, Adjusted: {adjusted_sl}")
                return round(adjusted_sl, self.price_decimals)
        return sl_price

    # === METHOD UPDATED TO BE MORE ROBUST ===
    def calculate_volume(self, account_balance: float, risk_percent: float, stop_loss_in_points: float) -> float | None:
        """
        Calculates the trade volume. Now accepts stop_loss_in_points as a float and validates it.
        """
        # More robust check for valid inputs.
        if not all([account_balance, risk_percent]) or stop_loss_in_points <= 0:
            log.error(f"Invalid inputs for volume calculation. SL points was: {stop_loss_in_points}")
            return None

        symbol_info = mt5.symbol_info(self.symbol)
        account_info = mt5.account_info()
        if not symbol_info or not account_info:
            log.error(f"Could not get symbol or account info for {self.symbol}.");
            return None

        risk_amount = account_balance * (risk_percent / 100.0)

        # trade_tick_value is the value of 1 point move for 1 lot. This is more direct.
        loss_per_lot = stop_loss_in_points * symbol_info.trade_tick_value
        if loss_per_lot <= 0:
            log.error(f"Cannot calculate volume. Loss per lot is {loss_per_lot}.");
            return None

        volume = risk_amount / loss_per_lot

        volume_step = symbol_info.volume_step
        min_volume = symbol_info.volume_min
        max_volume = symbol_info.volume_max

        volume = math.floor(volume / volume_step) * volume_step
        volume = round(volume, 2)

        if volume < min_volume:
            log.warning(f"Calculated volume {volume:.2f} is less than min volume {min_volume}. "
                        f"Rounding up to minimum to execute trade. Risk will be higher than target.")
            volume = min_volume

        if volume > max_volume:
            log.warning(f"Calculated volume {volume:.2f} exceeds max volume {max_volume}. Capping at max volume.")
            volume = max_volume

        # Corrected log message to report points accurately
        log.info(f"Dynamic Volume Calculated: {volume:.2f} lots for {self.symbol} "
                 f"(Risk Target: {risk_percent}%, SL: {stop_loss_in_points:.1f} points, Account: {account_balance:.2f})")
        return volume