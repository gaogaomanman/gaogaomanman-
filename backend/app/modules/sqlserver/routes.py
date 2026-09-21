"""SQL Server 查询模块：等价原 sqlserver-query-tool/server.js（:3003）。

响应风格与旧实现一致（业务错误 200 + `{success:false, error}`）。
表名无法参数化，改为**白名单字符校验 + 方括号包裹**（旧实现仅做了简单去引号）。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.core import db
from app.core.config import settings
from app.core.errors import DbError, ReadOnlyError
from app.query import mirror_status

router = APIRouter(prefix="/api/sqlserver", tags=["sqlserver"])

LEGACY_IDLE_SECONDS = 300

_IDENT_RE = re.compile(r"^[A-Za-z0-9_$#@ .\-\[\]]{1,128}$")


def _fail(message: str) -> dict:
    return {"success": False, "error": message}


def _safe_table(table: str | None) -> str:
    """表名安全处理：仅允许字母数字与常见标识符字符，返回可嵌入 SQL 的形式。"""
    name = (table or "").strip()
    if not name:
        raise ValueError("表名不能为空")
    if not _IDENT_RE.match(name):
        raise ValueError("表名包含非法字符")
    parts = [p.strip().strip("[]") for p in name.split(".") if p.strip()]
    return ".".join(f"[{p}]" for p in parts)


class SqlBody(BaseModel):
    sql: str = ""


class PreviewBody(BaseModel):
    table: str = ""
    limit: int = 100


@router.post("/query")
async def query(body: SqlBody) -> dict:
    try:
        result = await db.asql(body.sql or "")
    except ReadOnlyError as exc:
        return _fail(exc.message)
    except DbError as exc:
        return _fail(exc.message)
    return {
        "success": True,
        "rows": result["rows"],
        "columns": result["columns"],
        # 数据来源透出（同 dm 侧说明）
        "mode": result.get("mode") or "direct:sqlserver",
        "dataDeadline": mirror_status()["data_deadline"],
    }


@router.post("/connect")
async def connect() -> dict:
    health = await db.asql_health()
    if health.get("ok"):
        return {"success": True, "message": "连接成功"}
    return _fail(f"SQL Server 连接失败: {health.get('error')}")


@router.post("/auto-connect")
async def auto_connect() -> dict:
    health = await db.asql_health()
    if health.get("ok"):
        resp: dict = {
            "success": True,
            "message": "连接成功",
            "server": settings.sql_server,
            "port": settings.sql_port,
            "database": settings.sql_database,
        }
        # 镜像模式下页面应展示镜像信息而非源库地址（数据并不来自 SQL Server）
        if health.get("mirror"):
            resp["mirror"] = True
            resp["dataDeadline"] = health.get("data_deadline")
        return resp
    return _fail(f"SQL Server 连接失败: {health.get('error')}")


@router.post("/disconnect")
async def disconnect() -> dict:
    return {"success": True}


@router.get("/idle-time")
async def idle_time() -> dict:
    health = await db.asql_health()
    if health.get("ok"):
        return {"connected": True, "remaining": LEGACY_IDLE_SECONDS}
    return {"connected": False, "remaining": 0}


@router.get("/databases")
async def databases() -> dict:
    try:
        result = await db.asql("SELECT name FROM sys.databases ORDER BY name")
    except DbError as exc:
        return _fail(exc.message)
    return {"success": True, "rows": [{"name": db.scalar(r, 0)} for r in result["rows"]]}


@router.get("/tables")
async def tables() -> dict:
    try:
        result = await db.asql(
            "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME"
        )
    except DbError as exc:
        return _fail(exc.message)
    return {"success": True, "rows": [[db.scalar(r, 0), db.scalar(r, 1)] for r in result["rows"]]}


@router.get("/table-columns")
async def table_columns(table: str | None = Query(default=None)) -> dict:
    try:
        safe = _safe_table(table)
    except ValueError as exc:
        return _fail(str(exc))
    try:
        result = await db.asql(
            "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE "
            f"FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
            (safe.strip("[]").split(".")[-1],),
        )
    except DbError as exc:
        return _fail(exc.message)
    return {"success": True, "rows": result["rows"]}


@router.post("/table-preview")
async def table_preview(body: PreviewBody) -> dict:
    try:
        safe = _safe_table(body.table)
    except ValueError as exc:
        return _fail(str(exc))
    limit = max(1, min(int(body.limit or 100), 1000))
    try:
        result = await db.asql(f"SELECT TOP {limit} * FROM {safe}")
    except DbError as exc:
        return _fail(exc.message)
    return {"success": True, "columns": result["columns"], "rows": result["rows"]}
