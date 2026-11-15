#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ibkr_history_downloader.py
從本機 IB Gateway (Port 4002) 或 TWS (Port 7497) 下載真實 CME 期貨 (MES / ES / MNQ / NQ) 歷史 K 線，
支援單次下載與「全自動分批多次下載 (如 10 年 1 小時 K 線，分 5~10 次每次 1~2 年)」。
內建 IBKR 官方 Pacing 頻率安全保護、增量合併去重快取為本地 CSV (data/ibkr_cache/{SYMBOL}_{TF}.csv)。
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Union

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

# IBKR API 支援之 Bar Size 字串對照表
TIMEFRAME_TO_IB_BAR = {
    "1m": "1 min",
    "5m": "5 mins",
    "15m": "15 mins",
    "1h": "1 hour",
    "4h": "4 hours",
    "1d": "1 day",
}

# IBKR 各週期單次請求最大建議長度（避免 Error 162: Unsupported duration）
MAX_SINGLE_DURATION_PER_TF = {
    "1m": "1 W",
    "5m": "1 M",
    "15m": "3 M",
    "1h": "1 Y",
    "4h": "1 Y",
    "1d": "5 Y",
}


def get_cache_path(symbol: str, timeframe: str) -> str:
    """取得特定標的與週期在 data/ibkr_cache/ 的 CSV 絕對路徑"""
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


def _connect_ib(host: str = "127.0.0.1", port: Optional[int] = None, client_id: int = 77) -> tuple[Optional[IB], Optional[int]]:
    """嘗試連線至本機 IB Gateway 或 TWS"""
    if not IB_AVAILABLE:
        return None, None

    ports_to_try = [port] if port else [4002, 7497]
    ib = IB()
    connected_port = None

    for p in ports_to_try:
        for cid in (client_id, client_id + 1, client_id + 2, client_id + 3):
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
        return None, None
    return ib, connected_port


def _sanitize_duration(timeframe: str, duration: str) -> str:
    """依照 IBKR 官方規定校驗並防呆修正單次請求長度"""
    tf = timeframe.lower().strip()
    dur = duration.strip().upper()
    if tf == "1m" and dur not in ("1 D", "2 D", "3 D", "5 D", "1 W"):
        return "1 W"
    if tf in ("5m", "15m") and dur in ("1 Y", "2 Y", "5 Y", "10 Y"):
        return "3 M"
    if tf in ("1h", "4h") and dur in ("2 Y", "5 Y", "10 Y"):
        # IBKR 官方規定 1 小時 K 棒單次上限為 1 Y
        return "1 Y"
    return dur


def download_ibkr_history_to_csv(
    symbol: str = "MES",
    timeframe: str = "1h",
    duration: str = "1 Y",
    end_datetime: Optional[Union[str, datetime]] = "",
    host: str = "127.0.0.1",
    port: Optional[int] = None,
    client_id: int = 77,
    existing_ib: Optional[IB] = None
) -> Dict[str, Any]:
    """
    連線本機 IB Gateway / TWS 下載歷史 K 線（單次），並增量快取至 CSV。
    支援指定 end_datetime 向過去推進。
    """
    if not IB_AVAILABLE:
        return {"status": "error", "message": "未安裝 ib_async / ib_insync，無法連線 IBKR"}

    sym = symbol.upper().strip()
    tf = timeframe.lower().strip()
    bar_setting = TIMEFRAME_TO_IB_BAR.get(tf, "1 hour")
    dur = _sanitize_duration(tf, duration)

    # 處理 end_datetime 格式
    end_dt_str = ""
    if isinstance(end_datetime, datetime):
        end_dt_str = end_datetime.strftime("%Y%m%d %H:%M:%S")
    elif isinstance(end_datetime, str):
        end_dt_str = end_datetime.strip()

    owns_ib = False
    ib = existing_ib
    connected_port = port

    if ib is None or not ib.isConnected():
        ib, connected_port = _connect_ib(host, port, client_id)
        owns_ib = True

    if not ib or not ib.isConnected():
        return {
            "status": "error",
            "message": f"無法連線本機 IB Gateway (4002) 或 TWS (7497)。請確認已開啟並登入 IB Gateway/TWS 且啟用 API。"
        }

    try:
        contract = ContFuture(sym, "CME")
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return {"status": "error", "message": f"無法鎖定 CME 連續期貨合約: {sym}"}
        contract = qualified[0]

        bars = ib.reqHistoricalData(
            contract,
            endDateTime=end_dt_str,
            durationStr=dur,
            barSizeSetting=bar_setting,
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
            timeout=40
        )

        if not bars:
            return {
                "status": "warning",
                "message": f"IBKR 未回傳 {sym} ({bar_setting}, {dur}, 結束點: '{end_dt_str or '現前'}') 歷史數據，可能已達該商品在 CME 最早上市日期或權限限制。",
                "bars_count": 0
            }

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

        earliest_dt = df_merged["datetime"].iloc[0]
        latest_dt = df_merged["datetime"].iloc[-1]

        return {
            "status": "ok",
            "message": f"成功從 IBKR (Port {connected_port}) 下載 {len(df_new)} 根 {sym} ({tf}) K 棒，本地快取累計共 {len(df_merged)} 根！",
            "symbol": sym,
            "timeframe": tf,
            "downloaded_bars": len(df_new),
            "total_cached_bars": len(df_merged),
            "start": str(earliest_dt),
            "end": str(latest_dt),
            "cache_file": cache_path,
            "earliest_dt_str": earliest_dt.strftime("%Y%m%d %H:%M:%S")
        }
    except Exception as e:
        return {"status": "error", "message": f"IBKR 歷史下載失敗: {e}"}
    finally:
        if owns_ib and ib:
            try:
                ib.disconnect()
            except Exception:
                pass


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
    全自動分批下載多年歷史 K 線（例如分 5 次或 10 次下載 10 年 1 小時 K 線）。
    嚴格遵守 IBKR Pacing Limits（每批之間暫停 pacing_delay_sec 秒），
    並自動以每一批最早時間點作為上一批的 endDateTime 逐步向前回溯。
    """
    if not IB_AVAILABLE:
        return {"status": "error", "message": "未安裝 ib_async / ib_insync，無法連線 IBKR"}

    sym = symbol.upper().strip()
    tf = timeframe.lower().strip()
    dur = _sanitize_duration(tf, chunk_duration)
    cache_path = get_cache_path(sym, tf)

    # 決定起始推進時間點
    current_end_str = ""
    if continue_from_cache and os.path.exists(cache_path):
        try:
            df_existing = pd.read_csv(cache_path)
            if len(df_existing) > 0:
                earliest_existing = pd.to_datetime(df_existing["datetime"]).min()
                current_end_str = earliest_existing.strftime("%Y%m%d %H:%M:%S")
                print(f"[IBKR 分批下載] 發現既有快取，從現有最早時間點 {current_end_str} 繼續向前回溯歷史...")
        except Exception:
            pass

    ib, connected_port = _connect_ib(host, port, client_id)
    if not ib or not ib.isConnected():
        return {
            "status": "error",
            "message": f"無法連線本機 IB Gateway (4002) 或 TWS (7497)。請確認已開啟並登入 IB Gateway/TWS 且啟用 API。"
        }

    chunk_results = []
    total_downloaded = 0
    final_cached_count = 0
    start_time_iso = "--"
    end_time_iso = "--"

    try:
        for idx in range(1, total_chunks + 1):
            target_desc = f"結束於 {current_end_str}" if current_end_str else "當前最新時間"
            print(f"[IBKR 分批下載] 第 {idx}/{total_chunks} 批: 請求 {sym} ({tf}, 單次長度 {dur}, {target_desc})...")

            res = download_ibkr_history_to_csv(
                symbol=sym,
                timeframe=tf,
                duration=dur,
                end_datetime=current_end_str,
                host=host,
                port=connected_port,
                client_id=client_id,
                existing_ib=ib
            )

            if res.get("status") == "error":
                chunk_results.append({
                    "chunk": idx,
                    "status": "error",
                    "message": res.get("message")
                })
                break

            downloaded = res.get("downloaded_bars", 0)
            chunk_results.append({
                "chunk": idx,
                "status": "ok" if downloaded > 0 else "empty",
                "downloaded_bars": downloaded,
                "current_total": res.get("total_cached_bars", 0),
                "start": res.get("start"),
                "end": res.get("end"),
                "requested_end": current_end_str or "NOW"
            })

            total_downloaded += downloaded
            final_cached_count = res.get("total_cached_bars", 0)
            start_time_iso = res.get("start", start_time_iso)
            end_time_iso = res.get("end", end_time_iso)

            # 若此批回傳 0 根或最早時間未向前推進，代表已達交易所最早歷史極限
            if downloaded == 0:
                print(f"[IBKR 分批下載] 第 {idx} 批無更多數據回傳，已到達 {sym} 在交易所的最早上市歷史極限。提前完成。")
                break

            # 檢查並更新下一批的結束時間
            if os.path.exists(cache_path):
                df_curr = pd.read_csv(cache_path)
                curr_min_dt = pd.to_datetime(df_curr["datetime"]).min()
                next_end_str = curr_min_dt.strftime("%Y%m%d %H:%M:%S")
                if next_end_str == current_end_str:
                    print(f"[IBKR 分批下載] 時間點未再往前推進 ({next_end_str})，已收錄所有可用歷史。")
                    break
                current_end_str = next_end_str

            # 依 IBKR 官方規範暫停，避免 Pacing Violation (Error 162)
            if idx < total_chunks:
                print(f"[IBKR Pacing] 依規避頻率限制，安全休眠 {pacing_delay_sec} 秒...")
                ib.sleep(pacing_delay_sec)

        cme_note = ""
        if sym in ("MES", "MNQ"):
            cme_note = f"💡 提示：{sym} 微型合約於 2019 年 5 月 6 日才在 CME 上市。如需更長 (10年以上) 完整歷史，建議下載 ES / NQ（點位與走勢完全一致）。"

        return {
            "status": "ok",
            "message": f"成功完成 {len(chunk_results)} 批下載！本次下載累計 {total_downloaded} 根，本地快取累計 {final_cached_count} 根 K 棒 ({start_time_iso[:10]} ~ {end_time_iso[:10]})",
            "symbol": sym,
            "timeframe": tf,
            "total_chunks_executed": len(chunk_results),
            "total_downloaded_bars": total_downloaded,
            "total_cached_bars": final_cached_count,
            "start": start_time_iso,
            "end": end_time_iso,
            "cache_file": cache_path,
            "cme_note": cme_note,
            "chunks_detail": chunk_results
        }
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="IBKR 歷史 K 線下載器 (支援分批多次下載多年數據)")
    parser.add_argument("--symbol", type=str, default="ES", help="期貨標的代碼 (ES, MES, NQ, MNQ)")
    parser.add_argument("--timeframe", type=str, default="1h", help="K線週期 (1m, 5m, 15m, 1h, 4h, 1d)")
    parser.add_argument("--chunks", type=int, default=1, help="分批下載次數 (例如 5 或 10)")
    parser.add_argument("--duration", type=str, default="1 Y", help="單批下載長度 (1h 建議 1 Y, 1d 建議 2 Y)")
    parser.add_argument("--port", type=int, default=None, help="IB Gateway/TWS 端口 (預設嘗試 4002, 7497)")
    parser.add_argument("--delay", type=float, default=8.0, help="各批次之間安全間隔秒數 (預設 8.0 秒)")
    parser.add_argument("--continue-cache", action="store_true", help="從現有快取最早時間點向前繼續回溯")
    parser.add_argument("--list", action="store_true", help="列出本地所有已快取 CSV")

    args = parser.parse_args()

    if args.list:
        items = list_cached_csvs()
        print(f"\n📂 本地 IBKR 歷史快取目錄 ({CACHE_DIR}):")
        if not items:
            print("  (尚無任何快取 CSV 檔案)")
        else:
            for it in items:
                print(f"  • {it['filename']:<16} | {it['bars']:>7} 根 | {it['start'][:10]} ~ {it['end'][:10]} | {it['size_kb']} KB")
        return

    print("=" * 65)
    print(f"🚀 開始 IBKR 歷史下載: {args.symbol} ({args.timeframe})")
    print(f"   總批次: {args.chunks} 次 | 單批長度: {args.duration} | 安全間隔: {args.delay} 秒")
    print("=" * 65)

    if args.chunks > 1:
        res = download_multi_chunks(
            symbol=args.symbol,
            timeframe=args.timeframe,
            total_chunks=args.chunks,
            chunk_duration=args.duration,
            port=args.port,
            pacing_delay_sec=args.delay,
            continue_from_cache=args.continue_cache
        )
    else:
        res = download_ibkr_history_to_csv(
            symbol=args.symbol,
            timeframe=args.timeframe,
            duration=args.duration,
            port=args.port
        )

    print("\n執行結果:")
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
