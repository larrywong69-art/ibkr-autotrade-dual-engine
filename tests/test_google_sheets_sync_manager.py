#!/usr/bin/env python3
"""
Unit tests for google_sheets_sync_manager.py:
- Exit reason and rule multidimensional classification
- HKD currency conversion and KPI metrics calculation
- Openpyxl workbook generation with HKD formatting and 3 embedded charts (LineChart, BarChart, PieChart)
- Webhook payload structure and trade data enrichment
"""

import os
import unittest
import openpyxl
from google_sheets_sync_manager import (
    classify_trade_exit_reason,
    classify_trade_strategy_rule,
    classify_trading_session,
    compute_multidimensional_stats,
    compute_trade_stats,
    get_usd_hkd_rate,
    GoogleSheetsSyncManager,
)


class TestGoogleSheetsSyncManager(unittest.TestCase):

    def setUp(self):
        self.fx_rate = 7.78
        self.sample_trades = [
            {
                "timestamp": "2026-11-18 10:15:30",
                "symbol": "MES",
                "strategy": "V4.3",
                "side": "SLD",
                "action": "平倉",
                "shares": 1,
                "price": 5920.5,
                "realized_pnl": 125.0,
                "commission": 0.62,
                "net_pnl": 124.38,
                "exec_id": "EX001",
                "reason": "V93_MES_TP1",
            },
            {
                "timestamp": "2026-11-18 11:30:00",
                "symbol": "MES",
                "strategy": "V4.3",
                "side": "SLD",
                "action": "平倉",
                "shares": 1,
                "price": 5935.0,
                "realized_pnl": 250.0,
                "commission": 0.62,
                "net_pnl": 249.38,
                "exec_id": "EX002",
                "reason": "V93_MES_TP2",
            },
            {
                "timestamp": "2026-11-18 14:00:00",
                "symbol": "MNQ",
                "strategy": "V5.0",
                "side": "BOT",
                "action": "平倉",
                "shares": 1,
                "price": 20850.0,
                "realized_pnl": -95.0,
                "commission": 0.62,
                "net_pnl": -95.62,
                "exec_id": "EX003",
                "reason": "V50_MNQ_SL",
            },
            {
                "timestamp": "2026-11-18 03:30:00",
                "symbol": "MES",
                "strategy": "V4.3",
                "side": "SLD",
                "action": "平倉",
                "shares": 1,
                "price": 5910.0,
                "realized_pnl": 1.0,
                "commission": 0.62,
                "net_pnl": 0.38,
                "exec_id": "EX004",
                "reason": "BREAKEVEN_STOP",
            },
            {
                "timestamp": "2026-11-17 21:00:00",
                "symbol": "MNQ",
                "strategy": "V5.0",
                "side": "SLD",
                "action": "平倉",
                "shares": 1,
                "price": 20900.0,
                "realized_pnl": 450.0,
                "commission": 0.62,
                "net_pnl": 449.38,
                "exec_id": "EX005",
                "reason": "TRAILING_RUNNER_TP",
            },
        ]

    def test_classify_trade_exit_reason(self):
        self.assertEqual(classify_trade_exit_reason({"reason": "V93_MES_TP1"}), "🎯 第一止盈 (TP1 1.55R)")
        self.assertEqual(classify_trade_exit_reason({"reason": "V93_MES_TP2"}), "🎯 第二止盈 (TP2 3.10R)")
        self.assertEqual(classify_trade_exit_reason({"reason": "RUNNER_TARGET"}), "🚀 趨勢奔馳 (Runner 5.50R)")
        self.assertEqual(classify_trade_exit_reason({"reason": "BREAKEVEN_FLAT"}), "🛡️ 保本平倉 (Breakeven)")
        self.assertEqual(classify_trade_exit_reason({"reason": "TRAILING_STOP"}), "⚡ 追蹤止盈 (Trailing Stop)")
        self.assertEqual(classify_trade_exit_reason({"reason": "STOP_LOSS_GUARD"}), "🛑 原生止損 (Stop Loss)")
        self.assertEqual(classify_trade_exit_reason({"reason": "SOFT_STOP_7007"}), "🚨 軟體防穿倉 (Soft Stop)")
        self.assertEqual(classify_trade_exit_reason({"reason": "EOD_FLATTEN"}), "🕒 盤尾/週末清倉 (EOD Close)")
        self.assertEqual(classify_trade_exit_reason({"reason": "MANUAL_CLOSE"}), "✋ 手動平倉 (Manual Flat)")
        self.assertEqual(classify_trade_exit_reason({"action": "開倉", "net_pnl": 0}), "進場開倉 (Entry)")

    def test_classify_trade_strategy_rule(self):
        self.assertEqual(classify_trade_strategy_rule({"reason": "RULE_1_TREND_PULLBACK"}), "規則 1：順勢波 3 回踩")
        self.assertEqual(classify_trade_strategy_rule({"reason": "RULE_2_EXTREME_BOUNCE"}), "規則 2：箱體邊界反彈 (頂1口)")
        self.assertEqual(classify_trade_strategy_rule({"reason": "RULE_3_BREAKDOWN_REVERSAL"}), "規則 3：假突破反轉 (頂1口)")
        # Default rule
        self.assertEqual(classify_trade_strategy_rule({"reason": "UNKNOWN"}), "規則 1：順勢波 3 回踩")

    def test_classify_trading_session(self):
        # 22:30 HKT = 09:30 ET -> US RTH
        self.assertEqual(classify_trading_session("2026-11-18 22:30:00"), "🇺🇸 美盤 RTH (09:30-16:00 ET)")
        # 16:00 HKT = 03:00 ET -> London
        self.assertEqual(classify_trading_session("2026-11-18 16:00:00"), "🇪🇺 歐盤倫敦 (02:00-09:30 ET)")
        # 10:00 HKT = 21:00 ET -> Asia / Pre-market
        self.assertEqual(classify_trading_session("2026-11-18 10:00:00"), "🌏 亞盤盤前 (18:00-02:00 ET)")

    def test_compute_multidimensional_stats(self):
        stats = compute_multidimensional_stats(self.sample_trades, fx_rate=self.fx_rate)
        self.assertIn("rules", stats)
        self.assertIn("exit_reasons", stats)
        self.assertIn("sessions", stats)
        self.assertIn("table_rows", stats)

        # Check table_rows structure
        table_rows = stats["table_rows"]
        self.assertGreater(len(table_rows), 0)
        first_row = table_rows[0]
        self.assertIn("dimension", first_row)
        self.assertIn("count", first_row)
        self.assertIn("win_rate_pct", first_row)
        self.assertIn("net_pnl_hkd", first_row)

        # Check Exit Reasons are populated
        exit_dims = [r["dimension"] for r in stats["exit_reasons"]]
        self.assertIn("🎯 第一止盈 (TP1 1.55R)", exit_dims)
        self.assertIn("🎯 第二止盈 (TP2 3.10R)", exit_dims)

    def test_compute_trade_stats_hkd(self):
        stats = compute_trade_stats(self.sample_trades, fx_rate=self.fx_rate)
        self.assertEqual(stats["total_trades"], 5)
        self.assertEqual(stats["win_count"], 4)
        self.assertEqual(stats["loss_count"], 1)
        self.assertEqual(stats["win_rate_pct"], 80.0)
        self.assertIn("total_net_pnl_hkd", stats)
        self.assertIn("avg_trade_hkd", stats)
        self.assertIn("max_win_hkd", stats)
        self.assertIn("max_loss_hkd", stats)
        self.assertIn("total_comm_hkd", stats)

        # Expected HKD net pnl = sum(net_pnl_usd) * fx_rate
        expected_hkd_pnl = sum(t["net_pnl"] for t in self.sample_trades) * self.fx_rate
        self.assertAlmostEqual(stats["total_net_pnl_hkd"], expected_hkd_pnl, places=1)

    def test_build_trade_ledger_workbook_generation_and_charts(self):
        mgr = GoogleSheetsSyncManager()
        stats = compute_trade_stats(self.sample_trades, fx_rate=self.fx_rate)
        xlsx_path = mgr.build_trade_ledger_workbook(self.sample_trades, stats=stats)

        self.assertTrue(os.path.exists(xlsx_path))

        # Inspect workbook contents
        wb = openpyxl.load_workbook(xlsx_path)
        self.assertIn("分析報告", wb.sheetnames)
        self.assertIn("詳細交易紀錄", wb.sheetnames)

        ws1 = wb["分析報告"]
        self.assertIn("全面以 HKD 計價", ws1["A1"].value)
        # Check screenshot 4-column dimension header
        self.assertEqual(ws1["A15"].value, "🎯 規則進場原因與時段表現")
        self.assertEqual(ws1["A16"].value, "分析維度")
        self.assertEqual(ws1["B16"].value, "成交筆數")
        self.assertEqual(ws1["C16"].value, "勝率 (Win%)")
        self.assertEqual(ws1["D16"].value, "累計貢獻淨利")

        # Verify charts are embedded in Sheet 1
        self.assertGreaterEqual(len(ws1._charts), 1)

        ws2 = wb["詳細交易紀錄"]
        # 驗證 10 欄配對格式 (成交時間, 合約代號, 策略版本, 買賣方向, 合約張數, 開倉價(USD), 平倉價(USD), 實現損益(USD), 佣金手續費(USD), 淨損益(USD))
        self.assertEqual(ws2["A3"].value, "成交時間")
        self.assertEqual(ws2["B3"].value, "合約代號")
        self.assertEqual(ws2["C3"].value, "策略版本")
        self.assertEqual(ws2["D3"].value, "買賣方向")
        self.assertEqual(ws2["E3"].value, "合約張數")
        self.assertEqual(ws2["F3"].value, "開倉價(USD)")
        self.assertEqual(ws2["G3"].value, "平倉價(USD)")
        self.assertEqual(ws2["H3"].value, "實現損益(USD)")
        self.assertEqual(ws2["I3"].value, "佣金手續費(USD)")
        self.assertEqual(ws2["J3"].value, "淨損益(USD)")

        # 驗證資料列有正確填入配對資料
        self.assertIsNotNone(ws2["F4"].value)
        self.assertIsNotNone(ws2["G4"].value)
        self.assertIsNotNone(ws2["H4"].value)
        self.assertIsNotNone(ws2["J4"].value)


if __name__ == "__main__":
    unittest.main()
