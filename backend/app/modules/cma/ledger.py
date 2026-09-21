"""一单一库核对台账（留痕）。

**为什么要有它**：资质认定评审看的是"能力持续符合 + 管理体系持续有效运行"的**证据链**，
而不是某一时点的结论。所以每次核对都要留下可追溯的记录：

- 批次（batch）：一次"开始核对"= 一个批次，记录操作人 / 部门 / 用途 / 来源 / 客户端 IP /
  耗时 / 结论分布 / 覆盖率；
- 明细（item）：批次内每条标准号的输入值、平台返回值、结论、匹配方式、异常信息；
- 处置说明（note）：**批次级**的处置/复核结论；
- 处理意见（disposition）：**逐条**的处理意见——凡结论为「版本不符·待确认」「不在清单内」
  或「有备注」的条目都属"需处理"，必须各自写明处理意见（评审要看的是"发现→处置"的逐条闭环，
  一句批次说明代替不了）。未填写的条数以 `pending_cnt` 暴露出来，并在导出环节强制闭环。

**设计约定**：

- 存储为 SQLite（标准库 sqlite3，无额外依赖），默认 `<backend>/data/cma_ledger/ledger.sqlite`；
- **只增不删**：不提供删除接口，历史记录一经写入即固定，满足可追溯要求；
  （`disposition` / `note` 属"填写"性质，允许覆盖修改——它们不是核对记录本身。）
- **留痕不阻塞业务**：写台账失败只记日志并在响应里带 `ledger.error`，
  绝不因为"记录失败"而让核对本身失败（取证价值 > 严格事务）；
- 明细逐条写入并**实时重算批次聚合**：核对是 5 并发逐条请求，批次可能处于"进行中"状态，
  聚合值始终反映已落库的真实条数（不做内存态累加，避免进程重启丢数）；
- **字段变更必须可迁移**：老库（无 disposition 等字段）要能自动补列并回填派生值，
  否则升级后写入会直接报 `no such column`。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import BACKEND_DIR, settings

_LOG = logging.getLogger("app.cma.ledger")

# 结论取值（与前端展示一一对应，不要随意改动字面量）
IN_LIST = "在清单内"
VERSION_WARN = "版本不符·待确认"
NOT_IN_LIST = "不在清单内"
QUERY_ERROR = "查询异常"

# 「需处理」判定：结论为版本不符/不在清单内，或"在清单内但有平台备注"。
# 口径与用户确认一致（查询异常暂不纳入），SQL 侧的同口径表达式见 _NEED_SQL。
_NEED_CONCLUSIONS = (VERSION_WARN, NOT_IN_LIST)
_NEED_SQL = (
    f"(conclusion IN ('{VERSION_WARN}', '{NOT_IN_LIST}') "
    f"OR (conclusion = '{IN_LIST}' AND remark <> ''))"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cma_batch (
    batch_id      TEXT PRIMARY KEY,
    checked_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL DEFAULT '',
    operator      TEXT NOT NULL DEFAULT '',
    dept          TEXT NOT NULL DEFAULT '',
    purpose       TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    client_ip     TEXT NOT NULL DEFAULT '',
    expected      INTEGER NOT NULL DEFAULT 0,
    total         INTEGER NOT NULL DEFAULT 0,
    in_cnt        INTEGER NOT NULL DEFAULT 0,
    warn_cnt      INTEGER NOT NULL DEFAULT 0,
    out_cnt       INTEGER NOT NULL DEFAULT 0,
    err_cnt       INTEGER NOT NULL DEFAULT 0,
    remark_cnt    INTEGER NOT NULL DEFAULT 0,
    need_cnt      INTEGER NOT NULL DEFAULT 0,
    pending_cnt   INTEGER NOT NULL DEFAULT 0,
    cover_rate    REAL    NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    note          TEXT    NOT NULL DEFAULT '',
    created_ts    REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cma_item (
    batch_id        TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    input_code      TEXT NOT NULL DEFAULT '',
    platform_code   TEXT NOT NULL DEFAULT '',
    standard_method TEXT NOT NULL DEFAULT '',
    remark          TEXT NOT NULL DEFAULT '',
    conclusion      TEXT NOT NULL DEFAULT '',
    match_type      TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    need_disposition INTEGER NOT NULL DEFAULT 0,
    disposition     TEXT NOT NULL DEFAULT '',
    checked_at      TEXT NOT NULL,
    PRIMARY KEY (batch_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_cma_batch_checked_at ON cma_batch(checked_at);
CREATE INDEX IF NOT EXISTS idx_cma_item_batch ON cma_item(batch_id);
"""

# 老库补列（幂等）：`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加字段，
# 因此升级时必须显式 ALTER。缺列名 → 建列语句。
MIGRATIONS: dict[str, dict[str, str]] = {
    "cma_item": {
        "need_disposition": "ALTER TABLE cma_item ADD COLUMN need_disposition INTEGER NOT NULL DEFAULT 0",
        "disposition": "ALTER TABLE cma_item ADD COLUMN disposition TEXT NOT NULL DEFAULT ''",
    },
    "cma_batch": {
        "need_cnt": "ALTER TABLE cma_batch ADD COLUMN need_cnt INTEGER NOT NULL DEFAULT 0",
        "pending_cnt": "ALTER TABLE cma_batch ADD COLUMN pending_cnt INTEGER NOT NULL DEFAULT 0",
    },
}

_LOCK = threading.Lock()
_initialized: set[str] = set()


def ledger_dir() -> Path:
    raw = (settings.cma_ledger_dir or "").strip()
    return Path(raw) if raw else (BACKEND_DIR / "data" / "cma_ledger")


def db_path() -> Path:
    return ledger_dir() / "ledger.sqlite"


def enabled() -> bool:
    return bool(settings.cma_ledger_enabled)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    """打开台账库（必要时建表）。

    ⚠️ 建表判断必须同时看「进程内是否建过」**和**「文件是不是新建的」：
    只记进程内标记的话，运维在服务运行期间手工删掉/搬走 ledger.sqlite（做归档很常见），
    后续写入会撞上 `no such table` —— 台账静默失效，且难以察觉。
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    key = str(path)
    if fresh or key not in _initialized:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        _initialized.add(key)
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    """老库升级：补齐缺失字段并回填派生值（幂等，可重复执行）。

    回填的必要性：老记录的 `need_disposition` 默认 0 / 批次 `need_cnt` 默认 0，
    若不回填，"需处理"标记会与结论/备注不一致——台账会出现"明明是不在清单内，
    却不要求处理意见"的错误状态。
    """
    changed = False
    for table, cols in MIGRATIONS.items():
        existing = _table_columns(conn, table)
        for name, ddl in cols.items():
            if name not in existing:
                conn.execute(ddl)
                changed = True
    if not changed:
        return

    # 回填：按与写入路径完全相同的口径重算
    conn.execute(f"UPDATE cma_item SET need_disposition = CASE WHEN {_NEED_SQL} THEN 1 ELSE 0 END")
    conn.execute(
        """
        UPDATE cma_batch SET
            need_cnt = IFNULL((SELECT COUNT(*) FROM cma_item i
                                WHERE i.batch_id = cma_batch.batch_id AND i.need_disposition = 1), 0),
            pending_cnt = IFNULL((SELECT COUNT(*) FROM cma_item i
                                   WHERE i.batch_id = cma_batch.batch_id AND i.need_disposition = 1
                                     AND i.disposition = ''), 0)
        """
    )
    _LOG.info("台账表结构已迁移（补齐处理意见相关字段并回填）")


def new_batch_id() -> str:
    """批次号：时间前缀便于人工核对，短随机后缀防并发撞号。"""
    return f"CMA{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"


# ==================== 结论判定 ====================


def conclusion_of(result: dict[str, Any]) -> str:
    """把核对结果映射为台账结论（与页面统计口径完全一致）。"""
    if result.get("error"):
        return QUERY_ERROR
    if not result.get("found"):
        return NOT_IN_LIST
    if result.get("versionMismatch"):
        return VERSION_WARN
    return IN_LIST


def need_disposition_of(result: dict[str, Any]) -> int:
    """是否需要填写处理意见（与 SQL 侧 `_NEED_SQL` 口径一致）。

    三类：版本不符·待确认 / 不在清单内 / 在清单内但有平台备注。
    """
    conclusion = conclusion_of(result)
    if conclusion in _NEED_CONCLUSIONS:
        return 1
    if conclusion == IN_LIST and (result.get("remark") or ""):
        return 1
    return 0


def _recalc(conn: sqlite3.Connection, batch_id: str, expected: int) -> None:
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                                       AS total,
            SUM(CASE WHEN conclusion = ? THEN 1 ELSE 0 END)                AS in_cnt,
            SUM(CASE WHEN conclusion = ? THEN 1 ELSE 0 END)                AS warn_cnt,
            SUM(CASE WHEN conclusion = ? THEN 1 ELSE 0 END)                AS out_cnt,
            SUM(CASE WHEN conclusion = ? THEN 1 ELSE 0 END)                AS err_cnt,
            SUM(CASE WHEN remark <> '' AND conclusion <> ? THEN 1 ELSE 0 END) AS remark_cnt,
            SUM(CASE WHEN need_disposition = 1 THEN 1 ELSE 0 END)          AS need_cnt,
            SUM(CASE WHEN need_disposition = 1 AND disposition = '' THEN 1 ELSE 0 END) AS pending_cnt
        FROM cma_item WHERE batch_id = ?
        """,
        (IN_LIST, VERSION_WARN, NOT_IN_LIST, QUERY_ERROR, QUERY_ERROR, batch_id),
    ).fetchone()
    total = int(row["total"] or 0)
    in_cnt = int(row["in_cnt"] or 0)
    warn_cnt = int(row["warn_cnt"] or 0)
    out_cnt = int(row["out_cnt"] or 0)
    err_cnt = int(row["err_cnt"] or 0)
    remark_cnt = int(row["remark_cnt"] or 0)
    need_cnt = int(row["need_cnt"] or 0)
    pending_cnt = int(row["pending_cnt"] or 0)
    # 覆盖率口径：在清单内 / 有效条数（异常条不计入分母，避免网络抖动把覆盖率算低）
    valid = total - err_cnt
    cover = round(in_cnt / valid * 100, 1) if valid > 0 else 0.0
    conn.execute(
        """
        UPDATE cma_batch SET
            expected = ?, total = ?, in_cnt = ?, warn_cnt = ?, out_cnt = ?,
            err_cnt = ?, remark_cnt = ?, need_cnt = ?, pending_cnt = ?, cover_rate = ?
        WHERE batch_id = ?
        """,
        (
            expected, total, in_cnt, warn_cnt, out_cnt, err_cnt,
            remark_cnt, need_cnt, pending_cnt, cover, batch_id,
        ),
    )
    # 全部条目已落库 → 标记完成并结算耗时
    if expected > 0 and total >= expected:
        conn.execute(
            """
            UPDATE cma_batch
               SET finished_at = ?, duration_ms = CAST((julianday(?) - julianday(checked_at)) * 86400000 AS INTEGER)
             WHERE batch_id = ? AND finished_at = ''
            """,
            (_now(), _now(), batch_id),
        )


def _ensure_batch(conn: sqlite3.Connection, batch_id: str, meta: dict[str, Any], expected: int) -> None:
    if conn.execute("SELECT 1 FROM cma_batch WHERE batch_id = ?", (batch_id,)).fetchone():
        return
    conn.execute(
        """
        INSERT INTO cma_batch (batch_id, checked_at, operator, dept, purpose, source, client_ip,
                               expected, created_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            meta.get("checked_at") or _now(),
            (meta.get("operator") or "")[:64],
            (meta.get("dept") or "")[:64],
            (meta.get("purpose") or "")[:128],
            (meta.get("source") or "")[:32],
            (meta.get("client_ip") or "")[:64],
            expected,
            datetime.now().timestamp(),
        ),
    )


# ==================== 写入 ====================


def record_item(
    batch_id: str,
    seq: int,
    expected: int,
    input_code: str,
    result: dict[str, Any],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """记录单条核对明细（并发逐条调用；重复 seq 以最后一次为准）。"""
    if not enabled():
        return {"enabled": False, "batch_id": batch_id}
    meta = dict(meta or {})
    try:
        with _LOCK:
            conn = _connect()
            try:
                _ensure_batch(conn, batch_id, meta, expected)
                conclusion = conclusion_of(result)
                conn.execute(
                    """
                    INSERT INTO cma_item (batch_id, seq, input_code, platform_code, standard_method,
                                          remark, conclusion, match_type, error, need_disposition, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(batch_id, seq) DO UPDATE SET
                        input_code = excluded.input_code,
                        platform_code = excluded.platform_code,
                        standard_method = excluded.standard_method,
                        remark = excluded.remark,
                        conclusion = excluded.conclusion,
                        match_type = excluded.match_type,
                        error = excluded.error,
                        need_disposition = excluded.need_disposition,
                        checked_at = excluded.checked_at
                    """,
                    (
                        batch_id,
                        int(seq),
                        (input_code or "")[:128],
                        (str(result.get("standardCode") or ""))[:128],
                        (str(result.get("standardMethod") or ""))[:512],
                        (str(result.get("remark") or ""))[:2000],
                        conclusion,
                        (str(result.get("matchType") or ""))[:64],
                        (str(result.get("error") or ""))[:512],
                        need_disposition_of(result),
                        _now(),
                    ),
                )
                _recalc(conn, batch_id, expected)
                conn.commit()
                # 批次内条目可能很多，逐条回读聚合代价低（单表 COUNT/SUM，有主键索引）
                return {"enabled": True, "batch_id": batch_id}
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001  留痕失败不影响核对
        _LOG.warning("台账写入失败（batch=%s seq=%s）：%s", batch_id, seq, exc)
        return {"enabled": True, "batch_id": batch_id, "error": str(exc)}


def record_batch(
    batch_id: str,
    items: list[tuple[str, dict[str, Any]]],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一次性记录整批（供 /check-standards 使用）。items 为 [(input_code, result), ...]。"""
    if not enabled():
        return {"enabled": False, "batch_id": batch_id}
    meta = dict(meta or {})
    try:
        with _LOCK:
            conn = _connect()
            try:
                _ensure_batch(conn, batch_id, meta, len(items))
                now = _now()
                for seq, (input_code, result) in enumerate(items, start=1):
                    conn.execute(
                        """
                        INSERT INTO cma_item (batch_id, seq, input_code, platform_code, standard_method,
                                              remark, conclusion, match_type, error, need_disposition, checked_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(batch_id, seq) DO UPDATE SET
                            input_code = excluded.input_code,
                            platform_code = excluded.platform_code,
                            standard_method = excluded.standard_method,
                            remark = excluded.remark,
                            conclusion = excluded.conclusion,
                            match_type = excluded.match_type,
                            error = excluded.error,
                            need_disposition = excluded.need_disposition,
                            checked_at = excluded.checked_at
                        """,
                        (
                            batch_id,
                            seq,
                            (input_code or "")[:128],
                            (str(result.get("standardCode") or ""))[:128],
                            (str(result.get("standardMethod") or ""))[:512],
                            (str(result.get("remark") or ""))[:2000],
                            conclusion_of(result),
                            (str(result.get("matchType") or ""))[:64],
                            (str(result.get("error") or ""))[:512],
                            need_disposition_of(result),
                            now,
                        ),
                    )
                _recalc(conn, batch_id, len(items))
                conn.commit()
                return {"enabled": True, "batch_id": batch_id}
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("台账整批写入失败（batch=%s）：%s", batch_id, exc)
        return {"enabled": True, "batch_id": batch_id, "error": str(exc)}


def set_note(batch_id: str, note: str) -> dict[str, Any]:
    """补记处置/复核说明（台账只增不删，说明允许覆盖为最新版本）。"""
    if not enabled():
        return {"success": False, "error": "台账功能未启用"}
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE cma_batch SET note = ? WHERE batch_id = ?",
                ((note or "")[:1000], batch_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return {"success": False, "error": "批次不存在"}
            return {"success": True, "batch_id": batch_id, "note": (note or "")[:1000]}
        finally:
            conn.close()


def set_dispositions(batch_id: str, items: list[tuple[int, str]]) -> dict[str, Any]:
    """逐条填写/修改处理意见（一次请求可提交多条）。

    - 只有"需处理"的条目允许写处理意见：对无需处理条（在清单内且无备注）写入会返回该条的错误，
      避免出现"给一条完全正常的标准写了处置意见"这种对不上口径的数据；
    - 每次写入后重算批次 `pending_cnt`，页面与导出据此判断是否闭环。
    """
    if not enabled():
        return {"success": False, "error": "台账功能未启用"}
    if not items:
        return {"success": False, "error": "没有要保存的处理意见"}
    saved: list[int] = []
    failed: list[dict[str, Any]] = []
    with _LOCK:
        conn = _connect()
        try:
            head = conn.execute("SELECT expected FROM cma_batch WHERE batch_id = ?", (batch_id,)).fetchone()
            if head is None:
                return {"success": False, "error": "批次不存在"}
            for seq, disposition in items:
                row = conn.execute(
                    "SELECT need_disposition FROM cma_item WHERE batch_id = ? AND seq = ?",
                    (batch_id, int(seq)),
                ).fetchone()
                if row is None:
                    failed.append({"seq": int(seq), "error": "明细不存在"})
                    continue
                if not int(row["need_disposition"] or 0):
                    failed.append({"seq": int(seq), "error": "该条无需处理（在清单内且无备注）"})
                    continue
                conn.execute(
                    "UPDATE cma_item SET disposition = ? WHERE batch_id = ? AND seq = ?",
                    ((disposition or "").strip()[:1000], batch_id, int(seq)),
                )
                saved.append(int(seq))
            _recalc(conn, batch_id, int(head["expected"] or 0))
            conn.commit()
            pending = conn.execute(
                "SELECT pending_cnt FROM cma_batch WHERE batch_id = ?", (batch_id,)
            ).fetchone()["pending_cnt"]
            return {"success": True, "batch_id": batch_id, "saved": saved, "failed": failed, "pending_cnt": int(pending or 0)}
        finally:
            conn.close()


# ==================== 读取 ====================


def _where(
    start: str, end: str, keyword: str, conclusion: str, operator: str, pending: int = 0
) -> tuple[str, list[Any]]:
    """构造批次筛选条件。

    关键词 / 结论是**明细维度**的筛选：命中任一明细的批次才会被列出，
    这样"查某个标准号历史上核过几次、结论如何"能直接命中批次。
    """
    clauses: list[str] = []
    args: list[Any] = []
    if start:
        clauses.append("b.checked_at >= ?")
        args.append(start if len(start) > 10 else f"{start} 00:00:00")
    if end:
        clauses.append("b.checked_at <= ?")
        args.append(end if len(end) > 10 else f"{end} 23:59:59")
    if operator:
        clauses.append("b.operator LIKE ?")
        args.append(f"%{operator}%")
    if keyword:
        clauses.append(
            "EXISTS (SELECT 1 FROM cma_item i WHERE i.batch_id = b.batch_id "
            "AND (i.input_code LIKE ? OR i.platform_code LIKE ? OR i.standard_method LIKE ?))"
        )
        args.extend([f"%{keyword}%"] * 3)
    if conclusion:
        clauses.append("EXISTS (SELECT 1 FROM cma_item i WHERE i.batch_id = b.batch_id AND i.conclusion = ?)")
        args.append(conclusion)
    if pending:
        # 只看"还有应填未填处理意见"的批次——导出被阻断时用它一键定位
        clauses.append("b.pending_cnt > 0")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, args


def list_batches(
    page: int = 1,
    page_size: int = 20,
    start: str = "",
    end: str = "",
    keyword: str = "",
    conclusion: str = "",
    operator: str = "",
    pending: int = 0,
) -> dict[str, Any]:
    if not enabled():
        return {"success": False, "error": "台账功能未启用"}
    page = max(1, int(page or 1))
    page_size = min(200, max(1, int(page_size or 20)))
    where, args = _where(start, end, keyword, conclusion, operator, pending)
    with _LOCK:
        conn = _connect()
        try:
            total = int(conn.execute(f"SELECT COUNT(*) AS c FROM cma_batch b{where}", args).fetchone()["c"])
            rows = conn.execute(
                f"SELECT b.* FROM cma_batch b{where} ORDER BY b.checked_at DESC, b.rowid DESC LIMIT ? OFFSET ?",
                [*args, page_size, (page - 1) * page_size],
            ).fetchall()
        finally:
            conn.close()
    return {
        "success": True,
        "page": page,
        "pageSize": page_size,
        "total": total,
        "batches": [dict(r) for r in rows],
    }


def get_batch(batch_id: str) -> dict[str, Any]:
    if not enabled():
        return {"success": False, "error": "台账功能未启用"}
    with _LOCK:
        conn = _connect()
        try:
            head = conn.execute("SELECT * FROM cma_batch WHERE batch_id = ?", (batch_id,)).fetchone()
            if head is None:
                return {"success": False, "error": "批次不存在"}
            items = conn.execute(
                "SELECT * FROM cma_item WHERE batch_id = ? ORDER BY seq ASC", (batch_id,)
            ).fetchall()
        finally:
            conn.close()
    return {"success": True, "batch": dict(head), "items": [dict(r) for r in items]}


def stats(start: str = "", end: str = "", operator: str = "") -> dict[str, Any]:
    """台账累计统计：用于页面顶部"持续运行"概览与评审取证时的合计口径。"""
    if not enabled():
        return {"success": False, "error": "台账功能未启用"}
    where, args = _where(start, end, "", "", operator)
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS batches,
                       COALESCE(SUM(b.total), 0)  AS items,
                       COALESCE(SUM(b.in_cnt), 0) AS in_cnt,
                       COALESCE(SUM(b.warn_cnt), 0) AS warn_cnt,
                       COALESCE(SUM(b.out_cnt), 0) AS out_cnt,
                       COALESCE(SUM(b.err_cnt), 0) AS err_cnt,
                       COALESCE(SUM(b.remark_cnt), 0) AS remark_cnt,
                       COALESCE(SUM(b.need_cnt), 0) AS need_cnt,
                       COALESCE(SUM(b.pending_cnt), 0) AS pending_cnt,
                       MAX(b.checked_at) AS last_at
                  FROM cma_batch b{where}
                """,
                args,
            ).fetchone()
        finally:
            conn.close()
    data = dict(row)
    valid = int(data["items"]) - int(data["err_cnt"])
    data["cover_rate"] = round(int(data["in_cnt"]) / valid * 100, 1) if valid > 0 else 0.0
    data["operators"] = _operators()
    data["enabled"] = True
    data["dir"] = str(db_path())
    return {"success": True, "data": data}


def _history_rows(
    conn: sqlite3.Connection, table: str, column: str, keyword: str, limit: int
) -> list[dict[str, Any]]:
    """按去重值聚合历史填写内容（次数 + 最近出现时间）。

    排序：使用次数倒序 → 最近出现时间倒序。
    **不新增"填写时间"字段**（用户已确认处理意见只存内容）；`checked_at`（该条核对时间）作为
    最近程度的代理足够用——候选排序只影响"谁排在前面"，不参与任何判定口径。
    """
    clauses = [f"{column} <> ''"]
    args: list[Any] = []
    if keyword:
        clauses.append(f"{column} LIKE ?")
        args.append(f"%{keyword}%")
    sql = (
        f"SELECT {column} AS text, COUNT(*) AS uses, MAX(checked_at) AS last_at "
        f"FROM {table} WHERE {' AND '.join(clauses)} "
        f"GROUP BY {column} ORDER BY uses DESC, last_at DESC LIMIT ?"
    )
    return [dict(r) for r in conn.execute(sql, [*args, limit]).fetchall()]


def history(kind: str = "all", keyword: str = "", limit: int = 200) -> dict[str, Any]:
    """历史候选：`disposition`=逐条处理意见，`note`=批次处置说明，`all`=两者。

    用途：把"以前填过的措辞"变成可下拉选取的候选，既提速又统一口径。
    只读，不写任何数据。
    """
    if not enabled():
        return {"success": False, "error": "台账功能未启用"}
    kind = (kind or "all").strip().lower()
    if kind not in ("all", "disposition", "note"):
        return {"success": False, "error": "kind 只能是 all / disposition / note"}
    limit = min(500, max(1, int(limit or 200)))
    out: dict[str, Any] = {"success": True, "kind": kind}
    with _LOCK:
        conn = _connect()
        try:
            if kind in ("all", "disposition"):
                out["dispositions"] = _history_rows(conn, "cma_item", "disposition", keyword, limit)
            if kind in ("all", "note"):
                out["notes"] = _history_rows(conn, "cma_batch", "note", keyword, limit)
        finally:
            conn.close()
    return out


def _operators() -> list[str]:
    """历史操作人列表：给页面做候选人（避免同一人写出多种写法）。"""
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT operator, COUNT(*) AS c FROM cma_batch WHERE operator <> '' "
                "GROUP BY operator ORDER BY c DESC LIMIT 50"
            ).fetchall()
        finally:
            conn.close()
    return [r["operator"] for r in rows]


def export_all(
    start: str = "",
    end: str = "",
    keyword: str = "",
    conclusion: str = "",
    operator: str = "",
    pending: int = 0,
) -> dict[str, Any]:
    """导出用：批次 + 明细全量（页面自行生成 Excel，后端不做 xlsx 依赖）。"""
    if not enabled():
        return {"success": False, "error": "台账功能未启用"}
    where, args = _where(start, end, keyword, conclusion, operator, pending)
    with _LOCK:
        conn = _connect()
        try:
            batches = conn.execute(
                f"SELECT b.* FROM cma_batch b{where} ORDER BY b.checked_at ASC, b.rowid ASC", args
            ).fetchall()
            ids = [b["batch_id"] for b in batches]
            items: list[sqlite3.Row] = []
            if ids:
                marks = ",".join("?" * len(ids))
                items = conn.execute(
                    f"SELECT * FROM cma_item WHERE batch_id IN ({marks}) ORDER BY batch_id, seq", ids
                ).fetchall()
        finally:
            conn.close()
    return {
        "success": True,
        "batches": [dict(r) for r in batches],
        "items": [dict(r) for r in items],
    }
