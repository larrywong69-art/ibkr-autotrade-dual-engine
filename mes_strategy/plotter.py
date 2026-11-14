"""
Institutional performance visualization module for MES Strategy v4.6.
Generates publication-quality charts using Matplotlib:
1. Cumulative Equity Curve with High-Water Mark
2. Underwater Drawdown profile (%)
3. Trade PnL distribution (Wins vs Losses)
4. Cumulative PnL by Rule (Rule 2 Range, Rule 3 False Breakout, Rule 4 Pullback)
"""

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import numpy as np
from typing import Dict, Any


def plot_backtest_results(
    results: Dict[str, Any],
    output_png_path: str = "artifacts/file_generation/ttl=63d/output/mes_strategy_backtest_report.png"
):
    df_equity = results["equity_curve"]
    trades_df = results["trades"]
    metrics = results["metrics"]

    fig = plt.figure(figsize=(14, 10), dpi=150)
    gs = fig.add_gridspec(3, 2, height_ratios=[1.8, 1.0, 1.2], hspace=0.35, wspace=0.25)

    # 1. Equity Curve (Top span)
    ax_equity = fig.add_subplot(gs[0, :])
    ax_equity.plot(df_equity.index, df_equity["equity"], label="Portfolio Equity ($)", color="#1f77b4", linewidth=1.8)
    cummax = df_equity["equity"].cummax()
    ax_equity.plot(df_equity.index, cummax, label="High-Water Mark", color="#2ca02c", linestyle="--", alpha=0.7)
    ax_equity.axhline(metrics["initial_capital"], color="#7f7f7f", linestyle=":", label="Initial Capital ($50k)")

    title_str = (
        f"CME MES Nine-in-One Quant Strategy v4.6 Backtest Performance\n"
        f"Net Profit: ${metrics['total_net_profit']:,.2f} ({metrics['total_return_pct']:+.1f}%) | "
        f"Win Rate: {metrics['win_rate_pct']:.1f}% | Profit Factor: {metrics['profit_factor']:.2f} | "
        f"Max DD: {metrics['max_drawdown_pct']:.2f}% | Sharpe: {metrics['sharpe_ratio']:.2f} | Sortino: {metrics['sortino_ratio']:.2f}"
    )
    ax_equity.set_title(title_str, fontsize=12, fontweight="bold", pad=12)
    ax_equity.set_ylabel("Equity ($ USD)", fontsize=10)
    ax_equity.grid(True, linestyle="--", alpha=0.5)
    ax_equity.legend(loc="upper left", frameon=True, facecolor="white", framealpha=0.9)
    ax_equity.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))

    # 2. Drawdown Profile (Middle span)
    ax_dd = fig.add_subplot(gs[1, :])
    dd_pct = (df_equity["equity"] - cummax) / cummax * 100.0
    ax_dd.fill_between(df_equity.index, dd_pct, 0, color="#d62728", alpha=0.35, label="Drawdown (%)")
    ax_dd.plot(df_equity.index, dd_pct, color="#d62728", linewidth=1.0)
    ax_dd.axhline(-4.0, color="darkred", linestyle="--", linewidth=1.2, label="Rule 9: Daily Circuit Breaker (-4%)")
    ax_dd.set_title("Underwater Drawdown Profile (%)", fontsize=10, fontweight="bold")
    ax_dd.set_ylabel("Drawdown %", fontsize=10)
    ax_dd.grid(True, linestyle="--", alpha=0.5)
    ax_dd.legend(loc="lower left", frameon=True, facecolor="white", framealpha=0.9)
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))

    # 3. Trade PnL Distribution (Bottom Left)
    ax_trades = fig.add_subplot(gs[2, 0])
    if len(trades_df) > 0 and "pnl" in trades_df.columns:
        colors = ["#2ca02c" if p > 0 else "#d62728" for p in trades_df["pnl"]]
        ax_trades.bar(range(len(trades_df)), trades_df["pnl"], color=colors, width=0.8, alpha=0.85)
        ax_trades.axhline(0, color="black", linewidth=0.8)
        ax_trades.set_title(f"Trade PnL Sequence (Total Trades: {len(trades_df)})", fontsize=10, fontweight="bold")
        ax_trades.set_xlabel("Trade Sequence #", fontsize=9)
        ax_trades.set_ylabel("Realized PnL ($ USD)", fontsize=9)
        ax_trades.grid(True, linestyle="--", alpha=0.5)

    # 4. Performance by Strategy Rule (Bottom Right)
    ax_rules = fig.add_subplot(gs[2, 1])
    breakdown = metrics.get("rule_breakdown", {})
    if breakdown:
        rules = list(breakdown.keys())
        pnls = [breakdown[r]["total_pnl"] for r in rules]
        counts = [breakdown[r]["count"] for r in rules]
        win_rates = [breakdown[r]["win_rate"] for r in rules]

        clean_labels = [r.replace("_", "\n") for r in rules]
        bar_colors = ["#1f77b4", "#ff7f0e", "#2ca02c"][:len(rules)]
        bars = ax_rules.bar(clean_labels, pnls, color=bar_colors, alpha=0.85, width=0.5)

        for bar, count, wr in zip(bars, counts, win_rates):
            height = bar.get_height()
            offset = 15 if height >= 0 else -35
            ax_rules.annotate(
                f"${height:,.0f}\n({count} tr, {wr:.0f}% win)",
                xy=(bar.get_x() + bar.get_width() / 2, height),
                xytext=(0, offset),
                textcoords="offset points",
                ha="center", va="center",
                fontsize=8, fontweight="bold"
            )

        ax_rules.axhline(0, color="black", linewidth=0.8)
        ax_rules.set_title("Total PnL Contribution by Rule ($)", fontsize=10, fontweight="bold")
        ax_rules.set_ylabel("PnL ($ USD)", fontsize=9)
        ax_rules.grid(True, linestyle="--", alpha=0.5)

    plt.savefig(output_png_path, bbox_inches="tight")
    plt.close()
    return output_png_path
