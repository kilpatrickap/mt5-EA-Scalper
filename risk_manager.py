# risk_manager.py
import math
import MetaTrader5 as mt5
from logger_setup import log


class RiskManager:
    """
    Manages risk-related calculations for trades, including dynamic position sizing.
    """

    def __init__(self, symbol: str, stop_loss_pips: int, risk_reward_ratio: float, point: float, stops_level: int):
        self.symbol = symbol
        self.stop_loss_pips = stop_loss_pips
        self.risk_reward_ratio = risk_reward_ratio
        self.point = point
        self.stops_level = stops_level
        if 'e' in str(self.point).lower():
            self.price_decimals = int(str(self.point).split('-')[-1])
        else:
            self.price_decimals = len(str(self.point).split('.')[-1])

    # ... (calculate_sl_tp remains unchanged for legacy strategies) ...

    # === METHOD FULLY REWRITTEN TO BE SPREAD-AWARE ===
    def validate_and_adjust_sl(self, sl_price: float, current_ask: float, current_bid: float, order_type: str) -> float:
        """
        Ensures the calculated SL respects the broker's minimum stops_level,
        ACCOUNTING FOR THE SPREAD. This is the final, robust validation.
        """
        min_stop_distance_price = self.stops_level * self.point

        # For a BUY, the SL is compared against the BID price.
        if order_type.upper() == "BUY":
            # The distance from the BID to the SL must be at least the minimum.
            if (current_bid - sl_price) < min_stop_distance_price:
                adjusted_sl = current_bid - min_stop_distance_price
                log.warning(f"BUY SL for {self.symbol} too close to BID. Original: {sl_price}, Adjusted: {adjusted_sl}")
                return round(adjusted_sl, self.price_decimals)

        # For a SELL, the SL is compared against the ASK price.
        elif order_type.upper() == "SELL":
            # The distance from the SL to the ASK must be at least the minimum.
            if (sl_price - current_ask) < min_stop_distance_price:
                adjusted_sl = current_ask + min_stop_distance_price
                log.warning(
                    f"SELL SL for {self.symbol} too close to ASK. Original: {sl_price}, Adjusted: {adjusted_sl}")
                return round(adjusted_sl, self.price_decimals)

        return sl_price  # Return original if it's already valid

    def calculate_volume(self, account_balance: float, risk_percent: float, stop_loss_in_points: float) -> float | None:
        # This function is now correct and requires no further changes.
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
        log.info(
            f"Dynamic Volume Calculated: {volume:.2f} lots for {self.symbol} (Risk Target: {risk_percent}%, SL: {stop_loss_in_points:.1f} points, Account: {account_balance:.2f})")
        return volume
