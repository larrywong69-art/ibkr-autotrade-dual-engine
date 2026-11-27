#!/usr/bin/env python3
"""
Unit test suite for MTF (Multi-Timeframe) 1H Setup + 5m Precision Trigger logic.
Verifies that:
1. 1H Setup generates an Armed signal with 1H ATR and 60-minute TTL.
2. 5m evaluation waits when 1H is not armed or 5m bar conditions are not met.
3. 5m precision trigger executes bracket orders with 1H ATR SL/TP and immediately disarms.
4. 1H Armed setups expire cleanly after 60 minutes.
5. SHORT direction 5m precision trigger functions symmetrically.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from live_ibkr_dual_engine import LiveIBKRDualEngine, Direction
from mes_strategy.strategy_models import TradeSignal, SignalType, MomentumSnapshot


def _generate_synthetic_bars(n_bars=40, base_price=5500.0, step=1.0, is_1h=False):
    """Generate synthetic dict bars for util.df compatibility."""
    bars = []
    freq = "1h" if is_1h else "5min"
    dates = pd.date_range("2026-11-20 00:00:00", periods=n_bars, freq=freq, tz="UTC")
    for i, dt in enumerate(dates):
        p = base_price + i * step
        bars.append({
            "date": dt,
            "open": p - 0.5,
            "high": p + 2.0,
            "low": p - 2.0,
            "close": p,
            "volume": 100.0,
        })
    return bars


class TestMTFMultiTimeframeLogic(unittest.TestCase):
    def setUp(self):
        self.eng = object.__new__(LiveIBKRDualEngine)
        self.eng.enable_mtf = True
        self.eng.net_liquidation = 50000.0
        self.eng.contract_mes = SimpleNamespace(symbol="MES")
        self.eng.contract_mnq = SimpleNamespace(symbol="MNQ")
        self.eng.engine_v93_mes = MagicMock()
        self.eng.engine_v93_mnq = MagicMock()
        self.eng.armed_setup = {"MES": None, "MNQ": None}
        self.eng._last_1h_cache_ts = {}
        self.eng._last_1h_eval_bar_ts = {}
        self.eng._last_eval_bar_ts = {}
        self.eng._last_eval_bucket = {}
        self.eng._hist_fail_streak = {}
        self.eng._last_hist_fail_ts = {}
        self.eng._last_scan_diag_ts = {}
        self.eng._active_trade_meta = {}
        self.eng._consecutive_losses_today = 0
        self.eng._last_protect_ts = {}
        self.eng._round_to_tick = lambda p, t=0.25: round(p / t) * t
        self.eng.get_positions_summary = MagicMock(return_value={})
        self.eng.get_open_orders_summary = MagicMock(return_value={})
        self.eng._can_query_hist_bar = MagicMock(return_value=True)
        self.eng.get_4h_macro_regime = MagicMock(return_value={
            "ts": time.time(), "long": True, "short": False,
            "e20": 5480.0, "e50": 5450.0, "e200": 5300.0, "c": 5500.0, "adx_1h": 25.0
        })

        self.placed_signals = []
        self.eng.place_v93_bracket_order = MagicMock(side_effect=lambda sym, sig: self.placed_signals.append((sym, sig)))

        self.eng.ib = MagicMock()
        self.eng.ib.isConnected.return_value = True

    def test_evaluate_1h_setup_arms_on_valid_signal(self):
        """1H Setup: Evaluates 1H bars, calls strategy engine, and creates Armed setup with 1H ATR."""
        bars_1h = _generate_synthetic_bars(n_bars=35, base_price=5500.0, step=2.0, is_1h=True)
        self.eng.ib.reqHistoricalData.return_value = bars_1h

        dummy_signal = TradeSignal(
            timestamp=pd.Timestamp("2026-11-20 12:00:00", tz="UTC"),
            signal_type=SignalType.TREND_PULLBACK_WAVE3,
            direction=Direction.LONG,
            entry_price=5568.0,
            sl_price=5548.0,
            tp1_price=5600.0,
            tp2_price=5630.0,
            weighted_rr=2.5,
            contracts=2,
            momentum=MomentumSnapshot.STRONG,
            entry_bar_body=10.0,
            atr=25.0,
        )
        dummy_signal.runner_price = 5660.0
        dummy_signal.rule_code = "R1"
        dummy_signal.sl_dist = 20.5
        dummy_signal.tp1_dist = 38.75
        dummy_signal.tp2_dist = 77.5
        dummy_signal.split_lots = (1, 1, 0)

        self.eng.engine_v93_mes.evaluate_bar.return_value = (dummy_signal, None)

        setup = self.eng.evaluate_1h_setup("MES", self.eng.contract_mes, self.eng.engine_v93_mes, 50000.0, 0)

        self.assertIsNotNone(setup)
        self.assertTrue(setup["is_armed"])
        self.assertEqual(setup["symbol"], "MES")
        self.assertEqual(setup["direction"], Direction.LONG)
        self.assertEqual(setup["rule_code"], "R1")
        self.assertEqual(setup["atr_1h"], 25.0)
        self.assertEqual(setup["sl_dist"], 20.5)
        self.assertEqual(setup["tp1_dist"], 38.75)
        self.assertGreater(setup["expiry_ts"], time.time())
        self.assertEqual(self.eng.armed_setup["MES"], setup)

    def test_evaluate_1h_setup_returns_none_when_no_strategy_signal(self):
        """1H Setup: When strategy engine produces no signal, armed_setup is None."""
        bars_1h = _generate_synthetic_bars(n_bars=35, base_price=5500.0, is_1h=True)
        self.eng.ib.reqHistoricalData.return_value = bars_1h
        self.eng.engine_v93_mes.evaluate_bar.return_value = (None, None)

        setup = self.eng.evaluate_1h_setup("MES", self.eng.contract_mes, self.eng.engine_v93_mes, 50000.0, 0)
        self.assertIsNone(setup)
        self.assertIsNone(self.eng.armed_setup["MES"])

    def test_mtf_eval_bar_waiting_when_not_armed(self):
        """MTF Precision Trigger: When 1H is not armed, no order is placed."""
        self.eng.evaluate_1h_setup = MagicMock(return_value=None)
        self.eng._evaluate_symbol_bar(
            "MES", self.eng.contract_mes, self.eng.engine_v93_mes, 50000.0, 25.0, None, []
        )
        self.assertEqual(len(self.placed_signals), 0)

    def test_mtf_eval_bar_waiting_when_5m_bar_not_confirmed(self):
        """MTF Precision Trigger: 1H is armed LONG, but 5m bar is bearish -> Waits, no order placed."""
        self.eng.armed_setup["MES"] = {
            "is_armed": True,
            "symbol": "MES",
            "direction": Direction.LONG,
            "rule_code": "R1",
            "signal_type": SignalType.TREND_PULLBACK_WAVE3,
            "setup_price": 5500.0,
            "setup_time": "2026-11-20 12:00:00+00:00",
            "atr_1h": 25.0,
            "sl_dist": 20.5,
            "tp1_dist": 38.75,
            "tp2_dist": 77.5,
            "contracts": 2,
            "split_lots": (1, 1, 0),
            "armed_ts": time.time(),
            "expiry_ts": time.time() + 3600.0,
        }
        self.eng.evaluate_1h_setup = MagicMock(return_value=self.eng.armed_setup["MES"])

        # Create 5m bars where penultimate closed bar (idx=len-2) is bearish (close < open)
        bars_5m = _generate_synthetic_bars(n_bars=35, base_price=5510.0, step=0.5, is_1h=False)
        bars_5m[-2]["open"] = 5520.0
        bars_5m[-2]["close"] = 5515.0  # Bearish bar!

        self.eng.ib.reqHistoricalData.return_value = bars_5m

        self.eng._evaluate_symbol_bar(
            "MES", self.eng.contract_mes, self.eng.engine_v93_mes, 50000.0, 25.0, None, []
        )

        # Should not trigger order, setup must remain armed
        self.assertEqual(len(self.placed_signals), 0)
        self.assertIsNotNone(self.eng.armed_setup["MES"])

    def test_mtf_eval_bar_triggers_order_with_1h_atr_sl_and_disarms(self):
        """MTF Precision Trigger: 1H armed LONG, 5m bar confirms bullish -> Order placed with 1H ATR SL & disarmed."""
        now_ts = time.time()
        self.eng.armed_setup["MES"] = {
            "is_armed": True,
            "symbol": "MES",
            "direction": Direction.LONG,
            "rule_code": "R1",
            "signal_type": SignalType.TREND_PULLBACK_WAVE3,
            "setup_price": 5500.0,
            "setup_time": "2026-11-20 12:00:00+00:00",
            "atr_1h": 25.0,
            "sl_dist": 20.5,
            "tp1_dist": 38.75,
            "tp2_dist": 77.5,
            "contracts": 2,
            "split_lots": (1, 1, 0),
            "armed_ts": now_ts,
            "expiry_ts": now_ts + 3600.0,
        }
        self.eng.evaluate_1h_setup = MagicMock(return_value=self.eng.armed_setup["MES"])

        # Create 5m bars where penultimate closed bar is bullish (close > open) and closes at 5515.0
        bars_5m = _generate_synthetic_bars(n_bars=35, base_price=5500.0, step=0.5, is_1h=False)
        bars_5m[-3]["high"] = 5514.0
        bars_5m[-3]["low"] = 5508.0
        bars_5m[-2]["open"] = 5510.0
        bars_5m[-2]["close"] = 5515.0  # Bullish, above prev high, within setup + 1.25*atr
        bars_5m[-2]["high"] = 5516.0
        bars_5m[-2]["low"] = 5509.0

        self.eng.ib.reqHistoricalData.return_value = bars_5m

        self.eng._evaluate_symbol_bar(
            "MES", self.eng.contract_mes, self.eng.engine_v93_mes, 50000.0, 25.0, None, []
        )

        # 1. Order must be placed
        self.assertEqual(len(self.placed_signals), 1)
        sym, sig = self.placed_signals[0]
        self.assertEqual(sym, "MES")
        self.assertEqual(sig.direction, Direction.LONG)
        self.assertEqual(sig.entry_price, 5515.0)

        # 2. SL and TP MUST be based on 1H ATR (sl_dist = 20.5), NOT 5m micro ATR!
        self.assertEqual(sig.sl_dist, 20.5)
        self.assertEqual(sig.sl_price, 5515.0 - 20.5)
        self.assertEqual(sig.tp1_dist, 38.75)
        self.assertEqual(sig.tp1_price, 5515.0 + 38.75)
        self.assertEqual(sig.rule_code, "R1_MTF5M")

        # 3. Armed setup MUST be immediately disarmed to prevent duplicate entry
        self.assertIsNone(self.eng.armed_setup["MES"])

    def test_mtf_armed_setup_expires_after_60_minutes(self):
        """MTF Lifecycle: Setup older than 60 minutes expires and automatically disarms without placing orders."""
        now_ts = time.time()
        expired_setup = {
            "is_armed": True,
            "symbol": "MES",
            "direction": Direction.LONG,
            "rule_code": "R1",
            "signal_type": SignalType.TREND_PULLBACK_WAVE3,
            "setup_price": 5500.0,
            "setup_time": "2026-11-20 12:00:00+00:00",
            "atr_1h": 25.0,
            "sl_dist": 20.5,
            "tp1_dist": 38.75,
            "tp2_dist": 77.5,
            "contracts": 2,
            "split_lots": (1, 1, 0),
            "armed_ts": now_ts - 3650.0,
            "expiry_ts": now_ts - 50.0,  # Expired 50s ago!
        }
        self.eng.armed_setup["MES"] = expired_setup
        self.eng.evaluate_1h_setup = MagicMock(return_value=expired_setup)

        self.eng._evaluate_symbol_bar(
            "MES", self.eng.contract_mes, self.eng.engine_v93_mes, 50000.0, 25.0, None, []
        )

        self.assertEqual(len(self.placed_signals), 0)
        self.assertIsNone(self.eng.armed_setup["MES"])

    def test_mtf_short_precision_trigger(self):
        """MTF SHORT Precision Trigger: 1H armed SHORT, 5m confirms bear bar -> Places SHORT bracket and disarms."""
        now_ts = time.time()
        self.eng.armed_setup["MNQ"] = {
            "is_armed": True,
            "symbol": "MNQ",
            "direction": Direction.SHORT,
            "rule_code": "R2",
            "signal_type": SignalType.RANGE_BOUNCE,
            "setup_price": 21000.0,
            "setup_time": "2026-11-20 12:00:00+00:00",
            "atr_1h": 120.0,
            "sl_dist": 98.4,
            "tp1_dist": 186.0,
            "tp2_dist": 372.0,
            "contracts": 1,
            "split_lots": (0, 1, 0),
            "armed_ts": now_ts,
            "expiry_ts": now_ts + 3600.0,
        }
        self.eng.evaluate_1h_setup = MagicMock(return_value=self.eng.armed_setup["MNQ"])

        bars_5m = _generate_synthetic_bars(n_bars=35, base_price=21050.0, step=-2.0, is_1h=False)
        bars_5m[-3]["high"] = 20995.0
        bars_5m[-3]["low"] = 20980.0
        bars_5m[-2]["open"] = 20985.0
        bars_5m[-2]["close"] = 20970.0  # Bear bar, broken below prev low
        bars_5m[-2]["high"] = 20988.0
        bars_5m[-2]["low"] = 20968.0

        self.eng.ib.reqHistoricalData.return_value = bars_5m

        self.eng._evaluate_symbol_bar(
            "MNQ", self.eng.contract_mnq, self.eng.engine_v93_mnq, 50000.0, 120.0, None, []
        )

        self.assertEqual(len(self.placed_signals), 1)
        sym, sig = self.placed_signals[0]
        self.assertEqual(sym, "MNQ")
        self.assertEqual(sig.direction, Direction.SHORT)
        self.assertEqual(sig.entry_price, 20970.0)
        # SHORT SL is entry + sl_dist (1H ATR based, rounded to 0.25 tick)
        self.assertEqual(sig.sl_price, self.eng._round_to_tick(20970.0 + 98.4))
        self.assertEqual(sig.tp1_price, 20970.0 - 186.0)
        self.assertEqual(sig.rule_code, "R2_MTF5M")
        self.assertIsNone(self.eng.armed_setup["MNQ"])


if __name__ == "__main__":
    unittest.main()
