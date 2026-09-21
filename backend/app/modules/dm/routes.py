"""达梦查询模块：等价原 20260605160640/server.js（:3001）。

响应风格刻意与旧实现一致（业务错误返回 200 + `{success:false, error}`），
以便前端页面逻辑与提示文案原样保留。

保持等价的旧行为说明：
- `/idle-time` 的 `remaining` 旧实现恒为 300（写死），非真实空闲时间，此处等价保留。
- `/schema` 旧实现直接返回账号授权 schema 且 `tables` 恒为空数组，此处等价保留。
- SQL 校验：旧链路端到端只放行 SELECT/WITH（中间池比页面层更严），此处一致。
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.core import db
from app.core.config import settings
from app.core.errors import DbError, ReadOnlyError
from app.query import mirror_status

router = APIRouter(prefix="/api/dm", tags=["dm"])

# 与旧实现一致：空闲倒计时展示值（旧实现为写死常量 300）
LEGACY_IDLE_SECONDS = 300


class SqlBody(BaseModel):
    sql: str = ""


def _fail(message: str) -> dict:
    return {"success": False, "error": message}


@router.post("/query")
async def query(body: SqlBody) -> dict:
    try:
        result = await db.adm(body.sql or "")
    except ReadOnlyError as exc:
        return _fail(exc.message)
    except DbError as exc:
        return _fail(exc.message)
    return {
        "success": True,
        "type": "query",
        "columns": result["columns"],
        "rows": result["rows"],
        "rowCount": len(result["rows"]),
        # 数据来源透出：页面据此显示「镜像模式 / 直连模式」与数据截止时间。
        # 镜像不可用时会自动回退直连——必须让使用者看得见，不做静默降级。
        "mode": result.get("mode") or "direct:dm",
        "dataDeadline": mirror_status()["data_deadline"],
    }


@router.post("/connect")
async def connect() -> dict:
    health = await db.adm_health()
    if health.get("ok"):
        return {"success": True, "message": "连接成功"}
    return _fail(f"达梦连接失败: {health.get('error')}")


@router.post("/auto-connect")
async def auto_connect() -> dict:
    health = await db.adm_health()
    if health.get("ok"):
        resp: dict = {
            "success": True,
            "message": "连接成功",
            "host": settings.dm_host,
            "port": settings.dm_port,
            "user": settings.dm_user,
            "schema": settings.dm_schema,
        }
        # 镜像模式下页面应展示镜像信息而非源库地址（数据并不来自达梦）
        if health.get("mirror"):
            resp["mirror"] = True
            resp["dataDeadline"] = health.get("data_deadline")
        return resp
    return _fail(f"达梦连接失败: {health.get('error')}")


@router.post("/disconnect")
async def disconnect() -> dict:
    # 旧实现为空操作（连接由服务端持有），保持等价
    return {"success": True}


@router.get("/idle-time")
async def idle_time() -> dict:
    health = await db.adm_health()
    if health.get("ok"):
        return {"connected": True, "remaining": LEGACY_IDLE_SECONDS}
    return {"connected": False, "remaining": 0}


@router.get("/schema")
async def schema() -> dict:
    # 旧实现：不查 ALL_TABLES，直接返回授权 schema，tables 恒为空数组
    return {"success": True, "schemas": [{"schemaName": settings.dm_schema, "tables": []}]}


@router.get("/tables")
async def tables(schema: str | None = Query(default=None)) -> dict:
    owner = (schema or settings.dm_schema).upper()
    try:
        result = await db.adm(
            "SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER = ? ORDER BY TABLE_NAME", (owner,)
        )
    except DbError as exc:
        return _fail(exc.message)
    return {"success": True, "rows": [[db.scalar(r, 0)] for r in result["rows"]]}


@router.get("/table-columns")
async def table_columns(
    table: str | None = Query(default=None), schema: str | None = Query(default=None)
) -> dict:
    if not table:
        return _fail("请指定表名")
    owner = (schema or settings.dm_schema).upper()
    tname = table.upper()
    try:
        result = await db.adm(
            "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE FROM ALL_TAB_COLUMNS "
            "WHERE OWNER = ? AND TABLE_NAME = ? ORDER BY COLUMN_ID",
            (owner, tname),
        )
    except DbError as exc:
        return _fail(exc.message)

    columns: list[dict] = []
    for row in result["rows"]:
        nullable_raw = db.scalar(row, 3)
        columns.append(
            {
                "name": db.scalar(row, 0) or "",
                "type": db.scalar(row, 1) or "",
                "length": int(db.scalar(row, 2) or 0),
                "nullable": (str(nullable_raw).upper() != "N") if nullable_raw is not None else True,
            }
        )
    return {"success": True, "columns": columns}
