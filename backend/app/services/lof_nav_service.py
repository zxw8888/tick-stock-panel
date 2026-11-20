"""LOF 净值(NAV)同步 —— 走官方扩展数据通道, 不进内置行情/enriched 链路。

数据源: fuyao /api/fund/performance/nav (需 FUYAO_API_KEY)。
落库复用 app.services.ext_data.write_ext_parquet:
  data/ext_data/fund_nav/timeseries/date=<净值日>/part.parquet
前端扩展数据页自动列出 fund_nav 表; 可作为自选列/策略条件(fund_nav.unit_nav 等)。

净值 T+1 披露: 每晚 20:30 抓取, 当日分区通常为空, 次日回补前一日净值。
调用方负责幂等 —— write_ext_parquet 按 (symbol, nav_date) merge-upsert。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

TABLE_ID = "fund_nav"
TABLE_LABEL = "LOF基金净值(NAV)"
DASHBOARD_FIELDS = [
    ("symbol", "string", "LOF代码"),
    ("nav_date", "string", "净值日期"),
    ("unit_nav", "float", "单位净值"),
    ("adj_nav", "float", "复权净值"),
]


def ensure_table(data_dir: Path) -> None:
    """确保 fund_nav 扩展表配置存在(幂等)。已存在时不覆盖用户改动。"""
    from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField

    store = ExtConfigStore(data_dir)
    if store.get(TABLE_ID) is not None:
        return
    cfg = ExtConfig(
        id=TABLE_ID,
        label=TABLE_LABEL,
        mode="timeseries",
        fields=[ExtField(name=n, dtype=t, label=l) for n, t, l in DASHBOARD_FIELDS],
        description="fuyao 单位净值/复权净值; T+1 披露, 每晚回补。折溢价率可由 close/unit_nav-1 派生。",
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
    )
    store.create(cfg)
    logger.info("扩展表 %s 已创建", TABLE_ID)


def sync_nav(data_dir: Path, symbols: list[str] | None = None, *, range_: str = "fyear") -> int:
    """拉取 NAV 并按净值日写入 fund_nav 扩展表。返回写入行数。

    symbols=None 时取 LOF 维表全量; 显式传入时只拉指定标的。
    """
    from app.services import ext_data, lof_data

    ensure_table(data_dir)
    store = ext_data.ExtConfigStore(data_dir)
    cfg = store.get(TABLE_ID)
    if cfg is None:
        logger.warning("扩展表 %s 配置缺失, 跳过 NAV 同步", TABLE_ID)
        return 0

    if symbols is None:
        from app.tickflow.repository import DataStore

        repo_data = DataStore(data_dir)
        from app.tickflow.repository import KlineRepository

        inst = KlineRepository(repo_data).get_lof_instruments()
        symbols = inst["symbol"].to_list() if not inst.is_empty() and "symbol" in inst.columns else []
    if not symbols:
        logger.warning("LOF 标的为空, 跳过 NAV 同步")
        return 0

    df = lof_data.fetch_nav(symbols, range_=range_)
    if df.is_empty():
        logger.warning("fuyao NAV 返回为空(可能未配置 Key), 跳过写入")
        return 0

    total = 0
    for nav_day, group in df.group_by("nav_date"):
        day = nav_day[0] if isinstance(nav_day, tuple) else nav_day
        if isinstance(day, str):
            day = date.fromisoformat(day[:10])
        written = ext_data.write_ext_parquet(group, cfg, data_dir, snapshot_date=day)
        total += written
    logger.info("LOF NAV 同步完成: %d 行 / %d 净值日", total, df["nav_date"].n_unique())
    return total
