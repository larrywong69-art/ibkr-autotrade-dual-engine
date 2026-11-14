"""
CME MES Nine-in-One Quantitative Automated Trading Strategy Package (v4.6 Production).
"""

from mes_strategy.config import StrategyConfig
from mes_strategy.strategy_engine import NineInOneStrategy
from mes_strategy.backtester import BacktestRunner
from mes_strategy.ibkr_client import IBKRExecutionEngine

__version__ = "4.6.0"
