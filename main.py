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
    config = configparser.ConfigParser();
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
                strategies[symbol] = RegimeMomentumStrategy(fast_ema_period=int(symbol_config['fast_ema_period']),
                                                            slow_ema_period=int(symbol_config['slow_ema_period']),
                                                            adx_period=int(symbol_config['adx_period']),
                                                            adx_threshold=int(symbol_config['adx_threshold']),
                                                            stoch_k_period=int(symbol_config['stoch_k_period']),
                                                            stoch_d_period=int(symbol_config['stoch_d_period']),
                                                            stoch_slowing=int(symbol_config['stoch_slowing']),
                                                            stoch_oversold=int(symbol_config['stoch_oversold']),
                                                            stoch_overbought=int(symbol_config['stoch_overbought']));
                log.info(f"Initialized RegimeMomentumStrategy for {symbol}.")
        except KeyError as e:
            log.error(f"Config error for {symbol}: {e}. Skipping."); continue
    log.info(f"EA configured to trade symbols: {list(strategies.keys())}")

    # --- Main Trading Loop ---
    try:
        while True:
            log.info("------------------ New Trading Cycle ---------------------")
            for symbol in strategies.keys():
                log.info(f"--- Processing symbol: {symbol} ---")
                try:
                    strategy, risk_manager = strategies[symbol], risk_managers[symbol]
                    symbol_config = config[symbol]
                    timeframe_str, risk_percent = symbol_config['timeframe'], float(
                        symbol_config.get('risk_per_trade_percent', 1.0))

                    if connector.get_open_positions(symbol=symbol, magic_number=magic_number): continue

                    historical_data = connector.get_historical_data(symbol, timeframe_str, strategy.min_bars + 5)
                    if historical_data is None or historical_data.empty: continue

                    entry_signal = "HOLD"
                    if isinstance(strategy, EMARibbonScalper):
                        processed_df = strategy.calculate_signals(historical_data)
                        last_candle = processed_df.iloc[-2]
                        if last_candle['signal'] == 1:
                            entry_signal = "BUY"
                        elif last_candle['signal'] == -1:
                            entry_signal = "SELL"
                    # ... elif for other strategies would go here ...

                    log.info(f"Strategy Entry Signal for {symbol} on {timeframe_str}: {entry_signal}")

                    if entry_signal in ["BUY", "SELL"]:
                        sl_price = last_candle['stop_loss']

                        # === CORRECTED ORDER OF OPERATIONS ===
                        # 1. DATA INTEGRITY CHECK (MOVED TO THE TOP)
                        # First, ensure the strategy provided a valid number for the stop loss.
                        if pd.isna(sl_price) or sl_price <= 0:
                            log.warning(f"Strategy for {symbol} produced an invalid SL ({sl_price}). Skipping signal.")
                            continue

                        tick = mt5.symbol_info_tick(symbol)
                        if not tick: continue
                        entry_price = tick.ask if entry_signal == "BUY" else tick.bid

                        # 2. BROKER COMPLIANCE CHECK
                        sl_price = risk_manager.validate_and_adjust_sl(sl_price, tick.ask, tick.bid, entry_signal)

                        # 3. RACE CONDITION CHECK
                        if (entry_signal == "BUY" and sl_price >= entry_price) or (entry_signal == "SELL" and sl_price <= entry_price):
                            log.warning(
                                f"Race condition detected for {symbol}. Entry: {entry_price}, Adj. SL: {sl_price}. Aborting.")
                            continue
                        # ====================================

                        stop_distance = abs(entry_price - sl_price)
                        tp_price = entry_price + (
                                    stop_distance * risk_manager.risk_reward_ratio) if entry_signal == "BUY" else entry_price - (
                                    stop_distance * risk_manager.risk_reward_ratio)

                        min_stop_distance_price = risk_manager.stops_level * risk_manager.point
                        if abs(tp_price - entry_price) < min_stop_distance_price:
                            log.warning(f"TP for {symbol} too close after adjustments. Aborting.")
                            continue

                        stop_loss_points = stop_distance / risk_manager.point
                        account_info = mt5.account_info()
                        if not account_info:
                            log.error("Could not retrieve account info.");
                            continue
                        volume = risk_manager.calculate_volume(account_info.balance, risk_percent, stop_loss_points)

                        if volume:
                            log.info(
                                f"Placing {entry_signal} order for {symbol} | Vol: {volume}, SL: {sl_price}, TP: {tp_price}")
                            symbol_info = mt5.symbol_info(symbol)
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


if __name__ == "__main__":
    run()
