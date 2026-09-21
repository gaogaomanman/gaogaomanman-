"""看板接口：等价原「数据看板」/api/dashboard/*，统一挂到 /api/board/*。

错误映射保持旧行为：数据访问失败 → HTTP 502 + `{detail: "..."}`（前端读 `detail`）。
"""
from __future__ import annotations

import datetime

from fastapi import APIRouter, HTTPException, Query

from app.core.errors import DbError
from app.modules.board import service

router = APIRouter(prefix="/api/board", tags=["board"])


def _year_param(year: int | None) -> int:
    if year is None:
        return datetime.datetime.now().year
    if not isinstance(year, int) or not (2000 <= year <= 2100):
        raise HTTPException(status_code=400, detail="year 参数非法")
    return year


def _handle(exc: DbError) -> HTTPException:
    return HTTPException(status_code=502, detail=exc.message)


@router.get("/years")
async def years() -> dict:
    try:
        return {"success": True, "years": await service.get_years()}
    except DbError as exc:
        raise _handle(exc) from exc


@router.get("/summary")
async def summary(year: int | None = Query(default=None)) -> dict:
    try:
        return {"success": True, "data": await service.get_summary(_year_param(year))}
    except DbError as exc:
        raise _handle(exc) from exc


@router.get("/monthly")
async def monthly(year: int | None = Query(default=None)) -> dict:
    try:
        return {"success": True, "data": await service.get_monthly(_year_param(year))}
    except DbError as exc:
        raise _handle(exc) from exc


@router.get("/product")
async def product(year: int | None = Query(default=None)) -> dict:
    try:
        return {"success": True, "data": await service.get_product(_year_param(year))}
    except DbError as exc:
        raise _handle(exc) from exc


@router.get("/completion")
async def completion(year: int | None = Query(default=None)) -> dict:
    try:
        return {"success": True, "data": await service.get_completion(_year_param(year))}
    except DbError as exc:
        raise _handle(exc) from exc


@router.get("/item-count")
async def item_count(year: int | None = Query(default=None)) -> dict:
    try:
        return {"success": True, "data": await service.get_item_count(_year_param(year))}
    except DbError as exc:
        raise _handle(exc) from exc


@router.get("/detect-rate")
async def detect_rate(year: int | None = Query(default=None)) -> dict:
    try:
        return {"success": True, "data": await service.get_detect_rate(_year_param(year))}
    except DbError as exc:
        raise _handle(exc) from exc


@router.get("/top")
async def top(year: int | None = Query(default=None)) -> dict:
    try:
        return {"success": True, "data": await service.get_top(_year_param(year))}
    except DbError as exc:
        raise _handle(exc) from exc


@router.get("/risk")
async def risk(year: int | None = Query(default=None)) -> dict:
    try:
        return {"success": True, "data": await service.get_risk(_year_param(year))}
    except DbError as exc:
        raise _handle(exc) from exc


@router.get("/region")
async def region(year: int | None = Query(default=None)) -> dict:
    try:
        return {"success": True, "data": await service.get_region(_year_param(year))}
    except DbError as exc:
        raise _handle(exc) from exc


@router.get("/product-risk")
async def product_risk(year: int | None = Query(default=None)) -> dict:
    try:
        return {"success": True, "data": await service.get_product_risk(_year_param(year))}
    except DbError as exc:
        raise _handle(exc) from exc


@router.get("/history")
async def history(
    y1: int = Query(default=2020),
    y2: int = Query(default=2025),
    year: int | None = Query(default=None),
    overview: bool = Query(default=False),
) -> dict:
    """历史统计（2020–2025，来源 LIMS/SQL Server 镜像）。

    2020–2024 全年；2025 年为 LIMS 尚存数据（系统切换年起不完整）。
    传 year 时额外返回该年的明细（高风险/超标项目项次 + 样品类型分布）。
    口径见 service.get_history 的文档注释；年份范围可用 y1/y2 调整。
    """
    if not (2000 <= y1 <= y2 <= 2100):
        raise HTTPException(status_code=400, detail="y1/y2 参数非法")
    if year is not None and not (y1 <= year <= y2):
        raise HTTPException(status_code=400, detail="year 超出范围")
    try:
        return {"success": True, "data": await service.get_history(y1, y2, year, overview=overview)}
    except DbError as exc:
        raise _handle(exc) from exc
