"""K 线形态标注接口测试 (/api/kline/patterns*)."""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kline import router
from app.indicators.patterns import PATTERN_COLUMNS, PATTERN_LABELS


def _frame(rows: int = 40) -> pl.DataFrame:
    """构造一段带明确锤头/吞噬形态的日线: 前段横盘, 末段长下影 + 阳吞阴。"""
    base = date(2026, 1, 5)
    dates = [base + timedelta(days=i) for i in range(rows)]
    opens, highs, lows, closes = [], [], [], []
    for _ in range(rows):
        # 横盘: 实体很小, 上下影线对称
        opens.append(10.0)
        highs.append(10.05)
        lows.append(9.95)
        closes.append(10.0)
    # 倒数第二根: 阴线; 最后一根: 长下影小实体 (锤头) + 阳吞阴
    opens[-2], closes[-2], highs[-2], lows[-2] = 10.2, 10.0, 10.25, 9.95
    opens[-1], closes[-1], highs[-1], lows[-1] = 9.98, 10.1, 10.12, 9.4
    return pl.DataFrame({
        "symbol": ["600000.SH"] * rows,
        "date": dates,
        "open": opens, "high": highs, "low": lows, "close": closes,
    })


class _FakeRepo:
    def __init__(self, frame: pl.DataFrame | None = None) -> None:
        self._frame = frame if frame is not None else _frame()
        self.calls: list[tuple[str, str, date, date, list[str] | None]] = []

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        self.calls.append((asset_type, symbol, start, end, columns))
        if self._frame.is_empty():
            return self._frame
        return self._frame.filter(
            (pl.col("date") >= start) & (pl.col("date") <= end)
        ).sort("date")


def _client(frame: pl.DataFrame | None = None) -> tuple[TestClient, _FakeRepo]:
    repo = _FakeRepo(frame)
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    return TestClient(app), repo


def test_meta_exposes_all_61_patterns_with_labels():
    client, _ = _client()
    resp = client.get("/api/kline/patterns/meta")
    assert resp.status_code == 200
    payload = resp.json()["patterns"]
    assert [item["key"] for item in payload] == list(PATTERN_COLUMNS)
    assert len(payload) == 61
    assert {item["key"]: item["label"] for item in payload} == PATTERN_LABELS


def test_patterns_endpoint_returns_only_non_zero_hits():
    client, repo = _client()
    resp = client.get(
        "/api/kline/patterns",
        params={"symbol": "600000.SH", "start_date": "2026-01-20", "end_date": "2026-02-20"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "600000.SH"
    assert body["asset_type"] == "stock"
    assert len(body["patterns"]) == 61
    assert body["rows"], "should hit at least one pattern on the synthetic tail"
    for row in body["rows"]:
        assert row["date"] >= "2026-01-20"
        assert row["date"] <= "2026-02-20"
        assert any(value for key, value in row.items() if key != "date")
    # 预热窗口: 起始日往前多读 45 个日历日
    _, _, start, _, columns = repo.calls[0]
    assert start == date(2026, 1, 20) - timedelta(days=45)
    assert columns == ["symbol", "date", "open", "high", "low", "close"]


def test_patterns_endpoint_prunes_to_requested_columns():
    client, _ = _client()
    resp = client.get(
        "/api/kline/patterns",
        params={
            "symbol": "600000.SH",
            "start_date": "2026-01-20",
            "end_date": "2026-02-20",
            "patterns": "hammer,engulfing_pattern",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [item["key"] for item in body["patterns"]] == ["engulfing_pattern", "hammer"]
    for row in body["rows"]:
        assert set(row) <= {"date", "hammer", "engulfing_pattern"}


def test_patterns_endpoint_rejects_unknown_columns():
    client, _ = _client()
    resp = client.get(
        "/api/kline/patterns",
        params={
            "symbol": "600000.SH",
            "start_date": "2026-01-20",
            "end_date": "2026-02-20",
            "patterns": "not_a_pattern",
        },
    )
    assert resp.status_code == 400
    assert "not_a_pattern" in resp.json()["detail"]


def test_patterns_endpoint_rejects_reversed_range():
    client, _ = _client()
    resp = client.get(
        "/api/kline/patterns",
        params={"symbol": "600000.SH", "start_date": "2026-02-20", "end_date": "2026-01-20"},
    )
    assert resp.status_code == 400


def test_patterns_endpoint_rejects_bad_date_format():
    client, _ = _client()
    resp = client.get(
        "/api/kline/patterns",
        params={"symbol": "600000.SH", "start_date": "2026/01/20", "end_date": "2026-02-20"},
    )
    assert resp.status_code == 400
    assert "start_date" in resp.json()["detail"]


def test_patterns_endpoint_tolerates_missing_data():
    client, _ = _client(pl.DataFrame())
    resp = client.get(
        "/api/kline/patterns",
        params={"symbol": "600000.SH", "start_date": "2026-01-20", "end_date": "2026-02-20"},
    )
    assert resp.status_code == 200
    assert resp.json()["rows"] == []
