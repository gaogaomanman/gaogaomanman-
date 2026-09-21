"""SQLite 镜像同步器：全量快照（分批、幂等、原子切换）。

关键实现约定（均为实测结论，详见 `01_问题定义.md` 第 9 节）：
1. **达梦引擎必须用 `creator=` 注入 `dmPython.connect(...)`**，且 URL 仍要带 host 供方言解析；
   URL 直连会报 `[CODE:-70028]创建SOCKET连接失败`，不带 host 会报 `KeyError: 'host'`。
2. **必须先 import `app.core.dm_driver`**（完成 DLL 路径引导），否则加密模块加载失败。
3. **表结构不用方言反射**（达梦方言反射不可靠），改为查系统表（ALL_TAB_COLUMNS /
   INFORMATION_SCHEMA.COLUMNS）自建镜像表，见 `schema.py`。
4. 数据搬运走**源侧原生 SQL 简单列读取** + 镜像侧 `insert()`，不做复杂 SQL 编译。
5. 每个快照一个独立文件，写完后 `store.promote()` 原子切换，读取方不会读到半成品。
"""
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime
from typing import Callable

import app.core.dm_driver as _dm_driver  # noqa: F401  必须在 import dmPython 之前完成 DLL 引导
from sqlalchemy import MetaData, Table, create_engine, insert
from sqlalchemy.engine import Engine

from app.core.config import settings
from app.mirror import schema as mschema
from app.mirror import store

# 需要镜像的表（按"页面实际引用的表"清点，非估算）：
#   看板 board/service.py       : DT_SAMPLE, DT_SAMPLE_PROJECT, DT_SAMPLE_CATEGORY,
#                                 DT_RESULT_CHECK_IN, DT_DETECTION, DT_EQUIPMENT_BILL, DT_DETECTED_COMPANY
#   人员能力 personnel/service.py: DT_DETECTION, DT_SAMPLE, DT_SAMPLE_PROJECT（另用 SQL Server 的 TResult）
#   达梦页 dm/blocks/*.ts        : DT_DETECTION, DT_SAMPLE, DT_SAMPLE_PROJECT, DT_RESULT_CHECK_IN,
#                                 DT_SAMPLE_CATEGORY
DM_TABLES = [
    "DT_DETECTION",
    "DT_SAMPLE",
    "DT_SAMPLE_PROJECT",
    "DT_RESULT_CHECK_IN",
    "DT_SAMPLE_CATEGORY",
    "DT_EQUIPMENT_BILL",      # 看板-设备数（首次全量时遗漏）
    "DT_DETECTED_COMPANY",    # 看板-区县雷达（受检单位 COUNTY/ADDRESS，首次全量时遗漏）
]
# SQL Server 侧：看板不查 SQL Server；人员能力与 SQL Server 查询页用到下列 4 张
SQL_TABLES = [
    "TBe_Checked",
    "TResult",
    "TResult_Data",
    "VIEW_SubMisSampleResult",
]

BATCH = int(os.environ.get("MIRROR_BATCH") or settings.mirror_batch)

ProgressFn = Callable[[str, str, int], None]  # (source, table, rows_done)


def build_dm_engine() -> Engine:
    """达梦引擎（creator 注入，绕开方言的 URL 建连缺陷）。"""
    import dmPython  # noqa: E402

    url = (
        f"dm+dmPython://{settings.dm_user}:{settings.dm_password}"
        f"@{settings.dm_host}:{settings.dm_port}"
    )
    return create_engine(
        url,
        creator=lambda: dmPython.connect(
            user=settings.dm_user,
            password=settings.dm_password,
            server=settings.dm_host,
            port=int(settings.dm_port),
        ),
        pool_pre_ping=True,
        future=True,
    )


def build_sql_engine() -> Engine:
    """SQL Server 引擎（**pyodbc**，与 `app/core/db.py` 的建连方式保持一致）。

    注：本项目 SQL Server 走 ODBC 驱动（`sql_odbc_driver`，默认 "SQL Server"），
    不能用 `mssql+pymssql://`——实测 pymssql 在该服务器上 TDS 握手失败
    （`DB-Lib error 20002: TDS server connection failed`）。故同样用 creator 注入。
    """
    import pyodbc  # noqa: E402

    cs = (
        f"DRIVER={{{settings.sql_odbc_driver}}};"
        f"SERVER={settings.sql_server},{settings.sql_port};"
        f"DATABASE={settings.sql_database};"
        f"UID={settings.sql_user};PWD={settings.sql_password}"
    )
    return create_engine(
        "mssql+pyodbc://",
        creator=lambda: pyodbc.connect(cs, timeout=settings.sql_timeout),
        future=True,
        pool_pre_ping=True,
    )


def _copy_table(
    src_engine: Engine,
    kind: str,
    src_schema: str | None,
    table: str,
    cols: list[mschema.ColumnMeta],
    tgt_engine: Engine,
    mirror_table: Table,
    batch: int = BATCH,
    on_progress: ProgressFn | None = None,
    source: str = "",
    conn=None,
) -> int:
    """按源侧原生 SQL 整表复制到镜像（分批提交，流式读取）。

    ⚠️ 关键：payload 的 key 必须用**源表列元数据的列名**（`cols`，取自系统表，顺序与 SELECT 一致），
    **不能**用驱动返回的 `result.keys()`。原因（实测）：
      达梦 dmPython 返回的列名是**小写**（`id`/`name`），而系统表列为**大写**（`ID`/`NAME`）；
      若按驱动列名作 key，SQLAlchemy 插入时大小写不匹配 → **静默写入 NULL**（不报错、行数也正确），
      属于最危险的"看起来成功、数据是空的"故障。
    这里改为**按 SELECT 的列顺序对位映射**，与驱动的大小写行为彻底解耦。
    """
    sql = mschema.source_select_sql(kind, src_schema, table, cols)
    names = [c.name for c in cols]
    total = 0
    with src_engine.connect() as sconn:
        result = sconn.exec_driver_sql(sql)
        n_keys = len(result.keys())
        if n_keys != len(names):
            raise RuntimeError(
                f"{table} 列数不一致：SELECT 返回 {n_keys} 列，元数据 {len(names)} 列"
            )
        while True:
            rows = result.fetchmany(batch)
            if not rows:
                break
            payload = [dict(zip(names, r)) for r in rows]
            if conn is not None:
                # 外部事务（WAL 原地更新）：所有批次共用同一事务，COMMIT 前页面读到旧数据
                conn.execute(insert(mirror_table), payload)
            else:
                with tgt_engine.begin() as tconn:
                    tconn.execute(insert(mirror_table), payload)
            total += len(payload)
            if on_progress:
                on_progress(source, table, total)
    return total


def run_full_sync(
    on_progress: ProgressFn | None = None,
    only: list[str] | None = None,
    promote: bool = True,
    sources: list[str] | None = None,
) -> dict:
    """执行一次同步。

    sources: 需要从源库重搬的来源（"dm" / "sqlserver" 的子集）。
             默认取 `mirror_sync_sources`（当前默认仅 dm——SQL Server 数据已冻结不再变化）。
             被跳过的来源**不连源库**，其表以旧快照为底原样保留：
             先把 current.sqlite 复制为新快照，再 DROP 本次要重搬的表重灌。
             因此日常同步只搬达梦（约 1 分钟）；SQL Server 的 350 万行只在显式要求时重搬。

    only:    仅同步指定表名（冒烟测试 / 局部重跑）
    promote: 成功后是否原子切换 current.sqlite
    """
    store.ensure_dirs()
    started = datetime.now()
    snapshot = store.snapshot_path(started)
    if snapshot.exists():
        snapshot.unlink()
    tgt_engine = store.engine_for(snapshot, fast=True)

    want_sources = [s.strip().lower() for s in (sources or settings.mirror_sync_sources).split(",") if s.strip()]
    skipped = [s for s in ("dm", "sqlserver") if s not in want_sources]

    # 以旧快照为底：被跳过的来源原样保留（不连源库、不重搬）
    carry_meta: list[dict] = []
    base = store.CURRENT_DB if store.CURRENT_DB.exists() else None
    if skipped and base is not None:
        import shutil

        from sqlalchemy import inspect as sa_inspect

        # 底本可能处于 WAL 模式（手动原地更新后）：先把 WAL 合并回主文件，
        # 否则只复制 .sqlite 主文件会丢掉未 checkpoint 的数据
        try:
            with store.current_engine().connect() as _c:
                _c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            pass
        shutil.copy2(base, snapshot)
        insp = sa_inspect(tgt_engine)
        existing = set(insp.get_table_names())
        resync_tables = [
            t for s, ts in (("dm", DM_TABLES), ("sqlserver", SQL_TABLES)) if s in want_sources for t in ts
        ]
        drop_md = MetaData()
        drop_md.reflect(bind=tgt_engine, only=[t for t in resync_tables if t in existing])
        drop_md.drop_all(tgt_engine)
        # 旧 sync_meta 一并重建：被跳过来源的行改挂新快照名后原样保留（status=carried）
        carry_meta = [
            {**m, "snapshot": snapshot.name, "status": "carried"}
            for m in store.read_meta(base)
            if m.get("source") in skipped
        ]
        if insp.has_table("sync_meta"):
            store.SYNC_META.drop(tgt_engine, checkfirst=True)

    summary: dict = {
        "snapshot": snapshot.name,
        "started_at": started.isoformat(timespec="seconds"),
        "tables": [],
        "ok": True,
    }
    meta_rows: list[dict] = []

    def sync_source(source: str, kind: str, engine: Engine,
                    default_schema: str | None, names: list[str]) -> None:
        want = [n for n in names if (not only or n in only)]
        if not want:
            return
        mirror_md = MetaData()
        for table in want:
            t0 = datetime.now()
            try:
                # 1) 读源表列元数据
                if kind == "dm":
                    cols = mschema.read_dm_columns(engine, default_schema or "", table)
                    src_schema = default_schema
                else:
                    src_schema, cols = mschema.read_sql_columns(engine, table)
                if not cols:
                    raise RuntimeError(f"未从系统表读到 {table} 的列定义（表不存在或权限不足）")

                # 2) 建镜像表（显式类型，列顺序同源表）
                mirror_table = mschema.build_mirror_table(table, cols, mirror_md)
                mirror_md.create_all(tgt_engine, tables=[mirror_table], checkfirst=True)

                # 3) 分批复制数据
                rows = _copy_table(engine, kind, src_schema, table, cols,
                                   tgt_engine, mirror_table, on_progress=on_progress, source=source)

                # 3.5) 建索引（页面聚合查询依赖，详见 schema.INDEX_SPECS；建在数据灌完之后更快）
                with tgt_engine.begin() as iconn:
                    mschema.ensure_indexes(iconn, table)

                # 4) 行数校验：与源表比对，防止"静默少搬/空搬"
                src_sql = (
                    "SELECT COUNT(*) FROM "
                    + (mschema.quote(src_schema, kind) + "." if src_schema else "")
                    + mschema.quote(table, kind)
                )
                with engine.connect() as sconn:
                    src_count = int(sconn.exec_driver_sql(src_sql).scalar() or 0)
                if src_count != rows:
                    raise RuntimeError(f"行数校验不一致：源 {src_count} 行，镜像 {rows} 行")

                meta_rows.append({
                    "snapshot": snapshot.name, "source": source, "table_name": table,
                    "row_count": rows, "started_at": t0.isoformat(timespec="seconds"),
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "ok", "error": None,
                })
                summary["tables"].append({
                    "source": source, "table": table, "rows": rows,
                    "cols": len(cols), "status": "ok",
                })
            except Exception as exc:  # noqa: BLE001
                summary["ok"] = False
                err = (str(exc) + "\n" + traceback.format_exc())[:1000]
                meta_rows.append({
                    "snapshot": snapshot.name, "source": source, "table_name": table,
                    "row_count": -1, "started_at": t0.isoformat(timespec="seconds"),
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "failed", "error": err,
                })
                summary["tables"].append({
                    "source": source, "table": table, "rows": -1,
                    "status": "failed", "error": str(exc)[:200],
                })

    try:
        if "dm" in want_sources:
            sync_source("dm", "dm", build_dm_engine(), settings.dm_schema or "DETECTION", DM_TABLES)
    except Exception as exc:  # noqa: BLE001
        summary["ok"] = False
        summary["fatal_dm"] = str(exc)[:300]
    try:
        if "sqlserver" in want_sources:
            sync_source("sqlserver", "sqlserver", build_sql_engine(), None, SQL_TABLES)
    except Exception as exc:  # noqa: BLE001
        summary["ok"] = False
        summary["fatal_sqlserver"] = str(exc)[:300]

    # 写同步元信息（含被跳过来源的 carried 行）
    try:
        store.META.create_all(tgt_engine, tables=[store.SYNC_META], checkfirst=True)
        rows = meta_rows + carry_meta
        if rows:
            with tgt_engine.begin() as conn:
                conn.execute(insert(store.SYNC_META), rows)
    except Exception as exc:  # noqa: BLE001
        summary["ok"] = False
        summary["meta_error"] = str(exc)[:300]

    tgt_engine.dispose()
    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    summary["elapsed_sec"] = round((datetime.now() - started).total_seconds(), 1)
    summary["size_mb"] = round(snapshot.stat().st_size / 1024 / 1024, 2) if snapshot.exists() else 0

    if promote and summary["ok"]:
        store.promote(snapshot)
        summary["promoted"] = True
        summary["removed_snapshots"] = [p.name for p in store.prune_snapshots()]
    else:
        summary["promoted"] = False

    return summary


def run_inplace_sync(
    on_progress: ProgressFn | None = None,
    only: list[str] | None = None,
    sources: list[str] | None = None,
) -> dict:
    """WAL 原地更新：直接在 current.sqlite 上重搬来源表（默认仅达梦），**不复制底本**。

    与快照式同步（`run_full_sync`）的区别：
      - 不复制 2 GB 底本、不生成新快照文件 → 约 1 分钟（快照式约 5 分钟）；
      - 依赖 SQLite **WAL**：整个重搬在一个写事务里，COMMIT 前页面继续读旧数据，
        COMMIT 后无感切换；中途任何一步失败自动回滚，current 保持原状（等效于
        快照式的"失败不切换"）；
      - **不产生历史快照文件**——需要每晚历史存档请走定时快照同步。
    结束时执行 `wal_checkpoint(TRUNCATE)`，保证后续快照式同步复制底本时数据完整。
    """
    if not store.CURRENT_DB.exists():
        raise RuntimeError("镜像快照不存在（尚未同步），无法原地更新")
    started = datetime.now()
    want_sources = [s.strip().lower() for s in (sources or settings.mirror_sync_sources).split(",") if s.strip()]
    want_sources = [s for s in ("dm", "sqlserver") if s in want_sources]

    summary: dict = {
        "mode": "inplace",
        "sources": want_sources,
        "snapshot": f"inplace_{started:%Y%m%d_%H%M%S}",
        "started_at": started.isoformat(timespec="seconds"),
        "tables": [],
        "ok": True,
    }
    meta_rows: list[dict] = []
    eng = store.current_engine()

    # 1) 切 WAL（持久属性；有其他连接占用时可能短暂失败，重试）
    for _attempt in range(10):
        try:
            with eng.connect() as c:
                c.exec_driver_sql("PRAGMA busy_timeout=10000")
                mode = c.exec_driver_sql("PRAGMA journal_mode=WAL").scalar()
            if str(mode).lower() == "wal":
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    else:
        raise RuntimeError("无法切换 WAL 模式（current.sqlite 被占用），请稍后重试；定时快照同步不受影响")

    engine_builders = {
        "dm": lambda: build_dm_engine(),
        "sqlserver": lambda: build_sql_engine(),
    }
    mirror_md = MetaData()

    # 2) 单事务重搬：任一表失败 → 整体回滚，current 原样
    try:
        with eng.begin() as conn:
            conn.exec_driver_sql("PRAGMA cache_size=-200000")
            conn.exec_driver_sql("PRAGMA temp_store=MEMORY")
            for source in want_sources:
                src_engine = engine_builders[source]()
                schema = (settings.dm_schema or "DETECTION") if source == "dm" else None
                names = DM_TABLES if source == "dm" else SQL_TABLES
                for table in [n for n in names if (not only or n in only)]:
                    t0 = datetime.now()
                    # 1) 源表列元数据
                    if source == "dm":
                        cols = mschema.read_dm_columns(src_engine, schema or "", table)
                        src_schema = schema
                    else:
                        src_schema, cols = mschema.read_sql_columns(src_engine, table)
                    if not cols:
                        raise RuntimeError(f"未从系统表读到 {table} 的列定义")
                    # 2) 原地重建镜像表（同事务）
                    mt = mschema.build_mirror_table(table, cols, mirror_md)
                    mt.drop(conn, checkfirst=True)
                    mt.create(conn, checkfirst=True)
                    # 3) 分批复制（同一事务）
                    rows = _copy_table(src_engine, source, src_schema, table, cols,
                                       eng, mt, on_progress=on_progress, source=source, conn=conn)
                    # 3.5) 重建索引：上面 DROP TABLE 会连索引一起删掉，必须补回
                    mschema.ensure_indexes(conn, table)
                    # 4) 行数校验
                    src_sql = (
                        "SELECT COUNT(*) FROM "
                        + (mschema.quote(src_schema, source) + "." if src_schema else "")
                        + mschema.quote(table, source)
                    )
                    with src_engine.connect() as sc:
                        src_count = int(sc.exec_driver_sql(src_sql).scalar() or 0)
                    if src_count != rows:
                        raise RuntimeError(f"行数校验不一致：源 {src_count} 行，镜像 {rows} 行")
                    meta_rows.append({
                        "snapshot": summary["snapshot"], "source": source, "table_name": table,
                        "row_count": rows, "started_at": t0.isoformat(timespec="seconds"),
                        "finished_at": datetime.now().isoformat(timespec="seconds"),
                        "status": "ok", "error": None,
                    })
                    summary["tables"].append({"source": source, "table": table, "rows": rows, "status": "ok"})
                    if on_progress:
                        on_progress(source, table, rows)
    except Exception as exc:  # noqa: BLE001
        # 事务已回滚：current.sqlite 保持原样，页面数据不受影响
        summary["ok"] = False
        summary["error"] = str(exc)[:400]
        summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
        summary["elapsed_sec"] = round((datetime.now() - started).total_seconds(), 1)
        return summary

    # 3) 更新 sync_meta（被重搬的来源整组替换）
    try:
        with eng.begin() as conn:
            for s in want_sources:
                conn.execute(store.SYNC_META.delete().where(store.SYNC_META.c.source == s))
            if meta_rows:
                conn.execute(insert(store.SYNC_META), meta_rows)
    except Exception as exc:  # noqa: BLE001
        summary["ok"] = False
        summary["meta_error"] = str(exc)[:300]

    # 4) checkpoint：把 WAL 合并回主文件（后续快照式同步复制底本时数据才完整）
    try:
        with eng.connect() as c:
            c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # noqa: BLE001
        pass

    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    summary["elapsed_sec"] = round((datetime.now() - started).total_seconds(), 1)
    summary["size_mb"] = round(store.CURRENT_DB.stat().st_size / 1024 / 1024, 2)
    return summary
