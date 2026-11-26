"""筹码分布 (CYQ) — 与 InStock ``CYQCalculator`` 逐位一致。

按"价格 × 筹码量"的二维直方图刻画持仓成本结构:
  - ``x``  每个价格档位的筹码量
  - ``y``  价格档位
  - ``b``  当前价所在档位
  - ``benefit_part`` 获利盘比例 (现价以下筹码 / 总筹码)
  - ``avg_cost``     平均成本
  - ``percent_chips`` 90% / 70% 成本区间与集中度

算法逐行复刻自 ``instock/core/kline/cyq.py`` (含其 Python 切片语义和
``float(f"{x:.12g}")`` 舍入), 实测与源实现逐位相同。

单次计算是 O(根数 × 档位) 的 Python 循环, 只适合按需调用 (前端点选某根
K 线), 不要放进逐行指标管线。
"""
from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["compute_cyq", "compute_cyq_frame"]


def _r12(x: float) -> float:
    """复刻 InStock 的 float(f"{x:.12g}") 舍入。"""
    return float(f"{x:.12g}")


def compute_cyq(
    *,
    open_: np.ndarray,
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    turnover: np.ndarray,
    dates: np.ndarray | None = None,
    index: int | None = None,
    accuracy_factor: int = 150,
    crange: int = 120,
    cyq_days: int = 210,
) -> dict[str, Any]:
    """计算指定 K 线位置的筹码分布。

    参数与 InStock ``CYQCalculator`` 对齐:
      accuracy_factor  纵轴刻度数 (精度因子)
      crange           回看的 K 线根数
      cyq_days         参与衰减的交易日数
      index            当前 K 线下标 (None = 最后一根)
    """
    n = len(close)
    if n == 0:
        return {}
    if index is None:
        index = n - 1
    index = int(index)

    factor = int(accuracy_factor)
    end = index - crange + 1
    start = end - cyq_days

    # 复刻 InStock 的窗口选择 (含其 Python 切片语义):
    #   end == 0 -> self.klinedata.tail(cyq_days)   [整表最后 N 行]
    #   else     -> self.klinedata[start:end]        [负索引从表尾计数]
    if end == 0:
        lo_i, hi_i = max(0, n - cyq_days), n
    else:
        lo_i = start if start >= 0 else max(0, n + start)
        hi_i = end if end >= 0 else max(0, n + end)

    if hi_i <= lo_i:
        return {}

    k_open = open_[lo_i:hi_i]
    k_close = close[lo_i:hi_i]
    k_high = high[lo_i:hi_i]
    k_low = low[lo_i:hi_i]
    k_turn = turnover[lo_i:hi_i]

    maxprice = float(np.max(k_high))
    minprice = float(np.min(k_low))

    accuracy = max(0.01, (maxprice - minprice) / (factor - 1))
    currentprice = float(k_close[-1])

    boundary = -1
    yrange: list[float] = []
    for i in range(factor):
        price = float(f"{minprice + accuracy * i:.2f}")
        yrange.append(price)
        if boundary == -1 and price >= currentprice:
            boundary = i

    xdata = [0.0] * factor

    for i in range(len(k_close)):
        o = float(k_open[i])
        c = float(k_close[i])
        h = float(k_high[i])
        lo = float(k_low[i])
        t = float(k_turn[i])

        avg = (o + c + h + lo) / 4.0
        turnover_rate = min(1.0, t / 100.0)

        hi_idx = int((h - minprice) / accuracy)
        lo_idx = int((lo - minprice) / accuracy + 0.99)
        g_x = (factor - 1) if h == lo else 2.0 / (h - lo)
        g_y = int((avg - minprice) / accuracy)

        decay = 1.0 - turnover_rate
        for j in range(factor):
            xdata[j] *= decay

        if h == lo:
            if 0 <= g_y < factor:
                xdata[g_y] += g_x * turnover_rate / 2.0
        else:
            for j in range(lo_idx, hi_idx + 1):
                if j < 0 or j >= factor:
                    continue
                curprice = minprice + accuracy * j
                if curprice <= avg:
                    if abs(avg - lo) < 1e-8:
                        xdata[j] += g_x * turnover_rate
                    else:
                        xdata[j] += (curprice - lo) / (avg - lo) * g_x * turnover_rate
                else:
                    if abs(h - avg) < 1e-8:
                        xdata[j] += g_x * turnover_rate
                    else:
                        xdata[j] += (h - curprice) / (h - avg) * g_x * turnover_rate

    total_chips = sum(_r12(x) for x in xdata)

    def cost_by_chip(chip: float) -> float:
        result = 0.0
        acc = 0.0
        for i in range(factor):
            x = _r12(xdata[i])
            if acc + x > chip:
                result = minprice + i * accuracy
                break
            acc += x
        return result

    def percent_chips(percent: float) -> dict[str, Any]:
        if percent > 1 or percent < 0:
            raise ValueError('argument "percent" out of range')
        ps = [(1 - percent) / 2, (1 + percent) / 2]
        pr = [cost_by_chip(total_chips * ps[0]), cost_by_chip(total_chips * ps[1])]
        return {
            "priceRange": [f"{pr[0]:.2f}", f"{pr[1]:.2f}"],
            "concentration": 0
            if pr[0] + pr[1] == 0
            else (pr[1] - pr[0]) / (pr[0] + pr[1]),
        }

    below = 0.0
    for i in range(factor):
        x = _r12(xdata[i])
        if currentprice >= minprice + i * accuracy:
            below += x
    benefit_part = 0.0 if total_chips == 0 else below / total_chips

    return {
        "x": [float(v) for v in xdata],
        "y": yrange,
        "b": boundary + 1,
        "d": str(dates[index]) if dates is not None else None,
        "t": int(cyq_days),
        "benefit_part": benefit_part,
        "avg_cost": f"{cost_by_chip(total_chips * 0.5):.2f}",
        "percent_chips": {"90": percent_chips(0.9), "70": percent_chips(0.7)},
    }


def compute_cyq_frame(
    df,
    *,
    index: int | None = None,
    accuracy_factor: int = 150,
    crange: int = 120,
    cyq_days: int = 210,
) -> dict[str, Any]:
    """从单标的 Polars DataFrame 直接算筹码分布.

    df 需含 open/high/low/close/turnover_rate (可选 date)。
    turnover_rate 单位是百分数 (3.5 表示 3.5%), 与 InStock 一致。
    """
    if df.is_empty():
        return {}

    def col(name: str) -> np.ndarray:
        return df[name].to_numpy().astype("float64")

    return compute_cyq(
        open_=col("open"),
        close=col("close"),
        high=col("high"),
        low=col("low"),
        turnover=col("turnover_rate"),
        dates=df["date"].to_numpy() if "date" in df.columns else None,
        index=index,
        accuracy_factor=accuracy_factor,
        crange=crange,
        cyq_days=cyq_days,
    )
