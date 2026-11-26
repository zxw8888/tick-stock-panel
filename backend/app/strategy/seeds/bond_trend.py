"""债券趋势持有 (Bond Trend) — 长周期均线 + 动量, 票息资产的持有/避险切换

设计依据 (联网核实 2026-11-23):
  - 债券 ETF 有确定票息, 长期净值向上 (十年国债 ETF 自 2018 年起连年正收益);
  - 主流债券 ETF 择时用「动量 + 均值回归」判断利率方向, 或用长周期均线做
    趋势跟随 (站上长均线持有、跌破避险), 信号少但方向明确。

A股适配 (matrix_native 现算特征, 不依赖 ETF enriched 衍生列):
  - 入场: 收盘价站上长周期均线 (默认 MA120) 且中期动量为正;
  - 离场: 收盘价跌破长周期均线;
  - 评分: 中期动量 (趋势越强排名越前);
  - 用于债券/货币/现金流这类低波动组, 预期信号稀少, 属正常。
"""
from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "bond_trend",
    "name": "债券趋势持有",
    "description": (
        "债券ETF长线趋势: 站上MA120且中期动量为正持有, 跌破MA120避险; "
        "票息资产长期向上, 信号稀少但方向明确"
    ),
    "tags": ["债券", "趋势", "低波动", "均线"],
    "asset_types": ["etf"],
    "timeframes": ["1d"],
    "basic_filter": {},
    "params": [
        {
            "id": "ma_window",
            "label": "长周期均线(日)",
            "type": "int",
            "default": 120,
            "min": 20,
            "max": 250,
            "step": 10,
        },
        {
            "id": "momentum_window",
            "label": "动量窗口(日)",
            "type": "int",
            "default": 20,
            "min": 5,
            "max": 120,
            "step": 5,
        },
        {
            "id": "require_positive_momentum",
            "label": "要求中期动量为正",
            "type": "bool",
            "default": True,
        },
    ],
    "scoring": {"momentum_20d": 1.0},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 120


class BondTrendMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        return int(params.get("ma_window") or 120) + 1

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        ma_window = int(params.get("ma_window") or 120)
        mom_window = int(params.get("momentum_window") or 20)
        close = market.close
        valid = np.isfinite(close)

        ma = matrix_feature(market, f"ma{ma_window}")
        ma_valid = np.isfinite(ma)
        above = (close > ma) & ma_valid & valid

        momentum = matrix_feature(market, f"momentum_{mom_window}d")
        mom_valid = np.isfinite(momentum)
        if params.get("require_positive_momentum", True):
            entry = above & (momentum > 0) & mom_valid
        else:
            entry = above

        # 离场: 跌破长均线 (前一日在上方, 今日跌破)
        exit_ = (
            (close < ma) & ma_valid & valid
            & (shift(close, 1) >= shift(ma, 1))
        )

        score = np.nan_to_num(momentum, nan=0.0).astype(np.float32, copy=False)
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
        )


MATRIX_STRATEGY = BondTrendMatrixStrategy()
