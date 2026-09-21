"""人员能力表接口：等价原 人员能力表梳理/server.js（:3007）的两个模板，另加能力表上传。

响应字段与旧实现一致：`{success, name|keyword, total, rows, dbCount, errors}`；
`errors.dm` / `errors.sql` 供前端渲染「达梦 / SQL Server」两个状态灯（部分失败不影响另一库）。

**能力表比对**（本次新增）：页面上传的《检验检测能力表》由前端解析成「项目 + 标准」明细后
落到 `/ability-table`，卡1 查询结果每行附带 `inAbility`（在/不在）与 `inAbilityNote`（宽松命中的差异原因）。
未上传能力表时两者为空串，前端不渲染该列。
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter
from pydantic import BaseModel

from app.modules.personnel import ability_store, service

router = APIRouter(prefix="/api/personnel", tags=["personnel"])


class NameBody(BaseModel):
    name: str = ""


class KeywordBody(BaseModel):
    keyword: str = ""


class AbilityItem(BaseModel):
    """能力表条目（与前端 JSON 字段名一致）。"""

    project: str = ""
    method: str = ""


class AbilityTableBody(BaseModel):
    fileName: str = ""
    items: list[AbilityItem] = []


@router.post("/query-by-person")
async def query_by_person(body: NameBody) -> dict:
    name = (body.name or "").strip()
    if not name:
        return {"success": False, "error": "请输入检测人员姓名"}
    result = await service.person_capability(name)
    return {
        "success": True,
        "name": name,
        "total": len(result["rows"]),
        "rows": result["rows"],
        "dbCount": result["dbCount"],
        "errors": result["errors"],
    }


@router.post("/query-by-project")
async def query_by_project(body: KeywordBody) -> dict:
    keyword = (body.keyword or "").strip()
    if not keyword:
        return {"success": False, "error": "请输入检测项目或检测标准关键词"}
    result = await service.project_person(keyword)
    return {
        "success": True,
        "keyword": keyword,
        "total": len(result["rows"]),
        "rows": result["rows"],
        "dbCount": result["dbCount"],
        "errors": result["errors"],
    }


# ==================== 能力表（上传 / 状态 / 清除）====================


@router.post("/ability-table")
async def upload_ability_table(body: AbilityTableBody) -> dict:
    """整表替换保存能力表（页面上"上传/替换"）。

    失败（条目为空 / 超上限 / 写库异常）返回 `success=False` + 可读文案，
    且**不覆盖**上一次已生效的能力表——后端在同一个事务里做 DELETE + INSERT，异常自动回滚。
    """
    try:
        state = ability_store.save(body.fileName, [item.model_dump() for item in body.items])
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except sqlite3.Error as exc:
        return {"success": False, "error": f"能力表写入失败：{exc}"}
    return {
        "success": True,
        "message": f"能力表已保存：{state['fileName']}（{state['count']} 条）",
        **state,
    }


@router.get("/ability-table")
async def ability_table_status() -> dict:
    """能力表状态（页面加载时回显：文件名、上传时间、条目数、唯一项目数）。"""
    return {"success": True, **ability_store.status()}


@router.delete("/ability-table")
async def clear_ability_table() -> dict:
    """清除已生效的能力表（页面「清除」）。"""
    try:
        ability_store.clear()
    except sqlite3.Error as exc:
        return {"success": False, "error": f"清除能力表失败：{exc}"}
    return {"success": True, "message": "能力表已清除", "cleared": True, **ability_store.status()}
