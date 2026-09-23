"""SQLite 镜像存储层（快照管理 + 读取引擎）。

设计要点（对应 `output/数据库经SQLite镜像取数/01_问题定义.md` 的决策）：
- **一次同步 = 一个完整快照文件**，位于 `backend/data/mirror/snapshots/mirror_<时间戳>.sqlite`；
- 写完后**原子切换** `current.sqlite`，读取方永远只见完整快照，不会读到"半成品"；
- `sync_meta` 记录各表行数/耗时/状态，供导航页与看板展示「数据截止时间」；
- 保留历史快照（默认最近 10 个，可用环境变量调整），可对任一快照直接查询做回溯/对拍。

配置通过环境变量（默认值见下），后续如需并入 `app/core/config.py` 的 pydantic-settings 可直接迁移：
    MIRROR_ENABLED   是否启用镜像模式（1/0），默认 0（未启用时仍走直连）
    MIRROR_DIR       镜像根目录，默认 <backend>/data/mirror
    MIRROR_KEEP      快照保留个数，默认 10
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

# 镜像根目录：默认 backend/data/mirror（不可被静态托管访问）；
# 由 `MIRROR_DIR` 覆盖——**本机 C: 盘已满，生产上必须指向其他盘**，详见 config.py 注释。
BACKEND_DIR = Path(__file__).resolve().parents[2]
MIRROR_DIR = Path(settings.mirror_dir) if settings.mirror_dir else (BACKEND_DIR / "data" / "mirror")
SNAPSHOT_DIR = MIRROR_DIR / "snapshots"
CURRENT_DB = MIRROR_DIR / "current.sqlite"
KEEP_SNAPSHOTS = settings.mirror_keep

# 跨进程「让出句柄」协议（多实例共存必需，详见 promote 的注释）
RELEASE_FILE = MIRROR_DIR / "release.request"
ACK_DIR = MIRROR_DIR / "release.ack"
# 同步历史（JSON 行）——失败原因必须落盘：原先只存内存，进程一重启就查不到了
HISTORY_LOG = MIRROR_DIR / "sync_history.log"

# 同步元信息表（写在每个快照内）
META = MetaData()
SYNC_META = Table(
    "sync_meta", META,
    Column("snapshot", String, primary_key=True),
    Column("source", String),          # dm / sqlserver
    Column("table_name", String, primary_key=True),
    Column("row_count", Integer),
    Column("started_at", String),
    Column("finished_at", String),
    Column("status", String),          # ok / failed
    Column("error", String),
)

_engine_lock = threading.Lock()
_engines: dict[str, Engine] = {}


def ensure_dirs() -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def enabled() -> bool:
    """镜像模式是否启用（未启用时业务层仍走直连）。"""
    return bool(settings.mirror_enabled)


def snapshot_path(ts: datetime | None = None) -> Path:
    ts = ts or datetime.now()
    return SNAPSHOT_DIR / f"mirror_{ts.strftime('%Y%m%d_%H%M%S')}.sqlite"


def engine_for(db_file: Path, fast: bool = False) -> Engine:
    """取得（并缓存）某个 sqlite 文件的 SQLAlchemy 引擎。

    fast=True 为**批量装载专用**：关闭落盘同步、事务日志放内存、加大页缓存。
    仅用于写快照（该文件是可重建的中间产物，不需要崩溃持久性）；
    读取 current 时不要开，以免影响正常读一致性与磁盘占用。

    读取引擎用 `NullPool`（用完即关连接）——**这是多实例共存的关键**：
    连接池会把 current.sqlite 的句柄一直留着，而 Windows 上只要还有任何进程
    打开该文件，`promote()` 的 `os.replace` 就会以 WinError 32 失败。
    事故记录：2026-09-21/22 两晚 19:00 定时同步失败，根因就是长期开着的
    8090 调试实例（连接池常驻句柄）挡住了生产实例的原子切换。
    代价是每次查询重新打开库（本机开销很小，页面查询本就密集），
    用下面放大的页缓存（64MB）补偿。
    """
    key = f"{db_file}|{'fast' if fast else 'std'}"
    with _engine_lock:
        eng = _engines.get(key)
        if eng is None:
            eng = create_engine(
                f"sqlite:///{db_file}", future=True,
                poolclass=None if fast else NullPool,
            )
            if fast:

                @event.listens_for(eng, "connect")
                def _set_bulk_pragmas(dbapi_conn, _rec):  # pragma: no cover - 由 SQLite 回调
                    cur = dbapi_conn.cursor()
                    cur.execute("PRAGMA journal_mode=MEMORY")
                    cur.execute("PRAGMA synchronous=OFF")
                    cur.execute("PRAGMA temp_store=MEMORY")
                    cur.execute("PRAGMA cache_size=-200000")  # ≈200MB 页缓存
                    cur.close()

            else:

                @event.listens_for(eng, "connect")
                def _set_read_pragmas(dbapi_conn, _rec):  # pragma: no cover - 由 SQLite 回调
                    cur = dbapi_conn.cursor()
                    cur.execute("PRAGMA busy_timeout=10000")
                    cur.execute("PRAGMA cache_size=-65536")  # ≈64MB，补偿无连接池
                    cur.close()

            _engines[key] = eng
        return eng


_watch: dict[str, tuple[float, int]] = {}


def _invalidate_if_replaced(db_file: Path) -> None:
    """检测快照文件是否被换过，是则丢弃旧引擎。

    为什么必须做：`promote()` 的实现是**先删后替换**文件。若同步在**另一个进程**里跑
    （如定时任务用 CLI 执行），本进程已打开的 SQLite 句柄仍然指向"已被删除的旧文件"，
    于是出现**"同步明明成功了，页面数据却一直没变"**——一个很难排查的故障。
    （同进程内同步会走 `promote()`→`dispose_engines()`，不受影响；这里覆盖跨进程/外部同步。）
    """
    try:
        st = db_file.stat()
    except OSError:
        return
    key = str(db_file)
    sig = (st.st_mtime, st.st_size)
    old = _watch.get(key)
    if old is not None and old != sig:
        dispose_engines()
    _watch[key] = sig


def current_engine() -> Engine:
    """读取用引擎（指向 current.sqlite）。"""
    ensure_dirs()
    _invalidate_if_replaced(CURRENT_DB)
    return engine_for(CURRENT_DB)


def dispose_engines() -> None:
    """释放全部缓存引擎（原子切换后需调用，避免继续持有旧文件句柄）。"""
    with _engine_lock:
        for eng in _engines.values():
            try:
                eng.dispose()
            except Exception:  # noqa: BLE001
                pass
        _engines.clear()


# ==================== 跨进程「让出句柄」协议 ====================
# promote() 要把 tmp 原子替换成 current.sqlite，而 Windows 要求此时**没有任何
# 进程**打开该文件。本进程的句柄靠 dispose_engines() 释放，但别的实例（例如
# 长期开着的 8090 调试实例）不受本进程控制，于是每晚定时同步都会失败。
#
# 协议：promote 前写 release.request（含 nonce）→ 各实例的守护任务看到新 nonce
# 就 dispose_engines() 并写 release.ack/<pid> → promote 等到替换成功为止。
# 协议文件放在镜像目录里（它本身就是"锁"所在处），无需额外配置。

_handled_nonce = ""


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def request_release() -> str:
    """发出让出请求，返回本次 nonce。"""
    ensure_dirs()
    nonce = f"{os.getpid()}-{time.time():.3f}"
    try:
        RELEASE_FILE.write_text(
            json.dumps({"nonce": nonce, "pid": os.getpid(), "at": _ts()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return nonce


def read_acks() -> list[int]:
    """已响应当前让出请求的实例 PID（排障用）。"""
    pids: list[int] = []
    try:
        for f in ACK_DIR.glob("*.ack"):
            try:
                pids.append(int(f.stem))
            except ValueError:
                pass
    except OSError:
        pass
    return sorted(pids)


def clear_release() -> None:
    """撤下让出请求并清掉 ack（替换结束后调用）。"""
    try:
        RELEASE_FILE.unlink()
    except OSError:
        pass
    try:
        for f in ACK_DIR.glob("*.ack"):
            f.unlink()
    except OSError:
        pass


def release_readers() -> dict:
    """守护任务调用：收到让出请求就释放本进程读句柄并回 ack。

    所有实例都要跑（含 SCHEDULER_ENABLED=false 的调试实例）——
    否则生产实例的原子切换会被长期开着的调试实例挡住。
    """
    global _handled_nonce
    try:
        req = json.loads(RELEASE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  没有请求文件 / 内容损坏都按"无需处理"
        return {"handled": False}
    nonce = str(req.get("nonce") or "")
    if not nonce or nonce == _handled_nonce:
        return {"handled": False}
    _handled_nonce = nonce
    dispose_engines()
    try:
        ACK_DIR.mkdir(parents=True, exist_ok=True)
        (ACK_DIR / f"{os.getpid()}.ack").write_text(nonce, encoding="utf-8")
    except OSError:
        pass
    return {"handled": True, "nonce": nonce}


def holders_of(path: Path) -> list[int]:
    """谁正打开着这个文件（Windows 重启管理器；其他平台/失败返回空）。

    纯排障辅助：把 PID 写进 promote 的错误信息，省得下次还得靠工具猜。
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        rm = ctypes.WinDLL("rstrtmgr", use_last_error=True)

        class _Uniq(ctypes.Structure):
            _fields_ = [("pid", wintypes.DWORD), ("start", wintypes.FILETIME)]

        class _Info(ctypes.Structure):
            _fields_ = [("proc", _Uniq), ("app", wintypes.WCHAR * 256),
                        ("svc", wintypes.WCHAR * 64), ("kind", ctypes.c_uint),
                        ("status", wintypes.ULONG), ("session", wintypes.DWORD),
                        ("restartable", wintypes.BOOL)]

        session = wintypes.DWORD()
        key = ctypes.create_unicode_buffer(33)
        if rm.RmStartSession(ctypes.byref(session), None, key) != 0:
            return []
        try:
            arr = (ctypes.c_wchar_p * 1)(str(path))
            if rm.RmRegisterResources(session, 1, arr, 0, None, 0, None) != 0:
                return []
            need, have, reason = wintypes.DWORD(0), wintypes.DWORD(0), wintypes.DWORD(0)
            rm.RmGetList(session, ctypes.byref(need), ctypes.byref(have), None,
                         ctypes.byref(reason))
            if not need.value:
                return []
            buf = (_Info * need.value)()
            have = wintypes.DWORD(need.value)
            if rm.RmGetList(session, ctypes.byref(need), ctypes.byref(have), buf,
                            ctypes.byref(reason)) != 0:
                return []
            return sorted({buf[i].proc.pid for i in range(have.value)})
        finally:
            rm.RmEndSession(session)
    except Exception:  # noqa: BLE001  排障辅助，失败不影响主流程
        return []


def promote(snapshot: Path, wait_sec: float = 60.0) -> None:
    """把某个快照原子切换为 current。

    用 `os.replace` **直接原子覆盖**，不先删除旧文件——
    实测本机存在"安全删除"机制（删除进回收站），先删后换会因其中止而失败：
    `[safe-delete] 操作失败: … Error during a 'trash' operation: Some operations were aborted`。

    重试窗口 `wait_sec`（默认 60 秒；原先仅 6×0.5=3 秒）：Windows 上只要还有**任何**
    进程打开 current.sqlite，替换就会以 WinError 32 失败。3 秒既挡不住其他实例常开的
    连接池句柄，也挡不住"查询正好在飞"的短暂占用——2026-09-21/22 两晚定时同步因此
    100% 失败（快照白建、页面数据整天不更新）。现在配合让出协议 + 退避重试。
    """
    ensure_dirs()
    tmp = CURRENT_DB.with_suffix(".sqlite.tmp")
    shutil.copy2(snapshot, tmp)  # copy2 直接覆盖同名 tmp，无需先删

    # ⚠️ 释放引擎必须在**替换前一刻**，而不是复制前：
    # 复制 2 GB 需要几分钟，期间页面查询会重新打开 current.sqlite 的句柄；
    # 若先释放再复制，到 os.replace 时文件仍被占用 → 失败。
    request_release()  # 请其他实例（如长开的调试实例）也释放读句柄
    deadline = time.monotonic() + wait_sec
    interval = 0.3
    last_exc: Exception | None = None
    try:
        while True:
            dispose_engines()
            try:
                os.replace(tmp, CURRENT_DB)
                return
            except OSError as exc:
                last_exc = exc
                if time.monotonic() >= deadline:
                    break
                time.sleep(interval)
                interval = min(interval * 1.5, 2.0)  # 退避到 2 秒

        holders = holders_of(CURRENT_DB)
        acks = read_acks()
        detail = ""
        if holders:
            detail += "；占用进程 PID=%s" % ",".join(str(p) for p in holders)
        if acks:
            detail += "；已让出句柄的实例 PID=%s" % ",".join(str(p) for p in acks)
        raise RuntimeError(
            "切换 current 失败（%.0f 秒内 current.sqlite 一直被占用）：%s%s"
            % (wait_sec, last_exc, detail)
        )
    finally:
        clear_release()
        # 不留 2GB 级垃圾：成功时 tmp 已被 replace 掉，失败时删掉下次重来
        # （曾遗留 2.3GB 的 current.sqlite.tmp，白占 F: 还干扰排查）
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def list_snapshots() -> list[Path]:
    if not SNAPSHOT_DIR.is_dir():
        return []
    return sorted(SNAPSHOT_DIR.glob("mirror_*.sqlite"))


def prune_snapshots(keep: int | None = None) -> list[Path]:
    """只保留最近 keep 个快照，返回被删除的文件列表。"""
    keep = KEEP_SNAPSHOTS if keep is None else keep
    files = list_snapshots()
    removed: list[Path] = []
    for f in files[:-keep] if keep > 0 else files:
        try:
            f.unlink()
            removed.append(f)
        except Exception:  # noqa: BLE001
            pass
    return removed


def read_meta(db_file: Path | None = None) -> list[dict]:
    """读取某快照的 sync_meta（默认 current）。

    注意必须先做快照替换检测：promote 是"删+换"文件，
    否则这里会继续用旧引擎读旧文件，导致「数据截止时间」一直停在旧值。
    """
    target = db_file or CURRENT_DB
    _invalidate_if_replaced(target)
    eng = engine_for(target)
    try:
        with eng.connect() as conn:
            rows = conn.execute(select(SYNC_META)).mappings().all()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def data_deadline(db_file: Path | None = None) -> str | None:
    """数据截止时间：取 current 快照内最大的 finished_at。"""
    metas = read_meta(db_file)
    ts = [m.get("finished_at") for m in metas if m.get("finished_at")]
    return max(ts) if ts else None


def cleanup_stale_tmp(max_age_sec: float = 6 * 3600) -> bool:
    """清理上次异常留下的 current.sqlite.tmp（正常路径下 promote 会自己收尾）。

    残留的 tmp 是 2GB 级文件，既白占磁盘也会让排查困惑（曾遗留 2.3GB）。
    """
    tmp = CURRENT_DB.with_suffix(".sqlite.tmp")
    try:
        if tmp.exists() and (time.time() - tmp.stat().st_mtime) > max_age_sec:
            tmp.unlink()
            return True
    except OSError:
        pass
    return False


def append_history(record: dict) -> None:
    """把一次同步的结果追加到 `sync_history.log`（JSON 行）。

    为什么必须落盘：失败原因原先只存在 runner 的内存 `last_error` 里，
    服务一重启就查不到——2026-09-21/22 两晚的同步失败就是这么"查无此错"的，
    只能靠磁盘上残留的 2.3GB tmp 反推。
    """
    try:
        ensure_dirs()
        with HISTORY_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_history(limit: int = 20) -> list[dict]:
    """读取最近的同步历史（最新在前）。"""
    try:
        lines = HISTORY_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001  半行/损坏行跳过
            continue
    return list(reversed(out))
