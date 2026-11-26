"""TA-Lib 蜡烛形态识别 -- 纯 numpy 移植 (不依赖 TA-Lib).

判定条件逐条转译自 TA-Lib 0.6.x 官方 C 源码 `src/ta_func/ta_CDL*.c`,
输出与 TA-Lib 逐值一致: -100 (看跌) / 0 (无信号) / +100 (看涨).

蜡烛尺度定义 (取自 src/ta_common/ta_global.c):
    BodyLong        实体 > 1.0 x 前 10 根实体均值
    BodyVeryLong    实体 > 3.0 x 前 10 根实体均值
    BodyShort       实体 < 1.0 x 前 10 根实体均值
    BodyDoji        实体 <= 0.1 x 前 10 根 high-low 均值
    ShadowLong      影线 > 1.0 x 当根实体        (avgPeriod=0)
    ShadowVeryLong  影线 > 2.0 x 当根实体        (avgPeriod=0)
    ShadowShort     影线 < 1.0 x 前 10 根影线和均值 / 2
    ShadowVeryShort 影线 < 0.1 x 前 10 根 high-low 均值
    Near            <= 0.2 x 前 5 根 high-low 均值
    Far             >= 0.6 x 前 5 根 high-low 均值
    Equal           <= 0.05 x 前 5 根 high-low 均值

所有均值都"不含当根", 即只统计前 N 根. 数据不足 lookback 的位置输出 0.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "PATTERNS",
    "PATTERN_COLUMNS",
    "PATTERN_LABELS",
    "PATTERN_LOOKBACK",
    "TALIB_NAMES",
    "compute_patterns",
    "compute_patterns_frame",
]


def _at(arr: np.ndarray, k: int) -> np.ndarray:
    """arr[i + k] 的向量化形式; k<=0 表示回看, 越界处为 nan/False."""
    n = arr.shape[0]
    fill = False if arr.dtype == np.bool_ else np.nan
    out = np.full(arr.shape, fill, dtype=arr.dtype if arr.dtype == np.bool_ else np.float64)
    if k == 0:
        out[:] = arr
    elif k < 0:
        if n > -k:
            out[-k:] = arr[: n + k]
    elif k < n:
        out[: n - k] = arr[k:]
    return out


def _mean_at(values: np.ndarray, n: int, k: int = 0) -> np.ndarray:
    """以 i+k 结尾、往前 n 根 (不含 i+k) 的均值."""
    out = np.full(values.shape, np.nan, dtype=np.float64)
    if n <= 0:
        return out
    csum = np.concatenate(([0.0], np.cumsum(values)))
    j = np.arange(values.shape[0]) + k
    ok = (j - n >= 0) & (j <= values.shape[0])
    jj = j[ok]
    out[ok] = (csum[jj] - csum[jj - n]) / n
    return out


def _range(range_type: str, k: int, o, h, low, c) -> np.ndarray:
    if range_type == "RealBody":
        return np.abs(_at(c, k) - _at(o, k))
    if range_type == "HighLow":
        return _at(h, k) - _at(low, k)
    if range_type == "Shadows":
        return (
            _at(h, k)
            - np.maximum(_at(o, k), _at(c, k))
            + np.minimum(_at(o, k), _at(c, k))
            - _at(low, k)
        )
    raise ValueError(range_type)


def _candle_avg(range_type: str, n: int, factor: float, k: int, o, h, low, c) -> np.ndarray:
    """复刻 TA_CANDLEAVERAGE_OF: factor * (SUM / n) [/ 2 if Shadows]."""
    if n == 0:
        base = _range(range_type, k, o, h, low, c)
    else:
        base = _mean_at(_range(range_type, 0, o, h, low, c), n, k)
    return factor * base / 2.0 if range_type == "Shadows" else factor * base


def _ctx(o, h, low, c):
    def conv(a):
        a = np.asarray(a)
        return a if a.dtype == np.float64 else a.astype(np.float64)

    return conv(o), conv(h), conv(low), conv(c)


def _fill_lookback(res: np.ndarray, lb: int) -> np.ndarray:
    if lb > 0:
        res[:lb] = 0
    return res


def _cdltristar(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    n = c.shape[0]
    res = np.zeros(n, dtype=np.int64)
    avg = _candle_avg("HighLow", 10, 0.1, -2, o, h, low, c)
    body = np.abs(c - o)
    ok = (body <= avg) & (_at(body, -1) <= avg) & (_at(body, -2) <= avg)
    lo2 = np.minimum(_at(o, -2), _at(c, -2))
    hi2 = np.maximum(_at(o, -2), _at(c, -2))
    lo1 = np.minimum(_at(o, -1), _at(c, -1))
    hi1 = np.maximum(_at(o, -1), _at(c, -1))
    bear = ok & (lo1 > hi2) & (np.maximum(o, c) < hi1)
    bull = ok & (hi1 < lo2) & (np.minimum(o, c) > lo1)
    res[bear] = -100
    res[bull] = 100
    return _fill_lookback(res, 12)


def _cdlhikkake(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    n = c.shape[0]
    res = np.zeros(n, dtype=np.int64)
    cd = 0
    pattern = 0
    saved_hi = saved_lo = 0.0
    for i in range(2, n):
        inside = h[i - 1] < h[i - 2] and low[i - 1] > low[i - 2]
        down = h[i] < h[i - 1] and low[i] < low[i - 1]
        up = h[i] > h[i - 1] and low[i] > low[i - 1]
        if inside and (down or up):
            pattern = 100 if down else -100
            saved_hi, saved_lo = h[i - 1], low[i - 1]
            cd = 4
            if i >= 5:
                res[i] = pattern
        elif cd > 0 and ((pattern > 0 and c[i] > saved_hi) or (pattern < 0 and c[i] < saved_lo)):
            if i >= 5:
                res[i] = pattern + 100 * (1 if pattern > 0 else -1)
            cd = 0
        if cd > 0:
            cd -= 1
    return res


def _cdlhikkakemod(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    n = c.shape[0]
    res = np.zeros(n, dtype=np.int64)
    near = _candle_avg("HighLow", 5, 0.2, -2, o, h, low, c)
    cd = 0
    pattern = 0
    saved_hi = saved_lo = 0.0
    for i in range(7, n):
        inside = (
            h[i - 1] < h[i - 2]
            and low[i - 1] > low[i - 2]
            and h[i - 2] < h[i - 3]
            and low[i - 2] > low[i - 3]
        )
        down = h[i] < h[i - 1] and low[i] < low[i - 1]
        up = h[i] > h[i - 1] and low[i] > low[i - 1]
        hit = False
        if inside and down and c[i - 2] <= low[i - 2] + near[i]:
            pattern, saved_hi, saved_lo, cd = 100, h[i - 1], low[i - 1], 4
            hit = True
        elif inside and up and c[i - 2] >= h[i - 2] - near[i]:
            pattern, saved_hi, saved_lo, cd = -100, h[i - 1], low[i - 1], 4
            hit = True
        elif cd > 0 and ((pattern > 0 and c[i] > saved_hi) or (pattern < 0 and c[i] < saved_lo)):
            if i >= 10:
                res[i] = pattern + 100 * (1 if pattern > 0 else -1)
            cd = 0
            continue
        if hit and i >= 10:
            res[i] = pattern
        if cd > 0:
            cd -= 1
    return res


def _cdlkickingbylength(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    n = c.shape[0]
    res = np.zeros(n, dtype=np.int64)
    body = np.abs(c - o)
    up = _at(h, 0) - np.maximum(o, c)
    dn = np.minimum(o, c) - _at(low, 0)
    body1, up1, dn1 = _at(body, -1), _at(up, -1), _at(dn, -1)
    white1 = _at(c, -1) >= _at(o, -1)
    white = c >= o
    long0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    vs0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    long1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    vs1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    gap = ((~white1) & (low > _at(h, -1))) | (white1 & (h < _at(low, -1)))
    cond = (white1 != white) & gap
    maru = (body1 > long1) & (up1 < vs1) & (dn1 < vs1) & (body > long0) & (up < vs0) & (dn < vs0)
    longer = body > body1
    bull = np.where(longer, white, white1)
    hit = np.asarray(cond & maru, dtype=bool)
    res[hit & bull] = 100
    res[hit & ~bull] = -100
    return _fill_lookback(res, 11)


def _cdl2crows(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                    + (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
                ).astype(np.int32)
                + (
                    (
                        ((_at(o, -1) > _at(o, -2)) & (_at(o, -1) > _at(c, -2)))
                        & (_at(c, -1) > _at(o, -2))
                    )
                    & (_at(c, -1) > _at(c, -2))
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        np.abs(
                                            (
                                                (_at(c, -2)).astype(np.float64)
                                                - (_at(o, -2)).astype(np.float64)
                                            )
                                        )
                                        > _avg_rb10_1_m2
                                    )
                                    & (~(_at(c, 0) >= _at(o, 0)))
                                )
                                & (_at(o, 0) < _at(o, -1))
                            )
                            & (_at(o, 0) > _at(c, -1))
                        )
                        & (_at(c, 0) > _at(o, -2))
                    )
                    & (_at(c, 0) < _at(c, -2))
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdlupsidegap2crows(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                    + (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
                ).astype(np.int32)
                + (
                    (
                        ((_at(o, -1) > _at(o, -2)) & (_at(o, -1) > _at(c, -2)))
                        & (_at(c, -1) > _at(o, -2))
                    )
                    & (_at(c, -1) > _at(c, -2))
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        np.abs(
                                            (
                                                (_at(c, -2)).astype(np.float64)
                                                - (_at(o, -2)).astype(np.float64)
                                            )
                                        )
                                        > _avg_rb10_1_m2
                                    )
                                    & (
                                        np.abs(
                                            (
                                                (_at(c, -1)).astype(np.float64)
                                                - (_at(o, -1)).astype(np.float64)
                                            )
                                        )
                                        <= _avg_rb10_1_m1
                                    )
                                )
                                & (~(_at(c, 0) >= _at(o, 0)))
                            )
                            & (_at(o, 0) > _at(o, -1))
                        )
                        & (_at(c, 0) < _at(c, -1))
                    )
                    & (_at(c, 0) > _at(c, -2))
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdl3blackcrows(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl10_0p1_m2 = _candle_avg("HighLow", 10, 0.1, -2, o, h, low, c)
    _avg_hl10_0p1_m1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (_at(c, -3) >= _at(o, -3)).astype(np.int32)
                    + (~(_at(c, -2) >= _at(o, -2))).astype(np.int32)
                ).astype(np.int32)
                + (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (~(_at(c, 0) >= _at(o, 0)))
                                                        & (_at(o, -1) < _at(o, -2))
                                                    )
                                                    & (_at(o, -1) > _at(c, -2))
                                                )
                                                & (_at(o, 0) < _at(o, -1))
                                            )
                                            & (_at(o, 0) > _at(c, -1))
                                        )
                                        & (_at(h, -3) > _at(c, -2))
                                    )
                                    & (_at(c, -2) > _at(c, -1))
                                )
                                & (_at(c, -1) > _at(c, 0))
                            )
                            & (
                                (
                                    (np.minimum(_at(c, -2), _at(o, -2))).astype(np.float64)
                                    - (_at(low, -2)).astype(np.float64)
                                )
                                < _avg_hl10_0p1_m2
                            )
                        )
                        & (
                            (
                                (np.minimum(_at(c, -1), _at(o, -1))).astype(np.float64)
                                - (_at(low, -1)).astype(np.float64)
                            )
                            < _avg_hl10_0p1_m1
                        )
                    )
                    & (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        < _avg_hl10_0p1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 13)


def _cdlidentical3crows(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl10_0p1_m2 = _candle_avg("HighLow", 10, 0.1, -2, o, h, low, c)
    _avg_hl10_0p1_m1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    _avg_hl5_0p05_m2 = _candle_avg("HighLow", 5, 0.05, -2, o, h, low, c)
    _avg_hl5_0p05_m1 = _candle_avg("HighLow", 5, 0.05, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -2) >= _at(o, -2))).astype(np.int32)
                    + (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
                ).astype(np.int32)
                + (~(_at(c, 0) >= _at(o, 0))).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (np.minimum(_at(c, -2), _at(o, -2))).astype(
                                                            np.float64
                                                        )
                                                        - (_at(low, -2)).astype(np.float64)
                                                    )
                                                    < _avg_hl10_0p1_m2
                                                )
                                                & (
                                                    (
                                                        (np.minimum(_at(c, -1), _at(o, -1))).astype(
                                                            np.float64
                                                        )
                                                        - (_at(low, -1)).astype(np.float64)
                                                    )
                                                    < _avg_hl10_0p1_m1
                                                )
                                            )
                                            & (
                                                (
                                                    (np.minimum(_at(c, 0), _at(o, 0))).astype(
                                                        np.float64
                                                    )
                                                    - (_at(low, 0)).astype(np.float64)
                                                )
                                                < _avg_hl10_0p1_0
                                            )
                                        )
                                        & (_at(c, -2) > _at(c, -1))
                                    )
                                    & (_at(c, -1) > _at(c, 0))
                                )
                                & (
                                    _at(o, -1)
                                    <= (
                                        (_at(c, -2)).astype(np.float64)
                                        + (_avg_hl5_0p05_m2).astype(np.float64)
                                    )
                                )
                            )
                            & (
                                _at(o, -1)
                                >= (
                                    (_at(c, -2)).astype(np.float64)
                                    - (_avg_hl5_0p05_m2).astype(np.float64)
                                )
                            )
                        )
                        & (
                            _at(o, 0)
                            <= (
                                (_at(c, -1)).astype(np.float64)
                                + (_avg_hl5_0p05_m1).astype(np.float64)
                            )
                        )
                    )
                    & (
                        _at(o, 0)
                        >= ((_at(c, -1)).astype(np.float64) - (_avg_hl5_0p05_m1).astype(np.float64))
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdl3linestrike(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl5_0p2_m3 = _candle_avg("HighLow", 5, 0.2, -3, o, h, low, c)
    _avg_hl5_0p2_m2 = _candle_avg("HighLow", 5, 0.2, -2, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (
                        (_at(c, -3) >= _at(o, -3)).astype(np.int32)
                        == (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                    ).astype(np.int32)
                    + (
                        (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                        == (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                    ).astype(np.int32)
                ).astype(np.int32)
                + (
                    (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                    != (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            ((np.where((_at(c, -1) >= _at(o, -1)), 1, (-1))).astype(np.int32) * np.int32(100))
            & (
                -(
                    (
                        (
                            (
                                (
                                    _at(o, -2)
                                    >= (
                                        (np.minimum(_at(o, -3), _at(c, -3))).astype(np.float64)
                                        - (_avg_hl5_0p2_m3).astype(np.float64)
                                    )
                                )
                                & (
                                    _at(o, -2)
                                    <= (
                                        (np.maximum(_at(o, -3), _at(c, -3))).astype(np.float64)
                                        + (_avg_hl5_0p2_m3).astype(np.float64)
                                    )
                                )
                            )
                            & (
                                _at(o, -1)
                                >= (
                                    (np.minimum(_at(o, -2), _at(c, -2))).astype(np.float64)
                                    - (_avg_hl5_0p2_m2).astype(np.float64)
                                )
                            )
                        )
                        & (
                            _at(o, -1)
                            <= (
                                (np.maximum(_at(o, -2), _at(c, -2))).astype(np.float64)
                                + (_avg_hl5_0p2_m2).astype(np.float64)
                            )
                        )
                    )
                    & (
                        (
                            (
                                (
                                    ((_at(c, -1) >= _at(o, -1)) & (_at(c, -1) > _at(c, -2)))
                                    & (_at(c, -2) > _at(c, -3))
                                )
                                & (_at(o, 0) > _at(c, -1))
                            )
                            & (_at(c, 0) < _at(o, -3))
                        )
                        | (
                            (
                                (
                                    ((~(_at(c, -1) >= _at(o, -1))) & (_at(c, -1) < _at(c, -2)))
                                    & (_at(c, -2) < _at(c, -3))
                                )
                                & (_at(o, 0) < _at(c, -1))
                            )
                            & (_at(c, 0) > _at(o, -3))
                        )
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 8)


def _cdldarkcloudcover(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                    + (~(_at(c, 0) >= _at(o, 0))).astype(np.int32)
                ).astype(np.int32)
                + (_at(o, 0) > _at(h, -1)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            np.abs(
                                ((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                            )
                            > _avg_rb10_1_m1
                        )
                        & (_at(c, 0) > _at(o, -1))
                    )
                    & (
                        _at(c, 0)
                        < (
                            (_at(c, -1)).astype(np.float64)
                            - (
                                (
                                    np.abs(
                                        (
                                            (_at(c, -1)).astype(np.float64)
                                            - (_at(o, -1)).astype(np.float64)
                                        )
                                    )
                                ).astype(np.float64)
                                * np.float64(0.5)
                            ).astype(np.float64)
                        )
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdleveningdojistar(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_hl10_0p1_m1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                    + (~(_at(c, 0) >= _at(o, 0))).astype(np.int32)
                ).astype(np.int32)
                + (
                    (
                        ((_at(o, -1) > _at(o, -2)) & (_at(o, -1) > _at(c, -2)))
                        & (_at(c, -1) > _at(o, -2))
                    )
                    & (_at(c, -1) > _at(c, -2))
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            (
                                _at(c, 0)
                                < (
                                    (_at(c, -2)).astype(np.float64)
                                    - (
                                        (
                                            np.abs(
                                                (
                                                    (_at(c, -2)).astype(np.float64)
                                                    - (_at(o, -2)).astype(np.float64)
                                                )
                                            )
                                        ).astype(np.float64)
                                        * np.float64(0.3)
                                    ).astype(np.float64)
                                )
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, -2)).astype(np.float64)
                                        - (_at(o, -2)).astype(np.float64)
                                    )
                                )
                                > _avg_rb10_1_m2
                            )
                        )
                        & (
                            np.abs(
                                ((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                            )
                            <= _avg_hl10_0p1_m1
                        )
                    )
                    & (
                        np.abs((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                        > _avg_rb10_1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdldojistar(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = ((-np.where((_at(c, -1) >= _at(o, -1)), 1, (-1))).astype(np.int32) * np.int32(100)) & (
        -(
            (
                (
                    np.abs(((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64)))
                    > _avg_rb10_1_m1
                )
                & (
                    np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                    <= _avg_hl10_0p1_0
                )
            )
            & (
                (
                    (_at(c, -1) >= _at(o, -1))
                    & (
                        (
                            ((_at(o, 0) > _at(o, -1)) & (_at(o, 0) > _at(c, -1)))
                            & (_at(c, 0) > _at(o, -1))
                        )
                        & (_at(c, 0) > _at(c, -1))
                    )
                )
                | (
                    (~(_at(c, -1) >= _at(o, -1)))
                    & (
                        (
                            ((_at(o, 0) < _at(o, -1)) & (_at(o, 0) < _at(c, -1)))
                            & (_at(c, 0) < _at(o, -1))
                        )
                        & (_at(c, 0) < _at(c, -1))
                    )
                )
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlhangingman(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_rb0_1_0 = _candle_avg("RealBody", 0, 1.0, 0, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    _avg_hl5_0p2_m1 = _candle_avg("HighLow", 5, 0.2, -1, o, h, low, c)
    res = (-100) & (
        -(
            (
                (
                    (
                        np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                        < _avg_rb10_1_0
                    )
                    & (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        > _avg_rb0_1_0
                    )
                )
                & (
                    (
                        (_at(h, 0)).astype(np.float64)
                        - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                    )
                    < _avg_hl10_0p1_0
                )
            )
            & (
                (
                    _at(c, 0)
                    >= ((_at(h, -1)).astype(np.float64) - (_avg_hl5_0p2_m1).astype(np.float64))
                )
                & (
                    _at(o, 0)
                    >= ((_at(h, -1)).astype(np.float64) - (_avg_hl5_0p2_m1).astype(np.float64))
                )
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlinneck(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_hl5_0p05_m1 = _candle_avg("HighLow", 5, 0.05, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
                    + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                ).astype(np.int32)
                + (_at(o, 0) < _at(low, -1)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            np.abs(
                                ((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                            )
                            > _avg_rb10_1_m1
                        )
                        & (
                            _at(c, 0)
                            <= (
                                (_at(c, -1)).astype(np.float64)
                                + (_avg_hl5_0p05_m1).astype(np.float64)
                            )
                        )
                    )
                    & (_at(c, 0) >= _at(c, -1))
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlonneck(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_hl5_0p05_m1 = _candle_avg("HighLow", 5, 0.05, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
                    + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                ).astype(np.int32)
                + (_at(o, 0) < _at(low, -1)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            np.abs(
                                ((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                            )
                            > _avg_rb10_1_m1
                        )
                        & (
                            _at(c, 0)
                            <= (
                                (_at(low, -1)).astype(np.float64)
                                + (_avg_hl5_0p05_m1).astype(np.float64)
                            )
                        )
                    )
                    & (
                        _at(c, 0)
                        >= (
                            (_at(low, -1)).astype(np.float64)
                            - (_avg_hl5_0p05_m1).astype(np.float64)
                        )
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlthrusting(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_hl5_0p05_m1 = _candle_avg("HighLow", 5, 0.05, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
                    + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                ).astype(np.int32)
                + (_at(o, 0) < _at(low, -1)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            np.abs(
                                ((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                            )
                            > _avg_rb10_1_m1
                        )
                        & (
                            _at(c, 0)
                            > (
                                (_at(c, -1)).astype(np.float64)
                                + (_avg_hl5_0p05_m1).astype(np.float64)
                            )
                        )
                    )
                    & (
                        _at(c, 0)
                        <= (
                            np.abs(
                                (_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64)
                            )
                            * 0.5
                            + _at(c, -1)
                        )
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlshootingstar(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_rb0_1_0 = _candle_avg("RealBody", 0, 1.0, 0, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (
                        (_at(o, 0) > _at(o, -1)).astype(np.int32)
                        + (_at(o, 0) > _at(c, -1)).astype(np.int32)
                    ).astype(np.int32)
                    + (_at(c, 0) > _at(o, -1)).astype(np.int32)
                ).astype(np.int32)
                + (_at(c, 0) > _at(c, -1)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(4)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            np.abs(
                                ((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                            )
                            < _avg_rb10_1_0
                        )
                        & (
                            (
                                (_at(h, 0)).astype(np.float64)
                                - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            )
                            > _avg_rb0_1_0
                        )
                    )
                    & (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        < _avg_hl10_0p1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlstalledpattern(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_hl10_0p1_m1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    _avg_hl5_0p2_m2 = _candle_avg("HighLow", 5, 0.2, -2, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_hl5_0p2_m1 = _candle_avg("HighLow", 5, 0.2, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                    + (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                ).astype(np.int32)
                + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        (
                                            ((_at(c, 0) > _at(c, -1)) & (_at(c, -1) > _at(c, -2)))
                                            & (
                                                np.abs(
                                                    (
                                                        (_at(c, -2)).astype(np.float64)
                                                        - (_at(o, -2)).astype(np.float64)
                                                    )
                                                )
                                                > _avg_rb10_1_m2
                                            )
                                        )
                                        & (
                                            np.abs(
                                                (
                                                    (_at(c, -1)).astype(np.float64)
                                                    - (_at(o, -1)).astype(np.float64)
                                                )
                                            )
                                            > _avg_rb10_1_m1
                                        )
                                    )
                                    & (
                                        (
                                            (_at(h, -1)).astype(np.float64)
                                            - (np.maximum(_at(c, -1), _at(o, -1))).astype(
                                                np.float64
                                            )
                                        )
                                        < _avg_hl10_0p1_m1
                                    )
                                )
                                & (_at(o, -1) > _at(o, -2))
                            )
                            & (
                                _at(o, -1)
                                <= (
                                    (_at(c, -2)).astype(np.float64)
                                    + (_avg_hl5_0p2_m2).astype(np.float64)
                                )
                            )
                        )
                        & (
                            np.abs(
                                ((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                            )
                            < _avg_rb10_1_0
                        )
                    )
                    & (
                        _at(o, 0)
                        >= (
                            (
                                (_at(c, -1)).astype(np.float64)
                                - (
                                    np.abs(
                                        (
                                            (_at(c, 0)).astype(np.float64)
                                            - (_at(o, 0)).astype(np.float64)
                                        )
                                    )
                                ).astype(np.float64)
                            ).astype(np.float64)
                            - (_avg_hl5_0p2_m1).astype(np.float64)
                        )
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdladvanceblock(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl5_0p2_m2 = _candle_avg("HighLow", 5, 0.2, -2, o, h, low, c)
    _avg_hl5_0p2_m1 = _candle_avg("HighLow", 5, 0.2, -1, o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_sh10_1_m2 = _candle_avg("Shadows", 10, 1.0, -2, o, h, low, c)
    _avg_hl5_0p6_m2 = _candle_avg("HighLow", 5, 0.6, -2, o, h, low, c)
    _avg_hl5_0p6_m1 = _candle_avg("HighLow", 5, 0.6, -1, o, h, low, c)
    _avg_sh10_1_0 = _candle_avg("Shadows", 10, 1.0, 0, o, h, low, c)
    _avg_sh10_1_m1 = _candle_avg("Shadows", 10, 1.0, -1, o, h, low, c)
    _avg_rb0_1_0 = _candle_avg("RealBody", 0, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                    + (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                ).astype(np.int32)
                + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        (
                                            ((_at(c, 0) > _at(c, -1)) & (_at(c, -1) > _at(c, -2)))
                                            & (_at(o, -1) > _at(o, -2))
                                        )
                                        & (
                                            _at(o, -1)
                                            <= (
                                                (_at(c, -2)).astype(np.float64)
                                                + (_avg_hl5_0p2_m2).astype(np.float64)
                                            )
                                        )
                                    )
                                    & (_at(o, 0) > _at(o, -1))
                                )
                                & (
                                    _at(o, 0)
                                    <= (
                                        (_at(c, -1)).astype(np.float64)
                                        + (_avg_hl5_0p2_m1).astype(np.float64)
                                    )
                                )
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, -2)).astype(np.float64)
                                        - (_at(o, -2)).astype(np.float64)
                                    )
                                )
                                > _avg_rb10_1_m2
                            )
                        )
                        & (
                            (
                                (_at(h, -2)).astype(np.float64)
                                - (np.maximum(_at(c, -2), _at(o, -2))).astype(np.float64)
                            )
                            < _avg_sh10_1_m2
                        )
                    )
                    & (
                        (
                            (
                                (
                                    (
                                        np.abs(
                                            (
                                                (_at(c, -1)).astype(np.float64)
                                                - (_at(o, -1)).astype(np.float64)
                                            )
                                        )
                                        < (
                                            (
                                                np.abs(
                                                    (
                                                        (_at(c, -2)).astype(np.float64)
                                                        - (_at(o, -2)).astype(np.float64)
                                                    )
                                                )
                                            ).astype(np.float64)
                                            - (_avg_hl5_0p6_m2).astype(np.float64)
                                        )
                                    )
                                    & (
                                        np.abs(
                                            (
                                                (_at(c, 0)).astype(np.float64)
                                                - (_at(o, 0)).astype(np.float64)
                                            )
                                        )
                                        < (
                                            (
                                                np.abs(
                                                    (
                                                        (_at(c, -1)).astype(np.float64)
                                                        - (_at(o, -1)).astype(np.float64)
                                                    )
                                                )
                                            ).astype(np.float64)
                                            + (_avg_hl5_0p2_m1).astype(np.float64)
                                        )
                                    )
                                )
                                | (
                                    np.abs(
                                        (
                                            (_at(c, 0)).astype(np.float64)
                                            - (_at(o, 0)).astype(np.float64)
                                        )
                                    )
                                    < (
                                        (
                                            np.abs(
                                                (
                                                    (_at(c, -1)).astype(np.float64)
                                                    - (_at(o, -1)).astype(np.float64)
                                                )
                                            )
                                        ).astype(np.float64)
                                        - (_avg_hl5_0p6_m1).astype(np.float64)
                                    )
                                )
                            )
                            | (
                                (
                                    (
                                        np.abs(
                                            (
                                                (_at(c, 0)).astype(np.float64)
                                                - (_at(o, 0)).astype(np.float64)
                                            )
                                        )
                                        < np.abs(
                                            (
                                                (_at(c, -1)).astype(np.float64)
                                                - (_at(o, -1)).astype(np.float64)
                                            )
                                        )
                                    )
                                    & (
                                        np.abs(
                                            (
                                                (_at(c, -1)).astype(np.float64)
                                                - (_at(o, -1)).astype(np.float64)
                                            )
                                        )
                                        < np.abs(
                                            (
                                                (_at(c, -2)).astype(np.float64)
                                                - (_at(o, -2)).astype(np.float64)
                                            )
                                        )
                                    )
                                )
                                & (
                                    (
                                        (
                                            (_at(h, 0)).astype(np.float64)
                                            - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                                        )
                                        > _avg_sh10_1_0
                                    )
                                    | (
                                        (
                                            (_at(h, -1)).astype(np.float64)
                                            - (np.maximum(_at(c, -1), _at(o, -1))).astype(
                                                np.float64
                                            )
                                        )
                                        > _avg_sh10_1_m1
                                    )
                                )
                            )
                        )
                        | (
                            (
                                np.abs(
                                    (
                                        (_at(c, 0)).astype(np.float64)
                                        - (_at(o, 0)).astype(np.float64)
                                    )
                                )
                                < np.abs(
                                    (
                                        (_at(c, -1)).astype(np.float64)
                                        - (_at(o, -1)).astype(np.float64)
                                    )
                                )
                            )
                            & (
                                (
                                    (_at(h, 0)).astype(np.float64)
                                    - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                                )
                                > _avg_rb0_1_0
                            )
                        )
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdlhighwave(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_rb0_2_0 = _candle_avg("RealBody", 0, 2.0, 0, o, h, low, c)
    res = ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100)) & (
        -(
            (
                (
                    np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                    < _avg_rb10_1_0
                )
                & (
                    (
                        (_at(h, 0)).astype(np.float64)
                        - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                    )
                    > _avg_rb0_2_0
                )
            )
            & (
                (
                    (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                    - (_at(low, 0)).astype(np.float64)
                )
                > _avg_rb0_2_0
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdlengulfing(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    res = (
        ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(80))
        ^ (
            (
                ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100))
                ^ ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(80))
            )
            & (-((_at(o, 0) != _at(c, -1)) & (_at(c, 0) != _at(o, -1))).astype(np.int32))
        )
    ) & (
        -(
            (
                ((_at(c, 0) >= _at(o, 0)) & (~(_at(c, -1) >= _at(o, -1))))
                & (
                    ((_at(c, 0) >= _at(o, -1)) & (_at(o, 0) < _at(c, -1)))
                    | ((_at(c, 0) > _at(o, -1)) & (_at(o, 0) <= _at(c, -1)))
                )
            )
            | (
                ((~(_at(c, 0) >= _at(o, 0))) & (_at(c, -1) >= _at(o, -1)))
                & (
                    ((_at(o, 0) >= _at(c, -1)) & (_at(c, 0) < _at(o, -1)))
                    | ((_at(o, 0) > _at(c, -1)) & (_at(c, 0) <= _at(o, -1)))
                )
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 2)


def _cdlabandonedbaby(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_hl10_0p1_m1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (
                        (_at(low, -1) > _at(h, -2)).astype(np.int32)
                        + (_at(h, 0) < _at(low, -1)).astype(np.int32)
                    ).astype(np.int32)
                    + (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                ).astype(np.int32)
                + (~(_at(c, 0) >= _at(o, 0))).astype(np.int32)
            ).astype(np.int32)
            == np.int32(4)
        ),
        (
            ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100))
            & (
                -(
                    (
                        (
                            (
                                np.abs(
                                    (
                                        (_at(c, -2)).astype(np.float64)
                                        - (_at(o, -2)).astype(np.float64)
                                    )
                                )
                                > _avg_rb10_1_m2
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, -1)).astype(np.float64)
                                        - (_at(o, -1)).astype(np.float64)
                                    )
                                )
                                <= _avg_hl10_0p1_m1
                            )
                        )
                        & (
                            np.abs(
                                ((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                            )
                            > _avg_rb10_1_0
                        )
                    )
                    & (
                        _at(c, 0)
                        < (
                            (_at(c, -2)).astype(np.float64)
                            - (
                                (
                                    np.abs(
                                        (
                                            (_at(c, -2)).astype(np.float64)
                                            - (_at(o, -2)).astype(np.float64)
                                        )
                                    )
                                ).astype(np.float64)
                                * np.float64(0.3)
                            ).astype(np.float64)
                        )
                    )
                ).astype(np.int32)
            )
        ),
        np.where(
            (
                (
                    (
                        (
                            (_at(h, -1) < _at(low, -2)).astype(np.int32)
                            + (_at(low, 0) > _at(h, -1)).astype(np.int32)
                        ).astype(np.int32)
                        + (~(_at(c, -2) >= _at(o, -2))).astype(np.int32)
                    ).astype(np.int32)
                    + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                ).astype(np.int32)
                == np.int32(4)
            ),
            (
                ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100))
                & (
                    -(
                        (
                            (
                                (
                                    np.abs(
                                        (
                                            (_at(c, -2)).astype(np.float64)
                                            - (_at(o, -2)).astype(np.float64)
                                        )
                                    )
                                    > _avg_rb10_1_m2
                                )
                                & (
                                    np.abs(
                                        (
                                            (_at(c, -1)).astype(np.float64)
                                            - (_at(o, -1)).astype(np.float64)
                                        )
                                    )
                                    <= _avg_hl10_0p1_m1
                                )
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, 0)).astype(np.float64)
                                        - (_at(o, 0)).astype(np.float64)
                                    )
                                )
                                > _avg_rb10_1_0
                            )
                        )
                        & (
                            _at(c, 0)
                            > (
                                np.abs(
                                    (_at(c, -2)).astype(np.float64)
                                    - (_at(o, -2)).astype(np.float64)
                                )
                                * 0.3
                                + _at(c, -2)
                            )
                        )
                    ).astype(np.int32)
                )
            ),
            0,
        ),
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdlclosingmarubozu(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100)) & (
        -(
            (
                np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                > _avg_rb10_1_0
            )
            & (
                (
                    (_at(c, 0) >= _at(o, 0))
                    & (
                        (
                            (_at(h, 0)).astype(np.float64)
                            - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                        )
                        < _avg_hl10_0p1_0
                    )
                )
                | (
                    (~(_at(c, 0) >= _at(o, 0)))
                    & (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        < _avg_hl10_0p1_0
                    )
                )
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdldoji(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = np.where(
        (
            np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
            <= _avg_hl10_0p1_0
        ),
        100,
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdlgapsidesidewhite(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl5_0p2_m1 = _candle_avg("HighLow", 5, 0.2, -1, o, h, low, c)
    _avg_hl5_0p05_m1 = _candle_avg("HighLow", 5, 0.05, -1, o, h, low, c)
    res = np.where(
        (
            (
                (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(2)
        ),
        (
            np.where(
                np.where(
                    (np.minimum(_at(o, -1), _at(c, -1)) > np.maximum(_at(o, -2), _at(c, -2))), 1, 0
                ),
                100,
                (-100),
            )
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            (
                                                                (_at(o, -1) > _at(o, -2))
                                                                & (_at(o, -1) > _at(c, -2))
                                                            )
                                                            & (_at(c, -1) > _at(o, -2))
                                                        )
                                                        & (_at(c, -1) > _at(c, -2))
                                                    )
                                                    & (_at(o, 0) > _at(o, -2))
                                                )
                                                & (_at(o, 0) > _at(c, -2))
                                            )
                                            & (_at(c, 0) > _at(o, -2))
                                        )
                                        & (_at(c, 0) > _at(c, -2))
                                    )
                                    | (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            (
                                                                (_at(o, -1) < _at(o, -2))
                                                                & (_at(o, -1) < _at(c, -2))
                                                            )
                                                            & (_at(c, -1) < _at(o, -2))
                                                        )
                                                        & (_at(c, -1) < _at(c, -2))
                                                    )
                                                    & (_at(o, 0) < _at(o, -2))
                                                )
                                                & (_at(o, 0) < _at(c, -2))
                                            )
                                            & (_at(c, 0) < _at(o, -2))
                                        )
                                        & (_at(c, 0) < _at(c, -2))
                                    )
                                )
                                & (
                                    np.abs(
                                        (
                                            (_at(c, 0)).astype(np.float64)
                                            - (_at(o, 0)).astype(np.float64)
                                        )
                                    )
                                    >= (
                                        (
                                            np.abs(
                                                (
                                                    (_at(c, -1)).astype(np.float64)
                                                    - (_at(o, -1)).astype(np.float64)
                                                )
                                            )
                                        ).astype(np.float64)
                                        - (_avg_hl5_0p2_m1).astype(np.float64)
                                    )
                                )
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, 0)).astype(np.float64)
                                        - (_at(o, 0)).astype(np.float64)
                                    )
                                )
                                <= (
                                    (
                                        np.abs(
                                            (
                                                (_at(c, -1)).astype(np.float64)
                                                - (_at(o, -1)).astype(np.float64)
                                            )
                                        )
                                    ).astype(np.float64)
                                    + (_avg_hl5_0p2_m1).astype(np.float64)
                                )
                            )
                        )
                        & (
                            _at(o, 0)
                            >= (
                                (_at(o, -1)).astype(np.float64)
                                - (_avg_hl5_0p05_m1).astype(np.float64)
                            )
                        )
                    )
                    & (
                        _at(o, 0)
                        <= ((_at(o, -1)).astype(np.float64) + (_avg_hl5_0p05_m1).astype(np.float64))
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 7)


def _cdllongleggeddoji(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    _avg_rb0_1_0 = _candle_avg("RealBody", 0, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
            <= _avg_hl10_0p1_0
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        > _avg_rb0_1_0
                    )
                    | (
                        (
                            (_at(h, 0)).astype(np.float64)
                            - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                        )
                        > _avg_rb0_1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdlrickshawman(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    _avg_rb0_1_0 = _candle_avg("RealBody", 0, 1.0, 0, o, h, low, c)
    _avg_hl5_0p2_0 = _candle_avg("HighLow", 5, 0.2, 0, o, h, low, c)
    res = np.where(
        (
            np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
            <= _avg_hl10_0p1_0
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (
                                (
                                    (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                                    - (_at(low, 0)).astype(np.float64)
                                )
                                > _avg_rb0_1_0
                            )
                            & (
                                (
                                    (_at(h, 0)).astype(np.float64)
                                    - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                                )
                                > _avg_rb0_1_0
                            )
                        )
                        & (
                            (
                                _at(o, 0)
                                <= (
                                    (
                                        (_at(low, 0)).astype(np.float64)
                                        + (
                                            (
                                                (_at(h, 0)).astype(np.float64)
                                                - (_at(low, 0)).astype(np.float64)
                                            ).astype(np.float64)
                                            / np.float64(2)
                                        ).astype(np.float64)
                                    ).astype(np.float64)
                                    + (_avg_hl5_0p2_0).astype(np.float64)
                                )
                            )
                            | (
                                _at(c, 0)
                                <= (
                                    (
                                        (_at(low, 0)).astype(np.float64)
                                        + (
                                            (
                                                (_at(h, 0)).astype(np.float64)
                                                - (_at(low, 0)).astype(np.float64)
                                            ).astype(np.float64)
                                            / np.float64(2)
                                        ).astype(np.float64)
                                    ).astype(np.float64)
                                    + (_avg_hl5_0p2_0).astype(np.float64)
                                )
                            )
                        )
                    )
                    & (
                        (
                            _at(o, 0)
                            >= (
                                (
                                    (_at(low, 0)).astype(np.float64)
                                    + (
                                        (
                                            (_at(h, 0)).astype(np.float64)
                                            - (_at(low, 0)).astype(np.float64)
                                        ).astype(np.float64)
                                        / np.float64(2)
                                    ).astype(np.float64)
                                ).astype(np.float64)
                                - (_avg_hl5_0p2_0).astype(np.float64)
                            )
                        )
                        | (
                            _at(c, 0)
                            >= (
                                (
                                    (_at(low, 0)).astype(np.float64)
                                    + (
                                        (
                                            (_at(h, 0)).astype(np.float64)
                                            - (_at(low, 0)).astype(np.float64)
                                        ).astype(np.float64)
                                        / np.float64(2)
                                    ).astype(np.float64)
                                ).astype(np.float64)
                                - (_avg_hl5_0p2_0).astype(np.float64)
                            )
                        )
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdlmarubozu(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100)) & (
        -(
            (
                (
                    np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                    > _avg_rb10_1_0
                )
                & (
                    (
                        (_at(h, 0)).astype(np.float64)
                        - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                    )
                    < _avg_hl10_0p1_0
                )
            )
            & (
                (
                    (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                    - (_at(low, 0)).astype(np.float64)
                )
                < _avg_hl10_0p1_0
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdl3inside(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (
                        ((_at(c, -1) < _at(c, -2)) | (_at(c, -1) < _at(o, -2)))
                        & ((_at(o, -1) < _at(c, -2)) | (_at(o, -1) < _at(o, -2)))
                    ).astype(np.int32)
                    + (
                        ((_at(c, -1) > _at(c, -2)) | (_at(c, -1) > _at(o, -2)))
                        & ((_at(o, -1) > _at(c, -2)) | (_at(o, -1) > _at(o, -2)))
                    ).astype(np.int32)
                ).astype(np.int32)
                + (
                    (
                        ((_at(c, -2) >= _at(o, -2)) & (~(_at(c, 0) >= _at(o, 0))))
                        & (_at(c, 0) < _at(o, -2))
                    )
                    | (
                        ((~(_at(c, -2) >= _at(o, -2))) & (_at(c, 0) >= _at(o, 0)))
                        & (_at(c, 0) > _at(o, -2))
                    )
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            ((-np.where((_at(c, -2) >= _at(o, -2)), 1, (-1))).astype(np.int32) * np.int32(100))
            & (
                -(
                    (
                        np.abs(((_at(c, -2)).astype(np.float64) - (_at(o, -2)).astype(np.float64)))
                        > _avg_rb10_1_m2
                    )
                    & (
                        np.abs((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                        <= _avg_rb10_1_m1
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdl3outside(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    res = ((np.where((_at(c, -1) >= _at(o, -1)), 1, (-1))).astype(np.int32) * np.int32(100)) & (
        -(
            (
                (
                    (
                        ((_at(c, -1) >= _at(o, -1)) & (~(_at(c, -2) >= _at(o, -2))))
                        & (_at(c, -1) > _at(o, -2))
                    )
                    & (_at(o, -1) < _at(c, -2))
                )
                & (_at(c, 0) > _at(c, -1))
            )
            | (
                (
                    (
                        ((~(_at(c, -1) >= _at(o, -1))) & (_at(c, -2) >= _at(o, -2)))
                        & (_at(o, -1) > _at(c, -2))
                    )
                    & (_at(c, -1) < _at(o, -2))
                )
                & (_at(c, 0) < _at(c, -1))
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 3)


def _cdl3starsinsouth(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_rb0_1_m2 = _candle_avg("RealBody", 0, 1.0, -2, o, h, low, c)
    _avg_hl10_0p1_m1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -2) >= _at(o, -2))).astype(np.int32)
                    + (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
                ).astype(np.int32)
                + (~(_at(c, 0) >= _at(o, 0))).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            (
                                                                (
                                                                    np.abs(
                                                                        (
                                                                            (_at(c, -2)).astype(
                                                                                np.float64
                                                                            )
                                                                            - (_at(o, -2)).astype(
                                                                                np.float64
                                                                            )
                                                                        )
                                                                    )
                                                                    > _avg_rb10_1_m2
                                                                )
                                                                & (
                                                                    (
                                                                        (
                                                                            np.minimum(
                                                                                _at(c, -2),
                                                                                _at(o, -2),
                                                                            )
                                                                        ).astype(np.float64)
                                                                        - (_at(low, -2)).astype(
                                                                            np.float64
                                                                        )
                                                                    )
                                                                    > _avg_rb0_1_m2
                                                                )
                                                            )
                                                            & (
                                                                np.abs(
                                                                    (
                                                                        (_at(c, -1)).astype(
                                                                            np.float64
                                                                        )
                                                                        - (_at(o, -1)).astype(
                                                                            np.float64
                                                                        )
                                                                    )
                                                                )
                                                                < np.abs(
                                                                    (
                                                                        (_at(c, -2)).astype(
                                                                            np.float64
                                                                        )
                                                                        - (_at(o, -2)).astype(
                                                                            np.float64
                                                                        )
                                                                    )
                                                                )
                                                            )
                                                        )
                                                        & (_at(o, -1) > _at(c, -2))
                                                    )
                                                    & (_at(o, -1) <= _at(h, -2))
                                                )
                                                & (_at(low, -1) < _at(c, -2))
                                            )
                                            & (_at(low, -1) >= _at(low, -2))
                                        )
                                        & (
                                            (
                                                (np.minimum(_at(c, -1), _at(o, -1))).astype(
                                                    np.float64
                                                )
                                                - (_at(low, -1)).astype(np.float64)
                                            )
                                            > _avg_hl10_0p1_m1
                                        )
                                    )
                                    & (
                                        np.abs(
                                            (
                                                (_at(c, 0)).astype(np.float64)
                                                - (_at(o, 0)).astype(np.float64)
                                            )
                                        )
                                        < _avg_rb10_1_0
                                    )
                                )
                                & (
                                    (
                                        (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                                        - (_at(low, 0)).astype(np.float64)
                                    )
                                    < _avg_hl10_0p1_0
                                )
                            )
                            & (
                                (
                                    (_at(h, 0)).astype(np.float64)
                                    - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                                )
                                < _avg_hl10_0p1_0
                            )
                        )
                        & (_at(low, 0) > _at(low, -1))
                    )
                    & (_at(h, 0) < _at(h, -1))
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdl3whitesoldiers(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl10_0p1_m2 = _candle_avg("HighLow", 10, 0.1, -2, o, h, low, c)
    _avg_hl10_0p1_m1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    _avg_hl5_0p2_m2 = _candle_avg("HighLow", 5, 0.2, -2, o, h, low, c)
    _avg_hl5_0p2_m1 = _candle_avg("HighLow", 5, 0.2, -1, o, h, low, c)
    _avg_hl5_0p6_m2 = _candle_avg("HighLow", 5, 0.6, -2, o, h, low, c)
    _avg_hl5_0p6_m1 = _candle_avg("HighLow", 5, 0.6, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                    + (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                ).astype(np.int32)
                + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            (
                                                                (
                                                                    (_at(h, -2)).astype(np.float64)
                                                                    - (
                                                                        np.maximum(
                                                                            _at(c, -2), _at(o, -2)
                                                                        )
                                                                    ).astype(np.float64)
                                                                )
                                                                < _avg_hl10_0p1_m2
                                                            )
                                                            & (
                                                                (
                                                                    (_at(h, -1)).astype(np.float64)
                                                                    - (
                                                                        np.maximum(
                                                                            _at(c, -1), _at(o, -1)
                                                                        )
                                                                    ).astype(np.float64)
                                                                )
                                                                < _avg_hl10_0p1_m1
                                                            )
                                                        )
                                                        & (
                                                            (
                                                                (_at(h, 0)).astype(np.float64)
                                                                - (
                                                                    np.maximum(_at(c, 0), _at(o, 0))
                                                                ).astype(np.float64)
                                                            )
                                                            < _avg_hl10_0p1_0
                                                        )
                                                    )
                                                    & (_at(c, 0) > _at(c, -1))
                                                )
                                                & (_at(c, -1) > _at(c, -2))
                                            )
                                            & (_at(o, -1) > _at(o, -2))
                                        )
                                        & (
                                            _at(o, -1)
                                            <= (
                                                (_at(c, -2)).astype(np.float64)
                                                + (_avg_hl5_0p2_m2).astype(np.float64)
                                            )
                                        )
                                    )
                                    & (_at(o, 0) > _at(o, -1))
                                )
                                & (
                                    _at(o, 0)
                                    <= (
                                        (_at(c, -1)).astype(np.float64)
                                        + (_avg_hl5_0p2_m1).astype(np.float64)
                                    )
                                )
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, -1)).astype(np.float64)
                                        - (_at(o, -1)).astype(np.float64)
                                    )
                                )
                                > (
                                    (
                                        np.abs(
                                            (
                                                (_at(c, -2)).astype(np.float64)
                                                - (_at(o, -2)).astype(np.float64)
                                            )
                                        )
                                    ).astype(np.float64)
                                    - (_avg_hl5_0p6_m2).astype(np.float64)
                                )
                            )
                        )
                        & (
                            np.abs(
                                ((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                            )
                            > (
                                (
                                    np.abs(
                                        (
                                            (_at(c, -1)).astype(np.float64)
                                            - (_at(o, -1)).astype(np.float64)
                                        )
                                    )
                                ).astype(np.float64)
                                - (_avg_hl5_0p6_m1).astype(np.float64)
                            )
                        )
                    )
                    & (
                        np.abs((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                        > _avg_rb10_1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdlbelthold(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100)) & (
        -(
            (
                np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                > _avg_rb10_1_0
            )
            & (
                (
                    (_at(c, 0) >= _at(o, 0))
                    & (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        < _avg_hl10_0p1_0
                    )
                )
                | (
                    (~(_at(c, 0) >= _at(o, 0)))
                    & (
                        (
                            (_at(h, 0)).astype(np.float64)
                            - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                        )
                        < _avg_hl10_0p1_0
                    )
                )
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdlbreakaway(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m4 = _candle_avg("RealBody", 10, 1.0, -4, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (
                        (_at(c, -4) >= _at(o, -4)).astype(np.int32)
                        == (_at(c, -3) >= _at(o, -3)).astype(np.int32)
                    ).astype(np.int32)
                    + (
                        (_at(c, -3) >= _at(o, -3)).astype(np.int32)
                        == (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                    ).astype(np.int32)
                ).astype(np.int32)
                + (
                    (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                    != (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100))
            & (
                -(
                    (
                        np.abs(((_at(c, -4)).astype(np.float64) - (_at(o, -4)).astype(np.float64)))
                        > _avg_rb10_1_m4
                    )
                    & (
                        (
                            (
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (~(_at(c, -4) >= _at(o, -4)))
                                                    & (
                                                        (
                                                            (
                                                                (_at(o, -3) < _at(o, -4))
                                                                & (_at(o, -3) < _at(c, -4))
                                                            )
                                                            & (_at(c, -3) < _at(o, -4))
                                                        )
                                                        & (_at(c, -3) < _at(c, -4))
                                                    )
                                                )
                                                & (_at(h, -2) < _at(h, -3))
                                            )
                                            & (_at(low, -2) < _at(low, -3))
                                        )
                                        & (_at(h, -1) < _at(h, -2))
                                    )
                                    & (_at(low, -1) < _at(low, -2))
                                )
                                & (_at(c, 0) > _at(o, -3))
                            )
                            & (_at(c, 0) < _at(c, -4))
                        )
                        | (
                            (
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (_at(c, -4) >= _at(o, -4))
                                                    & (
                                                        (
                                                            (
                                                                (_at(o, -3) > _at(o, -4))
                                                                & (_at(o, -3) > _at(c, -4))
                                                            )
                                                            & (_at(c, -3) > _at(o, -4))
                                                        )
                                                        & (_at(c, -3) > _at(c, -4))
                                                    )
                                                )
                                                & (_at(h, -2) > _at(h, -3))
                                            )
                                            & (_at(low, -2) > _at(low, -3))
                                        )
                                        & (_at(h, -1) > _at(h, -2))
                                    )
                                    & (_at(low, -1) > _at(low, -2))
                                )
                                & (_at(c, 0) < _at(o, -3))
                            )
                            & (_at(c, 0) > _at(c, -4))
                        )
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 14)


def _cdlconcealbabyswall(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl10_0p1_m3 = _candle_avg("HighLow", 10, 0.1, -3, o, h, low, c)
    _avg_hl10_0p1_m2 = _candle_avg("HighLow", 10, 0.1, -2, o, h, low, c)
    _avg_hl10_0p1_m1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -3) >= _at(o, -3))).astype(np.int32)
                    + (~(_at(c, -2) >= _at(o, -2))).astype(np.int32)
                ).astype(np.int32)
                + (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (~(_at(c, 0) >= _at(o, 0)))
                                                    & (
                                                        (
                                                            (
                                                                np.minimum(_at(c, -3), _at(o, -3))
                                                            ).astype(np.float64)
                                                            - (_at(low, -3)).astype(np.float64)
                                                        )
                                                        < _avg_hl10_0p1_m3
                                                    )
                                                )
                                                & (
                                                    (
                                                        (_at(h, -3)).astype(np.float64)
                                                        - (
                                                            np.maximum(_at(c, -3), _at(o, -3))
                                                        ).astype(np.float64)
                                                    )
                                                    < _avg_hl10_0p1_m3
                                                )
                                            )
                                            & (
                                                (
                                                    (np.minimum(_at(c, -2), _at(o, -2))).astype(
                                                        np.float64
                                                    )
                                                    - (_at(low, -2)).astype(np.float64)
                                                )
                                                < _avg_hl10_0p1_m2
                                            )
                                        )
                                        & (
                                            (
                                                (_at(h, -2)).astype(np.float64)
                                                - (np.maximum(_at(c, -2), _at(o, -2))).astype(
                                                    np.float64
                                                )
                                            )
                                            < _avg_hl10_0p1_m2
                                        )
                                    )
                                    & (
                                        (
                                            ((_at(o, -1) < _at(o, -2)) & (_at(o, -1) < _at(c, -2)))
                                            & (_at(c, -1) < _at(o, -2))
                                        )
                                        & (_at(c, -1) < _at(c, -2))
                                    )
                                )
                                & (
                                    (
                                        (_at(h, -1)).astype(np.float64)
                                        - (np.maximum(_at(c, -1), _at(o, -1))).astype(np.float64)
                                    )
                                    > _avg_hl10_0p1_m1
                                )
                            )
                            & (_at(h, -1) > _at(c, -2))
                        )
                        & (_at(h, 0) > _at(h, -1))
                    )
                    & (_at(low, 0) < _at(low, -1))
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 13)


def _cdlcounterattack(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_hl5_0p05_m1 = _candle_avg("HighLow", 5, 0.05, -1, o, h, low, c)
    res = ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100)) & (
        -(
            (
                (
                    (
                        (
                            (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                            != (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                        )
                        & (
                            np.abs(
                                ((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                            )
                            > _avg_rb10_1_m1
                        )
                    )
                    & (
                        np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                        > _avg_rb10_1_0
                    )
                )
                & (
                    _at(c, 0)
                    <= ((_at(c, -1)).astype(np.float64) + (_avg_hl5_0p05_m1).astype(np.float64))
                )
            )
            & (
                _at(c, 0)
                >= ((_at(c, -1)).astype(np.float64) - (_avg_hl5_0p05_m1).astype(np.float64))
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdldragonflydoji(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = np.where(
        (
            np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
            <= _avg_hl10_0p1_0
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (_at(h, 0)).astype(np.float64)
                            - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                        )
                        < _avg_hl10_0p1_0
                    )
                    & (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        > _avg_hl10_0p1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdleveningstar(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                    + (~(_at(c, 0) >= _at(o, 0))).astype(np.int32)
                ).astype(np.int32)
                + (
                    (
                        ((_at(o, -1) > _at(o, -2)) & (_at(o, -1) > _at(c, -2)))
                        & (_at(c, -1) > _at(o, -2))
                    )
                    & (_at(c, -1) > _at(c, -2))
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (-100)
            & (
                -(
                    (
                        (
                            (
                                _at(c, 0)
                                < (
                                    (_at(c, -2)).astype(np.float64)
                                    - (
                                        (
                                            np.abs(
                                                (
                                                    (_at(c, -2)).astype(np.float64)
                                                    - (_at(o, -2)).astype(np.float64)
                                                )
                                            )
                                        ).astype(np.float64)
                                        * np.float64(0.3)
                                    ).astype(np.float64)
                                )
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, -2)).astype(np.float64)
                                        - (_at(o, -2)).astype(np.float64)
                                    )
                                )
                                > _avg_rb10_1_m2
                            )
                        )
                        & (
                            np.abs(
                                ((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                            )
                            <= _avg_rb10_1_m1
                        )
                    )
                    & (
                        np.abs((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                        > _avg_rb10_1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdlgravestonedoji(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = np.where(
        (
            np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
            <= _avg_hl10_0p1_0
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        < _avg_hl10_0p1_0
                    )
                    & (
                        (
                            (_at(h, 0)).astype(np.float64)
                            - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                        )
                        > _avg_hl10_0p1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdlhammer(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_rb0_1_0 = _candle_avg("RealBody", 0, 1.0, 0, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    _avg_hl5_0p2_m1 = _candle_avg("HighLow", 5, 0.2, -1, o, h, low, c)
    res = 100 & (
        -(
            (
                (
                    (
                        np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                        < _avg_rb10_1_0
                    )
                    & (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        > _avg_rb0_1_0
                    )
                )
                & (
                    (
                        (_at(h, 0)).astype(np.float64)
                        - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                    )
                    < _avg_hl10_0p1_0
                )
            )
            & (
                (
                    _at(c, 0)
                    <= ((_at(low, -1)).astype(np.float64) + (_avg_hl5_0p2_m1).astype(np.float64))
                )
                | (
                    _at(o, 0)
                    <= ((_at(low, -1)).astype(np.float64) + (_avg_hl5_0p2_m1).astype(np.float64))
                )
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlharami(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    np.abs(((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64)))
                    > _avg_rb10_1_m1
                ).astype(np.int32)
                + (
                    np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                    <= _avg_rb10_1_0
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(2)
        ),
        (
            ((-np.where((_at(c, -1) >= _at(o, -1)), 1, (-1))).astype(np.int32) * np.int32(100))
            ^ (
                (
                    (
                        (
                            (-np.where((_at(c, -1) >= _at(o, -1)), 1, (-1))).astype(np.int32)
                            * np.int32(80)
                        )
                        & (
                            -(
                                (
                                    (
                                        ((_at(c, 0) <= _at(c, -1)) | (_at(c, 0) <= _at(o, -1)))
                                        & ((_at(o, 0) <= _at(c, -1)) | (_at(o, 0) <= _at(o, -1)))
                                    )
                                    & ((_at(c, 0) >= _at(c, -1)) | (_at(c, 0) >= _at(o, -1)))
                                )
                                & ((_at(o, 0) >= _at(c, -1)) | (_at(o, 0) >= _at(o, -1)))
                            ).astype(np.int32)
                        )
                    )
                    ^ (
                        (-np.where((_at(c, -1) >= _at(o, -1)), 1, (-1))).astype(np.int32)
                        * np.int32(100)
                    )
                )
                & (
                    (
                        (
                            (
                                (
                                    ((_at(c, 0) < _at(c, -1)) | (_at(c, 0) < _at(o, -1)))
                                    & ((_at(o, 0) < _at(c, -1)) | (_at(o, 0) < _at(o, -1)))
                                )
                                & ((_at(c, 0) > _at(c, -1)) | (_at(c, 0) > _at(o, -1)))
                            )
                            & ((_at(o, 0) > _at(c, -1)) | (_at(o, 0) > _at(o, -1)))
                        ).astype(np.int32)
                    ).astype(np.int32)
                    - np.int32(1)
                )
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlharamicross(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    np.abs(((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64)))
                    > _avg_rb10_1_m1
                ).astype(np.int32)
                + (
                    np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                    <= _avg_hl10_0p1_0
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(2)
        ),
        (
            ((-np.where((_at(c, -1) >= _at(o, -1)), 1, (-1))).astype(np.int32) * np.int32(100))
            ^ (
                (
                    (
                        (
                            (-np.where((_at(c, -1) >= _at(o, -1)), 1, (-1))).astype(np.int32)
                            * np.int32(80)
                        )
                        & (
                            -(
                                (
                                    ((_at(c, 0) <= _at(c, -1)) | (_at(c, 0) <= _at(o, -1)))
                                    & ((_at(o, 0) <= _at(c, -1)) | (_at(o, 0) <= _at(o, -1)))
                                )
                                & (
                                    ((_at(c, 0) >= _at(c, -1)) | (_at(c, 0) >= _at(o, -1)))
                                    & ((_at(o, 0) >= _at(c, -1)) | (_at(o, 0) >= _at(o, -1)))
                                )
                            ).astype(np.int32)
                        )
                    )
                    ^ (
                        (-np.where((_at(c, -1) >= _at(o, -1)), 1, (-1))).astype(np.int32)
                        * np.int32(100)
                    )
                )
                & (
                    (
                        (
                            (
                                (
                                    ((_at(c, 0) < _at(c, -1)) | (_at(c, 0) < _at(o, -1)))
                                    & ((_at(o, 0) < _at(c, -1)) | (_at(o, 0) < _at(o, -1)))
                                )
                                & ((_at(c, 0) > _at(c, -1)) | (_at(c, 0) > _at(o, -1)))
                            )
                            & ((_at(o, 0) > _at(c, -1)) | (_at(o, 0) > _at(o, -1)))
                        ).astype(np.int32)
                    ).astype(np.int32)
                    - np.int32(1)
                )
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlhomingpigeon(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
                    + (~(_at(c, 0) >= _at(o, 0))).astype(np.int32)
                ).astype(np.int32)
                + (_at(c, 0) > _at(c, -1)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            100
            & (
                -(
                    (
                        (
                            np.abs(
                                ((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                            )
                            > _avg_rb10_1_m1
                        )
                        & (
                            np.abs(
                                ((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                            )
                            <= _avg_rb10_1_0
                        )
                    )
                    & (_at(o, 0) < _at(o, -1))
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlinvertedhammer(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_rb0_1_0 = _candle_avg("RealBody", 0, 1.0, 0, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (
                        (_at(o, 0) < _at(o, -1)).astype(np.int32)
                        + (_at(o, 0) < _at(c, -1)).astype(np.int32)
                    ).astype(np.int32)
                    + (_at(c, 0) < _at(o, -1)).astype(np.int32)
                ).astype(np.int32)
                + (_at(c, 0) < _at(c, -1)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(4)
        ),
        (
            100
            & (
                -(
                    (
                        (
                            np.abs(
                                ((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                            )
                            < _avg_rb10_1_0
                        )
                        & (
                            (
                                (_at(h, 0)).astype(np.float64)
                                - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            )
                            > _avg_rb0_1_0
                        )
                    )
                    & (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        < _avg_hl10_0p1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlkicking(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_hl10_0p1_m1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                    != (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                ).astype(np.int32)
                + (
                    ((~(_at(c, -1) >= _at(o, -1))) & (_at(low, 0) > _at(h, -1)))
                    | ((_at(c, -1) >= _at(o, -1)) & (_at(h, 0) < _at(low, -1)))
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(2)
        ),
        (
            ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100))
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        np.abs(
                                            (
                                                (_at(c, -1)).astype(np.float64)
                                                - (_at(o, -1)).astype(np.float64)
                                            )
                                        )
                                        > _avg_rb10_1_m1
                                    )
                                    & (
                                        (
                                            (_at(h, -1)).astype(np.float64)
                                            - (np.maximum(_at(c, -1), _at(o, -1))).astype(
                                                np.float64
                                            )
                                        )
                                        < _avg_hl10_0p1_m1
                                    )
                                )
                                & (
                                    (
                                        (np.minimum(_at(c, -1), _at(o, -1))).astype(np.float64)
                                        - (_at(low, -1)).astype(np.float64)
                                    )
                                    < _avg_hl10_0p1_m1
                                )
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, 0)).astype(np.float64)
                                        - (_at(o, 0)).astype(np.float64)
                                    )
                                )
                                > _avg_rb10_1_0
                            )
                        )
                        & (
                            (
                                (_at(h, 0)).astype(np.float64)
                                - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            )
                            < _avg_hl10_0p1_0
                        )
                    )
                    & (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        < _avg_hl10_0p1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlladderbottom(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl10_0p1_m1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -4) >= _at(o, -4))).astype(np.int32)
                    + (~(_at(c, -3) >= _at(o, -3))).astype(np.int32)
                ).astype(np.int32)
                + (~(_at(c, -2) >= _at(o, -2))).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        (
                                            ((_at(o, -4) > _at(o, -3)) & (_at(o, -3) > _at(o, -2)))
                                            & (_at(c, -4) > _at(c, -3))
                                        )
                                        & (_at(c, -3) > _at(c, -2))
                                    )
                                    & (~(_at(c, -1) >= _at(o, -1)))
                                )
                                & (
                                    (
                                        (_at(h, -1)).astype(np.float64)
                                        - (np.maximum(_at(c, -1), _at(o, -1))).astype(np.float64)
                                    )
                                    > _avg_hl10_0p1_m1
                                )
                            )
                            & (_at(c, 0) >= _at(o, 0))
                        )
                        & (_at(o, 0) > _at(o, -1))
                    )
                    & (_at(c, 0) > _at(h, -1))
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 14)


def _cdllongline(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_sh10_1_0 = _candle_avg("Shadows", 10, 1.0, 0, o, h, low, c)
    res = ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100)) & (
        -(
            (
                (
                    np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                    > _avg_rb10_1_0
                )
                & (
                    (
                        (_at(h, 0)).astype(np.float64)
                        - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                    )
                    < _avg_sh10_1_0
                )
            )
            & (
                (
                    (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                    - (_at(low, 0)).astype(np.float64)
                )
                < _avg_sh10_1_0
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdlmatchinglow(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl5_0p05_m1 = _candle_avg("HighLow", 5, 0.05, -1, o, h, low, c)
    res = 100 & (
        -(
            (
                ((~(_at(c, -1) >= _at(o, -1))) & (~(_at(c, 0) >= _at(o, 0))))
                & (
                    _at(c, 0)
                    <= ((_at(c, -1)).astype(np.float64) + (_avg_hl5_0p05_m1).astype(np.float64))
                )
            )
            & (
                _at(c, 0)
                >= ((_at(c, -1)).astype(np.float64) - (_avg_hl5_0p05_m1).astype(np.float64))
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 6)


def _cdlmathold(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m4 = _candle_avg("RealBody", 10, 1.0, -4, o, h, low, c)
    _avg_rb10_1_m3 = _candle_avg("RealBody", 10, 1.0, -3, o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (_at(c, -4) >= _at(o, -4)).astype(np.int32)
                    + (~(_at(c, -3) >= _at(o, -3))).astype(np.int32)
                ).astype(np.int32)
                + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            (
                                                                (
                                                                    (
                                                                        (
                                                                            (
                                                                                (
                                                                                    (
                                                                                        _at(o, -3)
                                                                                        > _at(o, -4)
                                                                                    )
                                                                                    & (
                                                                                        _at(o, -3)
                                                                                        > _at(c, -4)
                                                                                    )
                                                                                )
                                                                                & (
                                                                                    _at(c, -3)
                                                                                    > _at(o, -4)
                                                                                )
                                                                            )
                                                                            & (
                                                                                _at(c, -3)
                                                                                > _at(c, -4)
                                                                            )
                                                                        )
                                                                        & (
                                                                            (
                                                                                _at(o, -2)
                                                                                < _at(c, -4)
                                                                            )
                                                                            | (
                                                                                _at(c, -2)
                                                                                < _at(c, -4)
                                                                            )
                                                                        )
                                                                    )
                                                                    & (
                                                                        (_at(o, -1) < _at(c, -4))
                                                                        | (_at(c, -1) < _at(c, -4))
                                                                    )
                                                                )
                                                                & (
                                                                    (
                                                                        _at(o, -2)
                                                                        > (
                                                                            (_at(c, -4)).astype(
                                                                                np.float64
                                                                            )
                                                                            - (
                                                                                (
                                                                                    np.abs(
                                                                                        (
                                                                                            (
                                                                                                _at(
                                                                                                    c,
                                                                                                    -4,
                                                                                                )
                                                                                            ).astype(
                                                                                                np.float64
                                                                                            )
                                                                                            - (
                                                                                                _at(
                                                                                                    o,
                                                                                                    -4,
                                                                                                )
                                                                                            ).astype(
                                                                                                np.float64
                                                                                            )
                                                                                        )
                                                                                    )
                                                                                ).astype(np.float64)
                                                                                * np.float64(0.5)
                                                                            ).astype(np.float64)
                                                                        )
                                                                    )
                                                                    & (
                                                                        _at(c, -2)
                                                                        > (
                                                                            (_at(c, -4)).astype(
                                                                                np.float64
                                                                            )
                                                                            - (
                                                                                (
                                                                                    np.abs(
                                                                                        (
                                                                                            (
                                                                                                _at(
                                                                                                    c,
                                                                                                    -4,
                                                                                                )
                                                                                            ).astype(
                                                                                                np.float64
                                                                                            )
                                                                                            - (
                                                                                                _at(
                                                                                                    o,
                                                                                                    -4,
                                                                                                )
                                                                                            ).astype(
                                                                                                np.float64
                                                                                            )
                                                                                        )
                                                                                    )
                                                                                ).astype(np.float64)
                                                                                * np.float64(0.5)
                                                                            ).astype(np.float64)
                                                                        )
                                                                    )
                                                                )
                                                            )
                                                            & (
                                                                _at(o, -1)
                                                                > (
                                                                    (_at(c, -4)).astype(np.float64)
                                                                    - (
                                                                        (
                                                                            np.abs(
                                                                                (
                                                                                    (
                                                                                        _at(c, -4)
                                                                                    ).astype(
                                                                                        np.float64
                                                                                    )
                                                                                    - (
                                                                                        _at(o, -4)
                                                                                    ).astype(
                                                                                        np.float64
                                                                                    )
                                                                                )
                                                                            )
                                                                        ).astype(np.float64)
                                                                        * np.float64(0.5)
                                                                    ).astype(np.float64)
                                                                )
                                                            )
                                                        )
                                                        & (
                                                            _at(c, -1)
                                                            > (
                                                                (_at(c, -4)).astype(np.float64)
                                                                - (
                                                                    (
                                                                        np.abs(
                                                                            (
                                                                                (_at(c, -4)).astype(
                                                                                    np.float64
                                                                                )
                                                                                - (
                                                                                    _at(o, -4)
                                                                                ).astype(np.float64)
                                                                            )
                                                                        )
                                                                    ).astype(np.float64)
                                                                    * np.float64(0.5)
                                                                ).astype(np.float64)
                                                            )
                                                        )
                                                    )
                                                    & (
                                                        (_at(c, -2) < _at(o, -3))
                                                        & (_at(o, -2) < _at(o, -3))
                                                    )
                                                )
                                                & (
                                                    (_at(c, -1) < _at(c, -2))
                                                    | (_at(c, -1) < _at(o, -2))
                                                )
                                            )
                                            & (
                                                (_at(o, -1) < _at(c, -2))
                                                | (_at(o, -1) < _at(o, -2))
                                            )
                                        )
                                        & (_at(o, 0) > _at(c, -1))
                                    )
                                    & (
                                        ((_at(c, 0) > _at(h, -3)) & (_at(c, 0) > _at(h, -2)))
                                        & (_at(c, 0) > _at(h, -1))
                                    )
                                )
                                & (
                                    np.abs(
                                        (
                                            (_at(c, -4)).astype(np.float64)
                                            - (_at(o, -4)).astype(np.float64)
                                        )
                                    )
                                    > _avg_rb10_1_m4
                                )
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, -3)).astype(np.float64)
                                        - (_at(o, -3)).astype(np.float64)
                                    )
                                )
                                < _avg_rb10_1_m3
                            )
                        )
                        & (
                            np.abs(
                                ((_at(c, -2)).astype(np.float64) - (_at(o, -2)).astype(np.float64))
                            )
                            < _avg_rb10_1_m2
                        )
                    )
                    & (
                        np.abs((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                        < _avg_rb10_1_m1
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 14)


def _cdlmorningdojistar(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_hl10_0p1_m1 = _candle_avg("HighLow", 10, 0.1, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -2) >= _at(o, -2))).astype(np.int32)
                    + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                ).astype(np.int32)
                + (
                    (
                        ((_at(o, -1) < _at(o, -2)) & (_at(o, -1) < _at(c, -2)))
                        & (_at(c, -1) < _at(o, -2))
                    )
                    & (_at(c, -1) < _at(c, -2))
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (
                                _at(c, 0)
                                > (
                                    np.abs(
                                        (
                                            (_at(c, -2)).astype(np.float64)
                                            - (_at(o, -2)).astype(np.float64)
                                        )
                                    )
                                    * 0.3
                                    + _at(c, -2)
                                )
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, -2)).astype(np.float64)
                                        - (_at(o, -2)).astype(np.float64)
                                    )
                                )
                                > _avg_rb10_1_m2
                            )
                        )
                        & (
                            np.abs(
                                ((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                            )
                            <= _avg_hl10_0p1_m1
                        )
                    )
                    & (
                        np.abs((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                        > _avg_rb10_1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdlmorningstar(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -2) >= _at(o, -2))).astype(np.int32)
                    + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                ).astype(np.int32)
                + (
                    (
                        ((_at(o, -1) < _at(o, -2)) & (_at(o, -1) < _at(c, -2)))
                        & (_at(c, -1) < _at(o, -2))
                    )
                    & (_at(c, -1) < _at(c, -2))
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (
                                _at(c, 0)
                                > (
                                    np.abs(
                                        (
                                            (_at(c, -2)).astype(np.float64)
                                            - (_at(o, -2)).astype(np.float64)
                                        )
                                    )
                                    * 0.3
                                    + _at(c, -2)
                                )
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, -2)).astype(np.float64)
                                        - (_at(o, -2)).astype(np.float64)
                                    )
                                )
                                > _avg_rb10_1_m2
                            )
                        )
                        & (
                            np.abs(
                                ((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                            )
                            <= _avg_rb10_1_m1
                        )
                    )
                    & (
                        np.abs((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                        > _avg_rb10_1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdlpiercing(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
                    + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                ).astype(np.int32)
                + (_at(o, 0) < _at(low, -1)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (
                                np.abs(
                                    (
                                        (_at(c, -1)).astype(np.float64)
                                        - (_at(o, -1)).astype(np.float64)
                                    )
                                )
                                > _avg_rb10_1_m1
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, 0)).astype(np.float64)
                                        - (_at(o, 0)).astype(np.float64)
                                    )
                                )
                                > _avg_rb10_1_0
                            )
                        )
                        & (_at(c, 0) < _at(o, -1))
                    )
                    & (
                        _at(c, 0)
                        > (
                            np.abs(
                                (_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64)
                            )
                            * 0.5
                            + _at(c, -1)
                        )
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlrisefall3methods(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m4 = _candle_avg("RealBody", 10, 1.0, -4, o, h, low, c)
    _avg_rb10_1_m3 = _candle_avg("RealBody", 10, 1.0, -3, o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_rb10_1_m1 = _candle_avg("RealBody", 10, 1.0, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (
                        (_at(c, -4) >= _at(o, -4)).astype(np.int32)
                        != (_at(c, -3) >= _at(o, -3)).astype(np.int32)
                    ).astype(np.int32)
                    + (
                        (_at(c, -3) >= _at(o, -3)).astype(np.int32)
                        == (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                    ).astype(np.int32)
                ).astype(np.int32)
                + (
                    (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                    == (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            (np.int32(100) * (np.where((_at(c, -4) >= _at(o, -4)), 1, (-1))).astype(np.int32))
            & (
                -(
                    (
                        (
                            (
                                (
                                    (
                                        (
                                            (
                                                (
                                                    (
                                                        (
                                                            (
                                                                (
                                                                    (
                                                                        (
                                                                            (
                                                                                (
                                                                                    _at(c, -1)
                                                                                    >= _at(o, -1)
                                                                                ).astype(np.int32)
                                                                                != (
                                                                                    _at(c, 0)
                                                                                    >= _at(o, 0)
                                                                                ).astype(np.int32)
                                                                            )
                                                                            & (
                                                                                (
                                                                                    _at(o, -3)
                                                                                    < _at(h, -4)
                                                                                )
                                                                                | (
                                                                                    _at(c, -3)
                                                                                    < _at(h, -4)
                                                                                )
                                                                            )
                                                                        )
                                                                        & (
                                                                            (
                                                                                _at(o, -3)
                                                                                > _at(low, -4)
                                                                            )
                                                                            | (
                                                                                _at(c, -3)
                                                                                > _at(low, -4)
                                                                            )
                                                                        )
                                                                    )
                                                                    & (
                                                                        (_at(o, -2) < _at(h, -4))
                                                                        | (_at(c, -2) < _at(h, -4))
                                                                    )
                                                                )
                                                                & (
                                                                    (_at(o, -2) > _at(low, -4))
                                                                    | (_at(c, -2) > _at(low, -4))
                                                                )
                                                            )
                                                            & (
                                                                (_at(o, -1) < _at(h, -4))
                                                                | (_at(c, -1) < _at(h, -4))
                                                            )
                                                        )
                                                        & (
                                                            (_at(o, -1) > _at(low, -4))
                                                            | (_at(c, -1) > _at(low, -4))
                                                        )
                                                    )
                                                    & (
                                                        (
                                                            (_at(c, -4) >= _at(o, -4))
                                                            & (_at(c, -2) < _at(c, -3))
                                                        )
                                                        | (
                                                            (~(_at(c, -4) >= _at(o, -4)))
                                                            & (_at(c, -3) < _at(c, -2))
                                                        )
                                                    )
                                                )
                                                & (
                                                    (
                                                        (_at(c, -4) >= _at(o, -4))
                                                        & (_at(c, -1) < _at(c, -2))
                                                    )
                                                    | (
                                                        (~(_at(c, -4) >= _at(o, -4)))
                                                        & (_at(c, -2) < _at(c, -1))
                                                    )
                                                )
                                            )
                                            & (
                                                (
                                                    (_at(c, -4) >= _at(o, -4))
                                                    & (_at(o, 0) > _at(c, -1))
                                                )
                                                | (
                                                    (~(_at(c, -4) >= _at(o, -4)))
                                                    & (_at(c, -1) > _at(o, 0))
                                                )
                                            )
                                        )
                                        & (
                                            ((_at(c, -4) >= _at(o, -4)) & (_at(c, 0) > _at(c, -4)))
                                            | (
                                                (~(_at(c, -4) >= _at(o, -4)))
                                                & (_at(c, -4) > _at(c, 0))
                                            )
                                        )
                                    )
                                    & (
                                        np.abs(
                                            (
                                                (_at(c, -4)).astype(np.float64)
                                                - (_at(o, -4)).astype(np.float64)
                                            )
                                        )
                                        > _avg_rb10_1_m4
                                    )
                                )
                                & (
                                    np.abs(
                                        (
                                            (_at(c, -3)).astype(np.float64)
                                            - (_at(o, -3)).astype(np.float64)
                                        )
                                    )
                                    < _avg_rb10_1_m3
                                )
                            )
                            & (
                                np.abs(
                                    (
                                        (_at(c, -2)).astype(np.float64)
                                        - (_at(o, -2)).astype(np.float64)
                                    )
                                )
                                < _avg_rb10_1_m2
                            )
                        )
                        & (
                            np.abs(
                                ((_at(c, -1)).astype(np.float64) - (_at(o, -1)).astype(np.float64))
                            )
                            < _avg_rb10_1_m1
                        )
                    )
                    & (
                        np.abs((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                        > _avg_rb10_1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 14)


def _cdlseparatinglines(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl5_0p05_m1 = _candle_avg("HighLow", 5, 0.05, -1, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (
                        (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                        != (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                    ).astype(np.int32)
                    + (
                        _at(o, 0)
                        <= ((_at(o, -1)).astype(np.float64) + (_avg_hl5_0p05_m1).astype(np.float64))
                    ).astype(np.int32)
                ).astype(np.int32)
                + (
                    _at(o, 0)
                    >= ((_at(o, -1)).astype(np.float64) - (_avg_hl5_0p05_m1).astype(np.float64))
                ).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100))
            & (
                -(
                    (
                        np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                        > _avg_rb10_1_0
                    )
                    & (
                        (
                            (_at(c, 0) >= _at(o, 0))
                            & (
                                (
                                    (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                                    - (_at(low, 0)).astype(np.float64)
                                )
                                < _avg_hl10_0p1_0
                            )
                        )
                        | (
                            (~(_at(c, 0) >= _at(o, 0)))
                            & (
                                (
                                    (_at(h, 0)).astype(np.float64)
                                    - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                                )
                                < _avg_hl10_0p1_0
                            )
                        )
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 11)


def _cdlshortline(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    _avg_sh10_1_0 = _candle_avg("Shadows", 10, 1.0, 0, o, h, low, c)
    res = ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100)) & (
        -(
            (
                (
                    np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                    < _avg_rb10_1_0
                )
                & (
                    (
                        (_at(h, 0)).astype(np.float64)
                        - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                    )
                    < _avg_sh10_1_0
                )
            )
            & (
                (
                    (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                    - (_at(low, 0)).astype(np.float64)
                )
                < _avg_sh10_1_0
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdlspinningtop(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = ((np.where((_at(c, 0) >= _at(o, 0)), 1, (-1))).astype(np.int32) * np.int32(100)) & (
        -(
            (
                (
                    (
                        (_at(h, 0)).astype(np.float64)
                        - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                    )
                    > np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                )
                & (
                    (
                        (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                        - (_at(low, 0)).astype(np.float64)
                    )
                    > np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
                )
            )
            & (
                np.abs((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                < _avg_rb10_1_0
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdlsticksandwich(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl5_0p05_m2 = _candle_avg("HighLow", 5, 0.05, -2, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -2) >= _at(o, -2))).astype(np.int32)
                    + (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                ).astype(np.int32)
                + (~(_at(c, 0) >= _at(o, 0))).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            100
            & (
                -(
                    (
                        (_at(low, -1) > _at(c, -2))
                        & (
                            _at(c, 0)
                            <= (
                                (_at(c, -2)).astype(np.float64)
                                + (_avg_hl5_0p05_m2).astype(np.float64)
                            )
                        )
                    )
                    & (
                        _at(c, 0)
                        >= ((_at(c, -2)).astype(np.float64) - (_avg_hl5_0p05_m2).astype(np.float64))
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 7)


def _cdltakuri(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl10_0p1_0 = _candle_avg("HighLow", 10, 0.1, 0, o, h, low, c)
    _avg_rb0_2_0 = _candle_avg("RealBody", 0, 2.0, 0, o, h, low, c)
    res = np.where(
        (
            np.abs(((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)))
            <= _avg_hl10_0p1_0
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (_at(h, 0)).astype(np.float64)
                            - (np.maximum(_at(c, 0), _at(o, 0))).astype(np.float64)
                        )
                        < _avg_hl10_0p1_0
                    )
                    & (
                        (
                            (np.minimum(_at(c, 0), _at(o, 0))).astype(np.float64)
                            - (_at(low, 0)).astype(np.float64)
                        )
                        > _avg_rb0_2_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 10)


def _cdltasukigap(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_hl5_0p2_m1 = _candle_avg("HighLow", 5, 0.2, -1, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (
                        (
                            ((_at(o, -1) > _at(o, -2)) & (_at(o, -1) > _at(c, -2)))
                            & (_at(c, -1) > _at(o, -2))
                        )
                        & (_at(c, -1) > _at(c, -2))
                    ).astype(np.int32)
                    + (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                ).astype(np.int32)
                + (~(_at(c, 0) >= _at(o, 0))).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            ((np.where((_at(c, -1) >= _at(o, -1)), 1, (-1))).astype(np.int32) * np.int32(100))
            & (
                -(
                    (
                        (
                            ((_at(o, 0) < _at(c, -1)) & (_at(o, 0) > _at(o, -1)))
                            & (_at(c, 0) < _at(o, -1))
                        )
                        & ((_at(c, 0) > _at(c, -2)) & (_at(c, 0) > _at(o, -2)))
                    )
                    & (
                        np.abs(
                            (
                                np.abs(
                                    (
                                        (_at(c, -1)).astype(np.float64)
                                        - (_at(o, -1)).astype(np.float64)
                                    )
                                )
                            ).astype(np.float64)
                            - (
                                np.abs(
                                    (_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64)
                                )
                            ).astype(np.float64)
                        )
                        < _avg_hl5_0p2_m1
                    )
                ).astype(np.int32)
            )
        ),
        np.where(
            (
                (
                    (
                        (
                            (
                                ((_at(o, -1) < _at(o, -2)) & (_at(o, -1) < _at(c, -2)))
                                & (_at(c, -1) < _at(o, -2))
                            )
                            & (_at(c, -1) < _at(c, -2))
                        ).astype(np.int32)
                        + (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
                    ).astype(np.int32)
                    + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                ).astype(np.int32)
                == np.int32(3)
            ),
            (
                ((np.where((_at(c, -1) >= _at(o, -1)), 1, (-1))).astype(np.int32) * np.int32(100))
                & (
                    -(
                        (
                            (
                                ((_at(o, 0) < _at(o, -1)) & (_at(o, 0) > _at(c, -1)))
                                & (_at(c, 0) > _at(o, -1))
                            )
                            & ((_at(c, 0) < _at(c, -2)) & (_at(c, 0) < _at(o, -2)))
                        )
                        & (
                            np.abs(
                                (
                                    np.abs(
                                        (
                                            (_at(c, -1)).astype(np.float64)
                                            - (_at(o, -1)).astype(np.float64)
                                        )
                                    )
                                ).astype(np.float64)
                                - (
                                    np.abs(
                                        (_at(c, 0)).astype(np.float64)
                                        - (_at(o, 0)).astype(np.float64)
                                    )
                                ).astype(np.float64)
                            )
                            < _avg_hl5_0p2_m1
                        )
                    ).astype(np.int32)
                )
            ),
            0,
        ),
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 7)


def _cdlunique3river(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    _avg_rb10_1_m2 = _candle_avg("RealBody", 10, 1.0, -2, o, h, low, c)
    _avg_rb10_1_0 = _candle_avg("RealBody", 10, 1.0, 0, o, h, low, c)
    res = np.where(
        (
            (
                (
                    (~(_at(c, -2) >= _at(o, -2))).astype(np.int32)
                    + (~(_at(c, -1) >= _at(o, -1))).astype(np.int32)
                ).astype(np.int32)
                + (_at(c, 0) >= _at(o, 0)).astype(np.int32)
            ).astype(np.int32)
            == np.int32(3)
        ),
        (
            100
            & (
                -(
                    (
                        (
                            (
                                ((_at(c, -1) > _at(c, -2)) & (_at(o, -1) <= _at(o, -2)))
                                & (_at(low, -1) < _at(low, -2))
                            )
                            & (_at(o, 0) > _at(low, -1))
                        )
                        & (
                            np.abs(
                                ((_at(c, -2)).astype(np.float64) - (_at(o, -2)).astype(np.float64))
                            )
                            > _avg_rb10_1_m2
                        )
                    )
                    & (
                        np.abs((_at(c, 0)).astype(np.float64) - (_at(o, 0)).astype(np.float64))
                        < _avg_rb10_1_0
                    )
                ).astype(np.int32)
            )
        ),
        0,
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 12)


def _cdlxsidegap3methods(o, h, low, c):
    o, h, low, c = _ctx(o, h, low, c)
    res = ((np.where((_at(c, -2) >= _at(o, -2)), 1, (-1))).astype(np.int32) * np.int32(100)) & (
        -(
            (
                (
                    (
                        (
                            (
                                (
                                    (_at(c, -2) >= _at(o, -2)).astype(np.int32)
                                    == (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                                )
                                & (
                                    (_at(c, -1) >= _at(o, -1)).astype(np.int32)
                                    != (_at(c, 0) >= _at(o, 0)).astype(np.int32)
                                )
                            )
                            & ((_at(o, 0) < _at(c, -1)) | (_at(o, 0) < _at(o, -1)))
                        )
                        & ((_at(o, 0) > _at(c, -1)) | (_at(o, 0) > _at(o, -1)))
                    )
                    & ((_at(c, 0) < _at(c, -2)) | (_at(c, 0) < _at(o, -2)))
                )
                & ((_at(c, 0) > _at(c, -2)) | (_at(c, 0) > _at(o, -2)))
            )
            & (
                (
                    (_at(c, -2) >= _at(o, -2))
                    & (
                        (
                            ((_at(o, -1) > _at(o, -2)) & (_at(o, -1) > _at(c, -2)))
                            & (_at(c, -1) > _at(o, -2))
                        )
                        & (_at(c, -1) > _at(c, -2))
                    )
                )
                | (
                    (~(_at(c, -2) >= _at(o, -2)))
                    & (
                        (
                            ((_at(o, -1) < _at(o, -2)) & (_at(o, -1) < _at(c, -2)))
                            & (_at(c, -1) < _at(o, -2))
                        )
                        & (_at(c, -1) < _at(c, -2))
                    )
                )
            )
        ).astype(np.int32)
    )
    return _fill_lookback(np.asarray(res, dtype=np.int64), 2)


PATTERNS = {
    "tow_crows": _cdl2crows,
    "upside_gap_two_crows": _cdlupsidegap2crows,
    "three_black_crows": _cdl3blackcrows,
    "identical_three_crows": _cdlidentical3crows,
    "three_line_strike": _cdl3linestrike,
    "dark_cloud_cover": _cdldarkcloudcover,
    "evening_doji_star": _cdleveningdojistar,
    "doji_Star": _cdldojistar,
    "hanging_man": _cdlhangingman,
    "hikkake_pattern": _cdlhikkake,
    "modified_hikkake_pattern": _cdlhikkakemod,
    "in_neck_pattern": _cdlinneck,
    "on_neck_pattern": _cdlonneck,
    "thrusting_pattern": _cdlthrusting,
    "shooting_star": _cdlshootingstar,
    "stalled_pattern": _cdlstalledpattern,
    "advance_block": _cdladvanceblock,
    "high_wave_candle": _cdlhighwave,
    "engulfing_pattern": _cdlengulfing,
    "abandoned_baby": _cdlabandonedbaby,
    "closing_marubozu": _cdlclosingmarubozu,
    "doji": _cdldoji,
    "up_down_gap": _cdlgapsidesidewhite,
    "long_legged_doji": _cdllongleggeddoji,
    "rickshaw_man": _cdlrickshawman,
    "marubozu": _cdlmarubozu,
    "three_inside_up_down": _cdl3inside,
    "three_outside_up_down": _cdl3outside,
    "three_stars_in_the_south": _cdl3starsinsouth,
    "three_white_soldiers": _cdl3whitesoldiers,
    "belt_hold": _cdlbelthold,
    "breakaway": _cdlbreakaway,
    "concealing_baby_swallow": _cdlconcealbabyswall,
    "counterattack": _cdlcounterattack,
    "dragonfly_doji": _cdldragonflydoji,
    "evening_star": _cdleveningstar,
    "gravestone_doji": _cdlgravestonedoji,
    "hammer": _cdlhammer,
    "harami_pattern": _cdlharami,
    "harami_cross_pattern": _cdlharamicross,
    "homing_pigeon": _cdlhomingpigeon,
    "inverted_hammer": _cdlinvertedhammer,
    "kicking": _cdlkicking,
    "kicking_bull_bear": _cdlkickingbylength,
    "ladder_bottom": _cdlladderbottom,
    "long_line_candle": _cdllongline,
    "matching_low": _cdlmatchinglow,
    "mat_hold": _cdlmathold,
    "morning_doji_star": _cdlmorningdojistar,
    "morning_star": _cdlmorningstar,
    "piercing_pattern": _cdlpiercing,
    "rising_falling_three": _cdlrisefall3methods,
    "separating_lines": _cdlseparatinglines,
    "short_line_candle": _cdlshortline,
    "spinning_top": _cdlspinningtop,
    "stick_sandwich": _cdlsticksandwich,
    "takuri": _cdltakuri,
    "tasuki_gap": _cdltasukigap,
    "tristar_pattern": _cdltristar,
    "unique_3_river": _cdlunique3river,
    "upside_downside_gap": _cdlxsidegap3methods,
}

PATTERN_LABELS = {
    "tow_crows": "两只乌鸦",
    "upside_gap_two_crows": "向上跳空的两只乌鸦",
    "three_black_crows": "三只乌鸦",
    "identical_three_crows": "三胞胎乌鸦",
    "three_line_strike": "三线打击",
    "dark_cloud_cover": "乌云压顶",
    "evening_doji_star": "十字暮星",
    "doji_Star": "十字星",
    "hanging_man": "上吊线",
    "hikkake_pattern": "陷阱",
    "modified_hikkake_pattern": "修正陷阱",
    "in_neck_pattern": "颈内线",
    "on_neck_pattern": "颈上线",
    "thrusting_pattern": "插入",
    "shooting_star": "射击之星",
    "stalled_pattern": "停顿形态",
    "advance_block": "大敌当前",
    "high_wave_candle": "风高浪大线",
    "engulfing_pattern": "吞噬模式",
    "abandoned_baby": "弃婴",
    "closing_marubozu": "收盘缺影线",
    "doji": "十字",
    "up_down_gap": "向上/下跳空并列阳线",
    "long_legged_doji": "长脚十字",
    "rickshaw_man": "黄包车夫",
    "marubozu": "光头光脚/缺影线",
    "three_inside_up_down": "三内部上涨和下跌",
    "three_outside_up_down": "三外部上涨和下跌",
    "three_stars_in_the_south": "南方三星",
    "three_white_soldiers": "三个白兵",
    "belt_hold": "捉腰带线",
    "breakaway": "脱离",
    "concealing_baby_swallow": "藏婴吞没",
    "counterattack": "反击线",
    "dragonfly_doji": "蜻蜓十字/T形十字",
    "evening_star": "暮星",
    "gravestone_doji": "墓碑十字/倒T十字",
    "hammer": "锤头",
    "harami_pattern": "母子线",
    "harami_cross_pattern": "十字孕线",
    "homing_pigeon": "家鸽",
    "inverted_hammer": "倒锤头",
    "kicking": "反冲形态",
    "kicking_bull_bear": "由较长缺影线决定的反冲形态",
    "ladder_bottom": "梯底",
    "long_line_candle": "长蜡烛",
    "matching_low": "相同低价",
    "mat_hold": "铺垫",
    "morning_doji_star": "十字晨星",
    "morning_star": "晨星",
    "piercing_pattern": "刺透形态",
    "rising_falling_three": "上升/下降三法",
    "separating_lines": "分离线",
    "short_line_candle": "短蜡烛",
    "spinning_top": "纺锤",
    "stick_sandwich": "条形三明治",
    "takuri": "探水竿",
    "tasuki_gap": "跳空并列阴阳线",
    "tristar_pattern": "三星",
    "unique_3_river": "奇特三河床",
    "upside_downside_gap": "上升/下降跳空三法",
}

TALIB_NAMES = {
    "tow_crows": "CDL2CROWS",
    "upside_gap_two_crows": "CDLUPSIDEGAP2CROWS",
    "three_black_crows": "CDL3BLACKCROWS",
    "identical_three_crows": "CDLIDENTICAL3CROWS",
    "three_line_strike": "CDL3LINESTRIKE",
    "dark_cloud_cover": "CDLDARKCLOUDCOVER",
    "evening_doji_star": "CDLEVENINGDOJISTAR",
    "doji_Star": "CDLDOJISTAR",
    "hanging_man": "CDLHANGINGMAN",
    "hikkake_pattern": "CDLHIKKAKE",
    "modified_hikkake_pattern": "CDLHIKKAKEMOD",
    "in_neck_pattern": "CDLINNECK",
    "on_neck_pattern": "CDLONNECK",
    "thrusting_pattern": "CDLTHRUSTING",
    "shooting_star": "CDLSHOOTINGSTAR",
    "stalled_pattern": "CDLSTALLEDPATTERN",
    "advance_block": "CDLADVANCEBLOCK",
    "high_wave_candle": "CDLHIGHWAVE",
    "engulfing_pattern": "CDLENGULFING",
    "abandoned_baby": "CDLABANDONEDBABY",
    "closing_marubozu": "CDLCLOSINGMARUBOZU",
    "doji": "CDLDOJI",
    "up_down_gap": "CDLGAPSIDESIDEWHITE",
    "long_legged_doji": "CDLLONGLEGGEDDOJI",
    "rickshaw_man": "CDLRICKSHAWMAN",
    "marubozu": "CDLMARUBOZU",
    "three_inside_up_down": "CDL3INSIDE",
    "three_outside_up_down": "CDL3OUTSIDE",
    "three_stars_in_the_south": "CDL3STARSINSOUTH",
    "three_white_soldiers": "CDL3WHITESOLDIERS",
    "belt_hold": "CDLBELTHOLD",
    "breakaway": "CDLBREAKAWAY",
    "concealing_baby_swallow": "CDLCONCEALBABYSWALL",
    "counterattack": "CDLCOUNTERATTACK",
    "dragonfly_doji": "CDLDRAGONFLYDOJI",
    "evening_star": "CDLEVENINGSTAR",
    "gravestone_doji": "CDLGRAVESTONEDOJI",
    "hammer": "CDLHAMMER",
    "harami_pattern": "CDLHARAMI",
    "harami_cross_pattern": "CDLHARAMICROSS",
    "homing_pigeon": "CDLHOMINGPIGEON",
    "inverted_hammer": "CDLINVERTEDHAMMER",
    "kicking": "CDLKICKING",
    "kicking_bull_bear": "CDLKICKINGBYLENGTH",
    "ladder_bottom": "CDLLADDERBOTTOM",
    "long_line_candle": "CDLLONGLINE",
    "matching_low": "CDLMATCHINGLOW",
    "mat_hold": "CDLMATHOLD",
    "morning_doji_star": "CDLMORNINGDOJISTAR",
    "morning_star": "CDLMORNINGSTAR",
    "piercing_pattern": "CDLPIERCING",
    "rising_falling_three": "CDLRISEFALL3METHODS",
    "separating_lines": "CDLSEPARATINGLINES",
    "short_line_candle": "CDLSHORTLINE",
    "spinning_top": "CDLSPINNINGTOP",
    "stick_sandwich": "CDLSTICKSANDWICH",
    "takuri": "CDLTAKURI",
    "tasuki_gap": "CDLTASUKIGAP",
    "tristar_pattern": "CDLTRISTAR",
    "unique_3_river": "CDLUNIQUE3RIVER",
    "upside_downside_gap": "CDLXSIDEGAP3METHODS",
}

PATTERN_LOOKBACK = {
    "tow_crows": 12,
    "upside_gap_two_crows": 12,
    "three_black_crows": 13,
    "identical_three_crows": 12,
    "three_line_strike": 8,
    "dark_cloud_cover": 11,
    "evening_doji_star": 12,
    "doji_Star": 11,
    "hanging_man": 11,
    "hikkake_pattern": 5,
    "modified_hikkake_pattern": 10,
    "in_neck_pattern": 11,
    "on_neck_pattern": 11,
    "thrusting_pattern": 11,
    "shooting_star": 11,
    "stalled_pattern": 12,
    "advance_block": 12,
    "high_wave_candle": 10,
    "engulfing_pattern": 2,
    "abandoned_baby": 12,
    "closing_marubozu": 10,
    "doji": 10,
    "up_down_gap": 7,
    "long_legged_doji": 10,
    "rickshaw_man": 10,
    "marubozu": 10,
    "three_inside_up_down": 12,
    "three_outside_up_down": 3,
    "three_stars_in_the_south": 12,
    "three_white_soldiers": 12,
    "belt_hold": 10,
    "breakaway": 14,
    "concealing_baby_swallow": 13,
    "counterattack": 11,
    "dragonfly_doji": 10,
    "evening_star": 12,
    "gravestone_doji": 10,
    "hammer": 11,
    "harami_pattern": 11,
    "harami_cross_pattern": 11,
    "homing_pigeon": 11,
    "inverted_hammer": 11,
    "kicking": 11,
    "kicking_bull_bear": 11,
    "ladder_bottom": 14,
    "long_line_candle": 10,
    "matching_low": 6,
    "mat_hold": 14,
    "morning_doji_star": 12,
    "morning_star": 12,
    "piercing_pattern": 11,
    "rising_falling_three": 14,
    "separating_lines": 11,
    "short_line_candle": 10,
    "spinning_top": 10,
    "stick_sandwich": 7,
    "takuri": 10,
    "tasuki_gap": 7,
    "tristar_pattern": 12,
    "unique_3_river": 12,
    "upside_downside_gap": 2,
}


PATTERN_COLUMNS: tuple = tuple(PATTERNS)


def compute_patterns(open_, high, low, close, needed=None) -> dict:
    """一次算出全部 (或 needed 指定的) 形态列.

    Args:
        open_, high, low, close: 等长序列 (list / np.ndarray / polars Series).
        needed: 可选的列名集合; None 表示全部.

    Returns:
        {列名: np.int64 数组}, 取值 -100 / 0 / +100.
    """
    o, h, _low, c = _ctx(open_, high, low, close)
    cols = PATTERNS if needed is None else {k: v for k, v in PATTERNS.items() if k in needed}
    return {name: fn(o, h, low, c) for name, fn in cols.items()}


def _symbol_slices(symbols) -> list[slice]:
    """按 symbol 返回连续切片 (输入须已按 symbol 排序)。"""
    n = len(symbols)
    if n == 0:
        return []
    change = np.flatnonzero(symbols[1:] != symbols[:-1]) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [n]))
    return [slice(int(s), int(e)) for s, e in zip(starts, ends, strict=True)]


def compute_patterns_frame(
    df,
    needed=None,
    *,
    assume_sorted: bool = False,
):
    """从 Polars DataFrame 计算蜡烛形态, 按 symbol 分段后写回原行序。

    Args:
        df: 需含 symbol/open/high/low/close 列。
        needed: 可选列名集合; None 表示全部 61 列。
        assume_sorted: True 表示已按 (symbol, date) 排序。

    Returns:
        原 DataFrame 追加形态列, 取值 -200/-100/0/100/200, 整型。
    """
    import polars as pl

    if df.is_empty():
        return df

    want = (
        PATTERN_COLUMNS if needed is None else tuple(n for n in PATTERN_COLUMNS if n in set(needed))
    )
    if not want:
        return df

    df = df if assume_sorted else df.sort(["symbol", "date"])

    o = df["open"].to_numpy().astype(np.float64)
    h = df["high"].to_numpy().astype(np.float64)
    low = df["low"].to_numpy().astype(np.float64)
    c = df["close"].to_numpy().astype(np.float64)

    res = {name: np.zeros(len(df), dtype=np.int64) for name in want}
    for sl in _symbol_slices(df["symbol"].to_numpy()):
        part = compute_patterns(o[sl], h[sl], low[sl], c[sl], needed=want)
        for name, arr in part.items():
            res[name][sl] = arr
    return df.with_columns([pl.Series(name, res[name]) for name in want])
