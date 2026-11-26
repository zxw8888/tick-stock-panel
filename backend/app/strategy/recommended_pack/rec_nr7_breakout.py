"""NR7 volatility contraction followed by a 20-day high breakout."""

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
from app.backtest.matrix import valid_shift as shift

META = {
    "id": "rec_nr7_breakout",
    "name": "NR7波动收缩突破",
    "description": "7日最窄振幅后突破20日高点并放量，利用波动收缩后的方向选择。",
    "tags": ["推荐", "NR7", "波动收缩", "突破"],
    "origin": "recommended",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 3,
        "price_max": 300,
        "market_cap_min": 10e8,
        "amount_min": 0.3e8,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float", "default": 1.2, "min": 0.8, "max": 5.0, "step": 0.1},
    ],
    "scoring": {"momentum_20d": 0.4, "vol_ratio_5d": 0.35, "atr_pct": 0.25},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    "version": "1.0.0",
    "sources": ["Crabel (1990), Day Trading with Short Term Price Patterns and Opening Range Breakout"],
    "suitable_for": {
        "market_cap": ["small", "mid"],
        "styles": ["breakout", "trend"],
        "volatility": ["normal", "high"],
        "liquidity": ["normal", "high"],
        "boards": ["沪主板", "深主板", "创业板", "科创板", "北交所"],
        "avoid": ["ST", "次新", "低流动性", "退市风险"],
        "note": "适合完成横盘收缩、量能开始放大的中小盘股票；突破失败率高，必须配合止损。",
    },
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15


class Nr7BreakoutStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 70

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close
        valid = np.isfinite(close)
        range_valid = valid & np.isfinite(market.high) & np.isfinite(market.low)
        prev_close = matrix_feature(market, "prev_close")
        range_pct = np.full(market.shape, np.nan, dtype=np.float32)
        np.divide(
            market.high - market.low,
            prev_close,
            out=range_pct,
            where=range_valid & np.isfinite(prev_close) & (prev_close > 0),
        )
        prior_narrow = shift(valid_rolling_min(range_pct, range_valid, 6), 1)
        prior_high = shift(valid_rolling_max(market.high, range_valid, 20), 1)
        vol_ratio = matrix_feature(market, "vol_ratio_5d")

        nr7 = np.isfinite(prior_narrow) & np.isfinite(range_pct) & (range_pct <= prior_narrow)
        breakout = (
            nr7
            & np.isfinite(prior_high)
            & (close > prior_high)
            & np.isfinite(vol_ratio)
            & (vol_ratio >= float(params.get("vol_ratio_min", 1.2)))
        )
        ma20 = matrix_feature(market, "ma20")
        exit_ = valid & np.isfinite(ma20) & (close < ma20)

        strength = np.zeros(market.shape, dtype=np.float32)
        np.divide(close - prior_high, prior_high, out=strength, where=np.isfinite(prior_high) & (prior_high > 0))
        score = (
            2.0 * np.nan_to_num(strength, nan=0.0)
            + 0.3 * np.nan_to_num(matrix_feature(market, "momentum_20d"), nan=0.0)
            + 0.2 * np.nan_to_num(vol_ratio, nan=0.0)
        ).astype(np.float32, copy=False)
        score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)

        return make_signal_matrix(
            market.shape,
            entry=breakout.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
        )


MATRIX_STRATEGY = Nr7BreakoutStrategy()
