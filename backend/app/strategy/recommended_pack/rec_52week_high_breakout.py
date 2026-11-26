"""52-week high breakout with trend, volume and daily-strength confirmation."""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
)
from app.backtest.matrix import valid_shift as shift

META = {
    "id": "rec_52week_high_breakout",
    "name": "52周新高突破",
    "description": "收盘突破前240日最高价，并要求MA120趋势、量比和当日涨幅同时确认。",
    "tags": ["推荐", "52周新高", "突破", "趋势"],
    "origin": "recommended",
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 3,
        "price_max": 300,
        "market_cap_min": 20e8,
        "amount_min": 1.0e8,
        "exclude_st": True,
        "exclude_new_days": 80,
    },
    "params": [
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float", "default": 1.2, "min": 0.8, "max": 5.0, "step": 0.1},
        {"id": "min_change_pct", "label": "最低当日涨幅%", "type": "float", "default": 1.0, "min": 0.0, "max": 10.0, "step": 0.5},
    ],
    "scoring": {"momentum_60d": 0.4, "change_pct": 0.3, "vol_ratio_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    "version": "1.0.0",
    "sources": ["George & Hwang (2004), The 52-Week High and Momentum Investing"],
    "suitable_for": {
        "market_cap": ["mid", "large"],
        "styles": ["trend", "breakout", "leader"],
        "volatility": ["normal"],
        "liquidity": ["high"],
        "boards": ["沪主板", "深主板", "创业板", "科创板"],
        "avoid": ["ST", "次新", "低流动性", "高波动", "退市风险"],
        "note": "适合创新高后仍有量能配合的中大盘强势股，以及处于阶段新高的宽基/行业ETF。",
    },
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.07
MAX_HOLD_DAYS = 25


class FiftyTwoWeekHighBreakoutStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"high", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 270

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close
        valid = np.isfinite(close)
        high_valid = valid & np.isfinite(market.high)
        prior_high = shift(valid_rolling_max(market.high, high_valid, 240), 1)
        ma120 = matrix_feature(market, "ma120")
        vol_ratio = matrix_feature(market, "vol_ratio_5d")
        change_pct = matrix_feature(market, "change_pct")
        momentum = matrix_feature(market, "momentum_60d")

        entry = (
            valid
            & np.isfinite(prior_high)
            & np.isfinite(ma120)
            & np.isfinite(vol_ratio)
            & np.isfinite(change_pct)
            & (close > prior_high)
            & (close > ma120)
            & (vol_ratio >= float(params.get("vol_ratio_min", 1.2)))
            & (change_pct >= float(params.get("min_change_pct", 1.0)) / 100.0)
        )
        ma50 = matrix_feature(market, "ma50")
        exit_ = valid & np.isfinite(ma50) & (close < ma50)

        strength = np.zeros(market.shape, dtype=np.float32)
        np.divide(close - prior_high, prior_high, out=strength, where=np.isfinite(prior_high) & (prior_high > 0))
        score = (
            np.nan_to_num(strength, nan=0.0) * 3.0
            + np.nan_to_num(momentum, nan=0.0)
            + 0.1 * np.nan_to_num(vol_ratio, nan=0.0)
        ).astype(np.float32, copy=False)
        score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
        )


MATRIX_STRATEGY = FiftyTwoWeekHighBreakoutStrategy()
