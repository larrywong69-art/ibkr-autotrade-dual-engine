#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略代號：V9.3-Opt (v2)
策略名稱：V9.3-Opt 旗艦雙引擎實盤優化版 (v2)
來源：FOR TEST/live_ibkr_dual_engine_v2.py

核心規格：
1. 5:5 均衡資金池配置 (MES 50% : MNQ 50%)
2. 單筆硬封頂 4 口 (MAX_CONTRACTS_PER_ORDER = 4)
3. 高爆發力分梯配置：
   - 4口: (1, 1, 2) -> 50% 倉位留作 Runner，全力捕捉 5.50R+ 大單邊暴賺
   - 3口: (1, 1, 1)
   - 2口: (1, 0, 1)
   - 1口: (0, 0, 1) (掛 8.50R 天花板並由動態保本/追蹤全程鎖利)
4. 放寬止蝕下限 (防秒斬)：
   - 美股黃金窗口 (22:30–04:00 HKT / 09:30–15:00 ET): MES 7.0 pts / MNQ 28.0 pts
   - 非黃金窗口: MES 5.5 pts / MNQ 22.0 pts
5. 保本覆蓋佣金與滑價 (BE_BUFFER_PTS):
   - MES +0.75 pts / MNQ +1.50 pts (TP1 後移至此保本價)
6. 有效止盈重設連輸風控 (CONSEC_LOSS_RESET_MIN_PNL_USD):
   - 單筆實質獲利 >= $30 USD 才重置連輸計數，平倉微賺維持連輸計數防過早放大注碼
"""

from typing import Dict, Any, Tuple, Optional
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd

try:
    from strategies.v9_3_master_hybrid_live import is_us_golden_window_hkt, detect_market_session, _normalize_direction
except ImportError:
    try:
        from v9_3_master_hybrid_live import is_us_golden_window_hkt, detect_market_session, _normalize_direction
    except ImportError:
        def is_us_golden_window_hkt(dt: Any = None) -> bool:
            if dt is None:
                dt = datetime.now()
            elif isinstance(dt, (int, float)):
                dt = datetime.fromtimestamp(dt)
            elif isinstance(dt, str):
                dt = pd.to_datetime(dt)
            h = getattr(dt, "hour", 0)
            m = getattr(dt, "minute", 0)
            hm = h * 60 + m
            return (hm >= 1350 or hm < 240)

        def detect_market_session(dt: Any) -> str:
            if dt is None:
                return "美盤"
            h = getattr(dt, "hour", 9)
            if 9 <= h < 16:
                return "美盤"
            elif 2 <= h < 9:
                return "歐盤"
            else:
                return "亞盤"

        def _normalize_direction(direction: Any) -> str:
            s = str(direction).upper().strip()
            return "LONG" if s in ("LONG", "DIRECTION.LONG", "1", "BOT", "BUY") else "SHORT"

CONFIG: Dict[str, Any] = {
    "id": "v9.3-opt-v2",
    "name": "V9.3-Opt 旗艦雙引擎實盤優化版 (v2)",
    "color": "#8b5cf6",
    "stage": "🚀 實盤優化版 (v2)",
    "description": "5:5 資金雙核 + 單筆硬封頂 4 口 (1/1/2 梯次，50% Runner 奔馳 5.5R+) + 黃金窗口放寬止損 (MES 7.0 / MNQ 28.0) + 保本覆蓋佣金滑價 (+0.75 / +1.50) + 有效止盈 (>= $30) 重設連輸",
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
    "is_integer_lot_mode": True,
    "max_contracts_per_order": 4,
    "consec_loss_reset_min_pnl_usd": 30.0,
    "min_sl_golden": {"MES": 7.0, "MNQ": 28.0},
    "min_sl_offpeak": {"MES": 5.5, "MNQ": 22.0},
    "be_buffer_pts": {"MES": 0.75, "MNQ": 1.50},
    "lot_split_map": {
        1: (0, 0, 1),
        2: (1, 0, 1),
        3: (1, 1, 1),
        4: (1, 1, 2)
    }
}


def allocate_runner_tranches(qty: int) -> Tuple[int, int, int]:
    """
    高爆發力分梯配置 (TP1_1.55R, TP2_3.10R, RUNNER_8.50R):
    - 4 口: (1, 1, 2) -> 50% 倉位留作 Runner，全力捕捉 5.50R+ 大單邊暴賺
    - 3 口: (1, 1, 1) -> 三梯全配齊
    - 2 口: (1, 0, 1) -> 1 口鎖利觸發保本 + 1 口 Runner 保留大單邊爆發力
    - 1 口: (0, 0, 1) -> 掛 8.50R 天花板並由動態保本(1.55R)/追蹤(3.10R/5.50R)全程鎖利
    """
    q = max(0, int(qty))
    if q == 0:
        return (0, 0, 0)
    if q == 1:
        return (0, 0, 1)
    if q == 2:
        return (1, 0, 1)
    if q == 3:
        return (1, 1, 1)
    if q == 4:
        return (1, 1, 2)
    cnt_a = max(1, int(round(q * 0.25)))
    cnt_b = max(1, int(round(q * 0.25)))
    cnt_c = max(1, q - cnt_a - cnt_b)
    return (cnt_a, cnt_b, cnt_c)


def get_min_sl_dist(symbol: str, is_golden_window: bool) -> float:
    """取得放寬後的止蝕距離下限"""
    table = CONFIG["min_sl_golden"] if is_golden_window else CONFIG["min_sl_offpeak"]
    clean_sym = symbol.replace("CME_", "").upper()
    return float(table.get(clean_sym, 28.0 if clean_sym in ("MNQ", "NQ") else 7.0))


def get_v2_order_lots(
    rule_type: str,
    direction: Any,
    session: Optional[str] = None,
    current_equity: float = 30000.0,
    margin_per_contract: float = 2400.0,
    max_contracts_cap: int = 4,
    timestamp: Any = None,
    consecutive_losses: int = 0
) -> Tuple[int, Tuple[int, int, int]]:
    """
    V9.3-Opt (v2) 下單口數與拆單分梯計算：
    - 單筆硬封頂 4 口
    - 美股黃金窗口 (22:30–04:00 HKT) 打滿，非黃金窗口注碼減半
    - 連輸 >= 3 次注碼減半，連輸 >= 5 次限制 1 張
    """
    dir_str = _normalize_direction(direction)
    if not session and timestamp is not None:
        session = detect_market_session(timestamp)
    session_str = str(session or "美盤")

    if rule_type in ("R1", "R1_WAVE3_TREND"):
        if current_equity < 45000:
            total_lots = 2
        elif current_equity < 75000:
            total_lots = 5
        elif current_equity < 150000:
            total_lots = 7
        else:
            total_lots = 9

        is_europe = ("歐盤" in session_str or "EUROPE" in session_str.upper())
        is_asia = ("亞盤" in session_str or "ASIA" in session_str.upper())
        if (dir_str == "LONG" and is_europe) or (dir_str == "SHORT" and is_asia):
            total_lots = max(2, total_lots // 2)

    elif rule_type in ("R2", "R2_RANGE_BOUNCE"):
        if current_equity < 60000:
            total_lots = 1
        elif current_equity < 140000:
            total_lots = 2
        else:
            total_lots = 3
    else:
        return 0, (0, 0, 0)

    in_golden_window = is_us_golden_window_hkt(timestamp)
    if not in_golden_window and total_lots > 1:
        total_lots = max(1, total_lots // 2)

    if consecutive_losses >= 5:
        total_lots = 1
    elif consecutive_losses >= 3:
        if total_lots > 1:
            total_lots = max(1, total_lots // 2)

    # 單筆硬封頂 4 口限制 (先於保證金校驗)
    hard_cap = min(int(max_contracts_cap), int(CONFIG.get("max_contracts_per_order", 4)))
    max_by_margin = max(1, int((current_equity * 0.65) // max(500.0, margin_per_contract)))
    allowed_lots = max(1, min(total_lots, max_by_margin, hard_cap))

    split = allocate_runner_tranches(allowed_lots)
    return allowed_lots, split
