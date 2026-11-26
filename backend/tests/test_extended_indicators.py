"""InStock 130 指标移植的回归测试.

覆盖:
  - 数值口径 (与 TA-Lib 实现稳态一致, 用解析解断言)
  - 多标的隔离 (over("symbol") 不串行)
  - 缺失数据 / 平盘 / 零成交额的边界
  - needed 裁剪与 pipeline 集成
  - warmup 期保持 null, 不产生假 0 值
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.indicators.extended import (
    BASE_INPUTS_BY_COLUMN,
    CORE_DEPS,
    EXTENDED_COLUMNS,
    EXTENDED_INPUT_COLUMNS,
    WARMUP_BARS,
    compute_extended,
)
from app.indicators.pipeline import INDICATOR_COLUMNS, compute_indicators


def _bars(
    n: int = 320,
    symbols: tuple[tuple[str, float], ...] = (("600000", 0.0),),
) -> pl.DataFrame:
    rows = []
    for symbol, offset in symbols:
        rng = np.random.default_rng(abs(hash(symbol)) % (2**31))
        ret = rng.normal(0.0004, 0.015, n)
        close = (10.0 + offset) * np.cumprod(1.0 + ret)
        open_ = close * (1.0 + rng.normal(0.0, 0.003, n))
        high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.005, n)))
        low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.005, n)))
        volume = rng.integers(800_000, 9_000_000, n).astype(float)
        for i in range(n):
            rows.append({
                "symbol": symbol,
                "date": date(2023, 1, 2) + timedelta(days=i),
                "open": float(open_[i]),
                "high": float(high[i]),
                "low": float(low[i]),
                "close": float(close[i]),
                "volume": float(volume[i]),
                "amount": float(volume[i] * (high[i] + low[i] + close[i]) / 3.0),
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def test_extended_columns_cover_instock_set():
    """InStock 的指标全部有实现."""
    expected = {
        "macd", "macds", "macdh", "kdjk", "kdjd", "kdjj",
        "boll_ub", "boll", "boll_lb", "trix", "trix_20_sma",
        "cr", "rsi", "rsi_12", "vr", "vr_6_sma", "atr",
        "pdi", "mdi", "dx", "adx", "adxr",
        "wr_6", "wr_10", "wr_14", "cci", "cci_84",
        "dma", "dma_10_sma", "tema", "mfi", "mfisma",
        "vwma", "mvwma", "ppo", "ppos", "ppoh",
        "stochrsi_k", "stochrsi_d", "wt1", "wt2",
        "supertrend", "supertrend_ub", "supertrend_lb",
        "roc", "rocma", "rocema", "obv", "sar",
        "psy", "psyma", "ar", "br", "emv", "emva",
        "bias", "bias_12", "bias_24", "dpo", "madpo",
        "vhf", "rvi", "rvis", "force_2", "force_13",
        "ene", "ene_ue", "ene_le", "vol_5", "vol_10", "ma200",
    }
    assert expected <= set(EXTENDED_COLUMNS)


def test_extended_metadata_tables_are_complete():
    assert set(EXTENDED_COLUMNS) == set(BASE_INPUTS_BY_COLUMN)
    missing = set(EXTENDED_INPUT_COLUMNS) - set(WARMUP_BARS)
    assert not missing, f"缺 warmup 声明: {sorted(missing)}"
    assert set(EXTENDED_INPUT_COLUMNS) <= set(INDICATOR_COLUMNS)


def test_extended_input_columns_exclude_pure_intermediates():
    for name in ("m_price", "high_delta", "pdm", "rvi_x", "emva_em", "b_ub"):
        assert name in EXTENDED_COLUMNS
        assert name not in EXTENDED_INPUT_COLUMNS


def test_ma_and_bias_match_analytic_formula():
    df = compute_extended(_bars(120), needed={"ma6", "ma12", "bias"}, assume_sorted=True)
    close = df["close"]
    expected_ma6 = close.rolling_mean(6)
    expected_bias = (close - expected_ma6) / expected_ma6 * 100.0
    assert np.allclose(df["ma6"].to_numpy(), expected_ma6.to_numpy(), equal_nan=True)
    assert np.allclose(df["bias"].to_numpy(), expected_bias.to_numpy(), equal_nan=True)


def test_boll_uses_population_std():
    """TA-Lib BBANDS 用 ddof=0."""
    df = compute_extended(_bars(120), needed={"boll_ub", "boll_lb", "boll"}, assume_sorted=True)
    close = df["close"]
    mid = close.rolling_mean(20)
    std0 = close.rolling_std(20, ddof=0)
    std1 = close.rolling_std(20, ddof=1)
    assert np.allclose(df["boll"].to_numpy(), mid.to_numpy(), equal_nan=True)
    assert np.allclose(df["boll_ub"].to_numpy(), (mid + 2 * std0).to_numpy(), equal_nan=True)
    assert not np.allclose(df["boll_ub"].to_numpy(), (mid + 2 * std1).to_numpy(), equal_nan=True)


def test_macdh_is_not_doubled():
    """InStock 的 macdh 是 TA-Lib 原值 (DIF-DEA), 不是 A 股常见的 x2."""
    df = compute_extended(_bars(120), needed={"macd", "macds", "macdh"}, assume_sorted=True)
    diff = (df["macd"] - df["macds"]).to_numpy()
    assert np.allclose(df["macdh"].to_numpy(), diff, equal_nan=True)


def test_wr_is_negative_scale():
    """TA-Lib WILLR 返回 -100..0."""
    df = compute_extended(_bars(120), needed={"wr_6", "wr_14"}, assume_sorted=True)
    vals = df["wr_14"].drop_nulls().to_numpy()
    assert len(vals) > 0
    assert vals.min() >= -100.0001
    assert vals.max() <= 0.0001


def test_rsi_bounded():
    df = compute_extended(_bars(120), needed={"rsi", "rsi_12"}, assume_sorted=True)
    vals = df["rsi"].drop_nulls().to_numpy()
    assert (vals >= 0).all() and (vals <= 100).all()


def test_psy_counts_up_days():
    df = compute_extended(_bars(120), needed={"psy", "price_up"}, assume_sorted=True)
    up = df["price_up"].drop_nulls().to_numpy()
    assert up.max() == 1.0 and up.min() == 0.0
    vals = df["psy"].drop_nulls().to_numpy()
    assert (vals >= 0).all() and (vals <= 100).all()


def test_obv_first_bar_equals_volume():
    df = compute_extended(_bars(50), needed={"obv"}, assume_sorted=True)
    assert df["obv"][0] == pytest.approx(float(df["volume"][0]))


def test_vr_cr_ar_non_negative():
    """VR/CR/AR 恒为正 (分子分母都是非负量)。

    BR 例外: 跳空高开时 cy_l = prev_close - low 为负, InStock 原版同样为负,
    因此不在此断言。
    """
    df = compute_extended(_bars(120), needed={"vr", "cr", "ar", "br"}, assume_sorted=True)
    for c in ("vr", "cr", "ar"):
        vals = df[c].drop_nulls().to_numpy()
        assert (vals >= 0).all(), f"{c} 出现负值"
    assert df["br"].drop_nulls().len() > 0


def test_supertrend_hugs_a_band():
    df = compute_extended(
        _bars(200), needed={"supertrend", "supertrend_ub", "supertrend_lb"},
        assume_sorted=True,
    )
    st = df["supertrend"].to_numpy()
    ub = df["supertrend_ub"].to_numpy()
    lb = df["supertrend_lb"].to_numpy()
    assert np.isfinite(st).all()
    assert np.all(np.isclose(st, ub) | np.isclose(st, lb))
    assert (ub >= lb).all()


def test_sar_near_price_range():
    df = compute_extended(_bars(150), needed={"sar"}, assume_sorted=True)
    sar = df["sar"].drop_nulls().to_numpy()
    assert len(sar) > 0
    assert sar.max() < df["high"].max() * 1.5
    assert sar.min() > df["low"].min() * 0.5


def test_symbols_are_isolated():
    multi = _bars(120, (("600000", 0.0), ("300001", 20.0)))
    got = compute_extended(
        multi, needed={"ma6", "roc", "obv", "supertrend"}, assume_sorted=True
    )
    for sym in ("600000", "300001"):
        single = multi.filter(pl.col("symbol") == sym)
        exp = compute_extended(
            single, needed={"ma6", "roc", "obv", "supertrend"}, assume_sorted=True
        )
        act = got.filter(pl.col("symbol") == sym)
        for c in ("ma6", "roc", "obv", "supertrend"):
            assert np.allclose(
                act[c].to_numpy(), exp[c].to_numpy(), equal_nan=True
            ), f"{sym} 的 {c} 串了标的"


def test_unsorted_input_gives_same_result():
    base = _bars(150, (("600000", 0.0), ("300001", 5.0)))
    shuffled = base.sample(fraction=1.0, shuffle=True, seed=7)
    a = compute_extended(base, needed={"ma6", "cci", "vr"}, assume_sorted=False)
    b = compute_extended(shuffled, needed={"ma6", "cci", "vr"}, assume_sorted=False)
    j = a.join(b, on=["symbol", "date"], suffix="_b").sort(["symbol", "date"])
    for c in ("ma6", "cci", "vr"):
        assert np.allclose(j[c].to_numpy(), j[f"{c}_b"].to_numpy(), equal_nan=True)


def test_warmup_stays_null_by_default():
    df = compute_extended(_bars(80), needed={"ma200", "cci_84"}, assume_sorted=True)
    assert df["ma200"].null_count() == df.height
    assert df["cci_84"].null_count() == df.height


def test_fill_warmup_zero_matches_instock_semantics():
    df = compute_extended(
        _bars(80), needed={"ma200"}, assume_sorted=True, fill_warmup_zero=True
    )
    assert df["ma200"].null_count() == 0
    assert (df["ma200"] == 0.0).all()


def test_missing_amount_column_still_works():
    raw = _bars(120).drop("amount")
    df = compute_extended(raw, needed={"ma6", "roc", "cr", "vwma"}, assume_sorted=True)
    assert df["ma6"].drop_nulls().len() > 0
    assert df["roc"].drop_nulls().len() > 0
    assert "cr" not in df.columns
    assert "vwma" not in df.columns


def test_flat_zero_volume_rows_do_not_produce_inf():
    n = 60
    flat = pl.DataFrame({
        "symbol": ["X"] * n,
        "date": [date(2024, 1, 1) + timedelta(days=i) for i in range(n)],
        "open": [10.0] * n, "high": [10.0] * n,
        "low": [10.0] * n, "close": [10.0] * n,
        "volume": [0.0] * n, "amount": [0.0] * n,
    })
    out = compute_extended(
        flat,
        needed={"cr", "vwma", "vr", "emv", "ar", "br", "bias", "stochrsi_k"},
        assume_sorted=True,
    )
    for c in out.columns:
        if out[c].dtype.is_float():
            assert not out[c].is_infinite().any(), f"{c} 出现 inf"


def test_pipeline_default_path_adds_no_extended_columns():
    base = compute_indicators(_bars(100))
    assert not (set(base.columns) & set(EXTENDED_COLUMNS))


def test_pipeline_needed_pulls_in_core_deps():
    out = compute_indicators(_bars(100), needed={"roc"})
    assert "roc" in out.columns
    assert set(out.columns) >= CORE_DEPS


def test_pipeline_subset_equals_direct_extended():
    raw = _bars(150)
    via_pipeline = compute_indicators(raw, needed={"cci", "supertrend", "vr"})
    direct = compute_extended(
        compute_indicators(raw), needed={"cci", "supertrend", "vr"},
        assume_sorted=True, fill_warmup_zero=True,
    )
    for c in ("cci", "supertrend", "vr"):
        assert np.allclose(
            via_pipeline[c].to_numpy(), direct[c].to_numpy(), equal_nan=True
        )


def test_pipeline_empty_frame_passthrough():
    empty = _bars(10).head(0)
    assert compute_indicators(empty, needed={"roc"}).is_empty()
