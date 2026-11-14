"""
run_v5_dual_champion_backtest.py
CME MES Quantitative Strategy v5 Dual-Champion Fusion Backtest Suite (2020-2024).
Comprehensive 5-Year Continuous Backtest & Detailed Annual Breakdown (逐年回測 2020-2024).

Variants Evaluated:
1. v4.3: Baseline Defense Champion (1H HTF, Limit Orders, Cap 1 R2/R3, 50/25/25 Tranches)
2. v5.4: Session VWAP Dynamic Filter (Alone: Trend Value Guard)
3. v5.8: Asymmetric Tranche Harvesting (Alone: 60/25/15 + 1.25R/2.4R/1.2ATR Trail)
4. v5.8-VWAP: Dual Champion Fusion (VWAP Dynamic Filter + 60/25/15 Asymmetric Tranches)
5. v5.8-VWAP-4H: Triple Shield Fusion (4H Macro Lock + VWAP Dynamic Filter + 60/25/15 Asymmetric Tranches)
"""

import os
import sys
import time
import json
import shutil
from typing import Dict, Any, List, Optional
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Ensure workspace is on sys.path
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

from run_v5_optimization_suite import (
    generate_synthetic_mes_data,
    precompute_v5_market_data,
    run_single_v5_backtest
)


def compute_annual_breakdown(
    variant: str,
    df_daily: pd.DataFrame,
    trades_df: pd.DataFrame
) -> List[Dict[str, Any]]:
    """Compute detailed year-by-year performance metrics for 2020-2024."""
    annual_metrics = []
    
    # Ensure datetime index
    if not isinstance(df_daily.index, pd.DatetimeIndex):
        df_daily.index = pd.to_datetime(df_daily.index)

    years = sorted(df_daily.index.year.unique())
    prev_end_eq = 30000.0

    for yr in years:
        yr_daily = df_daily[df_daily.index.year == yr].copy()
        if len(yr_daily) == 0:
            continue

        yr_start_eq = prev_end_eq
        yr_end_eq = float(yr_daily["equity"].iloc[-1])
        prev_end_eq = yr_end_eq

        yr_pnl = yr_end_eq - yr_start_eq
        yr_ret_pct = (yr_pnl / yr_start_eq) * 100.0

        # Max Drawdown within year
        yr_peak = yr_daily["equity"].cummax()
        yr_dd = yr_peak - yr_daily["equity"]
        yr_dd_pct = (yr_dd / yr_peak) * 100.0
        yr_max_dd_pct = float(yr_dd_pct.max()) if len(yr_dd_pct) > 0 else 0.0

        # Annual Sharpe
        yr_daily["ret"] = yr_daily["equity"].pct_change().fillna(0.0)
        m_ret = yr_daily["ret"].mean()
        s_ret = yr_daily["ret"].std()
        yr_sharpe = (m_ret / s_ret * np.sqrt(252.0)) if s_ret > 1e-8 else 0.0

        # Annual Trades
        if not trades_df.empty and "exit_time" in trades_df.columns:
            exit_years = pd.to_datetime(trades_df["exit_time"]).dt.year
            yr_trades = trades_df[exit_years == yr]
            yr_count = len(yr_trades)
            if yr_count > 0:
                wins = yr_trades[yr_trades["pnl"] > 0]
                losses = yr_trades[yr_trades["pnl"] < 0]
                yr_win_rate = (len(wins) / yr_count) * 100.0
                gp = float(wins["pnl"].sum()) if len(wins) > 0 else 0.0
                gl = abs(float(losses["pnl"].sum())) if len(losses) > 0 else 0.0
                yr_pf = (gp / gl) if gl > 0 else 999.0
            else:
                yr_win_rate = 0.0
                yr_pf = 0.0
        else:
            yr_count = 0
            yr_win_rate = 0.0
            yr_pf = 0.0

        annual_metrics.append({
            "variant": variant,
            "year": int(yr),
            "start_equity": yr_start_eq,
            "end_equity": yr_end_eq,
            "net_pnl": yr_pnl,
            "return_pct": yr_ret_pct,
            "max_dd_pct": yr_max_dd_pct,
            "sharpe": yr_sharpe,
            "trade_count": yr_count,
            "win_rate_pct": yr_win_rate,
            "profit_factor": yr_pf
        })

    return annual_metrics


def generate_dual_champion_chart(
    results: Dict[str, Dict[str, Any]],
    output_png_path: str
):
    """Generate 4-panel institutional comparison chart."""
    fig, axes = plt.subplots(2, 2, figsize=(18, 12), dpi=150)
    fig.patch.set_facecolor("#0b0f19")

    for ax in axes.flat:
        ax.set_facecolor("#111827")
        ax.grid(True, linestyle="--", alpha=0.25, color="#374151")
        for spine in ax.spines.values():
            spine.set_color("#374151")
        ax.tick_params(colors="#9ca3af", labelsize=9)

    colors = {
        "v4.3": "#6b7280",            # Gray (Baseline Control)
        "v5.4": "#8b5cf6",            # Purple (VWAP Dynamic Filter)
        "v5.8": "#f97316",            # Orange (Asymmetric Tranches)
        "v5.8-VWAP": "#10b981",       # Emerald (Dual Champion Fusion)
        "v5.8-VWAP-4H": "#06b6d4"     # Cyan (Triple Shield Fusion)
    }

    # Panel 1: Compounded Equity Curves (Log Scale)
    ax1 = axes[0, 0]
    ax1.set_title("5-Year Compounded Equity Curves (2020-2024 Log Scale)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
    for var, res in results.items():
        df_d = res["daily_df"]
        lw = 2.6 if var in ("v5.8-VWAP", "v5.8-VWAP-4H") else 1.4
        alpha = 1.0 if var in ("v5.8-VWAP", "v5.8-VWAP-4H") else 0.75
        c = colors.get(var, "#ffffff")
        final_k = res["summary"]["final_equity"] / 1000.0
        ax1.plot(df_d.index, df_d["equity"], label=f"{var} (${final_k:,.0f}k)", color=c, linewidth=lw, alpha=alpha)

    ax1.set_yscale("log")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"${y:,.0f}"))
    ax1.legend(loc="upper left", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)
    ax1.set_ylabel("Account Equity (USD)", color="#9ca3af", fontsize=10)

    # Panel 2: Underwater Drawdown Curves (%)
    ax2 = axes[0, 1]
    ax2.set_title("Historical Underwater Drawdown Comparison (%)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
    for var, res in results.items():
        df_d = res["daily_df"]
        rolling_max = df_d["equity"].cummax()
        dd_pct = (df_d["equity"] - rolling_max) / rolling_max * 100.0
        lw = 2.4 if var in ("v5.8-VWAP", "v5.8-VWAP-4H") else 1.2
        c = colors.get(var, "#ffffff")
        mdd = res["summary"]["max_drawdown_pct"]
        ax2.plot(df_d.index, dd_pct, label=f"{var} (Max -{mdd:.1f}%)", color=c, linewidth=lw)

    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.0f}%"))
    ax2.legend(loc="lower left", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)
    ax2.set_ylabel("Drawdown from Peak", color="#9ca3af", fontsize=10)

    # Panel 3: Annual Return Comparison Bar Chart (2020-2024)
    ax3 = axes[1, 0]
    ax3.set_title("Annual Return Breakdown by Strategy Variant (%)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)

    years = [2020, 2021, 2022, 2023, 2024]
    n_vars = len(results)
    width = 0.16
    x_indices = np.arange(len(years))

    for i, (var, res) in enumerate(results.items()):
        annual_map = {row["year"]: row["return_pct"] for row in res["annual"]}
        y_vals = [annual_map.get(yr, 0.0) for yr in years]
        c = colors.get(var, "#ffffff")
        offsets = x_indices + (i - (n_vars - 1) / 2) * width
        ax3.bar(offsets, y_vals, width=width, label=var, color=c, alpha=0.85)

    ax3.set_xticks(x_indices)
    ax3.set_xticklabels([str(y) for y in years], color="#f3f4f6", fontsize=10)
    ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.0f}%"))
    ax3.legend(loc="upper right", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)
    ax3.set_ylabel("Annual Return (%)", color="#9ca3af", fontsize=10)

    # Panel 4: Risk-Adjusted Calmar & Sharpe Metrics
    ax4 = axes[1, 1]
    ax4.set_title("Calmar Ratio (CAGR / MDD) & Sharpe Ratio Comparison", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)

    var_names = list(results.keys())
    calmars = [results[v]["summary"]["calmar_ratio"] for v in var_names]
    sharpes = [results[v]["summary"]["annualized_sharpe"] for v in var_names]
    bar_colors = [colors.get(v, "#ffffff") for v in var_names]

    x = np.arange(len(var_names))
    w = 0.35
    b1 = ax4.bar(x - w/2, calmars, width=w, label="Calmar Ratio (防守性收益比)", color=bar_colors, alpha=0.9)
    b2 = ax4.bar(x + w/2, sharpes, width=w, label="Sharpe Ratio (夏普比率)", color="#e5e7eb", alpha=0.45)

    for bar in b1:
        h = bar.get_height()
        ax4.text(bar.get_x() + bar.get_width()/2., h + 0.2, f"{h:.1f}", ha="center", va="bottom", color="#f3f4f6", fontsize=8.5, fontweight="bold")
    for bar in b2:
        h = bar.get_height()
        ax4.text(bar.get_x() + bar.get_width()/2., h + 0.2, f"{h:.2f}", ha="center", va="bottom", color="#9ca3af", fontsize=8.0)

    ax4.set_xticks(x)
    ax4.set_xticklabels(var_names, color="#f3f4f6", fontsize=9.5)
    ax4.legend(loc="upper left", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)
    ax4.set_ylabel("Ratio Value", color="#9ca3af", fontsize=10)

    plt.tight_layout(pad=3.0)
    plt.savefig(output_png_path, dpi=150, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close()
    print(f"Saved comparative chart to {output_png_path}")

    # Mirror to artifact directory
    artifact_dir = os.getenv("ARTIFACT_DIR", "")
    if os.path.exists(artifact_dir):
        dest_path = os.path.join(artifact_dir, os.path.basename(output_png_path))
        shutil.copyfile(output_png_path, dest_path)
        print(f"Chart mirrored to artifact directory: {dest_path}")


def main():
    print("=" * 80)
    print("CME MES Dual Champion Fusion Suite (2020-2024)")
    print("Evaluating v5.8-VWAP against v4.3, v5.4, and v5.8 across 5-Year Continuous Backtest")
    print("=" * 80)

    # 1. Generate Dataset (seed=42)
    print("\n[Step 1/4] Generating 1,300 business days of continuous MES 5m & 1H data (seed=42)...")
    t0 = time.time()
    df_5m_raw, df_1h_raw = generate_synthetic_mes_data(
        start_date="2020-01-02",
        days=1300,
        seed=42
    )
    print(f"Data generated in {time.time() - t0:.2f}s: 5m bars={len(df_5m_raw):,}, 1H bars={len(df_1h_raw):,}")

    # 2. Precompute Indicators
    print("\n[Step 2/4] Precomputing indicators (VWAP, RSI, MACD, BB Width, 4H EMA50/Slope)...")
    t1 = time.time()
    df_5m_proc, df_1h_proc, df_4h_proc = precompute_v5_market_data(df_5m_raw, df_1h_raw)
    print(f"Indicators computed in {time.time() - t1:.2f}s: 4H bars={len(df_4h_proc):,}")

    # 3. Parallel Execution of Candidates
    variants = [
        "v4.3",          # Baseline Control
        "v5.4",          # VWAP Alone
        "v5.8",          # Asymmetric Tranches Alone
        "v5.8-VWAP",     # Dual Champion Fusion
        "v5.8-VWAP-4H"   # Triple Shield Fusion
    ]

    print(f"\n[Step 3/4] Launching parallel backtests for {len(variants)} variants (max_workers=5)...")
    t2 = time.time()
    results = {}

    with ProcessPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(run_single_v5_backtest, var, df_5m_proc, df_1h_proc, df_4h_proc): var
            for var in variants
        }
        for future in futures:
            var = futures[future]
            try:
                res = future.result()
                # Compute annual breakdown
                res["annual"] = compute_annual_breakdown(var, res["daily_df"], res["trades_df"])
                results[var] = res
            except Exception as e:
                print(f"Error in variant {var}: {e}")
                import traceback
                traceback.print_exc()

    print(f"All {len(variants)} variants completed in {time.time() - t2:.2f}s.")

    # 4. Export Institutional Results
    print("\n[Step 4/4] Exporting Institutional Results & Generating Scorecard...")
    summary_list = [results[v]["summary"] for v in variants if v in results]
    json_path = os.path.join(WORKSPACE_DIR, "backtest_v5_dual_champion_summary.json")
    csv_path = os.path.join(WORKSPACE_DIR, "backtest_v5_dual_champion_summary.csv")
    annual_csv_path = os.path.join(WORKSPACE_DIR, "backtest_v5_dual_champion_annual.csv")
    chart_png_path = os.path.join(WORKSPACE_DIR, "backtest_v5_dual_champion_chart.png")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary_list, f, indent=2)
    print(f"Exported: {json_path}")

    # Build Overall Summary CSV
    base_mdd = results["v4.3"]["summary"]["max_drawdown_pct"]
    base_calmar = results["v4.3"]["summary"]["calmar_ratio"]

    summary_rows = []
    for s in summary_list:
        v = s["variant"]
        mdd = s["max_drawdown_pct"]
        calmar = s["calmar_ratio"]
        mdd_imp = base_mdd - mdd
        calmar_lift = calmar - base_calmar
        summary_rows.append({
            "策略版本": v,
            "最終淨值 ($)": round(s["final_equity"], 2),
            "總回報率 (%)": round(s["total_return_pct"], 2),
            "年化CAGR (%)": round(s["cagr_pct"], 2),
            "最大回撤 MDD (%)": round(mdd, 2),
            "回撤改善 (%)": round(mdd_imp, 2),
            "夏普比率 (Sharpe)": round(s["annualized_sharpe"], 2),
            "索提諾 (Sortino)": round(s["annualized_sortino"], 2),
            "卡瑪比率 (Calmar)": round(calmar, 2),
            "卡瑪提升": round(calmar_lift, 2),
            "總成交筆數": s["total_tranches"],
            "勝率 (%)": round(s["win_rate_pct"], 2),
            "獲利因子 (PF)": round(s["profit_factor"], 2),
            "盈虧比 (Payoff)": round(s["payoff_ratio"], 2),
            "最大連續虧損": s["max_consecutive_losses"]
        })

    pd.DataFrame(summary_rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"Exported: {csv_path}")

    # Build Annual Breakdown CSV
    all_annual = []
    for v in variants:
        if v in results:
            for row in results[v]["annual"]:
                all_annual.append({
                    "策略版本": row["variant"],
                    "年份": row["year"],
                    "期初淨值 ($)": round(row["start_equity"], 2),
                    "期末淨值 ($)": round(row["end_equity"], 2),
                    "年度淨利 ($)": round(row["net_pnl"], 2),
                    "年度回報率 (%)": round(row["return_pct"], 2),
                    "年度最大回撤 (%)": round(row["max_dd_pct"], 2),
                    "夏普比率": round(row["sharpe"], 2),
                    "成交筆數": row["trade_count"],
                    "勝率 (%)": round(row["win_rate_pct"], 2),
                    "獲利因子": round(row["profit_factor"], 2)
                })

    pd.DataFrame(all_annual).to_csv(annual_csv_path, index=False, encoding="utf-8-sig")
    print(f"Exported Annual Breakdown: {annual_csv_path}")

    # Generate Chart
    generate_dual_champion_chart(results, chart_png_path)

    # Print Executive Terminal Scorecard
    print("\n" + "=" * 80)
    print("V5 DUAL CHAMPION FUSION EXECUTIVE SCORECARD:")
    print("=" * 80)
    scorecard_df = pd.DataFrame(summary_rows)
    print(scorecard_df.to_string(index=False))

    print("\n" + "=" * 80)
    print("ANNUAL BREAKDOWN BY YEAR (逐年回測對比 2020-2024):")
    print("=" * 80)
    annual_df = pd.DataFrame(all_annual)
    print(annual_df.to_string(index=False))


if __name__ == "__main__":
    main()
