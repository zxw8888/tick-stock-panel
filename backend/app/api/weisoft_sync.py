"""金字塔数据源同步 API —— 下载官方 FileData 包并合并进本地行情库。

这是「把历史往前补」的唯一入口: 本地 .dat 有多少只能读多少,
要拿到 1990 年至今的日K / 2000 年至今的 5 分钟, 必须走这里。

端点:
    GET  /api/weisoft/status           当前配置与本地数据概览
    GET  /api/weisoft/probe            探测某天服务器上有哪些包 (不下载)
    POST /api/weisoft/sync             同步单日
    POST /api/weisoft/sync-range       同步日期区间
"""

from __future__ import annotations

import logging
import threading
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.plugins.weisoft import datfile
from app.plugins.weisoft import sync as ws
from app.plugins.weisoft.provider import (
    ENV_DATA_ROOT,
    ENV_SERVER,
    resolve_data_root,
    resolve_server,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/weisoft", tags=["weisoft"])

# 同一时刻只允许一个同步任务, 避免两个请求同时写同一个 .dat
_sync_lock = threading.Lock()


class SyncIn(BaseModel):
    market: Literal["SH", "SZ", "BJ"] = "SH"
    period: Literal["Day", "Min1", "Min5"] = "Day"
    ymd: str = Field(..., description="交易日 YYYYMMDD")
    codes: list[str] | None = Field(None, description="只处理这些 6 位代码; 空=全部")
    dry_run: bool = False
    use_cache: bool = Field(
        True, description="优先读金字塔自己的 DataCache, 缺失再联网"
    )


class SyncRangeIn(BaseModel):
    market: Literal["SH", "SZ", "BJ"] = "SH"
    period: Literal["Day", "Min1", "Min5"] = "Day"
    start_ymd: str = Field(..., description="起始 YYYYMMDD")
    end_ymd: str = Field(..., description="结束 YYYYMMDD")
    codes: list[str] | None = None
    dry_run: bool = False
    use_cache: bool = True


def _root():
    root = resolve_data_root()
    if root is None:
        raise HTTPException(
            400,
            f"未找到金字塔 Data 目录; 请设置环境变量 {ENV_DATA_ROOT}",
        )
    return root


@router.get("/status")
def status() -> dict:
    """当前配置 + 本地数据概览。"""
    root = resolve_data_root()
    if root is None:
        return {
            "configured": False,
            "env_data_root": ENV_DATA_ROOT,
            "env_server": ENV_SERVER,
            "hint": f"设置环境变量 {ENV_DATA_ROOT} 指向金字塔 Data 目录",
        }
    markets = datfile.list_markets(root)
    periods: dict[str, dict] = {}
    for market in markets:
        for period in datfile.list_periods(root, market):
            codes = datfile.list_codes(root, market, period)
            periods.setdefault(period, {})[market] = len(codes)

    # DataCache: 金字塔 GUI「数据补充」留下的 zip, 同步时可免联网
    cache_dir = root.parent / "DataCache"
    cache: dict[str, dict] = {}
    if cache_dir.is_dir():
        for market_dir in sorted(cache_dir.iterdir()):
            if not market_dir.is_dir():
                continue
            for period_dir in sorted(market_dir.iterdir()):
                if not period_dir.is_dir():
                    continue
                files = sorted(period_dir.glob("*.zip"))
                if not files:
                    continue
                cache.setdefault(period_dir.name, {})[market_dir.name] = {
                    "packages": len(files),
                    "bytes": sum(f.stat().st_size for f in files),
                    "first": files[0].stem,
                    "last": files[-1].stem,
                }
    return {
        "configured": True,
        "data_root": str(root),
        "server": resolve_server(),
        "markets": markets,
        "periods": periods,
        "cache": cache,
        "capabilities": {
            "daily": True,
            "minute": "Min1" in periods or "Min5" in periods,
            "realtime": False,
            "depth5": False,
            "financial": False,
            "adj_factor": False,
        },
    }


@router.get("/probe")
def probe(ymd: str) -> dict:
    """探测某天服务器上有哪些包 (只发 HEAD 级请求, 不下载正文)。"""
    import urllib.error
    import urllib.request

    server = resolve_server()
    out: list[dict] = []
    for market in ("SH", "SZ"):
        for period in ("Day", "Min1", "Min5"):
            url = ws.package_url(server, market, period, ymd)
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    size = int(resp.headers.get("Content-Length") or 0)
                out.append({"market": market, "period": period, "status": 200,
                            "bytes": size})
            except urllib.error.HTTPError as exc:
                out.append({"market": market, "period": period,
                            "status": exc.code, "bytes": 0})
            except Exception as exc:  # noqa: BLE001
                out.append({"market": market, "period": period,
                            "status": 0, "error": str(exc)})
    return {"ymd": ymd, "server": server, "packages": out}


@router.post("/sync")
def sync_day(req: SyncIn) -> dict:
    """同步单日。dry_run=true 只统计不写盘。"""
    root = _root()
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(409, "已有同步任务在运行, 请稍后再试")
    try:
        codes = set(req.codes) if req.codes else None
        results = ws.sync_one_day(
            root, req.market, req.period, req.ymd,
            server=resolve_server(), codes=codes, dry_run=req.dry_run,
            use_cache=req.use_cache,
        )
    except ws.SyncError as exc:
        raise HTTPException(502, f"同步失败: {exc}") from exc
    finally:
        _sync_lock.release()

    if not results:
        return {"ymd": req.ymd, "market": req.market, "period": req.period,
                "found": False, "message": "该日服务器无包 (404)"}
    added = sum(r.added for r in results)
    replaced = sum(r.replaced for r in results)
    return {
        "ymd": req.ymd, "market": req.market, "period": req.period,
        "found": True, "dry_run": req.dry_run,
        "symbols": len(results),
        "added": added, "replaced": replaced,
        "details": [
            {"code": r.code, "before": r.before, "after": r.after,
             "added": r.added, "replaced": r.replaced}
            for r in sorted(results, key=lambda x: -x.added)[:20]
        ],
    }


@router.post("/sync-range")
def sync_range(req: SyncRangeIn) -> dict:
    """同步日期区间。逐日拉取, 无包的日子自动跳过。"""
    root = _root()
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(409, "已有同步任务在运行, 请稍后再试")
    days: list[dict] = []
    try:
        codes = set(req.codes) if req.codes else None

        def _done(ymd: str, res) -> None:
            if res is None:
                days.append({"ymd": ymd, "found": False})
            else:
                days.append({
                    "ymd": ymd, "found": True,
                    "symbols": len(res),
                    "added": sum(r.added for r in res),
                })

        ws.sync_range(
            root, req.market, req.period, req.start_ymd, req.end_ymd,
            server=resolve_server(), codes=codes, dry_run=req.dry_run,
            use_cache=req.use_cache,
            on_day_done=_done,
        )
    except ws.SyncError as exc:
        raise HTTPException(502, f"同步失败: {exc}") from exc
    finally:
        _sync_lock.release()

    hit = [d for d in days if d.get("found")]
    return {
        "market": req.market, "period": req.period,
        "start_ymd": req.start_ymd, "end_ymd": req.end_ymd,
        "dry_run": req.dry_run,
        "days_total": len(days),
        "days_with_package": len(hit),
        "added": sum(d.get("added", 0) for d in hit),
        "days": days,
    }
