"""资产类型常量 —— TSP fork 新增 LOF 时集中定义, 避免枚举散落多文件。

上游 TSP 只有 stock/index/etf 三个字符串字面量散落在 ~15 处; 本 fork 把
"全集 / 场内基金 / 支持日K的资产" 等集合收敛到此, 新增资产类型时先改这里,
再按 docs/lof-fork-patches.md 逐点接入。升级上游 rebase 时以本文件为锚。
"""
from __future__ import annotations

# 完整资产类型集合 (顺序与 UI 展示一致)
ASSET_TYPES = ("stock", "index", "etf", "lof")

# 场内基金: 价格精度 3 位小数, 无涨跌停/连板/股本换手逻辑
FUND_LIKE_ASSETS = frozenset({"etf", "lof"})

# 有独立 enriched 分区目录的资产
ENRICHED_ASSETS = frozenset({"stock", "index", "etf", "lof"})

# TickFlow 的 exchanges.get_instruments(instrument_type=...) 枚举映射。
# 实测 (2026-11-18): TickFlow 免费档 fund 返回 0 条, lof 直接 400;
# 故 LOF 维表不经过 TickFlow, 由 app.services.lof_data 提供。保留映射仅为
# 契约完整与未来上游支持时一行切换。
INSTRUMENT_TYPE_MAP = {
    "stock": "stock",
    "index": "index",
    "etf": "etf",
    "lof": "fund",  # 上游若支持 fund 则生效; 当前走 lof_data 专用通道
}


def is_fund_like(asset_type: str) -> bool:
    """是否场内基金口径 (3 位小数 / 无涨跌停)。"""
    return asset_type in FUND_LIKE_ASSETS


def to_instrument_type(asset_type: str) -> str:
    """asset_type → TickFlow instrument_type。"""
    return INSTRUMENT_TYPE_MAP.get(asset_type, asset_type)
