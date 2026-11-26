"""模拟盘账户级股票范围 — 归一化、按账户过滤、分组动态跟随与合成规则范围。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.paper import _validate_auto_scope
from app.config import settings
from app.services import watchlist
from app.strategy import paper, paper_auto, paper_watch
from app.strategy.monitor import MonitorRuleEngine


def _auto_rule() -> dict:
    return {
        "name": "跟demo",
        "match_kind": "strategy",
        "match_id": "demo",
        "side": "buy",
        "size_mode": "fixed_amount",
        "size_value": 5000,
        "order_type": "next_open",
        "cooldown_days": 0,
        "max_positions": 0,
        "enabled": True,
    }


def _event(symbol: str, *, rule_id: str = "paper_watch:demo") -> dict:
    return {
        "source": "strategy",
        "strategy_id": "demo",
        "rule_id": rule_id,
        "type": "buy_signal",
        "symbol": symbol,
        "price": 10.0,
    }


class _FakeStrategyEngine:
    def get(self, strategy_id: str):
        if strategy_id != "demo":
            raise ValueError(f"unknown strategy: {strategy_id}")
        return SimpleNamespace(
            meta={"id": "demo", "name": "示例策略"},
            execution_backend="polars_expr",
            filter_history_fn=None,
        )


def test_normalize_and_validate_auto_scope():
    assert paper.normalize_auto_scope(None) == {"scope": "all", "symbols": [], "group_id": None}
    assert paper.normalize_auto_scope({"scope": "symbols", "symbols": ["B", "A", "A"]}) == {
        "scope": "symbols", "symbols": ["B", "A"], "group_id": None,
    }
    assert paper.scope_key({"scope": "all"}) == "all"
    assert paper.scope_key({"scope": "watchlist_group", "group_id": "g1"}) == "g_g1"
    key_a = paper.scope_key({"scope": "symbols", "symbols": ["B", "A"]})
    key_b = paper.scope_key({"scope": "symbols", "symbols": ["A", "B"]})
    assert key_a == key_b and key_a.startswith("s_")

    with pytest.raises(ValueError, match="scope 必须是"):
        paper.validate_auto_scope({"scope": "sector"})
    with pytest.raises(ValueError, match="symbols 不能为空"):
        paper.validate_auto_scope({"scope": "symbols", "symbols": []})
    with pytest.raises(ValueError, match="自选分组"):
        paper.validate_auto_scope({"scope": "watchlist_group", "group_id": ""})


def test_manual_scope_filters_events_and_isolates_accounts(tmp_path):
    cfg_one = {"scope": "symbols", "symbols": ["000001.SZ"]}
    cfg_two = {"scope": "symbols", "symbols": ["600000.SH"]}
    paper.create_account(tmp_path, 100_000, account_id="acc1", auto_scope=cfg_one)
    paper.create_account(tmp_path, 100_000, account_id="acc2", auto_scope=cfg_two)
    paper_auto.create_auto_rule(tmp_path, _auto_rule(), account_id="acc1")
    paper_auto.create_auto_rule(tmp_path, _auto_rule(), account_id="acc2")

    id_one = paper_watch.synthetic_rule_id("demo", paper.scope_key(cfg_one))
    id_two = paper_watch.synthetic_rule_id("demo", paper.scope_key(cfg_two))
    syms = ("000001.SZ", "600000.SH", "300750.SZ")
    one = paper_auto.on_rule_events(tmp_path, [_event(s, rule_id=id_one) for s in syms], account_id="acc1")
    two = paper_auto.on_rule_events(tmp_path, [_event(s, rule_id=id_two) for s in syms], account_id="acc2")
    assert [o["symbol"] for o in one] == ["000001.SZ"]
    assert [o["symbol"] for o in two] == ["600000.SH"]

    # 老账户缺 auto_scope: 兼容为 all
    paper.create_account(tmp_path, 100_000, account_id="legacy")
    paper_auto.create_auto_rule(tmp_path, _auto_rule(), account_id="legacy")
    all_events = paper_auto.on_rule_events(tmp_path, [_event(s) for s in syms], account_id="legacy")
    assert {o["symbol"] for o in all_events} == {"000001.SZ", "600000.SH", "300750.SZ"}


def test_group_scope_dynamic_follow(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, group = watchlist.create_group("模拟盘范围")
    gid = group["id"]
    watchlist.add("000001.SZ", group_id=gid)

    cfg = {"scope": "watchlist_group", "group_id": gid}
    paper.create_account(tmp_path, 100_000, account_id="gacc", auto_scope=cfg)
    paper_auto.create_auto_rule(tmp_path, _auto_rule(), account_id="gacc")
    group_rule_id = paper_watch.synthetic_rule_id("demo", paper.scope_key(cfg))
    events = [_event(s, rule_id=group_rule_id) for s in ("000001.SZ", "600000.SH")]

    assert [o["symbol"] for o in paper_auto.on_rule_events(tmp_path, events, account_id="gacc")] == ["000001.SZ"]

    watchlist.add("600000.SH", group_id=gid)
    assert {o["symbol"] for o in paper_auto.on_rule_events(tmp_path, events, account_id="gacc")} == {
        "000001.SZ", "600000.SH",
    }

    watchlist.remove_from_group("000001.SZ", gid)
    assert [o["symbol"] for o in paper_auto.on_rule_events(tmp_path, events, account_id="gacc")] == ["600000.SH"]
    assert paper.auto_scope_warning(tmp_path, "gacc") is None


def test_group_scope_missing_or_empty_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, group = watchlist.create_group("空组")
    for account_id, gid in (("empty", group["id"]), ("ghost", "ghost_group")):
        paper.create_account(
            tmp_path, 100_000, account_id=account_id,
            auto_scope={"scope": "watchlist_group", "group_id": gid},
        )
        paper_auto.create_auto_rule(tmp_path, _auto_rule(), account_id=account_id)
        gid_rule_id = paper_watch.synthetic_rule_id("demo", paper.scope_key({"scope": "watchlist_group", "group_id": gid}))
        assert paper_auto.on_rule_events(tmp_path, [_event("000001.SZ", rule_id=gid_rule_id)], account_id=account_id) == []
        assert "已暂停" in (paper.auto_scope_warning(tmp_path, account_id) or "")


def test_batch_event_skipped_under_restricted_scope(tmp_path):
    paper.create_account(tmp_path, 100_000, account_id="acc1", auto_scope={"scope": "symbols", "symbols": ["000001.SZ"]})
    paper_auto.create_auto_rule(tmp_path, _auto_rule(), account_id="acc1")
    ev = _event("_batch")
    assert paper_auto.on_rule_events(tmp_path, [ev], account_id="acc1") == []


def test_scope_specific_synthetic_rules_and_scope_key_routing(tmp_path):
    key_all = paper.scope_key({"scope": "all"})
    key_manual = paper.scope_key({"scope": "symbols", "symbols": ["000001.SZ"]})
    paper.create_account(tmp_path, 100_000, account_id="acc1", auto_scope={"scope": "all"})
    paper.create_account(
        tmp_path, 100_000, account_id="acc2",
        auto_scope={"scope": "symbols", "symbols": ["000001.SZ"]},
    )
    paper_auto.create_auto_rule(tmp_path, _auto_rule(), account_id="acc1")
    paper_auto.create_auto_rule(tmp_path, _auto_rule(), account_id="acc2")

    engine = MonitorRuleEngine()
    engine.set_strategy_engine(_FakeStrategyEngine())
    engine.add_rule({
        "id": "paper_watch:demo:stale", "name": "旧规则", "type": "strategy",
        "strategy_id": "demo", "scope": "all", "enabled": True, "notify_events": ["buy_signal"],
    })
    result = paper_watch.sync(tmp_path, engine, _FakeStrategyEngine())
    all_id = paper_watch.synthetic_rule_id("demo", key_all)
    manual_id = paper_watch.synthetic_rule_id("demo", key_manual)
    assert all_id in engine.rules and manual_id in engine.rules
    assert engine.rules[all_id]["scope"] == "all"
    assert engine.rules[manual_id]["scope"] == "symbols"
    assert engine.rules[manual_id]["symbols"] == ["000001.SZ"]
    assert "paper_watch:demo:stale" not in engine.rules
    assert set(result["watching"]) == {"demo"}

    # 合成事件按 scope_key 二次分流: acc2 只接受自己范围的事件
    events = [
        _event("000001.SZ", rule_id=manual_id),
        _event("600000.SH", rule_id=all_id),
    ]
    assert [o["symbol"] for o in paper_auto.on_rule_events(tmp_path, events, account_id="acc2")] == ["000001.SZ"]


def test_real_monitor_rule_scope_still_filters_account(tmp_path):
    """已有真监控规则时不生成合成规则, 但账户范围仍做事件二次过滤。"""
    paper.create_account(tmp_path, 100_000, account_id="acc1", auto_scope={"scope": "symbols", "symbols": ["000001.SZ"]})
    paper_auto.create_auto_rule(tmp_path, _auto_rule(), account_id="acc1")

    engine = MonitorRuleEngine()
    engine.set_strategy_engine(_FakeStrategyEngine())
    engine.add_rule({
        "id": "mr_real", "name": "真监控", "type": "strategy", "strategy_id": "demo",
        "asset_type": "stock", "scope": "all", "enabled": True, "notify_events": ["buy_signal"],
    })
    result = paper_watch.sync(tmp_path, engine, _FakeStrategyEngine())
    assert result["watching"] == []
    assert not any(rid.startswith("paper_watch:") for rid in engine.rules)

    events = [_event("000001.SZ", rule_id="mr_real"), _event("600000.SH", rule_id="mr_real")]
    assert [o["symbol"] for o in paper_auto.on_rule_events(tmp_path, events, account_id="acc1")] == ["000001.SZ"]


def test_api_scope_validation_and_persistence(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, group = watchlist.create_group("合法分组")
    cfg = {"scope": "watchlist_group", "group_id": group["id"]}
    assert _validate_auto_scope(cfg) == {"scope": "watchlist_group", "symbols": [], "group_id": group["id"]}

    with pytest.raises(HTTPException) as exc:
        _validate_auto_scope({"scope": "watchlist_group", "group_id": "ghost"})
    assert exc.value.status_code == 400

    acc = paper.create_account(tmp_path, 100_000, account_id="apiacc", auto_scope={"scope": "symbols", "symbols": ["000001.SZ"]})
    assert paper.get_auto_scope(tmp_path, "apiacc") == acc["auto_scope"]
    updated = paper.update_settings(tmp_path, "apiacc", auto_scope=cfg)
    expected = {"scope": "watchlist_group", "symbols": [], "group_id": group["id"]}
    assert paper.get_auto_scope(tmp_path, "apiacc") == updated["auto_scope"] == expected
