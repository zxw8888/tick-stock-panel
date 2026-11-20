"""单标的长时间 / 分钟K同步编排。

只做分流、分窗、失败隔离与写后刷新；取数/落盘继续复用 kline_sync /
index_sync / minute_shared_pull，避免复制数据源和单位口径。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Callable

from app.asset_types import ASSET_TYPES
from app.market_time import cn_today

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^(\d{6})(?:\.(SZ|SH|BJ))?$")

# 全量回溯的安全停止线：只是防失控，不代表数据源一定有那么早的历史。
FULL_FLOOR = {
    "stock": date(1990, 12, 19),
    "index": date(1990, 12, 19),
    "etf": date(2004, 1, 1),
    "lof": date(1998, 1, 1),
}
DAILY_WINDOW_DAYS = {"stock": 8 * 365, "index": 365, "etf": 365, "lof": 365}
MAX_DAILY_WINDOWS = {"stock": 8, "index": 30, "etf": 30, "lof": 30}
MINUTE_WINDOW_DAYS = 30
MAX_MINUTE_WINDOWS = 12

VIEW_BY_ASSET = {
    "stock": ("kline_daily",),
    "etf": ("kline_etf_daily", "kline_etf_enriched", "kline_etf_minute"),
    "index": ("kline_index_daily", "kline_index_enriched"),
    "lof": ("kline_lof_daily", "kline_lof_enriched"),
}
MINUTE_VIEW_BY_ASSET = {"stock": ("kline_minute",), "etf": ("kline_etf_minute",)}


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def normalize_symbol(symbol: str) -> str:
    """接受 000001.SZ / 000001，统一补全交易所后缀。"""
    raw = (symbol or "").strip().upper()
    m = _SYMBOL_RE.match(raw)
    if not m:
        raise ValueError("标的代码格式不合法")
    code, exchange = m.groups()
    if exchange:
        return f"{code}.{exchange}"
    if code[0] in {"5", "6", "9"}:
        return f"{code}.SH"
    if code[0] in {"4", "8"}:
        return f"{code}.BJ"
    return f"{code}.SZ"


def _noop(stage: str, pct: int, msg: str, stage_pct: int | None = None,
          skip_log: bool = False) -> None:
    return None


def _dt_start(day: date) -> datetime:
    return datetime.combine(day, dt_time.min)


def _dt_end(day: date) -> datetime:
    return datetime.combine(day, dt_time.max)


def _daily_windows(asset_type: str, range_mode: str,
                   start_date: date | None, end_date: date | None) -> tuple[list[Window], bool]:
    if range_mode == "custom":
        assert start_date is not None and end_date is not None
        cursor_end = end_date
        windows: list[Window] = []
        while cursor_end >= start_date and len(windows) < MAX_DAILY_WINDOWS[asset_type]:
            span = DAILY_WINDOW_DAYS[asset_type]
            cursor_start = max(start_date, cursor_end - timedelta(days=span - 1))
            windows.append(Window(cursor_start, cursor_end))
            cursor_end = cursor_start - timedelta(days=1)
        return list(reversed(windows)), cursor_end >= start_date

    floor = FULL_FLOOR[asset_type]
    cursor_end = cn_today()
    windows = []
    while cursor_end >= floor and len(windows) < MAX_DAILY_WINDOWS[asset_type]:
        span = DAILY_WINDOW_DAYS[asset_type]
        cursor_start = max(floor, cursor_end - timedelta(days=span - 1))
        windows.append(Window(cursor_start, cursor_end))
        cursor_end = cursor_start - timedelta(days=1)
    return list(reversed(windows)), cursor_end >= floor


def _minute_windows(range_mode: str, start_date: date | None,
                    end_date: date | None) -> tuple[list[Window], bool]:
    if range_mode == "custom":
        assert start_date is not None and end_date is not None
        cursor_end = end_date
        windows: list[Window] = []
        while cursor_end >= start_date and len(windows) < MAX_MINUTE_WINDOWS:
            cursor_start = max(start_date, cursor_end - timedelta(days=MINUTE_WINDOW_DAYS - 1))
            windows.append(Window(cursor_start, cursor_end))
            cursor_end = cursor_start - timedelta(days=1)
        return list(reversed(windows)), cursor_end >= start_date

    cursor_end = cn_today()
    floor = cursor_end - timedelta(days=MINUTE_WINDOW_DAYS * MAX_MINUTE_WINDOWS - 1)
    windows = []
    while cursor_end >= floor and len(windows) < MAX_MINUTE_WINDOWS:
        cursor_start = max(floor, cursor_end - timedelta(days=MINUTE_WINDOW_DAYS - 1))
        windows.append(Window(cursor_start, cursor_end))
        cursor_end = cursor_start - timedelta(days=1)
    return list(reversed(windows)), False


def _refresh_views(repo, names: tuple[str, ...]) -> list[str]:
    """只刷新本次实际写入的视图，路径与 daily_pipeline._refresh_single_view 保持一致。"""
    root = repo.store.data_dir.as_posix()
    paths = {
        "kline_daily": f"{root}/kline_daily/**/*.parquet",
        "kline_enriched": f"{root}/kline_daily_enriched/**/*.parquet",
        "kline_index_daily": f"{root}/kline_index_daily/**/*.parquet",
        "kline_index_enriched": f"{root}/kline_index_enriched/**/*.parquet",
        "kline_etf_daily": f"{root}/kline_etf_daily/**/*.parquet",
        "kline_etf_enriched": f"{root}/kline_etf_enriched/**/*.parquet",
        "kline_lof_daily": f"{root}/kline_lof_daily/**/*.parquet",
        "kline_lof_enriched": f"{root}/kline_lof_enriched/**/*.parquet",
        "kline_minute": f"{root}/kline_minute/**/*.parquet",
        "kline_etf_minute": f"{root}/kline_etf_minute/**/*.parquet",
    }
    done: list[str] = []
    for name in names:
        path = paths.get(name)
        if path:
            repo.refresh_view(name, path)
            done.append(name)
    return done


def run_symbol_sync(
    repo, capset, *,
    symbol: str,
    asset_type: str,
    granularity: str,
    range_mode: str,
    start_date: date | None,
    end_date: date | None,
    on_progress: Callable[..., None] | None = None,
) -> dict:
    """按 (资产, 粒度, 范围) 拉取并 merge-upsert 单个标的的数据。"""
    emit = on_progress or _noop
    if asset_type not in ASSET_TYPES:
        raise ValueError("不支持的资产类型")
    if granularity not in {"daily", "minute"}:
        raise ValueError("不支持的数据粒度")
    if range_mode not in {"custom", "full"}:
        raise ValueError("不支持的范围模式")
    if granularity == "minute" and asset_type not in {"stock", "etf"}:
        raise ValueError("指数/LOF 不支持分钟K落库")
    if range_mode == "custom" and (start_date is None or end_date is None):
        raise ValueError("自定义范围必须提供起止日期")

    symbol = normalize_symbol(symbol)
    emit("symbol_sync_prepare", 2, f"准备同步 {symbol} …")

    if granularity == "daily":
        windows, truncated = _daily_windows(asset_type, range_mode, start_date, end_date)
        views = VIEW_BY_ASSET[asset_type]
        if asset_type == "stock":
            views = ("kline_daily",)
    else:
        windows, truncated = _minute_windows(range_mode, start_date, end_date)
        views = MINUTE_VIEW_BY_ASSET[asset_type]

    from app.services import index_sync, kline_sync, minute_shared_pull

    failures: list[str] = []
    warnings: list[str] = []
    written = 0
    actual_start: date | None = None
    actual_end: date | None = None

    for idx, window in enumerate(windows):
        pct = 10 + int(80 * idx / max(len(windows), 1))
        emit("symbol_sync_fetch", pct, f"拉取窗口 {idx + 1}/{len(windows)}: "
             f"{window.start} ~ {window.end}")
        try:
            if granularity == "daily":
                if asset_type == "stock":
                    failed_out: list[str] = []
                    zero_out: list[str] = []
                    n = kline_sync.sync_and_persist_daily_batch(
                        [symbol], repo, capset,
                        start_date=_dt_start(window.start), end_date=_dt_end(window.end),
                        zero_row_out=zero_out, failed_out=failed_out,
                    )
                    if failed_out:
                        failures.append(f"{window.start}: 拉取失败 {failed_out[:3]}")
                    if zero_out and not failed_out:
                        warnings.append(f"{window.start}: 数据源未返回该日K")
                else:
                    fn = {
                        "etf": index_sync.sync_and_persist_etf_daily,
                        "index": index_sync.sync_and_persist_index_daily,
                        "lof": index_sync.sync_and_persist_lof_daily,
                    }[asset_type]
                    kwargs = dict(start_date=_dt_start(window.start),
                                  end_date=_dt_end(window.end),
                                  symbols_override=[symbol])
                    if asset_type != "lof":
                        kwargs["capset"] = capset
                    n = fn(repo, **kwargs)
            else:
                n = minute_shared_pull.sync_history(
                    repo, capset, symbol, asset_type, window.days)

            written += int(n or 0)
            if n:
                actual_start = window.start if actual_start is None else min(actual_start, window.start)
                actual_end = window.end if actual_end is None else max(actual_end, window.end)
            else:
                warnings.append(f"{window.start}: 数据源未返回数据")
        except Exception as exc:  # noqa: BLE001 - 单窗失败隔离
            failures.append(f"{window.start}: {exc}")
            logger.warning("symbol sync window failed: %s %s", symbol, window, exc_info=True)

    if failures and written == 0:
        raise RuntimeError("全部窗口拉取失败：" + "; ".join(failures[:3]))
    if failures:
        warnings.append(f"{len(failures)} 个窗口失败，其余窗口已写入；失败明细见日志")
    if truncated:
        warnings.append("已达到安全窗口上限，结果可能未覆盖完整历史")

    emit("symbol_sync_refresh", 96, "刷新视图与缓存…")
    refreshed = _refresh_views(repo, views)
    repo.invalidate_enriched_asset(asset_type)

    result = {
        "symbol": symbol,
        "asset_type": asset_type,
        "granularity": granularity,
        "range_mode": range_mode,
        "requested": {
            "start": start_date.isoformat() if start_date else None,
            "end": end_date.isoformat() if end_date else None,
        },
        "actual": {
            "start": actual_start.isoformat() if actual_start else None,
            "end": actual_end.isoformat() if actual_end else None,
        },
        "written_rows": written,
        "windows_fetched": len(windows),
        "truncated": truncated,
        "warnings": warnings,
        "refreshed_views": refreshed,
    }
    if granularity == "minute":
        result["minute_rows"] = written
    emit("done", 100, f"写入 {written} 行")
    return result
