"""海龟交易法则 (Turtle Trading) — 唐奇安通道突破 + ATR 头寸风控

原版规则来源: Curtis Faith《The Original Turtle Trading Rules》
  - 系统1: 20日唐奇安上轨突破入场, 10日下轨跌破离场 (短线)
  - 系统2: 55日唐奇安上轨突破入场, 20日下轨跌破离场 (长线)
  - 原版两套并行; 本策略用 system 参数切换, 便于单独研究和调参
  - N = ATR(20)。头寸单位 Unit = 账户1% / N; 每涨0.5N加仓, 最多4单位
  - 止损 = 最近入场价 - 2N (反向为 +2N)

A股适配说明 (与原版的差异, 均为引擎约束所致):
  - A股无做空与普通逐笔加仓原语, 故只实现多头突破; 加仓/头寸规模由回测
    的仓位管理承担, 策略层用评分把"突破强度"和"波动可控"优先排序。
  - 原版盘中触及突破即入场, 本系统按日线收盘确认 (next_bar 成交口径),
    收盘确认会过滤部分假突破, 属公开改良而非原版。
  - 唐奇安通道按「不含当日」计算 (过去N日的高低点), 避免未来函数。
  - 离场信号 = 反向通道跌破, 或价格较近期高点回撤超过 2N(由 STOP_LOSS
    百分比近似, 随波动率放大/收紧由评分体现)。
"""
from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
    valid_rolling_min,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "turtle_trading",
    "name": "海龟交易法则",
    "description": (
        "唐奇安通道突破(A股多头版): 系统1=20日上轨进/10日下轨出, "
        "系统2=55日上轨进/20日下轨出; N=ATR(20), 评分=突破强度/波动可控"
    ),
    "tags": ["海龟", "趋势", "唐奇安通道", "突破", "ATR"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 5,
        "price_max": 300,
        "market_cap_min": 20e8,
        "amount_min": 5e7,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {
            "id": "system",
            "label": "交易系统(system1=20进10出 / system2=55进20出)",
            "type": "select",
            "default": "system1",
            "options": ["system1", "system2"],
        },
        {
            "id": "entry_window",
            "label": "入场通道天数(0=随系统)",
            "type": "int",
            "default": 0,
            "min": 0,
            "max": 120,
            "step": 1,
        },
        {
            "id": "exit_window",
            "label": "离场通道天数(0=随系统)",
            "type": "int",
            "default": 0,
            "min": 0,
            "max": 60,
            "step": 1,
        },
        {
            "id": "use_atr_filter",
            "label": "启用ATR风控评分",
            "type": "bool",
            "default": True,
        },
        {
            "id": "min_atr_pct",
            "label": "最低ATR波动率(过滤僵尸股)",
            "type": "float",
            "default": 0.01,
            "min": 0.0,
            "max": 0.2,
            "step": 0.005,
        },
        {
            "id": "max_atr_pct",
            "label": "最高ATR波动率(过滤过度投机)",
            "type": "float",
            "default": 0.12,
            "min": 0.02,
            "max": 0.5,
            "step": 0.01,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "change_pct": 0.3, "vol_ratio_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_n_day_high"]
EXIT_SIGNALS = ["signal_n_day_low"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 20


class TurtleTradingMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"high", "low", "close"})

    def required_warmup_bars(self, params: dict) -> int:
        system = str(params.get("system", "system1")).lower()
        entry_n, _exit_n = self._windows(system)
        return max(entry_n + 1, 60)

    @staticmethod
    def _windows(system: str) -> tuple[int, int]:
        if system == "system2":
            return 55, 20
        return 20, 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        system = str(params.get("system", "system1")).lower()
        default_entry, default_exit = self._windows(system)

        # 通道窗口: 0 表示跟随所选系统; 大于 0 则是用户自定义覆盖
        custom_entry = int(params.get("entry_window") or 0)
        custom_exit = int(params.get("exit_window") or 0)
        entry_n = custom_entry if custom_entry > 0 else default_entry
        exit_n = custom_exit if custom_exit > 0 else default_exit

        close = market.close
        close_valid = np.isfinite(close)
        high_valid = close_valid & np.isfinite(market.high)
        low_valid = close_valid & np.isfinite(market.low)

        # 唐奇安通道: 过去 N 日的最高/最低价, 整体后移1日 -> 严格不含当日
        upper = shift(valid_rolling_max(market.high, high_valid, entry_n), 1)
        lower = shift(valid_rolling_min(market.low, low_valid, exit_n), 1)

        close_valid_f = close_valid
        upper_valid = np.isfinite(upper)
        lower_valid = np.isfinite(lower)

        # 入场: 今收上破过去 entry_n 日上轨
        entry = (close > upper) & upper_valid & close_valid_f

        # ATR 风控过滤 (评分层面 + 僵尸股/过度投机过滤)
        if params.get("use_atr_filter", True):
            atr_pct = matrix_feature(market, "atr_pct")
            lo = float(params.get("min_atr_pct", 0.01))
            hi = float(params.get("max_atr_pct", 0.12))
            vol_ok = np.isfinite(atr_pct) & (atr_pct >= lo) & (atr_pct <= hi)
            entry = entry & vol_ok

        # 离场: 今收跌破过去 exit_n 日下轨 (通道穿越)
        exit_ = (close < lower) & lower_valid & close_valid_f

        # 评分: 突破强度(相对上轨的百分比距离) + 动量; 海龟偏好强趋势温和波动
        breakout_strength = np.zeros(market.shape, dtype=np.float32)
        np.divide(close - upper, upper, out=breakout_strength, where=upper_valid & (upper != 0))
        momentum = matrix_feature(market, "momentum_20d")
        score = (
            np.nan_to_num(breakout_strength, nan=0.0) * 3.0
            + np.nan_to_num(momentum, nan=0.0)
        ).astype(np.float32, copy=False)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_n_day_high",),
            exit_signal_ids=("signal_n_day_low",),
        )


MATRIX_STRATEGY = TurtleTradingMatrixStrategy()
