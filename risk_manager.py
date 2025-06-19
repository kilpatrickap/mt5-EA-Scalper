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
        """
        Calculates SL/TP and also returns the validated stop loss distance in pips.
        """
        if not all([order_type, current_ask, current_bid]):
            log.error("RiskManager received invalid inputs for SL/TP calculation.")
            return None, None, None

        spread_in_points = (current_ask - current_bid) / self.point
        min_stop_level_in_points = self.stops_level
        required_sl_distance_in_points = spread_in_points + min_stop_level_in_points

        sl_in_pips = self.stop_loss_pips

        if sl_in_pips < required_sl_distance_in_points:
            original_sl_pips = self.stop_loss_pips
            sl_in_pips = math.ceil(required_sl_distance_in_points) # Round up to nearest whole pip
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

    def calculate_volume(self, account_balance: float, risk_percent: float, stop_loss_pips: int) -> float | None:
        """
        Calculates the trade volume based on a fixed percentage of account equity.
        """
        if not all([account_balance, risk_percent, stop_loss_pips]):
            log.error("Invalid inputs for volume calculation.")
            return None

        # 1. Get symbol and account info
        symbol_info = mt5.symbol_info(self.symbol)
        account_info = mt5.account_info()
        if not symbol_info or not account_info:
            log.error(f"Could not get symbol or account info for {self.symbol}.")
            return None

        # 2. Calculate risk amount in account currency
        risk_amount = account_balance * (risk_percent / 100.0)

        # 3. Calculate value of 1 pip for 1 lot
        # (tick_value * pip_size_in_points) / tick_size
        pip_value_per_lot = (symbol_info.trade_tick_value * (self.point * 10)) / symbol_info.trade_tick_size

        if pip_value_per_lot == 0:
            log.error(f"Pip value for {self.symbol} is zero. Cannot calculate volume.")
            return None

        # 4. Calculate loss per lot for the given stop loss
        loss_per_lot = stop_loss_pips * pip_value_per_lot

        # 5. Calculate ideal volume
        try:
            volume = risk_amount / loss_per_lot
        except ZeroDivisionError:
            log.error("Cannot calculate volume due to ZeroDivisionError (loss_per_lot is zero).")
            return None

        # 6. Normalize volume according to broker limits
        volume_step = symbol_info.volume_step
        min_volume = symbol_info.volume_min
        max_volume = symbol_info.volume_max

        # Adjust to the nearest valid volume step
        volume = math.floor(volume / volume_step) * volume_step
        volume = round(volume, 2) # Round to handle potential float inaccuracies

        # 7. Check against min/max volume limits
        if volume < min_volume:
            log.warning(f"Calculated volume {volume} is less than min volume {min_volume}. "
                        f"Risk is too small for this trade. No trade will be placed.")
            return None # or return min_volume if you prefer to trade anyway with higher risk
        if volume > max_volume:
            log.warning(f"Calculated volume {volume} exceeds max volume {max_volume}. Capping at max volume.")
            volume = max_volume

        log.info(f"Dynamic Volume Calculated: {volume:.2f} lots for {self.symbol} "
                 f"(Risk: {risk_percent}%, SL: {stop_loss_pips} pips, Account: {account_balance:.2f})")
        return volume