"""
Generate clean, publication-grade 4-panel comparison chart from 5-year backtest data.
"""
import os
import json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# Configure fonts safely
plt.rcParams['font.family'] = 'sans-serif'
available_fonts = [f.name for f in fm.fontManager.ttflist]
for cand in ['PingFang HK', 'PingFang TC', 'Heiti TC', 'Arial Unicode MS', 'DejaVu Sans']:
    if cand in available_fonts:
        plt.rcParams['font.sans-serif'] = [cand, 'DejaVu Sans', 'Arial']
        break
plt.rcParams['axes.unicode_minus'] = False

workspace_dir = os.path.dirname(os.path.abspath(__file__))
artifact_dir = os.getenv("ARTIFACT_DIR", "")

summary_path = os.path.join(workspace_dir, "backtest_5yr_summary.json")
daily_path = os.path.join(workspace_dir, "backtest_5yr_equity_daily.csv")

with open(summary_path, "r", encoding="utf-8") as f:
    summaries = json.load(f)

daily_df = pd.read_csv(daily_path, index_col=0, parse_dates=True)

# Build results dict
results = {s["variant"]: s for s in summaries}

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
    "v4.0": "v4.0 (Pure W3 / Market)",
    "v4.1": "v4.1 (Cap 1 R2R3 / Market)",
    "v4.2": "v4.2 (Pure W3 / Limit LMT)",
    "v4.3": "v4.3 (Cap 1 R2R3 / Limit LMT)"
}

for ax in axes.flat:
    ax.set_facecolor("#111827")
    ax.tick_params(colors="#9ca3af", labelsize=9)
    for spine in ax.spines.values():
        spine.set_color("#374151")
    ax.grid(True, linestyle="--", alpha=0.25, color="#4b5563")

# Panel 1: Continuous 5-Year Equity Curves
ax1 = axes[0, 0]
ax1.set_title("5-Year Continuous Compounding Equity ($30,000 -> $16M+)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
ax1.axhline(30000, color="#6b7280", linestyle=":", linewidth=1.2, label="Initial Capital ($30k)")
ax1.axhline(45000, color="#22c55e", linestyle="--", alpha=0.5, linewidth=1.0, label="Tier 2 Gate ($45k)")
ax1.axhline(75000, color="#eab308", linestyle="--", alpha=0.5, linewidth=1.0, label="Tier 3 Gate ($75k)")

for var in ["v4.0", "v4.1", "v4.2", "v4.3"]:
    col = f"{var}_equity"
    if col in daily_df.columns:
        ax1.plot(daily_df.index, daily_df[col], label=labels[var], color=colors[var], linewidth=2.0)

ax1.set_ylabel("Account Equity (USD)", color="#d1d5db", fontsize=10)
ax1.legend(loc="upper left", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)
ax1.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))

# Panel 2: Underwater Drawdown Curves
ax2 = axes[0, 1]
ax2.set_title("Underwater Drawdown (%) [Max DD: 23.9% ~ 26.5%]", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
ax2.axhline(-5.0, color="#ef4444", linestyle=":", alpha=0.7, linewidth=1.0, label="Daily Breaker (-5%)")

for var in ["v4.0", "v4.1", "v4.2", "v4.3"]:
    col = f"{var}_equity"
    if col in daily_df.columns:
        s = daily_df[col]
        rolling_pk = s.cummax()
        dd_pct = ((s - rolling_pk) / rolling_pk) * 100.0
        ax2.plot(daily_df.index, dd_pct, label=labels[var], color=colors[var], linewidth=1.6, alpha=0.85)

ax2.set_ylabel("Drawdown (%)", color="#d1d5db", fontsize=10)
ax2.legend(loc="lower left", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)
ax2.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("{x:.1f}%"))

# Panel 3: Annual Return Comparison Bar Chart
ax3 = axes[1, 0]
ax3.set_title("Annual Returns (%) by Strategy (2020 - 2024)", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)
years = ["2020", "2021", "2022", "2023", "2024"]
x = np.arange(len(years))
width = 0.18
offsets = [-1.5 * width, -0.5 * width, 0.5 * width, 1.5 * width]

for idx, var in enumerate(["v4.0", "v4.1", "v4.2", "v4.3"]):
    annual_data = results[var]["annual"]
    returns = [annual_data[yr]["return_pct"] for yr in years]
    ax3.bar(x + offsets[idx], returns, width, label=labels[var], color=colors[var], alpha=0.9)

ax3.set_xticks(x)
ax3.set_xticklabels(years, color="#d1d5db", fontsize=10)
ax3.set_ylabel("Annual Return (%)", color="#d1d5db", fontsize=10)
ax3.axhline(0, color="#4b5563", linewidth=1.0)
ax3.legend(loc="upper right", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)
ax3.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("{x:.1f}%"))

# Panel 4: Key Performance Radar / Bar Chart Comparison
ax4 = axes[1, 1]
ax4.set_title("Institutional Risk-Adjusted Scorecard", color="#f3f4f6", fontsize=12, fontweight="bold", pad=12)

metric_keys = ["CAGR (%)", "Max DD (%)", "Sharpe (x10)", "Calmar (x10)", "Win Rate (%)"]
x4 = np.arange(len(metric_keys))
w4 = 0.18

for idx, var in enumerate(["v4.0", "v4.1", "v4.2", "v4.3"]):
    s = results[var]
    vals = [
        s["cagr_pct"],
        s["max_drawdown_pct"],
        s["annualized_sharpe"] * 10.0,
        s["calmar_ratio"] * 10.0,
        s["win_rate_pct"]
    ]
    ax4.bar(x4 + offsets[idx], vals, w4, label=labels[var], color=colors[var], alpha=0.9)

ax4.set_xticks(x4)
ax4.set_xticklabels(metric_keys, color="#d1d5db", fontsize=9.5)
ax4.set_ylabel("Standardized Metric Value", color="#d1d5db", fontsize=10)
ax4.legend(loc="upper left", facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6", fontsize=8.5)

plt.tight_layout(pad=3.0)

out_chart_ws = os.path.join(workspace_dir, "backtest_5yr_comparison_chart.png")
fig.savefig(out_chart_ws, facecolor=fig.get_facecolor(), edgecolor="none")
plt.close(fig)

# Mirror to artifact dir
if os.path.exists(artifact_dir):
    out_chart_art = os.path.join(artifact_dir, "backtest_5yr_comparison_chart.png")
    import shutil
    shutil.copy(out_chart_ws, out_chart_art)
    print("Mirror complete:", out_chart_art)

print("Clean chart generated:", out_chart_ws)
