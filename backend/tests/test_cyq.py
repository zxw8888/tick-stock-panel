"""筹码分布 (CYQ) 移植的回归测试.

覆盖:
  - 输出结构完整 (x/y/b/benefit_part/avg_cost/percent_chips)
  - 价格档位单调、筹码非负、总量守恒
  - benefit_part / avg_cost 的解析关系
  - 窗口语义 (crange / cyq_days / 索引越界)
  - 边界: 空表 / 超短 / 一字板 / 零换手
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.indicators.chips import compute_cyq, compute_cyq_frame


def _frame(n: int = 400, seed: int = 1, flat: bool = False) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0004, 0.017, n)
    close = 20.0 * np.cumprod(1.0 + ret)
    open_ = close * (1.0 + rng.normal(0.0, 0.004, n))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.006, n)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.006, n)))
    if flat:
        high[:] = close
        low[:] = close
        open_[:] = close
    turnover = np.clip(np.abs(rng.normal(2.5, 1.8, n)), 0.0, 100.0)
    return pl.DataFrame({
        "symbol": ["600000"] * n,
        "date": [date(2023, 1, 2) + timedelta(days=i) for i in range(n)],
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": rng.integers(1_000_000, 50_000_000, n).astype(float),
        "turnover_rate": turnover,
    })


def test_result_shape_and_keys():
    got = compute_cyq_frame(_frame())
    assert set(got) == {
        "x", "y", "b", "d", "t", "benefit_part", "avg_cost", "percent_chips",
    }
    assert len(got["x"]) == len(got["y"]) == 150
    assert got["t"] == 210
    assert set(got["percent_chips"]) == {"90", "70"}


def test_price_axis_is_monotonic_and_uniform():
    got = compute_cyq_frame(_frame())
    y = np.asarray(got["y"])
    assert np.all(np.diff(y) > 0)
    steps = np.diff(y)
    assert steps.max() - steps.min() < 0.05, "价格档位应近似等距"


def test_chips_are_non_negative_and_bounded():
    """筹码量必须非负; 总量由换手衰减决定, 不做归一化 (与 InStock 一致)."""
    got = compute_cyq_frame(_frame())
    x = np.asarray(got["x"])
    assert np.all(x >= 0.0)
    assert x.sum() > 0.0


def test_benefit_part_is_fraction():
    got = compute_cyq_frame(_frame())
    assert 0.0 <= got["benefit_part"] <= 1.0


def test_avg_cost_within_price_range():
    df = _frame(n=400)
    got = compute_cyq_frame(df)
    lo = float(df["low"].to_numpy()[-210:].min())
    hi = float(df["high"].to_numpy()[-210:].max())
    assert lo - 0.01 <= float(got["avg_cost"]) <= hi + 0.01


def test_percent_chips_ranges_are_ordered():
    got = compute_cyq_frame(_frame())
    p90 = got["percent_chips"]["90"]["priceRange"]
    p70 = got["percent_chips"]["70"]["priceRange"]
    assert float(p90[0]) <= float(p90[1])
    assert float(p70[0]) <= float(p70[1])
    # 70% 区间必须落在 90% 区间内
    assert float(p90[0]) <= float(p70[0]) and float(p70[1]) <= float(p90[1])


def test_concentration_is_between_zero_and_one():
    got = compute_cyq_frame(_frame())
    for key in ("90", "70"):
        conc = got["percent_chips"][key]["concentration"]
        assert 0.0 <= conc <= 1.0


def test_index_none_uses_last_bar():
    df = _frame()
    last = compute_cyq_frame(df, index=len(df) - 1)
    default = compute_cyq_frame(df)
    assert default["x"] == last["x"]
    assert default["avg_cost"] == last["avg_cost"]


def test_different_index_gives_different_distribution():
    """窗口必须落在 [crange + cyq_days, n) 内才非空 (InStock 同语义)."""
    df = _frame(n=600)
    a = compute_cyq_frame(df, index=400)
    b = compute_cyq_frame(df, index=len(df) - 1)
    assert a and b
    assert a["avg_cost"] != b["avg_cost"]


def test_accuracy_factor_controls_resolution():
    df = _frame()
    coarse = compute_cyq_frame(df, accuracy_factor=50)
    fine = compute_cyq_frame(df, accuracy_factor=300)
    assert len(coarse["y"]) == 50
    assert len(fine["y"]) == 300


def test_cyq_days_limits_lookback():
    df = _frame(n=600)
    short = compute_cyq_frame(df, cyq_days=30)
    long_ = compute_cyq_frame(df, cyq_days=210)
    assert short["t"] == 30 and long_["t"] == 210
    # 回看窗口不同, 价格轴范围必然不同
    assert (short["y"][0], short["y"][-1]) != (long_["y"][0], long_["y"][-1])


def test_empty_input_returns_empty_dict():
    empty = pl.DataFrame(schema={
        "symbol": pl.Utf8, "date": pl.Date,
        "open": pl.Float64, "high": pl.Float64,
        "low": pl.Float64, "close": pl.Float64,
        "turnover_rate": pl.Float64,
    })
    assert compute_cyq_frame(empty) == {}


def test_flat_price_series_does_not_crash():
    """一字板: high == low, 走 g_x 的特殊分支."""
    got = compute_cyq_frame(_frame(n=400, flat=True))
    assert got  # 不应返回空
    assert np.all(np.asarray(got["x"]) >= 0.0)


def test_zero_turnover_keeps_initial_state():
    df = _frame(n=400).with_columns(pl.lit(0.0).alias("turnover_rate"))
    got = compute_cyq_frame(df)
    assert np.allclose(np.asarray(got["x"]), 0.0)
    assert got["benefit_part"] == 0.0


def test_dates_are_echoed():
    df = _frame(n=400)
    got = compute_cyq_frame(df)
    assert got["d"] == str(df["date"].to_numpy()[-1])


def test_degenerate_window_returns_empty():
    """切片窗口为空时返回 {} (InStock 同位置抛 IndexError).

    实测 (n=400): index=0/50/100 有数据, index=120/200 空窗口.
    原因是 start 为负时按 Python 负索引解析后 lo > hi.
    """
    df = _frame(n=400)
    assert compute_cyq_frame(df, index=120) == {}
    assert compute_cyq_frame(df, index=200) == {}
    assert compute_cyq_frame(df, index=0) != {}
    assert compute_cyq_frame(df, index=100) != {}


def test_short_series_still_computes():
    """n < crange + cyq_days 时 Python 负索引切片仍能取到数据, 与 InStock 一致."""
    got = compute_cyq_frame(_frame(n=200))
    assert got and got["t"] == 210
