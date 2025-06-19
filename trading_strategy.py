# trading_strategy.py
import pandas as pd
import pandas_ta as ta
import numpy as np


# --- EXISTING STRATEGY (UNCHANGED) ---
class RegimeMomentumStrategy:
    """
    A trading strategy that combines a regime filter (ADX) with a momentum
    entry (EMA trend + Stochastic pullback).
    """

    def __init__(self, fast_ema_period: int, slow_ema_period: int, adx_period: int,
                 adx_threshold: int, stoch_k_period: int, stoch_d_period: int,
                 stoch_slowing: int, stoch_oversold: int, stoch_overbought: int):
        self.fast_ema_period = fast_ema_period
        self.slow_ema_period = slow_ema_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.stoch_k = stoch_k_period
        self.stoch_d = stoch_d_period
        self.stoch_slowing = stoch_slowing
        self.stoch_oversold = stoch_oversold
        self.stoch_overbought = stoch_overbought
        self.min_bars = max(self.fast_ema_period, self.slow_ema_period, self.adx_period, self.stoch_k)

    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df.ta.ema(length=self.fast_ema_period, append=True)
        df.ta.ema(length=self.slow_ema_period, append=True)
        df.ta.adx(length=self.adx_period, append=True)
        df.ta.stoch(k=self.stoch_k, d=self.stoch_d, smooth_k=self.stoch_slowing, append=True)
        return df

    def get_entry_signal(self, historical_data: pd.DataFrame) -> str:
        if historical_data is None or len(historical_data) < self.min_bars + 3:
            return "HOLD"
        df = self._calculate_indicators(historical_data.copy())
        fast_ema_col = f'EMA_{self.fast_ema_period}'
        slow_ema_col = f'EMA_{self.slow_ema_period}'
        adx_col = f'ADX_{self.adx_period}'
        stoch_k_col = f'STOCHk_{self.stoch_k}_{self.stoch_d}_{self.stoch_slowing}'
        last = df.iloc[-2]
        prev = df.iloc[-3]
        if pd.isna(last[adx_col]) or pd.isna(last[stoch_k_col]) or pd.isna(last[slow_ema_col]):
            return "HOLD"
        is_trending = last[adx_col] > self.adx_threshold
        is_uptrend = last[fast_ema_col] > last[slow_ema_col]
        is_downtrend = last[fast_ema_col] < last[slow_ema_col]
        stoch_crossed_up = last[stoch_k_col] > self.stoch_oversold and prev[stoch_k_col] <= self.stoch_oversold
        if is_trending and is_uptrend and stoch_crossed_up:
            return "BUY"
        stoch_crossed_down = last[stoch_k_col] < self.stoch_overbought and prev[stoch_k_col] >= self.stoch_overbought
        if is_trending and is_downtrend and stoch_crossed_down:
            return "SELL"
        return "HOLD"

    def get_exit_signal(self, historical_data: pd.DataFrame, position_type: str) -> bool:
        if historical_data is None or len(historical_data) < self.min_bars + 3:
            return False
        df = historical_data.copy()
        df.ta.ema(length=self.fast_ema_period, append=True)
        df.ta.ema(length=self.slow_ema_period, append=True)
        fast_ema_col = f'EMA_{self.fast_ema_period}'
        slow_ema_col = f'EMA_{self.slow_ema_period}'
        last = df.iloc[-2]
        prev = df.iloc[-3]
        if pd.isna(last[slow_ema_col]) or pd.isna(prev[slow_ema_col]):
            return False
        if position_type.upper() == "BUY":
            if last[fast_ema_col] < last[slow_ema_col] and prev[fast_ema_col] >= prev[slow_ema_col]:
                return True
        elif position_type.upper() == "SELL":
            if last[fast_ema_col] > last[slow_ema_col] and prev[fast_ema_col] <= prev[slow_ema_col]:
                return True
        return False


# --- NEW STRATEGY ---
class EMARibbonScalper:
    """
    Implements the "EMA Ribbon Breakout" scalping strategy.
    This class generates signals in a vectorized way for backtesting efficiency.
    """

    def __init__(self, ema_fast_periods: list[int], ema_slow_period: int, rsi_period: int,
                 rsi_level: int, consolidation_threshold_pips: float, risk_reward_ratio: float, pip_size: float):
        self.ema_ribbon_periods = ema_fast_periods
        self.ema_slow_period = ema_slow_period
        self.rsi_period = rsi_period
        self.rsi_level = rsi_level
        self.consolidation_threshold = consolidation_threshold_pips * pip_size
        self.risk_reward_ratio = risk_reward_ratio
        self.pip_size = pip_size
        self.min_bars = max(self.ema_ribbon_periods + [self.ema_slow_period, self.rsi_period]) + 5

    def calculate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculates all indicators and generates final signal, SL, and TP columns.
        Returns the DataFrame with these new columns.
        """
        if len(df) < self.min_bars:
            return df  # Not enough data

        # 1. Calculate Indicators
        self.ema_ribbon_cols = []
        for period in self.ema_ribbon_periods:
            col_name = f'EMA_{period}'
            df.ta.ema(length=period, append=True, col_names=(col_name,))
            self.ema_ribbon_cols.append(col_name)

        self.ema_slow_col = f"EMA_{self.ema_slow_period}"
        df.ta.ema(length=self.ema_slow_period, append=True, col_names=(self.ema_slow_col,))

        self.rsi_col = f"RSI_{self.rsi_period}"
        df.ta.rsi(length=self.rsi_period, append=True, col_names=(self.rsi_col,))

        df['ribbon_max'] = df[self.ema_ribbon_cols].max(axis=1)
        df['ribbon_min'] = df[self.ema_ribbon_cols].min(axis=1)

        # 2. Define Conditions (Vectorized)
        ribbon_spread = df['ribbon_max'] - df['ribbon_min']
        is_consolidating = ribbon_spread < self.consolidation_threshold

        rsi_cross_up = (df[self.rsi_col] > self.rsi_level) & (df[self.rsi_col].shift(1) <= self.rsi_level)
        rsi_cross_down = (df[self.rsi_col] < self.rsi_level) & (df[self.rsi_col].shift(1) >= self.rsi_level)

        # Long Signal Conditions
        long_cond = (
                is_consolidating.shift(1) &
                (df['close'] > df[self.ema_slow_col]) &
                (df['close'] > df['ribbon_max']) &
                (df['ribbon_min'] > df[self.ema_slow_col]) &
                rsi_cross_up
        )

        # Short Signal Conditions
        short_cond = (
                is_consolidating.shift(1) &
                (df['close'] < df[self.ema_slow_col]) &
                (df['close'] < df['ribbon_min']) &
                (df['ribbon_max'] < df[self.ema_slow_col]) &
                rsi_cross_down
        )

        # 3. Generate Signals and SL/TP
        df['signal'] = np.where(long_cond, 1, np.where(short_cond, -1, 0))
        df['stop_loss'] = 0.0
        df['take_profit'] = 0.0

        # Use .loc with boolean masks for assignment to avoid SettingWithCopyWarning
        # Long SL/TP
        long_indices = df[df['signal'] == 1].index
        if not long_indices.empty:
            slowest_ribbon_ema = f'EMA_{self.ema_ribbon_periods[-1]}'
            sl_values = np.minimum(df.loc[long_indices, 'low'], df.loc[long_indices, slowest_ribbon_ema])
            tp_distance = (df.loc[long_indices, 'close'] - sl_values) * self.risk_reward_ratio
            df.loc[long_indices, 'stop_loss'] = sl_values
            df.loc[long_indices, 'take_profit'] = df.loc[long_indices, 'close'] + tp_distance

        # Short SL/TP
        short_indices = df[df['signal'] == -1].index
        if not short_indices.empty:
            slowest_ribbon_ema = f'EMA_{self.ema_ribbon_periods[-1]}'
            sl_values = np.maximum(df.loc[short_indices, 'high'], df.loc[short_indices, slowest_ribbon_ema])
            tp_distance = (sl_values - df.loc[short_indices, 'close']) * self.risk_reward_ratio
            df.loc[short_indices, 'stop_loss'] = sl_values
            df.loc[short_indices, 'take_profit'] = df.loc[short_indices, 'close'] - tp_distance

        return df