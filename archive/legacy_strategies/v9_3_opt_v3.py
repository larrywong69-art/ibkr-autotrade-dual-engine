#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略代號：V9.3-Opt (v3)
策略名稱：V9.3-Opt v3 結構反轉否決與熔斷旗艦版 (v3)
來源：FOR TEST/live_ibkr_dual_engine_v3.py

核心規格：
1. 承襲 V9.3-Opt (v2) 所有優化：
   - 5:5 資金雙核 + 單筆硬封頂 4 口 + (1, 1, 2) 50% Runner 分梯
   - 放寬止蝕下限 (黃金窗口 MES 7.0 / MNQ 28.0；非黃金 MES 5.5 / MNQ 22.0)
   - 保本覆蓋佣金滑價 (+0.75 / +1.50) + 有效止盈 (>= $30) 重設連輸
2. v3 獨家強化風控：
   (a) 1H 結構反轉否決閘門 (Anti-Squeeze Veto)：
       - SHORT: 同時滿足 1H EMA20 > EMA50 且 現價 > 1H EMA20 始否決做空（不誤殺死貓彈）
       - LONG: 同時滿足 1H EMA20 < EMA50 且 現價 < 1H EMA20 始否決做多
   (b) 分方向連輸 4 次冷卻 1 小時 (Directional Cooldown)：
       - MES/MNQ 及做多/做空互不干擾，同方向連輸 4 次冷卻 1 小時 (3600秒)
       - 提早解鎖：若現價穿越 1H EMA20 則全面解鎖恢復常態注碼
       - 若僅跌/升穿最後一根 1H K 棒極值 (Low/High)，提早解鎖並賦予 1 張輕倉試單權
   (c) 1H 極值解鎖輕倉試單權 (Probe Order)：
       - 1 張輕倉試單，可穿透 1H 動能否決閘門，提早捕捉高空/低多轉折
   (d) 單日戶口 4% 硬熔斷 (Daily 4% Circuit Breaker)：
       - 當日累計虧損達 -4.0% 淨值時，立即停止當日新開倉直至次日美東交易日
"""

from typing import Dict, Any, Tuple, Optional
from datetime import datetime
import pandas as pd

try:
    from strategies.v9_3_opt_v2 import (
        allocate_runner_tranches,
        get_min_sl_dist,
        get_v2_order_lots,
        is_us_golden_window_hkt,
        detect_market_session,
        _normalize_direction,
    )
except ImportError:
    from v9_3_opt_v2 import (
        allocate_runner_tranches,
        get_min_sl_dist,
        get_v2_order_lots,
        is_us_golden_window_hkt,
        detect_market_session,
        _normalize_direction,
    )

CONFIG: Dict[str, Any] = {
    "id": "v9.3-opt-v3",
    "name": "V9.3-Opt v3 結構反轉否決與熔斷旗艦版 (v3)",
    "color": "#ec4899",
    "stage": "🚀 結構反轉否決與熔斷版 (v3)",
    "description": "承襲 v2 全部優化 + 1H 結構反轉否決閘門 (EMA20>EMA50且現價>EMA20否決做空) + 分方向連輸 4 次冷卻 1 小時 (提早破 1H EMA20 解鎖 / 破 1H 極值解鎖 1 張試單) + 單日 4% 硬熔斷保護",
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
    "dir_consec_loss_lock_threshold": 4,
    "dir_cooldown_seconds": 3600.0,
    "daily_max_loss_pct": 0.04,
    "lot_split_map": {
        1: (0, 0, 1),
        2: (1, 0, 1),
        3: (1, 1, 1),
        4: (1, 1, 2)
    }
}


def check_1h_momentum_veto(
    direction: str,
    cur_price: float,
    ema20_1h: float,
    ema50_1h: float
) -> Tuple[bool, str]:
    """
    1H 結構反轉否決閘門 (Anti-Squeeze Veto)：
    - SHORT 否決：1H EMA20 > EMA50 (金叉) 且 現價 > 1H EMA20
    - LONG 否決：1H EMA20 < EMA50 (死叉) 且 現價 < 1H EMA20
    """
    if ema20_1h <= 0.0 or ema50_1h <= 0.0 or cur_price <= 0.0:
        return False, ""
    dir_str = "LONG" if str(direction).upper() in ("LONG", "1", "BOT", "BUY") else "SHORT"
    if dir_str == "SHORT":
        if (ema20_1h > ema50_1h) and (cur_price > ema20_1h):
            return True, f"1H金叉反轉否決做空 (1H EMA20 {ema20_1h:.2f} > EMA50 {ema50_1h:.2f} 且 現價 {cur_price:.2f} > EMA20)"
    elif dir_str == "LONG":
        if (ema20_1h < ema50_1h) and (cur_price < ema20_1h):
            return True, f"1H死叉反轉否決做多 (1H EMA20 {ema20_1h:.2f} < EMA50 {ema50_1h:.2f} 且 現價 {cur_price:.2f} < EMA20)"
    return False, ""


def check_directional_cooldown_unlock(
    direction: str,
    cur_price: float,
    ema20_1h: float,
    last_1h_low: float,
    last_1h_high: float
) -> Tuple[bool, bool, str]:
    """
    冷卻期間檢查提早解鎖：
    - 返回 (unlocked, probe_1_lot, reason)
    - via_ema20: 穿越 1H EMA20 -> 全面解鎖 (probe_1_lot=False)
    - via_extreme: 穿越前 1H 極值 -> 輕倉試單解鎖 (probe_1_lot=True)
    """
    dir_str = "LONG" if str(direction).upper() in ("LONG", "1", "BOT", "BUY") else "SHORT"
    via_ema20 = False
    via_extreme = False
    reasons = []

    if cur_price > 0.0:
        if dir_str == "SHORT":
            if ema20_1h > 0.0 and cur_price < ema20_1h:
                via_ema20 = True
                reasons.append(f"現價跌破 1H EMA20 ({ema20_1h:.2f})")
            if last_1h_low > 0.0 and cur_price < last_1h_low:
                via_extreme = True
                reasons.append(f"現價跌破前1H最低點 ({last_1h_low:.2f})")
        else:
            if ema20_1h > 0.0 and cur_price > ema20_1h:
                via_ema20 = True
                reasons.append(f"現價突破 1H EMA20 ({ema20_1h:.2f})")
            if last_1h_high > 0.0 and cur_price > last_1h_high:
                via_extreme = True
                reasons.append(f"現價突破前1H最高點 ({last_1h_high:.2f})")

    if not via_ema20 and not via_extreme:
        return False, False, "處於分方向連輸4次冷卻期中"

    probe_1_lot = bool(via_extreme and not via_ema20)
    return True, probe_1_lot, " / ".join(reasons)


def get_v3_order_lots(
    rule_type: str,
    direction: Any,
    session: Optional[str] = None,
    current_equity: float = 30000.0,
    margin_per_contract: float = 2400.0,
    max_contracts_cap: int = 4,
    timestamp: Any = None,
    consecutive_losses: int = 0,
    probe_1_lot: bool = False
) -> Tuple[int, Tuple[int, int, int]]:
    """
    V9.3-Opt (v3) 下單口數與分梯計算：
    - 若由 1H 極值觸發解鎖試單權 (probe_1_lot=True)，嚴格鎖定為 1 口 (0, 0, 1)
    - 其餘遵循 v2 邏輯 (4 口硬封頂、黃金窗口/連輸階梯、(1,1,2) 分梯)
    """
    if probe_1_lot:
        return 1, (0, 0, 1)
    return get_v2_order_lots(
        rule_type=rule_type,
        direction=direction,
        session=session,
        current_equity=current_equity,
        margin_per_contract=margin_per_contract,
        max_contracts_cap=max_contracts_cap,
        timestamp=timestamp,
        consecutive_losses=consecutive_losses
    )
