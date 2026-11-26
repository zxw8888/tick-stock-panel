"""标的维表同步: 单个交易所拉取失败不能抹掉该交易所的存量标的。

回归背景 (2026-11-23): 盘前 9:10 同步时 tickflow SZ/BJ 接口返回 503,
旧实现只写成功拉到的 SH+BJ 行, 整表覆盖后深市 3000+ 只股票 (含 000001
平安银行、000651 格力电器) 从 instruments.parquet 消失, 自选搜索搜不到。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.services import instrument_sync as isync
from app.tickflow import client as tf_client

_FIELDS = [
    "symbol", "name", "code", "exchange", "region", "type",
    "listing_date", "total_shares", "float_shares", "tick_size",
    "limit_up", "limit_down",
]


def _item(symbol: str, name: str) -> dict:
    code, ex = symbol.split(".")
    return {
        "symbol": symbol, "name": name, "code": code,
        "exchange": ex, "region": "CN", "type": "stock",
        "ext": {
            "listing_date": "2000-01-01", "total_shares": 1e9,
            "float_shares": 9e8, "tick_size": 0.01,
        },
    }


def _write_existing(tmp_path: Path, symbols: list[tuple[str, str]]) -> None:
    rows = []
    for symbol, name in symbols:
        row = _item(symbol, name)
        row.update({k: row["ext"].get(k) for k in _FIELDS if k in row["ext"]})
        row.pop("ext")
        rows.append(row)
    out = tmp_path / "instruments"
    out.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).with_columns(pl.lit("2026-11-22").alias("as_of")).write_parquet(
        out / "instruments.parquet"
    )


def _stub_tf(monkeypatch, by_exchange: dict[str, object]) -> None:
    """by_exchange 的值: 行列表 / 异常实例。"""
    class _Exchanges:
        def get_instruments(self, ex: str, instrument_type: str = "stock"):
            value = by_exchange[ex]
            if isinstance(value, Exception):
                raise value
            return value

    monkeypatch.setattr(isync, "_fetch_instruments_via_provider", lambda: None)
    monkeypatch.setattr(
        isync, "get_client",
        lambda: type("Client", (), {"exchanges": _Exchanges()})(),
    )
    monkeypatch.setattr(tf_client, "current_mode", lambda: "free")


def test_failed_exchange_keeps_existing_rows(tmp_path, monkeypatch):
    _write_existing(tmp_path, [
        ("600000.SH", "浦发银行"), ("000001.SZ", "平安银行"), ("920001.BJ", "北交甲"),
    ])
    _stub_tf(monkeypatch, {
        "SH": [_item("600000.SH", "浦发银行")],
        "SZ": RuntimeError("503 Service Unavailable"),
        "BJ": [_item("920001.BJ", "北交甲")],
    })

    assert isync.sync_instruments(tmp_path) == 3
    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    by_symbol = {r["symbol"]: r for r in df.to_dicts()}
    assert by_symbol["000001.SZ"]["name"] == "平安银行"
    assert by_symbol["000001.SZ"]["as_of"].isoformat() == cn_today().isoformat()


def test_empty_exchange_response_keeps_existing_rows(tmp_path, monkeypatch):
    _write_existing(tmp_path, [("600000.SH", "浦发银行"), ("000651.SZ", "格力电器")])
    _stub_tf(monkeypatch, {
        "SH": [_item("600000.SH", "浦发银行")],
        "SZ": [],
        "BJ": [],
    })

    assert isync.sync_instruments(tmp_path) == 2
    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert df["symbol"].to_list() == ["600000.SH", "000651.SZ"]


def test_truncated_exchange_payload_keeps_existing_rows(tmp_path, monkeypatch):
    """接口 200 但只回少量行时也不能把存量交易所整批覆盖掉。"""
    _write_existing(tmp_path, [
        ("600000.SH", "浦发银行"),
        ("000001.SZ", "平安银行"), ("000651.SZ", "格力电器"),
        ("000333.SZ", "美的集团"), ("002594.SZ", "比亚迪"),
    ])
    _stub_tf(monkeypatch, {
        "SH": [_item("600000.SH", "浦发银行")],
        "SZ": [_item("000001.SZ", "平安银行")],  # 1/4, 明显不完整
        "BJ": [],
    })

    assert isync.sync_instruments(tmp_path) == 5
    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert set(df["symbol"].to_list()) == {
        "600000.SH", "000001.SZ", "000651.SZ", "000333.SZ", "002594.SZ",
    }


def test_all_exchanges_failed_leaves_table_untouched(tmp_path, monkeypatch):
    _write_existing(tmp_path, [("600000.SH", "浦发银行"), ("000001.SZ", "平安银行")])
    path = tmp_path / "instruments" / "instruments.parquet"
    before = path.read_bytes()
    _stub_tf(monkeypatch, {
        "SH": RuntimeError("503"), "SZ": RuntimeError("503"), "BJ": RuntimeError("503"),
    })

    assert isync.sync_instruments(tmp_path) == 0
    assert path.read_bytes() == before
