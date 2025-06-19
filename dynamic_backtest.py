# dynamic_backtest.py
import configparser
import pandas as pd
from datetime import datetime
from tqdm import tqdm
import MetaTrader5 as mt5
from dateutil.relativedelta import relativedelta
import itertools

from logger_setup import log
from mt5_connector import MT5Connector
# Import both strategies to make the optimizer modular
from trading_strategy import RegimeMomentumStrategy, EMARibbonScalper

# --- 1. OPTIMIZATION PARAMETER RANGES ---
# Define parameter sets for EACH strategy you might want to optimize.

OPTIMIZATION_PARAMS_SCALPER = {
    'consolidation_threshold_pips': [2.5, 3.5, 4.5],
    'risk_reward_ratio': [1.0, 1.2, 1.5],
    'max_trade_duration_candles': [6, 8, 10]
}

OPTIMIZATION_PARAMS_REGIME = {
    'stop_loss_pips': [70, 90, 110],
    'risk_reward_ratio': [1.5, 1.8, 2.0],
    'adx_threshold': [28, 30, 32]
}


def run_single_backtest(df: pd.DataFrame, symbol_info: dict, params: dict):
    """
    Runs a single, high-speed backtest for one combination of parameters.
    This is a non-verbose function designed for rapid iteration.
    Returns the profit factor and total trades.
    """
    symbol = symbol_info['name']
    point = symbol_info['point']
    strategy_type = params['strategy_type']

    # --- A. Instantiate the correct strategy based on params ---
    if strategy_type == 'EMARibbonScalper':
        strategy = EMARibbonScalper(
            ema_fast_periods=[5, 8, 11, 14],  # Fixed for this optimization
            ema_slow_period=50,
            rsi_period=9,
            rsi_level=50,
            consolidation_threshold_pips=float(params['consolidation_threshold_pips']),
            risk_reward_ratio=float(params['risk_reward_ratio']),
            pip_size=point
        )
    elif strategy_type == 'RegimeMomentum':
        # This part remains for legacy support, but is less efficient
        log.warning("Optimizing RegimeMomentum uses a slower, non-vectorized loop.")
        # The old bar-by-bar logic would be needed here. For this update, we focus on the new model.
        # To keep the example clean, we will assume optimization is for the new Scalper.
        # A full implementation would require a separate loop structure for the old strategy.
        return 0, 0  # Not implemented in this example for brevity
    else:
        return 0, 0

    # --- B. The NEW, FAST, VECTORIZED Backtest Core ---
    # 1. Calculate all signals, SL, TP in one go.
    df_with_signals = strategy.calculate_signals(df.copy())

    # 2. Simulation Loop
    sim_start_index = \
    df_with_signals[df_with_signals['time'] >= datetime.strptime(params['start_date'], '%Y-%m-%d')].index[0]
    current_trade = None
    completed_trades = []
    max_trade_duration = int(params['max_trade_duration_candles'])

    for i in range(sim_start_index, len(df_with_signals)):
        current_candle = df_with_signals.iloc[i]

        # Check for exits on an open trade
        if current_trade:
            exit_price, comment = None, ''
            if current_trade['type'] == 'BUY':
                if current_candle['low'] <= current_trade['sl']:
                    exit_price, comment = current_trade['sl'], 'SL Hit'
                elif current_candle['high'] >= current_trade['tp']:
                    exit_price, comment = current_trade['tp'], 'TP Hit'
            elif current_trade['type'] == 'SELL':
                if current_candle['high'] >= current_trade['sl']:
                    exit_price, comment = current_trade['sl'], 'SL Hit'
                elif current_candle['low'] <= current_trade['tp']:
                    exit_price, comment = current_trade['tp'], 'TP Hit'

            if not exit_price and (i - current_trade['entry_index']) >= max_trade_duration:
                exit_price, comment = current_candle['close'], 'Time Stop'

            if exit_price:
                pnl = (exit_price - current_trade['entry_price']) / point if current_trade['type'] == 'BUY' else (
                                                                                                                             current_trade[
                                                                                                                                 'entry_price'] - exit_price) / point
                current_trade.update({'pnl_pips': pnl})
                completed_trades.append(current_trade)
                current_trade = None

        # Check for a new entry signal if no trade is open
        if not current_trade:
            if current_candle['signal'] != 0:
                current_trade = {
                    'type': "BUY" if current_candle['signal'] == 1 else "SELL",
                    'entry_price': current_candle['close'],
                    'sl': current_candle['stop_loss'],
                    'tp': current_candle['take_profit'],
                    'entry_index': i
                }

    # --- C. Calculate and Return Results ---
    if not completed_trades:
        return 0, 0

    results_df = pd.DataFrame(completed_trades)
    gross_profit = results_df[results_df['pnl_pips'] > 0]['pnl_pips'].sum()
    gross_loss = abs(results_df[results_df['pnl_pips'] <= 0]['pnl_pips'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    return profit_factor, len(results_df)


def run_dynamic_backtest():
    """ Main function to execute the optimization process. """
    log.info("--- Starting Dynamic Backtest (Optimization) ---")
    config = configparser.ConfigParser()
    config.read('config.ini')

    # --- Configuration and Data Fetching ---
    backtest_params = config['backtest_parameters']
    mt5_creds = config['mt5_credentials']
    symbol = backtest_params['backtest_symbol']
    # Read the strategy config for the target symbol
    try:
        symbol_config = config[symbol]
        strategy_type = symbol_config['strategy_type']
    except KeyError as e:
        log.error(f"Config section for '{symbol}' or 'strategy_type' key not found: {e}. Aborting.")
        return

    start_date_str = backtest_params['start_date']
    end_date_str = backtest_params['end_date']
    timeframe_str = symbol_config['timeframe']

    # --- Select the correct parameter set based on strategy type ---
    if strategy_type == 'EMARibbonScalper':
        param_set = OPTIMIZATION_PARAMS_SCALPER
        log.info(f"Selected EMARibbonScalper for optimization on {symbol}.")
    elif strategy_type == 'RegimeMomentum':
        param_set = OPTIMIZATION_PARAMS_REGIME
        log.info(f"Selected RegimeMomentumStrategy for optimization on {symbol}.")
    else:
        log.error(f"No optimization parameter set defined for strategy type: {strategy_type}")
        return

    # --- Data Fetching ---
    connector = MT5Connector(login=int(mt5_creds['account']), password=mt5_creds['password'],
                             server=mt5_creds['server'])
    if not connector.connect(): return
    symbol_info_obj = mt5.symbol_info(symbol)
    if not symbol_info_obj: log.error(f"Could not retrieve info for {symbol}."); connector.disconnect(); return
    symbol_info = {'name': symbol_info_obj.name, 'point': symbol_info_obj.point}  # Simplified for passing

    # Fetch data... (using MT5 constants directly for robustness)
    timeframe_map = {'M1': mt5.TIMEFRAME_M1, 'M5': mt5.TIMEFRAME_M5, 'M15': mt5.TIMEFRAME_M15, 'H1': mt5.TIMEFRAME_H1}
    mt5_timeframe = timeframe_map.get(timeframe_str.upper())
    buffer_start_date = datetime.strptime(start_date_str, '%Y-%m-%d') - relativedelta(months=2)
    rates = mt5.copy_rates_range(symbol, mt5_timeframe, buffer_start_date, datetime.strptime(end_date_str, '%Y-%m-%d'))
    connector.disconnect()

    if rates is None or len(rates) == 0: log.error("Failed to fetch historical data."); return
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df.sort_values(by='time').reset_index(drop=True)
    log.info(f"Data for {symbol} fetched successfully. Starting optimization...")

    # --- Optimization Loop ---
    keys, values = zip(*param_set.items())
    param_combinations = [dict(zip(keys, v)) for v in itertools.product(*values)]
    all_results = []

    for params in tqdm(param_combinations, desc=f"Optimizing {strategy_type} on {symbol}"):
        # Add static params needed by the backtest function
        params['start_date'] = start_date_str
        params['strategy_type'] = strategy_type

        profit_factor, total_trades = run_single_backtest(df, symbol_info, params)

        result = params.copy()
        result['profit_factor'] = round(profit_factor, 2)
        result['total_trades'] = total_trades
        all_results.append(result)

    # --- Reporting ---
    if not all_results: log.warning("Optimization finished with no results."); return

    results_df = pd.DataFrame(all_results)
    results_df = results_df.sort_values(by='profit_factor', ascending=False).reset_index(drop=True)

    print("\n" + "=" * 80)
    print(f"{'OPTIMIZATION REPORT':^80}")
    print("=" * 80)
    print(f" Symbol: {symbol} ({timeframe_str}) | Strategy: {strategy_type}")
    print(f" Period: {start_date_str} to {end_date_str}")
    print(f" Total Combinations Tested: {len(all_results)}")
    print("-" * 80)

    # Filter for reasonably good results
    profitable_results = results_df[(results_df['profit_factor'] > 1.2) & (results_df['total_trades'] > 10)]

    if profitable_results.empty:
        print("No robustly profitable parameter combinations found (PF > 1.2 & Trades > 10).")
        print("Showing the Top 5 best results found regardless of filters:")
        print(results_df.head(5).to_string())
    else:
        print("Profitable & Robust Combinations Found (PF > 1.2 & Trades > 10):")
        print(profitable_results.to_string())

    best_params = results_df.iloc[0]
    print("-" * 80)
    print("BEST OVERALL PARAMETERS FOUND:")
    # Dynamically print the best parameters found
    for key, val in best_params.items():
        if key not in ['profit_factor', 'total_trades', 'start_date', 'strategy_type']:
            print(f"  - {key.replace('_', ' ').title()}: {val}")
    print(f"  - Resulting Profit Factor: {best_params['profit_factor']}")
    print(f"  - Resulting Total Trades: {best_params['total_trades']}")
    print("=" * 80)


if __name__ == "__main__":
    run_dynamic_backtest()