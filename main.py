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
        mt5_creds = config['mt5_credentials']
        trade_params = config['trading_parameters']
    except KeyError as e:
        log.error(f"Configuration error: Missing section or key - {e}. Aborting.");
        return
    log.info("Starting Python Multi-Symbol/Multi-Strategy Expert Advisor")
    connector = MT5Connector(
        login=int(mt5_creds['account']), password=mt5_creds['password'], server=mt5_creds['server']
    )
    if not connector.connect():
        log.error("Failed to connect to MT5. Exiting.");
        return
    try:
        symbols_to_trade = [s.strip() for s in trade_params['symbols'].split(',')]
        magic_number = int(trade_params['magic_number'])
    except KeyError as e:
        log.error(f"Trading parameter '{e}' is missing in config.ini. Exiting.");
        connector.disconnect();
        return
    strategies, risk_managers = {}, {}
    log.info("Initializing strategies and risk managers for each symbol...")
    for symbol in symbols_to_trade:
        try:
            symbol_config = config[symbol]
            strategy_type = symbol_config['strategy_type']
            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info:
                log.error(f"Could not get info for {symbol}. It will be skipped.");
                continue
            point = symbol_info.point
            risk_managers[symbol] = RiskManager(
                symbol=symbol, stop_loss_pips=int(symbol_config.get('stop_loss_pips', 100)),
                risk_reward_ratio=float(symbol_config['risk_reward_ratio']), point=point,
                stops_level=symbol_info.trade_stops_level
            )
            if strategy_type == 'EMARibbonScalper':
                strategies[symbol] = EMARibbonScalper(
                    ema_fast_periods=[int(p) for p in symbol_config['ema_fast_periods'].split(',')],
                    ema_slow_period=int(symbol_config['ema_slow_period']),
                    rsi_period=int(symbol_config['rsi_period']),
                    rsi_level=int(symbol_config['rsi_level']),
                    consolidation_threshold_pips=float(symbol_config['consolidation_threshold_pips']),
                    risk_reward_ratio=float(symbol_config['risk_reward_ratio']), pip_size=point
                );
                log.info(f"Initialized EMARibbonScalper for {symbol}.")
            elif strategy_type == 'RegimeMomentum':
                strategies[symbol] = RegimeMomentumStrategy(
                    fast_ema_period=int(symbol_config['fast_ema_period']),
                    slow_ema_period=int(symbol_config['slow_ema_period']),
                    adx_period=int(symbol_config['adx_period']),
                    adx_threshold=int(symbol_config['adx_threshold']),
                    stoch_k_period=int(symbol_config['stoch_k_period']),
                    stoch_d_period=int(symbol_config['stoch_d_period']),
                    stoch_slowing=int(symbol_config['stoch_slowing']),
                    stoch_oversold=int(symbol_config['stoch_oversold']),
                    stoch_overbought=int(symbol_config['stoch_overbought'])
                );
                log.info(f"Initialized RegimeMomentumStrategy for {symbol}.")
            else:
                log.error(f"Unknown strategy_type '{strategy_type}' for symbol {symbol}. Skipping.")
                if symbol in risk_managers: del risk_managers[symbol]
                continue
        except KeyError as e:
            log.error(f"Configuration error for symbol '{symbol}': Missing key {e}. This symbol will be skipped.")
            if symbol in strategies: del strategies[symbol]
            if symbol in risk_managers: del risk_managers[symbol]
            continue
    log.info(f"EA configured to trade symbols: {list(strategies.keys())}")

    # --- 4. Main Trading Loop ---
    try:
        while True:
            log.info("-------------------- New Trading Cycle --------------------")
            for symbol in strategies.keys():
                log.info(f"--- Processing symbol: {symbol} ---")
                try:
                    strategy, risk_manager = strategies[symbol], risk_managers[symbol]
                    symbol_config = config[symbol]
                    timeframe_str, risk_percent = symbol_config['timeframe'], float(
                        symbol_config.get('risk_per_trade_percent', 1.0))

                    if connector.get_open_positions(symbol=symbol, magic_number=magic_number): continue

                    historical_data = connector.get_historical_data(symbol, timeframe_str, strategy.min_bars + 5)
                    if historical_data is None or historical_data.empty:
                        log.warning(f"Could not fetch historical data for {symbol}. Skipping cycle.");
                        continue

                    if isinstance(strategy, EMARibbonScalper):
                        processed_df = strategy.calculate_signals(historical_data)
                        last_candle = processed_df.iloc[-2]
                        if last_candle['signal'] in [1, -1]:
                            entry_signal = "BUY" if last_candle['signal'] == 1 else "SELL"
                            log.info(f"EMARibbonScalper signal found for {symbol}: {entry_signal}")

                            sl_price = last_candle['stop_loss']
                            tick = mt5.symbol_info_tick(symbol)
                            if not tick: continue
                            entry_price = tick.ask if entry_signal == "BUY" else tick.bid

                            # === FINALIZED VALIDATION AND CALCULATION LOGIC ===
                            sl_price = risk_manager.validate_and_adjust_sl(sl_price, entry_price, entry_signal)
                            stop_distance_price = abs(entry_price - sl_price)

                            # CRITICAL FIX: Calculate stop distance in POINTS and ensure it's an INTEGER
                            stop_loss_in_points = int(round(stop_distance_price / risk_manager.point))

                            # Abort if the stop distance is zero after rounding, which can happen with very tight stops
                            if stop_loss_in_points == 0:
                                log.warning(f"Stop distance for {symbol} is zero after adjustment. Aborting trade.")
                                continue

                            tp_price = entry_price + (
                                        stop_distance_price * risk_manager.risk_reward_ratio) if entry_signal == "BUY" else entry_price - (
                                        stop_distance_price * risk_manager.risk_reward_ratio)

                            account_info = mt5.account_info()
                            if not account_info:
                                log.error("Could not retrieve account info. Skipping trade.");
                                continue

                            # Pass the clean integer value to the volume calculator
                            volume = risk_manager.calculate_volume(account_info.balance, risk_percent,
                                                                   stop_loss_in_points)

                            if volume:
                                symbol_info = mt5.symbol_info(symbol)
                                sl_price = round(sl_price, symbol_info.digits)
                                tp_price = round(tp_price, symbol_info.digits)
                                connector.place_order(symbol, entry_signal, volume, sl_price, tp_price, magic_number)

                    # ... (Legacy strategy logic remains here) ...

                except Exception as e:
                    log.error(f"Unexpected error processing {symbol}: {e}", exc_info=True);
                    continue

            sleep_duration = int(trade_params['main_loop_sleep_seconds'])
            log.info(f"Cycle complete. Sleeping for {sleep_duration} seconds...")
            time.sleep(sleep_duration)

    except KeyboardInterrupt:
        log.info("EA stopped by user.")
    except Exception as e:
        log.error(f"A critical error occurred in the main loop: {e}", exc_info=True)
    finally:
        connector.disconnect()
        log.info("Python Expert Advisor has been shut down.")


if __name__ == "__main__":
    run()