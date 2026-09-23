"""导航状态聚合：替代旧导航页的"前端跨端口轮询"。

旧实现：导航页 `setInterval(5000)` 分别探测 4000/3001/3003/3005/3007/3002 的首页与
中间池健康接口，再按 HTTP 结果切换状态灯文案。
新实现：同源单服务，状态由后端聚合返回，前端只负责把状态映射为同样的文案与样式：

- 中间池 → 统一为「数据源」状态：达梦与 SQL Server 均已连通 = `已连接 · 运行正常`
- 各工具 → 其依赖数据源可达则 `在线`；3002（一单一库核对）为独立工具，恒为 `独立工具`
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter

from app.core import db
from app.query import mirror_status

router = APIRouter(prefix="/api/nav", tags=["nav"])

# 状态缓存：避免导航页每 5 秒轮询都真实探库
_CACHE_TTL = 5.0
_cache: dict = {"ts": 0.0, "payload": None}


def _module_entries(dm_ok: bool, sql_ok: bool) -> list[dict]:
    return [
        {"key": "dm", "port": 3001, "name": "达梦数据库查询", "online": dm_ok, "standalone": False},
        {"key": "sqlserver", "port": 3003, "name": "SQL Server 查询", "online": sql_ok, "standalone": False},
        {"key": "board", "port": 3005, "name": "数据看板", "online": dm_ok, "standalone": False},
        {"key": "personnel", "port": 3007, "name": "人员能力表梳理", "online": dm_ok or sql_ok, "standalone": False},
        {"key": "cma", "port": 3002, "name": "一单一库核对", "online": True, "standalone": True},
        # 抽采样进度统计：完成量取自达梦镜像快照，故其可用性与达梦数据源一致
        {"key": "progress", "port": 8080, "name": "抽采样进度统计", "online": dm_ok, "standalone": False},
        # 查询模板规则：纯本地配置（SQLite），不依赖任何数据源，恒为可用
        {"key": "limsrules", "port": 8080, "name": "查询模板规则", "online": True, "standalone": True},
    ]


async def _probe() -> dict:
    dm, sql = await asyncio.gather(db.adm_health(), db.asql_health())
    dm_ok = bool(dm.get("ok"))
    sql_ok = bool(sql.get("ok"))
    return {
        "pool": {"ok": dm_ok and sql_ok},
        "data_sources": {
            "dm": {"ok": dm_ok, "error": dm.get("error")},
            "sql": {"ok": sql_ok, "error": sql.get("error")},
        },
        "modules": _module_entries(dm_ok, sql_ok),
        # 数据来源模式 + 数据截止时间：让使用者知道"现在看到的数据是什么时候的"。
        # 镜像模式下即使源库暂时不可达，页面依然可用——这一点必须显示出来，不能默默生效。
        "mirror": mirror_status(),
    }


@router.get("/status")
async def status() -> dict:
    now = time.time()
    cached = _cache.get("payload")
    if cached is not None and now - float(_cache["ts"]) < _CACHE_TTL:
        return {"success": True, **cached}
    payload = await _probe()
    _cache["ts"] = now
    _cache["payload"] = payload
    return {"success": True, **payload}
