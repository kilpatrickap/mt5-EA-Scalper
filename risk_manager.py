# risk_manager.py
import math
import MetaTrader5 as mt5
from logger_setup import log


class RiskManager:
    """
    Manages risk-related calculations for trades, including SL/TP validation
    and dynamic position sizing, based on the provided "working" example's logic.
    """

    def __init__(self, symbol: str, stop_loss_pips: int, risk_reward_ratio: float, point: float, stops_level: int):
        """
        Initializes the RiskManager with parameters for a specific symbol.
        """
        if not isinstance(risk_reward_ratio, (float, int)) or risk_reward_ratio <= 0:
            raise ValueError("risk_reward_ratio must be a positive number.")
        # stop_loss_pips from config is now a fallback, not a primary driver for dynamic strategies.

        self.symbol = symbol
        self.stop_loss_pips_fallback = stop_loss_pips # For strategies that don't provide a dynamic SL
        self.risk_reward_ratio = risk_reward_ratio
        self.point = point
        self.stops_level = stops_level

        # Robustly determine decimal places for rounding, handling scientific notation like 1e-05
        if 'e' in str(self.point).lower():
            self.price_decimals = int(str(self.point).split('-')[-1])
        else:
            self.price_decimals = len(str(self.point).split('.')[-1])

    def calculate_sl_tp(self, order_type: str, entry_price: float, suggested_sl_price: float) -> tuple[float | None, float | None, int | None]:
        """
        Validates the strategy's suggested SL price, adjusts if necessary, and calculates the corresponding TP.
        Returns the final SL price, TP price, and the validated stop loss distance in WHOLE PIPS.
        """
        if not all([order_type, entry_price, suggested_sl_price]):
            log.error("RiskManager received invalid inputs for SL/TP calculation.")
            return None, None, None

        # 1. Calculate the required minimum stop distance in points from the entry price
        symbol_info_tick = mt5.symbol_info_tick(self.symbol)
        spread_in_points = (symbol_info_tick.ask - symbol_info_tick.bid) / self.point
        min_stop_level_in_points = self.stops_level
        # The required distance from entry is the broker's min stop level plus the spread
        required_sl_distance_in_points = min_stop_level_in_points + spread_in_points

        # 2. Calculate the stop distance proposed by the strategy in points
        strategy_sl_distance_in_points = abs(entry_price - suggested_sl_price) / self.point

        # 3. Validate and adjust the stop distance
        final_sl_distance_in_points = strategy_sl_distance_in_points
        if final_sl_distance_in_points < required_sl_distance_in_points:
            log.warning(
                f"Strategy SL for {self.symbol} is too tight ({final_sl_distance_in_points:.1f} points). "
                f"Required: {required_sl_distance_in_points:.1f} points. Adjusting SL outwards."
            )
            final_sl_distance_in_points = required_sl_distance_in_points + 1 # Add a 1-point buffer

        # Convert final distance in points to price and whole pips for return
        sl_in_price_diff = final_sl_distance_in_points * self.point
        tp_in_price_diff = sl_in_price_diff * self.risk_reward_ratio
        final_sl_pips = math.ceil(final_sl_distance_in_points / 10) # Convert points to pips, rounding up

        # 4. Calculate final SL/TP prices
        if order_type.upper() == "BUY":
            sl_price = entry_price - sl_in_price_diff
            tp_price = entry_price + tp_in_price_diff
        elif order_type.upper() == "SELL":
            sl_price = entry_price + sl_in_price_diff
            tp_price = entry_price - tp_in_price_diff
        else:
            log.error(f"Invalid order_type '{order_type}' received in RiskManager.")
            return None, None, None

        return round(sl_price, self.price_decimals), round(tp_price, self.price_decimals), final_sl_pips

    def calculate_volume(self, account_balance: float, risk_percent: float, stop_loss_pips: int) -> float | None:
        """
        Calculates the trade volume based on a fixed percentage of account equity and a validated SL in pips.
        """
        if not all([account_balance, risk_percent, stop_loss_pips]) or stop_loss_pips <= 0:
            log.error("Invalid inputs for volume calculation.")
            return None

        # 1. Get symbol and account info
        symbol_info = mt5.symbol_info(self.symbol)
        if not symbol_info:
            log.error(f"Could not get symbol info for {self.symbol}.")
            return None

        # 2. Calculate risk amount in account currency
        risk_amount = account_balance * (risk_percent / 100.0)

        # 3. Calculate value of 1 pip for 1 lot in the account currency
        # (tick_value * pip_size_in_points) / tick_size
        pip_size_in_points = self.point * 10
        pip_value_per_lot = (symbol_info.trade_tick_value * pip_size_in_points) / symbol_info.trade_tick_size
        if pip_value_per_lot <= 0:
            log.error(f"Pip value for {self.symbol} is zero or negative. Cannot calculate volume.")
            return None

        # 4. Calculate total loss for 1 lot with the given stop loss
        loss_per_lot = stop_loss_pips * pip_value_per_lot

        # 5. Calculate ideal volume
        try:
            volume = risk_amount / loss_per_lot
        except ZeroDivisionError:
            log.error("Cannot calculate volume due to ZeroDivisionError (loss_per_lot is zero).")
            return None

        # 6. Normalize volume according to broker limits
        volume_step, min_volume, max_volume = symbol_info.volume_step, symbol_info.volume_min, symbol_info.volume_max
        volume = math.floor(volume / volume_step) * volume_step
        volume = round(volume, 2)  # Round to handle potential float inaccuracies

        # 7. Check against min/max volume limits
        if volume < min_volume:
            log.warning(f"Calculated volume {volume:.2f} is < min {min_volume}. "
                        f"Risk is too small for this trade. No trade will be placed.")
            return None # Return None to prevent taking a trade with skewed risk
        if volume > max_volume:
            log.warning(f"Calculated volume {volume:.2f} > max {max_volume}. Capping at max volume.")
            volume = max_volume

        log.info(f"Dynamic Volume Calculated: {volume:.2f} lots for {self.symbol} "
                 f"(Risk: {risk_percent}%, SL: {stop_loss_pips} pips, Account: {account_balance:.2f})")
        return volume