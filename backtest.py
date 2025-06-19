# backtest.py

import configparser
import pandas as pd
from datetime import datetime
from tqdm import tqdm
import MetaTrader5 as mt5

# Using the original logger name as requested
from logger_setup import log
from mt5_connector import MT5Connector
from trading_strategy import EMARibbonScalper


def run_backtest():
    """
    Main function to execute the backtesting process.
    It fetches data, calculates signals, simulates trading bar-by-bar, and reports performance.
    """
    # --- 1. Configuration and Setup ---
    log.info("--- Starting Backtest ---")
    config = configparser.ConfigParser()
    config.read('config.ini')

    try:
        backtest_params = config['backtest_parameters']
        mt5_creds = config['mt5_credentials']
        symbol = backtest_params['backtest_symbol']
        strategy_config = config[symbol]
        start_date = datetime.strptime(backtest_params['start_date'], '%Y-%m-%d')
        end_date = datetime.strptime(backtest_params['end_date'], '%Y-%m-%d')
        timeframe_str = strategy_config['timeframe']
    except KeyError as e:
        log.error(f"Configuration error: Missing section or key - {e}. Aborting.")
        return

    # --- 2. Data Fetching and Connection Handling ---
    log.info(
        f"Connecting to MT5 to fetch data for {symbol} on {timeframe_str} from {start_date.date()} to {end_date.date()}...")
    connector = MT5Connector(login=int(mt5_creds['account']), password=mt5_creds['password'],
                             server=mt5_creds['server'])
    if not connector.connect(): return
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        log.error(f"Could not retrieve info for {symbol}. It may not be in Market Watch or is invalid.")
        connector.disconnect()
        return
    point = symbol_info.point

    timeframe_map = {'M1': mt5.TIMEFRAME_M1, 'M5': mt5.TIMEFRAME_M5, 'M15': mt5.TIMEFRAME_M15, 'H1': mt5.TIMEFRAME_H1}
    mt5_timeframe = timeframe_map.get(timeframe_str.upper())
    if not mt5_timeframe:
        log.error(f"Invalid timeframe '{timeframe_str}' in config for {symbol}.")
        connector.disconnect()
        return

    from dateutil.relativedelta import relativedelta
    buffer_start_date = start_date - relativedelta(months=2)
    rates = mt5.copy_rates_range(symbol, mt5_timeframe, buffer_start_date, end_date)
    connector.disconnect()
    log.info("Disconnected from MT5. Proceeding with offline simulation.")

    if rates is None or len(rates) == 0:
        log.error("Failed to fetch historical data.");
        return

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df.sort_values(by='time').reset_index(drop=True)

    sim_start_index = df[df['time'] >= start_date].index[0]
    log.info(f"Prepared {len(df)} total data points. Simulation will start at index {sim_start_index}.")

    # --- 3. Initialize Strategy and Calculate ALL Signals ---
    log.info("Initializing strategy and calculating all signals...")
    strategy = EMARibbonScalper(
        ema_fast_periods=[int(p) for p in strategy_config['ema_fast_periods'].split(',')],
        # === THIS IS THE CORRECTED LINE ===
        # The script now looks for 'ema_slow_period' to match the config file standard.
        ema_slow_period=int(strategy_config['ema_slow_period']),
        # ==================================
        rsi_period=int(strategy_config['rsi_period']),
        rsi_level=int(strategy_config['rsi_level']),
        consolidation_threshold_pips=float(strategy_config['consolidation_threshold_pips']),
        risk_reward_ratio=float(strategy_config['risk_reward_ratio']),
        pip_size=point
    )
    df = strategy.calculate_signals(df)
    log.info("All signals, SL, and TP levels have been pre-calculated.")

    # --- 4. The Simulation Loop ---
    log.info("Starting simulation loop...")
    current_trade = None
    completed_trades = []
    max_trade_duration = int(strategy_config.get('max_trade_duration_candles', 10))

    for i in tqdm(range(sim_start_index, len(df)), desc=f"Backtesting {symbol}"):
        current_candle = df.iloc[i]

        if current_trade:
            exit_price, pnl, comment = None, 0, ''

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
                pnl = (exit_price - current_trade['entry_price']) / point if current_trade['type'] == 'BUY' else \
                    (current_trade['entry_price'] - exit_price) / point
                current_trade.update({'exit_price': exit_price, 'exit_time': current_candle['time'], 'pnl_pips': pnl,
                                      'comment': comment})
                completed_trades.append(current_trade)
                current_trade = None

        if not current_trade:
            if current_candle['signal'] != 0:
                signal_type = "BUY" if current_candle['signal'] == 1 else "SELL"
                entry_price = current_candle['close']
                sl_price = current_candle['stop_loss']
                tp_price = current_candle['take_profit']

                if sl_price > 0 and tp_price > 0:
                    current_trade = {
                        'id': len(completed_trades) + 1,
                        'symbol': symbol,
                        'type': signal_type,
                        'entry_time': current_candle['time'],
                        'entry_price': entry_price,
                        'sl': sl_price,
                        'tp': tp_price,
                        'entry_index': i
                    }

    # --- 5. Reporting ---
    log.info("Simulation complete. Generating report...")
    if not completed_trades:
        log.warning("No trades were executed during the backtest period.")
        return

    results_df = pd.DataFrame(completed_trades)
    total_trades = len(results_df)
    winning_trades = results_df[results_df['pnl_pips'] > 0]
    losing_trades = results_df[results_df['pnl_pips'] <= 0]
    win_rate = (len(winning_trades) / total_trades) * 100 if total_trades > 0 else 0
    total_pnl_pips = results_df['pnl_pips'].sum()
    avg_win_pips = winning_trades['pnl_pips'].mean() if len(winning_trades) > 0 else 0
    avg_loss_pips = losing_trades['pnl_pips'].mean() if len(losing_trades) > 0 else 0
    gross_profit = winning_trades['pnl_pips'].sum()
    gross_loss = abs(losing_trades['pnl_pips'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    print("\n" + "=" * 50)
    print(f"{'BACKTEST REPORT':^50}")
    print("=" * 50)
    print(f" Symbol            : {symbol} ({timeframe_str})")
    print(f" Period            : {start_date.date()} to {end_date.date()}")
    print(f" Strategy          : EMA Ribbon Breakout")
    print("-" * 50)
    print(f"{'PERFORMANCE METRICS':^50}")
    print("-" * 50)
    print(f" Total Net Profit  : {total_pnl_pips:.2f} pips")
    print(f" Profit Factor     : {profit_factor:.2f}")
    print(f" Total Trades      : {total_trades}")
    print(f" Win Rate          : {win_rate:.2f}%")
    print(f" Average Win       : {avg_win_pips:.2f} pips")
    print(f" Average Loss      : {avg_loss_pips:.2f} pips")
    print("=" * 50)

    results_filename = f"backtest_results_{symbol}_{timeframe_str}_{start_date.date()}_to_{end_date.date()}.csv"
    results_df.to_csv(results_filename, index=False)
    log.info(f"Detailed trade log saved to '{results_filename}'")


if __name__ == "__main__":
    run_backtest()