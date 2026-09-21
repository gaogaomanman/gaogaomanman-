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

import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event, select
from sqlalchemy.engine import Engine

from app.core.config import settings

# 镜像根目录：默认 backend/data/mirror（不可被静态托管访问）；
# 由 `MIRROR_DIR` 覆盖——**本机 C: 盘已满，生产上必须指向其他盘**，详见 config.py 注释。
BACKEND_DIR = Path(__file__).resolve().parents[2]
MIRROR_DIR = Path(settings.mirror_dir) if settings.mirror_dir else (BACKEND_DIR / "data" / "mirror")
SNAPSHOT_DIR = MIRROR_DIR / "snapshots"
CURRENT_DB = MIRROR_DIR / "current.sqlite"
KEEP_SNAPSHOTS = settings.mirror_keep

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
    """
    key = f"{db_file}|{'fast' if fast else 'std'}"
    with _engine_lock:
        eng = _engines.get(key)
        if eng is None:
            eng = create_engine(f"sqlite:///{db_file}", future=True)
            if fast:

                @event.listens_for(eng, "connect")
                def _set_bulk_pragmas(dbapi_conn, _rec):  # pragma: no cover - 由 SQLite 回调
                    cur = dbapi_conn.cursor()
                    cur.execute("PRAGMA journal_mode=MEMORY")
                    cur.execute("PRAGMA synchronous=OFF")
                    cur.execute("PRAGMA temp_store=MEMORY")
                    cur.execute("PRAGMA cache_size=-200000")  # ≈200MB 页缓存
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


def promote(snapshot: Path) -> None:
    """把某个快照原子切换为 current。

    用 `os.replace` **直接原子覆盖**，不先删除旧文件——
    实测本机存在"安全删除"机制（删除进回收站），先删后换会因其中止而失败：
    `[safe-delete] 操作失败: … Error during a 'trash' operation: Some operations were aborted`。
    """
    ensure_dirs()
    tmp = CURRENT_DB.with_suffix(".sqlite.tmp")
    shutil.copy2(snapshot, tmp)  # copy2 直接覆盖同名 tmp，无需先删

    # ⚠️ 释放引擎必须在**替换前一刻**，而不是复制前：
    # 复制 2 GB 需要几分钟，期间页面查询会重新打开 current.sqlite 的句柄；
    # 若先释放再复制，到 os.replace 时文件仍被占用 → 失败。
    last_exc: Exception | None = None
    for _ in range(6):
        dispose_engines()
        try:
            os.replace(tmp, CURRENT_DB)
            return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.5)
    raise RuntimeError(
        f"切换 current 失败（current.sqlite 仍被占用，请稍后重试）：{last_exc}"
    )


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
