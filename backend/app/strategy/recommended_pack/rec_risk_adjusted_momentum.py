"""Risk-adjusted momentum: 60-day return divided by 20-day annualized volatility."""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)

META = {
    "id": "rec_risk_adjusted_momentum",
    "name": "风险调整动量",
    "description": "60日动量除以20日年化波动，叠加MA120趋势过滤；追求上涨效率而不是单纯涨幅。",
    "tags": ["推荐", "动量", "风险调整", "趋势"],
    "origin": "recommended",
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 3,
        "price_max": 300,
        "market_cap_min": 30e8,
        "amount_min": 0.8e8,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {"id": "min_momentum_60d", "label": "最低60日动量", "type": "float", "default": 0.05, "min": 0.0, "max": 0.5, "step": 0.01},
        {"id": "min_annual_vol", "label": "最低年化波动", "type": "float", "default": 0.12, "min": 0.02, "max": 0.8, "step": 0.01},
        {"id": "max_annual_vol", "label": "最高年化波动", "type": "float", "default": 0.65, "min": 0.1, "max": 2.0, "step": 0.05},
    ],
    "scoring": {"momentum_60d": 0.6, "annual_vol_20d": 0.2, "ma120_bias": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    "version": "1.0.0",
    "sources": ["Moskowitz, Ooi & Pedersen (2012), Time Series Momentum", "AQR momentum research"],
    "suitable_for": {
        "market_cap": ["mid", "large"],
        "styles": ["trend", "leader"],
        "volatility": ["normal"],
        "liquidity": ["high"],
        "boards": ["沪主板", "深主板", "创业板", "科创板"],
        "avoid": ["ST", "次新", "低流动性", "高波动", "退市风险"],
        "note": "适合有中期趋势、波动可控、成交活跃的中大盘龙头，也可用于宽基或行业ETF。",
    },
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 30


class RiskAdjustedMomentumStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 180

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close
        valid = np.isfinite(close)
        momentum = matrix_feature(market, "momentum_60d")
        volatility = matrix_feature(market, "annual_vol_20d")
        ma120 = matrix_feature(market, "ma120")
        min_momentum = float(params.get("min_momentum_60d", 0.05))
        min_vol = float(params.get("min_annual_vol", 0.12))
        max_vol = float(params.get("max_annual_vol", 0.65))

        entry = (
            valid
            & np.isfinite(momentum)
            & np.isfinite(volatility)
            & np.isfinite(ma120)
            & (close > ma120)
            & (momentum > min_momentum)
            & (volatility >= min_vol)
            & (volatility <= max_vol)
        )
        exit_ = valid & (
            (close < ma120)
            | (np.isfinite(momentum) & (momentum < 0.0))
        )

        safe_vol = np.maximum(np.nan_to_num(volatility, nan=0.0), 0.05)
        score = (
            np.nan_to_num(momentum, nan=0.0) / safe_vol
            + 0.15 * np.nan_to_num(matrix_feature(market, "ma120_bias"), nan=0.0)
        ).astype(np.float32, copy=False)
        score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
        )


MATRIX_STRATEGY = RiskAdjustedMomentumStrategy()
