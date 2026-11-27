"""
AutoTrade 本地多策略量化回測實驗室 (Backtest Lab)
"""
import sys
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)

if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
