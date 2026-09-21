"""同步执行器：镜像 → 源库 的唯一通道（应用层不直接连源库）。

线程安全：同一时刻只允许一个同步在跑（`threading.Lock` 非阻塞获取）。
同步在后台线程执行（约 8 分钟），期间业务查询继续读旧快照，
完成后原子切换 + 引擎失效检测，读取方无感。
"""
from __future__ import annotations

import threading
from datetime import datetime

_lock = threading.Lock()
_state: dict = {
    "syncing": False,
    "started_at": None,
    "finished_at": None,
    "last": None,       # 最近一次同步的汇总（run_full_sync 返回值）
    "last_error": None,
}


def state() -> dict:
    """同步状态快照（供 /api/mirror/status 与管理界面展示）。"""
    return dict(_state)


def sync_now(mode: str = "snapshot", sources: list[str] | None = None) -> dict:
    """执行一次同步（阻塞）。

    mode="inplace"：WAL 原地更新 current（约 1 分钟，手动按钮用）；
    mode="snapshot"：快照式（约 5 分钟，每晚 19:00 定时任务用，保留历史存档）。
    sources=None 时取配置 `mirror_sync_sources`（默认仅达梦）。
    已在同步中则直接返回不重复执行。
    """
    if not _lock.acquire(blocking=False):
        return {"started": False, "reason": "已有同步在进行中"}
    _state.update(
        syncing=True,
        started_at=datetime.now().isoformat(timespec="seconds"),
        last_error=None,
    )
    try:
        from app.mirror import sync

        if mode == "inplace":
            res = sync.run_inplace_sync(sources=sources)
        else:
            res = sync.run_full_sync(sources=sources)
        _state["last"] = res
        _state["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return {"started": True, "result": res}
    except Exception as exc:  # noqa: BLE001
        _state["last_error"] = str(exc)[:500]
        _state["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return {"started": True, "error": str(exc)[:300]}
    finally:
        _state["syncing"] = False
        _lock.release()
