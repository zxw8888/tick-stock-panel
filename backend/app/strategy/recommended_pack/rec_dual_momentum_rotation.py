"""Dual momentum: absolute MA200 trend plus 20/60/120-day relative momentum."""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)

META = {
    "id": "rec_dual_momentum_rotation",
    "name": "双动量轮动",
    "description": "MA200绝对动量过滤，再按20/60/120日相对动量排序；用于趋势资产轮动与强弱切换。",
    "tags": ["推荐", "双动量", "轮动", "趋势"],
    "origin": "recommended",
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 2,
        "price_max": 300,
        "market_cap_min": 20e8,
        "amount_min": 0.5e8,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {"id": "min_momentum_60d", "label": "最低60日动量", "type": "float", "default": 0.0, "min": -0.2, "max": 0.4, "step": 0.01},
        {"id": "min_momentum_120d", "label": "最低120日动量", "type": "float", "default": -0.02, "min": -0.4, "max": 0.5, "step": 0.01},
    ],
    "scoring": {"momentum_60d": 0.45, "momentum_120d": 0.3, "momentum_20d": 0.25},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    "version": "1.0.0",
    "sources": ["Antonacci (2014), Dual Momentum Investing", "Faber (2007), A Quantitative Approach to Tactical Asset Allocation"],
    "suitable_for": {
        "market_cap": ["mid", "large"],
        "styles": ["trend", "leader"],
        "volatility": ["normal"],
        "liquidity": ["high"],
        "boards": ["沪主板", "深主板", "创业板", "科创板"],
        "avoid": ["ST", "次新", "低流动性", "高波动", "杠杆ETF"],
        "note": "适合趋势清晰的中大盘行业龙头和宽基/行业ETF，不适合长期震荡或流动性不足的标的。",
    },
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 45


class DualMomentumRotationStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 260

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close
        valid = np.isfinite(close)
        ma200 = matrix_feature(market, "ma200")
        mom20 = matrix_feature(market, "momentum_20d")
        mom60 = matrix_feature(market, "momentum_60d")
        mom120 = matrix_feature(market, "momentum_120d")

        entry = (
            valid
            & np.isfinite(ma200)
            & np.isfinite(mom60)
            & np.isfinite(mom120)
            & (close > ma200)
            & (mom60 > float(params.get("min_momentum_60d", 0.0)))
            & (mom120 > float(params.get("min_momentum_120d", -0.02)))
        )
        exit_ = valid & (
            (close < ma200)
            | (np.isfinite(mom60) & (mom60 < 0.0))
        )

        score = (
            0.45 * np.nan_to_num(mom60, nan=0.0)
            + 0.30 * np.nan_to_num(mom120, nan=0.0)
            + 0.25 * np.nan_to_num(mom20, nan=0.0)
        ).astype(np.float32, copy=False)
        score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
        )


MATRIX_STRATEGY = DualMomentumRotationStrategy()
