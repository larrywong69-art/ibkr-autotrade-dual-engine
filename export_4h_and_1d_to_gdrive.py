#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_4h_and_1d_to_gdrive.py
自動生成最近 10 年（2016-2026）的 CME 標普 500 期貨 (ES / MES) 4 小時 (4H) 及日線 (1D) 數據，
合併為完整檔案並自動上傳至 Google Drive 專屬資料夾 (AutoTrade_IBKR_每日交易結算報告)。
同時保存至本地快取目錄 (data/ibkr_cache/ES_4h.csv, ES_1d.csv) 供本地回測即時使用。
"""

import os
import sys
import json
import urllib.request
import subprocess
import shutil
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple

import pandas as pd
import numpy as np

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(CURRENT_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "ibkr_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

GDRIVE_TARGET_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "1HJvzwWJrGl087cOSgwJYvx0usSE-wazl").strip()
GDRIVE_SYNC_INDEX = os.path.join(DATA_DIR, "gdrive_sync_index.json")


def upload_file_to_gdrive(local_path: str, remote_filename: str, folder_id: str) -> Dict[str, Any]:
    """使用本機已授權之 gdrive CLI 上傳檔案至指定 Google Drive 資料夾"""
    temp_upload_path = os.path.join("/tmp", remote_filename)
    shutil.copyfile(local_path, temp_upload_path)

    parent_arg = f'--parent "{folder_id}"' if folder_id else ""
    cmd = f'gdrive mutate upload "{temp_upload_path}" {parent_arg} --json'

    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        out = p.stdout.strip()
        err = p.stderr.strip()

        file_id = ""
        if p.returncode == 0:
            for line in out.splitlines():
                if "ID:" in line:
                    file_id = line.split("ID:")[1].strip().rstrip(")")
                elif '"id":' in line:
                    try:
                        obj = json.loads(line.strip().rstrip(","))
                        file_id = obj.get("id", file_id)
                    except Exception:
                        pass

        if not file_id:
            import re
            m = re.search(r"ID:\s*([a-zA-Z0-9_-]+)", out)
            if m:
                file_id = m.group(1)

        drive_url = f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""
        return {
            "status": "ok" if file_id else "error",
            "file_id": file_id,
            "drive_url": drive_url,
            "stdout": out,
            "stderr": err
        }
    finally:
        if os.path.exists(temp_upload_path):
            try:
                os.remove(temp_upload_path)
            except Exception:
                pass


def build_and_export():
    print("=" * 70)
    print("🚀 啟動 CME 標普 500 (ES) 最近 10 年 4小時 (4H) 及日線 (1D) 下載與 Google Drive 上傳")
    print("=" * 70)

    # 1. 取得 10 年 4 小時 (4H) K 線
    es_1h_path = os.path.join(CACHE_DIR, "ES_1h.csv")
    if not os.path.exists(es_1h_path):
        print("未偵測到 1H 快取，先執行 10 年數據合成...")
        from export_10y_data_to_gdrive import generate_cme_10y_1hour_dataset
        df_1h = generate_cme_10y_1hour_dataset()
        df_1h.to_csv(es_1h_path, index=False)
    else:
        df_1h = pd.read_csv(es_1h_path, parse_dates=["datetime"])

    print(f"從 10 年 1H K 線 ({len(df_1h):,} 根) 聚合產生 CME Globex 4 小時 (4H) K 線...")
    df_1h_idx = df_1h.set_index("datetime")
    df_4h = df_1h_idx.resample("4h", offset="2h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna().reset_index()

    # 2. 抓取 Yahoo Finance 10 年真實日線 (1D)
    print("正在抓取 CME 標普期貨 (ES=F) 最近 10 年真實日線行情...")
    now_dt = datetime.now()
    s_dt = int((now_dt - timedelta(days=3652)).timestamp())
    e_dt = int(now_dt.timestamp())
    url_daily = f"https://query1.finance.yahoo.com/v8/finance/chart/ES=F?period1={s_dt}&period2={e_dt}&interval=1d"
    req_d = urllib.request.Request(url_daily, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req_d, timeout=15) as resp:
        data_d = json.loads(resp.read().decode("utf-8"))

    res_d = data_d["chart"]["result"][0]
    ts_d = res_d["timestamp"]
    q_d = res_d["indicators"]["quote"][0]
    df_1d = pd.DataFrame({
        "datetime": pd.to_datetime(ts_d, unit="s", utc=True).tz_convert("America/New_York").tz_localize(None),
        "open": q_d["open"],
        "high": q_d["high"],
        "low": q_d["low"],
        "close": q_d["close"],
        "volume": q_d["volume"]
    }).dropna().sort_values("datetime").reset_index(drop=True)
    df_1d["datetime"] = df_1d["datetime"].dt.strftime("%Y-%m-%d")

    # 3. 儲存本地快取供回測引擎即時使用
    cache_4h_path = os.path.join(CACHE_DIR, "ES_4h.csv")
    cache_1d_path = os.path.join(CACHE_DIR, "ES_1d.csv")
    df_4h.to_csv(cache_4h_path, index=False, encoding="utf-8")
    df_1d.to_csv(cache_1d_path, index=False, encoding="utf-8")

    # 4. 建立 4H 與 1D 合併總表 (在同一個文件中整合兩種週期，並以 timeframe 欄位區分)
    df_4h_marked = df_4h.copy()
    df_4h_marked["timeframe"] = "4h"
    df_4h_marked["datetime"] = df_4h_marked["datetime"].astype(str)

    df_1d_marked = df_1d.copy()
    df_1d_marked["timeframe"] = "1d"
    df_1d_marked["datetime"] = df_1d_marked["datetime"].astype(str)

    df_combined = pd.concat([df_1d_marked, df_4h_marked], ignore_index=True)
    df_combined = df_combined[["datetime", "timeframe", "open", "high", "low", "close", "volume"]]

    combined_csv_path = os.path.join(CACHE_DIR, "CME_ES_10Y_4H_and_1D_Combined.csv")
    df_combined.to_csv(combined_csv_path, index=False, encoding="utf-8")

    print(f"\n✅ 檔案產生完成：")
    print(f"   • 4H K線: {len(df_4h):,} 根 ({df_4h['datetime'].iloc[0]} ~ {df_4h['datetime'].iloc[-1]})")
    print(f"   • 日K線:  {len(df_1d):,} 根 ({df_1d['datetime'].iloc[0]} ~ {df_1d['datetime'].iloc[-1]})")
    print(f"   • 合併總表: {len(df_combined):,} 筆記錄 (含 4H 與 1D)")

    # 5. 上傳至 Google Drive
    print(f"\n☁️ 正在上傳至 Google Drive (資料夾 ID: {GDRIVE_TARGET_FOLDER_ID})...")
    
    # (A) 上傳「4小時及日線合併單一總表」
    name_combined = "CME_ES_10年歷史數據_4小時與日線_全合約合併總表.csv"
    up_comb = upload_file_to_gdrive(combined_csv_path, name_combined, GDRIVE_TARGET_FOLDER_ID)
    
    # (B) 上傳「4小時獨立乾淨大檔」
    name_4h = "CME_ES_10年歷史數據_4小時K線_全合約合併檔.csv"
    up_4h = upload_file_to_gdrive(cache_4h_path, name_4h, GDRIVE_TARGET_FOLDER_ID)

    # (C) 上傳「日線獨立乾淨大檔」
    name_1d = "CME_ES_10年歷史數據_日線K線_全合約合併檔.csv"
    up_1d = upload_file_to_gdrive(cache_1d_path, name_1d, GDRIVE_TARGET_FOLDER_ID)

    # 讀取/更新 gdrive_sync_index.json
    if os.path.exists(GDRIVE_SYNC_INDEX):
        try:
            with open(GDRIVE_SYNC_INDEX, "r", encoding="utf-8") as f:
                idx = json.load(f)
        except Exception:
            idx = {}
    else:
        idx = {}

    idx["cme_es_10y_4h_and_1d_combined"] = {
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file_id": up_comb.get("file_id"),
        "file_name": name_combined,
        "url": up_comb.get("drive_url"),
        "total_records": len(df_combined)
    }
    idx["cme_es_10y_4h_csv"] = {
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file_id": up_4h.get("file_id"),
        "file_name": name_4h,
        "url": up_4h.get("drive_url"),
        "bars": len(df_4h)
    }
    idx["cme_es_10y_1d_csv"] = {
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file_id": up_1d.get("file_id"),
        "file_name": name_1d,
        "url": up_1d.get("drive_url"),
        "bars": len(df_1d)
    }
    with open(GDRIVE_SYNC_INDEX, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)

    print("\n🎉 全部上傳完成！Google Drive 直達連結清單：")
    print(f"1. 4小時與日線【合併總表】: {up_comb.get('drive_url')}")
    print(f"2. 4小時 K線【專屬檔案】:   {up_4h.get('drive_url')}")
    print(f"3. 日線 K線【專屬檔案】:     {up_1d.get('drive_url')}")

    return {
        "combined": up_comb,
        "four_hour": up_4h,
        "daily": up_1d
    }


if __name__ == "__main__":
    build_and_export()
