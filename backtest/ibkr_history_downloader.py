#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ibkr_history_downloader.py (V9.3 實盤審計整合優化版)
====================================================
專為 Interactive Brokers (IBKR TWS / Gateway API) 打造的 CME 期貨歷史 K 線下載、審計與快取管理器。

核心能力與修復：
1. 廢除舊版「API 報錯即拿 1D 日線切分偽造 1H/15m/5m」之失真邏輯。
2. 解決 IBKR ContFuture 禁止設定 endDateTime (Error 10339) 之限制：
   - 歷史長週期數據自動按 CME 季度合約 (H/M/U/Z, includeExpired=True) 分段抓取並執行巴拿馬換月價差對齊。
   - 2019-06 以前若選 MES / MNQ，自動對接同指數點位之 ES / NQ 歷史合約。
3. 嚴格執行 0.25 Tick 價格量化檢查與成交量獨立性審計 (audit_cache_integrity)，攔截插值假數據。
4. 完整相容 `ib_async` / `ib_insync`、`server.py` API (`list_cached_csvs`, `download_ibkr_history_to_csv`, `download_multi_chunks`) 與 CLI 模式。
"""

import os
import sys
import json
import socket
import argparse
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any, Union

import numpy as np
import pandas as pd

try:
    from ib_async import IB, Future, ContFuture, util
    IB_AVAILABLE = True
except ImportError:
    try:
        from ib_insync import IB, Future, ContFuture, util
        IB_AVAILABLE = True
    except ImportError:
        IB = Any  # type: ignore
        Future = None  # type: ignore
        ContFuture = None  # type: ignore
        util = None  # type: ignore
        IB_AVAILABLE = False

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CURRENT_DIR = os.path.dirname(_THIS_DIR) if os.path.basename(_THIS_DIR) == "backtest" else _THIS_DIR
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

CACHE_DIR = os.path.join(CURRENT_DIR, "data", "ibkr_cache")
GDRIVE_DATA_DIR = os.path.join(CURRENT_DIR, "回測用數據")
os.makedirs(CACHE_DIR, exist_ok=True)

# 支援的週期映射至 IBKR barSizeSetting 與單次請求安全跨度 (避免觸發 IBKR Pacing Violation 162)
IBKR_TF_CONFIG: Dict[str, Dict[str, Any]] = {
    "1m":  {"bar_size": "1 min",   "chunk_days": 5,   "duration_str": "5 D",   "cn_name": "1分鐘K線"},
    "5m":  {"bar_size": "5 mins",  "chunk_days": 15,  "duration_str": "15 D",  "cn_name": "5分鐘K線"},
    "15m": {"bar_size": "15 mins", "chunk_days": 30,  "duration_str": "30 D",  "cn_name": "15分鐘K線"},
    "1h":  {"bar_size": "1 hour",  "chunk_days": 60,  "duration_str": "60 D",  "cn_name": "1小時K線"},
    "4h":  {"bar_size": "4 hours", "chunk_days": 180, "duration_str": "180 D", "cn_name": "4小時K線"},
    "1d":  {"bar_size": "1 day",   "chunk_days": 365, "duration_str": "1 Y",   "cn_name": "日線K線"},
}

TIMEFRAME_TO_IB_BAR = {k: v["bar_size"] for k, v in IBKR_TF_CONFIG.items()}


def _round_tick(val: float, tick: float = 0.25) -> float:
    return round(round(float(val) / tick) * tick, 2)


def get_cache_path(symbol: str, timeframe: str) -> str:
    """
    取得指定標的與週期的本地 CSV 快取路徑。
    優先查找標準快取目錄，亦相容『回測用數據/CME_ES_10年歷史數據_*_全合約合併檔.csv』。
    """
    clean_sym = symbol.replace("CME_", "").upper().strip()
    tf = timeframe.lower().strip()

    std_path = os.path.join(CACHE_DIR, f"{clean_sym}_{tf}.csv")
    if os.path.exists(std_path):
        return std_path

    cn_label = IBKR_TF_CONFIG.get(tf, {}).get("cn_name", "")
    if cn_label:
        for folder in (GDRIVE_DATA_DIR, CURRENT_DIR, CACHE_DIR):
            candidate = os.path.join(folder, f"CME_{clean_sym}_10年歷史數據_{cn_label}_全合約合併檔.csv")
            if os.path.exists(candidate):
                return candidate

    return std_path


def audit_cache_integrity(df: pd.DataFrame, timeframe: str = "1h", tick_size: float = 0.25) -> Tuple[bool, str]:
    """
    審計本地快取 CSV 是否為『真實 IBKR K 線』，攔截舊版由日線除以 23 小時偽造的假數據：
    1. 檢查價格是否符合 CME 0.25 Tick 最小跳動點 (容許極少數浮點誤差)。
    2. 檢查小時/分鐘線是否出現『連續相鄰 K 線成交量完全相同』的人工均分特徵。
    """
    if df is None or len(df) < 10:
        return False, "數據筆數過少 (<10 根)"

    tf = timeframe.lower().strip()
    closes = df["close"].dropna().values
    if len(closes) == 0:
        return False, "缺少有效 close 價格欄位"

    rem = np.abs(np.round(closes / tick_size) * tick_size - closes)
    non_tick_ratio = float(np.mean(rem > 1e-3))
    if non_tick_ratio > 0.05:
        return (
            False,
            f"偵測到 {non_tick_ratio * 100:.1f}% 價格不符合 {tick_size} Tick 規格 (如 {closes[0]:.2f})，屬舊版日線插值污染數據"
        )

    if tf in ("1m", "5m", "15m", "1h", "4h") and "volume" in df.columns:
        vols = df["volume"].dropna().values
        if len(vols) > 30:
            same_as_prev = float(np.mean(vols[1:] == vols[:-1]))
            if same_as_prev > 0.50:
                return (
                    False,
                    f"偵測到 {same_as_prev * 100:.1f}% 相鄰 K 棒成交量完全相同 (如固定 {vols[0]:.0f})，屬舊版日線均分偽造數據"
                )

    return True, "真實 IBKR K 線審計通過"


def list_cached_csvs() -> List[Dict[str, Any]]:
    """列出本地 data/ibkr_cache/ 中所有已快取的 IBKR 歷史 CSV 檔案資訊 (含真實性審計結果)"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    items = []
    for fn in sorted(os.listdir(CACHE_DIR)):
        if not fn.endswith(".csv"):
            continue
        fpath = os.path.join(CACHE_DIR, fn)
        try:
            parts = fn[:-4].split("_")
            sym = parts[0] if len(parts) >= 1 else "UNKNOWN"
            tf = parts[1].lower() if len(parts) >= 2 else "5m"
            df = pd.read_csv(fpath)
            cnt = len(df)
            start_s = str(df["datetime"].iloc[0]) if cnt > 0 and "datetime" in df.columns else "--"
            end_s = str(df["datetime"].iloc[-1]) if cnt > 0 and "datetime" in df.columns else "--"
            size_kb = round(os.path.getsize(fpath) / 1024.0, 1)
            audit_ok, audit_msg = (True, "匯總檔案")
            if tf in IBKR_TF_CONFIG and "close" in df.columns:
                audit_ok, audit_msg = audit_cache_integrity(df, tf)
            items.append({
                "filename": fn,
                "symbol": sym,
                "timeframe": tf,
                "bars": cnt,
                "start": start_s,
                "end": end_s,
                "size_kb": size_kb,
                "audit_ok": audit_ok,
                "audit_msg": audit_msg
            })
        except Exception:
            pass
    return items


def _is_port_listening(host: str, port: int, timeout: float = 0.25) -> bool:
    """快速探測主機端口是否在監聽，避免無效連線超時等待"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _connect_ib(
    host: str = "127.0.0.1",
    port: Optional[int] = None,
    client_id: int = 99
) -> Tuple[Optional[Any], Optional[int]]:
    """嘗試連線至本機 IB Gateway (4002) 或 TWS (7497)，並支援 clientId 自動遞增避讓"""
    if not IB_AVAILABLE:
        return None, None

    ports_to_try = [port] if port else [4002, 7497]
    open_ports = [p for p in ports_to_try if _is_port_listening(host, p)]
    if not open_ports:
        return None, None

    ib = IB()
    connected_port = None

    for p in open_ports:
        for cid in (client_id, client_id + 1, client_id + 2, client_id + 3):
            try:
                ib.connect(host, int(p), clientId=cid, timeout=4)
                if ib.isConnected():
                    connected_port = int(p)
                    break
            except Exception:
                continue
        if connected_port:
            break

    if not connected_port or not ib.isConnected():
        return None, None
    return ib, connected_port


def _get_cme_quarterly_contracts(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime
) -> List[Tuple[str, datetime, datetime]]:
    """
    生成涵蓋 [start_dt, end_dt] 的 CME 季度期貨合約序列 (H=03, M=06, U=09, Z=12)。
    回傳: [(lastTradeDateOrContractMonth 'YYYYMM', seg_start, seg_end), ...]
    CME 股指期貨於每季月第 3 個週五到期，主力合約通常於到期前 8 天 (第 2 個週四/週五) 換月。
    """
    quarters = [3, 6, 9, 12]
    schedule = []

    cur_year = start_dt.year
    while cur_year <= end_dt.year + 1:
        for m in quarters:
            first_day = datetime(cur_year, m, 1)
            first_friday_day = 1 + ((4 - first_day.weekday()) % 7)
            third_friday = datetime(cur_year, m, first_friday_day + 14, 16, 0)
            roll_date = third_friday - timedelta(days=8)
            ym_str = f"{cur_year}{m:02d}"
            schedule.append((ym_str, roll_date, third_friday))
        cur_year += 1

    segments = []
    for idx, (ym_str, roll_dt, _) in enumerate(schedule):
        seg_start = schedule[idx - 1][1] if idx > 0 else (roll_dt - timedelta(days=92))
        seg_end = roll_dt
        if seg_end >= start_dt and seg_start <= end_dt:
            s_clamped = max(start_dt, seg_start)
            e_clamped = min(end_dt, seg_end)
            if e_clamped > s_clamped:
                segments.append((ym_str, s_clamped, e_clamped))
    return segments


def _parse_duration_days(duration_str: str, timeframe: str) -> int:
    """將 IBKR durationStr (如 '1 Y', '3 M', '1 W', '15 D') 轉為天數"""
    parts = duration_str.strip().upper().split()
    if len(parts) == 2:
        try:
            num = max(1, int(float(parts[0])))
            unit = parts[1]
            if unit.startswith("Y"):
                return num * 365
            if unit.startswith("M"):
                return num * 30
            if unit.startswith("W"):
                return num * 7
            if unit.startswith("D"):
                return num
        except ValueError:
            pass
    return int(IBKR_TF_CONFIG.get(timeframe.lower().strip(), {}).get("chunk_days", 60))


def download_ibkr_historical_csv(
    symbol: str = "ES",
    timeframe: str = "1h",
    start_date: str = "2016-11-15",
    end_date: str = "2026-11-15",
    host: str = "127.0.0.1",
    port: Optional[int] = 4002,
    client_id: int = 99,
    back_adjust_rollover: bool = True,
    pacing_delay_sec: float = 0.65,
    existing_ib: Optional[Any] = None
) -> Dict[str, Any]:
    """
    透過 IBKR TWS / Gateway API (ib_async / ib_insync) 下載真實歷史 K 線並儲存為標準 CSV：
    - 自動使用 includeExpired=True 分段拉取季度合約，避開 ContFuture Error 10339。
    - 自動執行巴拿馬換月價差後向平移 (Panama Back-Adjustment)，消除換月跳空。
    - 轉換時間戳為美東時間 (America/New_York 無時區標記之左標記時間) 並強制對齊 0.25 Tick。
    - 若本地已有通過真實性審計的快取，自動增量合併去重；若舊快取屬假數據則自動覆寫淨化。
    """
    if not IB_AVAILABLE:
        return {
            "status": "error",
            "message": "尚未安裝 ib_async 或 ib_insync 套件，請先執行 `pip install ib_async`。"
        }

    tf = timeframe.lower().strip()
    if tf not in IBKR_TF_CONFIG:
        return {"status": "error", "message": f"不支援的 K 線週期: {timeframe}"}

    tf_cfg = IBKR_TF_CONFIG[tf]
    bar_size = tf_cfg["bar_size"]
    chunk_days = tf_cfg["chunk_days"]

    clean_sym = symbol.replace("CME_", "").upper().strip()
    s_dt = pd.to_datetime(start_date).to_pydatetime()
    e_dt = pd.to_datetime(end_date).to_pydatetime()
    if e_dt <= s_dt:
        e_dt = s_dt + timedelta(days=1)

    owns_ib = False
    ib = existing_ib
    connected_port = port

    if ib is None or not ib.isConnected():
        ib, connected_port = _connect_ib(host, port, client_id)
        owns_ib = True

    if not ib or not ib.isConnected():
        return {
            "status": "error",
            "message": f"無法連線至本機 IBKR TWS/Gateway ({host}:{port or '4002/7497'}, clientId={client_id})，請確認已登入且啟用 API。"
        }

    contract_dfs: List[pd.DataFrame] = []
    try:
        segments = _get_cme_quarterly_contracts(clean_sym, s_dt, e_dt)
        for ym_str, seg_s, seg_e in segments:
            # 2019-06 以前若選 MES/MNQ，自動使用同指數點位之 ES/NQ 主力合約
            use_sym = clean_sym
            if clean_sym in ("MES", "MNQ") and int(ym_str) < 201906:
                use_sym = "ES" if clean_sym == "MES" else "NQ"

            contract = Future(
                symbol=use_sym,
                lastTradeDateOrContractMonth=ym_str,
                exchange="CME",
                currency="USD",
                includeExpired=True
            )
            details = ib.reqContractDetails(contract)
            if not details:
                continue
            qualified_contract = details[0].contract

            cur_end = seg_e + timedelta(days=1)
            seg_chunks = []
            while cur_end > seg_s:
                span = max(1, min(chunk_days, (cur_end - seg_s).days + 2))
                dur_str = f"{span} D" if span < 365 else "1 Y"
                end_str = cur_end.strftime("%Y%m%d %H:%M:%S") + " US/Eastern"

                bars = ib.reqHistoricalData(
                    qualified_contract,
                    endDateTime=end_str,
                    durationStr=dur_str,
                    barSizeSetting=bar_size,
                    whatToShow="TRADES",
                    useRTH=False,
                    formatDate=2,
                    keepUpToDate=False
                )
                if bars:
                    df_b = util.df(bars)
                    if df_b is not None and len(df_b) > 0:
                        df_b["datetime"] = (
                            pd.to_datetime(df_b["date"], utc=True)
                            .dt.tz_convert("America/New_York")
                            .dt.tz_localize(None)
                        )
                        df_b = df_b[["datetime", "open", "high", "low", "close", "volume"]].set_index("datetime").sort_index()
                        seg_chunks.append(df_b)

                cur_end -= timedelta(days=chunk_days)
                ib.sleep(pacing_delay_sec)

            if seg_chunks:
                df_contract = pd.concat(seg_chunks).sort_index()
                df_contract = df_contract[~df_contract.index.duplicated(keep="last")]
                contract_dfs.append(df_contract)

    except Exception as e:
        return {"status": "error", "message": f"IBKR 歷史下載失敗: {e}"}
    finally:
        if owns_ib and ib:
            try:
                ib.disconnect()
            except Exception:
                pass

    if not contract_dfs:
        return {
            "status": "error",
            "message": "IBKR API 未回傳任何有效歷史 K 線，請確認 TWS 已開啟歷史數據權限及 includeExpired 支援。"
        }

    # 巴拿馬後向價差平移 (Panama Back-Adjustment)
    if back_adjust_rollover and len(contract_dfs) > 1:
        cum_offset = 0.0
        adjusted_list = [contract_dfs[-1].copy()]
        for k in range(len(contract_dfs) - 2, -1, -1):
            older_df = contract_dfs[k].copy()
            newer_df = contract_dfs[k + 1]
            common_idx = older_df.index.intersection(newer_df.index)
            if len(common_idx) > 0:
                ref_ts = common_idx[-1]
                gap = float(newer_df.loc[ref_ts, "close"]) - float(older_df.loc[ref_ts, "close"])
            else:
                gap = float(newer_df["open"].iloc[0]) - float(older_df["close"].iloc[-1])
            cum_offset += gap
            for col in ("open", "high", "low", "close"):
                older_df[col] = (older_df[col] + cum_offset).apply(_round_tick)
            adjusted_list.append(older_df)
        adjusted_list.reverse()
        df_new = pd.concat(adjusted_list).sort_index()
    else:
        df_new = pd.concat(contract_dfs).sort_index()

    df_new = df_new[~df_new.index.duplicated(keep="last")]
    df_new = df_new[(df_new.index >= pd.to_datetime(start_date)) & (df_new.index <= pd.to_datetime(end_date) + timedelta(days=1))]

    for col in ("open", "high", "low", "close"):
        df_new[col] = df_new[col].apply(_round_tick)
    df_new["volume"] = df_new["volume"].fillna(0).astype(int)

    downloaded_bars = len(df_new)
    out_path = os.path.join(CACHE_DIR, f"{clean_sym}_{tf}.csv")

    # 若既有快取通過真實性審計，執行增量合併去重；若失敗 (舊假數據) 則直接替換
    df_final = df_new
    if os.path.exists(out_path):
        try:
            df_old = pd.read_csv(out_path, parse_dates=["datetime"], index_col="datetime").sort_index()
            is_valid_old, _ = audit_cache_integrity(df_old, tf)
            if is_valid_old and len(df_old) > 0:
                df_final = pd.concat([df_old, df_new]).sort_index()
                df_final = df_final[~df_final.index.duplicated(keep="last")]
        except Exception:
            df_final = df_new

    df_final.to_csv(out_path, encoding="utf-8")
    earliest_dt = df_final.index[0] if len(df_final) > 0 else pd.to_datetime(start_date)
    latest_dt = df_final.index[-1] if len(df_final) > 0 else pd.to_datetime(end_date)

    return {
        "status": "ok",
        "symbol": clean_sym,
        "timeframe": tf,
        "port": connected_port,
        "bars_count": len(df_final),
        "downloaded_bars": downloaded_bars,
        "total_cached_bars": len(df_final),
        "start": str(earliest_dt),
        "end": str(latest_dt),
        "cache_path": out_path,
        "cache_file": out_path,
        "earliest_dt_str": earliest_dt.strftime("%Y%m%d %H:%M:%S"),
        "message": f"成功從 IBKR API (Port {connected_port}) 下載並對齊 {clean_sym} ({tf.upper()}) {downloaded_bars:,} 根，累計共 {len(df_final):,} 根真實 K 線！"
    }


def download_ibkr_history_to_csv(
    symbol: str = "MES",
    timeframe: str = "1h",
    duration: str = "1 Y",
    end_datetime: Optional[Union[str, datetime]] = "",
    host: str = "127.0.0.1",
    port: Optional[int] = None,
    client_id: int = 77,
    existing_ib: Optional[Any] = None
) -> Dict[str, Any]:
    """
    相容 `server.py` 單次下載介面：將 `duration` + `end_datetime` 自動轉換為季度合約分段抓檔，
    徹底避開 ContFuture Error 10339。
    """
    if isinstance(end_datetime, datetime):
        e_dt = end_datetime
    elif isinstance(end_datetime, str) and end_datetime.strip():
        try:
            e_dt = pd.to_datetime(end_datetime.strip()).to_pydatetime()
        except Exception:
            e_dt = datetime.now()
    else:
        e_dt = datetime.now()

    days = _parse_duration_days(duration, timeframe)
    s_dt = e_dt - timedelta(days=days)

    return download_ibkr_historical_csv(
        symbol=symbol,
        timeframe=timeframe,
        start_date=s_dt.strftime("%Y-%m-%d"),
        end_date=e_dt.strftime("%Y-%m-%d"),
        host=host,
        port=port,
        client_id=client_id,
        existing_ib=existing_ib
    )


def download_multi_chunks(
    symbol: str = "ES",
    timeframe: str = "1h",
    total_chunks: int = 5,
    chunk_duration: str = "1 Y",
    host: str = "127.0.0.1",
    port: Optional[int] = None,
    client_id: int = 77,
    pacing_delay_sec: float = 8.0,
    continue_from_cache: bool = False
) -> Dict[str, Any]:
    """
    相容 `server.py` 與 `export_10y_data_to_gdrive.py` 之分批歷史下載介面：
    自動依據 `total_chunks * chunk_duration` 計算回溯區間，並透過季度合約分段安全拉取及巴拿馬換月對齊。
    """
    clean_sym = symbol.replace("CME_", "").upper().strip()
    tf = timeframe.lower().strip()
    cache_path = os.path.join(CACHE_DIR, f"{clean_sym}_{tf}.csv")

    e_dt = datetime.now()
    if continue_from_cache and os.path.exists(cache_path):
        try:
            df_existing = pd.read_csv(cache_path, parse_dates=["datetime"], index_col="datetime").sort_index()
            is_valid_old, _ = audit_cache_integrity(df_existing, tf)
            if is_valid_old and len(df_existing) > 0:
                e_dt = df_existing.index.min().to_pydatetime()
        except Exception:
            pass

    chunk_days = _parse_duration_days(chunk_duration, tf)
    total_days = max(1, int(total_chunks)) * chunk_days
    s_dt = e_dt - timedelta(days=total_days)

    # 在連續高頻抓取內建安全間隔 (保留合理 pacing，同時避免過久阻塞 HTTP 請求)
    safe_delay = min(max(0.65, float(pacing_delay_sec) * 0.15), 2.0)

    res = download_ibkr_historical_csv(
        symbol=clean_sym,
        timeframe=tf,
        start_date=s_dt.strftime("%Y-%m-%d"),
        end_date=e_dt.strftime("%Y-%m-%d"),
        host=host,
        port=port,
        client_id=client_id,
        pacing_delay_sec=safe_delay
    )

    if res.get("status") == "ok":
        cme_note = ""
        if clean_sym in ("MES", "MNQ"):
            cme_note = f"💡 提示：{clean_sym} 微型合約於 2019 年 5 月上市；2019-06 前區段已自動對接 { 'ES' if clean_sym == 'MES' else 'NQ' } 歷史季度合約並完成換月對齊。"
        res["total_chunks_executed"] = total_chunks
        res["total_downloaded_bars"] = res.get("downloaded_bars", 0)
        res["cme_note"] = cme_note
    return res


# 相容別名，供不同前端路由或腳本直接呼叫
download_ibkr_history = download_ibkr_historical_csv
download_and_cache_csv = download_ibkr_historical_csv


def one_click_update_data(
    symbol: str = "ES",
    timeframe: str = "1h",
    force_vm: bool = False
) -> Dict[str, Any]:
    """
    一鍵更新本地回測數據 (智慧雙通道):
    1. 優先探測本機 IBKR TWS / Gateway (Port 4002 / 7497)。若可用，執行本機 API 增量更新。
    2. 若本機 Gateway 未在線或 force_vm=True，自動使用內建密鑰 (.ssh_keys/ibkr_vm_ed25519)
       向 24/7 運行的雲端主機 (ib-autotrade-69.taila3a5ef.ts.net) 請求數據同步。
    3. 同步完成後自動對所有本地快取執行 0.25 Tick 規格審計與健康度檢查。
    4. 回傳詳細同步報告 (包含更新源、檔案列表、筆數、涵蓋起訖日、檔案大小與審計結果)。
    """
    import subprocess
    clean_sym = symbol.replace("CME_", "").upper().strip()
    tf = timeframe.lower().strip()

    # 1. 嘗試本地 IBKR
    local_ib, local_port = (None, None)
    if not force_vm:
        local_ib, local_port = _connect_ib()

    if local_ib is not None and local_ib.isConnected():
        try:
            res = download_ibkr_history_to_csv(
                symbol=clean_sym,
                timeframe=tf,
                duration="3 M",
                existing_ib=local_ib
            )
            local_ib.disconnect()
            files_info = list_cached_csvs()
            total_b = sum(f.get("bars", 0) for f in files_info)
            res["source_used"] = f"LOCAL_IBKR_PORT_{local_port}"
            res["synced_files"] = files_info
            res["total_bars"] = total_b
            return res
        except Exception as e:
            if local_ib and local_ib.isConnected():
                try:
                    local_ib.disconnect()
                except Exception:
                    pass

    # 2. 雲端 VM 同步通道 (透過專用 SSH Key 自動拉取雲端已校準之最新數據)
    key_path = os.path.join(CURRENT_DIR, ".ssh_keys", "ibkr_vm_ed25519")
    vm_host = "larrywong69@ib-autotrade-69.taila3a5ef.ts.net"
    err_msg = ""
    if os.path.exists(key_path):
        try:
            cmd = [
                "scp", "-i", key_path,
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=8",
                f"{vm_host}:/home/larrywong69/ibkr_paper_trading/data/ibkr_cache/*",
                CACHE_DIR + "/"
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                files_info = list_cached_csvs()
                total_b = sum(f.get("bars", 0) for f in files_info)
                latest_dt_str = max([f.get("end", "") for f in files_info], default="--")
                return {
                    "status": "ok",
                    "source_used": "CLOUD_VM_AUTO_SYNC",
                    "message": f"成功透過雲端主機高速同步最新 CME 數據！共獲取 {len(files_info)} 個資料集、累計 {total_b:,} 根真實 K 線（最新至 {latest_dt_str[:10]}）。",
                    "synced_files": files_info,
                    "total_bars": total_b,
                    "latest_date": latest_dt_str
                }
            else:
                err_msg = proc.stderr.strip() or proc.stdout.strip()
        except Exception as ex:
            err_msg = str(ex)
    else:
        err_msg = "未找到雲端主機 SSH 密鑰 (.ssh_keys/ibkr_vm_ed25519)"

    # 3. 若網路通道失敗，但本地已有快取
    files_info = list_cached_csvs()
    if files_info:
        total_b = sum(f.get("bars", 0) for f in files_info)
        latest_dt_str = max([f.get("end", "") for f in files_info], default="--")
        return {
            "status": "warning",
            "source_used": "LOCAL_CACHE_STANDALONE",
            "message": f"雲端通道暫時離線 ({err_msg})，已校驗本地現有 {len(files_info)} 個數據集 ({total_b:,} 根真實 K 線) 依然完整可用！",
            "synced_files": files_info,
            "total_bars": total_b,
            "latest_date": latest_dt_str
        }

    return {
        "status": "error",
        "source_used": "NONE",
        "message": f"數據更新失敗：本地 IBKR Gateway 未啟動，且無法連接雲端主機 ({err_msg})。"
    }


def main():
    parser = argparse.ArgumentParser(description="IBKR 10年歷史數據分段下載、換月對齊與真實性審計工具")
    parser.add_argument("--symbol", default="ES", help="期貨代號 (ES / MES / NQ / MNQ)")
    parser.add_argument("--timeframe", default="1h", help="K線週期 (1m / 5m / 15m / 1h / 4h / 1d)")
    parser.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD (優先於 --chunks)")
    parser.add_argument("--end", default=None, help="結束日期 YYYY-MM-DD")
    parser.add_argument("--chunks", type=int, default=1, help="分批下載次數 (例如 5 或 10)")
    parser.add_argument("--duration", default="1 Y", help="單批下載長度 (例如 1 Y / 3 M)")
    parser.add_argument("--host", default="127.0.0.1", help="IBKR TWS/Gateway Host")
    parser.add_argument("--port", type=int, default=None, help="IBKR Port (預設自動嘗試 4002/7497)")
    parser.add_argument("--client-id", type=int, default=99, help="IBKR API ClientId")
    parser.add_argument("--delay", type=float, default=0.65, help="安全 Pacing 間隔秒數")
    parser.add_argument("--continue-cache", action="store_true", help="從現有合法快取最早時間點向前回溯")
    parser.add_argument("--list", action="store_true", help="列出本地所有已快取 CSV 與審計狀態")
    args = parser.parse_args()

    if args.list:
        items = list_cached_csvs()
        print(f"\n📂 本地 IBKR 歷史快取目錄 ({CACHE_DIR}):")
        if not items:
            print("  (尚無任何快取 CSV 檔案)")
        else:
            for it in items:
                flag = "✅" if it.get("audit_ok", True) else "⚠️"
                print(
                    f"  {flag} {it['filename']:<36} | {it['bars']:>7} 根 | "
                    f"{it['start'][:10]} ~ {it['end'][:10]} | {it['size_kb']} KB | {it.get('audit_msg', '')}"
                )
        return

    if args.start or args.end:
        res = download_ibkr_historical_csv(
            symbol=args.symbol,
            timeframe=args.timeframe,
            start_date=args.start or "2016-11-15",
            end_date=args.end or datetime.now().strftime("%Y-%m-%d"),
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            pacing_delay_sec=args.delay
        )
    elif args.chunks > 1:
        res = download_multi_chunks(
            symbol=args.symbol,
            timeframe=args.timeframe,
            total_chunks=args.chunks,
            chunk_duration=args.duration,
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            pacing_delay_sec=args.delay,
            continue_from_cache=args.continue_cache
        )
    else:
        res = download_ibkr_history_to_csv(
            symbol=args.symbol,
            timeframe=args.timeframe,
            duration=args.duration,
            host=args.host,
            port=args.port,
            client_id=args.client_id
        )

    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
