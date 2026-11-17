#!/usr/bin/env python3
"""
Permanent safety regression suite for AutoTrade dual-engine live protection,
IBKR incident filtering, and the daily closed-loop audit/autofix sandbox.
"""

import os
import sys
import time
import subprocess
import tempfile
import shutil
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from live_ibkr_dual_engine import LiveIBKRDualEngine
from report_and_incident_manager import IncidentManager
from daily_auto_audit_fixer import DailyAutoAuditFixer


def _make_trade(
    oid, sym, otype, action, qty, price,
    order_ref="", parent_id=0, oca="", client_id=25, status="Submitted"
):
    order = SimpleNamespace(
        orderId=oid,
        permId=oid * 10,
        parentId=parent_id,
        clientId=client_id,
        ocaGroup=oca,
        orderRef=order_ref,
        orderType=otype,
        action=action,
        totalQuantity=qty,
        lmtPrice=price if otype == "LMT" else 0.0,
        auxPrice=price if otype != "LMT" else 0.0,
        tif="GTC",
    )
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=sym),
        order=order,
        orderStatus=SimpleNamespace(status=status),
    )


class TestLiveEngineOrderHygiene(unittest.TestCase):
    def setUp(self):
        self.eng = object.__new__(LiveIBKRDualEngine)
        self.eng.contract_mes = SimpleNamespace(symbol="MES")
        self.eng.contract_mnq = SimpleNamespace(symbol="MNQ")
        self.eng.engine_v93_mes = MagicMock()
        self.eng.engine_v93_mnq = MagicMock()
        self.eng._last_protect_ts = {}
        self.eng._entry_order_first_seen = {}
        self.eng._last_hist_fail_ts = {}
        self.eng._warned_uncancellable_ids = set()
        self.eng._cancel_attempt_ts = {}
        self.eng.incident_mgr = MagicMock()
        self.eng._round_to_tick = lambda p, t=0.25: round(p / t) * t

        self.cancelled_ids = []
        self.placed_orders = []
        req_counter = [9000]

        def next_id():
            req_counter[0] += 1
            return req_counter[0]

        self.eng.ib = MagicMock()
        self.eng.ib.isConnected.return_value = True
        self.eng.ib.client.getReqId.side_effect = next_id
        self.eng.ib.cancelOrder.side_effect = lambda o: self.cancelled_ids.append(o.orderId)
        self.eng.ib.placeOrder.side_effect = lambda c, o: self.placed_orders.append((c.symbol, o))
        self.eng.ib.sleep.return_value = None

    def test_bracket_outside_rth_false_and_distinct_refs(self):
        orders = self.eng._build_single_bracket("BUY", "SELL", 2, 7560.0, 7555.0, 7570.0, "V93_MES", "TP1")
        self.assertEqual(len(orders), 3)
        self.assertTrue(all(o.outsideRth is False for o in orders))
        self.assertEqual(orders[0].orderRef, "V93_MES_ENTRY_TP1")
        self.assertEqual(orders[1].orderRef, "V93_MES_SL_TP1")
        self.assertEqual(orders[2].orderRef, "V93_MES_TP_TP1")

    def test_cancels_wrong_direction_and_flat_orphan_orders(self):
        mes_pos = {"symbol": "MES", "direction": "SHORT", "contracts": 1, "entry_price": 7566.38, "current_price": 7563.0}
        self.eng.get_real_positions = lambda: (mes_pos, None)
        self.eng.ib.openTrades.return_value = [
            _make_trade(6576, "MES", "LMT", "BUY", 1, 7552.25, "GUARD_MES_TP_TP2", oca="G1"),
            _make_trade(6575, "MES", "STP", "BUY", 1, 7571.00, "GUARD_MES_SL_TP2", oca="G1"),
            _make_trade(6570, "MES", "STP", "SELL", 1, 7555.50, "GUARD_MES_SL_TP2", oca="G2"),
            _make_trade(6571, "MES", "LMT", "SELL", 1, 7573.00, "GUARD_MES_TP_TP2", oca="G2"),
            _make_trade(6197, "MNQ", "LMT", "BUY", 1, 27708.75, "V50_MNQ_TP1"),
        ]
        self.eng.ensure_position_protection()
        self.assertEqual(set(self.cancelled_ids), {6570, 6571, 6197})
        self.assertEqual(len(self.placed_orders), 0)

    def test_preserves_fresh_entry_bracket_cancels_after_ttl(self):
        self.eng.get_real_positions = lambda: (None, None)
        self.eng.ib.openTrades.return_value = [
            _make_trade(7001, "MNQ", "LMT", "BUY", 1, 27800.0, "V93_MNQ_ENTRY_TP2", parent_id=0),
            _make_trade(7002, "MNQ", "STP", "SELL", 1, 27780.0, "V93_MNQ_SL_TP2", parent_id=7001, status="PreSubmitted"),
            _make_trade(7003, "MNQ", "LMT", "SELL", 1, 27860.0, "V93_MNQ_TP_TP2", parent_id=7001, status="PreSubmitted"),
        ]
        self.eng.ensure_position_protection()
        self.assertEqual(self.cancelled_ids, [])

        self.eng._entry_order_first_seen[7001] = time.time() - 1300.0
        self.eng.ensure_position_protection()
        self.assertEqual(self.cancelled_ids, [7001])

    def test_trims_over_hedged_guard_orders(self):
        mes_pos = {"symbol": "MES", "direction": "LONG", "contracts": 2, "entry_price": 7560.0, "current_price": 7562.0}
        self.eng.get_real_positions = lambda: (mes_pos, None)
        self.eng.ib.openTrades.return_value = [
            _make_trade(429, "MES", "STP", "SELL", 2, 7555.5, "V93_MES_SL_TP2", oca="O1"),
            _make_trade(6568, "MES", "STP", "SELL", 1, 7555.5, "GUARD_MES_SL_TP1", oca="G1"),
            _make_trade(6570, "MES", "STP", "SELL", 1, 7555.5, "GUARD_MES_SL_TP2", oca="G2"),
        ]
        self.eng.ensure_position_protection()
        self.assertEqual(set(self.cancelled_ids), {6568, 6570})


class TestIncidentManagerClassification(unittest.TestCase):
    def test_suppresses_2109_downgrades_empty_202_and_catches_margin(self):
        mgr = IncidentManager()
        recorded = []
        mgr.record_incident = lambda **kw: recorded.append(kw) or kw

        mgr.handle_ibkr_error_event(1, 2109, "Order Event Warning:Attribute 'Outside Regular Trading Hours' is ignored. PlaceOrder is now being processed.")
        self.assertEqual(len(recorded), 0)

        mgr.handle_ibkr_error_event(2, 202, "Order Canceled - reason:")
        self.assertEqual(recorded[-1]["severity"], "INFO")
        self.assertEqual(recorded[-1]["category"], "TRADING")

        mgr.handle_ibkr_error_event(3, 201, "Order rejected - reason: insufficient margin funds")
        self.assertEqual(recorded[-1]["severity"], "CRITICAL")
        self.assertEqual(recorded[-1]["category"], "MARGIN")


class TestDailyAutoAuditFixer(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="audit_unit_")
        self.fixer = DailyAutoAuditFixer(base_dir=self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_deterministic_audit_catches_wrong_side_and_orphan(self):
        snapshot = {
            "window_hours": 24,
            "trades": [],
            "incidents": [{"code": 7001}, {"code": 7001}],
            "log_lines": [],
            "status_snapshot": {
                "ibkr_connected": True,
                "v43": {
                    "active_position": {"direction": "SHORT", "contracts": 1},
                    "open_orders": [
                        {"order_id": "6575", "action": "買入 BUY", "order_type": "STP", "contracts": 1, "order_ref": "GUARD_MES_SL"},
                        {"order_id": "6570", "action": "沽出 SELL", "order_type": "STP", "contracts": 1, "order_ref": "GUARD_MES_SL"},
                    ],
                },
                "v50": {
                    "active_position": None,
                    "open_orders": [
                        {"order_id": "6197", "action": "買入 BUY", "order_type": "LMT", "contracts": 1, "order_ref": "V50_MNQ_TP1"},
                    ],
                },
            },
        }
        res = self.fixer.run_deterministic_audit(snapshot)
        rule_ids = {f["rule_id"] for f in res["findings"]}
        self.assertIn("WRONG_DIRECTION_ORDER", rule_ids)
        self.assertIn("ORPHAN_EXIT_ORDER", rule_ids)
        self.assertIn("FREQUENT_NAKED_GUARDIAN", rule_ids)
        self.assertLess(res["health_score"], 50)

    def test_clean_snapshot_scores_100(self):
        snapshot = {
            "window_hours": 24,
            "trades": [],
            "incidents": [],
            "log_lines": [],
            "status_snapshot": {
                "ibkr_connected": True,
                "v43": {
                    "active_position": {"direction": "LONG", "contracts": 1},
                    "open_orders": [
                        {"order_id": "10", "action": "沽出 SELL", "order_type": "STP", "contracts": 1, "order_ref": "V93_MES_SL_TP2"},
                        {"order_id": "11", "action": "沽出 SELL", "order_type": "LMT", "contracts": 1, "order_ref": "V93_MES_TP_TP2"},
                    ],
                },
                "v50": {"active_position": None, "open_orders": []},
            },
        }
        res = self.fixer.run_deterministic_audit(snapshot)
        self.assertEqual(res["health_score"], 100)
        self.assertEqual(res["findings_count"], 0)

    def test_isolated_worktree_accepts_valid_patch_and_rejects_broken_patch(self):
        repo_dir = tempfile.mkdtemp(prefix="sandbox_git_")
        try:
            subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True)

            target = os.path.join(repo_dir, "server.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("def health():\n    return 'BAD'\n")

            os.makedirs(os.path.join(repo_dir, "tests"), exist_ok=True)
            with open(os.path.join(repo_dir, "tests", "test_smoke.py"), "w", encoding="utf-8") as f:
                f.write(
                    "import unittest\nfrom server import health\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_h(self): self.assertEqual(health(), 'OK')\n"
                )
            subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)

            fixer = DailyAutoAuditFixer(base_dir=repo_dir)

            # 1. Broken syntax patch must be rejected
            bad = fixer.apply_patches_in_isolated_worktree(
                patches=[{
                    "file_path": "server.py",
                    "explanation": "broken syntax",
                    "search_block": "    return 'BAD'",
                    "replace_block": "    return (",
                }],
                branch_name="autofix/test-bad",
                push_branch=False,
            )
            self.assertEqual(bad["status"], "rejected")

            # 2. Valid patch passes unit test and commits on autofix branch without touching main
            good = fixer.apply_patches_in_isolated_worktree(
                patches=[{
                    "file_path": "server.py",
                    "explanation": "fix health return",
                    "search_block": "    return 'BAD'",
                    "replace_block": "    return 'OK'",
                }],
                branch_name="autofix/test-good",
                push_branch=False,
            )
            self.assertEqual(good["status"], "ready_for_review")
            self.assertTrue(good["tests_passed"])
            with open(target, "r", encoding="utf-8") as f:
                self.assertIn("'BAD'", f.read(), "Working tree must stay untouched")
        finally:
            shutil.rmtree(repo_dir, ignore_errors=True)

    def test_telegram_audit_notification_includes_problems_and_solutions(self):
        from telegram_notifier import TelegramNotifier
        n = object.__new__(TelegramNotifier)
        sent_messages = []
        n.send_message_sync = lambda text, **kw: sent_messages.append(text) or True
        n.send_message_async = lambda text, **kw: sent_messages.append(text)

        sample_report = {
            "date": "2026-11-16",
            "deterministic_audit": {
                "health_score": 65,
                "findings_count": 1,
                "findings": [{"severity": "CRITICAL", "title": "MES 持倉方向與掛單方向衝突"}],
                "stats": {"trades_24h": 5, "incidents_24h": 12, "realized_pnl_24h": 42.5},
            },
            "ai_analysis": {
                "summary": "跨 ClientId 重連遺漏掛單導致重複補單。",
                "root_causes": ["try_connect 未呼叫 reqAllOpenOrders()"],
                "recommended_patches": [{
                    "file_path": "live_ibkr_dual_engine.py",
                    "explanation": "連線後主動同步全帳戶掛單並撤銷錯向單",
                }],
            },
            "patch_result": {
                "status": "ready_for_review",
                "branch": "autofix/2026-11-16",
                "applied_files": ["live_ibkr_dual_engine.py"],
            },
            "gdrive": {"folder_url": "https://drive.google.com/drive/folders/TEST"},
        }
        ok = n.notify_daily_audit(sample_report, sync=True)
        self.assertTrue(ok)
        self.assertEqual(len(sent_messages), 1)
        msg = sent_messages[0]
        self.assertIn("發現問題", msg)
        self.assertIn("解決方案與修復建議", msg)
        self.assertIn("autofix/2026-11-16", msg)
        self.assertIn("Google Drive", msg)

    def test_server_audit_async_job_and_nan_sanitization(self):
        import json
        import math
        import threading
        import time
        import urllib.request
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch
        import server

        cleaned = server.sanitize_json_value({"a": float("nan"), "b": [math.inf, 1.5]})
        self.assertIsNone(cleaned["a"])
        self.assertIsNone(cleaned["b"][0])
        self.assertEqual(cleaned["b"][1], 1.5)
        json.dumps(cleaned, allow_nan=False)

        with server._AUDIT_JOB_LOCK:
            server._AUDIT_JOB_STATE.update({
                "status": "idle",
                "running": False,
                "started_at": None,
                "finished_at": None,
                "error": None,
                "result": None,
            })

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ControlDashboardHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            fake_report = {
                "status": "ok",
                "run_at": "2026-11-16 13:35:00",
                "deterministic_audit": {"health_score": 100, "findings_count": 0, "findings": []},
                "ai_analysis": {"status": "skipped", "reason": "ok"},
                "patch_result": {"status": "skipped"},
                "gdrive": {"status": "skipped"},
                "bad_float": float("nan"),
            }
            with patch("daily_auto_audit_fixer.DailyAutoAuditFixer.run_daily_closed_loop", return_value=fake_report):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/audit/run",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 202)
                    start_data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(start_data["status"], "running")

                final_status = None
                for _ in range(30):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/audit/status", timeout=5) as sresp:
                        status_data = json.loads(sresp.read().decode("utf-8"))
                        if not status_data.get("running"):
                            final_status = status_data
                            break
                    time.sleep(0.05)

                self.assertIsNotNone(final_status)
                self.assertEqual(final_status["status"], "ok")
                self.assertIsNone(final_status["result"]["bad_float"])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_pair_executions_fifo_and_commission_inclusive_pnl(self):
        from live_ibkr_dual_engine import pair_executions_fifo

        sample_fills = [
            # Case A: Open SHORT 1 -> Close BOT 1 (IBKR realized_pnl=71.28 already includes open+close fees)
            {"exec_id": "e1", "timestamp": "2026-11-16 00:00:10", "symbol": "MES", "side": "SLD", "direction": "SHORT", "contracts": 1, "price": 7613.75, "realized_pnl": 0.0, "commission": 0.61},
            {"exec_id": "e2", "timestamp": "2026-11-16 00:05:10", "symbol": "MES", "side": "BOT", "direction": "LONG", "contracts": 1, "price": 7599.25, "realized_pnl": 71.28, "commission": 0.61},
            # Case B: Open SHORT 2 -> 2 partial Closes BOT 1 + BOT 1
            {"exec_id": "e3", "timestamp": "2026-11-16 01:00:00", "symbol": "MES", "side": "SLD", "direction": "SHORT", "contracts": 2, "price": 7611.50, "realized_pnl": 0.0, "commission": 1.22},
            {"exec_id": "e4", "timestamp": "2026-11-16 01:10:00", "symbol": "MES", "side": "BOT", "direction": "LONG", "contracts": 1, "price": 7598.25, "realized_pnl": 65.03, "commission": 0.61},
            {"exec_id": "e5", "timestamp": "2026-11-16 01:11:00", "symbol": "MES", "side": "BOT", "direction": "LONG", "contracts": 1, "price": 7598.25, "realized_pnl": 65.03, "commission": 0.61},
            # Case C: Same-second Close (realized!=0) + New Open (realized==0) at 04:23:47
            {"exec_id": "e6", "timestamp": "2026-11-16 03:50:28", "symbol": "MES", "side": "BOT", "direction": "LONG", "contracts": 2, "price": 7559.75, "realized_pnl": 0.0, "commission": 1.22},
            {"exec_id": "e7", "timestamp": "2026-11-16 04:23:47", "symbol": "MES", "side": "SLD", "direction": "SHORT", "contracts": 1, "price": 7566.50, "realized_pnl": 0.0, "commission": 0.61},
            {"exec_id": "e8", "timestamp": "2026-11-16 04:23:47", "symbol": "MES", "side": "SLD", "direction": "SHORT", "contracts": 2, "price": 7566.50, "realized_pnl": 65.06, "commission": 1.22},
        ]

        groups = pair_executions_fifo(sample_fills, fx_rate=7.78)
        self.assertEqual(len(groups), 4)

        # Latest group: open SHORT 1 @ 7566.50 (unclosed)
        g0 = groups[0]
        self.assertEqual(g0["status"], "OPEN")
        self.assertEqual(g0["direction"], "SHORT")
        self.assertEqual(g0["open_price"], 7566.50)
        self.assertEqual(g0["close_orders"], [])

        # Second latest: LONG 2 @ 7559.75 closed @ 7566.50 at same second (65.06)
        g1 = groups[1]
        self.assertEqual(g1["status"], "CLOSED")
        self.assertEqual(g1["open_price"], 7559.75)
        self.assertEqual(len(g1["close_orders"]), 1)
        self.assertEqual(g1["close_orders"][0]["close_price"], 7566.50)
        self.assertAlmostEqual(g1["total_pnl"], 65.06, places=2)

        # Third: SHORT 2 @ 7611.50 paired with 2 sub-closes -> 130.06
        g2 = groups[2]
        self.assertEqual(g2["status"], "CLOSED")
        self.assertEqual(g2["open_price"], 7611.50)
        self.assertEqual(len(g2["close_orders"]), 2)
        self.assertAlmostEqual(g2["total_pnl"], 130.06, places=2)

        # Fourth: SHORT 1 @ 7613.75 -> 7599.25 = 71.28
        g3 = groups[3]
        self.assertAlmostEqual(g3["total_pnl"], 71.28, places=2)

    def test_hkd_unified_pnl_status_export(self):
        import json
        import os
        import tempfile
        import live_ibkr_dual_engine as eng_mod

        eng = eng_mod.LiveIBKRDualEngine.__new__(eng_mod.LiveIBKRDualEngine)
        eng.is_connected = True
        eng.is_reconnecting = False
        eng.reconnect_attempts = 0
        eng.reconnect_delay = 5.0
        eng.account_id = "DU123456"
        eng.currency = "HKD"
        eng.usd_hkd_rate = 7.78
        eng.status_message = "OK"
        eng.market_paused = False
        eng.market_data_type = 1
        eng.market_data_status = "REAL_TIME"
        eng.net_liquidation = 1000000.0
        eng.cash_balance = 950000.0
        eng.available_funds = 900000.0
        eng.init_margin_req = 50000.0
        eng.realized_pnl = 0.0
        eng.unrealized_pnl = 0.0
        eng.equity_usd_total = 128534.7
        eng.pool_v43_usd = 77120.82
        eng.pool_v50_usd = 51413.88
        eng.symbol_mes = "MESZ6"
        eng.symbol_mnq = "MNQZ6"
        eng.trading_enabled_global = True
        eng.trading_enabled_v43 = True
        eng.trading_enabled_v50 = True
        eng.price_mes = 7570.0
        eng.bid_mes = 7569.75
        eng.ask_mes = 7570.25
        eng.price_mnq = 27900.0
        eng.bid_mnq = 27899.5
        eng.ask_mnq = 27900.5
        eng.engine_v91_mes = eng_mod.StrategyEngineV93(eng_mod.CONFIG_V93)
        eng.engine_v91_mnq = eng_mod.StrategyEngineV93(eng_mod.CONFIG_V93)

        pos_mes = eng._build_position_snapshot("MES", 1, "LONG", 7560.0 * 5.0)
        self.assertAlmostEqual(pos_mes["unrealized_pnl"], 50.0, places=2)
        self.assertAlmostEqual(pos_mes["unrealized_pnl_hkd"], 389.0, places=2)

        eng.get_real_positions = lambda: (pos_mes, None)
        eng.get_real_open_orders = lambda: ([], [])
        eng.get_real_executions = lambda: (
            [{"exec_id": "x1", "timestamp": "2026-11-17 10:00:00", "symbol": "MES", "side": "BOT", "direction": "LONG", "contracts": 1, "price": 7560.0, "realized_pnl": 0.0, "commission": 0.61}],
            [{"exec_id": "x2", "timestamp": "2026-11-17 11:00:00", "symbol": "MNQ", "side": "SLD", "direction": "SHORT", "contracts": 1, "price": 27900.0, "realized_pnl": 100.0, "commission": 0.61}],
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_status = os.path.join(tmp, "trade_status.json")
            orig_status_file = eng_mod.STATUS_FILE
            try:
                eng_mod.STATUS_FILE = tmp_status
                eng.write_status_file()
            finally:
                eng_mod.STATUS_FILE = orig_status_file

            with open(tmp_status, "r", encoding="utf-8") as f:
                data = json.load(f)

        self.assertAlmostEqual(data["unrealized_pnl"], 389.0, places=2)
        self.assertAlmostEqual(data["unrealized_pnl_hkd"], 389.0, places=2)
        self.assertAlmostEqual(data["realized_pnl"], 778.0, places=2)
        self.assertAlmostEqual(data["realized_pnl_hkd"], 778.0, places=2)
        self.assertEqual(len(data["paired_trades"]), 2)


if __name__ == "__main__":
    unittest.main()
