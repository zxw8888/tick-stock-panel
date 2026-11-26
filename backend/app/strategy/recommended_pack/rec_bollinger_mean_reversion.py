"""Bollinger mean reversion inside a long-term uptrend."""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)

META = {
    "id": "rec_bollinger_mean_reversion",
    "name": "布林均值回归",
    "description": "长期趋势向上时，价格跌破布林下轨且RSI14超卖，等待回归MA20。",
    "tags": ["推荐", "布林带", "均值回归", "超跌"],
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
        {"id": "rsi_max", "label": "RSI14上限", "type": "float", "default": 30.0, "min": 15.0, "max": 45.0, "step": 1.0},
        {"id": "require_ma200", "label": "要求MA200上方", "type": "bool", "default": True},
    ],
    "scoring": {"boll_position": 0.5, "rsi_14": 0.3, "momentum_60d": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    "version": "1.0.0",
    "sources": ["Bollinger (2001), Bollinger on Bollinger Bands", "Connors & Alvarez (2008), Short Term Trading Strategies That Work"],
    "suitable_for": {
        "market_cap": ["large", "mid"],
        "styles": ["reversal", "range"],
        "volatility": ["low", "normal"],
        "liquidity": ["high"],
        "boards": ["沪主板", "深主板", "创业板", "科创板"],
        "avoid": ["ST", "次新", "低流动性", "高波动", "退市风险"],
        "note": "适合长期趋势仍向上、短期超跌的流动性好的股票或ETF；不适合基本面恶化或单边下跌标的。",
    },
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15


class BollingerMeanReversionStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 240

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close
        valid = np.isfinite(close)
        lower = matrix_feature(market, "boll_lower")
        upper = matrix_feature(market, "boll_upper")
        ma20 = matrix_feature(market, "ma20")
        ma200 = matrix_feature(market, "ma200")
        rsi = matrix_feature(market, "rsi_14")

        entry = (
            valid
            & np.isfinite(lower)
            & np.isfinite(rsi)
            & (close < lower)
            & (rsi < float(params.get("rsi_max", 30.0)))
        )
        if params.get("require_ma200", True):
            entry &= np.isfinite(ma200) & (close > ma200)
        exit_ = valid & (
            (np.isfinite(ma20) & (close > ma20))
            | (np.isfinite(rsi) & (rsi > 55.0))
        )

        width = upper - lower
        position = np.full(market.shape, 0.0, dtype=np.float32)
        np.divide(close - lower, width, out=position, where=np.isfinite(width) & (width > 0))
        score = (
            np.nan_to_num(30.0 - rsi, nan=0.0) / 30.0
            - np.nan_to_num(position, nan=0.0)
            + 0.2 * np.nan_to_num(matrix_feature(market, "momentum_60d"), nan=0.0)
        ).astype(np.float32, copy=False)
        score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
        )


MATRIX_STRATEGY = BollingerMeanReversionStrategy()
