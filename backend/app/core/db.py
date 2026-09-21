"""数据访问层：达梦（dmPython）与 SQL Server（pyodbc）直连。

设计要点（依据 W-3 验证结论）：
- 行结构统一为**位置数组**（tuple / pyodbc.Row → list），与旧 Node 中间池
  `rows: [[...]]` 完全一致，前端与业务层无需改行结构。
- 达梦侧维护轻量连接池（等价旧中间池 poolMin=1 / poolMax=6）。
- SQL Server 侧依赖 ODBC 驱动管理器的连接池（pyodbc 默认 pooling），连接随用随关。
- 所有阻塞调用通过 asyncio.to_thread 包装，避免阻塞事件循环。
- 结果统一经 json_safe 处理（Decimal / datetime / bytes）。
- 统一提供**参数化查询**（达梦 `?`、SQL Server `?`），禁止拼接用户输入。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Sequence

from app.core import dm_driver
from app.core.config import settings
from app.mirror import store
from app.query import is_mirror_mode, try_mirror
from app.core.errors import DbError
from app.core.readonly import assert_dm_readonly, assert_mssql_readonly
from app.core.serialize import json_safe, json_safe_row

Params = Sequence[Any] | None


# ==================== 达梦 ====================


class _DmPool:
    """达梦连接池：空闲连接复用；出错时按健康探测决定回收或丢弃。"""

    def __init__(self) -> None:
        self._idle: list[Any] = []
        self._lock = threading.Lock()
        self._max = max(1, settings.dm_pool_max)

    def _connect(self):
        driver = dm_driver.get_driver()
        try:
            return driver.connect(
                user=settings.dm_user,
                password=settings.dm_password,
                server=settings.dm_host,
                port=settings.dm_port,
            )
        except Exception as exc:  # noqa: BLE001
            raise DbError(f"达梦连接失败: {exc}") from exc

    def acquire(self):
        with self._lock:
            if self._idle:
                return self._idle.pop()
        return self._connect()

    def release(self, conn: Any, ok: bool) -> None:
        if not ok and not self._ping(conn):
            self._close(conn)
            return
        with self._lock:
            if len(self._idle) < self._max:
                self._idle.append(conn)
                return
        self._close(conn)

    @staticmethod
    def _ping(conn: Any) -> bool:
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT 1 AS T FROM DUAL")
                cur.fetchone()
            finally:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _close(conn: Any) -> None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def close_all(self) -> None:
        with self._lock:
            conns, self._idle = self._idle, []
        for c in conns:
            self._close(c)


_dm_pool = _DmPool()


def dm_execute(sql: str, params: Params = None, *, check_readonly: bool = True) -> dict:
    """执行达梦查询（阻塞），返回 {columns, rows}。"""
    if check_readonly:
        assert_dm_readonly(sql)
    conn = _dm_pool.acquire()
    ok = False
    try:
        cur = conn.cursor()
        try:
            if params:
                cur.execute(sql, tuple(params))
            else:
                cur.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            raw = cur.fetchall()
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
        ok = True
        return {"columns": columns, "rows": [json_safe_row(r) for r in raw]}
    except DbError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DbError(f"达梦查询失败: {exc}") from exc
    finally:
        _dm_pool.release(conn, ok)


def dm_query(sql: str) -> dict:
    return dm_execute(sql)


def dm_close_all() -> None:
    _dm_pool.close_all()


# ==================== SQL Server ====================


def _sql_connect():
    import pyodbc

    cs = (
        f"DRIVER={{{settings.sql_odbc_driver}}};"
        f"SERVER={settings.sql_server},{settings.sql_port};"
        f"DATABASE={settings.sql_database};"
        f"UID={settings.sql_user};PWD={settings.sql_password}"
    )
    try:
        return pyodbc.connect(cs, timeout=settings.sql_timeout)
    except Exception as exc:  # noqa: BLE001
        raise DbError(f"SQL Server 连接失败: {exc}") from exc


def sql_execute(sql: str, params: Params = None, *, check_readonly: bool = True) -> dict:
    """执行 SQL Server 查询（阻塞），返回 {columns, rows}。"""
    if check_readonly:
        assert_mssql_readonly(sql)
    conn = _sql_connect()
    try:
        cur = conn.cursor()
        try:
            if params:
                cur.execute(sql, tuple(params))
            else:
                cur.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            raw = cur.fetchall()
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
        return {"columns": columns, "rows": [json_safe_row(r) for r in raw]}
    except DbError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DbError(f"SQL Server 查询失败: {exc}") from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def sql_query(sql: str) -> dict:
    return sql_execute(sql)


# ==================== 健康检查 ====================


def dm_health() -> dict:
    """达梦"连通性"。

    镜像模式下**不连达梦**：以镜像快照是否存在为准。
    架构约定：应用层只连镜像；源库只允许同步进程连接。
    """
    if is_mirror_mode():
        ok = store.CURRENT_DB.exists()
        return {
            "ok": ok,
            "mirror": True,
            "data_deadline": store.data_deadline() if ok else None,
            "error": None if ok else "镜像快照不存在（尚未同步）",
        }
    try:
        dm_execute("SELECT 1 AS T FROM DUAL")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def sql_health() -> dict:
    """SQL Server"连通性"（镜像模式策略同 `dm_health`：不连源库，看快照）。"""
    if is_mirror_mode():
        ok = store.CURRENT_DB.exists()
        return {
            "ok": ok,
            "mirror": True,
            "data_deadline": store.data_deadline() if ok else None,
            "error": None if ok else "镜像快照不存在（尚未同步）",
        }
    try:
        sql_execute("SELECT 1 AS T")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ==================== 异步包装 ====================


async def adm(sql: str, params: Params = None) -> dict:
    """达梦查询。

    镜像模式（DATA_MODE=mirror，默认）下**只走镜像**：SQL 翻译为 SQLite 可执行形式后跑快照。
    无法在镜像上执行（无快照 / 含不可翻译构造 / 元数据查询 / 执行失败）时抛
    `MirrorUnavailable`（DbError 子类），**不回退直连**——
    架构约定：应用层只连镜像，源库只允许同步进程连接。
    只有 DATA_MODE=direct 时才走源库直连（用于核对与调试）。
    """
    hit = await asyncio.to_thread(try_mirror, sql, "dm", params, is_mirror_mode())
    if hit is not None:
        return hit
    return await asyncio.to_thread(dm_execute, sql, params)


async def asql(sql: str, params: Params = None) -> dict:
    """SQL Server 查询（策略同 `adm`：镜像模式只走镜像，不回退直连）。"""
    hit = await asyncio.to_thread(try_mirror, sql, "sqlserver", params, is_mirror_mode())
    if hit is not None:
        return hit
    return await asyncio.to_thread(sql_execute, sql, params)


# 兼容别名（语义更明确）
adm_query = adm
asql_query = asql


async def adm_health() -> dict:
    return await asyncio.to_thread(dm_health)


async def asql_health() -> dict:
    return await asyncio.to_thread(sql_health)


def scalar(row: Any, idx: int = 0) -> Any:
    """安全取位置数组的第 idx 列。"""
    try:
        return json_safe(row[idx])
    except Exception:  # noqa: BLE001
        return None
