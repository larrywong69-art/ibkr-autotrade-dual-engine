"""
Event-driven Backtesting Engine for CME MES Nine-in-One Strategy v4.6.
Accurately models:
- 1H HTF / 5m LTF time-synchronization with zero lookahead bias
- Multi-tranche executions (Tranche A 50%, B 25%, C 25% with 4R trailing stop)
- Slippage and IBKR commissions ($0.62 / contract side)
- CME STP PRT execution semantics
- Daily 17:00 ET circuit breaker reset
- Full institutional performance metrics & export
"""

import numpy as np
import pandas as pd
from typing import Dict, Any, List, Tuple, Optional
from mes_strategy.config import StrategyConfig
from mes_strategy.strategy_engine import NineInOneStrategy
from mes_strategy.indicators import (
    calculate_atr, calculate_price_sortino, calculate_adx,
    calculate_adx_percentile, calculate_ema_and_slope, calculate_rvol
)


def prepare_market_data(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    config: StrategyConfig
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Calculate all technical indicators on 5m and 1H data."""
    # 5m indicators
    df_5m = df_5m.copy()
    df_5m["atr14"] = calculate_atr(
        df_5m["high"], df_5m["low"], df_5m["close"],
        period=config.indicators.atr_period,
        gap_cap_multiplier=config.indicators.atr_gap_cap_multiplier
    )
    df_5m["price_sortino"] = calculate_price_sortino(
        df_5m["close"],
        window=config.indicators.price_sortino_window
    )
    df_5m["rvol"] = calculate_rvol(
        df_5m,
        volume_col="volume",
        lookback_days=config.indicators.rvol_lookback_days
    )

    # 1H indicators
    df_1h = df_1h.copy()
    df_1h["atr_1h"] = calculate_atr(
        df_1h["high"], df_1h["low"], df_1h["close"],
        period=config.indicators.atr_period
    )
    df_1h["ema200"], df_1h["slope"] = calculate_ema_and_slope(
        df_1h["close"],
        period=config.indicators.htf_ema_period,
        slope_window=config.indicators.htf_slope_window
    )
    adx_series, _, _ = calculate_adx(
        df_1h["high"], df_1h["low"], df_1h["close"],
        period=config.indicators.adx_period
    )
    df_1h["adx14"] = adx_series
    df_1h["adx_percentile"] = calculate_adx_percentile(
        df_1h["adx14"],
        window=config.indicators.adx_percentile_window,
        percentile=config.indicators.adx_percentile,
        lower_bound=config.indicators.adx_lower_bound,
        upper_bound=config.indicators.adx_upper_bound
    )

    # Consecutive bars on same side of EMA200
    same_side = np.zeros(len(df_1h), dtype=int)
    for i in range(1, len(df_1h)):
        c = df_1h["close"].iloc[i]
        e = df_1h["ema200"].iloc[i]
        prev_c = df_1h["close"].iloc[i - 1]
        prev_e = df_1h["ema200"].iloc[i - 1]
        if (c > e and prev_c > prev_e) or (c < e and prev_c < prev_e):
            same_side[i] = same_side[i - 1] + 1
        else:
            same_side[i] = 1
    df_1h["consecutive_same_side"] = same_side

    return df_5m, df_1h


class BacktestRunner:
    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        self.strategy = NineInOneStrategy(self.config)

    def run(self, df_5m: pd.DataFrame, df_1h: pd.DataFrame, precomputed: bool = False) -> Dict[str, Any]:
        """Execute historical bar-by-bar backtest."""
        if precomputed:
            df_5m_proc, df_1h_proc = df_5m, df_1h
        else:
            df_5m_proc, df_1h_proc = prepare_market_data(df_5m, df_1h, self.config)

        equity = self.config.initial_capital
        equity_curve = []
        timestamps = []
        actions_log = []

        # Map 1H bars to 5m index (as-of join to avoid future data leakage)
        df_1h_indices = df_1h_proc.index
        h_pointer = 0

        current_trading_date = None

        # Start after sufficient warmup bars (e.g. 50 bars)
        warmup = max(60, self.config.indicators.price_sortino_window * 2)

        for i in range(warmup, len(df_5m_proc)):
            curr_bar = df_5m_proc.iloc[i]
            t = curr_bar.name if isinstance(curr_bar.name, pd.Timestamp) else pd.to_datetime(curr_bar.name)

            # Check 17:00 ET settlement reset
            bar_date = t.date()
            if bar_date != current_trading_date:
                current_trading_date = bar_date
                self.strategy.reset_daily_circuit_breaker(str(bar_date), equity)

            # Align HTF 1H bar (latest closed 1H bar before t)
            while h_pointer + 1 < len(df_1h_indices) and df_1h_indices[h_pointer + 1] <= t:
                h_pointer += 1

            htf_row = df_1h_proc.iloc[h_pointer]
            htf_data = {
                "close": float(htf_row["close"]),
                "ema200": float(htf_row["ema200"]),
                "slope": float(htf_row["slope"]),
                "atr": float(htf_row["atr_1h"]),
                "adx_percentile": float(htf_row["adx_percentile"]),
                "consecutive_same_side": int(htf_row["consecutive_same_side"])
            }

            # OnBarClose execution
            actions, signal = self.strategy.on_bar_close(df_5m_proc, i, htf_data)
            if actions:
                for act in actions:
                    actions_log.append(f"[{t}] {act}")

            # Mark to market equity
            equity = self.strategy.daily_risk.current_equity
            equity_curve.append(equity)
            timestamps.append(t)

        df_equity = pd.DataFrame({"datetime": timestamps, "equity": equity_curve})
        df_equity.set_index("datetime", inplace=True)

        # Compile metrics
        trades_df = pd.DataFrame(self.strategy.trade_history)
        metrics = self._calculate_metrics(df_equity, trades_df)

        return {
            "metrics": metrics,
            "equity_curve": df_equity,
            "trades": trades_df,
            "actions_log": actions_log,
            "df_5m": df_5m_proc
        }

    def _calculate_metrics(self, df_equity: pd.DataFrame, trades_df: pd.DataFrame) -> Dict[str, Any]:
        """Compute institutional performance metrics."""
        initial = self.config.initial_capital
        if len(df_equity) == 0:
            return {}

        final_equity = df_equity["equity"].iloc[-1]
        total_net_profit = final_equity - initial
        total_return_pct = (total_net_profit / initial) * 100.0

        # Drawdown
        rolling_max = df_equity["equity"].cummax()
        drawdown = df_equity["equity"] - rolling_max
        drawdown_pct = (drawdown / rolling_max) * 100.0
        max_drawdown_dollar = abs(drawdown.min())
        max_drawdown_pct = abs(drawdown_pct.min())

        # Trade metrics
        total_trades = len(trades_df)
        if total_trades > 0:
            winning_trades = trades_df[trades_df["pnl"] > 0]
            losing_trades = trades_df[trades_df["pnl"] < 0]
            win_count = len(winning_trades)
            loss_count = len(losing_trades)
            win_rate = (win_count / total_trades) * 100.0 if total_trades > 0 else 0.0

            gross_profit = winning_trades["pnl"].sum() if win_count > 0 else 0.0
            gross_loss = abs(losing_trades["pnl"].sum()) if loss_count > 0 else 0.0
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

            avg_trade = trades_df["pnl"].mean()
            avg_win = winning_trades["pnl"].mean() if win_count > 0 else 0.0
            avg_loss = abs(losing_trades["pnl"].mean()) if loss_count > 0 else 0.0
            payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0
        else:
            win_rate = 0.0
            profit_factor = 0.0
            avg_trade = 0.0
            avg_win = 0.0
            avg_loss = 0.0
            payoff_ratio = 0.0

        # Daily returns for Sharpe and Sortino
        daily_equity = df_equity["equity"].resample("D").last().dropna()
        daily_returns = daily_equity.pct_change().dropna()

        if len(daily_returns) > 5 and daily_returns.std() > 0:
            sharpe_ratio = float(np.sqrt(252) * daily_returns.mean() / daily_returns.std())
            neg_returns = daily_returns[daily_returns < 0]
            downside_std = neg_returns.std() if len(neg_returns) > 1 and neg_returns.std() > 0 else 1e-6
            sortino_ratio = float(np.sqrt(252) * daily_returns.mean() / downside_std)
        else:
            sharpe_ratio = 0.0
            sortino_ratio = 0.0

        # Calmar Ratio
        calmar_ratio = (total_return_pct / max_drawdown_pct) if max_drawdown_pct > 0 else 0.0

        # Rule breakdown
        rule_breakdown = {}
        if total_trades > 0 and "signal_type" in trades_df.columns:
            for sig in trades_df["signal_type"].unique():
                sig_trades = trades_df[trades_df["signal_type"] == sig]
                sig_wins = sig_trades[sig_trades["pnl"] > 0]
                rule_breakdown[sig] = {
                    "count": len(sig_trades),
                    "win_rate": (len(sig_wins) / len(sig_trades)) * 100.0,
                    "total_pnl": sig_trades["pnl"].sum()
                }

        return {
            "initial_capital": initial,
            "final_equity": final_equity,
            "total_net_profit": total_net_profit,
            "total_return_pct": total_return_pct,
            "max_drawdown_dollar": max_drawdown_dollar,
            "max_drawdown_pct": max_drawdown_pct,
            "total_trades": total_trades,
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "avg_trade_pnl": avg_trade,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "payoff_ratio": payoff_ratio,
            "sharpe_ratio": sharpe_ratio,
            "sortino_ratio": sortino_ratio,
            "calmar_ratio": calmar_ratio,
            "rule_breakdown": rule_breakdown
        }
