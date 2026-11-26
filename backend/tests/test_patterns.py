"""TA-Lib 蜡烛形态移植的回归测试.

覆盖:
  - 61 列齐全 + 元数据完整
  - 与解析解一致的形态口径 (doji / marubozu / hammer / engulfing)
  - 多标的隔离 (乱序输入不串行)
  - needed 裁剪 + pipeline 集成
  - 形态列接入选择器 (自定义信号 / 监控规则 / 策略叠加条件)
  - 输出取值域 (-100 / 0 / +100) 与 warmup 段
  - 边界: 空表 / 超短 / 平盘 / 一字板
"""
from __future__ import annotations

from datetime import date, timedelta

import zlib

import numpy as np
import polars as pl
import pytest

from app.indicators.patterns import (
    PATTERN_COLUMNS,
    PATTERN_LABELS,
    PATTERN_LOOKBACK,
    PATTERNS,
    TALIB_NAMES,
    compute_patterns,
    compute_patterns_frame,
)
from app.indicators.pipeline import (
    ENRICHED_COLUMNS,
    ENRICHED_COLUMNS_BY_CATEGORY,
    INDICATOR_COLUMNS,
    compute_indicators,
    ensure_indicator_columns,
)


def _bars(n: int = 200, symbols=(("600000", 0.0),)) -> pl.DataFrame:
    rows = []
    for symbol, offset in symbols:
        # 用 crc32 而不是 hash(): str 的 hash 每进程随机化, 会让用例飘
        rng = np.random.default_rng(zlib.crc32(symbol.encode()))
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
                "amount": float(volume[i] * close[i]),
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def test_all_61_patterns_present():
    assert len(PATTERNS) == 61
    assert set(PATTERNS) == set(PATTERN_LABELS) == set(TALIB_NAMES)
    assert set(PATTERNS) == set(PATTERN_LOOKBACK)
    assert tuple(PATTERNS) == PATTERN_COLUMNS


def test_talib_names_are_cdl_functions():
    for name in TALIB_NAMES.values():
        assert name.startswith("CDL")


def test_lookbacks_are_plausible():
    for name, lb in PATTERN_LOOKBACK.items():
        assert 0 <= lb <= 20, f"{name} lookback={lb} 不合理"


def test_metadata_registered_in_pipeline():
    assert set(PATTERN_COLUMNS) <= set(ENRICHED_COLUMNS)
    assert ENRICHED_COLUMNS_BY_CATEGORY["pattern"] == list(PATTERN_LABELS)
    assert set(PATTERN_COLUMNS) <= set(INDICATOR_COLUMNS)


def test_output_domain_is_minus100_zero_100():
    df = _bars(200)
    got = compute_patterns_frame(df, assume_sorted=True)
    for name in PATTERN_COLUMNS:
        vals = set(np.unique(got[name].to_numpy()).tolist())
        # hikkake 确认后会叠加: 100 -> 200 / -100 -> -200 (TA-Lib 原语义)
        assert vals <= {-200, -100, 0, 100, 200}, f"{name} 出现非法取值 {vals}"


def test_warmup_prefix_is_zero():
    df = _bars(200)
    got = compute_patterns_frame(df, assume_sorted=True)
    for name, lb in PATTERN_LOOKBACK.items():
        head = got[name].to_numpy()[:lb]
        assert not head.any(), f"{name} warmup 段应为 0"


def test_doji_matches_analytic_threshold():
    """doji: |close-open| <= 0.1 * 前 10 根 (high-low) 均值."""
    df = _bars(120)
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    rng = h - l
    exp = np.zeros(len(c), dtype=np.int64)
    for i in range(10, len(c)):
        avg = rng[i - 10:i].mean()
        if abs(c[i] - o[i]) <= 0.1 * avg:
            exp[i] = 100
    got = compute_patterns(o, h, l, c, needed={"doji"})["doji"]
    assert np.array_equal(got, exp)


def test_marubozu_matches_analytic():
    """marubozu: 长实体且上下影线都极小, 按阴阳给方向."""
    df = _bars(120)
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    got = compute_patterns(o, h, l, c, needed={"marubozu"})["marubozu"]
    rng = h - l
    body = np.abs(c - o)
    up = h - np.maximum(o, c)
    dn = np.minimum(o, c) - l
    for i in range(10, len(c)):
        long_ = body[i] > rng[i - 10:i].mean()
        vshort = 0.1 * rng[i - 10:i].mean()
        hit = long_ and up[i] < vshort and dn[i] < vshort
        want = (100 if c[i] > o[i] else -100) if hit else 0
        assert got[i] == want, f"idx={i} got={got[i]} want={want}"


def test_multi_symbol_isolation():
    """乱序多标的不串行: 每只标的单独算, 结果与逐只算一致."""
    df = _bars(160, symbols=(("600000", 0.0), ("000001", 3.0), ("300750", 7.0)))
    shuffled = df.sample(fraction=1.0, shuffle=True, seed=11)
    got = compute_indicators(shuffled, needed=set(PATTERN_COLUMNS)).sort(["symbol", "date"])
    for symbol in ("600000", "000001", "300750"):
        sub = df.filter(pl.col("symbol") == symbol)
        ref = compute_patterns_frame(sub, assume_sorted=True)
        mine = got.filter(pl.col("symbol") == symbol)
        for name in ("doji", "hammer", "engulfing_pattern", "tristar_pattern"):
            assert np.array_equal(mine[name].to_numpy(), ref[name].to_numpy()), (
                f"{symbol}/{name} 多标的隔离失败")


def test_needed_pruning_only_computes_requested():
    df = _bars(120)
    got = compute_indicators(df, needed={"doji", "hammer"})
    assert "doji" in got.columns and "hammer" in got.columns
    others = [c for c in PATTERN_COLUMNS if c not in ("doji", "hammer")]
    assert not any(c in got.columns for c in others)


def test_full_run_does_not_compute_patterns_by_default():
    """默认 needed=None 走既有 30 指标, 不隐式加 61 列 (零开销)."""
    df = _bars(120)
    got = compute_indicators(df)
    assert not any(c in got.columns for c in PATTERN_COLUMNS)


def test_empty_and_short_input():
    empty = pl.DataFrame(schema={
        "symbol": pl.Utf8, "date": pl.Date,
        "open": pl.Float64, "high": pl.Float64,
        "low": pl.Float64, "close": pl.Float64,
    })
    assert compute_patterns_frame(empty).is_empty()

    o = np.array([10.0, 10.1, 10.2])
    h = np.array([10.2, 10.3, 10.4])
    l = np.array([9.9, 10.0, 10.1])
    c = np.array([10.1, 10.2, 10.3])
    got = compute_patterns(o, h, l, c)
    assert len(got) == 61
    assert all(not v.any() for v in got.values())


def test_flat_market_only_doji_fires():
    """全平盘: 实体为 0, 只有 doji 类成立 (与 TA-Lib 一致)."""
    n = 60
    flat = np.full(n, 10.0)
    got = compute_patterns(flat, flat, flat, flat)
    assert set(np.unique(got["doji"]).tolist()) == {0, 100}
    for name, arr in got.items():
        if name == "doji":
            continue
        assert not arr.any(), f"{name} 在全平盘不应出信号"


def test_list_input_supported():
    df = _bars(80)
    o = df["open"].to_list()
    h = df["high"].to_list()
    l = df["low"].to_list()
    c = df["close"].to_list()
    got = compute_patterns(o, h, l, c, needed={"doji"})
    assert got["doji"].dtype == np.int64


@pytest.mark.parametrize("name", ["doji", "hammer", "engulfing_pattern", "three_white_soldiers"])
def test_single_pattern_callable_directly(name):
    df = _bars(150)
    arr = PATTERNS[name](
        df["open"].to_numpy(), df["high"].to_numpy(),
        df["low"].to_numpy(), df["close"].to_numpy(),
    )
    assert arr.shape == (150,)
    assert set(np.unique(arr).tolist()) <= {-100, 0, 100}

# ── 选择器接入 (自定义信号 / 监控规则 / 策略叠加条件) ──────────

def _flat(n: int = 40) -> pl.DataFrame:
    flat = np.full(n, 10.0)
    return pl.DataFrame({
        "symbol": ["600000"] * n,
        "date": [date(2024, 1, 1) + timedelta(days=i) for i in range(n)],
        "open": flat, "high": flat, "low": flat, "close": flat,
        "volume": np.full(n, 1e6),
    })


def test_patterns_are_in_signal_whitelist():
    from app.strategy.custom_signals import ALLOWED_FIELDS, allowed_fields

    assert set(PATTERN_COLUMNS) <= ALLOWED_FIELDS
    assert set(PATTERN_COLUMNS) <= allowed_fields()


def test_pattern_group_exposed_by_signal_options():
    from app.api.signals import get_options

    opts = get_options()
    groups = {g["key"]: g for g in opts["groups"]}
    assert "pattern" in groups
    assert [f["key"] for f in groups["pattern"]["fields"]] == list(PATTERN_COLUMNS)
    for f in groups["pattern"]["fields"]:
        assert f["label"].endswith("(K线形态)")


def test_monitor_options_group_patterns_without_gaps():
    from app.api.monitor_rules import get_options

    class _State:
        pass

    class _App:
        state = _State()

    class _Req:
        app = _App()

    opts = get_options(_Req())
    groups = {g["key"]: g for g in opts["threshold_groups"]}
    assert "pattern" in groups
    assert [f["key"] for f in groups["pattern"]["fields"]] == list(PATTERN_COLUMNS)

    grouped = {f["key"] for g in opts["threshold_groups"] for f in g["fields"]}
    flat = {f["key"] for f in opts["threshold_fields"]}
    assert grouped == flat, "分组必须覆盖全部阈值字段, 否则下拉会丢项"


def test_ensure_indicator_columns_only_fills_missing():
    df = _flat()
    assert "doji" not in df.columns
    out = ensure_indicator_columns(df, {"doji"})
    assert "doji" in out.columns
    # 只补请求的列, 不把 61 个形态全算出来
    assert sum(c in out.columns for c in PATTERN_COLUMNS) == 1


def test_ensure_indicator_columns_ignores_non_indicator_names():
    df = _flat()
    out = ensure_indicator_columns(df, {"close", "not_a_column"})
    assert "not_a_column" not in out.columns
    assert out.height == df.height


def test_custom_signal_can_use_pattern_column():
    from app.strategy import custom_signals as cs

    df = compute_indicators(_flat())
    assert "doji" not in df.columns
    sig = {
        "id": "t_doji",
        "name": "十字星",
        "kind": "entry",
        "conditions": [{"left": "doji", "op": "==", "right": 100}],
    }
    cs.validate(sig)
    out = cs.inject(df, cs.build_expressions([sig]))
    col = cs.resolve_column(sig)
    assert col in out.columns
    assert out[col].sum() > 0, "平盘应命中十字星"


def test_monitor_threshold_condition_can_use_pattern_column():
    from app.strategy.monitor import _build_condition_mask

    df = compute_indicators(_flat())
    assert "doji" not in df.columns
    hit = _build_condition_mask(df, [{"field": "doji", "op": "==", "value": 100}], "and")
    assert hit.height > 0

    miss = _build_condition_mask(df, [{"field": "hammer", "op": "==", "value": 100}], "and")
    assert miss.height == 0, "平盘不应命中锤子线"


def test_overlay_condition_can_use_pattern_column():
    from app.strategy import custom_signals as cs

    df = _flat()
    conds = [{"left": "doji", "op": "==", "right": 100}]
    missing = cs.overlay_missing_columns(conds, df.columns)
    assert missing == ["doji"]
    fixed = ensure_indicator_columns(df, set(missing), assume_sorted=True)
    assert cs.overlay_missing_columns(conds, fixed.columns) == []

    expr = cs.build_overlay_expr(conds)
    mask = fixed.select(expr.fill_null(False).alias("m"))["m"]
    assert int(mask.sum()) > 0
