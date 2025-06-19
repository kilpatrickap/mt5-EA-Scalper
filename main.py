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
    # --- Configuration and Initialization (Unchanged) ---
    config = configparser.ConfigParser()
    config.read('config.ini')
    try:
        mt5_creds, trade_params = config['mt5_credentials'], config['trading_parameters']
    except KeyError as e:
        log.error(f"Config error: {e}. Aborting.");
        return
    log.info("Starting Python Multi-Symbol/Multi-Strategy EA")
    connector = MT5Connector(login=int(mt5_creds['account']), password=mt5_creds['password'],
                             server=mt5_creds['server'])
    if not connector.connect(): log.error("Failed to connect to MT5. Exiting."); return
    try:
        symbols_to_trade, magic_number = [s.strip() for s in trade_params['symbols'].split(',')], int(
            trade_params['magic_number'])
    except KeyError as e:
        log.error(f"Trading param '{e}' missing. Exiting.");
        connector.disconnect();
        return
    strategies, risk_managers = {}, {}
    log.info("Initializing strategies and risk managers...")
    for symbol in symbols_to_trade:
        try:
            symbol_config = config[symbol]
            strategy_type = symbol_config['strategy_type']
            symbol_info = mt5.symbol_info(symbol)
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
            elif strategy_type == 'RegimeMomentum':
                strategies[symbol] = RegimeMomentumStrategy(fast_ema_period=int(symbol_config['fast_ema_period']),
                                                            slow_ema_period=int(symbol_config['slow_ema_period']),
                                                            adx_period=int(symbol_config['adx_period']),
                                                            adx_threshold=int(symbol_config['adx_threshold']),
                                                            stoch_k_period=int(symbol_config['stoch_k_period']),
                                                            stoch_d_period=int(symbol_config['stoch_d_period']),
                                                            stoch_slowing=int(symbol_config['stoch_slowing']),
                                                            stoch_oversold=int(symbol_config['stoch_oversold']),
                                                            stoch_overbought=int(symbol_config['stoch_overbought']))
                log.info(f"Initialized RegimeMomentumStrategy for {symbol}.")
        except KeyError as e:
            log.error(f"Config error for {symbol}: {e}. Skipping.");
            continue
    log.info(f"EA configured to trade symbols: {list(strategies.keys())}")

    # --- Main Trading Loop ---
    try:
        while True:
            log.info("---------------- New Trading Cycle ---------------")
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
                    last_candle = None
                    if isinstance(strategy, EMARibbonScalper):
                        processed_df = strategy.calculate_signals(historical_data)
                        last_candle = processed_df.iloc[-2]
                        if last_candle['signal'] == 1:
                            entry_signal = "BUY"
                        elif last_candle['signal'] == -1:
                            entry_signal = "SELL"

                    log.info(f"Strategy Entry Signal for {symbol} on {timeframe_str}: {entry_signal}")

                    if entry_signal in ["BUY", "SELL"]:
                        if last_candle is None: continue

                        # === REFACTORED TRADE EXECUTION LOGIC STARTS HERE ===

                        # 1. Get the suggested SL from the strategy
                        suggested_sl_price = last_candle['stop_loss']
                        if pd.isna(suggested_sl_price) or suggested_sl_price <= 0:
                            log.warning(
                                f"Strategy for {symbol} produced an invalid SL ({suggested_sl_price}). Skipping signal.")
                            continue

                        # 2. Get the current market price for entry
                        tick = mt5.symbol_info_tick(symbol)
                        if not tick: continue
                        entry_price = tick.ask if entry_signal == "BUY" else tick.bid

                        # 3. Use the RiskManager to get final, validated SL/TP prices and SL distance in pips
                        sl_price, tp_price, stop_loss_in_pips = risk_manager.calculate_sl_tp(
                            order_type=entry_signal,
                            entry_price=entry_price,
                            suggested_sl_price=suggested_sl_price
                        )

                        # Check if the risk manager could determine valid parameters
                        if sl_price is None:
                            log.warning(f"RiskManager failed to calculate valid SL/TP for {symbol}. Skipping signal.")
                            continue

                        # 4. Get account info and calculate volume using the validated stop_loss_in_pips
                        account_info = mt5.account_info()
                        if not account_info:
                            log.error("Could not retrieve account info.");
                            continue

                        volume = risk_manager.calculate_volume(
                            account_balance=account_info.balance,
                            risk_percent=risk_percent,
                            stop_loss_pips=stop_loss_in_pips
                        )

                        # 5. If volume is valid, place the order with the final parameters from the RiskManager
                        if volume:
                            log.info(
                                f"Placing {entry_signal} order for {symbol} | Vol: {volume}, SL: {sl_price}, TP: {tp_price}")
                            # No need to round again, RiskManager already did it.
                            connector.place_order(symbol, entry_signal, volume, sl_price, tp_price, magic_number)

                        # === REFACTORED TRADE EXECUTION LOGIC ENDS HERE ===

                except Exception as e:
                    log.error(f"Error processing {symbol}: {e}", exc_info=True);
                    continue

            sleep_duration = int(trade_params.get('main_loop_sleep_seconds', 30))
            log.info(f"Cycle complete. Sleeping for {sleep_duration}s...")
            time.sleep(sleep_duration)
    except KeyboardInterrupt:
        log.info("EA stopped by user.")
    finally:
        connector.disconnect();
        log.info("Python EA shut down.")


if __name__ == "__main__":
    run()