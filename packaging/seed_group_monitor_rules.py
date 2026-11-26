"""按自选分组批量生成"策略监控"规则 (动态跟随分组成员)。

用法 (需 TSP 已启动, 且新版已放开 ETF 分组作用域):
    .venv/Scripts/python.exe packaging/seed_group_monitor_rules.py --dry-run
    .venv/Scripts/python.exe packaging/seed_group_monitor_rules.py

机制: 规则存 group_id 引用而非标的快照, 分组内加减标的后下一轮监控自动跟随。
走 POST /api/monitor-rules (校验 + 原子写 + 引擎热重载), 不直接写 JSON。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:3018"

# 组名 -> 组码 (来自实测 watchlist_groups.json)
GROUP_CODES = {
    "行业主题ETF": "hyzt", "跨境ETF": "kjetf", "宽基ETF": "kj", "策略ETF": "cl",
    "债券ETF": "zq", "商品ETF": "sp", "货币ETF": "hb", "海外": "hw",
    "ETF": "etf", "持仓": "cc", "现金流": "xj", "红利": "hl", "银行": "yh",
}

# 组名 -> {资产类型: [策略id]} —— 按上一轮策略适配分析
GROUP_STRATEGIES = {
    "行业主题ETF": {"etf": ["trend_breakout", "boll_breakout", "breakout_new_high_60d", "oversold_bounce", "rsi_midline_pullback"]},
    "跨境ETF": {"etf": ["turtle_trading", "bullish_alignment", "trend_breakout", "rsi_midline_pullback"]},
    "宽基ETF": {"etf": ["turtle_trading", "bullish_alignment", "low_volatility_leader", "ma_golden_cross", "macd_golden"]},
    "策略ETF": {"etf": ["low_volatility_leader", "bullish_alignment", "turtle_trading"]},
    "商品ETF": {"etf": ["turtle_trading", "trend_breakout", "boll_breakout"]},
    "ETF": {"etf": ["turtle_trading", "trend_breakout", "low_volatility_leader"]},
    "红利": {"etf": ["low_volatility_leader", "bullish_alignment", "turtle_trading"]},
    "海外": {"etf": ["turtle_trading", "bullish_alignment", "trend_breakout"]},
    "持仓": {"etf": ["turtle_trading", "bullish_alignment", "low_volatility_leader"],
             "stock": ["bullish_alignment", "low_volatility_leader"]},
    "银行": {"stock": ["bullish_alignment", "low_volatility_leader", "ma_golden_cross"]},
    "债券ETF": {"etf": ["bond_trend", "low_volatility_leader", "ma_golden_cross"]},
    "货币ETF": {"etf": ["bond_trend", "low_volatility_leader", "ma_golden_cross"]},
    "现金流": {"etf": ["bond_trend", "low_volatility_leader", "ma_golden_cross"],
             "stock": ["bullish_alignment", "ma_golden_cross"]},
}
LOW_SENSITIVE = {"债券ETF", "货币ETF", "现金流"}


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def detect_port() -> str:
    global BASE
    for p in (3018, 3019, 3020, 3021, 3022):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{p}/api/capabilities", timeout=2)
            BASE = f"http://127.0.0.1:{p}"
            return BASE
        except Exception:
            continue
    raise SystemExit("找不到运行中的 TSP 后端 (扫 3018-3022)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    base = detect_port()
    print(f"[info] 后端 {base}", flush=True)

    groups = get("/api/watchlist/groups")
    if isinstance(groups, dict):
        groups = groups.get("groups", [])
    gid_by_name = {g["name"]: g["id"] for g in groups}
    print(f"[info] 分组 {len(groups)} 个", flush=True)

    wl_raw = get("/api/watchlist")
    wl = wl_raw.get("symbols", wl_raw.get("rows", []))
    enr = get("/api/watchlist/enriched").get("rows", [])
    asset_of = {r["symbol"]: r.get("asset_type") for r in enr}
    # 组 -> 资产类型 -> 成员集合
    members: dict[str, dict[str, set]] = {}
    for row in wl:
        at = asset_of.get(row["symbol"])
        if at not in ("stock", "etf"):
            continue  # LOF/index 跳过 (无评估轮)
        for gid in row.get("group_ids") or []:
            members.setdefault(gid, {}).setdefault(at, set()).add(row["symbol"])

    strategies = get("/api/strategies")
    if strategies.get("load_errors"):
        print(f"[warn] 策略加载错误: {strategies['load_errors']}", flush=True)
    sup = {s["id"]: set(s.get("asset_types", [])) for s in strategies["strategies"]}
    name_of = {s["id"]: s.get("name", s["id"]) for s in strategies["strategies"]}
    print(f"[info] 策略 {len(sup)} 个", flush=True)

    payloads = []
    skipped = []
    for gname, at_map in GROUP_STRATEGIES.items():
        gid = gid_by_name.get(gname)
        if not gid:
            skipped.append(f"分组不存在: {gname}")
            continue
        code = GROUP_CODES.get(gname, gname[:4])
        for at, sids in at_map.items():
            if not members.get(gid, {}).get(at):
                skipped.append(f"{gname}/{at}: 组内无该资产类型成员")
                continue
            for sid in sids:
                if sid not in sup:
                    skipped.append(f"{gname}/{sid}: 策略不存在")
                    continue
                if at not in sup[sid]:
                    skipped.append(f"{gname}/{sid}: 策略不支持 {at}")
                    continue
                checksum = hashlib.sha1(sid.encode()).hexdigest()[:2]
                rid = f"mgrp_{code}_{sid[:18]}_{'etf' if at=='etf' else 'stk'}_{checksum}"
                low = gname in LOW_SENSITIVE
                nm = f"{gname} · {name_of[sid]}（分组动态）" + ("（低敏）" if low else "")
                payloads.append({
                    "id": rid, "name": nm, "enabled": True,
                    "type": "strategy", "asset_type": at,
                    "scope": "watchlist_group", "group_id": gid, "symbols": [],
                    "strategy_id": sid, "direction": "entry",
                    "notify_events": ["pool_entry"],
                    "conditions": [], "logic": "and",
                    "cooldown_seconds": 86400 if low else 3600,
                    "severity": "info", "webhook_channels": [], "message": "",
                })

    ids = [p["id"] for p in payloads]
    if len(ids) != len(set(ids)):
        dup = {i for i in ids if ids.count(i) > 1}
        raise SystemExit(f"规则 id 冲突: {dup}")
    print(f"[info] 拟生成 {len(payloads)} 条规则; 跳过 {len(skipped)} 项", flush=True)
    for s in skipped:
        print(f"  [skip] {s}", flush=True)

    if args.dry_run:
        for p in payloads:
            print(f"  {p['id']} | {p['name']} | {p['asset_type']}", flush=True)
        print("[dry-run] 未提交", flush=True)
        return

    ok = fail = 0
    for p in payloads:
        try:
            r = post("/api/monitor-rules", p)
            if r.get("ok"):
                ok += 1
            else:
                fail += 1
                print(f"  [FAIL] {p['id']}: {r}", flush=True)
        except urllib.error.HTTPError as e:
            fail += 1
            print(f"  [FAIL] {p['id']}: HTTP {e.code} {e.read().decode('utf-8', 'ignore')[:200]}", flush=True)
        except Exception as e:
            fail += 1
            print(f"  [FAIL] {p['id']}: {e}", flush=True)
    print(f"[done] 成功 {ok}, 失败 {fail}", flush=True)

    listed = get("/api/monitor-rules").get("rules", [])
    mgrp = [r for r in listed if r.get("id", "").startswith("mgrp_")]
    warn = [r["id"] for r in mgrp if r.get("runtime_warning")]
    print(f"[verify] 磁盘共 {len(mgrp)} 条 mgrp_* 规则, runtime_warning {len(warn)} 条", flush=True)
    if warn:
        print(f"  warn ids: {warn}", flush=True)


if __name__ == "__main__":
    main()
