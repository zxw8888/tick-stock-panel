"""LOF 数据同步单元测试 (fork 新增)。

覆盖 M0 探测结论、维表映射、日K单位口径、repository 存储与资产分流。
不依赖网络: 外部 API 一律 monkeypatch。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository, daily_dirname, enriched_dirname
from app.services import index_sync, lof_data


@pytest.fixture()
def repo(tmp_path: Path) -> KlineRepository:
    store = DataStore(tmp_path)
    return KlineRepository(store)


def test_dirname_mapping():
    assert daily_dirname("lof") == "kline_lof_daily"
    assert enriched_dirname("lof") == "kline_lof_enriched"


def test_lof_code_segment_filter():
    """16/501/502/505/506 段是 LOF; 508(REITs) 与 51/15(ETF) 必须排除。"""
    from app.services.lof_data import _LOF_PREFIXES, _symbol

    for code in ("160216", "161725", "501060", "502010", "505888", "506002"):
        assert code.startswith(_LOF_PREFIXES), code
        assert _symbol(code).endswith((".SH" if code[0] == "5" else ".SZ"))
    for code in ("508000", "510300", "159915"):
        assert not code.startswith(_LOF_PREFIXES), code


def test_eastmoney_daily_columns_and_volume_unit(monkeypatch):
    """东财 klines 列序: 日期,开,收,高,低,量(手),额(元); 不得把量当股再除 100。"""
    import httpx

    sample = {
        "data": {
            "klines": ["2026-11-18,0.615,0.617,0.620,0.612,30511,1877879.096,1.30"],
        }
    }

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return sample

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            return _Resp()

    # 雪球主源返回空 → 走东财分支
    monkeypatch.setattr(lof_data, "_xueqiu_daily", lambda *a, **kw: pl.DataFrame())
    monkeypatch.setattr(httpx, "Client", _Client)
    df = lof_data.fetch_daily(["160216.SZ"], start=date(2026, 11, 18), end=date(2026, 11, 18))
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["open"] == 0.615
    assert row["close"] == 0.617  # 第 3 列是收盘, 不是最高
    assert row["high"] == 0.620
    assert row["low"] == 0.612
    assert row["volume"] == 30511  # 已是手
    assert row["amount"] == 1877879.096


def test_xueqiu_daily_volume_share_to_hand(monkeypatch):
    """雪球日K volume 是股 → 出口必须 /100 折回手; 不得与东财口径混用。"""
    import httpx

    payload = {
        "data": {
            "symbol": "SZ160216",
            "column": ["timestamp", "volume", "open", "high", "low", "close",
                       "chg", "percent", "turnoverrate", "amount"],
            "item": [
                [1795363200000, 3649642, 0.539, 0.543, 0.537, 0.54, 0.003, 0.56, 2.12, 1969688.0],
            ],
        }
    }

    class _Resp:
        status_code = 200
        headers: dict = {}

        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def get(self, *a, **kw):
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _Client)
    df = lof_data._xueqiu_daily(["160216.SZ"], date(2026, 11, 1), date(2026, 11, 23))
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "160216.SZ"
    assert row["date"] == date(2026, 11, 23)
    assert row["close"] == 0.54
    assert row["volume"] == 36496  # 3649642 股 → 36496 手
    assert row["amount"] == 1969688.0


def test_daily_prefers_xueqiu_and_skips_eastmoney(monkeypatch):
    """雪球覆盖全部标的后不得再打东财; 东财通道被显式打桩为抛错。"""
    xq = pl.DataFrame({
        "symbol": ["160216.SZ"], "date": [date(2026, 11, 23)],
        "open": [0.539], "high": [0.543], "low": [0.537], "close": [0.54],
        "volume": [36496], "amount": [1969688.0],
    })
    monkeypatch.setattr(lof_data, "_xueqiu_daily", lambda *a, **kw: xq)

    def _boom(*a, **kw):
        raise AssertionError("雪球已覆盖, 不应调用东财")

    monkeypatch.setattr(lof_data, "_eastmoney_daily", _boom)
    df = lof_data.fetch_daily(["160216.SZ"], start=date(2026, 11, 1), end=date(2026, 11, 23))
    assert df.height == 1


def test_realtime_tencent_units_and_fallback(monkeypatch):
    """腾讯快照: volume 已是手, amount 万元→元, 百分比→小数; 且是 fuyao 之后的兜底。"""
    import httpx

    body = (
        'v_sz160216="1~国泰商品LOF~160216~0.540~0.537~0.539~36496~18248~18248~'
        '0.539~100~0.538~200~0.537~300~0.536~400~0.535~500~0.540~100~0.541~200~'
        '0.542~300~0.543~400~0.544~500~~20261123150000~0.003~0.56~0.543~0.537~'
        '0.540/36496/1969688~36496~197~2.12~~~0.543~0.537~1.11~19.70~";'
    )

    class _Resp:
        status_code = 200
        text = body
        encoding = "gbk"

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(lof_data, "_fuyo_key", lambda: "")
    monkeypatch.setattr(httpx, "Client", _Client)
    recs = lof_data.fetch_realtime(["160216.SZ"])
    assert len(recs) == 1
    r = recs[0]
    assert r["symbol"] == "160216.SZ"
    assert r["last_price"] == 0.54
    assert r["volume"] == 36496  # 手, 不再换算
    assert r["amount"] == 197 * 10_000.0  # 万元 → 元
    assert round(r["change_pct"], 4) == 0.0056  # 0.56% → 小数制


def test_repository_lof_roundtrip(repo, monkeypatch):
    """维表落盘 → resolve_asset_type → 日K/enriched → unified view 打标。"""
    import httpx

    inst = pl.DataFrame({
        "symbol": ["160216.SZ"], "name": ["国泰商品LOF"],
        "code": ["160216"], "asset_type": ["lof"],
    })
    repo.save_lof_instruments(inst)
    assert repo.resolve_asset_type("160216.SZ") == "lof"
    assert "160216.SZ" in repo.get_lof_symbol_set()

    daily = pl.DataFrame({
        "symbol": ["160216.SZ"], "date": [date(2026, 11, 18)],
        "open": [0.615], "high": [0.620], "low": [0.612], "close": [0.617],
        "volume": [30511], "amount": [1877879.096],
    })
    repo.append_lof_daily(daily)
    repo.refresh_index_views()  # 真实管道的 sync_and_persist_lof_daily 末尾即调用
    assert repo.db.execute("SELECT count(*) FROM kline_lof_daily").fetchone()[0] == 1

    # 直接验证 unified view 的 asset_type 打标(视图在无数据源时也能建)
    repo.refresh_index_views()
    row = repo.db.execute(
        "SELECT asset_type, close FROM kline_daily_all WHERE symbol = '160216.SZ'"
    ).fetchone()
    assert row == ("lof", 0.617)


def test_sync_and_persist_lof_daily_wires_everything(repo, monkeypatch):
    """端到端: d/fetch_daily 打桩 → 派表 + enriched 两个分区都落盘。"""
    fetched = pl.DataFrame({
        "symbol": ["501060.SH", "501060.SH"],
        "date": [date(2026, 11, 17), date(2026, 11, 18)],
        "open": [2.35, 2.37], "high": [2.37, 2.38], "low": [2.33, 2.36],
        "close": [2.36, 2.379], "volume": [1400, 2432], "amount": [331857.0, 577043.0],
    })
    monkeypatch.setattr(lof_data, "fetch_daily", lambda *a, **kw: fetched)
    inst = pl.DataFrame({
        "symbol": ["501060.SH"], "name": ["金选300A类LOF"],
        "code": ["501060"], "asset_type": ["lof"],
    })
    repo.save_lof_instruments(inst)
    n = index_sync.sync_and_persist_lof_daily(
        repo, start_date=datetime(2026, 11, 1), end_date=datetime(2026, 11, 18)
    )
    assert n == 2
    assert repo.db.execute("SELECT count(*) FROM kline_lof_daily").fetchone()[0] == 2
    assert repo.db.execute("SELECT count(*) FROM kline_lof_enriched").fetchone()[0] == 2


def test_fund_snapshot_mapping():
    """fuyao 场内基金快照 change_pct 百分数 → 小数制。"""
    rec = lof_data._map_fund_snapshot(
        {
            "thscode": "160216.SZ", "last_price": 0.566, "prev_price": 0.564,
            "price_change_ratio_pct": 0.35, "open_price": 0.565,
            "high_price": 0.568, "low_price": 0.564, "volume": 1918900,
            "turnover": 1086808.428, "price_change": 0.002,
        },
        1700000000000,
    )
    assert rec["symbol"] == "160216.SZ"
    assert rec["change_pct"] == pytest.approx(0.0035)
    assert rec["volume"] == 19189


def test_price_scale_etf_vs_lof():
    """东财兜底快照: LOF 3 位小数 /1000, ETF 2 位小数 /100。"""
    assert lof_data._price_scale("160216.SZ") == 1000.0
    assert lof_data._price_scale("501060.SH") == 1000.0
    assert lof_data._price_scale("510300.SH") == 100.0
    assert lof_data._price_scale("159915.SZ") == 100.0


def test_enriched_latest_asset_lof(repo):
    """LOF enriched 缓存可读 — watchlist /enriched LOF 分支的数据源。"""
    dt = date(2026, 11, 18)
    daily = pl.DataFrame({
        "symbol": ["160216.SZ"], "date": [dt],
        "open": [0.615], "high": [0.620], "low": [0.612], "close": [0.617],
        "volume": [30511], "amount": [1877879.096],
    })
    repo.append_lof_daily(daily)
    repo.append_lof_enriched(daily)
    # 默认 refresh=True 懒加载 — 与 watchlist /enriched 的调用方式一致
    df, cache_date = repo.get_enriched_latest_asset("lof")
    assert not df.is_empty()
    assert "160216.SZ" in df["symbol"].to_list()
    assert cache_date == dt


def test_active_daily_tables_covers_lof_without_zip_mismatch(monkeypatch):
    """新增 LOF 表后, 完整性检查开关不能再用长度不一致的 strict zip。"""
    from app.services import data_integrity, preferences

    monkeypatch.setattr(preferences, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(preferences, "get_realtime_pull_stock", lambda: False)
    monkeypatch.setattr(preferences, "get_pipeline_pull_etf", lambda: False)
    monkeypatch.setattr(preferences, "get_realtime_pull_etf", lambda: False)
    monkeypatch.setattr(preferences, "get_pipeline_pull_index", lambda: False)
    monkeypatch.setattr(preferences, "get_pipeline_pull_lof", lambda: True)
    monkeypatch.setattr(preferences, "get_realtime_pull_lof", lambda: False)

    assert data_integrity.active_daily_tables() == ("kline_daily", "kline_lof_daily")

    # 关掉盘后、只开实时 LOF 也必须生效
    monkeypatch.setattr(preferences, "get_pipeline_pull_lof", lambda: False)
    monkeypatch.setattr(preferences, "get_realtime_pull_lof", lambda: True)
    assert data_integrity.active_daily_tables() == ("kline_daily", "kline_lof_daily")
