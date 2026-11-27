#!/usr/bin/env python3
"""金字塔 (WinStock) 历史数据批量补齐 CLI。

用途: 把金字塔本地 .dat 的历史往前补到服务器能提供的深度。

    Day   1990-12-19 起 (靠 20240701 全量包)
    Min1  2010-01-04 起
    Min5  2000-01-04 起

关键行为:
  - 优先读金字塔自己的 DataCache (<安装目录>/DataCache/<市场>/<周期>/<日期>.zip)。
    用户在 GUI 里做过「数据补充」的话, 这里已经有包, 全程零网络。
  - 没有缓存才联网拉 FileData。
  - 合并是「按日期去重 + 升序」, 重复执行幂等, 不会写坏已有数据。

用法:
    # 看配置和缓存概况
    python scripts/weisoft_bulk_sync.py --status

    # 用本地缓存里的全部日包补日K (不联网)
    python scripts/weisoft_bulk_sync.py --from-cache --market SH --period Day

    # 拉 20240701 全量包 (日K 一次补到 1990)
    python scripts/weisoft_bulk_sync.py --full --market SH --period Day

    # 按区间补增量 (周末自动跳过, 无包日期 404 自动跳过)
    python scripts/weisoft_bulk_sync.py --range 20261101 20261125 --period Min1

    # 只补指定标的
    python scripts/weisoft_bulk_sync.py --full --codes 510300,000001

    # 先看会改什么, 不落盘
    python scripts/weisoft_bulk_sync.py --full --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许直接 `python scripts/xxx.py` 运行
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.plugins.weisoft import datfile  # noqa: E402
from app.plugins.weisoft import sync as ws  # noqa: E402
from app.plugins.weisoft.provider import (  # noqa: E402
    ENV_DATA_ROOT,
    resolve_data_root,
    resolve_server,
)


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def cmd_status(root: Path) -> int:
    print(f"data_root : {root}")
    print(f"server    : {resolve_server()}")
    markets = datfile.list_markets(root)
    print(f"markets   : {', '.join(markets) or '(none)'}")
    print()
    print(f"{'market':8} {'period':8} {'files':>7} {'cache':>7} {'cache range':>24}")
    for market in markets:
        for period in datfile.list_periods(root, market):
            codes = datfile.list_codes(root, market, period)
            pkgs = ws.available_packages(root, market, period)
            rng = f"{pkgs[0]}~{pkgs[-1]}" if pkgs else "-"
            print(f"{market:8} {period:8} {len(codes):>7} {len(pkgs):>7} {rng:>24}")

    cache_dir = root.parent / "DataCache"
    if cache_dir.is_dir():
        total = sum(f.stat().st_size for f in cache_dir.rglob("*.zip"))
        count = sum(1 for _ in cache_dir.rglob("*.zip"))
        print()
        print(f"DataCache : {count} zips, {_fmt_bytes(total)} ({cache_dir})")
    return 0


def _progress(ymd: str, res) -> None:
    if res is None:
        return
    added = sum(r.added for r in res)
    replaced = sum(r.replaced for r in res)
    print(f"  {ymd}: {len(res)} symbols, +{added} rows, ~{replaced} replaced", flush=True)


def _run_sync(root: Path, args, days: list[str]) -> int:
    codes = {c.strip() for c in (args.codes or "").split(",") if c.strip()} or None
    if codes:
        print(f"codes filter: {', '.join(sorted(codes))}")
    print(f"{args.market}/{args.period}: {len(days)} 个日期, dry_run={args.dry_run}, "
          f"use_cache={args.use_cache}")

    total_added = total_replaced = hit = 0
    t0 = time.time()
    for i, ymd in enumerate(days, 1):
        try:
            res = ws.sync_one_day(
                root, args.market, args.period, ymd,
                server=resolve_server(), codes=codes,
                dry_run=args.dry_run, use_cache=args.use_cache,
            )
        except ws.SyncError as exc:
            print(f"  {ymd}: SKIP ({exc})", flush=True)
            continue
        if not res:
            continue
        hit += 1
        added = sum(r.added for r in res)
        replaced = sum(r.replaced for r in res)
        total_added += added
        total_replaced += replaced
        print(f"  [{i}/{len(days)}] {ymd}: {len(res)} symbols, "
              f"+{added} rows, ~{replaced} replaced", flush=True)

    print()
    print(f"完成: {hit} 个日期有包, 新增 {total_added} 行, 覆盖 {total_replaced} 行, "
          f"耗时 {time.time() - t0:.1f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="金字塔历史数据批量补齐 (下载 → 解压 → 合并)",
    )
    ap.add_argument("--data-root", help=f"金字塔 Data 目录; 默认读 {ENV_DATA_ROOT}")
    ap.add_argument("--market", default="SH", choices=["SH", "SZ", "BJ"])
    ap.add_argument("--period", default="Day", choices=["Day", "Min1", "Min5"])
    ap.add_argument("--codes", help="逗号分隔的 6 位代码; 省略=包内全部品种")
    ap.add_argument("--dry-run", action="store_true", help="只统计, 不写盘")
    ap.add_argument("--no-cache", dest="use_cache", action="store_false",
                    help="忽略 DataCache, 强制联网")
    ap.set_defaults(use_cache=True)

    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--status", action="store_true", help="只看配置与缓存概况")
    mode.add_argument("--full", action="store_true", help="拉全量包 (日K 到 1990)")
    mode.add_argument("--from-cache", action="store_true", help="只用本地缓存的包")
    mode.add_argument("--range", nargs=2, metavar=("START", "END"),
                      help="按 YYYYMMDD 区间补增量")

    args = ap.parse_args(argv)

    root = Path(args.data_root) if args.data_root else resolve_data_root()
    if root is None:
        print(f"未找到金字塔 Data 目录; 请用 --data-root 或设置 {ENV_DATA_ROOT}",
              file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"目录不存在: {root}", file=sys.stderr)
        return 2

    if args.status:
        return cmd_status(root)

    if args.full:
        if args.period != "Day":
            print("--full 只适用于 Day (全量包只存在于日K)", file=sys.stderr)
            return 2
        days = [ws.FULL_DAY_PACKAGE]
    elif args.from_cache:
        days = ws.available_packages(root, args.market, args.period)
        if not days:
            print(f"DataCache 里没有 {args.market}/{args.period} 的包",
                  file=sys.stderr)
            return 1
    else:
        start, end = args.range
        days = list(ws.iter_trade_days(start, end))

    return _run_sync(root, args, days)


if __name__ == "__main__":
    raise SystemExit(main())
