# mt5_connector.py
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
from logger_setup import log


class MT5Connector:
    """Handles connection and trade execution with the MetaTrader 5 terminal."""

    def __init__(self, login, password, server):
        self._login = login
        self._password = password
        self._server = server
        self.connection_status = False

    def connect(self):
        """Initializes connection to the MT5 terminal."""
        log.info("Initializing MT5 connection...")
        if not mt5.initialize(login=self._login, password=self._password, server=self._server):
            log.error(f"MT5 initialize() failed, error code = {mt5.last_error()}")
            mt5.shutdown()
            self.connection_status = False
            return False

        log.info(f"MT5 connection successful to account {self._login}.")
        self.connection_status = True
        return True

    def disconnect(self):
        """Shuts down the connection to the MT5 terminal."""
        log.info("Shutting down MT5 connection.")
        mt5.shutdown()
        self.connection_status = False

    def get_historical_data(self, symbol, timeframe_str, num_bars=100):
        """Fetches historical price data and sorts it chronologically."""
        timeframe_map = {
            'M1': mt5.TIMEFRAME_M1, 'M5': mt5.TIMEFRAME_M5, 'M15': mt5.TIMEFRAME_M15,
            'M30': mt5.TIMEFRAME_M30, 'H1': mt5.TIMEFRAME_H1, 'H4': mt5.TIMEFRAME_H4,
            'D1': mt5.TIMEFRAME_D1, 'W1': mt5.TIMEFRAME_W1, 'MN1': mt5.TIMEFRAME_MN1
        }
        timeframe = timeframe_map.get(timeframe_str.upper())
        if timeframe is None:
            log.error(f"Invalid timeframe specified: {timeframe_str}")
            return None

        try:
            rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, num_bars)
            if rates is None:
                log.error(f"Failed to get historical data for {symbol}. Error: {mt5.last_error()}")
                return None

            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')

            # --- THIS IS THE FIX ---
            # MT5 returns data newest-to-oldest. We reverse it to be chronological (oldest-to-newest).
            # This makes all subsequent logic (MA calculation, indexing) correct and intuitive.
            df = df.iloc[::-1].reset_index(drop=True)
            # --- END OF FIX ---

            return df
        except Exception as e:
            log.error(f"Exception in get_historical_data: {e}")
            return None

    def get_open_positions(self, symbol=None, magic_number=None):
        """Retrieves all open positions, optionally filtering by symbol and magic number."""
        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            log.warning("No positions found or error occurred.")
            return []

        if magic_number:
            return [p for p in positions if p.magic == magic_number]
        return list(positions)

    def place_order(self, symbol, order_type, volume, sl_price, tp_price, magic_number):
        """Places a market order."""
        log.info(f"Attempting to place order: {symbol}, Type: {order_type}, Vol: {volume}")
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            log.error(f"Symbol not found: {symbol}")
            return None

        point = symbol_info.point
        price_buy = mt5.symbol_info_tick(symbol).ask
        price_sell = mt5.symbol_info_tick(symbol).bid

        if order_type == "BUY":
            trade_type = mt5.ORDER_TYPE_BUY
            price = price_buy
        elif order_type == "SELL":
            trade_type = mt5.ORDER_TYPE_SELL
            price = price_sell
        else:
            log.error(f"Invalid order type: {order_type}")
            return None

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": trade_type,
            "price": price,
            "sl": float(sl_price),
            "tp": float(tp_price),
            "deviation": 20,
            "magic": magic_number,
            "comment": "Python EA",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error(f"Order send failed, retcode={result.retcode} - {result.comment}")
            return None

        log.info(f"Order placed successfully. Ticket: {result.order}, Price: {result.price}, Volume: {result.volume}")
        return result

    def close_position(self, position, comment="Closing position"):
        """Closes an open position."""
        log.info(f"Attempting to close position ticket #{position.ticket}")

        order_type_map = {
            mt5.ORDER_TYPE_BUY: mt5.ORDER_TYPE_SELL,
            mt5.ORDER_TYPE_SELL: mt5.ORDER_TYPE_BUY
        }

        price_map = {
            mt5.ORDER_TYPE_BUY: mt5.symbol_info_tick(position.symbol).bid,  # Close a BUY with a SELL
            mt5.ORDER_TYPE_SELL: mt5.symbol_info_tick(position.symbol).ask  # Close a SELL with a BUY
        }

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": position.ticket,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": order_type_map[position.type],
            "price": price_map[position.type],
            "deviation": 20,
            "magic": position.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error(f"Failed to close position #{position.ticket}. Retcode={result.retcode} - {result.comment}")
            return False

        log.info(f"Position #{position.ticket} closed successfully.")
        return True