"""20-day breakout, pullback to the breakout level, and hold with lower volume."""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
    valid_rolling_sum,
)
from app.backtest.matrix import valid_shift as shift

META = {
    "id": "rec_breakout_retest_hold",
    "name": "突破回踩确认",
    "description": "20日新高后在突破位附近缩量回踩并守住，过滤追高和假突破。",
    "tags": ["推荐", "突破", "回踩", "量价"],
    "origin": "recommended",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 3,
        "price_max": 500,
        "market_cap_min": 20e8,
        "amount_min": 0.5e8,
        "exclude_st": True,
        "exclude_new_days": 80,
    },
    "params": [
        {"id": "retest_tolerance", "label": "突破位容差", "type": "float", "default": 0.03, "min": 0.01, "max": 0.08, "step": 0.005},
        {"id": "max_vol_ratio", "label": "回踩最大量比", "type": "float", "default": 1.0, "min": 0.5, "max": 2.0, "step": 0.1},
    ],
    "scoring": {"momentum_60d": 0.5, "vol_ratio_5d": 0.3, "ma60_bias": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    "version": "1.0.0",
    "sources": ["Minervini (2013), Trade Like a Stock Market Wizard", "O'Neil (2009), How to Make Money in Stocks"],
    "suitable_for": {
        "market_cap": ["mid", "large"],
        "styles": ["breakout", "trend", "leader"],
        "volatility": ["normal"],
        "liquidity": ["high"],
        "boards": ["沪主板", "深主板", "创业板", "科创板"],
        "avoid": ["ST", "次新", "低流动性", "高波动", "退市风险"],
        "note": "适合突破后回踩确认的中大盘强势股；不适合突破后快速跌破平台或流动性不足的标的。",
    },
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 25


class BreakoutRetestHoldStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 130

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close
        valid = np.isfinite(close)
        high_valid = valid & np.isfinite(market.high)
        low_valid = valid & np.isfinite(market.low)
        prior_high = shift(valid_rolling_max(market.high, high_valid, 20), 1)
        breakout = valid & np.isfinite(prior_high) & (close > prior_high)

        level_source = np.where(breakout, prior_high, np.nan).astype(np.float32)
        level_valid = np.isfinite(level_source)
        breakout_level = valid_rolling_max(level_source, level_valid, 10)
        recent_breakout = valid_rolling_sum(
            breakout.astype(np.float32),
            valid,
            10,
        ) > 0
        tolerance = float(params.get("retest_tolerance", 0.03))
        level_ok = np.isfinite(breakout_level) & (breakout_level > 0)
        retest = (
            valid
            & recent_breakout
            & level_ok
            & ~breakout
            & (close >= breakout_level * (1.0 - tolerance))
            & (close <= breakout_level * (1.0 + tolerance))
            & (market.low <= breakout_level * (1.0 + tolerance))
            & (market.high >= breakout_level * (1.0 - tolerance))
        )
        vol_ratio = matrix_feature(market, "vol_ratio_5d")
        ma60 = matrix_feature(market, "ma60")
        entry = (
            retest
            & np.isfinite(vol_ratio)
            & (vol_ratio < float(params.get("max_vol_ratio", 1.0)))
            & np.isfinite(ma60)
            & (close > ma60)
        )
        ma20 = matrix_feature(market, "ma20")
        exit_ = valid & (
            (np.isfinite(ma20) & (close < ma20))
            | (level_ok & (close < breakout_level * 0.97))
        )

        distance = np.zeros(market.shape, dtype=np.float32)
        np.divide(close - breakout_level, breakout_level, out=distance, where=level_ok)
        score = (
            np.nan_to_num(matrix_feature(market, "momentum_60d"), nan=0.0)
            - np.abs(np.nan_to_num(distance, nan=0.0)) * 2.0
            + 0.15 * np.nan_to_num(matrix_feature(market, "ma60_bias"), nan=0.0)
        ).astype(np.float32, copy=False)
        score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
        )


MATRIX_STRATEGY = BreakoutRetestHoldStrategy()
