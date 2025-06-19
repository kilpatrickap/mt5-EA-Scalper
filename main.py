# main.py
import configparser
import time
import pandas as pd
import MetaTrader5 as mt5

from logger_setup import log
from mt5_connector import MT5Connector
from risk_manager import RiskManager
# Import both strategies to make the engine multi-strategy capable
from trading_strategy import RegimeMomentumStrategy, EMARibbonScalper


def run():
    """Main execution function for the multi-strategy Expert Advisor."""
    # --- 1. Configuration Loading ---
    config = configparser.ConfigParser()
    config.read('config.ini')

    try:
        mt5_creds = config['mt5_credentials']
        trade_params = config['trading_parameters']
    except KeyError as e:
        log.error(f"Configuration error: Missing section or key - {e}. Aborting.")
        return

    # --- 2. Initialization of Main Components ---
    log.info("Starting Python Multi-Symbol/Multi-Strategy Expert Advisor")
    connector = MT5Connector(
        login=int(mt5_creds['account']),
        password=mt5_creds['password'],
        server=mt5_creds['server']
    )

    if not connector.connect():
        log.error("Failed to connect to MT5. Exiting.")
        return

    try:
        symbols_to_trade = [s.strip() for s in trade_params['symbols'].split(',')]
        magic_number = int(trade_params['magic_number'])
    except KeyError as e:
        log.error(f"Trading parameter '{e}' is missing in config.ini. Exiting.")
        connector.disconnect()
        return

    # --- 3. Pre-Loop Initialization of Strategies and Risk Managers (NOW MODULAR) ---
    strategies = {}
    risk_managers = {}
    log.info("Initializing strategies and risk managers for each symbol...")

    for symbol in symbols_to_trade:
        try:
            symbol_config = config[symbol]
            strategy_type = symbol_config['strategy_type']

            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info:
                log.error(f"Could not get info for {symbol}. It will be skipped.")
                continue
            point = symbol_info.point

            # Initialize Risk Manager (shared logic)
            # For EMARibbonScalper, stop_loss_pips is a placeholder as it's calculated dynamically.
            risk_managers[symbol] = RiskManager(
                symbol=symbol,
                stop_loss_pips=int(symbol_config.get('stop_loss_pips', 100)),  # Use .get for safety
                risk_reward_ratio=float(symbol_config['risk_reward_ratio']),
                point=point,
                stops_level=symbol_info.trade_stops_level
            )

            # Initialize the correct Strategy based on config
            if strategy_type == 'EMARibbonScalper':
                strategies[symbol] = EMARibbonScalper(
                    ema_fast_periods=[int(p) for p in symbol_config['ema_fast_periods'].split(',')],
                    ema_slow_period=int(symbol_config['ema_slow_period']),
                    rsi_period=int(symbol_config['rsi_period']),
                    rsi_level=int(symbol_config['rsi_level']),
                    consolidation_threshold_pips=float(symbol_config['consolidation_threshold_pips']),
                    risk_reward_ratio=float(symbol_config['risk_reward_ratio']),
                    pip_size=point
                )
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
                )
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
                    strategy = strategies[symbol]
                    risk_manager = risk_managers[symbol]
                    symbol_config = config[symbol]
                    timeframe_str = symbol_config['timeframe']
                    risk_percent = float(symbol_config.get('risk_per_trade_percent', 1.0))

                    open_positions = connector.get_open_positions(symbol=symbol, magic_number=magic_number)

                    # --- EXIT LOGIC (STRATEGY-AWARE) ---
                    if open_positions:
                        # For RegimeMomentumStrategy, check for a protective exit signal
                        if isinstance(strategy, RegimeMomentumStrategy):
                            historical_data = connector.get_historical_data(symbol, timeframe_str,
                                                                            strategy.min_bars + 5)
                            current_pos = open_positions[0]
                            pos_type = "BUY" if current_pos.type == mt5.ORDER_TYPE_BUY else "SELL"
                            if strategy.get_exit_signal(historical_data, pos_type):
                                log.info(
                                    f"Protective exit signal for {symbol}. Closing position #{current_pos.ticket}.")
                                connector.close_position(current_pos, comment="Closed due to trend failure")
                        else:
                            # For EMARibbonScalper, exits are handled by SL/TP set at the broker.
                            log.info(f"Holding current position for {symbol}. Exit managed by SL/TP.")
                        continue  # Skip to next symbol if a position is open and being managed

                    # --- ENTRY LOGIC (STRATEGY-AWARE) ---
                    if not open_positions:
                        historical_data = connector.get_historical_data(symbol, timeframe_str, strategy.min_bars + 5)
                        if historical_data is None or historical_data.empty:
                            log.warning(f"Could not fetch historical data for {symbol}. Skipping cycle.")
                            continue

                        # --- EMARibbonScalper Entry Logic ---
                        if isinstance(strategy, EMARibbonScalper):
                            processed_df = strategy.calculate_signals(historical_data)
                            # Check the last COMPLETED candle for a signal
                            last_candle = processed_df.iloc[-2]

                            if last_candle['signal'] in [1, -1]:
                                entry_signal = "BUY" if last_candle['signal'] == 1 else "SELL"
                                sl_price = last_candle['stop_loss']
                                tp_price = last_candle['take_profit']
                                log.info(f"EMARibbonScalper signal found for {symbol}: {entry_signal}")

                                tick = mt5.symbol_info_tick(symbol)
                                if not tick: continue

                                # Use dynamic SL/TP from strategy to calculate volume
                                entry_price = tick.ask if entry_signal == "BUY" else tick.bid
                                sl_pips = abs(entry_price - sl_price) / risk_manager.point

                                account_balance = mt5.account_info().balance
                                volume = risk_manager.calculate_volume(account_balance, risk_percent, sl_pips)

                                if volume:
                                    connector.place_order(symbol, entry_signal, volume, sl_price, tp_price,
                                                          magic_number)

                        # --- RegimeMomentumStrategy Entry Logic ---
                        elif isinstance(strategy, RegimeMomentumStrategy):
                            entry_signal = strategy.get_entry_signal(historical_data)
                            log.info(f"RegimeMomentumStrategy signal for {symbol}: {entry_signal}")

                            if entry_signal in ["BUY", "SELL"]:
                                tick = mt5.symbol_info_tick(symbol)
                                if not tick: continue

                                # Use fixed SL/TP from RiskManager
                                sl_price, tp_price, sl_pips = risk_manager.calculate_sl_tp(entry_signal, tick.ask,
                                                                                           tick.bid)

                                if sl_price and tp_price and sl_pips:
                                    account_balance = mt5.account_info().balance
                                    volume = risk_manager.calculate_volume(account_balance, risk_percent, sl_pips)
                                    if volume:
                                        connector.place_order(symbol, entry_signal, volume, sl_price, tp_price,
                                                              magic_number)

                except Exception as e:
                    log.error(f"Unexpected error processing {symbol}: {e}", exc_info=True)
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