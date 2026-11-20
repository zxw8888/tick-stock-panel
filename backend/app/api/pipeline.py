"""盘后管道 API — 异步触发 + 进度跟踪。"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import logging

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.asset_types import ASSET_TYPES
from app.jobs import daily_pipeline
from app.market_time import cn_today
from app.services import preferences, symbol_sync as symbol_sync_service
from app.tickflow.capabilities import Cap
from app.services.pipeline_jobs import (
    JobCancelledError,
    job_store,
    release_run_slot,
    run_with_capacity,
    try_acquire_run_slot,
)
from app.api.data import invalidate_storage_cache

# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class SymbolSyncBody(BaseModel):
    """单标的同步请求体。asset_type 可省略，服务端按标的维表解析并强校验。"""

    model_config = {"extra": "forbid"}

    symbol: str = Field(min_length=1, max_length=32)
    asset_type: str | None = None
    granularity: str
    range: str
    start_date: str | None = None
    end_date: str | None = None


def _parse_iso_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} 必须是 YYYY-MM-DD 格式")
    if parsed.isoformat() != value:
        raise HTTPException(400, f"{field} 必须是标准 YYYY-MM-DD 格式")
    return parsed


def _daily_sync_allowed(capset) -> bool:
    """日K可由 TickFlow 或自定义日K源提供；LOF 走东财，不受此门禁。"""
    if capset.has(Cap.KLINE_DAILY_BATCH):
        return True
    from app.data_providers import custom as custom_sources

    return custom_sources.provider_has_dataset(preferences.get_daily_data_provider(), "daily")


def _minute_sync_allowed(capset) -> bool:
    from app.services import kline_sync

    provider = kline_sync._selected_minute_provider_name()
    if provider == "tickflow":
        return capset.has(Cap.KLINE_MINUTE_BATCH)
    _, fallback, error = kline_sync._resolve_minute_provider(provider)
    return error is None and not fallback


@router.post("/run")
async def run_now(request: Request) -> dict:
    """异步触发盘后管道,立即返回 job_id。客户端轮询 /jobs/{id} 拿进度。"""
    return await trigger_pipeline_job(
        request.app.state.repo,
        request.app.state.capabilities,
        getattr(request.app.state, "quote_service", None),
    )


async def trigger_pipeline_job(repo, capset, quote_service=None) -> dict:
    """触发盘后管道后台任务(HTTP 端点与 AI 助手共用同一条触发路径)。

    若已有任务在跑,**返回该任务 id 而不是开新任务**(防止并发拉数据撞限流)。
    卡死判定按「进度停滞」而非总时长(慢带宽下长任务不会被误杀), 见 reap_stale。
    """
    return await _submit_job(
        repo, quote_service,
        lambda progress: (lambda: daily_pipeline.run_now(repo, capset, on_progress=progress)),
    )


async def _submit_job(repo, quote_service, make_run, *, long_running: bool = False) -> dict:
    """任务化执行公共机制: 单飞 create → 重任务槽 → 行情暂停 → 缓存失效。

    make_run(progress) 返回实际执行闭包(阻塞 IO + CPU, 在 executor 里跑)。
    盘后管道与独立除权因子同步共用 —— 两者写同一批 parquet, 必须共享
    单飞与重任务槽防并发覆写。
    """
    # 检测卡死的 running job (如 reload 后孤儿 task / 网络读无限阻塞)。
    # reap_stale 会在 /run 和 /jobs/{id} 轮询端点都调用,保证卡死后能自愈。
    job_store.reap_stale()

    # 单飞: 复用任何活跃 (pending∨running) 任务, is_new=False 时不再调度新任务
    job_id, is_new = job_store.create(long_running=long_running)
    if not is_new:
        return {"job_id": job_id, "reused": True}

    def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                 skip_log: bool = False) -> None:
        job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

    run = make_run(progress)

    # 在 executor 里跑同步任务(pipeline 内部都是阻塞 IO + CPU)
    async def task() -> None:
        # 重任务执行槽: 防僵尸并发(reap 后线程仍活时新任务不得并行写 parquet)
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        # 任务运行期间暂停实时行情取数, 防止覆写同一批 parquet 竞态
        qs = quote_service
        try:
            loop = asyncio.get_event_loop()

            def _run() -> dict:
                try:
                    if qs:
                        with qs.paused():
                            return run()
                    return run()
                finally:
                    repo.refresh_cache()

            result = await loop.run_in_executor(_long_task_executor, run_with_capacity, job_id, _run)
            job_store.succeed(job_id, result)
            invalidate_storage_cache()
        except JobCancelledError:
            # 已被 reap/手动取消终止: job 状态已由 terminate() 写为 failed,
            # 拉取线程在分块回调处自行退出, 这里无需(也无法)再写状态。
            logger.warning("pipeline job %s cancelled", job_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("pipeline job failed")
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()
        finally:
            release_run_slot(job_id)

    asyncio.create_task(task())
    return {"job_id": job_id, "reused": False}


@router.post("/symbol-sync")
async def symbol_sync(request: Request, body: SymbolSyncBody) -> dict:
    """单个标的长时间 / 分钟K同步，任务化执行并复用统一进度轮询。"""
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    quote_service = getattr(request.app.state, "quote_service", None)

    try:
        symbol = symbol_sync_service.normalize_symbol(body.symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    resolved = repo.resolve_asset_type(symbol)
    asset_type = body.asset_type
    if asset_type is not None:
        if asset_type not in ASSET_TYPES:
            raise HTTPException(400, "不支持的资产类型")
        if resolved != "stock" and asset_type != resolved:
            raise HTTPException(400, "资产类型与标的不匹配")
    else:
        asset_type = resolved

    if body.granularity not in {"daily", "minute"}:
        raise HTTPException(400, "granularity 必须是 daily 或 minute")
    if body.range not in {"custom", "full"}:
        raise HTTPException(400, "range 必须是 custom 或 full")

    start_date: date | None = None
    end_date: date | None = None
    if body.range == "custom":
        if not body.start_date or not body.end_date:
            raise HTTPException(400, "自定义范围必须提供 start_date 与 end_date")
        start_date = _parse_iso_date(body.start_date, "start_date")
        end_date = _parse_iso_date(body.end_date, "end_date")
        if start_date < date(1990, 12, 19) or end_date > cn_today() or start_date > end_date:
            raise HTTPException(400, "日期范围不合法")
    elif body.start_date or body.end_date:
        raise HTTPException(400, "full 模式不接受起止日期")

    if body.granularity == "minute":
        if asset_type not in {"stock", "etf"}:
            raise HTTPException(400, "指数/LOF 不支持分钟K落库")
        if not _minute_sync_allowed(capset):
            raise HTTPException(403, "当前分钟数据源不可用")
    elif asset_type != "lof" and not _daily_sync_allowed(capset):
        raise HTTPException(403, "当前日K数据源不可用")

    return await _submit_job(
        repo, quote_service,
        lambda progress: (lambda: symbol_sync_service.run_symbol_sync(
            repo, capset,
            symbol=symbol,
            asset_type=asset_type,
            granularity=body.granularity,
            range_mode=body.range,
            start_date=start_date,
            end_date=end_date,
            on_progress=progress,
        )),
        long_running=(body.granularity == "minute" or body.range == "full"),
    )


@router.post("/adj-factor/run")
async def run_adj_factor_now(request: Request) -> dict:
    """独立同步除权因子(全历史 + 受影响个股 enriched 局部重算), 任务化+进度轮询。

    与盘后管道共用单飞与重任务槽 (两者写同一批 parquet); 门控与矩阵
    usable 同口径: TickFlow 档位不足且未路由到其他可用源时 400。
    """
    from app.services import preferences
    from app.tickflow.capabilities import Cap

    capset = request.app.state.capabilities
    if not (capset.has(Cap.ADJ_FACTOR) or preferences.get_adj_factor_provider() != "tickflow"):
        raise HTTPException(
            status_code=400,
            detail="除权因子能力不可用: 请先在 设置 → 数据源 中配置可用数据源",
        )
    repo = request.app.state.repo
    return await _submit_job(
        repo,
        getattr(request.app.state, "quote_service", None),
        lambda progress: (lambda: daily_pipeline.run_adj_factor_sync(repo, capset, on_progress=progress)),
    )


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    # 每次轮询都检查卡死 job — 前端持续轮询, 进度停滞超阈值后必定自愈,
    # 无需用户再次手动点「同步」。
    job_store.reap_stale()
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return j


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """手动取消一个 running 的 job(协作式: 拉取线程在当前分块完成后自行退出)。"""
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    if j["status"] not in ("running", "pending"):
        raise HTTPException(status_code=400, detail=f"job status is {j['status']}, cannot cancel")
    job_store.terminate(job_id, "用户手动取消")
    return {"cancelled": job_id}


@router.get("/jobs")
async def list_jobs(limit: int = 20) -> dict:
    # 常驻侧栏轮询只读内存，不排队等待长计算占用的同步路由线程。
    return job_store.recent_snapshot(limit)
