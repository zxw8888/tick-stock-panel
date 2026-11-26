"""InStock 130 指标移植 (Polars + numpy, 不依赖 TA-Lib)。

来源: ``E:/project/stock/instock/core/indicator/calculate_indicator.py``

口径说明
--------
* TA-Lib ``EMA(x, n)``  -> alpha = 2/(n+1) (不是 1/n), 首值用 SMA 种子。
* TA-Lib ``ATR/RSI``    -> alpha = 1/n (Wilder 平滑)。
* ``SUM/MA/MAX/MIN``    -> 定长窗口, 窗口不满为 null。
* ``BBANDS/STDDEV``     -> 总体标准差 (ddof=0), 与 polars 默认 ddof=1 不同。
* 移植后 warmup 期保留 null, 不填 0.0 —— 与 panel 既有 30 个指标一致,
  避免"数据不足"被当成"值为 0"产生假信号。稳态区间与 InStock 逐列一致
  (实测差异为 1e-9 量级浮点噪声)。

列名沿用 InStock 原始命名, 以便直接复用 InStock 的选股公式。
带连字符的 ``cr-ma1/2/3`` 额外提供 ``cr_ma1/2/3`` 别名 —— polars 表达式
里 ``cr-ma1`` 会被解析成减法。
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import polars as pl

__all__ = [
    "BASE_INPUTS_BY_COLUMN",
    "CORE_DEPS",
    "EXTENDED_BY_CATEGORY",
    "EXTENDED_COLUMNS",
    "EXTENDED_INDICATORS",
    "EXTENDED_INPUT_COLUMNS",
    "WARMUP_BARS",
    "compute_extended",
    "extended_deps",
]


def _ema(span: int) -> float:
    """TA-Lib EMA 的 alpha。"""
    return 2.0 / (span + 1.0)


def _wilder(n: int) -> float:
    """TA-Lib ATR/RSI 的 alpha。"""
    return 1.0 / n


# ================================================================
# 指标清单 (按 InStock 输出顺序分组, 共 130 列)
# ================================================================
EXTENDED_INDICATORS: dict[str, str] = {
    # ── MACD (与 panel 的 macd_dif/dea/hist 同源; macdh 口径不同, 见下) ──
    "macd": "MACD DIF (TA-Lib 原值)",
    "macds": "MACD DEA",
    "macdh": "MACD 柱 (TA-Lib 口径: DIF-DEA, 未 x2)",
    # ── KDJ (TA-Lib STOCH: K=SMA(RSV9,5), D=SMA(K,5)) ──
    "kdjk": "KDJ K 值",
    "kdjd": "KDJ D 值",
    "kdjj": "KDJ J 值",
    # ── BOLL (TA-Lib 用 ddof=0) ──
    "boll_ub": "布林上轨",
    "boll": "布林中轨",
    "boll_lb": "布林下轨",
    # ── TRIX ──
    "trix": "三重指数平滑变化率 (%)",
    "trix_20_sma": "TRIX 20 日均线",
    # ── CR (中间价动量) ──
    "m_price": "中间价 = 成交额/成交量",
    "m_price_sf1": "前一日中间价 (首日填 0)",
    "h_m": "高价 - min(中间价, 高价)",
    "m_l": "中间价 - min(中间价, 低价)",
    "h_m_sum": "h_m 26 日累计",
    "m_l_sum": "m_l 26 日累计",
    "cr": "CR 能量指标",
    "cr-ma1": "CR 5 日均线",
    "cr-ma2": "CR 10 日均线",
    "cr-ma3": "CR 20 日均线",
    "cr_ma1": "CR 5 日均线 (别名)",
    "cr_ma2": "CR 10 日均线 (别名)",
    "cr_ma3": "CR 20 日均线 (别名)",
    # ── RSI ──
    "rsi": "RSI 14 (等价 rsi_14)",
    "rsi_12": "RSI 12",
    # ── VR 成交量比率 ──
    "av": "上涨日成交量",
    "avs": "av 26 日累计",
    "bv": "下跌日成交量",
    "bvs": "bv 26 日累计",
    "cv": "平盘日成交量",
    "cvs": "cv 26 日累计",
    "vr": "VR 成交量比率",
    "vr_6_sma": "VR 6 日均线",
    # ── ATR 组件 ──
    "h_l": "当日振幅 (高-低)",
    "h_cy": "高 - 前收",
    "cy_l": "前收 - 低",
    "h_cy_a": "|高 - 前收|",
    "cy_l_a": "|前收 - 低|",
    "tr": "真实波幅",
    "atr": "ATR 14 (等价 atr_14)",
    # ── DMI (stockstats 公式, 非 TA-Lib PLUS_DI) ──
    "high_delta": "最高价日变化",
    "high_m": "上升动向 (正值部分)",
    "low_delta": "最低价日变化 (取反)",
    "low_m": "下降动向 (正值部分)",
    "pdm": "+DM (EMA14)",
    "pdi": "+DI",
    "mdm": "-DM (EMA14)",
    "mdi": "-DI",
    "dx": "DX 动向指数",
    "adx": "ADX",
    "adxr": "ADXR",
    # ── WR ──
    "wr_6": "威廉指标 6",
    "wr_10": "威廉指标 10",
    "wr_14": "威廉指标 14",
    # ── CCI ──
    "cci": "CCI 14",
    "cci_84": "CCI 84",
    # ── DMA ──
    "ma50": "50 日均线",
    "dma": "DMA (MA10-MA50)",
    "dma_10_sma": "DMA 10 日均线",
    # ── TEMA ──
    "tema": "三重指数均线",
    # ── MFI ──
    "mfi": "资金流量指标 14",
    "mfisma": "MFI 6 日均线",
    # ── VWMA ──
    "tpv_14": "14 日成交额累计",
    "vol_14": "14 日成交量累计",
    "vwma": "成交量加权均价",
    "mvwma": "VWMA 6 日均线",
    # ── PPO ──
    "ppo": "PPO (%)",
    "ppos": "PPO 信号线",
    "ppoh": "PPO 柱",
    # ── StochRSI ──
    "rsi_min": "RSI 14 日最低",
    "rsi_max": "RSI 14 日最高",
    "stochrsi_k": "StochRSI K",
    "stochrsi_d": "StochRSI D",
    # ── 沃尔特 WT ──
    "esa": "ESA (中间价 EMA10)",
    "esa_d": "ESA 偏差 EMA10",
    "esa_ci": "ESA CI",
    "wt1": "WT1",
    "wt2": "WT2",
    # ── Supertrend ──
    "m_atr": "ATR x3",
    "hl_avg": "(高+低)/2",
    "b_ub": "基础上轨",
    "b_lb": "基础下轨",
    "supertrend_ub": "Supertrend 上轨",
    "supertrend_lb": "Supertrend 下轨",
    "supertrend": "Supertrend",
    # ── ROC ──
    "roc": "变动率 12 (%)",
    "rocma": "ROC 6 日均线",
    "rocema": "ROC EMA9",
    # ── OBV / SAR ──
    "obv": "能量潮",
    "sar": "抛物线 SAR",
    # ── PSY ──
    "price_up": "上涨标志 (1/0)",
    "price_up_sum": "12 日上涨天数",
    "psy": "心理线 (%)",
    "psyma": "PSY 6 日均线",
    # ── BRAR ──
    "h_o": "高 - 开",
    "o_l": "开 - 低",
    "h_o_sum": "h_o 26 日累计",
    "o_l_sum": "o_l 26 日累计",
    "ar": "AR 人气指标",
    "h_cy_sum": "h_cy 26 日累计",
    "cy_l_sum": "cy_l 26 日累计",
    "br": "BR 意愿指标",
    # ── EMV ──
    "prev_high": "前一日最高 (首日填 0)",
    "prev_low": "前一日最低 (首日填 0)",
    "phl_avg": "前一日高低均值",
    "emva_em": "EMV 日值",
    "emv": "EMV 14 日累计",
    "emva": "EMV 9 日均线",
    # ── BIAS ──
    "ma6": "6 日均线",
    "ma12": "12 日均线",
    "ma24": "24 日均线",
    "bias": "乖离率 6 (%)",
    "bias_12": "乖离率 12 (%)",
    "bias_24": "乖离率 24 (%)",
    # ── DPO ──
    "c_m_11": "11 日均线 (DPO 用)",
    "dpo": "DPO",
    "madpo": "DPO 6 日均线",
    # ── VHF ──
    "hcp_lcp": "28 日收盘极差",
    "vhf": "十字过滤线",
    # ── RVI ──
    "rvi_x": "RVI 分子分量",
    "rvi_y": "RVI 分母分量",
    "rvi": "相对活力指数",
    "rvis": "RVI 平滑",
    # ── FI ──
    "fi": "力度指标日值",
    "force_2": "力度 2 日 EMA",
    "force_13": "力度 13 日 EMA",
    # ── ENE ──
    "ene_ue": "轨道线上轨",
    "ene_le": "轨道线下轨",
    "ene": "轨道线中轨",
    # ── VOL ──
    "vol_5": "5 日均量 (等价 vol_ma5)",
    "vol_10": "10 日均量 (等价 vol_ma10)",
    # ── MA ──
    "ma200": "200 日均线",
}

#: 纯中间量: 是别的指标的中间步骤, 不构成独立交易含义
_INTERMEDIATE_COLUMNS: frozenset[str] = frozenset({
    "m_price", "m_price_sf1", "h_m", "m_l", "h_m_sum", "m_l_sum",
    "av", "bv", "cv", "avs", "bvs", "cvs",
    "h_l", "h_cy", "cy_l", "h_cy_a", "cy_l_a", "tr",
    "high_delta", "high_m", "low_delta", "low_m", "pdm", "mdm",
    "tpv_14", "vol_14", "rsi_min", "rsi_max",
    "m_atr", "hl_avg", "b_ub", "b_lb",
    "price_up", "price_up_sum",
    "h_o", "o_l", "h_o_sum", "o_l_sum", "h_cy_sum", "cy_l_sum",
    "prev_high", "prev_low", "phl_avg", "emva_em",
    "c_m_11", "hcp_lcp", "rvi_x", "rvi_y", "fi",
})

#: 全部 extended 列名
EXTENDED_COLUMNS: tuple[str, ...] = tuple(EXTENDED_INDICATORS)

#: 可作为策略输入列的扩展指标 (排除纯中间量)
EXTENDED_INPUT_COLUMNS: tuple[str, ...] = tuple(
    c for c in EXTENDED_COLUMNS if c not in _INTERMEDIATE_COLUMNS
)

#: extended 指标依赖的 panel 核心列 (缺失时由 compute_indicators 补算)
CORE_DEPS: frozenset[str] = frozenset({
    "prev_close", "ma10", "ma20", "atr_14", "rsi_14", "change_pct",
    "vol_ma5", "vol_ma10",
})


def extended_deps() -> dict[str, frozenset[str]]:
    """返回 extended 列 -> 其依赖的 panel 核心列 (供 needed 闭包使用)。"""
    return {c: CORE_DEPS for c in EXTENDED_COLUMNS}


#: 每列所需的最小历史根数 (回测 warmup / 前端提示用)
WARMUP_BARS: dict[str, int] = {
    # 短周期 (默认 60 根足够)
    "macd": 34, "macds": 34, "macdh": 34,
    "kdjk": 9, "kdjd": 9, "kdjj": 9,
    "rsi": 14, "rsi_12": 12,
    "atr": 14, "tr": 2,
    "wr_6": 6, "wr_10": 10, "wr_14": 14,
    "psy": 12, "psyma": 17,
    "roc": 12, "rocma": 17, "rocema": 20,
    "bias": 6, "bias_12": 12, "bias_24": 24,
    "ma6": 6, "ma12": 12,
    "vol_5": 5, "vol_10": 10,
    "dma": 50, "dma_10_sma": 59,
    "force_2": 2, "force_13": 13,
    "ene_ue": 10, "ene_le": 10, "ene": 10,
    "rvis": 13, "rvi": 13,
    "ppo": 34, "ppos": 42, "ppoh": 42,
    "pdi": 16, "mdi": 16, "dx": 16, "adx": 21, "adxr": 26,
    "price_up": 1, "price_up_sum": 12,
    "h_o": 1, "o_l": 1, "h_o_sum": 26, "o_l_sum": 26,
    "h_cy_sum": 26, "cy_l_sum": 26,
    "h_l": 1, "h_cy": 1, "cy_l": 1, "h_cy_a": 1, "cy_l_a": 1,
    "high_delta": 1, "low_delta": 1, "high_m": 1, "low_m": 1,
    "pdm": 15, "mdm": 15,
    "prev_high": 1, "prev_low": 1, "phl_avg": 1,
    "m_atr": 14, "hl_avg": 1, "b_ub": 14, "b_lb": 14,
    "rsi_min": 27, "rsi_max": 27,
    "c_m_11": 11, "hcp_lcp": 28, "rvi_x": 4, "rvi_y": 4, "fi": 2,
    "vwma": 14, "mvwma": 19, "esa": 10, "esa_d": 10,
    "emv": 14, "emva": 23,
    "ma200": 200,
    "cci_84": 84,
    "ma50": 50,
    "vhf": 28,
    "ma24": 24,
    "trix": 36, "trix_20_sma": 55,
    "cr": 26, "cr-ma1": 26, "cr-ma2": 26, "cr-ma3": 26,
    "cr_ma1": 26, "cr_ma2": 26, "cr_ma3": 26,
    "ar": 26, "br": 26,
    "vr": 26, "vr_6_sma": 31,
    "ma20": 20, "boll_ub": 20, "boll": 20, "boll_lb": 20,
    "dpo": 12, "madpo": 17,
    "wt2": 25, "wt1": 21, "esa_ci": 21,
    "stochrsi_k": 27, "stochrsi_d": 29,
    "mfi": 14, "mfisma": 19,
    "cci": 14,
    "tema": 40,
    "supertrend": 14, "supertrend_ub": 14, "supertrend_lb": 14,
    "sar": 2, "obv": 2,
}


#: 每列需要的原始 OHLCV 列 (供回测 base_columns 解析)
_NEEDS_AMOUNT: frozenset[str] = frozenset({
    "m_price", "m_price_sf1", "h_m", "m_l", "h_m_sum", "m_l_sum",
    "cr", "cr-ma1", "cr-ma2", "cr-ma3", "cr_ma1", "cr_ma2", "cr_ma3",
    "tpv_14", "vwma", "mvwma",
    "esa", "esa_d", "esa_ci", "wt1", "wt2",
    "emva_em", "emv", "emva",
})

BASE_INPUTS_BY_COLUMN: dict[str, frozenset[str]] = {
    c: (frozenset({"amount"}) if c in _NEEDS_AMOUNT else frozenset())
    for c in EXTENDED_COLUMNS
}


#: 扩展指标的分类 (前端字段选择器分组用)
EXTENDED_BY_CATEGORY: dict[str, list[str]] = {
    "ext_ma": [
        "ma6", "ma12", "ma24", "ma50", "ma200",
        "tema", "dma", "dma_10_sma",
    ],
    "ext_bias": ["bias", "bias_12", "bias_24", "dpo", "madpo"],
    "ext_momentum": ["roc", "rocma", "rocema", "rvi", "rvis", "vhf"],
    "ext_trend": [
        "supertrend", "supertrend_ub", "supertrend_lb", "sar",
        "adx", "adxr", "pdi", "mdi", "dx",
        "ar", "br", "cr", "cr_ma1", "cr_ma2", "cr_ma3",
        "ene", "ene_ue", "ene_le",
    ],
    "ext_oscillator": [
        "rsi", "rsi_12", "kdjk", "kdjd", "kdjj",
        "cci", "cci_84", "wr_6", "wr_10", "wr_14",
        "stochrsi_k", "stochrsi_d", "mfi", "mfisma",
        "psy", "psyma", "wt1", "wt2",
    ],
    "ext_volume": [
        "obv", "vr", "vr_6_sma", "vwma", "mvwma",
        "vol_5", "vol_10", "force_2", "force_13",
    ],
    "ext_other": [
        "macd", "macds", "macdh", "boll", "boll_ub", "boll_lb",
        "trix", "trix_20_sma", "ppo", "ppoh", "ppos",
        "emv", "emva", "atr", "esa", "esa_ci", "esa_d",
    ],
}


# ================================================================
# numpy 辅助
# ================================================================
def _symbol_slices(symbols: np.ndarray) -> list[slice]:
    """按 symbol 返回连续切片 (输入须已按 symbol 排序)。"""
    if len(symbols) == 0:
        return []
    change = np.flatnonzero(symbols[1:] != symbols[:-1]) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [len(symbols)]))
    return [slice(int(s), int(e)) for s, e in zip(starts, ends, strict=True)]


def _cci(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    tp = (high + low + close) / 3.0
    out = np.full(tp.shape, np.nan, dtype=np.float64)
    if len(tp) < n:
        return out
    win = np.lib.stride_tricks.sliding_window_view(tp, n)
    ma = win.mean(axis=1)
    mad = np.abs(win - ma[:, None]).mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[n - 1:] = (tp[n - 1:] - ma) / (0.015 * mad)
    return out


def _sar(high: np.ndarray, low: np.ndarray, af_step: float = 0.02,
         af_max: float = 0.2) -> np.ndarray:
    """Wilder 抛物线 SAR (TA-Lib 默认 accel=0.02, max=0.2)。"""
    n = len(high)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < 2:
        return out
    is_long = True
    af = af_step
    ep = high[0]
    sar = low[0]
    out[0] = sar
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if is_long:
            if i >= 2:
                sar = min(sar, low[i - 1], low[i - 2])
            elif i == 1:
                sar = min(sar, low[i - 1])
            if low[i] < sar:
                is_long = False
                sar = ep
                ep = low[i]
                af = af_step
            elif high[i] > ep:
                ep = high[i]
                af = min(af + af_step, af_max)
        else:
            if i >= 2:
                sar = max(sar, high[i - 1], high[i - 2])
            elif i == 1:
                sar = max(sar, high[i - 1])
            if high[i] > sar:
                is_long = True
                sar = ep
                ep = high[i]
                af = af_step
            elif low[i] < ep:
                ep = low[i]
                af = min(af + af_step, af_max)
        out[i] = sar
    return out


def _supertrend(close: np.ndarray, b_ub: np.ndarray, b_lb: np.ndarray
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """InStock 原版 supertrend (逐根递推)。"""
    n = len(close)
    ub = np.empty(n, dtype=np.float64)
    lb = np.empty(n, dtype=np.float64)
    st = np.empty(n, dtype=np.float64)
    for i in range(n):
        if i == 0:
            ub[i] = b_ub[i]
            lb[i] = b_lb[i]
            st[i] = ub[i] if close[i] <= ub[i] else lb[i]
            continue
        last_close = close[i - 1]
        curr_close = close[i]
        last_ub = ub[i - 1]
        last_lb = lb[i - 1]
        last_st = st[i - 1]
        curr_b_ub = b_ub[i]
        curr_b_lb = b_lb[i]

        ub[i] = curr_b_ub if (curr_b_ub < last_ub or last_close > last_ub) else last_ub
        lb[i] = curr_b_lb if (curr_b_lb > last_lb or last_close < last_lb) else last_lb

        if last_st == last_ub:
            st[i] = ub[i] if curr_close <= ub[i] else lb[i]
        elif last_st == last_lb:
            st[i] = lb[i] if curr_close > lb[i] else ub[i]
        else:
            st[i] = ub[i] if curr_close <= ub[i] else lb[i]
    return ub, lb, st


# ================================================================
# 主入口
# ================================================================
def _numpy_stage(
    df: pl.DataFrame,
    names: list[str],
    fn: Callable[[dict[str, np.ndarray], int], dict[str, np.ndarray]],
    cols: list[str],
) -> pl.DataFrame:
    """按 symbol 分段跑 numpy 递推, 结果按原行序写回。"""
    sym = df["symbol"].to_numpy()
    arrs = {c: df[c].to_numpy().astype(np.float64) for c in cols}
    res = {n: np.full(len(df), np.nan, dtype=np.float64) for n in names}
    for sl in _symbol_slices(sym):
        sub = {c: arrs[c][sl] for c in cols}
        out = fn(sub, sl.stop - sl.start)
        for n in names:
            res[n][sl] = out[n]
    return df.with_columns([pl.Series(n, res[n]) for n in names])


def compute_extended(
    df: pl.DataFrame,
    needed: set[str] | None = None,
    *,
    assume_sorted: bool = False,
    fill_warmup_zero: bool = False,
) -> pl.DataFrame:
    """计算 InStock 的 130 个扩展指标。

    参数
    ----
    needed:
        None  -> 全部 130 列。
        集合  -> 只算这些列 (不在 ``EXTENDED_COLUMNS`` 里的名字被忽略)。
    assume_sorted:
        True 表示 df 已按 (symbol, date) 排好序, 跳过排序。
    fill_warmup_zero:
        True  -> 复刻 InStock 语义: NaN/Inf/窗口不足一律写 0.0
                 (InStock 的选股公式按这个假设写的)。
        False -> 默认保持 null (与本仓既有 30 个指标一致, 不产生假 0 值)。
    """
    if df.is_empty():
        return df

    want = (
        set(EXTENDED_COLUMNS)
        if needed is None
        else (set(needed) & set(EXTENDED_COLUMNS))
    )
    if not want:
        return df

    df = df if assume_sorted else df.sort(["symbol", "date"])

    # 复用 panel 已有的核心指标, 缺失时按需补算 (needed 里不含扩展列, 不会递归)
    missing_core = CORE_DEPS - set(df.columns)
    if missing_core:
        from app.indicators.pipeline import compute_indicators

        df = compute_indicators(df, needed=missing_core, assume_sorted=True)

    has_amount = "amount" in df.columns

    def ema(col: str, n: int) -> pl.Expr:
        return pl.col(col).ewm_mean(alpha=_ema(n), adjust=False).over("symbol")

    def shift0(col: str, n: int = 1) -> pl.Expr:
        return pl.col(col).shift(n).over("symbol").fill_null(0.0)

    def diff0(col: str) -> pl.Expr:
        """InStock 的 np.insert(np.diff(x), 0, 0.0) 语义: 首行填 0。"""
        return (pl.col(col) - pl.col(col).shift(1).over("symbol")).fill_null(0.0)

    def rsum(col: str, n: int) -> pl.Expr:
        return pl.col(col).rolling_sum(n).over("symbol")

    def rmean(col: str, n: int) -> pl.Expr:
        return pl.col(col).rolling_mean(n).over("symbol")

    def rmax(col: str, n: int) -> pl.Expr:
        return pl.col(col).rolling_max(n).over("symbol")

    def rmin(col: str, n: int) -> pl.Expr:
        return pl.col(col).rolling_min(n).over("symbol")

    # ── Pass A: 单根 K 线派生列 ─────────────────────────────
    prev_close0 = pl.col("prev_close").fill_null(0.0)
    pass_a: list[pl.Expr] = [
        prev_close0.alias("_pc0"),
        (pl.col("high") - pl.col("low")).alias("h_l"),
        (pl.col("high") - prev_close0).alias("h_cy"),
        (prev_close0 - pl.col("low")).alias("cy_l"),
        (pl.col("high") - prev_close0).abs().alias("h_cy_a"),
        (prev_close0 - pl.col("low")).abs().alias("cy_l_a"),
        ((pl.col("high") + pl.col("low")) / 2.0).alias("hl_avg"),
        (pl.col("high") - pl.col("open")).alias("h_o"),
        (pl.col("open") - pl.col("low")).alias("o_l"),
        shift0("high").alias("prev_high"),
        shift0("low").alias("prev_low"),
        pl.when(pl.col("close") > prev_close0).then(1.0).otherwise(0.0).alias("price_up"),
        diff0("high").alias("high_delta"),
        (-diff0("low")).alias("low_delta"),
        (diff0("close") * pl.col("volume")).alias("fi"),
    ]
    if has_amount:
        pass_a.append((pl.col("amount") / pl.col("volume")).alias("m_price"))
    df = df.with_columns(pass_a)

    df = df.with_columns([
        pl.max_horizontal(pl.col("h_l"), pl.col("h_cy_a"), pl.col("cy_l_a")).alias("tr"),
        ((pl.col("high_delta") + pl.col("high_delta").abs()) / 2.0).alias("high_m"),
        ((pl.col("low_delta") + pl.col("low_delta").abs()) / 2.0).alias("low_m"),
        ((pl.col("prev_high") + pl.col("prev_low")) / 2.0).alias("phl_avg"),
    ])
    if has_amount:
        df = df.with_columns([
            shift0("m_price").alias("m_price_sf1"),
        ]).with_columns([
            (pl.col("high") - pl.min_horizontal(pl.col("m_price_sf1"), pl.col("high"))).alias("h_m"),
            (pl.col("m_price_sf1") - pl.min_horizontal(pl.col("m_price_sf1"), pl.col("low"))).alias("m_l"),
        ])

    # ── Pass B: 均线 / 窗口统计 ────────────────────────────
    b: list[pl.Expr] = [
        rmean("close", 6).alias("ma6"),
        rmean("close", 12).alias("ma12"),
        rmean("close", 24).alias("ma24"),
        rmean("close", 50).alias("ma50"),
        rmean("close", 200).alias("ma200"),
        rmean("volume", 5).alias("vol_5"),
        rmean("volume", 10).alias("vol_10"),
    ]
    df = df.with_columns(b)

    # MACD (TA-Lib: DIF/DEA/柱, 柱不乘 2)
    if {"macd_dif", "macd_dea"} <= set(df.columns):
        df = df.with_columns([
            pl.col("macd_dif").alias("macd"),
            pl.col("macd_dea").alias("macds"),
        ])
    else:
        df = df.with_columns([
            (ema("close", 12) - ema("close", 26)).alias("macd"),
        ]).with_columns([
            ema("macd", 9).alias("macds"),
        ])
    df = df.with_columns([
        (pl.col("macd") - pl.col("macds")).alias("macdh"),
    ])

    # ── BOLL (ddof=0, TA-Lib 口径) ─────────────────────────
    df = df.with_columns([
        pl.col("close").rolling_std(20, ddof=0).over("symbol").alias("_boll_std0"),
    ]).with_columns([
        (pl.col("ma20") + 2 * pl.col("_boll_std0")).alias("boll_ub"),
        pl.col("ma20").alias("boll"),
        (pl.col("ma20") - 2 * pl.col("_boll_std0")).alias("boll_lb"),
    ])

    # ── KDJ (TA-Lib STOCH, matype=1 => EMA alpha=1/3) ──────
    _ll9 = rmin("low", 9)
    _hh9 = rmax("high", 9)
    _rng9 = _hh9 - _ll9
    _rsv = (
        pl.when(_rng9 > 0)
        .then(100.0 * (pl.col("close") - _ll9) / _rng9)
        .otherwise(0.0)
    )
    df = df.with_columns([_rsv.alias("_rsv9")]).with_columns([
        pl.col("_rsv9").ewm_mean(alpha=1.0 / 3, adjust=False).over("symbol").alias("kdjk"),
    ]).with_columns([
        pl.col("kdjk").ewm_mean(alpha=1.0 / 3, adjust=False).over("symbol").alias("kdjd"),
    ]).with_columns([
        (3 * pl.col("kdjk") - 2 * pl.col("kdjd")).alias("kdjj"),
    ])

    # ── TRIX ──────────────────────────────────────────────
    df = df.with_columns([ema("close", 12).alias("_t1")])
    df = df.with_columns([ema("_t1", 12).alias("_t2")])
    df = df.with_columns([ema("_t2", 12).alias("_t3")])
    df = df.with_columns([
        (100.0 * (pl.col("_t3") - pl.col("_t3").shift(1).over("symbol"))
         / pl.col("_t3").shift(1).over("symbol")).alias("trix"),
    ])
    df = df.with_columns([rmean("trix", 20).alias("trix_20_sma")])

    # ── CR ────────────────────────────────────────────────
    if has_amount:
        df = df.with_columns([
            rsum("h_m", 26).alias("h_m_sum"),
            rsum("m_l", 26).alias("m_l_sum"),
        ]).with_columns([
            pl.when(pl.col("m_l_sum") != 0)
            .then(100.0 * pl.col("h_m_sum") / pl.col("m_l_sum"))
            .otherwise(None)
            .alias("cr"),
        ]).with_columns([
            rmean("cr", 5).alias("cr_ma1"),
            rmean("cr", 10).alias("cr_ma2"),
            rmean("cr", 20).alias("cr_ma3"),
        ])

    # ── RSI 12 ────────────────────────────────────────────
    df = df.with_columns([
        pl.col("rsi_14").alias("rsi"),
    ])
    _delta = pl.col("close").diff().over("symbol")
    df = df.with_columns([
        pl.when(_delta > 0).then(_delta).otherwise(0.0).alias("_gain"),
        pl.when(_delta < 0).then(-_delta).otherwise(0.0).alias("_loss"),
    ])
    df = df.with_columns([
        pl.col("_gain").ewm_mean(alpha=_wilder(12), adjust=False).over("symbol").alias("_ag12"),
        pl.col("_loss").ewm_mean(alpha=_wilder(12), adjust=False).over("symbol").alias("_al12"),
    ]).with_columns([
        (100.0 - 100.0 / (1.0 + pl.col("_ag12") / pl.when(pl.col("_al12") == 0)
                          .then(1e-12).otherwise(pl.col("_al12")))).alias("rsi_12"),
    ])

    # ── VR ────────────────────────────────────────────────
    _up = pl.col("change_pct") > 0
    _dn = pl.col("change_pct") < 0
    df = df.with_columns([
        pl.when(_up).then(pl.col("volume")).otherwise(0.0).alias("av"),
        pl.when(_dn).then(pl.col("volume")).otherwise(0.0).alias("bv"),
        pl.when(~_up & ~_dn).then(pl.col("volume")).otherwise(0.0).alias("cv"),
    ]).with_columns([
        rsum("av", 26).alias("avs"),
        rsum("bv", 26).alias("bvs"),
        rsum("cv", 26).alias("cvs"),
    ]).with_columns([
        pl.when((pl.col("bvs") + pl.col("cvs") / 2) != 0)
        .then(100.0 * (pl.col("avs") + pl.col("cvs") / 2) / (pl.col("bvs") + pl.col("cvs") / 2))
        .otherwise(None)
        .alias("vr"),
    ]).with_columns([
        rmean("vr", 6).alias("vr_6_sma"),
    ])

    # ── ATR 别名 ───────────────────────────────────────────
    df = df.with_columns([pl.col("atr_14").alias("atr")])

    # ── DMI (stockstats 公式) ──────────────────────────────
    # polars 不支持对 when 结果直接 .ewm_mean, 先落中间列
    df = df.with_columns([
        pl.when(pl.col("high_m") > pl.col("low_m")).then(pl.col("high_m")).otherwise(0.0).alias("_hdm"),
        pl.when(pl.col("low_m") > pl.col("high_m")).then(pl.col("low_m")).otherwise(0.0).alias("_ldm"),
    ])
    df = df.with_columns([
        ema("_hdm", 14).alias("pdm"),
        ema("_ldm", 14).alias("mdm"),
    ]).with_columns([
        pl.when(pl.col("atr") != 0)
        .then(100.0 * pl.col("pdm") / pl.col("atr"))
        .otherwise(None)
        .alias("pdi"),
        pl.when(pl.col("atr") != 0)
        .then(100.0 * pl.col("mdm") / pl.col("atr"))
        .otherwise(None)
        .alias("mdi"),
    ]).with_columns([
        pl.when((pl.col("pdi") + pl.col("mdi")) != 0)
        .then(100.0 * (pl.col("pdi") - pl.col("mdi")).abs() / (pl.col("pdi") + pl.col("mdi")))
        .otherwise(None)
        .alias("dx"),
    ])
    df = df.with_columns([ema("dx", 6).alias("adx")])
    df = df.with_columns([ema("adx", 6).alias("adxr")])

    # ── WR ────────────────────────────────────────────────
    for n in (6, 10, 14):
        _hh = rmax("high", n)
        _ll = rmin("low", n)
        df = df.with_columns([
            pl.when((_hh - _ll) != 0)
            .then(-100.0 * (_hh - pl.col("close")) / (_hh - _ll))
            .otherwise(None)
            .alias(f"wr_{n}"),
        ])

    # ── CCI (需要 MAD, 走 numpy) ──────────────────────────
    df = _numpy_stage(
        df,
        ["cci", "cci_84"],
        lambda a, n: {
            "cci": _cci(a["high"], a["low"], a["close"], 14),
            "cci_84": _cci(a["high"], a["low"], a["close"], 84),
        },
        ["high", "low", "close"],
    )

    # ── DMA / TEMA ────────────────────────────────────────
    df = df.with_columns([
        (pl.col("ma10") - pl.col("ma50")).alias("dma"),
    ]).with_columns([
        rmean("dma", 10).alias("dma_10_sma"),
    ])
    df = df.with_columns([ema("close", 14).alias("_te1")])
    df = df.with_columns([ema("_te1", 14).alias("_te2")])
    df = df.with_columns([ema("_te2", 14).alias("_te3")])
    df = df.with_columns([
        (3 * pl.col("_te1") - 3 * pl.col("_te2") + pl.col("_te3")).alias("tema"),
    ])

    # ── MFI ───────────────────────────────────────────────
    _tp = (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0
    _tp_prev = _tp.shift(1).over("symbol")
    _mf = _tp * pl.col("volume")
    df = df.with_columns([
        pl.when(_tp > _tp_prev).then(_mf).when(_tp < _tp_prev).then(0.0)
        .otherwise(None).alias("_posmf"),
        pl.when(_tp < _tp_prev).then(_mf).when(_tp > _tp_prev).then(0.0)
        .otherwise(None).alias("_negmf"),
    ]).with_columns([
        rsum("_posmf", 14).alias("_sp"),
        rsum("_negmf", 14).alias("_sn"),
    ]).with_columns([
        pl.when(pl.col("_sn") > 0)
        .then(100.0 - 100.0 / (1.0 + pl.col("_sp") / pl.col("_sn")))
        .otherwise(pl.when(pl.col("_sp") > 0).then(100.0).otherwise(None))
        .alias("mfi"),
    ]).with_columns([
        rmean("mfi", 6).alias("mfisma"),
    ])

    # ── VWMA ──────────────────────────────────────────────
    if has_amount:
        df = df.with_columns([
            rsum("amount", 14).alias("tpv_14"),
            rsum("volume", 14).alias("vol_14"),
        ]).with_columns([
            pl.when(pl.col("vol_14") != 0)
            .then(pl.col("tpv_14") / pl.col("vol_14"))
            .otherwise(None)
            .alias("vwma"),
        ]).with_columns([
            rmean("vwma", 6).alias("mvwma"),
        ])

    # ── PPO ───────────────────────────────────────────────
    _e12 = ema("close", 12)
    _e26 = ema("close", 26)
    df = df.with_columns([
        pl.when(_e26 != 0).then(100.0 * (_e12 - _e26) / _e26).otherwise(None).alias("ppo"),
    ]).with_columns([
        ema("ppo", 9).alias("ppos"),
    ]).with_columns([
        (pl.col("ppo") - pl.col("ppos")).alias("ppoh"),
    ])

    # ── StochRSI ──────────────────────────────────────────
    df = df.with_columns([
        rmin("rsi", 14).alias("rsi_min"),
        rmax("rsi", 14).alias("rsi_max"),
    ]).with_columns([
        pl.when((pl.col("rsi_max") - pl.col("rsi_min")) != 0)
        .then(100.0 * (pl.col("rsi") - pl.col("rsi_min"))
              / (pl.col("rsi_max") - pl.col("rsi_min")))
        .otherwise(None)
        .alias("stochrsi_k"),
    ]).with_columns([
        rmean("stochrsi_k", 3).alias("stochrsi_d"),
    ])

    # ── WT (沃尔特) ────────────────────────────────────────
    if has_amount:
        df = df.with_columns([ema("m_price", 10).alias("esa")])
        df = df.with_columns([
            (pl.col("m_price") - pl.col("esa")).abs().alias("_esa_dev"),
        ]).with_columns([
            ema("_esa_dev", 10).alias("esa_d"),
        ]).with_columns([
            pl.when(pl.col("esa_d") != 0)
            .then((pl.col("m_price") - pl.col("esa")) / (0.015 * pl.col("esa_d")))
            .otherwise(None)
            .alias("esa_ci"),
        ]).with_columns([
            ema("esa_ci", 21).alias("wt1"),
        ]).with_columns([
            rmean("wt1", 4).alias("wt2"),
        ])

    # ── Supertrend 基础轨 ──────────────────────────────────
    df = df.with_columns([
        (pl.col("atr") * 3).alias("m_atr"),
    ]).with_columns([
        (pl.col("hl_avg") + pl.col("m_atr")).alias("b_ub"),
        (pl.col("hl_avg") - pl.col("m_atr")).alias("b_lb"),
    ])

    # ── ROC ───────────────────────────────────────────────
    _c12 = pl.col("close").shift(12).over("symbol")
    df = df.with_columns([
        pl.when(_c12 != 0).then(100.0 * (pl.col("close") - _c12) / _c12).otherwise(None).alias("roc"),
    ]).with_columns([
        rmean("roc", 6).alias("rocma"),
    ]).with_columns([
        ema("roc", 9).alias("rocema"),
    ])

    # ── OBV (可向量化) ─────────────────────────────────────
    # TA-Lib OBV 首根 = volume[0] (不是 0), 之后按涨跌累加
    _is_first = pl.int_range(pl.len()).over("symbol") == 0
    df = df.with_columns([
        pl.when(_is_first)
        .then(pl.col("volume"))
        .when(pl.col("close") > pl.col("prev_close"))
        .then(pl.col("volume"))
        .when(pl.col("close") < pl.col("prev_close"))
        .then(-pl.col("volume"))
        .otherwise(0.0)
        .alias("_obv_delta"),
    ]).with_columns([
        pl.col("_obv_delta").cum_sum().over("symbol").alias("obv"),
    ])

    # ── PSY ───────────────────────────────────────────────
    df = df.with_columns([
        rsum("price_up", 12).alias("price_up_sum"),
    ]).with_columns([
        pl.when(pl.col("price_up_sum").is_not_null())
        .then(100.0 * pl.col("price_up_sum") / 12.0)
        .otherwise(None)
        .alias("psy"),
    ]).with_columns([
        rmean("psy", 6).alias("psyma"),
    ])

    # ── BRAR ──────────────────────────────────────────────
    df = df.with_columns([
        rsum("h_o", 26).alias("h_o_sum"),
        rsum("o_l", 26).alias("o_l_sum"),
        rsum("h_cy", 26).alias("h_cy_sum"),
        rsum("cy_l", 26).alias("cy_l_sum"),
    ]).with_columns([
        pl.when(pl.col("o_l_sum") != 0)
        .then(100.0 * pl.col("h_o_sum") / pl.col("o_l_sum"))
        .otherwise(None)
        .alias("ar"),
        pl.when(pl.col("cy_l_sum") != 0)
        .then(100.0 * pl.col("h_cy_sum") / pl.col("cy_l_sum"))
        .otherwise(None)
        .alias("br"),
    ])

    # ── EMV ───────────────────────────────────────────────
    if has_amount:
        df = df.with_columns([
            pl.when(pl.col("amount") != 0)
            .then((pl.col("hl_avg") - pl.col("phl_avg")) * pl.col("h_l") / pl.col("amount"))
            .otherwise(None)
            .alias("emva_em"),
        ]).with_columns([
            rsum("emva_em", 14).alias("emv"),
        ]).with_columns([
            rmean("emv", 9).alias("emva"),
        ])

    # ── BIAS ──────────────────────────────────────────────
    df = df.with_columns([
        pl.when(pl.col("ma6") != 0)
        .then(100.0 * (pl.col("close") - pl.col("ma6")) / pl.col("ma6"))
        .otherwise(None).alias("bias"),
        pl.when(pl.col("ma12") != 0)
        .then(100.0 * (pl.col("close") - pl.col("ma12")) / pl.col("ma12"))
        .otherwise(None).alias("bias_12"),
        pl.when(pl.col("ma24") != 0)
        .then(100.0 * (pl.col("close") - pl.col("ma24")) / pl.col("ma24"))
        .otherwise(None).alias("bias_24"),
    ])

    # ── DPO ───────────────────────────────────────────────
    df = df.with_columns([
        rmean("close", 11).alias("c_m_11"),
    ]).with_columns([
        (pl.col("close") - shift0("c_m_11")).alias("dpo"),
    ]).with_columns([
        rmean("dpo", 6).alias("madpo"),
    ])

    # ── VHF ───────────────────────────────────────────────
    df = df.with_columns([
        (rmax("close", 28) - rmin("close", 28)).alias("hcp_lcp"),
        (pl.col("close") - pl.col("prev_close")).abs().alias("_abs_dc"),
    ]).with_columns([
        rsum("_abs_dc", 28).alias("_sum_dc"),
    ]).with_columns([
        pl.when(pl.col("_sum_dc") != 0)
        .then(pl.col("hcp_lcp") / pl.col("_sum_dc"))
        .otherwise(None)
        .alias("vhf"),
    ])

    # ── RVI ───────────────────────────────────────────────
    df = df.with_columns([
        (((pl.col("close") - pl.col("open"))
          + 2 * (pl.col("prev_close") - shift0("open"))
          + 2 * (shift0("close", 2) - shift0("open", 2))
          + (shift0("close", 3) - shift0("open", 3))) / 6.0).alias("rvi_x"),
        (((pl.col("high") - pl.col("low"))
          + 2 * (pl.col("prev_high") - pl.col("prev_low"))
          + 2 * (shift0("high", 2) - shift0("low", 2))
          + (shift0("high", 3) - shift0("low", 3))) / 6.0).alias("rvi_y"),
    ])
    df = df.with_columns([
        pl.when(rmean("rvi_y", 10) != 0)
        .then(rmean("rvi_x", 10) / rmean("rvi_y", 10))
        .otherwise(None)
        .alias("rvi"),
    ]).with_columns([
        ((pl.col("rvi")
          + 2 * shift0("rvi")
          + 2 * shift0("rvi", 2)
          + shift0("rvi", 3)) / 6.0).alias("rvis"),
    ])

    # ── 力度指标 ───────────────────────────────────────────
    df = df.with_columns([
        ema("fi", 2).alias("force_2"),
        ema("fi", 13).alias("force_13"),
    ])

    # ── ENE 轨道线 ─────────────────────────────────────────
    df = df.with_columns([
        (1.11 * pl.col("ma10")).alias("ene_ue"),
        (0.91 * pl.col("ma10")).alias("ene_le"),
    ]).with_columns([
        ((pl.col("ene_ue") + pl.col("ene_le")) / 2.0).alias("ene"),
    ])

    # ── CR 连字符别名 ──────────────────────────────────────
    if "cr_ma1" in df.columns:
        df = df.with_columns([
            pl.col("cr_ma1").alias("cr-ma1"),
            pl.col("cr_ma2").alias("cr-ma2"),
            pl.col("cr_ma3").alias("cr-ma3"),
        ])

    # ── SAR + Supertrend (状态机, 按需) ────────────────────
    if "sar" in want:
        df = _numpy_stage(
            df, ["sar"], lambda a, n: {"sar": _sar(a["high"], a["low"])},
            ["high", "low"],
        )
    if want & {"supertrend", "supertrend_ub", "supertrend_lb"}:
        df = _numpy_stage(
            df,
            ["supertrend_ub", "supertrend_lb", "supertrend"],
            lambda a, n: dict(zip(
                ("supertrend_ub", "supertrend_lb", "supertrend"),
                _supertrend(a["close"], a["b_ub"], a["b_lb"]),
                strict=True,
            )),
            ["close", "b_ub", "b_lb"],
        )

    # ── 清理临时列 ─────────────────────────────────────────
    tmp = [c for c in df.columns if c.startswith("_")]
    df = df.drop(tmp)

    # ── 只保留需要的扩展列 ─────────────────────────────────
    keep = [c for c in df.columns if c not in EXTENDED_COLUMNS or c in want]
    df = df.select(keep)

    # ── NaN/Inf 处理 ───────────────────────────────────────
    present = [c for c in want if c in df.columns]
    if present:
        if fill_warmup_zero:
            df = df.with_columns([
                pl.when(
                    pl.col(c).is_null() | pl.col(c).is_nan() | pl.col(c).is_infinite()
                ).then(0.0).otherwise(pl.col(c)).alias(c)
                for c in present
            ])
        else:
            df = df.with_columns([
                pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite())
                .then(None).otherwise(pl.col(c)).alias(c)
                for c in present
            ])

    return df
