# risk_manager.py
import math
import MetaTrader5 as mt5
from logger_setup import log


class RiskManager:
    """
    Manages trade risk, including SL/TP calculation, validation,
    and dynamic volume sizing. It now enforces valid stop levels.
    """

    def __init__(self, symbol: str, stop_loss_pips: int, risk_reward_ratio: float, point: float, stops_level: int):
        self.symbol = symbol
        self.risk_reward_ratio = risk_reward_ratio
        self.point = point
        self.stops_level = stops_level
        self.stop_loss_pips = stop_loss_pips
        # Robustly determine decimal places for rounding, handling scientific notation like 1e-05
        if 'e' in str(self.point).lower():
            self.price_decimals = int(str(self.point).split('-')[-1])
        else:
            self.price_decimals = len(str(self.point).split('.')[-1])

    def calculate_sl_tp(self, order_type: str, current_ask: float, current_bid: float):
        """
        Calculates SL/TP based on a fixed pip amount from config.
        Useful for strategies that do not provide their own dynamic SL.
        """
        sl_pips = self.stop_loss_pips
        tp_pips = sl_pips * self.risk_reward_ratio

        # Assuming 1 pip = 10 points for most forex pairs
        price_diff_sl = (sl_pips * 10) * self.point
        price_diff_tp = (tp_pips * 10) * self.point

        if order_type.upper() == "BUY":
            sl = current_bid - price_diff_sl
            tp = current_ask + price_diff_tp
        elif order_type.upper() == "SELL":
            sl = current_ask + price_diff_sl
            tp = current_bid - price_diff_tp
        else:
            return None, None

        return round(sl, self.price_decimals), round(tp, self.price_decimals)

    def validate_and_adjust_sl(self, sl_price: float, current_ask: float, current_bid: float, order_type: str) -> float:
        """
        Validates the proposed stop loss against the broker's minimum distance (stops_level).
        If the SL is too close, it adjusts it outwards to the minimum valid distance.
        This method will ALWAYS return a valid SL price, never None.
        """
        min_stop_distance_price = self.stops_level * self.point

        if order_type.upper() == "BUY":
            # For a BUY, SL must be below the current ask price.
            # The closest it can be is current_ask - min_distance.
            min_valid_sl = current_ask - min_stop_distance_price

            # If the proposed SL is higher than the minimum valid SL (i.e., too close to the price)
            if sl_price > min_valid_sl:
                adjusted_sl = round(min_valid_sl, self.price_decimals)
                log.warning(f"Strategy SL for {self.symbol} ({sl_price}) is too close. "
                            f"Adjusting to broker's min distance: {adjusted_sl}")
                sl_price = adjusted_sl

        elif order_type.upper() == "SELL":
            # For a SELL, SL must be above the current bid price.
            # The closest it can be is current_bid + min_distance.
            min_valid_sl = current_bid + min_stop_distance_price

            # If the proposed SL is lower than the minimum valid SL (i.e., too close to the price)
            if sl_price < min_valid_sl:
                adjusted_sl = round(min_valid_sl, self.price_decimals)
                log.warning(f"Strategy SL for {self.symbol} ({sl_price}) is too close. "
                            f"Adjusting to broker's min distance: {adjusted_sl}")
                sl_price = adjusted_sl

        # Return the original or adjusted (but always valid) SL price, correctly rounded.
        return round(sl_price, self.price_decimals)

    def calculate_volume(self, account_balance: float, risk_percent: float, stop_loss_in_points: float) -> float | None:
        """
        Calculates the trade volume based on risk percentage and stop loss distance in points.
        """
        if not all([account_balance, risk_percent]) or stop_loss_in_points <= 0:
            log.error(f"Invalid inputs for volume calculation. SL points was: {stop_loss_in_points}")
            return None

        symbol_info = mt5.symbol_info(self.symbol)
        if not symbol_info:
            log.error(f"Could not get symbol info for {self.symbol}.");
            return None

        risk_amount = account_balance * (risk_percent / 100.0)

        # Use trade_tick_value for an accurate calculation of loss per lot in account currency
        loss_per_lot = stop_loss_in_points * symbol_info.trade_tick_value
        if loss_per_lot <= 0:
            log.error(f"Cannot calculate volume. Loss per lot is {loss_per_lot}.");
            return None

        volume = risk_amount / loss_per_lot

        # Normalize and constrain volume according to the symbol's specifications
        volume_step, min_volume, max_volume = symbol_info.volume_step, symbol_info.volume_min, symbol_info.volume_max
        volume = math.floor(volume / volume_step) * volume_step
        volume = round(volume, 2)  # Most brokers use 2 decimal places for lots

        if volume < min_volume:
            log.warning(
                f"Calculated volume {volume:.2f} is < min {min_volume}. Using min volume. Risk will be higher.");
            volume = min_volume
        if volume > max_volume:
            log.warning(f"Calculated volume {volume:.2f} is > max {max_volume}. Capping at max volume.");
            volume = max_volume

        # Convert points to pips for display (assuming 1 pip = 10 points)
        sl_in_pips_for_log = stop_loss_in_points / 10

        log.info(
            f"Dynamic Volume Calculated: {volume:.2f} lots for {self.symbol} (Risk: {risk_percent}%, SL: {sl_in_pips_for_log:.1f} pips, Account: {account_balance:.2f})")
        return volume