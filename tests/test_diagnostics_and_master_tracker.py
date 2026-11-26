#!/usr/bin/env python3
"""
單元測試：交易明細與異常報告分開生成、不生成 HTML、累計追蹤總表每日同步
"""
import os
import csv
import json
import unittest
from unittest.mock import patch, MagicMock

from gdrive_report_manager import (
    GoogleDriveReportManager,
    TRADES_REPORT_DIR,
    INCIDENTS_REPORT_DIR,
    GDRIVE_TEMP_TRADES_DIR,
    GDRIVE_TEMP_INCIDENTS_DIR,
    GDRIVE_SUBFOLDERS
)


class TestDiagnosticsAndMasterTracker(unittest.TestCase):
    def setUp(self):
        self.mgr = GoogleDriveReportManager(folder_id="test_folder_id")
        self.sample_trades = [
            {
                "timestamp": "2026-11-26 10:00:00",
                "symbol": "MES",
                "direction": "BUY",
                "shares": 2,
                "price": 6000.25,
                "realized_pnl": 50.0,
                "commission": 1.25,
                "net_pnl": 48.75,
                "exec_id": "EX001"
            }
        ]
        self.sample_incidents = [
            {
                "timestamp": "2026-11-26 10:05:00",
                "severity": "WARNING",
                "category": "SYSTEM",
                "code": 366,
                "source": "HMDS",
                "message": "No historical data query found",
                "details": {"ticker_id": 9993}
            }
        ]
        self.sample_status = {
            "ibkr_connected": True,
            "equity": 100000.0,
            "currency": "USD",
            "realized_pnl": 50.0,
            "unrealized_pnl_usd": 10.0,
            "v43": {
                "active_position": {"direction": "多倉", "contracts": 1, "entry_price": 6000.0, "unrealized_pnl": 10.0},
                "open_orders": []
            },
            "v50": {
                "active_position": {"direction": "空倉", "contracts": 0, "entry_price": "", "unrealized_pnl": 0.0},
                "open_orders": []
            }
        }

    def test_generate_diagnostics_files_no_html_and_separate_folders(self):
        """測試診斷報告完全不再生成 HTML 檔案，且交易明細與異常報告分開存入各自子資料夾"""
        res = self.mgr.generate_diagnostics_files(
            trades=self.sample_trades,
            incidents=self.sample_incidents,
            status_snapshot=self.sample_status,
            log_lines=["test log line 1", "test log line 2"]
        )

        # 1. 斷言不再生成 HTML
        self.assertIsNone(res["html_path"])
        # 2. 斷言分開生成交易明細與異常報告
        self.assertTrue(os.path.exists(res["trades_csv_path"]))
        self.assertTrue(os.path.exists(res["incidents_csv_path"]))
        self.assertTrue(os.path.exists(res["trades_export_path"]))
        self.assertTrue(os.path.exists(res["incidents_export_path"]))

        # 3. 斷言儲存目錄正確
        self.assertTrue(res["trades_csv_path"].startswith(TRADES_REPORT_DIR))
        self.assertTrue(res["incidents_csv_path"].startswith(INCIDENTS_REPORT_DIR))
        self.assertTrue(res["trades_export_path"].startswith(GDRIVE_TEMP_TRADES_DIR))
        self.assertTrue(res["incidents_export_path"].startswith(GDRIVE_TEMP_INCIDENTS_DIR))

        # 4. 驗證交易明細內容
        with open(res["trades_csv_path"], "r", encoding="utf-8-sig") as f:
            content = f.read()
            self.assertIn("=== 1. 系統與持倉掛單快照 ===", content)
            self.assertIn("=== 2. 最新成交審計紀錄", content)
            self.assertIn("MES", content)
            self.assertIn("EX001", content)

        # 5. 驗證異常報告內容
        with open(res["incidents_csv_path"], "r", encoding="utf-8-sig") as f:
            content = f.read()
            self.assertIn("=== 1. 異常稽核統計摘要 ===", content)
            self.assertIn("=== 2. 最新錯誤與異常稽核紀錄", content)
            self.assertIn("No historical data query found", content)
            self.assertIn("test log line 2", content)

    def test_generate_incidents_csv_structure(self):
        """測試 generate_incidents_csv 核心報表格式"""
        output_file = os.path.join(INCIDENTS_REPORT_DIR, "test_incidents_output.csv")
        report_data = {
            "report_date": "2026-11-26",
            "incident_audit": {
                "total_count": 1,
                "by_severity": {"INFO": 0, "WARNING": 1, "ERROR": 0, "CRITICAL": 0},
                "recent_top": self.sample_incidents
            }
        }
        self.mgr.generate_incidents_csv(report_data, output_file, log_lines=["sys engine ok"])
        self.assertTrue(os.path.exists(output_file))

        with open(output_file, "r", encoding="utf-8-sig") as f:
            reader = list(csv.reader(f))
            headers = [r[0] for r in reader if r]
            self.assertIn("=== 異常稽核核心統計 ===", headers)
            self.assertIn("=== 異常事件詳細記錄 ===", headers)
            self.assertIn("=== 系統引擎運行日誌片段 (最新) ===", headers)

    @patch.object(GoogleDriveReportManager, "_upload_to_gdrive")
    def test_sync_master_tracker_to_gdrive(self, mock_upload):
        """測試累計追蹤總表同步至 Google Drive 根目錄"""
        mock_upload.return_value = ("fake_master_id", "https://fake_url/master", "")

        res = self.mgr.sync_master_tracker_to_gdrive()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["file_id"], "fake_master_id")
        self.assertEqual(res["url"], "https://fake_url/master")

        # 斷言上傳目標 subfolder 為空 (即根目錄)
        mock_upload.assert_called_once()
        _, kwargs = mock_upload.call_args
        self.assertEqual(kwargs.get("subfolder"), "")

    @patch("google_sheets_sync_manager.GoogleSheetsSyncManager.sync_all")
    @patch.object(GoogleDriveReportManager, "_upload_to_gdrive")
    @patch.object(GoogleDriveReportManager, "sync_master_tracker_to_gdrive")
    def test_push_daily_report_separate_folders_and_master_sync(
        self, mock_sync_master, mock_upload, mock_sheets_sync
    ):
        """測試 push_daily_report: 不產生 HTML、明細與異常分開上傳對應子資料夾、必定更新累計總表"""
        mock_sync_master.return_value = {
            "status": "ok",
            "file_id": "master_id_123",
            "url": "https://drive.google.com/master_123"
        }
        mock_upload.side_effect = [
            ("trades_id_1", "https://drive.google.com/trades_1", ""),      # 交易明細
            ("incidents_id_1", "https://drive.google.com/incidents_1", ""), # 異常報告
        ]
        mock_sheets_sync.return_value = {"status": "ok", "trade_ledger_url": "https://ledger"}

        report_data = {
            "report_date": "2026-11-26",
            "executions": self.sample_trades,
            "incident_audit": {
                "total_count": 1,
                "by_severity": {"WARNING": 1},
                "recent_top": self.sample_incidents
            },
            "pnl_performance": {"net_pnl": 48.75, "total_trades": 1},
            "equity_summary": {"start_equity": 100000.0, "end_equity": 100048.75}
        }

        res = self.mgr.push_daily_report(report_data, force_reupload=True)

        self.assertEqual(res["status"], "ok")
        # 斷言無 HTML / Doc
        self.assertEqual(res["doc_id"], "")
        self.assertEqual(res["doc_url"], "")

        # 斷言交易明細與異常報告均有上傳
        self.assertEqual(res["trades_sheet_id"], "trades_id_1")
        self.assertEqual(res["incidents_sheet_id"], "incidents_id_1")

        # 斷言累計總表同步被調用
        mock_sync_master.assert_called_once()

        # 斷言 _upload_to_gdrive 分別使用了 "交易明細" 與 "異常報告" 作為 subfolder
        self.assertEqual(mock_upload.call_count, 2)
        call1_kwargs = mock_upload.call_args_list[0][1]
        call2_kwargs = mock_upload.call_args_list[1][1]
        self.assertEqual(call1_kwargs.get("subfolder"), "交易明細")
        self.assertEqual(call2_kwargs.get("subfolder"), "異常報告")

    @patch.object(GoogleDriveReportManager, "_detect_upload_backend")
    @patch.object(GoogleDriveReportManager, "_run_cmd_args")
    def test_upload_via_rclone_subfolder(self, mock_run, mock_detect):
        """測試 rclone 上傳支援 subfolder"""
        mock_run.side_effect = [
            (0, "", ""),  # rclone copyto
            (0, json.dumps([{"ID": "subfile_123"}]), "")  # rclone lsjson
        ]

        dummy_path = os.path.join(TRADES_REPORT_DIR, "dummy_test.csv")
        with open(dummy_path, "w") as f:
            f.write("test")

        file_id, url, err = self.mgr._upload_via_rclone("gdrive:", dummy_path, subfolder="交易明細")

        self.assertEqual(file_id, "subfile_123")
        self.assertEqual(err, "")
        # 驗證 copyto 目標路徑包含 subfolder
        first_call_args = mock_run.call_args_list[0][0][0]
        self.assertIn("gdrive:交易明細/dummy_test.csv", first_call_args)


if __name__ == "__main__":
    unittest.main()
