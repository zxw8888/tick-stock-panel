"""RSI2 extreme reversion inside a long-term uptrend."""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)

META = {
    "id": "rec_rsi2_extreme_reversion",
    "name": "RSI2极端反转",
    "description": "RSI2低于10、MA200长期趋势向上且5日急跌，捕捉短期极端超卖后的反弹。",
    "tags": ["推荐", "RSI2", "超跌反弹", "均值回归"],
    "origin": "recommended",
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 2,
        "price_max": 300,
        "market_cap_min": 20e8,
        "amount_min": 0.8e8,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {"id": "rsi2_max", "label": "RSI2上限", "type": "float", "default": 10.0, "min": 1.0, "max": 25.0, "step": 1.0},
        {"id": "max_momentum_5d", "label": "5日跌幅阈值", "type": "float", "default": -0.04, "min": -0.2, "max": 0.0, "step": 0.005},
        {"id": "require_ma200", "label": "要求MA200上方", "type": "bool", "default": True},
    ],
    "scoring": {"momentum_5d": 0.5, "rsi_2": 0.3, "momentum_60d": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    "version": "1.0.0",
    "sources": ["Connors & Alvarez (2008), Short Term Trading Strategies That Work"],
    "suitable_for": {
        "market_cap": ["large", "mid"],
        "styles": ["reversal", "range"],
        "volatility": ["normal"],
        "liquidity": ["high"],
        "boards": ["沪主板", "深主板", "创业板", "科创板"],
        "avoid": ["ST", "次新", "低流动性", "高波动", "退市风险"],
        "note": "适合长期趋势向上、短期急跌但流动性充足的股票和ETF；不适合基本面暴雷或持续阴跌标的。",
    },
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 10


class Rsi2ExtremeReversionStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 240

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close
        valid = np.isfinite(close)
        rsi2 = matrix_feature(market, "rsi_2")
        momentum_5d = matrix_feature(market, "momentum_5d")
        ma200 = matrix_feature(market, "ma200")
        ma10 = matrix_feature(market, "ma10")

        entry = (
            valid
            & np.isfinite(rsi2)
            & np.isfinite(momentum_5d)
            & (rsi2 < float(params.get("rsi2_max", 10.0)))
            & (momentum_5d <= float(params.get("max_momentum_5d", -0.04)))
        )
        if params.get("require_ma200", True):
            entry &= np.isfinite(ma200) & (close > ma200)
        exit_ = valid & (
            (np.isfinite(ma10) & (close > ma10))
            | (np.isfinite(rsi2) & (rsi2 > 60.0))
        )

        score = (
            -2.0 * np.nan_to_num(momentum_5d, nan=0.0)
            + np.nan_to_num(10.0 - rsi2, nan=0.0) / 10.0
            + 0.2 * np.nan_to_num(matrix_feature(market, "momentum_60d"), nan=0.0)
        ).astype(np.float32, copy=False)
        score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
        )


MATRIX_STRATEGY = Rsi2ExtremeReversionStrategy()
