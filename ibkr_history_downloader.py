#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ibkr_history_downloader.py
從本機 IB Gateway (Port 4002) 或 TWS (Port 7497) 下載真實 CME (MES / MNQ) 歷史 K 線，
並增量合併、去重後快取為本地 CSV (data/ibkr_cache/{SYMBOL}_{TF}.csv)，供離線極速回測使用。
"""

import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional

import pandas as pd

try:
    from ib_async import IB, ContFuture, util
    IB_AVAILABLE = True
except ImportError:
    try:
        from ib_insync import IB, ContFuture, util
        IB_AVAILABLE = True
    except ImportError:
        IB_AVAILABLE = False

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(CURRENT_DIR, "data", "ibkr_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

TIMEFRAME_TO_IB_BAR = {
    "1m": "1 min",
    "5m": "5 mins",
    "15m": "15 mins",
    "1h": "1 hour",
    "4h": "4 hours",
    "1d": "1 day",
}


def get_cache_path(symbol: str, timeframe: str) -> str:
    sym = symbol.upper().strip()
    tf = timeframe.lower().strip()
    return os.path.join(CACHE_DIR, f"{sym}_{tf}.csv")


def list_cached_csvs() -> List[Dict[str, Any]]:
    """列出本地 data/ibkr_cache/ 中所有已快取的 IBKR 歷史 CSV 檔案資訊"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    items = []
    for fn in sorted(os.listdir(CACHE_DIR)):
        if not fn.endswith(".csv"):
            continue
        fpath = os.path.join(CACHE_DIR, fn)
        try:
            parts = fn[:-4].split("_")
            sym = parts[0] if len(parts) >= 1 else "UNKNOWN"
            tf = parts[1] if len(parts) >= 2 else "5m"
            df = pd.read_csv(fpath)
            cnt = len(df)
            start_s = str(df["datetime"].iloc[0]) if cnt > 0 else "--"
            end_s = str(df["datetime"].iloc[-1]) if cnt > 0 else "--"
            size_kb = round(os.path.getsize(fpath) / 1024.0, 1)
            items.append({
                "filename": fn,
                "symbol": sym,
                "timeframe": tf,
                "bars": cnt,
                "start": start_s,
                "end": end_s,
                "size_kb": size_kb
            })
        except Exception:
            pass
    return items


def download_ibkr_history_to_csv(
    symbol: str = "MES",
    timeframe: str = "5m",
    duration: str = "1 M",
    host: str = "127.0.0.1",
    port: Optional[int] = None,
    client_id: int = 77
) -> Dict[str, Any]:
    """
    連線本機 IB Gateway / TWS 下載歷史 K 線，並增量快取至 CSV。
    """
    if not IB_AVAILABLE:
        return {"status": "error", "message": "未安裝 ib_async / ib_insync，無法連線 IBKR"}

    sym = symbol.upper().strip()
    tf = timeframe.lower().strip()
    bar_setting = TIMEFRAME_TO_IB_BAR.get(tf, "5 mins")

    # 依 IBKR 規則防呆調整最大單次請求長度
    dur = duration.strip().upper()
    if tf == "1m" and dur not in ("1 D", "2 D", "3 D", "5 D", "1 W"):
        dur = "1 W"
    elif tf in ("5m", "15m") and dur in ("1 Y", "2 Y", "5 Y", "10 Y"):
        dur = "3 M"
    elif tf in ("1h", "4h") and dur in ("5 Y", "10 Y"):
        dur = "2 Y"

    ports_to_try = [port] if port else [4002, 7497]
    ib = IB()
    connected_port = None

    for p in ports_to_try:
        for cid in (client_id, client_id + 1, client_id + 2):
            try:
                ib.connect(host, int(p), clientId=cid, timeout=8)
                if ib.isConnected():
                    connected_port = p
                    break
            except Exception:
                continue
        if connected_port:
            break

    if not connected_port or not ib.isConnected():
        return {
            "status": "error",
            "message": f"無法連線本機 IB Gateway (4002) 或 TWS (7497)。請確認本機已開啟並登入 IB Gateway/TWS 且啟用 API。"
        }

    try:
        contract = ContFuture(sym, "CME")
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return {"status": "error", "message": f"無法鎖定 CME 連續合約: {sym}"}
        contract = qualified[0]

        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=dur,
            barSizeSetting=bar_setting,
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
            timeout=30
        )

        if not bars:
            return {"status": "error", "message": f"IBKR 未回傳 {sym} ({bar_setting}, {dur}) 歷史數據，請確認市場數據權限或稍後重試"}

        df_new = util.df(bars)
        df_new["datetime"] = pd.to_datetime(df_new["date"]).dt.tz_localize(None)
        df_new = df_new[["datetime", "open", "high", "low", "close", "volume"]].copy()
        df_new["volume"] = df_new["volume"].fillna(0).astype(int)

        cache_path = get_cache_path(sym, tf)
        if os.path.exists(cache_path):
            try:
                df_old = pd.read_csv(cache_path)
                df_old["datetime"] = pd.to_datetime(df_old["datetime"])
                df_merged = pd.concat([df_old, df_new], ignore_index=True)
            except Exception:
                df_merged = df_new
        else:
            df_merged = df_new

        df_merged = df_merged.drop_duplicates(subset=["datetime"], keep="last")
        df_merged = df_merged.sort_values("datetime").reset_index(drop=True)
        df_merged.to_csv(cache_path, index=False, encoding="utf-8")

        return {
            "status": "ok",
            "message": f"成功從 IBKR (Port {connected_port}) 下載 {len(df_new)} 根 {sym} ({tf}) K棒，已增量快取共 {len(df_merged)} 根！",
            "symbol": sym,
            "timeframe": tf,
            "downloaded_bars": len(df_new),
            "total_cached_bars": len(df_merged),
            "start": str(df_merged["datetime"].iloc[0]),
            "end": str(df_merged["datetime"].iloc[-1]),
            "cache_file": cache_path
        }
    except Exception as e:
        return {"status": "error", "message": f"IBKR 歷史下載失敗: {e}"}
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
