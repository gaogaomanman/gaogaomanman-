"""AI工具合集 统一后端入口。

单一入口设计：仅一个服务（默认 :8080），七个模块按路径分区：
  /           导航页（前端 SPA）
  /dm         达梦数据库查询
  /sqlserver  SQL Server 查询
  /board      检测中心数据看板
  /personnel  人员能力表梳理
  /cma        一单一库核对
  /progress   抽采样进度统计

数据库访问为后端直连（达梦 dmPython + SQL Server pyodbc），不再需要 Node 中间池。
前端构建产物由本服务同源托管（frontend/dist）。
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core import db
from app.core.config import FRONTEND_DIST, settings
from app.modules.board.routes import router as board_router
from app.modules.cma.routes import router as cma_router
from app.modules.dm.routes import router as dm_router
from app.modules.limsrules.routes import router as limsrules_router
from app.modules.mirror.routes import router as mirror_router
from app.modules.nav.routes import router as nav_router
from app.modules.personnel.routes import router as personnel_router
from app.modules.progress import service as progress_service
from app.modules.progress.routes import router as progress_router
from app.modules.sqlserver.routes import router as sqlserver_router
from app.mirror import runner
from app.mirror import store as mirror_store
from app.query import is_mirror_mode


async def _release_watcher() -> None:
    """后台守护：响应其他实例的「让出 current.sqlite 句柄」请求。

    多实例共存的必需一环——生产实例做快照原子切换前会写 `release.request`，
    本任务看到新请求就 `dispose_engines()` 释放句柄并回 ack，
    否则对方的 `os.replace` 会一直以 WinError 32 失败（历史事故见 store.promote 注释）。

    **不受 `SCHEDULER_ENABLED` 控制**：任何实例都可能占着镜像库，
    长期开着的调试实例尤其必须参与，它正是 2026-09-21/22 两晚的占用方。
    1.5 秒轮询、每次只读一个几十字节的文件，开销可忽略。
    """
    while True:
        try:
            await asyncio.to_thread(mirror_store.release_readers)
        except Exception:  # noqa: BLE001  守护任务不允许抛错
            pass
        await asyncio.sleep(1.5)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # 启动即校验关键配置（fail fast）
    settings.validate_required()

    # 让出句柄守护：先于调度开关启动，调试实例也要跑（见函数注释）
    release_task = asyncio.create_task(_release_watcher())

    sync_task: asyncio.Task | None = None

    # scheduler_enabled=False 的实例（开发/调试用）不跑任何后台定时任务，
    # 避免与生产实例重复搬数、重复重算。
    if not settings.scheduler_enabled:
        print("[scheduler] SCHEDULER_ENABLED=false：本实例不启动任何定时任务（开发模式）")
        try:
            yield
        finally:
            release_task.cancel()
            db.dm_close_all()
        return

    if is_mirror_mode() and not mirror_store.CURRENT_DB.exists():
        # 镜像模式下若无快照，应用无法取数——启动即触发一次同步（后台执行）
        asyncio.create_task(asyncio.to_thread(runner.sync_now, trigger="startup"))

    at = (settings.mirror_sync_at or "").strip()
    if at:
        try:
            _hh, _mm = (int(x) for x in at.split(":", 1))
            if not (0 <= _hh < 24 and 0 <= _mm < 60):
                raise ValueError(at)
        except ValueError:
            print(f"[mirror] MIRROR_SYNC_AT 格式应为 HH:MM，收到 {at!r}，定时同步未启用")
        else:

            async def _daily_sync_loop(hh: int, mm: int) -> None:
                """每天 HH:MM（本地时间）同步一次：应用层不连源库，数据新鲜度全靠这里。"""
                while True:
                    now = datetime.now()
                    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if target <= now:
                        target += timedelta(days=1)
                    await asyncio.sleep((target - now).total_seconds())
                    try:
                        await asyncio.to_thread(runner.sync_now, trigger="schedule")
                    except Exception:  # noqa: BLE001  失败不影响服务，状态见 /api/mirror/status
                        pass

            sync_task = asyncio.create_task(_daily_sync_loop(_hh, _mm))

    # 抽采样进度：完成量每天重算（必须晚于镜像同步，否则重算的仍是昨天的快照）。
    # 留空即只允许手动同步；失败不影响服务，状态见 /api/progress/sync-log。
    progress_task: asyncio.Task | None = None
    p_at = (settings.progress_sync_at or "").strip()
    if p_at:
        try:
            _phh, _pmm = (int(x) for x in p_at.split(":", 1))
            if not (0 <= _phh < 24 and 0 <= _pmm < 60):
                raise ValueError(p_at)
        except ValueError:
            print(f"[progress] PROGRESS_SYNC_AT 格式应为 HH:MM，收到 {p_at!r}，定时重算未启用")
        else:

            async def _daily_progress_loop(hh: int, mm: int) -> None:
                while True:
                    now = datetime.now()
                    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if target <= now:
                        target += timedelta(days=1)
                    await asyncio.sleep((target - now).total_seconds())
                    try:
                        await progress_service.sync_year(datetime.now().year)
                    except Exception:  # noqa: BLE001  失败已在 sync_log 留痕
                        pass

            progress_task = asyncio.create_task(_daily_progress_loop(_phh, _pmm))

    yield
    if sync_task is not None:
        sync_task.cancel()
    if progress_task is not None:
        progress_task.cancel()
    release_task.cancel()
    db.dm_close_all()


app = FastAPI(
    title=settings.app_name,
    description="AI工具合集（FastAPI + Vue3 统一工程；达梦/SQL Server 直连，单一入口路径分区）",
    version="1.0.0",
    lifespan=lifespan,
)

# 单服务同源，理论上无需 CORS；保留全放行以便局域网/开发调试（无 Cookie 鉴权场景）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _router in (nav_router, dm_router, sqlserver_router, board_router, personnel_router, cma_router, progress_router, mirror_router, limsrules_router):
    app.include_router(_router)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name}


@app.get("/api/app-info")
async def app_info() -> dict:
    return {
        "app": settings.app_name,
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/api/health",
        "modules": {
            "nav": "/",
            "dm": "/dm",
            "sqlserver": "/sqlserver",
            "board": "/board",
            "personnel": "/personnel",
            "cma": "/cma",
            "progress": "/progress",
            "limsrules": "/limsrules",
        },
    }


# 静态托管前端构建产物（放在所有 /api 路由之后）
# 前端为 history 模式的 SPA（/dm、/board … 都是前端路由），因此：
#   - 构建产物中的静态资源按真实路径返回；
#   - 其余非 /api 路径回退到 index.html，交给 vue-router 解析。
if FRONTEND_DIST.exists():
    _ASSETS_DIR = FRONTEND_DIST / "assets"
    if _ASSETS_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_ASSETS_DIR)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (FRONTEND_DIST / full_path).resolve()
        try:
            inside = candidate.is_relative_to(FRONTEND_DIST.resolve())
        except AttributeError:  # pragma: no cover - Python < 3.9
            inside = str(candidate).startswith(str(FRONTEND_DIST.resolve()))
        if full_path and inside and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(FRONTEND_DIST / "index.html"))

else:

    @app.get("/")
    async def root() -> dict:
        return {
            "app": settings.app_name,
            "message": "前端尚未构建：请执行 cd frontend && npm run build",
            "docs": "/docs",
        }
