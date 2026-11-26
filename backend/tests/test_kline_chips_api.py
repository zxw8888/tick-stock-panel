"""筹码分布接口测试 (/api/kline/chips)."""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kline import router


def _frame(rows: int = 400, turnover: bool = True) -> pl.DataFrame:
    base = date.today() - timedelta(days=rows - 1)
    dates = [base + timedelta(days=i) for i in range(rows)]
    close = [10.0 + (i % 20) * 0.1 for i in range(rows)]
    data = {
        "symbol": ["600000.SH"] * rows,
        "date": dates,
        "open": [c - 0.05 for c in close],
        "high": [c + 0.15 for c in close],
        "low": [c - 0.15 for c in close],
        "close": close,
        "volume": [1_000_000.0 + i * 1000 for i in range(rows)],
    }
    if turnover:
        data["turnover_rate"] = [1.5 + (i % 5) * 0.2 for i in range(rows)]
    return pl.DataFrame(data)


class _FakeRepo:
    def __init__(self, frame: pl.DataFrame | None = None, asset_type: str = "stock") -> None:
        self._frame = frame if frame is not None else _frame()
        self._asset_type = asset_type
        self.calls: list[tuple[str, str, date, date, list[str] | None]] = []

    def resolve_asset_type(self, symbol: str) -> str:
        return self._asset_type

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        self.calls.append((asset_type, symbol, start, end, columns))
        if self._frame.is_empty():
            return self._frame
        return self._frame.filter(
            (pl.col("date") >= start) & (pl.col("date") <= end)
        ).sort("date")


def _client(frame: pl.DataFrame | None = None, asset_type: str = "stock") -> tuple[TestClient, _FakeRepo]:
    repo = _FakeRepo(frame, asset_type)
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    return TestClient(app), repo


def test_chips_returns_distribution_for_stock():
    client, repo = _client()
    resp = client.get("/api/kline/chips", params={"symbol": "600000.SH"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["turnover_source"] == "turnover_rate"
    chips = body["chips"]
    assert len(chips["x"]) == len(chips["y"]) == 150
    assert chips["t"] == 210
    assert set(chips["percent_chips"]) == {"90", "70"}
    # 默认截止日 = 今天; 窗口按 (cyq_days + crange) * 1.5 + 60 日历日回看
    assert repo.calls[0][0] == "stock"
    assert "turnover_rate" in (repo.calls[0][4] or [])


def test_chips_falls_back_to_volume_proxy_without_turnover():
    client, _ = _client(_frame(turnover=False), asset_type="etf")
    resp = client.get("/api/kline/chips", params={"symbol": "510300.SH"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["turnover_source"] == "volume_proxy"
    assert body["chips"]["x"]


def test_chips_accepts_explicit_date_and_truncates_frame():
    frame = _frame()
    target = frame["date"][200]
    client, _ = _client(frame)
    resp = client.get("/api/kline/chips", params={"symbol": "600000.SH", "date": target.isoformat()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == target.isoformat()
    assert body["chips"]["d"] == str(target)


def test_chips_unknown_date_returns_empty_payload():
    client, _ = _client()
    resp = client.get("/api/kline/chips", params={"symbol": "600000.SH", "date": "1990-01-01"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["chips"] == {}
    assert "detail" in body


def test_chips_missing_data_returns_empty():
    client, _ = _client(pl.DataFrame())
    resp = client.get("/api/kline/chips", params={"symbol": "600000.SH"})
    assert resp.status_code == 200
    assert resp.json()["chips"] == {}


def test_chips_rejects_reversed_range():
    client, _ = _client()
    resp = client.get(
        "/api/kline/chips",
        params={"symbol": "600000.SH", "start_date": "2026-02-20", "end_date": "2026-01-20"},
    )
    assert resp.status_code == 400


def test_chips_rejects_bad_date_format():
    client, _ = _client()
    resp = client.get("/api/kline/chips", params={"symbol": "600000.SH", "date": "2026/01/20"})
    assert resp.status_code == 400


def test_chips_rejects_bad_start_date_format():
    client, _ = _client()
    resp = client.get("/api/kline/chips", params={"symbol": "600000.SH", "start_date": "2026/01/20"})
    assert resp.status_code == 400
    assert "start_date" in resp.json()["detail"]


def test_chips_rejects_bad_end_date_format():
    client, _ = _client()
    resp = client.get("/api/kline/chips", params={"symbol": "600000.SH", "end_date": "2026-13-40"})
    assert resp.status_code == 400
    assert "end_date" in resp.json()["detail"]
