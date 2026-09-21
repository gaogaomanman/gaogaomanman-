"""镜像管理接口：状态查询 + 手动同步。

只有这里的同步逻辑（以及定时任务）允许连接源库；业务查询一律走镜像。
"""
from __future__ import annotations

import threading

from fastapi import APIRouter
from pydantic import BaseModel

from app.mirror import runner, store
from app.query import mirror_status

router = APIRouter(prefix="/api/mirror", tags=["mirror"])


@router.get("/status")
async def status() -> dict:
    """镜像状态：模式 / 数据截止时间 / 快照文件 / 同步进度与最近一次结果。"""
    snapshots = [
        {"name": f.name, "size_mb": round(f.stat().st_size / 1024 / 1024, 1)}
        for f in store.list_snapshots()
    ]
    return {
        "success": True,
        **mirror_status(),
        "current_db": str(store.CURRENT_DB),
        "exists": store.CURRENT_DB.exists(),
        "snapshots": snapshots,
        "sync": runner.state(),
    }


class SyncBody(BaseModel):
    """mode="inplace"（默认，WAL 原地更新，约 1 分钟）| "snapshot"（快照式，约 5 分钟）。

    sources="dm"（默认）或 "dm,sqlserver"（连 SQL Server 一起重搬）。
    """

    mode: str | None = None
    sources: str | None = None


@router.post("/sync")
async def trigger_sync(body: SyncBody | None = None) -> dict:
    """手动触发一次同步（后台执行，立即返回；进度看 /api/mirror/status）。

    手动默认走 **WAL 原地更新**（不复制底本，约 1 分钟）；
    每晚 19:00 的定时任务走快照式（保留历史存档）。
    """
    if runner.state()["syncing"]:
        return {"success": False, "message": "已有同步在进行中"}
    mode = (body.mode if body else None) or "inplace"
    sources = None
    if body and body.sources:
        sources = [s.strip() for s in body.sources.split(",") if s.strip()]
    threading.Thread(target=runner.sync_now, kwargs={"mode": mode, "sources": sources}, daemon=True).start()
    if mode == "inplace":
        msg = "镜像原地更新已开始（后台执行，约 2~4 分钟）"
    else:
        msg = "快照同步已开始（后台执行，约 5 分钟）"
    return {"success": True, "message": msg, "mode": mode, "sources": sources or ["dm"]}
