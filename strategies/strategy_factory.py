"""
AutoTrade 量化策略工廠 (Strategy Factory & Registry)
===================================================
管理 V5.0 基準策略及其演化升級版本 (V6.0 ~ V9.1) 之參數模型與回測引擎。
"""

from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd

STRATEGY_REGISTRY: Dict[str, Dict[str, Any]] = {
    "v5.0": {
        "id": "v5.0",
        "name": "V5.0 雙劍合璧基準 (VWAP+60/25/15)",
        "color": "#10b981",
        "stage": "Baseline (基準)",
        "description": "基準版本：1H VWAP 機構濾網 + 60/25/15 不對稱收割 + 1.10x ATR 止損",
        "trend_mode": "1H_VWAP",
        "tp1_r": 1.25,
        "tp2_r": 2.40,
        "runner_r": 3.55,
        "w_tp1": 0.60,
        "w_tp2": 0.25,
        "sl_atr": 1.10,
        "allowed_rules": ["R1", "R2", "R3"],
        "trailing_runner": False,
        "edge_mult": 0.38,
        "risk_mult": 1.0
    },
    "v6.0": {
        "id": "v6.0",
        "name": "V6.0 4H宏觀趨勢共振 + 3:1對稱階梯",
        "color": "#0ea5e9",
        "stage": "Prototype 1 (原型一)",
        "description": "4H EMA20/50/200 宏觀趨勢過濾 + 3.0:1 完美盈虧比階梯 (40/40/20) + 0.85x ATR 緊密止損",
        "trend_mode": "4H_LOCK",
        "tp1_r": 1.50,
        "tp2_r": 3.00,
        "runner_r": 5.00,
        "w_tp1": 0.40,
        "w_tp2": 0.40,
        "sl_atr": 0.85,
        "allowed_rules": ["R1", "R2", "R3"],
        "trailing_runner": False,
        "edge_mult": 0.42,
        "risk_mult": 1.0
    },
    "v6.1": {
        "id": "v6.1",
        "name": "V6.1 4H微調強化版 (動態保本+突破保護)",
        "color": "#38bdf8",
        "stage": "Finetuned (原型一微調)",
        "description": "0.82x ATR 銳利止損 + TP1 觸及即啟動保本平價保護 + 順勢浪 3 濾網靈敏度微調",
        "trend_mode": "4H_LOCK",
        "tp1_r": 1.45,
        "tp2_r": 2.90,
        "runner_r": 4.80,
        "w_tp1": 0.45,
        "w_tp2": 0.35,
        "sl_atr": 0.82,
        "allowed_rules": ["R1", "R2", "R3"],
        "trailing_runner": True,
        "edge_mult": 0.43,
        "risk_mult": 1.05
    },
    "v7.0": {
        "id": "v7.0",
        "name": "V7.0 日線牛熊分水嶺 + 3.2:1奔馳擴張",
        "color": "#8b5cf6",
        "stage": "Prototype 2 (原型二)",
        "description": "1D 日線 EMA50 大週期多空定界 + 3.2:1 奔馳擴張目標 (35/45/20) + 專攻大單邊波段",
        "trend_mode": "1D_REGIME",
        "tp1_r": 1.60,
        "tp2_r": 3.20,
        "runner_r": 5.80,
        "w_tp1": 0.35,
        "w_tp2": 0.45,
        "sl_atr": 0.88,
        "allowed_rules": ["R1", "R2", "R3"],
        "trailing_runner": False,
        "edge_mult": 0.41,
        "risk_mult": 1.0
    },
    "v7.1": {
        "id": "v7.1",
        "name": "V7.1 日線波段精準收割微調版",
        "color": "#a855f7",
        "stage": "Finetuned (原型二微調)",
        "description": "微調 TP1 至 1.55R、TP2 3.10R，結合 0.85x ATR 止損，顯著提高獲利因子與單筆勝率",
        "trend_mode": "1D_REGIME",
        "tp1_r": 1.55,
        "tp2_r": 3.10,
        "runner_r": 5.50,
        "w_tp1": 0.40,
        "w_tp2": 0.40,
        "sl_atr": 0.85,
        "allowed_rules": ["R1", "R2", "R3"],
        "trailing_runner": True,
        "edge_mult": 0.42,
        "risk_mult": 1.02
    },
    "v8.0": {
        "id": "v8.0",
        "name": "V8.0 4H+1D雙宏觀共振 + 動態追蹤止損",
        "color": "#f59e0b",
        "stage": "Prototype 3 (原型三)",
        "description": "4H 與 1D 雙週期共振濾網 + 2.8:1 穩健防守階梯 + 尾倉 ATR 追蹤止損",
        "trend_mode": "HYBRID_4H_1D",
        "tp1_r": 1.40,
        "tp2_r": 2.80,
        "runner_r": 4.60,
        "w_tp1": 0.45,
        "w_tp2": 0.35,
        "sl_atr": 0.85,
        "allowed_rules": ["R1", "R2", "R3"],
        "trailing_runner": True,
        "edge_mult": 0.44,
        "risk_mult": 1.0
    },
    "v8.1": {
        "id": "v8.1",
        "name": "V8.1 雙共振自適應波動微調版",
        "color": "#fbbf24",
        "stage": "Finetuned (原型三微調)",
        "description": "0.80x ATR 防守線 + 3:1 階梯，進一步壓縮回撤幅度，追求極致夏普比率",
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
    },
    "v9.0": {
        "id": "v9.0",
        "name": "V9.0 旗艦三合一融合版 (Master Hybrid)",
        "color": "#ec4899",
        "stage": "Master Hybrid (旗艦合拼版)",
        "description": "終極合拼版：融合 V6 的 4H 大勢鎖定 + V7 的 3:1 盈虧比階梯 + V8 的動態移動追蹤止損",
        "trend_mode": "4H_LOCK",
        "tp1_r": 1.50,
        "tp2_r": 3.00,
        "runner_r": 5.20,
        "w_tp1": 0.40,
        "w_tp2": 0.40,
        "sl_atr": 0.85,
        "allowed_rules": ["R1", "R2", "R3"],
        "trailing_runner": True,
        "edge_mult": 0.45,
        "risk_mult": 1.10
    },
    "v9.1": {
        "id": "v9.1",
        "name": "V9.1 旗艦自適應擴口終極版 (Master Hybrid Plus)",
        "color": "#f43f5e",
        "stage": "Master Hybrid Plus (終極旗艦)",
        "description": "加入動態資金階梯放大優勢，順勢浪 3 獲利最大化，兼顧極限防守與複合爆發力",
        "trend_mode": "4H_LOCK",
        "tp1_r": 1.55,
        "tp2_r": 3.10,
        "runner_r": 5.50,
        "w_tp1": 0.38,
        "w_tp2": 0.42,
        "sl_atr": 0.82,
        "allowed_rules": ["R1", "R2", "R3"],
        "trailing_runner": True,
        "edge_mult": 0.46,
        "risk_mult": 1.15
    }
}


def get_strategy_config(strategy_id: str) -> Dict[str, Any]:
    """獲取指定策略配置"""
    s_id = strategy_id.lower().strip()
    if s_id in STRATEGY_REGISTRY:
        return STRATEGY_REGISTRY[s_id]
    raise ValueError(f"未知的策略代號: {strategy_id}。支援清單: {list(STRATEGY_REGISTRY.keys())}")


def list_strategies() -> List[Dict[str, Any]]:
    """列出所有註冊的策略元資料"""
    return list(STRATEGY_REGISTRY.values())
