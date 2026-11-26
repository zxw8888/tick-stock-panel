"""推荐策略包 API — 清单/状态、安装与首次提示确认。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import recommended_strategies

router = APIRouter(prefix="/api/strategy-recommended", tags=["strategy-recommended"])
logger = logging.getLogger(__name__)


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


@router.get("")
def get_recommended(request: Request) -> dict:
    try:
        return recommended_strategies.list_recommended(_data_dir(request))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class InstallIn(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=50)
    overwrite: bool = False


@router.post("/install")
def install_recommended(req: InstallIn, request: Request) -> dict:
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="策略引擎未初始化")
    try:
        result = recommended_strategies.install_recommended(
            _data_dir(request),
            req.ids,
            overwrite=req.overwrite,
            engine=engine,
        )
    except recommended_strategies.RecommendedConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        from app.api.strategy import _invalidate_strategy_runtime

        _invalidate_strategy_runtime(request)
    except Exception as exc:  # noqa: BLE001
        logger.warning("recommended install cache invalidation failed: %s", exc)
    return result


class PromptAckIn(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=50)


@router.post("/prompt-ack")
def ack_prompt(req: PromptAckIn, request: Request) -> dict:
    try:
        return recommended_strategies.ack_prompt(_data_dir(request), req.ids)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
