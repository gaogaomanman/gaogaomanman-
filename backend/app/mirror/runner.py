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


def sync_now(mode: str = "snapshot", sources: list[str] | None = None,
             trigger: str = "manual") -> dict:
    """执行一次同步（阻塞）。

    mode="inplace"：WAL 原地更新 current（约 1 分钟，手动按钮用）；
    mode="snapshot"：快照式（约 5 分钟，每晚 19:00 定时任务用，保留历史存档）。
    sources=None 时取配置 `mirror_sync_sources`（默认仅达梦）。
    trigger：schedule / manual / startup，仅用于留痕，便于事后区分"是谁触发的"。
    已在同步中则直接返回不重复执行。
    """
    if not _lock.acquire(blocking=False):
        return {"started": False, "reason": "已有同步在进行中"}
    started = datetime.now()
    _state.update(
        syncing=True,
        started_at=started.isoformat(timespec="seconds"),
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
        _record(trigger, mode, sources, res)
        return {"started": True, "result": res}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:500]
        _state["last_error"] = msg
        _state["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _record(trigger, mode, sources, None, error=msg)
        return {"started": True, "error": str(exc)[:300]}
    finally:
        _state["syncing"] = False
        _lock.release()


def _record(trigger: str, mode: str, sources: list[str] | None, res: dict | None,
            error: str | None = None) -> None:
    """把本次同步结果落盘（`sync_history.log`）。

    原先失败原因只留在内存 `last_error` 里，服务一重启就"查无此错"——
    2026-09-21/22 两晚的定时同步失败正是因此只能靠磁盘残留反推。
    现在历史可查，失败是"哪一晚、什么原因、占用进程是谁"一目了然。
    """
    from app.mirror import store

    data = res or {}
    failed = [t.get("table") for t in data.get("tables", []) if t.get("status") != "ok"]
    store.append_history({
        "at": datetime.now().isoformat(timespec="seconds"),
        "trigger": trigger,
        "mode": mode,
        "ok": error is None and bool(data.get("ok")),
        "promoted": data.get("promoted"),
        "snapshot": data.get("snapshot"),
        "elapsed_sec": data.get("elapsed_sec"),
        "size_mb": data.get("size_mb"),
        "tables_failed": failed or None,
        "error": error,
    })
