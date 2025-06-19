# main.py
import configparser
import time
import pandas as pd
import MetaTrader5 as mt5

from logger_setup import log
from mt5_connector import MT5Connector
from risk_manager import RiskManager
from trading_strategy import RegimeMomentumStrategy, EMARibbonScalper


def run():
    """Main execution function for the multi-strategy Expert Advisor."""
    # --- Configuration and Initialization (No Changes Here) ---
    config = configparser.ConfigParser()
    config.read('config.ini')
    try:
        mt5_creds, trade_params = config['mt5_credentials'], config['trading_parameters']
    except KeyError as e:
        log.error(f"Config error: {e}. Aborting."); return
    log.info("Starting Python Multi-Symbol/Multi-Strategy EA")
    connector = MT5Connector(login=int(mt5_creds['account']), password=mt5_creds['password'],
                             server=mt5_creds['server'])
    if not connector.connect(): log.error("Failed to connect to MT5. Exiting."); return
    try:
        symbols_to_trade, magic_number = [s.strip() for s in trade_params['symbols'].split(',')], int(
            trade_params['magic_number'])
    except KeyError as e:
        log.error(f"Trading param '{e}' missing. Exiting."); connector.disconnect(); return
    strategies, risk_managers = {}, {}
    log.info("Initializing strategies and risk managers...")
    for symbol in symbols_to_trade:
        try:
            symbol_config = config[symbol];
            strategy_type = symbol_config['strategy_type']
            symbol_info = mt5.symbol_info(symbol);
            if not symbol_info: log.error(f"Could not get info for {symbol}. Skipping."); continue
            point = symbol_info.point
            risk_managers[symbol] = RiskManager(symbol=symbol,
                                                stop_loss_pips=int(symbol_config.get('stop_loss_pips', 100)),
                                                risk_reward_ratio=float(symbol_config['risk_reward_ratio']),
                                                point=point, stops_level=symbol_info.trade_stops_level)
            if strategy_type == 'EMARibbonScalper':
                strategies[symbol] = EMARibbonScalper(
                    ema_fast_periods=[int(p) for p in symbol_config['ema_fast_periods'].split(',')],
                    ema_slow_period=int(symbol_config['ema_slow_period']), rsi_period=int(symbol_config['rsi_period']),
                    rsi_level=int(symbol_config['rsi_level']),
                    consolidation_threshold_pips=float(symbol_config['consolidation_threshold_pips']),
                    risk_reward_ratio=float(symbol_config['risk_reward_ratio']), pip_size=point)
                log.info(f"Initialized EMARibbonScalper for {symbol}.")
            # ... (Legacy init logic remains) ...
            elif strategy_type == 'RegimeMomentum':
                strategies[symbol] = RegimeMomentumStrategy(
                    fast_ema_period=int(symbol_config['fast_ema_period']),
                    slow_ema_period=int(symbol_config['slow_ema_period']),
                    adx_period=int(symbol_config['adx_period']), adx_threshold=int(symbol_config['adx_threshold']),
                    stoch_k_period=int(symbol_config['stoch_k_period']),
                    stoch_d_period=int(symbol_config['stoch_d_period']),
                    stoch_slowing=int(symbol_config['stoch_slowing']),
                    stoch_oversold=int(symbol_config['stoch_oversold']),
                    stoch_overbought=int(symbol_config['stoch_overbought'])
                );
                log.info(f"Initialized RegimeMomentumStrategy for {symbol}.")
        except KeyError as e:
            log.error(f"Config error for {symbol}: {e}. Skipping."); continue
    log.info(f"EA configured to trade symbols: {list(strategies.keys())}")

    # --- Main Trading Loop ---
    try:
        while True:
            log.info("--- New Trading Cycle ---")
            for symbol in strategies.keys():
                log.info(f"--- Processing symbol: {symbol} ---")
                try:
                    strategy, risk_manager = strategies[symbol], risk_managers[symbol]
                    # === THIS IS THE CORRECTED LOGIC ===
                    # Get the config for the current symbol in the loop
                    symbol_config = config[symbol]
                    # Use the correct 'symbol_config' object to get parameters
                    timeframe_str = symbol_config['timeframe']
                    risk_percent = float(symbol_config.get('risk_per_trade_percent', 1.0))
                    # ==================================

                    if connector.get_open_positions(symbol=symbol, magic_number=magic_number): continue

                    historical_data = connector.get_historical_data(symbol, timeframe_str, strategy.min_bars + 5)
                    if historical_data is None or historical_data.empty: continue

                    if isinstance(strategy, EMARibbonScalper):
                        processed_df = strategy.calculate_signals(historical_data)
                        last_candle = processed_df.iloc[-2]
                        if last_candle['signal'] in [1, -1]:
                            entry_signal = "BUY" if last_candle['signal'] == 1 else "SELL"
                            log.info(f"EMARibbonScalper signal found for {symbol}: {entry_signal}")

                            sl_price_initial = last_candle['stop_loss']
                            tick = mt5.symbol_info_tick(symbol)
                            if not tick: continue
                            entry_price = tick.ask if entry_signal == "BUY" else tick.bid

                            sl_price = risk_manager.validate_and_adjust_sl(sl_price_initial, tick.ask, tick.bid,
                                                                           entry_signal)

                            if (entry_signal == "BUY" and sl_price >= entry_price) or \
                                    (entry_signal == "SELL" and sl_price <= entry_price):
                                log.warning(
                                    f"Race condition detected for {symbol}. Entry: {entry_price}, Adj. SL: {sl_price}. Aborting.")
                                continue

                            stop_distance = abs(entry_price - sl_price)
                            tp_price = entry_price + (
                                        stop_distance * risk_manager.risk_reward_ratio) if entry_signal == "BUY" else entry_price - (
                                        stop_distance * risk_manager.risk_reward_ratio)

                            min_stop_distance_price = risk_manager.stops_level * risk_manager.point
                            if abs(tp_price - entry_price) < min_stop_distance_price:
                                log.warning(f"TP for {symbol} too close after adjustments. Aborting to maintain R:R.")
                                continue

                            stop_loss_points = stop_distance / risk_manager.point
                            account_info = mt5.account_info()
                            if not account_info:
                                log.error("Could not get account info. Skipping trade.");
                                continue
                            volume = risk_manager.calculate_volume(account_info.balance, risk_percent, stop_loss_points)

                            if volume:
                                symbol_info = mt5.symbol_info(symbol)  # Get fresh info for digits
                                sl_price, tp_price = round(sl_price, symbol_info.digits), round(tp_price,
                                                                                                symbol_info.digits)
                                connector.place_order(symbol, entry_signal, volume, sl_price, tp_price, magic_number)
                except Exception as e:
                    log.error(f"Error processing {symbol}: {e}", exc_info=True); continue

            sleep_duration = int(trade_params.get('main_loop_sleep_seconds', 30))
            log.info(f"Cycle complete. Sleeping for {sleep_duration}s...")
            time.sleep(sleep_duration)
    except KeyboardInterrupt:
        log.info("EA stopped by user.")
    finally:
        connector.disconnect(); log.info("Python EA shut down.")


if __name__ == "__main__": run()
