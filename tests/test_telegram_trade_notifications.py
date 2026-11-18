#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
單元測試：Telegram 成交通報升級版功能驗證
1. 寫明開倉或平倉 (標題與動作清晰標記)
2. 觸發原因改為顯示開倉邏輯 (如規則 1：順勢波 3 回踩 / 規則 2：極端邊界反彈 / 規則 3：微突破反轉)
3. 平倉加上 USD + HKD 雙幣盈虧金額及出場原因 (止盈 TP1/TP2/Runner、原生止損、保本平倉、追蹤止盈、防穿倉)
"""

import unittest
from unittest.mock import MagicMock, patch
from telegram_notifier import TelegramNotifier
from live_ibkr_dual_engine import LiveIBKRDualEngine


class TestTelegramTradeNotifications(unittest.TestCase):
    def setUp(self):
        self.notifier = TelegramNotifier()
        self.notifier.bot_token = "TEST_TOKEN"
        self.notifier.chat_id = "TEST_CHAT_ID"
        self.notifier.notify_trades = True

    @patch.object(TelegramNotifier, "send_message_sync")
    def test_open_long_notification(self, mock_send):
        """測試好倉開倉通知：寫明開倉、開倉邏輯、止盈止損"""
        mock_send.return_value = True

        self.notifier.notify_trade(
            strategy="V9.3 旗艦雙引擎 (MES)",
            symbol="MES",
            contract="MESZ6",
            action="🟢 買入開倉 (好倉 / LONG)",
            quantity=2,
            price=6010.25,
            tp_price=6025.50,
            sl_price=5995.00,
            is_closing=False,
            entry_rule="規則 1：順勢波 3 回踩",
            sync=True
        )

        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]

        # 斷言關鍵標籤
        self.assertIn("【AutoTrade 交易成交通報 - 開倉 (好倉)】", text)
        self.assertIn("買入開倉 (好倉 / LONG)", text)
        self.assertIn("2 口", text)
        self.assertIn("6,010.25 USD", text)
        self.assertIn("開倉邏輯", text)
        self.assertIn("規則 1：順勢波 3 回踩", text)
        self.assertIn("止盈目標", text)
        self.assertIn("6,025.50 USD", text)
        self.assertIn("初始止損", text)
        self.assertIn("5,995.00 USD", text)
        # 開倉不應出現平倉專用欄位
        self.assertNotIn("出場原因", text)
        self.assertNotIn("實現損益", text)

    @patch.object(TelegramNotifier, "send_message_sync")
    def test_open_short_notification(self, mock_send):
        """測試淡倉開倉通知：寫明淡倉開倉、規則 3 微突破反轉"""
        mock_send.return_value = True

        self.notifier.notify_trade(
            strategy="V9.3 旗艦雙引擎 (MES)",
            symbol="MES",
            contract="MESZ6",
            action="🔴 沽出開倉 (淡倉 / SHORT)",
            quantity=1,
            price=6000.00,
            tp_price=5985.00,
            sl_price=6010.00,
            is_closing=False,
            entry_rule="規則 3：微突破反轉 (頂 1 口)",
            sync=True
        )

        text = mock_send.call_args[0][0]

        self.assertIn("【AutoTrade 交易成交通報 - 開倉 (淡倉)】", text)
        self.assertIn("沽出開倉 (淡倉 / SHORT)", text)
        self.assertIn("規則 3：微突破反轉 (頂 1 口)", text)

    @patch.object(TelegramNotifier, "send_message_sync")
    def test_close_take_profit_notification(self, mock_send):
        """測試獲利平倉通知：寫明平倉 (獲利)、原開倉邏輯、出場原因與雙幣損益 (USD + HKD)"""
        mock_send.return_value = True

        self.notifier.notify_trade(
            strategy="V9.3 旗艦雙引擎 (MES)",
            symbol="MES",
            contract="MESZ6",
            action="沽出平倉 (平好倉 / SELL CLOSE)",
            quantity=1,
            price=6025.50,
            pnl=76.25,
            pnl_hkd=593.23,
            commission=1.24,
            is_closing=True,
            entry_rule="規則 1：順勢波 3 回踩",
            exit_reason="🎯 第一止盈 (TP1 1.55R)",
            fx_rate=7.78,
            sync=True
        )

        text = mock_send.call_args[0][0]

        self.assertIn("【AutoTrade 交易成交通報 - 平倉 (獲利)】", text)
        self.assertIn("沽出平倉 (平好倉 / SELL CLOSE)", text)
        self.assertIn("🎯 <b>出場原因</b>", text)
        self.assertIn("第一止盈 (TP1 1.55R)", text)
        self.assertIn("實現損益", text)
        self.assertIn("+76.25 USD (+HK$593.23)", text)
        self.assertIn("手續費佣金", text)
        self.assertIn("淨損益", text)
        self.assertIn("原開倉邏輯", text)
        self.assertIn("規則 1：順勢波 3 回踩", text)

    @patch.object(TelegramNotifier, "send_message_sync")
    def test_close_stop_loss_notification(self, mock_send):
        """測試止損平倉通知：寫明平倉 (止損)、出場原因、負損益 (USD + HKD)"""
        mock_send.return_value = True

        self.notifier.notify_trade(
            strategy="V9.3 旗艦雙引擎 (MNQ)",
            symbol="MNQ",
            contract="MNQZ6",
            action="買入平倉 (平淡倉 / BUY TO COVER)",
            quantity=1,
            price=21050.00,
            pnl=-35.00,
            pnl_hkd=-272.30,
            commission=1.24,
            is_closing=True,
            entry_rule="規則 2：極端邊界反彈 (頂 1 口)",
            exit_reason="🛑 原生止損 (Stop Loss)",
            fx_rate=7.78,
            sync=True
        )

        text = mock_send.call_args[0][0]

        self.assertIn("【AutoTrade 交易成交通報 - 平倉 (止損)】", text)
        self.assertIn("買入平倉 (平淡倉 / BUY TO COVER)", text)
        self.assertIn("出場原因", text)
        self.assertIn("原生止損 (Stop Loss)", text)
        self.assertIn("-35.00 USD (-HK$272.30)", text)
        self.assertIn("規則 2：極端邊界反彈 (頂 1 口)", text)

    @patch.object(TelegramNotifier, "notify_trade")
    def test_engine_on_exec_details_integration(self, mock_notify):
        """測試 LiveIBKRDualEngine.on_exec_details 觸發 Telegram 推播並防止重複發送"""
        mock_notify.return_value = True

        engine = LiveIBKRDualEngine()
        engine._active_trade_meta["MES"] = {
            "direction": "LONG",
            "entry": 6000.0,
            "init_sl": 5990.0,
            "tp1": 6015.0,
            "rule_desc": "規則 1：順勢波 3 回踩",
            "be_active": False,
            "trailing_active": False
        }

        # 模擬開倉成交
        fill_entry = MagicMock()
        fill_entry.contract.symbol = "MES"
        fill_entry.contract.localSymbol = "MESZ6"
        fill_entry.execution.execId = "EXEC_TEST_ENTRY_001"
        fill_entry.execution.orderId = 5001
        fill_entry.execution.orderRef = "V93_MES_ENTRY_TP1"
        fill_entry.execution.side = "BOT"
        fill_entry.execution.shares = 1.0
        fill_entry.execution.price = 6000.0
        fill_entry.execution.time = "2026-11-19 01:25:00"
        fill_entry.commissionReport = None

        engine.on_exec_details(None, fill_entry)
        self.assertIn("EXEC_TEST_ENTRY_001", engine._notified_exec_ids)
        self.assertEqual(mock_notify.call_count, 1)

        # 重複回呼不應再次推播
        engine.on_exec_details(None, fill_entry)
        self.assertEqual(mock_notify.call_count, 1)

        # 模擬平倉成交 (止盈出場)
        fill_exit = MagicMock()
        fill_exit.contract.symbol = "MES"
        fill_exit.contract.localSymbol = "MESZ6"
        fill_exit.execution.execId = "EXEC_TEST_EXIT_002"
        fill_exit.execution.orderId = 5002
        fill_exit.execution.orderRef = "V93_MES_TP_TP1"
        fill_exit.execution.side = "SLD"
        fill_exit.execution.shares = 1.0
        fill_exit.execution.price = 6015.0
        fill_exit.execution.time = "2026-11-19 01:26:00"
        fill_exit.commissionReport = None

        engine.on_exec_details(None, fill_exit)
        self.assertIn("EXEC_TEST_EXIT_002", engine._notified_exec_ids)
        self.assertEqual(mock_notify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
