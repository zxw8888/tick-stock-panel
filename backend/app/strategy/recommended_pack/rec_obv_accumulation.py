"""OBV accumulation with price trend and liquidity confirmation."""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)

META = {
    "id": "rec_obv_accumulation",
    "name": "OBV吸筹",
    "description": "OBV 20日趋势向上、价格站上MA60且成交额达标，识别量价同步的吸筹结构。",
    "tags": ["推荐", "OBV", "量价", "吸筹"],
    "origin": "recommended",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 3,
        "price_max": 300,
        "market_cap_min": 20e8,
        "amount_min": 1.0e8,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {"id": "min_obv_trend", "label": "最低OBV趋势", "type": "float", "default": 0.1, "min": -0.5, "max": 0.8, "step": 0.05},
        {"id": "min_amount_mean", "label": "最低20日成交额(亿)", "type": "float", "default": 0.8, "min": 0.1, "max": 20.0, "step": 0.1},
    ],
    "scoring": {"obv_trend_20d": 0.5, "momentum_60d": 0.3, "amount_mean_20d": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    "version": "1.0.0",
    "sources": ["Granville (1976), New Key to Stock Market Profits", "Murphy (1999), Technical Analysis of the Financial Markets"],
    "suitable_for": {
        "market_cap": ["mid", "large"],
        "styles": ["trend", "leader"],
        "volatility": ["normal"],
        "liquidity": ["high"],
        "boards": ["沪主板", "深主板", "创业板", "科创板"],
        "avoid": ["ST", "次新", "低流动性", "高波动", "退市风险"],
        "note": "适合成交活跃、量价配合的中大盘股票；不适合靠单日脉冲放量制造的量价假象。",
    },
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.07
MAX_HOLD_DAYS = 35


class ObvAccumulationStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume", "amount"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 120

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close
        valid = np.isfinite(close)
        obv = matrix_feature(market, "obv_trend_20d")
        amount_mean = matrix_feature(market, "amount_mean_20d")
        ma60 = matrix_feature(market, "ma60")
        mom20 = matrix_feature(market, "momentum_20d")

        entry = (
            valid
            & np.isfinite(obv)
            & np.isfinite(amount_mean)
            & np.isfinite(ma60)
            & (obv > float(params.get("min_obv_trend", 0.1)))
            & (amount_mean >= float(params.get("min_amount_mean", 0.8)))
            & (close > ma60)
            & (np.isfinite(mom20) & (mom20 > 0.0))
        )
        exit_ = valid & (
            (np.isfinite(ma60) & (close < ma60))
            | (np.isfinite(obv) & (obv < -0.05))
        )

        score = (
            2.0 * np.nan_to_num(obv, nan=0.0)
            + np.nan_to_num(matrix_feature(market, "momentum_60d"), nan=0.0)
            + 0.1 * np.log1p(np.maximum(np.nan_to_num(amount_mean, nan=0.0), 0.0))
        ).astype(np.float32, copy=False)
        score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
        )


MATRIX_STRATEGY = ObvAccumulationStrategy()
