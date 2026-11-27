#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoTrade 量化策略工廠 (Strategy Factory & Registry)
===================================================
管理最新確認之核心策略版本：
1. V9.3: 旗艦雙引擎整數自適應實盤基準版 (Baseline Benchmark)
2. V9.9: 弱點優化終極冠軍版 (Live Takeover Champion, 50輪定向演化, 接管實盤)
3. V10.0: 旗艦雙引擎 100 輪演化終極冠軍版 (Grand Champion)
4. V10.0-Compound: 100 輪演化階梯複利高回報版 (Compound Growth Champion)
"""

from typing import Dict, Any, List

try:
    from strategies.v9_9 import CONFIG as V99_CONFIG
except ImportError:
    try:
        from v9_9 import CONFIG as V99_CONFIG
    except ImportError:
        V99_CONFIG = {
            "id": "v9.9",
            "name": "V9.9 弱點優化終極冠軍版 (CAGR 27% 旗艦雙引擎)",
            "color": "#f59e0b",
            "stage": "👑 終極優化冠軍版 (V9.9)",
            "description": "10年真實數據 50 輪弱點定向演化冠軍：動態階梯複利 (每$10k增1口/封頂8口) + 引擎強化 (R2 權重 1.25x / Runner 5.5R) + R1 趨勢過濾 (ADX>=22) + 亞盤避空 + 雙倍滑價保本",
            "trend_mode": "4H_LOCK",
            "tp1_r": 1.50,
            "tp2_r": 3.00,
            "runner_r": 5.50,
            "sl_atr": 0.80,
            "r2_sl_atr": 0.82,
            "r1_min_adx": 22.0,
            "r2_max_adx_filter": 30.0,
            "allowed_rules": ["R1", "R2"],
            "trailing_runner": True,
            "edge_mult": 0.0,
            "legacy_edge_mult": 0.46,
            "risk_mult": 1.15,
            "is_integer_lot_mode": True,
            "compounding_mode": True,
            "base_lots": 2,
            "equity_per_lot": 10000.0,
            "max_contracts_per_order": 8,
            "r2_lot_scale": 1.25,
            "skip_asia_short": True,
            "asia_lot_discount": 0.40,
            "consec_loss_reset_min_pnl_usd": 30.0,
            "min_sl_golden": {"MES": 7.0, "MNQ": 28.0},
            "min_sl_offpeak": {"MES": 5.5, "MNQ": 22.0},
            "be_buffer_pts": {"MES": 0.75, "MNQ": 1.50},
        }

try:
    from strategies.v10_0 import CONFIG as V10_CONFIG, CONFIG_COMPOUND as V10_COMPOUND_CONFIG
except ImportError:
    try:
        from v10_0 import CONFIG as V10_CONFIG, CONFIG_COMPOUND as V10_COMPOUND_CONFIG
    except ImportError:
        V10_CONFIG = dict(V99_CONFIG)
        V10_CONFIG.update({"id": "v10.0", "name": "V10.0 旗艦雙引擎 100 輪演化終極冠軍版 (Grand Champion)"})
        V10_COMPOUND_CONFIG = dict(V10_CONFIG)
        V10_COMPOUND_CONFIG.update({"id": "v10.0-compound", "name": "V10.0 階梯複利高回報演化版"})

# 核心策略配置庫：僅保留最新確認之核心版本
STRATEGY_REGISTRY: Dict[str, Dict[str, Any]] = {
    # 1. 實盤基準對照組 (Baseline)
    "v9.3": {
        "id": "v9.3",
        "name": "V9.3 旗艦雙引擎整數自適應實盤版 (Master Hybrid Live)",
        "color": "#e11d48",
        "stage": "📌 實盤基準對照 (V9.3)",
        "description": "全整數口三梯次拆單 + R2 隨淨值三階擴口 (1->2->3口) + 低效時段減半濾網 + 1H ADX>30 強趨勢保護",
        "trend_mode": "4H_LOCK",
        "tp1_r": 1.55,
        "tp2_r": 3.10,
        "runner_r": 5.50,
        "sl_atr": 0.82,
        "r2_sl_atr": 0.85,
        "allowed_rules": ["R1", "R2"],
        "trailing_runner": True,
        "edge_mult": 0.0,
        "legacy_edge_mult": 0.46,
        "risk_mult": 1.15,
        "r2_max_adx_filter": 30.0,
        "is_integer_lot_mode": True
    },
    "v9.3.13": {
        "id": "v9.3",
        "name": "V9.3 旗艦雙引擎整數自適應實盤版 (Master Hybrid Live)",
        "color": "#e11d48",
        "stage": "📌 實盤基準對照 (V9.3)",
        "description": "全整數口三梯次拆單 + R2 隨淨值三階擴口 (1->2->3口) + 低效時段減半濾網 + 1H ADX>30 強趨勢保護",
        "trend_mode": "4H_LOCK",
        "tp1_r": 1.55,
        "tp2_r": 3.10,
        "runner_r": 5.50,
        "sl_atr": 0.82,
        "r2_sl_atr": 0.85,
        "allowed_rules": ["R1", "R2"],
        "trailing_runner": True,
        "edge_mult": 0.0,
        "legacy_edge_mult": 0.46,
        "risk_mult": 1.15,
        "r2_max_adx_filter": 30.0,
        "is_integer_lot_mode": True
    },

    # 2. 弱點優化實盤接管冠軍版 (Live Takeover Champion)
    "v9.9": V99_CONFIG,
    "v9_9": V99_CONFIG,

    # 3. 100 輪演化終極冠軍版 (Grand Champion)
    "v10.0": V10_CONFIG,
    "v10_0": V10_CONFIG,
    "v10.0-champ": V10_CONFIG,

    # 4. 100 輪演化階梯複利版 (Compound Growth Champion)
    "v10.0-compound": V10_COMPOUND_CONFIG,
    "v10_0_compound": V10_COMPOUND_CONFIG,
}


def get_strategy_config(strategy_id: str) -> Dict[str, Any]:
    """獲取指定策略配置"""
    s_id = strategy_id.lower().strip()
    if s_id in STRATEGY_REGISTRY:
        return STRATEGY_REGISTRY[s_id]
    raise ValueError(f"未知的策略代號: {strategy_id}。支援清單: {list(STRATEGY_REGISTRY.keys())}")


def list_strategies() -> List[Dict[str, Any]]:
    """列出所有註冊的策略元資料（去重別名，嚴格按版本排序）"""
    seen = set()
    result = []
    # 按照優先順序輸出：v10.0, v9.9, v9.3, v10.0-compound
    ordered_keys = ["v10.0", "v9.9", "v9.3", "v10.0-compound"]
    for k in ordered_keys:
        if k in STRATEGY_REGISTRY:
            s = STRATEGY_REGISTRY[k]
            sid = s.get("id")
            if sid and sid not in seen:
                seen.add(sid)
                result.append(s)
    return result
