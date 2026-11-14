"""
CME MES Nine-in-One Quantitative Strategy 5-Year Continuous Backtest Suite (2020-2024).
Evaluates the 4 exported strategy variants:
- v4.0: Pure Wave 3 Market (Rules 2&3 Disabled, Market Orders)
- v4.1: Cap 1 R2/R3 Market (Rules 2&3 Capped at 1 Lot, Market Orders)
- v4.2: Pure Wave 3 Limit (Rules 2&3 Disabled, Limit Orders LMT)
- v4.3: Cap 1 R2/R3 Limit (Rules 2&3 Capped at 1 Lot, Limit Orders LMT)

Initial Capital: $30,000 USD
Continuous Compounding across 1,300 business days (~358,800 5m bars).
Dynamic Equity Scaling Tiers:
- Tier 1 ($0 ~ $45k): 2 / 3 / 5 contracts
- Tier 2 ($45k ~ $75k): 2 / 4 / 6 contracts
- Tier 3 (>= $75k): 4 / 8 / 12 contracts
"""

import os
import sys
import json
import time
import shutil
from typing import Dict, Any, List, Tuple
from concurrent.futures import ProcessPoolExecutor
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Ensure AutoTrade is in python path
AUTO_TRADE_PATH = os.path.dirname(os.path.abspath(__file__))
if AUTO_TRADE_PATH not in sys.path:
    sys.path.insert(0, AUTO_TRADE_PATH)

from mes_strategy.data_generator import generate_synthetic_mes_data
from mes_strategy.backtester import prepare_market_data
from backtest_v4_suite import AutoTradeV4Engine, build_v4_strategy_config


def run_single_backtest(
    variant: str,
    df_5m_proc: pd.DataFrame,
    df_1h_proc: pd.DataFrame
) -> Dict[str, Any]:
    """Execute historical continuous backtest for a single variant."""
    print(f"[{variant}] Starting 5-year continuous backtest...")
    start_time = time.time()
    
    engine = AutoTradeV4Engine(variant=variant)
    df_1h_indices = df_1h_proc.index
    h_pointer = 0
    current_trading_date = None

    timestamps = []
    equity_curve = []
    daily_equity_map = {}

    warmup = 60
    total_bars = len(df_5m_proc)

    for i in range(warmup, total_bars):
        curr_bar = df_5m_proc.iloc[i]
        t = curr_bar.name if isinstance(curr_bar.name, pd.Timestamp) else pd.to_datetime(curr_bar.name)
        bar_date = t.date()

        # Check daily settlement reset
        if bar_date != current_trading_date:
            if current_trading_date is not None:
                # Store end of previous day's equity
                daily_equity_map[current_trading_date] = engine.daily_risk.current_equity
            current_trading_date = bar_date
            engine.reset_daily_circuit_breaker(str(bar_date), engine.daily_risk.current_equity)

        # Align HTF 1H bar
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

        # Step engine
        engine.on_bar_close(df_5m_proc, i, htf_data)

        eq = engine.daily_risk.current_equity
        equity_curve.append(eq)
        timestamps.append(t)

    # Finalize at the end of backtest
    final_bar = df_5m_proc.iloc[-1]
    final_time = final_bar.name if isinstance(final_bar.name, pd.Timestamp) else pd.to_datetime(final_bar.name)
    engine.finalize_backtest(float(final_bar["close"]), final_time)
    if current_trading_date is not None:
        daily_equity_map[current_trading_date] = engine.daily_risk.current_equity

    elapsed = time.time() - start_time
    print(f"[{variant}] Completed in {elapsed:.1f}s. Final Equity: ${engine.realized_equity:,.2f}")

    # Build Trade DataFrame
    trades_df = pd.DataFrame(engine.trade_history)
    if not trades_df.empty:
        trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
        trades_df["exit_time"] = pd.to_datetime(trades_df["exit_time"])

    # Build Daily Equity DataFrame
    daily_dates = sorted(daily_equity_map.keys())
    daily_equities = [daily_equity_map[d] for d in daily_dates]
    df_daily = pd.DataFrame({"date": pd.to_datetime(daily_dates), "equity": daily_equities})
    df_daily.set_index("date", inplace=True)

    # Calculate Institutional Metrics
    initial_cap = 30000.0
    final_eq = engine.realized_equity
    net_profit = final_eq - initial_cap
    total_ret_pct = (net_profit / initial_cap) * 100.0

    # 5-Year CAGR
    cagr_pct = (((final_eq / initial_cap) ** (1.0 / 5.0)) - 1.0) * 100.0

    # Drawdown metrics based on daily equity
    rolling_max = df_daily["equity"].cummax()
    drawdowns = df_daily["equity"] - rolling_max
    drawdown_pcts = (drawdowns / rolling_max) * 100.0
    max_dd_dollar = abs(float(drawdowns.min())) if len(drawdowns) > 0 else 0.0
    max_dd_pct = abs(float(drawdown_pcts.min())) if len(drawdown_pcts) > 0 else 0.0

    # Daily Returns, Sharpe, Sortino
    df_daily["daily_ret"] = df_daily["equity"].pct_change().fillna(0.0)
    mean_daily_ret = df_daily["daily_ret"].mean()
    std_daily_ret = df_daily["daily_ret"].std()
    sharpe = (mean_daily_ret / std_daily_ret * np.sqrt(252.0)) if std_daily_ret > 1e-8 else 0.0

    downside_daily = df_daily["daily_ret"][df_daily["daily_ret"] < 0.0]
    downside_std = downside_daily.std() if len(downside_daily) > 1 else 1e-6
    sortino = (mean_daily_ret / downside_std * np.sqrt(252.0)) if downside_std > 1e-8 else 0.0

    calmar = (cagr_pct / max_dd_pct) if max_dd_pct > 0.01 else 0.0

    # Trade Metrics
    total_tranches = len(trades_df)
    unique_positions = trades_df["position_id"].nunique() if total_tranches > 0 else 0
    if total_tranches > 0:
        wins = trades_df[trades_df["pnl"] > 0]
        losses = trades_df[trades_df["pnl"] < 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total_tranches) * 100.0
        
        gross_profit = float(wins["pnl"].sum()) if win_count > 0 else 0.0
        gross_loss = abs(float(losses["pnl"].sum())) if loss_count > 0 else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

        avg_trade = float(trades_df["pnl"].mean())
        avg_win = float(wins["pnl"].mean()) if win_count > 0 else 0.0
        avg_loss = abs(float(losses["pnl"].mean())) if loss_count > 0 else 0.0
        payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0
        
        total_comm = float(trades_df["commission"].sum())

        # Max consecutive losses
        pnl_signs = (trades_df["pnl"] < 0).astype(int)
        loss_runs = pnl_signs.groupby((pnl_signs != pnl_signs.shift()).cumsum()).cumsum()
        max_consec_loss = int(loss_runs.max()) if len(loss_runs) > 0 else 0

        # Position level win rate
        pos_pnl = trades_df.groupby("position_id")["pnl"].sum()
        pos_win_rate = float((pos_pnl > 0).mean() * 100.0)
    else:
        win_rate = 0.0
        profit_factor = 0.0
        avg_trade = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        payoff_ratio = 0.0
        total_comm = 0.0
        max_consec_loss = 0
        pos_win_rate = 0.0

    # Rule Breakdown
    rule_metrics = {}
    for r_type, r_name in [
        ("TREND_PULLBACK_W3", "Rule 4 (順勢回調浪3)"),
        ("RANGE_BOUNCE", "Rule 2 (箱體邊界反彈)"),
        ("FALSE_BREAKOUT_REV", "Rule 3 (假突破反轉)")
    ]:
        if total_tranches > 0 and r_type in trades_df["signal_type"].values:
            sub = trades_df[trades_df["signal_type"] == r_type]
            r_cnt = len(sub)
            r_pnl = float(sub["pnl"].sum())
            r_win = float((sub["pnl"] > 0).mean() * 100.0)
            rule_metrics[r_type] = {
                "name": r_name,
                "trades": r_cnt,
                "net_pnl": r_pnl,
                "win_rate": r_win
            }
        else:
            rule_metrics[r_type] = {
                "name": r_name,
                "trades": 0,
                "net_pnl": 0.0,
                "win_rate": 0.0
            }

    # Annual Breakdown
    annual_breakdown = {}
    years = [2020, 2021, 2022, 2023, 2024]
    start_equity_year = initial_cap

    for yr in years:
        yr_str = str(yr)
        yr_daily = df_daily[df_daily.index.year == yr]
        if not yr_daily.empty:
            yr_end_eq = yr_daily["equity"].iloc[-1]
            yr_pnl = yr_end_eq - start_equity_year
            yr_ret = (yr_pnl / start_equity_year) * 100.0
            
            # Annual drawdown
            yr_rolling_max = yr_daily["equity"].cummax()
            yr_dd = ((yr_daily["equity"] - yr_rolling_max) / yr_rolling_max) * 100.0
            yr_max_dd = abs(float(yr_dd.min()))

            # Annual trades
            if total_tranches > 0:
                yr_trades = trades_df[trades_df["exit_time"].dt.year == yr]
                yr_trade_count = len(yr_trades)
                yr_win_rate = float((yr_trades["pnl"] > 0).mean() * 100.0) if yr_trade_count > 0 else 0.0
            else:
                yr_trade_count = 0
                yr_win_rate = 0.0

            annual_breakdown[yr_str] = {
                "start_equity": start_equity_year,
                "end_equity": yr_end_eq,
                "net_pnl": yr_pnl,
                "return_pct": yr_ret,
                "trades": yr_trade_count,
                "win_rate": yr_win_rate,
                "max_drawdown_pct": yr_max_dd
            }
            start_equity_year = yr_end_eq
        else:
            annual_breakdown[yr_str] = {
                "start_equity": start_equity_year,
                "end_equity": start_equity_year,
                "net_pnl": 0.0,
                "return_pct": 0.0,
                "trades": 0,
                "win_rate": 0.0,
                "max_drawdown_pct": 0.0
            }

    summary = {
        "variant": variant,
        "initial_capital": initial_cap,
        "final_equity": final_eq,
        "net_profit": net_profit,
        "total_return_pct": total_ret_pct,
        "cagr_pct": cagr_pct,
        "max_drawdown_dollar": max_dd_dollar,
        "max_drawdown_pct": max_dd_pct,
        "annualized_sharpe": sharpe,
        "annualized_sortino": sortino,
        "calmar_ratio": calmar,
        "total_positions": unique_positions,
        "total_tranches": total_tranches,
        "win_rate_pct": win_rate,
        "position_win_rate_pct": pos_win_rate,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff_ratio,
        "avg_trade_pnl": avg_trade,
        "avg_win_pnl": avg_win,
        "avg_loss_pnl": avg_loss,
        "max_consecutive_losses": max_consec_loss,
        "total_commission": total_comm,
        "limit_orders_placed": engine.limit_orders_placed,
        "limit_orders_filled": engine.limit_orders_filled,
        "limit_orders_expired": engine.limit_orders_expired,
        "limit_fill_rate_pct": (engine.limit_orders_filled / engine.limit_orders_placed * 100.0) if engine.limit_orders_placed > 0 else 0.0,
        "rules": rule_metrics,
        "annual": annual_breakdown,
        "elapsed_seconds": elapsed
    }

    return {
        "summary": summary,
        "trades_df": trades_df,
        "daily_df": df_daily
    }


def generate_comparative_visualizations(
    results: Dict[str, Dict[str, Any]],
    output_png_path: str
):
    """Generate high-resolution institutional 4-panel comparison chart."""
    import matplotlib.font_manager as fm
    available_fonts = [f.name for f in fm.fontManager.ttflist]
    for candidate in ["PingFang HK", "PingFang TC", "PingFang SC", "Heiti TC", "Arial Unicode MS"]:
        if candidate in available_fonts:
            plt.rcParams["font.sans-serif"] = [candidate, "DejaVu Sans", "Arial"]
            break
    plt.rcParams["axes.unicode_minus"] = False

    plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")
    fig, axes = plt.subplots(2, 2, figsize=(18, 12), dpi=300)
    fig.patch.set_facecolor("#0b0f19")

    colors = {
        "v4.0": "#3b82f6",  # Blue (Pure W3 Market)
        "v4.1": "#10b981",  # Emerald (Cap 1 R2R3 Market)
        "v4.2": "#f59e0b",  # Amber (Pure W3 Limit)
        "v4.3": "#8b5cf6"   # Purple (Cap 1 R2R3 Limit)
    }

    labels = {
        "v4.0": "v4.0 (純浪3 / 市價)",
        "v4.1": "v4.1 (R2R3限1口 / 市價)",
        "v4.2": "v4.2 (純浪3 / 限價LMT)",
        "v4.3": "v4.3 (R2R3限1口 / 限價LMT)"
    }

    for ax in axes.flat:
        ax.set_facecolor("#111827")
        ax.tick_params(colors="#9ca3af", labelsize=9)
        for spine in ax.spines.values():
            spine.set_color("#374151")
        ax.grid(True, linestyle="--", alpha=0.25, color="#4b5563")

    # Panel 1: Continuous 5-Year Equity Curves
    ax1 = axes[0, 0]
    ax1.set_title("連續 5 年複利淨值走勢 (Continuous 5-Yr Equity Compounding)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
    ax1.axhline(30000, color="#6b7280", linestyle=":", linewidth=1.2, label="起始資金 ($30,000)")
    ax1.axhline(45000, color="#22c55e", linestyle="--", alpha=0.5, linewidth=1.0, label="Tier 2 門檻 ($45,000)")
    ax1.axhline(75000, color="#eab308", linestyle="--", alpha=0.5, linewidth=1.0, label="Tier 3 門檻 ($75,000)")

    for var_name, res in results.items():
        daily_df = res["daily_df"]
        ax1.plot(daily_df.index, daily_df["equity"], label=labels[var_name], color=colors[var_name], linewidth=2.0)

    ax1.set_ylabel("戶口淨值 (USD)", color="#d1d5db", fontsize=10)
    ax1.legend(loc="upper left", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)
    ax1.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))

    # Panel 2: Underwater Drawdown Curves
    ax2 = axes[0, 1]
    ax2.set_title("水下回撤百分比 (Underwater Drawdown %)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
    ax2.axhline(-5.0, color="#ef4444", linestyle=":", alpha=0.7, linewidth=1.0, label="單日熔斷閾值 (-5%)")

    for var_name, res in results.items():
        daily_df = res["daily_df"]
        rolling_pk = daily_df["equity"].cummax()
        dd_pct = ((daily_df["equity"] - rolling_pk) / rolling_pk) * 100.0
        ax2.plot(daily_df.index, dd_pct, label=labels[var_name], color=colors[var_name], linewidth=1.6, alpha=0.85)

    ax2.set_ylabel("回撤百分比 (%)", color="#d1d5db", fontsize=10)
    ax2.legend(loc="lower left", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)
    ax2.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("{x:.1f}%"))

    # Panel 3: Annual Return Comparison Bar Chart
    ax3 = axes[1, 0]
    ax3.set_title("逐年回報率對比 (Annual Returns % 2020-2024)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
    years = ["2020", "2021", "2022", "2023", "2024"]
    x = np.arange(len(years))
    width = 0.18

    offsets = [-1.5 * width, -0.5 * width, 0.5 * width, 1.5 * width]
    for idx, (var_name, res) in enumerate(results.items()):
        annual_data = res["summary"]["annual"]
        returns = [annual_data[yr]["return_pct"] for yr in years]
        ax3.bar(x + offsets[idx], returns, width, label=labels[var_name], color=colors[var_name], alpha=0.9)

    ax3.set_xticks(x)
    ax3.set_xticklabels(years, color="#d1d5db", fontsize=10)
    ax3.set_ylabel("年度回報率 (%)", color="#d1d5db", fontsize=10)
    ax3.axhline(0, color="#4b5563", linewidth=1.0)
    ax3.legend(loc="upper right", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)
    ax3.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("{x:.1f}%"))

    # Panel 4: Key Performance Radar / Bar Chart Comparison
    ax4 = axes[1, 1]
    ax4.set_title("核心量化指標對照 (Institutional Risk-Adjusted Scorecard)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
    
    metric_keys = ["年化CAGR (%)", "最大回撤 (%)", "夏普比率 (Sharpe)", "卡瑪比率 (Calmar)", "勝率 (%)"]
    v_names = list(results.keys())
    x4 = np.arange(len(metric_keys))
    w4 = 0.18

    for idx, (var_name, res) in enumerate(results.items()):
        s = res["summary"]
        vals = [
            s["cagr_pct"],
            s["max_drawdown_pct"],
            s["annualized_sharpe"] * 10.0, # scaled for visibility
            s["calmar_ratio"] * 10.0,     # scaled for visibility
            s["win_rate_pct"]
        ]
        ax4.bar(x4 + offsets[idx], vals, w4, label=labels[var_name], color=colors[var_name], alpha=0.9)

    ax4.set_xticks(x4)
    ax4.set_xticklabels(["CAGR (%)", "最大回撤 (%)", "夏普 (x10)", "卡瑪 (x10)", "勝率 (%)"], color="#d1d5db", fontsize=9.5)
    ax4.set_ylabel("指標數值 (標準化)", color="#d1d5db", fontsize=10)
    ax4.legend(loc="upper left", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)

    plt.tight_layout(pad=3.0)
    plt.savefig(output_png_path, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close()
    print(f"Saved comparative chart to {output_png_path}")


def main():
    print("=" * 80)
    print("CME MES Nine-in-One v4 Strategy Continuous 5-Year Backtest (2020-2024)")
    print("=" * 80)

    # 1. Generate 1,300 business days of continuous MES 5m & 1H data
    print("\n[Step 1/4] Generating 1,300 business days of continuous multi-regime MES data...")
    t0 = time.time()
    df_5m_raw, df_1h_raw = generate_synthetic_mes_data(
        start_date="2020-01-02",
        days=1300,
        seed=42
    )
    print(f"Data generated in {time.time() - t0:.2f}s: 5m bars={len(df_5m_raw):,}, 1H bars={len(df_1h_raw):,}")

    # 2. Precompute indicators once (guarantees 100% data identity across all 4 versions)
    print("\n[Step 2/4] Precomputing indicators (Sortino, ATR, ADX, Slope)...")
    t1 = time.time()
    base_cfg = build_v4_strategy_config("v4.0")
    df_5m_proc, df_1h_proc = prepare_market_data(df_5m_raw, df_1h_raw, base_cfg)
    print(f"Indicators computed in {time.time() - t1:.2f}s.")

    # 3. Run all 4 variants in parallel
    variants = ["v4.0", "v4.1", "v4.2", "v4.3"]
    print(f"\n[Step 3/4] Launching parallel backtests for: {', '.join(variants)}...")
    t2 = time.time()

    results = {}
    with ProcessPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(run_single_backtest, var, df_5m_proc, df_1h_proc): var
            for var in variants
        }
        for future in futures:
            var = futures[future]
            try:
                res = future.result()
                results[var] = res
            except Exception as e:
                print(f"Error running backtest for {var}: {e}")
                raise e

    print(f"All 4 backtests completed in {time.time() - t2:.2f}s.")

    # 4. Save and Export Data
    print("\n[Step 4/4] Compiling institutional report and exporting data files...")
    workspace_dir = os.path.dirname(os.path.abspath(__file__))
    artifact_dir = os.getenv("ARTIFACT_DIR", "")

    # Export Summary CSV & JSON
    summaries = [results[v]["summary"] for v in variants]
    summary_df = pd.DataFrame([{
        "策略版本": s["variant"],
        "起始資金 ($)": s["initial_capital"],
        "最終淨值 ($)": s["final_equity"],
        "總純利 ($)": s["net_profit"],
        "總回報率 (%)": s["total_return_pct"],
        "5年年化CAGR (%)": s["cagr_pct"],
        "最大回撤 ($)": s["max_drawdown_dollar"],
        "最大回撤 (%)": s["max_drawdown_pct"],
        "年化夏普比率": s["annualized_sharpe"],
        "年化索提諾比率": s["annualized_sortino"],
        "卡瑪比率 (Calmar)": s["calmar_ratio"],
        "平倉批次 (Tranches)": s["total_tranches"],
        "獨立開倉 (Positions)": s["total_positions"],
        "勝率 (%)": s["win_rate_pct"],
        "獲利因子 (Profit Factor)": s["profit_factor"],
        "盈虧比 (Payoff Ratio)": s["payoff_ratio"],
        "平均單筆損益 ($)": s["avg_trade_pnl"],
        "最大連續虧損 (次)": s["max_consecutive_losses"],
        "佣金手續費總支出 ($)": s["total_commission"],
        "限價單掛單次數": s["limit_orders_placed"],
        "限價單成交次數": s["limit_orders_filled"],
        "限價單成交率 (%)": s["limit_fill_rate_pct"]
    } for s in summaries])
    
    summary_csv_path = os.path.join(workspace_dir, "backtest_5yr_summary.csv")
    summary_df.to_csv(summary_csv_path, index=False)
    print(f"Exported: {summary_csv_path}")

    summary_json_path = os.path.join(workspace_dir, "backtest_5yr_summary.json")
    with open(summary_json_path, "w", encoding="utf-8") as f:
        json.dump(summaries, f, indent=2, ensure_ascii=False)
    print(f"Exported: {summary_json_path}")

    # Export Annual Breakdown CSV
    annual_rows = []
    for s in summaries:
        var = s["variant"]
        for yr, y_data in s["annual"].items():
            annual_rows.append({
                "策略版本": var,
                "年份": yr,
                "年初資金 ($)": y_data["start_equity"],
                "年末淨值 ($)": y_data["end_equity"],
                "年度純利 ($)": y_data["net_pnl"],
                "年度回報率 (%)": y_data["return_pct"],
                "平倉批次": y_data["trades"],
                "勝率 (%)": y_data["win_rate"],
                "年內最大回撤 (%)": y_data["max_drawdown_pct"]
            })
    annual_df = pd.DataFrame(annual_rows)
    annual_csv_path = os.path.join(workspace_dir, "backtest_5yr_annual_breakdown.csv")
    annual_df.to_csv(annual_csv_path, index=False)
    print(f"Exported: {annual_csv_path}")

    # Export Rules Breakdown CSV
    rule_rows = []
    for s in summaries:
        var = s["variant"]
        for r_code, r_info in s["rules"].items():
            rule_rows.append({
                "策略版本": var,
                "規則代碼": r_code,
                "規則名稱": r_info["name"],
                "平倉批次": r_info["trades"],
                "淨損益 ($)": r_info["net_pnl"],
                "勝率 (%)": r_info["win_rate"]
            })
    rules_df = pd.DataFrame(rule_rows)
    rules_csv_path = os.path.join(workspace_dir, "backtest_5yr_rules_breakdown.csv")
    rules_df.to_csv(rules_csv_path, index=False)
    print(f"Exported: {rules_csv_path}")

    # Export Trade Logs for each variant
    for var in variants:
        t_path = os.path.join(workspace_dir, f"backtest_5yr_trades_{var}.csv")
        results[var]["trades_df"].to_csv(t_path, index=False)
        print(f"Exported Trade Log: {t_path}")

    # Export Aligned Daily Equity
    daily_combined = pd.DataFrame({
        f"{var}_equity": results[var]["daily_df"]["equity"]
        for var in variants
    })
    daily_csv_path = os.path.join(workspace_dir, "backtest_5yr_equity_daily.csv")
    daily_combined.to_csv(daily_csv_path)
    print(f"Exported Daily Equity: {daily_csv_path}")

    # Generate Chart
    chart_workspace_path = os.path.join(workspace_dir, "backtest_5yr_comparison_chart.png")
    generate_comparative_visualizations(results, chart_workspace_path)

    # Copy chart to artifact directory for rendering
    if os.path.exists(artifact_dir):
        chart_artifact_path = os.path.join(artifact_dir, "backtest_5yr_comparison_chart.png")
        shutil.copy(chart_workspace_path, chart_artifact_path)
        print(f"Chart mirrored to artifact directory: {chart_artifact_path}")

    print("\n" + "=" * 80)
    print("BACKTEST SUITE COMPLETED SUCCESSFULLY!")
    print("=" * 80)
    print("\nEXECUTIVE SCORECARD:")
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
