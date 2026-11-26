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
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from live_ibkr_dual_engine import LiveIBKRDualEngine, _IBKRWrapperNoiseFilter, Direction
from mes_strategy.strategy_models import TradeSignal, SignalType
from strategies.defense_champion_v9_3.strategy import StrategyEngineV93
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

    def test_bracket_outside_rth_true_and_distinct_refs(self):
        orders = self.eng._build_single_bracket("BUY", "SELL", 2, 7560.0, 7555.0, 7570.0, "V93_MES", "TP1")
        self.assertEqual(len(orders), 3)
        self.assertTrue(all(o.outsideRth is True for o in orders))
        self.assertEqual(orders[0].orderRef, "V93_MES_ENTRY_TP1")
        self.assertEqual(orders[1].orderRef, "V93_MES_SL_TP1")
        self.assertEqual(orders[2].orderRef, "V93_MES_TP_TP1")

    def test_soft_stop_failsafe_triggers_emergency_flat_after_dwell(self):
        flatted = []
        self.eng.emergency_flat_symbol = lambda sym, reason="": flatted.append((sym, reason)) or {"status": "ok"}
        self.eng._stop_breach_first_seen = {}
        self.eng._last_soft_stop_ts = {}

        # Case 1: Normal unbreached SHORT MES (SL=7416.50, current=7409.00) -> no trigger
        mes_ok = {"symbol": "MES", "direction": "SHORT", "contracts": 7, "entry_price": 7411.20, "current_price": 7409.00}
        self.eng.get_real_positions = lambda: (mes_ok, None)
        self.eng.ib.openTrades.return_value = [
            _make_trade(7623, "MES", "STP", "BUY", 7, 7416.50, "V93_MES_SL_RUNNER", status="PreSubmitted"),
        ]
        self.eng.ensure_position_protection()
        self.assertEqual(flatted, [])
        self.assertNotIn("MES", self.eng._stop_breach_first_seen)

        # Case 2: Market jumps to 7439.75 while STP stays PreSubmitted -> starts timer, flats after dwell >= 5s
        mes_breach = {"symbol": "MES", "direction": "SHORT", "contracts": 7, "entry_price": 7411.20, "current_price": 7439.75}
        self.eng.get_real_positions = lambda: (mes_breach, None)
        self.eng.ensure_position_protection()
        self.assertEqual(flatted, [])
        self.assertIn("MES", self.eng._stop_breach_first_seen)

        self.eng._stop_breach_first_seen["MES"] = time.time() - 6.0
        self.eng.ensure_position_protection()
        self.assertEqual(len(flatted), 1)
        self.assertEqual(flatted[0][0], "MES")
        self.eng.incident_mgr.record_incident.assert_called()
        self.assertEqual(self.eng.incident_mgr.record_incident.call_args.kwargs.get("code"), 7007)

        # Case 3: Naked position gets GUARD STP+TP with outsideRth=True
        self.eng.engine_v93_mes.calculate_tranches.return_value = (3, 2, 2)
        self.eng._fetch_latest_atr = lambda c, f=4.5: 5.0
        self.eng.ib.openTrades.return_value = []
        self.eng.ensure_position_protection()
        self.assertTrue(len(self.placed_orders) >= 2)
        self.assertTrue(all(o.outsideRth is True for _, o in self.placed_orders))

    def test_hmds_query_throttling_backoff_and_noise_filter(self):
        self.eng._last_eval_bucket = {}
        self.eng._hist_fail_streak = {}

        t0 = 1794920110.0  # cleanly inside a 5-min bucket (offset >= 4s)
        self.assertTrue(self.eng._can_query_hist_bar("MES", t0))

        # Same 5-min bucket already evaluated -> blocked until next 5-min boundary
        self.eng._last_eval_bucket["MES"] = int(t0 // 300)
        self.assertFalse(self.eng._can_query_hist_bar("MES", t0 + 30.0))
        next_bucket_early = (int(t0 // 300) + 1) * 300 + 2.0
        self.assertFalse(self.eng._can_query_hist_bar("MES", next_bucket_early))
        self.assertTrue(self.eng._can_query_hist_bar("MES", next_bucket_early + 3.0))

        # Exponential failure backoff: 60s -> 120s -> 240s -> 300s
        self.eng._last_eval_bucket = {}
        self.assertEqual(self.eng._record_hist_failure("MES", t0), 60.0)
        self.assertFalse(self.eng._can_query_hist_bar("MES", t0 + 30.0))
        self.assertTrue(self.eng._can_query_hist_bar("MES", t0 + 61.0))

        self.assertEqual(self.eng._record_hist_failure("MES", t0 + 61.0), 120.0)
        self.assertFalse(self.eng._can_query_hist_bar("MES", t0 + 150.0))
        self.assertEqual(self.eng._record_hist_failure("MES", t0 + 185.0), 240.0)
        self.assertEqual(self.eng._record_hist_failure("MES", t0 + 430.0), 300.0)

        # Noise filter drops benign 162 cancellation flood, keeps IP conflict & real errors
        flt = _IBKRWrapperNoiseFilter()
        rec_cancel = logging.LogRecord(
            "ib_async.wrapper", logging.ERROR, "", 0,
            "Error 162, reqId 7840: Historical Market Data Service error message:API historical data query cancelled: 7840",
            (), None
        )
        rec_ip = logging.LogRecord(
            "ib_async.wrapper", logging.ERROR, "", 0,
            "Error 162, reqId 12: Trading TWS session is connected from a different IP address",
            (), None
        )
        self.assertFalse(flt.filter(rec_cancel))
        self.assertTrue(flt.filter(rec_ip))

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

    def test_partial_fill_grace_preserves_pending_entry_siblings_then_cleans_tail(self):
        mes_pos = {"symbol": "MES", "direction": "SHORT", "contracts": 3, "entry_price": 7411.25, "current_price": 7410.0}
        self.eng.get_real_positions = lambda: (mes_pos, None)
        trades = [
            _make_trade(105, "MES", "STP", "BUY", 3, 7416.50, "V93_MES_SL_TP1", parent_id=101, status="Submitted"),
            _make_trade(102, "MES", "LMT", "SELL", 3, 7411.25, "V93_MES_ENTRY_TP2", parent_id=0, status="Submitted"),
            _make_trade(103, "MES", "STP", "BUY", 3, 7416.50, "V93_MES_SL_TP2", parent_id=102, status="PreSubmitted"),
            _make_trade(104, "MES", "LMT", "BUY", 3, 7395.25, "V93_MES_TP_TP2", parent_id=102, status="PreSubmitted"),
        ]
        self.eng.ib.openTrades.return_value = trades
        # Within 15s partial-fill grace window: neither Parent 2 (102) nor SL1/SL2 should be cancelled
        self.eng.ensure_position_protection()
        self.assertEqual(self.cancelled_ids, [])

        # Past 15s grace window: only Parent 2 tail (102) is cancelled cleanly (code 7008), active SL1 (105) remains
        self.eng._entry_order_first_seen[102] = time.time() - 16.0
        self.eng.ensure_position_protection()
        self.assertEqual(self.cancelled_ids, [102])
        self.assertEqual(self.eng.incident_mgr.record_incident.call_args.kwargs.get("code"), 7008)

    def test_margin_guard_checks_both_symbols_and_uses_live_fx(self):
        self.eng.account_id = "DU999999"
        self.eng.currency = "HKD"
        self.eng.usd_hkd_rate = 7.85
        self.eng.pool_v43_usd = 20000.0
        self.eng.engine_v93_mes.calculate_tranches.return_value = (1, 1, 0)

        sig = SimpleNamespace(
            direction=Direction.LONG,
            contracts=5,
            entry_price=7500.0,
            sl_price=7495.0,
            tp1_price=7507.75,
            tp2_price=7515.50,
            runner_price=None,
            split_lots=None,
        )

        # net_liquidation <= 0 blocks new entries
        self.eng.net_liquidation = 0.0
        self.eng.available_funds = 30000.0
        self.eng.place_v93_bracket_order("MES", sig)
        self.assertEqual(self.placed_orders, [])

        # With live FX 7.85 and 30,000 HKD (~$3,821 USD) available, 5 MES gets downsized to 2 lots (1800 * 2 <= 3821)
        self.eng.net_liquidation = 250000.0
        self.eng.available_funds = 30000.0
        self.eng._force_golden_window = True
        self.eng.place_v93_bracket_order("MES", sig)
        self.assertEqual(sig.contracts, 2)
        self.assertEqual(len(self.placed_orders), 6)  # 2 tranches * 3 orders, runner_price=None safe

    def test_sync_account_and_market_ledger_pnl_alignment(self):
        """驗證官方 $LEDGER-RealizedPnL 與 $LEDGER-FuturesPNL 正確同步至港幣與美金"""
        self.eng.account_id = "DU999999"
        self.eng.ib.isConnected.return_value = True
        self.eng.ib.accountValues.return_value = [
            SimpleNamespace(tag="NetLiquidation", value="1044882.29", currency="HKD"),
            SimpleNamespace(tag="$LEDGER-RealizedPnL", value="15986.40", currency="BASE"),
            SimpleNamespace(tag="$LEDGER-RealizedPnL", value="2039.30", currency="USD"),
            SimpleNamespace(tag="$LEDGER-FuturesPNL", value="15599.93", currency="BASE"),
            SimpleNamespace(tag="$LEDGER-FuturesPNL", value="1990.00", currency="USD"),
            SimpleNamespace(tag="$LEDGER-ExchangeRate", value="7.8392", currency="USD"),
            SimpleNamespace(tag="RealizedPnL", value="0.00", currency="HKD"),
        ]
        self.eng.ib.pnl.return_value = []
        self.eng.sync_account_and_market()

        self.assertAlmostEqual(self.eng.realized_pnl_hkd, 15986.40, places=2)
        self.assertAlmostEqual(self.eng.realized_pnl_usd, 2039.30, places=2)
        self.assertAlmostEqual(self.eng.futures_pnl_hkd, 15599.93, places=2)
        self.assertAlmostEqual(self.eng.futures_pnl_usd, 1990.00, places=2)
        self.assertAlmostEqual(self.eng.realized_pnl, 15986.40, places=2)
        self.assertAlmostEqual(self.eng.usd_hkd_rate, 7.8392, places=4)

        # 驗證輸出至 status 檔案時雙幣與純利對齊
        import live_ibkr_dual_engine as eng_mod
        import json
        self.eng.is_connected = True
        self.eng.status_message = "OK"
        self.eng.market_paused = False
        self.eng.symbol_mes = "MESZ6"
        self.eng.symbol_mnq = "MNQZ6"
        self.eng.trading_enabled_global = True
        self.eng.trading_enabled_v43 = True
        self.eng.trading_enabled_v50 = True
        self.eng.get_real_positions = lambda: (None, None)
        self.eng.get_real_open_orders = lambda: ([], [])
        self.eng.get_real_executions = lambda: ([], [])
        self.eng._merge_with_historical_trades = lambda trades: []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_status = os.path.join(tmp, "trade_status.json")
            orig_status = eng_mod.STATUS_FILE
            try:
                eng_mod.STATUS_FILE = tmp_status
                self.eng.write_status_file()
                with open(tmp_status, "r", encoding="utf-8") as f:
                    st_data = json.load(f)
                self.assertAlmostEqual(st_data["realized_pnl_hkd"], 15986.40, places=2)
                self.assertAlmostEqual(st_data["realized_pnl_usd"], 2039.30, places=2)
                self.assertAlmostEqual(st_data["futures_pnl_hkd"], 15599.93, places=2)
                self.assertAlmostEqual(st_data["futures_pnl_usd"], 1990.00, places=2)
            finally:
                eng_mod.STATUS_FILE = orig_status

    def test_emergency_flat_sets_market_order_outside_rth(self):
        self.eng.account_id = "DU999999"
        self.eng.usd_hkd_rate = 7.78
        self.eng.currency = "HKD"
        self.eng.price_mes = 7415.0
        self.eng.contract_mes = SimpleNamespace(symbol="MES", conId=815824257)
        self.eng.contract_mnq = SimpleNamespace(symbol="MNQ", conId=815824267)
        self.eng.ib.openTrades.return_value = []
        self.eng.ib.positions.return_value = [
            SimpleNamespace(contract=SimpleNamespace(symbol="MES", conId=815824257), position=-2, avgCost=37050.0)
        ]
        self.eng.write_status_file = lambda: None
        self.eng.sync_account_and_market = lambda: None

        res = self.eng.emergency_flat_symbol("MES", reason="測試盤外平倉")
        self.assertEqual(res.get("status"), "ok")
        self.assertTrue(res.get("executed"))
        flat_orders = [o for sym, o in self.placed_orders if sym == "MES"]
        self.assertEqual(len(flat_orders), 1)
        self.assertIs(flat_orders[0].outsideRth, True)
        self.assertEqual(flat_orders[0].tif, "GTC")

    def test_live_breakeven_and_trailing_stop_moves_monotonically(self):
        self.eng._active_trade_meta = {
            "MES": {
                "direction": "LONG",
                "entry": 7500.0,
                "init_sl": 7490.0,
                "sl_dist": 10.0,
                "tp1": 7515.50,
                "tp2": 7531.00,
                "be_active": False,
                "trailing_active": False,
            }
        }
        self.eng._last_sl_modify_ts = {}
        sl_trade = _make_trade(888, "MES", "STP", "SELL", 1, 7490.0, "V93_MES_SL_RUNNER")
        self.eng.ib.openTrades.return_value = [sl_trade]

        # At TP1 (7516.00 >= 7515.50): move SL to BE +0.25 -> 7500.25
        mes_tp1 = {"symbol": "MES", "direction": "LONG", "contracts": 1, "entry_price": 7500.0, "current_price": 7516.0}
        self.eng.get_real_positions = lambda: (mes_tp1, None)
        self.eng.ensure_position_protection()
        self.assertEqual(sl_trade.order.auxPrice, 7500.25)
        self.assertIs(sl_trade.order.transmit, True)
        self.assertTrue(self.eng._active_trade_meta["MES"]["be_active"])

        enriched = self.eng._enrich_pos_with_orders(dict(mes_tp1), [], self.eng._active_trade_meta.get("MES"))
        self.assertTrue(enriched.get("is_trailing_active"))

        # Past TP2 (7541.00 >= 7531.00): SL trails to 7515.50 + (7541 - 7531)*0.5 = 7520.50
        self.eng._last_sl_modify_ts["MES"] = time.time() - 12.0
        mes_tp2 = {"symbol": "MES", "direction": "LONG", "contracts": 1, "entry_price": 7500.0, "current_price": 7541.0}
        self.eng.get_real_positions = lambda: (mes_tp2, None)
        self.eng.ensure_position_protection()
        self.assertEqual(sl_trade.order.auxPrice, 7520.50)

        # Pullback to 7535.00: trailing SL NEVER steps backwards
        self.eng._last_sl_modify_ts["MES"] = time.time() - 12.0
        mes_pullback = {"symbol": "MES", "direction": "LONG", "contracts": 1, "entry_price": 7500.0, "current_price": 7535.0}
        self.eng.get_real_positions = lambda: (mes_pullback, None)
        self.eng.ensure_position_protection()
        self.assertEqual(sl_trade.order.auxPrice, 7520.50)


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

    def test_build_daily_pnl_calendar_and_endpoint(self):
        import json
        import os
        import tempfile
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        import server

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            rep_dir = os.path.join(tmp, "reports")
            os.makedirs(data_dir, exist_ok=True)
            os.makedirs(rep_dir, exist_ok=True)

            # 11-12 由 daily_report 補齊 (63.78 USD -> 496.21 HKD)
            with open(os.path.join(rep_dir, "daily_report_2026-11-12.json"), "w", encoding="utf-8") as rf:
                json.dump({
                    "report_date": "2026-11-12",
                    "pnl_performance": {"total_trades": 1, "win_count": 1, "loss_count": 0, "total_realized_pnl": 63.78},
                    "strategy_breakdown": {"v43_mes": {"realized_pnl": 63.78}, "v50_mnq": {"realized_pnl": 0.0}},
                }, rf)

            # 11-16 由 trades.jsonl 配對 (+65.06 USD -> +506.17 HKD)
            rows = [
                {"exec_id": "c1", "timestamp": "2026-11-16 03:50:28", "symbol": "MES", "side": "BOT", "direction": "LONG", "shares": 2, "price": 7559.75, "realized_pnl": 0.0, "commission": 1.22},
                {"exec_id": "c2", "timestamp": "2026-11-16 04:23:47", "symbol": "MES", "side": "SLD", "direction": "SHORT", "shares": 2, "price": 7566.50, "realized_pnl": 65.06, "commission": 1.22},
            ]
            with open(os.path.join(data_dir, "trades.jsonl"), "w", encoding="utf-8") as tf:
                for r in rows:
                    tf.write(json.dumps(r) + "\n")

            with open(os.path.join(tmp, "trade_status.json"), "w", encoding="utf-8") as sf:
                json.dump({"fx_rate_usd_hkd": 7.78, "recent_trades": []}, sf)

            cal = server.build_daily_pnl_calendar(month_str="2026-11", base_dir=tmp)
            self.assertEqual(cal["month"], "2026-11")
            self.assertIn("2026-11-12", cal["days"])
            self.assertIn("2026-11-16", cal["days"])
            self.assertAlmostEqual(cal["days"]["2026-11-12"]["pnl_hkd"], 496.21, places=2)
            self.assertAlmostEqual(cal["days"]["2026-11-16"]["pnl_hkd"], 506.17, places=2)
            self.assertAlmostEqual(cal["monthly_summary"]["net_pnl_hkd"], 1002.38, places=2)
            self.assertEqual(cal["monthly_summary"]["win_days"], 2)

        # 驗證真實專案下的 HTTP /api/pnl/calendar 端點可回傳標準 JSON
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ControlDashboardHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/pnl/calendar?month=2026-11", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(payload["status"], "ok")
                self.assertEqual(payload["month"], "2026-11")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_incident_ack_resets_unread_count_via_http(self):
        import json
        import os
        import tempfile
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        import report_and_incident_manager as rim
        import server

        with tempfile.TemporaryDirectory() as tmp:
            orig_jsonl = rim.INCIDENTS_JSONL
            orig_ack = rim.INCIDENTS_ACK_FILE
            orig_log = rim.INCIDENTS_LOG
            rim.INCIDENTS_JSONL = os.path.join(tmp, "incidents.jsonl")
            rim.INCIDENTS_ACK_FILE = os.path.join(tmp, "incidents_ack.json")
            rim.INCIDENTS_LOG = os.path.join(tmp, "incidents.log")

            try:
                mgr = rim.IncidentManager()
                mgr.record_incident("WARNING", "CONNECTION", 1100, "test disconnect 1")
                mgr.record_incident("ERROR", "TRADING", 201, "test order reject 2")

                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ControlDashboardHandler)
                port = httpd.server_address[1]
                t = threading.Thread(target=httpd.serve_forever, daemon=True)
                t.start()
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/incidents?limit=10", timeout=5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        self.assertEqual(data["summary"]["total_count"], 2)
                        self.assertEqual(data["summary"]["unread_count"], 2)
                        self.assertTrue(data["summary"]["unread_has_error"])

                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/incidents/ack",
                        data=json.dumps({"total_count": 2}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        ack_data = json.loads(resp.read().decode("utf-8"))
                        self.assertEqual(ack_data["status"], "ok")
                        self.assertEqual(ack_data["summary"]["unread_count"], 0)
                        self.assertEqual(ack_data["summary"]["total_count"], 2)
                        self.assertFalse(ack_data["summary"]["unread_has_error"])

                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/incidents?limit=1", timeout=5) as resp:
                        polled = json.loads(resp.read().decode("utf-8"))
                        self.assertEqual(polled["summary"]["unread_count"], 0)
                        self.assertEqual(polled["summary"]["total_count"], 2)

                    mgr.record_incident("CRITICAL", "MARGIN", 4002, "test new incident 3")
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/incidents?limit=1", timeout=5) as resp:
                        polled2 = json.loads(resp.read().decode("utf-8"))
                        self.assertEqual(polled2["summary"]["total_count"], 3)
                        self.assertEqual(polled2["summary"]["unread_count"], 1)
                        self.assertTrue(polled2["summary"]["unread_has_critical"])
                finally:
                    httpd.shutdown()
                    httpd.server_close()
            finally:
                rim.INCIDENTS_JSONL = orig_jsonl
                rim.INCIDENTS_ACK_FILE = orig_ack
                rim.INCIDENTS_LOG = orig_log

    def test_get_version_info_and_endpoint(self):
        import json
        import os
        import tempfile
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        import server

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "VERSION"), "w", encoding="utf-8") as vf:
                vf.write("9.3.3\n")

            same = server.get_version_info(base_dir=tmp, check_remote=True, remote_fetcher=lambda: "9.3.3\n")
            self.assertEqual(same["version"], "9.3.3")
            self.assertEqual(same["tag"], "v9.3.3")
            self.assertFalse(same["update_available"])

            newer = server.get_version_info(base_dir=tmp, check_remote=True, remote_fetcher=lambda: "9.3.4\n")
            self.assertEqual(newer["latest_tag"], "v9.3.4")
            self.assertTrue(newer["update_available"])

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ControlDashboardHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(payload["status"], "ok")
                self.assertTrue(payload["version"])
                self.assertIn("./update.sh", payload["update_command"])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_overnight_pairing_from_jsonl_and_orphan_open_timestamp(self):
        import json
        import os
        import tempfile
        import live_ibkr_dual_engine as eng_mod

        # 1. 真孤立平倉單不得把平倉時間偽造成開倉時間
        orphan = eng_mod.pair_executions_fifo([
            {"exec_id": "oc1", "timestamp": "2026-11-17 09:03:43", "symbol": "MNQ", "side": "BOT", "direction": "LONG", "contracts": 1, "price": 27161.25, "realized_pnl": 416.78, "commission": 0.61}
        ], fx_rate=7.78)
        self.assertEqual(len(orphan), 1)
        self.assertEqual(orphan[0]["status"], "ORPHAN_CLOSE")
        self.assertEqual(orphan[0]["open_timestamp"], "")
        self.assertEqual(orphan[0]["close_orders"][0]["timestamp"], "2026-11-17 09:03:43")

        # 2. 跨日平倉應自動與 data/trades.jsonl 的前日開倉紀錄配對，各自顯示開倉與平倉時間
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
        eng.price_mes = 7408.75
        eng.bid_mes = 7408.5
        eng.ask_mes = 7409.0
        eng.price_mnq = 27161.25
        eng.bid_mnq = 27161.0
        eng.ask_mnq = 27161.5
        eng.engine_v91_mes = eng_mod.StrategyEngineV93(eng_mod.CONFIG_V93)
        eng.engine_v91_mnq = eng_mod.StrategyEngineV93(eng_mod.CONFIG_V93)
        eng.get_real_positions = lambda: (None, None)
        eng.get_real_open_orders = lambda: ([], [])
        eng.get_real_executions = lambda: (
            [],
            [{"exec_id": "close_today", "timestamp": "2026-11-17 09:03:43", "symbol": "MNQ", "side": "BOT", "direction": "LONG", "contracts": 1, "price": 27161.25, "realized_pnl": 416.78, "commission": 0.61}],
        )

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            with open(os.path.join(tmp, "data", "trades.jsonl"), "w", encoding="utf-8") as tf:
                tf.write(json.dumps({
                    "exec_id": "open_yest",
                    "timestamp": "2026-11-16 19:08:42",
                    "symbol": "MNQ",
                    "side": "SLD",
                    "direction": "SHORT",
                    "shares": 1,
                    "price": 27370.25,
                    "realized_pnl": 0.0,
                    "commission": 0.61,
                }) + "\n")

            tmp_status = os.path.join(tmp, "trade_status.json")
            orig_status = eng_mod.STATUS_FILE
            try:
                eng_mod.STATUS_FILE = tmp_status
                eng.write_status_file()
            finally:
                eng_mod.STATUS_FILE = orig_status

            with open(tmp_status, "r", encoding="utf-8") as f:
                data = json.load(f)

        self.assertEqual(len(data["paired_trades"]), 1)
        grp = data["paired_trades"][0]
        self.assertEqual(grp["status"], "CLOSED")
        self.assertEqual(grp["open_timestamp"], "2026-11-16 19:08:42")
        self.assertEqual(grp["open_price"], 27370.25)
        self.assertEqual(grp["close_orders"][0]["timestamp"], "2026-11-17 09:03:43")
        self.assertAlmostEqual(grp["total_pnl"], 416.78, places=2)

    def test_telegram_send_retries_after_connection_reset(self):
        import telegram_notifier as tg_mod

        notifier = tg_mod.TelegramNotifier()
        orig_token, orig_chat_id = notifier.bot_token, notifier.chat_id
        notifier.bot_token = "test-token"
        notifier.chat_id = "123456"

        class _FakeResp:
            def __init__(self, body: bytes):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        calls = {"n": 0}

        def _fake_urlopen(req, timeout=0):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError(104, "Connection reset by peer")
            return _FakeResp(b'{"ok": true}')

        try:
            with unittest.mock.patch.object(tg_mod.urllib.request, "urlopen", side_effect=_fake_urlopen), \
                    unittest.mock.patch.object(tg_mod.time, "sleep") as sleep_mock:
                ok = notifier.send_message_sync("hello")
                self.assertTrue(ok)
                self.assertEqual(calls["n"], 2)
                sleep_mock.assert_called_once()

                calls["n"] = 0
                ok_tunnel = notifier.notify_tunnel_ready("https://example.trycloudflare.com", "PIN123", sync=True)
                self.assertTrue(ok_tunnel)
                self.assertEqual(calls["n"], 2)
        finally:
            notifier.bot_token = orig_token
            notifier.chat_id = orig_chat_id

    def test_competing_session_10197_throttles_halts_opens_and_recovers(self):
        import tempfile
        import types
        import report_and_incident_manager as rim
        import live_ibkr_dual_engine as eng_mod

        with tempfile.TemporaryDirectory() as tmp:
            orig_jsonl = rim.INCIDENTS_JSONL
            orig_log = rim.INCIDENTS_LOG
            rim.INCIDENTS_JSONL = os.path.join(tmp, "incidents.jsonl")
            rim.INCIDENTS_LOG = os.path.join(tmp, "incidents.log")
            try:
                mgr = rim.IncidentManager()
                mgr._rate_limit_ts = {}
                with unittest.mock.patch.object(mgr, "record_incident", wraps=mgr.record_incident) as rec_mock:
                    mgr.handle_ibkr_error_event(1, 10197, "No market data during competing live session")
                    mgr.handle_ibkr_error_event(2, 10197, "No market data during competing live session")
                    self.assertEqual(rec_mock.call_count, 1)
                    self.assertIn("10197", rec_mock.call_args.kwargs["message"])

                eng = eng_mod.LiveIBKRDualEngine.__new__(eng_mod.LiveIBKRDualEngine)
                eng.incident_mgr = mgr
                eng.account_id = "DU123456"
                eng.currency = "HKD"
                eng.usd_hkd_rate = 7.78
                eng.net_liquidation = 1000000.0
                eng.cash_balance = 950000.0
                eng.available_funds = 900000.0
                eng.init_margin_req = 50000.0
                eng.realized_pnl = 0.0
                eng.unrealized_pnl = 0.0
                eng.last_snapshot_time = time.time()
                eng.market_data_type = 1
                eng.market_data_status = "REAL_TIME"
                eng._competing_session_ts = 0.0
                eng._last_10197_log_ts = 0.0
                eng._last_live_quote_sig = None
                eng.price_mes = 7400.0
                eng.bid_mes = 7399.75
                eng.ask_mes = 7400.25
                eng.price_mnq = 27100.0
                eng.bid_mnq = 27099.75
                eng.ask_mnq = 27100.25

                class _FakeTicker:
                    def __init__(self, px):
                        self.px = px
                        self.last = px
                        self.close = px
                        self.bid = px - 0.25
                        self.ask = px + 0.25
                        self.marketDataType = 1

                    def marketPrice(self):
                        return self.px

                eng.ticker_mes = _FakeTicker(7400.0)
                eng.ticker_mnq = _FakeTicker(27100.0)

                hist_calls = {"n": 0}
                eng.ib = types.SimpleNamespace(
                    isConnected=lambda: True,
                    accountValues=lambda acc: [],
                    accountSummary=lambda: [],
                    pnl=lambda: [],
                    reqHistoricalData=lambda *a, **kw: hist_calls.__setitem__("n", hist_calls["n"] + 1) or [],
                )
                eng.ledger_mgr = types.SimpleNamespace(record_equity_snapshot=lambda *a, **kw: None)

                eng.on_ibkr_error(1, 10197, "No market data during competing live session")
                self.assertEqual(eng.market_data_status, "COMPETING_SESSION")

                # Sync with stale identical quotes keeps COMPETING_SESSION even though ticker.marketDataType == 1
                eng.sync_account_and_market()
                self.assertEqual(eng.market_data_status, "COMPETING_SESSION")

                # New openings halted during COMPETING_SESSION
                eng._evaluate_symbol_bar("MES", object(), object(), 50000.0, 5.0, None, [])
                self.assertEqual(hist_calls["n"], 0)

                # Once competing session closes and live quotes advance (>2s later), status recovers to REAL_TIME
                eng._competing_session_ts = time.time() - 5.0
                eng.ticker_mes.px = 7401.5
                eng.ticker_mes.last = 7401.5
                eng.sync_account_and_market()
                self.assertEqual(eng.market_data_status, "REAL_TIME")
            finally:
                rim.INCIDENTS_JSONL = orig_jsonl
                rim.INCIDENTS_LOG = orig_log

    def test_hmds_prolonged_failure_triggers_reconnect_and_5m_scan_logs(self):
        import types
        import pandas as pd
        import live_ibkr_dual_engine as eng_mod

        eng = eng_mod.LiveIBKRDualEngine.__new__(eng_mod.LiveIBKRDualEngine)
        eng._hist_fail_streak = {}
        eng._last_hist_fail_ts = {}
        eng._last_eval_bar_ts = {}
        eng._last_eval_bucket = {}
        eng._last_hmds_recovery_ts = 0.0
        eng._last_scan_log_ts = {}
        eng.market_data_status = "REAL_TIME"
        eng.net_liquidation = 1000000.0
        eng.is_connected = True
        eng.is_reconnecting = False
        disconnected = {"called": 0}
        eng.ib = types.SimpleNamespace(
            isConnected=lambda: True,
            disconnect=lambda: disconnected.__setitem__("called", disconnected["called"] + 1),
        )

        t0 = 1_700_000_000.0
        for _ in range(3):
            eng._record_hist_failure("MES", t0)
            eng._record_hist_failure("MNQ", t0)
        self.assertEqual(disconnected["called"], 0)

        eng._record_hist_failure("MES", t0 + 10.0)
        self.assertEqual(disconnected["called"], 0)
        eng._record_hist_failure("MNQ", t0 + 10.0)
        self.assertEqual(disconnected["called"], 1)
        self.assertFalse(eng.is_connected)
        self.assertTrue(eng.is_reconnecting)

        # Verify _evaluate_symbol_bar triggers real V9.3 R1 SHORT signal when bar pulls back to 5m EMA20,
        # and logs diagnostic blocker summary when no signal triggers
        dates = pd.date_range("2026-11-18 00:00", periods=60, freq="5min", tz="UTC")
        closes = [7450.0 - i * 1.0 for i in range(60)]
        bars_no_sig = [
            {"date": d, "open": c + 0.5, "high": c + 0.75, "low": c - 0.5, "close": c, "volume": 100}
            for d, c in zip(dates, closes)
        ]
        # Downtrend where price never pulls up to ema20 -> no R1 short, ADX 44.2 > 30 blocks R2
        placed = []
        eng.is_connected = True
        eng.ib = types.SimpleNamespace(
            isConnected=lambda: True,
            reqHistoricalData=lambda *a, **kw: bars_no_sig,
        )
        eng.get_4h_macro_regime = lambda *a, **kw: {
            "long": False, "short": True, "e20": 7467.0, "e50": 7525.0, "e200": 7635.0, "c": 7412.5, "adx_1h": 44.2
        }
        eng.place_v93_bracket_order = lambda sym, sig: placed.append((sym, sig))
        v93 = eng_mod.StrategyEngineV93(eng_mod.CONFIG_V93)

        with unittest.mock.patch.object(eng_mod, "calculate_atr", wraps=eng_mod.calculate_atr), \
                self.assertLogs("DualEngineLive", level="INFO") as cm:
            eng._can_query_hist_bar = lambda sym, ts: True
            eng._evaluate_symbol_bar("MES", object(), v93, 60000.0, 4.5, None, [])
        self.assertEqual(len(placed), 0)
        self.assertTrue(any("5m掃描" in line and "1H_ADX(44.2)>30禁R2逆勢" in line for line in cm.output))

        # Now make bar[-2] cleanly pull back up to 5m EMA20 and close bearish below EMA20 -> must fire R1 SHORT
        bars_r1_short = list(bars_no_sig)
        last_close = bars_r1_short[-2]["close"]
        bars_r1_short[-2] = {
            "date": dates[-2],
            "open": last_close + 15.0,
            "high": last_close + 25.0,
            "low": last_close - 1.0,
            "close": last_close,
            "volume": 200,
        }
        eng._last_eval_bar_ts = {}
        eng.ib = types.SimpleNamespace(
            isConnected=lambda: True,
            reqHistoricalData=lambda *a, **kw: bars_r1_short,
        )
        eng._evaluate_symbol_bar("MES", object(), v93, 60000.0, 4.5, None, [])
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0][0], "MES")
        self.assertEqual(str(placed[0][1].direction).upper().endswith("SHORT"), True)


class TestProductionOptimizationsP0P2(unittest.TestCase):
    def test_p0_mnq_stop_loss_harmonization(self):
        eng = object.__new__(LiveIBKRDualEngine)
        eng.contract_mnq = SimpleNamespace(symbol="MNQ")
        eng.contract_mes = SimpleNamespace(symbol="MES")
        eng.incident_mgr = MagicMock()
        eng.engine_v93_mes = MagicMock()
        eng.engine_v93_mnq = MagicMock()
        eng.ib = MagicMock()
        eng.ib.isConnected = lambda: True
        eng._last_protect_ts = {}
        eng._stop_breach_first_seen = {}
        eng._last_soft_stop_ts = {}
        eng._warned_uncancellable_ids = set()
        eng._cancel_attempt_ts = {}
        eng._round_to_tick = LiveIBKRDualEngine._round_to_tick

        # 2 MNQ child stops with 27 pts spread (21000.25 vs 20973.25)
        sl1 = _make_trade(101, "MNQ", "STP", "SELL", 1, 21000.25, "_SL_TP1")
        sl2 = _make_trade(102, "MNQ", "STP", "SELL", 1, 20973.25, "_SL_TP2")
        eng.get_real_positions = lambda: (None, {"contracts": 2, "direction": "LONG", "entry_price": 21000.0, "current_price": 21050.0})
        eng._get_symbol_open_trades = lambda sym: [sl1, sl2] if sym == "MNQ" else []
        eng._active_trade_meta = {"MNQ": {"direction": "LONG", "current_sl": 21000.25, "be_active": True}}

        eng.ensure_position_protection()

        # Must record incident 7011 for harmonization
        rec_calls = eng.incident_mgr.record_incident.call_args_list
        has_7011 = any(c.kwargs.get("code") == 7011 for c in rec_calls)
        self.assertTrue(has_7011, "Incident 7011 should be recorded for stop loss harmonization")

        # Order 102 should be updated to target_sl (21000.25)
        self.assertEqual(sl2.order.auxPrice, 21000.25)

    def test_p0_bracket_pre_order_min_sl_floor(self):
        eng = object.__new__(LiveIBKRDualEngine)
        eng.ib = MagicMock()
        eng.ib.isConnected = lambda: True
        eng.contract_mnq = SimpleNamespace(symbol="MNQ")
        eng._apply_live_margin_guard = lambda *a: True
        eng.engine_v93_mnq = StrategyEngineV93(symbol="MNQ")
        eng._active_trade_meta = {}
        eng._order_rules = {}
        eng._order_kinds = {}
        eng._order_exit_reasons = {}
        eng._round_to_tick = LiveIBKRDualEngine._round_to_tick

        bracket_mock = [MagicMock(orderId=1), MagicMock(orderId=2), MagicMock(orderId=3)]
        eng._build_single_bracket = MagicMock(return_value=bracket_mock)

        # Signal with narrow stop of only 5 pts on MNQ (less than 18.0 pts floor)
        sig = SimpleNamespace(
            direction=Direction.LONG,
            signal_type=SignalType.TREND_PULLBACK_WAVE3,
            entry_price=21000.0,
            sl_price=20995.0,
            tp1_price=21020.0,
            tp2_price=21050.0,
            contracts=1,
            rule_code="R1"
        )

        eng.place_v93_bracket_order("MNQ", sig)

        meta = eng._active_trade_meta.get("MNQ")
        self.assertIsNotNone(meta)
        # Should be adjusted to 21000.0 - 18.0 = 20982.0
        self.assertEqual(meta["init_sl"], 20982.0)
        self.assertEqual(meta["current_sl"], 20982.0)

    def test_p0_soft_stop_watchdog_does_not_prematurely_dump(self):
        eng = object.__new__(LiveIBKRDualEngine)
        eng.contract_mnq = SimpleNamespace(symbol="MNQ")
        eng.contract_mes = SimpleNamespace(symbol="MES")
        eng.incident_mgr = MagicMock()
        eng.engine_v93_mes = MagicMock()
        eng.engine_v93_mnq = MagicMock()
        eng.ib = MagicMock()
        eng.ib.isConnected = lambda: True
        eng._last_protect_ts = {}
        eng._stop_breach_first_seen = {}
        eng._last_soft_stop_ts = {}
        eng.emergency_flat_symbol = MagicMock()
        eng._round_to_tick = LiveIBKRDualEngine._round_to_tick

        # Long position: stop at 20973.0. Current market pulls back to 20990.0 (above stop)
        sl1 = _make_trade(201, "MNQ", "STP", "SELL", 2, 20973.0, "_SL")
        eng.get_real_positions = lambda: (None, {"contracts": 2, "direction": "LONG", "entry_price": 21000.0, "current_price": 20990.0})
        eng._get_symbol_open_trades = lambda sym: [sl1] if sym == "MNQ" else []

        eng.ensure_position_protection()
        self.assertEqual(eng.emergency_flat_symbol.call_count, 0)

        # Now simulate market dropping to 20970.0 (below 20973.0 - 1.00 tolerance) for > 5 seconds
        eng.get_real_positions = lambda: (None, {"contracts": 2, "direction": "LONG", "entry_price": 21000.0, "current_price": 20970.0})
        eng._stop_breach_first_seen["MNQ"] = time.time() - 6.0

        eng.ensure_position_protection()
        self.assertEqual(eng.emergency_flat_symbol.call_count, 1)
        self.assertIn("軟體止蝕防穿倉保護", eng.emergency_flat_symbol.call_args[1].get("reason", ""))

    def test_p0_eod_square_off(self):
        eng = object.__new__(LiveIBKRDualEngine)
        eng.ib = MagicMock()
        eng.ib.isConnected = lambda: True
        eng.incident_mgr = MagicMock()
        eng.emergency_flat_symbol = MagicMock()
        eng._trim_position_to_one = MagicMock()
        eng._get_symbol_open_trades = lambda sym: []
        eng._eod_squared_today = False

        from datetime import datetime
        from zoneinfo import ZoneInfo
        from unittest.mock import patch

        # 1. Wednesday 16:52 ET -> 週一至週四不強制清倉，部位繼續運行原生硬止損
        fake_wed_1652 = datetime(2026, 11, 18, 16, 52, 0, tzinfo=ZoneInfo("America/New_York"))
        eng.get_real_positions = lambda: ({"contracts": 2, "unrealized_pnl": 150.0}, {"contracts": 2, "unrealized_pnl": -80.0})

        with patch("live_ibkr_dual_engine.datetime") as mock_dt:
            mock_dt.now.return_value = fake_wed_1652
            eng.check_eod_square_off()

        self.assertEqual(eng.emergency_flat_symbol.call_count, 0)
        self.assertEqual(eng._trim_position_to_one.call_count, 0)
        self.assertFalse(eng._eod_squared_today)

        # 2. Friday 16:52 ET -> 週五收市保護：
        #    MES 浮盈 2 口 -> 減倉 1 口 (保留 1 口底倉過週末)
        #    MNQ 浮虧 2 口 -> 市價全平清倉防週末跳空
        fake_fri_1652 = datetime(2026, 11, 20, 16, 52, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("live_ibkr_dual_engine.datetime") as mock_dt:
            mock_dt.now.return_value = fake_fri_1652
            eng.check_eod_square_off()

        self.assertEqual(eng._trim_position_to_one.call_count, 1)
        self.assertEqual(eng._trim_position_to_one.call_args[0][0], "MES")
        self.assertEqual(eng._trim_position_to_one.call_args[0][2], 1)

        self.assertEqual(eng.emergency_flat_symbol.call_count, 1)
        self.assertEqual(eng.emergency_flat_symbol.call_args[0][0], "MNQ")
        self.assertTrue(eng._eod_squared_today)

        # Outside square off window (e.g. 15:00 ET) -> does not trigger
        eng.emergency_flat_symbol.reset_mock()
        eng._trim_position_to_one.reset_mock()
        eng._eod_squared_today = False
        fake_fri_1500 = datetime(2026, 11, 20, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("live_ibkr_dual_engine.datetime") as mock_dt:
            mock_dt.now.return_value = fake_fri_1500
            eng.check_eod_square_off()
        self.assertEqual(eng.emergency_flat_symbol.call_count, 0)
        self.assertEqual(eng._trim_position_to_one.call_count, 0)

    def test_p1_us_open_shock_absorber(self):
        eng = object.__new__(LiveIBKRDualEngine)
        eng.net_liquidation = 100000.0
        eng.market_data_status = "REAL_TIME"
        eng.ib = MagicMock()
        eng._can_query_hist_bar = lambda sym, ts: True
        eng._open_orders = {}

        from datetime import datetime
        from zoneinfo import ZoneInfo
        from unittest.mock import patch

        # 1. 09:28 ET -> 盤前競價時段 (09:25 - 09:30 ET) 凍結開倉
        fake_pre_open = datetime(2026, 11, 18, 9, 28, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("live_ibkr_dual_engine.datetime") as mock_dt:
            mock_dt.now.return_value = fake_pre_open
            eng._evaluate_symbol_bar("MES", None, None, 60000.0, 4.5, None, [])

        self.assertEqual(eng.ib.reqHistoricalData.call_count, 0)

        # 2. 09:35 ET -> 開市頭 15 分鐘 (09:30 - 09:45 ET)：
        # a) 空倉狀態下允許評估 K 棒
        fake_open_time = datetime(2026, 11, 18, 9, 35, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("live_ibkr_dual_engine.datetime") as mock_dt:
            mock_dt.now.return_value = fake_open_time
            eng._evaluate_symbol_bar("MES", None, None, 60000.0, 4.5, None, [])
        self.assertEqual(eng.ib.reqHistoricalData.call_count, 1)

        # b) 持有浮虧部位 (u_pnl <= 0) 嚴禁加倉
        eng.ib.reqHistoricalData.reset_mock()
        losing_pos = {"contracts": 1, "direction": "LONG", "unrealized_pnl": -50.0}
        with patch("live_ibkr_dual_engine.datetime") as mock_dt:
            mock_dt.now.return_value = fake_open_time
            eng._evaluate_symbol_bar("MES", None, None, 60000.0, 4.5, losing_pos, [])
        self.assertEqual(eng.ib.reqHistoricalData.call_count, 0)

        # c) 持有浮盈部位 (u_pnl > 0) 允許順勢加倉評估
        eng.ib.reqHistoricalData.reset_mock()
        winning_pos = {"contracts": 1, "direction": "LONG", "unrealized_pnl": 120.0}
        with patch("live_ibkr_dual_engine.datetime") as mock_dt:
            mock_dt.now.return_value = fake_open_time
            eng._evaluate_symbol_bar("MES", None, None, 60000.0, 4.5, winning_pos, [])
        self.assertEqual(eng.ib.reqHistoricalData.call_count, 1)

    def test_p1_consecutive_loss_circuit_breaker(self):
        eng = object.__new__(LiveIBKRDualEngine)
        eng.ledger_mgr = MagicMock()
        eng.incident_mgr = MagicMock()
        eng._consecutive_losses_today = 0
        eng._daily_trading_halted = False
        eng._daily_loss_circuit_until = 0.0
        eng._last_loss_event_ts = 0.0
        eng._active_trade_meta = {}
        eng._order_kinds = {}
        eng._order_rules = {}
        eng._order_exit_reasons = {}
        eng._notified_exec_ids = set()
        eng.usd_hkd_rate = 7.78
        eng.account_id = "DU12345"
        eng._safe_float = LiveIBKRDualEngine._safe_float

        def _make_fill(exec_id, pnl):
            trade = MagicMock()
            fill = SimpleNamespace(
                contract=SimpleNamespace(symbol="MNQ", localSymbol="MNQZ6"),
                commissionReport=SimpleNamespace(commission=1.5, realizedPNL=pnl),
                execution=SimpleNamespace(
                    execId=exec_id,
                    orderId=1001,
                    orderRef="V93_MNQ_SL",
                    side="SLD",
                    shares=1,
                    price=21000.0,
                    clientId=25,
                    time="2026-11-18 10:00:00"
                )
            )
            return trade, fill

        # 1st loss
        t, f = _make_fill("e1", -50.0)
        eng.on_exec_details(t, f)
        self.assertEqual(eng._consecutive_losses_today, 1)

        # 2nd loss (advance timestamp)
        eng._last_loss_event_ts = time.time() - 10.0
        t, f = _make_fill("e2", -30.0)
        eng.on_exec_details(t, f)
        self.assertEqual(eng._consecutive_losses_today, 2)

        # 3rd loss -> 取消冷卻停開，保持 _daily_loss_circuit_until == 0.0，改為注碼減半
        eng._last_loss_event_ts = time.time() - 10.0
        t, f = _make_fill("e3", -40.0)
        eng.on_exec_details(t, f)
        self.assertEqual(eng._consecutive_losses_today, 3)
        self.assertEqual(eng._daily_loss_circuit_until, 0.0)
        self.assertFalse(eng._daily_trading_halted)

        # 4th and 5th loss -> 取消當日停開限制，保持 _daily_trading_halted == False，連輸5次限制1張
        eng._last_loss_event_ts = time.time() - 10.0
        t, f = _make_fill("e4", -20.0)
        eng.on_exec_details(t, f)
        eng._last_loss_event_ts = time.time() - 10.0
        t, f = _make_fill("e5", -25.0)
        eng.on_exec_details(t, f)
        self.assertEqual(eng._consecutive_losses_today, 5)
        self.assertFalse(eng._daily_trading_halted)

        # 驗證注碼規則：連輸 3 次減半、連輸 5 次限 1 張
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from strategies.v9_3_master_hybrid_live import get_live_order_lots
        hkt_golden = datetime(2026, 11, 20, 23, 0, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        # 基準：100000 淨值常規注碼為 7 口 (MES R1 階梯: 2/5/7/9)
        lots_base, _ = get_live_order_lots("R1", "LONG", current_equity=100000.0, consecutive_losses=0, timestamp=hkt_golden)
        lots_3loss, _ = get_live_order_lots("R1", "LONG", current_equity=100000.0, consecutive_losses=3, timestamp=hkt_golden)
        lots_5loss, _ = get_live_order_lots("R1", "LONG", current_equity=100000.0, consecutive_losses=5, timestamp=hkt_golden)
        self.assertEqual(lots_base, 7)
        self.assertEqual(lots_3loss, 3)  # 7 // 2 = 3 口
        self.assertEqual(lots_5loss, 1)  # 強制 1 張

        # Winning fill -> 止盈則重算歸零
        t, f = _make_fill("e6", 100.0)
        eng.on_exec_details(t, f)
        self.assertEqual(eng._consecutive_losses_today, 0)

    def test_p1_r2_macro_trend_and_europe_filters(self):
        import pandas as pd
        engine = StrategyEngineV93(symbol="MNQ")
        dates = pd.date_range("2026-11-18 07:00", periods=50, freq="5min", tz="UTC")
        df = pd.DataFrame({
            "open": [21000.0] * 50,
            "high": [21050.0] * 50,
            "low": [20950.0] * 50,
            "close": [21010.0] * 50,
            "volume": [100] * 50,
            "atr": [20.0] * 50,
            "box_high": [21100.0] * 50,
            "box_low": [20900.0] * 50,
            "ema20": [21000.0] * 50,
            "ema50": [21000.0] * 50,
            "ema200_4h": [21200.0] * 50,
            "ema20_4h": [21100.0] * 50,
            "ema50_4h": [21150.0] * 50,
            "close_4h": [21000.0] * 50,
            "adx_1h": [15.0] * 50,
        }, index=dates)

        # In 4H downtrend (macro_short=True), R2 LONG must be blocked
        df.loc[dates[-2], "low"] = 20900.0 + 0.1 * 20.0
        df.loc[dates[-2], "close"] = 20900.0 + 0.3 * 20.0
        df.loc[dates[-2], "open"] = 20900.0 + 0.2 * 20.0
        df.loc[dates[-2], "ema20_4h"] = 20800.0
        df.loc[dates[-2], "ema50_4h"] = 20900.0
        df.loc[dates[-2], "close_4h"] = 20750.0

        sig, _ = engine.evaluate_bar(len(df) - 2, df, 100000.0, 0.5)
        self.assertIsNone(sig, "R2 Long should be blocked in 4H macro downtrend")

    def test_p2_report_reconciliation_table(self):
        from report_and_incident_manager import DailyReportGenerator
        gen = DailyReportGenerator()
        report_data = {
            "report_date": "2026-11-19",
            "generated_at": "2026-11-20 06:00:00",
            "account_currency": "HKD",
            "usd_hkd_rate": 7.78,
            "equity_summary": {
                "start_equity": 2000000.0,
                "end_equity": 2010000.0,
                "high_equity": 2015000.0,
                "low_equity": 1995000.0,
                "net_pnl": 10000.0,
                "daily_return_pct": 0.5,
                "intraday_drawdown_pct": 0.25,
                "cash_balance": 1800000.0,
                "available_funds": 1500000.0,
                "margin_requirement": 300000.0,
                "unrealized_pnl_usd": 0.0
            },
            "pnl_performance": {
                "total_trades": 10,
                "winning_trades": 6,
                "losing_trades": 4,
                "win_rate_pct": 60.0,
                "gross_profit": 2000.0,
                "gross_loss": -800.0,
                "total_commission": 50.0,
                "net_pnl": 1150.0,
                "profit_factor": 2.5
            },
            "strategy_breakdown": {
                "v43_mes": {
                    "strategy_name": "V9.3 旗艦雙引擎整數自適應 (MES 60% 優先池)",
                    "symbol": "MES",
                    "trades_count": 6,
                    "realized_pnl": 1200.0,
                    "commission": 30.0,
                    "net_pnl": 1170.0
                },
                "v50_mnq": {
                    "strategy_name": "V9.3 旗艦雙引擎整數自適應 (MNQ 40% 防飢餓池)",
                    "symbol": "MNQ",
                    "trades_count": 4,
                    "realized_pnl": 0.0,
                    "commission": 20.0,
                    "net_pnl": -20.0
                }
            },
            "incident_audit": {"has_critical": False, "incidents": []},
            "executions": []
        }

        md = gen._format_markdown_report(report_data)
        self.assertIn("帳戶損益三欄對帳公式 (Three-Column PnL Reconciliation)", md)
        self.assertIn("期貨已實現淨損益 (Realized Net PnL)", md)
        self.assertIn("總結算淨權益變動 (Total Converted Net)", md)
        self.assertIn("V9.3 旗艦雙引擎整數自適應 (MES 60% 優先池)", md)


class TestFifoPairingAndCalendarReconciliation(unittest.TestCase):
    """驗證同秒開倉/止損 FIFO 撮合順序、持倉基準校準與日曆防重複計入。"""

    def test_fifo_same_second_entry_sl_ordering(self):
        from live_ibkr_dual_engine import pair_executions_fifo

        entry_exec = {
            "timestamp": "2026-11-25 13:39:53",
            "symbol": "MNQ",
            "side": "SLD",
            "contracts": 1,
            "price": 25484.75,
            "order_ref": "V93_MNQ_ENTRY_TP2",
            "realized_pnl": 0.0,
            "exec_id": "0001f2.d3a9",
        }
        sl_exec = {
            "timestamp": "2026-11-25 13:39:53",
            "symbol": "MNQ",
            "side": "BOT",
            "contracts": 1,
            "price": 25487.00,
            "order_ref": "V93_MNQ_SL_TP2",
            "realized_pnl": -4.72,
            "exec_id": "0001f2.d3aa",
        }

        # 即使輸入順序顛倒（SL 在前），_sort_key 亦能依 ENTRY=0/SL=2 優先權在先撮合開倉
        groups = pair_executions_fifo([sl_exec, entry_exec], fx_rate=7.8)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["status"], "CLOSED")
        self.assertEqual(groups[0]["total_pnl"], -4.72)
        self.assertEqual(len(groups[0]["close_orders"]), 1)

    def test_position_grounding_clears_residual_open(self):
        from live_ibkr_dual_engine import pair_executions_fifo

        unclosed_entry = {
            "timestamp": "2026-11-25 16:47:25",
            "symbol": "MNQ",
            "side": "SLD",
            "contracts": 1,
            "price": 25400.0,
            "order_ref": "V93_MNQ_ENTRY_RUNNER",
            "realized_pnl": 0.0,
            "exec_id": "0001f2.e001",
        }

        # 若未傳入 current_positions，該單呈現 OPEN
        groups_raw = pair_executions_fifo([unclosed_entry], fx_rate=7.8)
        self.assertEqual(groups_raw[0]["status"], "OPEN")

        # 傳入經紀商持倉基準 current_positions={"MNQ": 0}，雙保險強制校準為 CLOSED
        groups_grounded = pair_executions_fifo([unclosed_entry], fx_rate=7.8, current_positions={"MNQ": 0})
        self.assertEqual(groups_grounded[0]["status"], "CLOSED")
        self.assertEqual(groups_grounded[0]["closed_contracts"], 1)

    def test_calendar_no_duplicate_live_status_day(self):
        import json
        from server import build_daily_pnl_calendar

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = os.path.join(tmp_dir, "data")
            os.makedirs(data_dir, exist_ok=True)

            trades_file = os.path.join(data_dir, "trades.jsonl")
            with open(trades_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "timestamp": "2026-11-25 10:00:00",
                    "symbol": "MES",
                    "side": "BOT",
                    "contracts": 1,
                    "price": 6000.0,
                    "order_ref": "V93_MES_ENTRY_TP1",
                    "realized_pnl": 0.0,
                    "exec_id": "test_e1",
                }) + "\n")
                f.write(json.dumps({
                    "timestamp": "2026-11-25 10:05:00",
                    "symbol": "MES",
                    "side": "SLD",
                    "contracts": 1,
                    "price": 6010.0,
                    "order_ref": "V93_MES_TP_TP1",
                    "realized_pnl": 50.0,
                    "exec_id": "test_e2",
                }) + "\n")

            # status_snapshot 具有 UTC 跨日 11-26 的 last_update 與當日已實現損益
            status_file = os.path.join(tmp_dir, "trade_status.json")
            with open(status_file, "w", encoding="utf-8") as f:
                json.dump({
                    "last_update": "2026-11-26 00:30:00",
                    "realized_pnl_hkd": 390.0,
                    "fx_rate_usd_hkd": 7.8,
                    "recent_trades": [],
                }, f)

            res = build_daily_pnl_calendar("2026-11", base_dir=tmp_dir)
            days = res["days"]
            self.assertIn("2026-11-25", days)
            # 確保不會產生虛假雙重計入的 2026-11-26 live_status 卡片
            self.assertNotIn("2026-11-26", days)
            self.assertEqual(res["monthly_summary"]["net_pnl_usd"], 50.0)


if __name__ == "__main__":
    unittest.main()
