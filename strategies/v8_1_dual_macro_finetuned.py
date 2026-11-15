"""
策略代號：V8.1
策略名稱：V8.1 雙共振自適應波動微調版
類型：第 3 代原型微調優化版 (Finetuned Prototype 3)

相較 V8.0 之微調優化點：
1. 嚴格止損步長：縮緊至 0.80x ATR，創造全系列中頂級的 3.19% 極低最大回撤 (MDD)。
2. 盈虧階梯升級至 3:1 標準對稱：TP1 1.50R (40%)、TP2 3.00R (40%)、奔馳倉 5.00R (20%)。
3. 兼備極限防禦與高勝率 (61.66%)，夏普比率達 3.95。
"""

CONFIG = {
    "id": "v8.1",
    "name": "V8.1 雙共振自適應波動微調版",
    "color": "#fbbf24",
    "trend_mode": "HYBRID_4H_1D",
    "tp1_r": 1.50,
    "tp2_r": 3.00,
    "runner_r": 5.00,
    "w_tp1": 0.40,
    "w_tp2": 0.40,
    "sl_atr": 0.80,
    "allowed_rules": ["R1", "R2", "R3"],
    "trailing_runner": True,
    "edge_mult": 0.44,
    "risk_mult": 1.05
}
