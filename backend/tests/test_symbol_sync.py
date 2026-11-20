"""单标的长时间 / 分钟K同步回归测试。

覆盖资产分流、窗口分片、失败隔离、写后刷新顺序和 LOF 读取链路。
不触网：所有数据源函数按模块 monkeypatch。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import index_sync, kline_sync, minute_shared_pull, symbol_sync
from app.tickflow.repository import DataStore, KlineRepository


class Repo:
    """记录刷新与缓存失效调用顺序的轻量 repo 桩。"""

    def __init__(self, tmp_path: Path):
        self.store = SimpleNamespace(data_dir=tmp_path)
        self.calls: list[str] = []

    def refresh_view(self, name: str, path: str) -> None:
        self.calls.append(f"refresh:{name}")

    def invalidate_enriched_asset(self, asset_type: str) -> None:
        self.calls.append(f"invalidate:{asset_type}")


@pytest.fixture()
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def test_normalize_symbol():
    assert symbol_sync.normalize_symbol("600000") == "600000.SH"
    assert symbol_sync.normalize_symbol("000001") == "000001.SZ"
    assert symbol_sync.normalize_symbol("430047") == "430047.BJ"
    assert symbol_sync.normalize_symbol("160216.sz") == "160216.SZ"
    with pytest.raises(ValueError):
        symbol_sync.normalize_symbol("")


def test_minute_window_slicing_and_truncation():
    windows, truncated = symbol_sync._minute_windows(
        "custom", date(2026, 1, 1), date(2026, 3, 1)
    )
    assert not truncated
    assert windows[0].start == date(2026, 1, 1)
    assert windows[-1].end == date(2026, 3, 1)
    assert all(w.days <= symbol_sync.MINUTE_WINDOW_DAYS for w in windows)

    long_windows, truncated = symbol_sync._minute_windows(
        "custom", date(2020, 1, 1), date(2026, 3, 1)
    )
    assert truncated
    assert len(long_windows) == symbol_sync.MAX_MINUTE_WINDOWS


def test_stock_custom_daily_dispatches_batch_sync(repo: Repo, monkeypatch):
    calls: list[dict] = []

    def fake_batch(symbols, repo_arg, capset, *, start_date, end_date, **kwargs):
        calls.append({
            "symbols": symbols,
            "start": start_date,
            "end": end_date,
            "zero_row_out": kwargs.get("zero_row_out"),
            "failed_out": kwargs.get("failed_out"),
        })
        return 3

    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", fake_batch)
    result = symbol_sync.run_symbol_sync(
        repo, SimpleNamespace(),
        symbol="600000", asset_type="stock", granularity="daily",
        range_mode="custom", start_date=date(2015, 1, 1),
        end_date=date(2020, 12, 31),
    )
    assert result["written_rows"] == 3
    assert result["asset_type"] == "stock"
    assert result["actual"] == {"start": "2015-01-01", "end": "2020-12-31"}
    assert calls[0]["symbols"] == ["600000.SH"]
    assert calls[0]["start"] == datetime(2015, 1, 1)
    assert calls[0]["end"] == datetime(2020, 12, 31, 23, 59, 59, 999999)
    assert isinstance(calls[0]["zero_row_out"], list)
    assert isinstance(calls[0]["failed_out"], list)
    assert repo.calls == ["refresh:kline_daily", "invalidate:stock"]


def test_non_stock_daily_dispatches_symbols_override(repo: Repo, monkeypatch):
    calls: dict[str, list] = {"etf": [], "index": [], "lof": []}

    def fake_etf(repo_arg, capset, *, start_date, end_date, symbols_override):
        calls["etf"].append((symbols_override, start_date.date(), end_date.date(), capset))
        return 2

    def fake_index(repo_arg, capset, *, start_date, end_date, symbols_override):
        calls["index"].append((symbols_override, start_date.date(), end_date.date(), capset))
        return 3

    def fake_lof(repo_arg, *, start_date, end_date, symbols_override):
        calls["lof"].append((symbols_override, start_date.date(), end_date.date()))
        return 4

    monkeypatch.setattr(index_sync, "sync_and_persist_etf_daily", fake_etf)
    monkeypatch.setattr(index_sync, "sync_and_persist_index_daily", fake_index)
    monkeypatch.setattr(index_sync, "sync_and_persist_lof_daily", fake_lof)

    common = dict(
        granularity="daily", range_mode="custom",
        start_date=date(2025, 1, 1), end_date=date(2025, 1, 31),
    )
    assert symbol_sync.run_symbol_sync(
        repo, "cap", symbol="510300", asset_type="etf", **common)["written_rows"] == 2
    assert symbol_sync.run_symbol_sync(
        repo, "cap", symbol="000001.SH", asset_type="index", **common)["written_rows"] == 3
    assert symbol_sync.run_symbol_sync(
        repo, "cap", symbol="160216", asset_type="lof", **common)["written_rows"] == 4

    assert calls["etf"][0][:3] == (["510300.SH"], date(2025, 1, 1), date(2025, 1, 31))
    assert calls["index"][0][:3] == (["000001.SH"], date(2025, 1, 1), date(2025, 1, 31))
    assert calls["lof"][0] == (["160216.SZ"], date(2025, 1, 1), date(2025, 1, 31))
    assert repo.calls[-3:] == [
        "refresh:kline_lof_daily", "refresh:kline_lof_enriched", "invalidate:lof",
    ]


def test_minute_dispatches_shared_history(repo: Repo, monkeypatch):
    calls: list[tuple[str, str, int]] = []

    def fake_history(repo_arg, capset, symbol, asset_type, days):
        calls.append((symbol, asset_type, days))
        return 5

    monkeypatch.setattr(minute_shared_pull, "sync_history", fake_history)
    result = symbol_sync.run_symbol_sync(
        repo, "cap", symbol="510300", asset_type="etf", granularity="minute",
        range_mode="custom", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
    )
    assert calls == [
        ("510300.SH", "etf", 1),
        ("510300.SH", "etf", 30),
    ]
    assert result["written_rows"] == 10
    assert result["minute_rows"] == 10
    assert repo.calls == ["refresh:kline_etf_minute", "invalidate:etf"]


def test_partial_window_failure_isolated(repo: Repo, monkeypatch):
    windows = [
        symbol_sync.Window(date(2026, 1, 1), date(2026, 1, 2)),
        symbol_sync.Window(date(2026, 1, 3), date(2026, 1, 5)),
    ]
    monkeypatch.setattr(symbol_sync, "_minute_windows", lambda *a, **kw: (windows, False))

    def fake_history(repo_arg, capset, symbol, asset_type, days):
        if days == 2:
            raise RuntimeError("source timeout")
        return 7

    monkeypatch.setattr(minute_shared_pull, "sync_history", fake_history)
    result = symbol_sync.run_symbol_sync(
        repo, "cap", symbol="000001", asset_type="stock", granularity="minute",
        range_mode="custom", start_date=date(2026, 1, 1), end_date=date(2026, 1, 4),
    )
    assert result["written_rows"] == 7
    assert result["truncated"] is False
    assert any("1 个窗口失败" in warning for warning in result["warnings"])


def test_repo_supports_lof_daily_asset(tmp_path: Path):
    """修复后的 get_daily_asset 必须能读到独立 LOF enriched 数据。"""
    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    raw = pl.DataFrame({
        "symbol": ["160216.SZ"],
        "date": [date(2026, 11, 18)],
        "open": [0.615], "high": [0.620], "low": [0.612], "close": [0.617],
        "volume": [30511], "amount": [1877879.096],
    })
    repo.append_lof_daily(raw)
    from app.indicators.pipeline import compute_enriched

    repo.append_lof_enriched(compute_enriched(raw, factors=None, instruments=None))
    repo.refresh_index_views()
    got = repo.get_daily_asset(
        "lof", "160216.SZ", date(2026, 11, 18), date(2026, 11, 18),
        columns=["symbol", "date", "close"],
    )
    assert not got.is_empty()
    assert got["symbol"][0] == "160216.SZ"
    assert got["close"][0] == pytest.approx(0.617)


def test_minute_rejects_index_and_lof(repo: Repo):
    with pytest.raises(ValueError, match="分钟K"):
        symbol_sync.run_symbol_sync(
            repo, SimpleNamespace(), symbol="000001.SH", asset_type="index",
            granularity="minute", range_mode="custom",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
    with pytest.raises(ValueError, match="分钟K"):
        symbol_sync.run_symbol_sync(
            repo, SimpleNamespace(), symbol="160216", asset_type="lof",
            granularity="minute", range_mode="custom",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
