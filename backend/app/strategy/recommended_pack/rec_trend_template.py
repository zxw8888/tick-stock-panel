"""Minervini-style trend template using moving-average structure and proximity to highs."""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import valid_shift as shift

META = {
    "id": "rec_trend_template",
    "name": "趋势模板",
    "description": "MA50>MA150>MA200、MA200向上、距240日高点不超过25%，复刻Minervini趋势模板。",
    "tags": ["推荐", "趋势模板", "均线", "成长"],
    "origin": "recommended",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 5,
        "price_max": 500,
        "market_cap_min": 30e8,
        "amount_min": 1.0e8,
        "exclude_st": True,
        "exclude_new_days": 120,
    },
    "params": [
        {"id": "max_distance_to_high", "label": "距240日高点上限", "type": "float", "default": 0.25, "min": 0.05, "max": 0.6, "step": 0.05},
        {"id": "min_momentum_60d", "label": "最低60日动量", "type": "float", "default": 0.0, "min": -0.2, "max": 0.4, "step": 0.01},
    ],
    "scoring": {"momentum_60d": 0.55, "distance_to_high_240d": 0.25, "ma200_bias": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    "version": "1.0.0",
    "sources": ["Minervini (2013), Trade Like a Stock Market Wizard"],
    "suitable_for": {
        "market_cap": ["mid", "large"],
        "styles": ["trend", "leader"],
        "volatility": ["low", "normal"],
        "liquidity": ["high"],
        "boards": ["沪主板", "深主板", "创业板", "科创板"],
        "avoid": ["ST", "次新", "低流动性", "高波动", "退市风险"],
        "note": "适合长期均线多头排列、回撤受控的中大盘成长龙头；不适合短线题材连板和长期下降趋势股。",
    },
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60


class TrendTemplateStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 300

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close
        valid = np.isfinite(close)
        ma50 = matrix_feature(market, "ma50")
        ma150 = matrix_feature(market, "ma150")
        ma200 = matrix_feature(market, "ma200")
        ma200_prior = shift(ma200, 20)
        momentum = matrix_feature(market, "momentum_60d")
        distance = matrix_feature(market, "distance_to_high_240d")
        max_distance = float(params.get("max_distance_to_high", 0.25))

        entry = (
            valid
            & np.isfinite(ma50)
            & np.isfinite(ma150)
            & np.isfinite(ma200)
            & np.isfinite(ma200_prior)
            & np.isfinite(momentum)
            & np.isfinite(distance)
            & (close > ma50)
            & (ma50 > ma150)
            & (ma150 > ma200)
            & (ma200 > ma200_prior)
            & (distance >= -max_distance)
            & (momentum > float(params.get("min_momentum_60d", 0.0)))
        )
        exit_ = valid & np.isfinite(ma50) & (close < ma50)

        score = (
            np.nan_to_num(momentum, nan=0.0)
            - 2.0 * np.nan_to_num(distance, nan=0.0)
            + 0.2 * np.nan_to_num(matrix_feature(market, "ma200_bias"), nan=0.0)
        ).astype(np.float32, copy=False)
        score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
        )


MATRIX_STRATEGY = TrendTemplateStrategy()
