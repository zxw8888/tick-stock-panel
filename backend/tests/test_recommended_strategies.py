"""推荐策略包 — 清单、安装状态、API 与矩阵执行回归。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy_recommended as recommended_api
from app.api.strategy import _strategy_detail
from app.services import recommended_strategies as rs
from app.strategy.engine import StrategyEngine


def _installed_engine(data_dir):
    return StrategyEngine(strategy_dirs=[data_dir / "strategies" / "custom"])


def test_manifest_is_complete_and_loadable(tmp_path):
    manifest = rs.load_manifest()
    assert manifest["pack_version"] == "2026.11.1"
    assert len(manifest["strategies"]) == 10
    assert len({item["id"] for item in manifest["strategies"]}) == 10
    for item in manifest["strategies"]:
        assert (rs.pack_dir() / item["file"]).is_file()
        assert item["suitable_for"]["note"]

    result = rs.auto_install_recommended(tmp_path)
    assert len(result["installed_ids"]) == 10
    engine = _installed_engine(tmp_path)
    assert not engine.load_errors(), engine.load_errors()
    assert len(engine.list_strategies()) == 10
    for item in manifest["strategies"]:
        strategy = engine.get(item["id"])
        assert strategy.meta["origin"] == "recommended"
        assert strategy.meta["suitable_for"] == item["suitable_for"]
        assert strategy.source == "custom"


def test_auto_install_is_idempotent_and_never_resurrects(tmp_path):
    assert len(rs.auto_install_recommended(tmp_path)["installed_ids"]) == 10
    assert rs.auto_install_recommended(tmp_path)["installed_ids"] == []

    custom = tmp_path / "strategies" / "custom"
    victim = custom / "rec_risk_adjusted_momentum.py"
    victim.unlink()
    assert rs.auto_install_recommended(tmp_path)["installed_ids"] == []

    listing = rs.list_recommended(tmp_path)
    status = {item["id"]: item["status"] for item in listing["items"]}
    assert status["rec_risk_adjusted_momentum"] == "missing"
    assert "rec_risk_adjusted_momentum" not in listing["prompt_pending_ids"]


def test_modified_file_is_not_overwritten_until_explicit(tmp_path):
    rs.auto_install_recommended(tmp_path)
    target = tmp_path / "strategies" / "custom" / "rec_risk_adjusted_momentum.py"
    original = target.read_text(encoding="utf-8")
    target.write_text(original + "\n# local user change\n", encoding="utf-8")

    assert rs.auto_install_recommended(tmp_path)["installed_ids"] == []
    assert "# local user change" in target.read_text(encoding="utf-8")
    status = {item["id"]: item["status"] for item in rs.list_recommended(tmp_path)["items"]}
    assert status["rec_risk_adjusted_momentum"] == "modified"

    engine = _installed_engine(tmp_path)
    result = rs.install_recommended(
        tmp_path, ["rec_risk_adjusted_momentum"], overwrite=True, engine=engine,
    )
    assert result["installed_ids"] == ["rec_risk_adjusted_momentum"]
    assert "# local user change" not in target.read_text(encoding="utf-8")
    assert result["backup_dir"]
    backup = next((tmp_path / "user_data" / "recommended_strategy_backups").rglob(target.name))
    assert "# local user change" in backup.read_text(encoding="utf-8")


def test_conflict_requires_overwrite_and_backs_up(tmp_path):
    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    target = custom / "rec_risk_adjusted_momentum.py"
    target.write_text("# user file with same name\n", encoding="utf-8")

    assert len(rs.auto_install_recommended(tmp_path)["installed_ids"]) == 9
    status = {item["id"]: item["status"] for item in rs.list_recommended(tmp_path)["items"]}
    assert status["rec_risk_adjusted_momentum"] == "conflict"

    with pytest.raises(rs.RecommendedConflictError):
        rs.install_recommended(tmp_path, ["rec_risk_adjusted_momentum"])
    assert target.read_text(encoding="utf-8") == "# user file with same name\n"

    result = rs.install_recommended(
        tmp_path, ["rec_risk_adjusted_momentum"], overwrite=True,
    )
    assert result["backup_dir"]
    assert target.read_text(encoding="utf-8").startswith('"""Risk-adjusted momentum')


def test_prompt_ack_clears_pending(tmp_path):
    rs.auto_install_recommended(tmp_path)
    listing = rs.list_recommended(tmp_path)
    assert len(listing["prompt_pending_ids"]) == 10
    acked = rs.ack_prompt(tmp_path, listing["prompt_pending_ids"][:3])
    assert len(acked["prompt_pending_ids"]) == 7
    acked = rs.ack_prompt(tmp_path, [])
    assert acked["prompt_pending_ids"] == []


def _api_client(tmp_path, monkeypatch):
    engine = StrategyEngine(strategy_dirs=[tmp_path / "strategies" / "custom"])
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = engine
    app.include_router(recommended_api.router)
    monkeypatch.setattr(
        "app.api.strategy._invalidate_strategy_runtime", lambda _request: None,
    )
    return TestClient(app), engine


def test_api_list_install_and_prompt_ack(tmp_path, monkeypatch):
    client, engine = _api_client(tmp_path, monkeypatch)
    response = client.get("/api/strategy-recommended")
    assert response.status_code == 200
    payload = response.json()
    assert payload["pack_version"] == "2026.11.1"
    assert len(payload["items"]) == 10
    assert {item["status"] for item in payload["items"]} == {"not_installed"}

    response = client.post("/api/strategy-recommended/install", json={
        "ids": ["rec_risk_adjusted_momentum"], "overwrite": False,
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["installed_ids"] == ["rec_risk_adjusted_momentum"]
    assert engine.has("rec_risk_adjusted_momentum")

    response = client.post("/api/strategy-recommended/prompt-ack", json={"ids": []})
    assert response.status_code == 200
    assert response.json()["prompt_pending_ids"] == []


def test_api_conflict_returns_409(tmp_path, monkeypatch):
    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "rec_risk_adjusted_momentum.py").write_text("# user file\n", encoding="utf-8")
    client, _engine = _api_client(tmp_path, monkeypatch)

    response = client.post("/api/strategy-recommended/install", json={
        "ids": ["rec_risk_adjusted_momentum"], "overwrite": False,
    })
    assert response.status_code == 409
    assert "覆盖" in response.json()["detail"]


def test_strategy_detail_exposes_recommended_metadata():
    class Dummy:
        meta = {
            "id": "rec_x",
            "name": "推荐策略",
            "origin": "recommended",
            "suitable_for": {"market_cap": ["large"], "note": "测试"},
        }
        basic_filter = {}
        entry_signals = []
        exit_signals = []
        stop_loss = None
        take_profit = None
        trailing_stop = None
        trailing_take_profit_activate = None
        trailing_take_profit_drawdown = None
        max_hold_days = None
        execution_backend = "matrix_native"
        source = "custom"

    detail = _strategy_detail(Dummy())
    assert detail["origin"] == "recommended"
    assert detail["suitable_for"]["note"] == "测试"


def _market(rows: int = 320, cols: int = 4):
    from app.backtest.matrix import MarketDataMatrix

    rng = np.random.default_rng(7)
    returns = rng.normal(0.0006, 0.014, (rows, cols))
    close = (10.0 * np.exp(np.cumsum(returns, axis=0))).astype(np.float32)
    open_ = (close * (1.0 + rng.normal(0, 0.003, (rows, cols)))).astype(np.float32)
    high = (np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, 0.004, (rows, cols))))).astype(np.float32)
    low = (np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, 0.004, (rows, cols))))).astype(np.float32)
    volume = rng.lognormal(13, 0.4, (rows, cols)).astype(np.float32)
    amount = (close * volume).astype(np.float32)
    timestamps = np.datetime64("2025-01-01") + np.arange(rows).astype("timedelta64[D]")
    session_ids = np.tile(np.arange(rows, dtype=np.int32)[:, None], (1, cols))
    return MarketDataMatrix(
        timestamps=timestamps,
        timestamp_labels=tuple(str(value) for value in timestamps),
        session_ids=session_ids,
        symbols=tuple(f"S{i}" for i in range(cols)),
        names=tuple(f"N{i}" for i in range(cols)),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        tradable=np.ones((rows, cols), dtype=bool),
        limit_up_locked=np.zeros((rows, cols), dtype=bool),
        limit_down_locked=np.zeros((rows, cols), dtype=bool),
        fields={"amount": amount},
    )


def _slice_market(market, rows: int):
    from app.backtest.matrix import MarketDataMatrix

    return MarketDataMatrix(
        timestamps=market.timestamps[:rows],
        timestamp_labels=market.timestamp_labels[:rows],
        session_ids=market.session_ids[:rows],
        symbols=market.symbols,
        names=market.names,
        open=market.open[:rows],
        high=market.high[:rows],
        low=market.low[:rows],
        close=market.close[:rows],
        volume=market.volume[:rows],
        tradable=market.tradable[:rows],
        limit_up_locked=market.limit_up_locked[:rows],
        limit_down_locked=market.limit_down_locked[:rows],
        fields={name: value[:rows] for name, value in market.fields.items()},
    )


def test_all_recommended_strategies_compute_and_are_causal(tmp_path):
    rs.auto_install_recommended(tmp_path)
    engine = _installed_engine(tmp_path)
    market = _market()
    truncated = _slice_market(market, 300)
    for meta in engine.list_strategies():
        strategy = engine.get(meta["id"])
        params = {p["id"]: p["default"] for p in strategy.meta.get("params", [])}
        signals = strategy.matrix_strategy.compute_signals(market, params)
        assert signals.shape == market.shape
        assert np.isfinite(signals.score).all()
        assert signals.entry.shape == market.shape

        prefix = strategy.matrix_strategy.compute_signals(truncated, params)
        np.testing.assert_array_equal(signals.entry[:300], prefix.entry)
        np.testing.assert_array_equal(signals.exit[:300], prefix.exit)
        np.testing.assert_allclose(signals.score[:300], prefix.score, rtol=1e-5, atol=1e-6)
