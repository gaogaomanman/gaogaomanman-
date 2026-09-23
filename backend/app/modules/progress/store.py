"""抽采样进度统计：本地 SQLite 存储层（本模块**唯一写库的地方**）。

设计约定（沿用 `personnel/ability_store.py` 的既有范式）：
- 标准库 `sqlite3`，无额外依赖；库文件 `<progress_dir>/progress.sqlite`，
  目录由 `PROGRESS_DIR` 决定，留空 → `<backend>/data/progress`；
- 建表幂等（每次连接 `executescript`），写入一律 `with conn:` 单事务，异常自动回滚；
- 出问题时**旧数据不丢**：整表替换失败会回滚，页面不会因为一次同步失败而变空。

## 表结构（对应看板三级表头）

```
task_type     任务类型   市例行 / 市监督 …（**看板的第一层选择**；每个任务一张考核表）
  └ scheme    列方案     一个任务 × 一个年份 = 一套表头列（指标项每年会变，按年各存一份）
      └ category  品类   农产品-豇豆、芹菜、辣椒 …（看板表头列；is_other=兜底桶）
          └ product_type  产品类型  豇豆 / 芹菜 / 辣椒 …（关键词载体；任务量可录到这一级）
big_kind      大类       农产品 / 畜产品 / 水产品（兜底归集的分组依据，跨方案共用）
region        区域       张家港市 / 常熟市 / …（看板行；keys=地址解析结果的别名）
contract      合同       源库的合同编号 → 归属到某个**任务类型**
task_quota    任务量     手工录入，键 = (年份, 任务类型, 区域, 产品类型/品类)
done_sample   完成量明细 从镜像抓取的**逐样品投影**（原始字段 + 任务归属 + 归类结果）
sync_log      同步日志   只增不删
ignore_name   忽略名单   水质/土壤/肥料等不参与统计的样品名
```

## 为什么"任务类型"要独立成层

使用方的考核表是**按任务**下达的（2026 年是「市例行」「市监督」两张），两个任务的指标项
（表头列，如省例行的生鲜乳/马铃薯、市例行的小麦/杨梅）并不相同，且**每年会调整一次**。
所以列挂在 `scheme(任务, 年份)` 上，而不是全局一份。每个样品按合同归属到唯一任务，
因此归类结果仍然是"一样品一个结果"，不需要按方案做多套投影。

## 为什么完成量存"逐样品"而不是聚合值

看板的「统计范围」是可配置的（年份 / 合同多选 / 业务类别多选 / 截止日期），
聚合值一旦落地就**只能按落地时的口径查**。逐样品投影（单年几千行）既能任意切片统计，
又能在**不重新连镜像**的前提下，靠改配置重算归类（见 `service.reclassify`）。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from app.core.config import BACKEND_DIR, settings
from app.modules.progress.normalize import clean_field, contract_key

_LOG = logging.getLogger("app.progress.store")

# ==================== 保护性上限 ====================
MAX_NAME = 64
MAX_KEYS = 2000             # 关键词 / 别名串（「其他蔬菜」要接住几十个品种名）
MAX_BIG_KIND = 32
MAX_NOTE = 500
MAX_REGION = 64
MAX_QUOTA = 10_000_000
MIN_YEAR, MAX_YEAR = 2000, 2100

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"
SOURCE_BASE = "base"


# ==================== 路径 ====================


def progress_dir() -> Path:
    """数据目录：配置优先，留空则 `<backend>/data/progress`。"""
    configured = (settings.progress_dir or "").strip()
    return Path(configured) if configured else BACKEND_DIR / "data" / "progress"


def db_file() -> Path:
    return progress_dir() / "progress.sqlite"


_SCHEMA = """
-- 任务类型（看板第一层选择）：使用方的考核表按任务组织，2026 年只有「市例行」「市监督」两张
CREATE TABLE IF NOT EXISTS task_type (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    sort_no    INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_task_type_name ON task_type(name);

-- 列方案：任务 × 年份 = 一套表头列（`category.scheme_id` 指向它）。
-- 按年存的原因：考核表的指标项每年调整一次，历史年份的表要能按当年的列显示。
CREATE TABLE IF NOT EXISTS scheme (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    year         INTEGER NOT NULL,
    task_type_id INTEGER NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_scheme_year_task ON scheme(year, task_type_id);

CREATE TABLE IF NOT EXISTS big_kind (
    name       TEXT PRIMARY KEY,
    keywords   TEXT NOT NULL DEFAULT '',
    sort_no    INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS category (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    big_kind   TEXT NOT NULL DEFAULT '',
    is_other   INTEGER NOT NULL DEFAULT 0,
    sort_no    INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT '',
    -- 这一列属于哪个「列方案」（任务 × 年份）；0 = 老库待迁移
    scheme_id  INTEGER NOT NULL DEFAULT 0
);
-- ⚠️ category 的唯一索引（(scheme_id, name)）在 `_migrate()` 里建：
-- 老库里是 name 全局唯一（`ux_category_name`），且老表缺 scheme_id 列，
-- 在这里建会先于 ALTER TABLE 执行而报 "no such column: scheme_id"。

CREATE TABLE IF NOT EXISTS product_type (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL DEFAULT 0,
    name        TEXT NOT NULL,
    keywords    TEXT NOT NULL DEFAULT '',
    is_other    INTEGER NOT NULL DEFAULT 0,
    sort_no     INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL DEFAULT ''
);
-- ⚠️ product_type 的唯一索引在 `_migrate()` 里建：老库缺 category_id 列，
-- 若在这里建索引会先于 ALTER TABLE 执行而直接报 "no such column"。

CREATE TABLE IF NOT EXISTS region (
    name       TEXT PRIMARY KEY,
    keys       TEXT NOT NULL DEFAULT '',
    sort_no    INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    source     TEXT NOT NULL DEFAULT 'auto',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS contract (
    contract_no TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'auto',
    enabled     INTEGER NOT NULL DEFAULT 1,
    name_locked INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT '',
    -- 合同归并：源库里同一份合同存在拼写漂移（实测 `ps2026002-市例行` 与 `ps2026002-市例性`），
    -- 把变体填到 `alias_of` 指向的正式编号后，统计与筛选会把它并入正式合同。
    alias_of    TEXT NOT NULL DEFAULT '',
    -- 这份合同属于哪个任务类型（0 = 未纳入任何考核表，看板不算它）
    task_type_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_quota (
    year            INTEGER NOT NULL,
    -- ⚠️ 任务量按**任务类型**下达（考核表是一张任务一张表），不再按合同：
    -- 同一任务的多个合同编号（源库里 `ps2026002-市例行` 与 `ps2026002-市例性` 是同一份）
    -- 共用一套任务量，否则要给同一张表录两遍。
    task_type_id    INTEGER NOT NULL DEFAULT 0,
    region          TEXT NOT NULL,
    product_type_id INTEGER NOT NULL,
    quota           INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT '',
    -- 任务量有两种下达粒度（使用方的考核表是按"品类"下达的，如「农产品-豇豆、芹菜、辣椒」= 10 批次）：
    --   品类级：product_type_id = 0 且 category_id = 品类 id
    --   产品级：product_type_id = 产品类型 id 且 category_id = 0
    -- 两者可共存；品类合计 = 品类级 + 其下产品级之和。
    -- ⚠️ category_id 必须进主键：否则同一区域同一任务只能存一个品类级任务量（后面的会覆盖前面的）。
    category_id     INTEGER NOT NULL DEFAULT 0,
    -- 保留原始合同编号仅作溯源（不参与主键，页面按任务汇总）
    contract_no     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (year, task_type_id, region, product_type_id, category_id)
);

-- 逐样品投影：原始字段照存，归类结果可随时按最新配置重算（不重连镜像）
CREATE TABLE IF NOT EXISTS done_sample (
    year            INTEGER NOT NULL,
    sample_id       INTEGER NOT NULL,
    detection_no    TEXT NOT NULL DEFAULT '',
    contract_no     TEXT NOT NULL DEFAULT '',
    biz             TEXT NOT NULL DEFAULT '',
    task_name       TEXT NOT NULL DEFAULT '',
    region_key      TEXT NOT NULL DEFAULT '',
    region          TEXT NOT NULL DEFAULT '',
    sample_name     TEXT NOT NULL DEFAULT '',
    sample_category TEXT NOT NULL DEFAULT '',
    sample_date     TEXT NOT NULL DEFAULT '',
    -- 样品归属的任务类型（同步时由合同推导，0 = 未纳入任何考核表）
    task_type_id    INTEGER NOT NULL DEFAULT 0,
    product_type_id INTEGER NOT NULL DEFAULT 0,
    match_layer     TEXT NOT NULL DEFAULT '',
    synced_at       TEXT NOT NULL DEFAULT '',
    -- 区域的原始来源照存（区县 + 两个地址）：切「区域判定优先」时可在本地重算，不必重新连镜像
    county          TEXT NOT NULL DEFAULT '',
    addr            TEXT NOT NULL DEFAULT '',
    addr2           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (year, sample_id)
);
CREATE INDEX IF NOT EXISTS ix_done_sample_year ON done_sample(year);
CREATE INDEX IF NOT EXISTS ix_done_sample_name ON done_sample(year, sample_name);
-- ⚠️ done_sample(year, task_type_id) 的索引在 `_migrate()` 里建：老库缺 task_type_id 列，
-- 在这里建会先于 ALTER TABLE 执行而报 "no such column"。

CREATE TABLE IF NOT EXISTS ignore_name (
    name       TEXT PRIMARY KEY,
    note       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    year         INTEGER NOT NULL,
    started_at   TEXT NOT NULL DEFAULT '',
    finished_at  TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    data_deadline TEXT NOT NULL DEFAULT '',
    rows         INTEGER NOT NULL DEFAULT 0,
    done_total   INTEGER NOT NULL DEFAULT 0,
    contract_count INTEGER NOT NULL DEFAULT 0,
    unmatched    INTEGER NOT NULL DEFAULT 0,
    unclassified INTEGER NOT NULL DEFAULT 0,
    no_contract  INTEGER NOT NULL DEFAULT 0,
    ignored      INTEGER NOT NULL DEFAULT 0,
    elapsed_ms   INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS app_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT ''
);
"""


# task_quota 主键升级（合同 → 任务类型）用的重建语句：SQLite 不支持改主键，只能 建新表 → 搬数据 → 换名。
# 老行的任务类型由合同推导（合同表此时可能还没分配任务 → 落到 0，bootstrap 里会再按合同编号补一次）。
_REBUILD_TASK_QUOTA = (
    "ALTER TABLE task_quota RENAME TO task_quota_old",
    "CREATE TABLE task_quota ("
    "  year INTEGER NOT NULL, task_type_id INTEGER NOT NULL DEFAULT 0, region TEXT NOT NULL,"
    "  product_type_id INTEGER NOT NULL, quota INTEGER NOT NULL DEFAULT 0,"
    "  updated_at TEXT NOT NULL DEFAULT '', category_id INTEGER NOT NULL DEFAULT 0,"
    "  contract_no TEXT NOT NULL DEFAULT '',"
    "  PRIMARY KEY (year, task_type_id, region, product_type_id, category_id))",
    # 搬数据时按新主键**汇总**（同一任务的多个合同编号可能各有同格任务量，加起来才是该任务的下达量）
    "INSERT INTO task_quota (year, task_type_id, region, product_type_id, quota, updated_at, category_id, contract_no) "
    "SELECT year, task_type_id, region, product_type_id, SUM(quota), MAX(updated_at), category_id, MAX(contract_no) FROM ("
    "  SELECT o.year AS year,"
    "    IFNULL((SELECT c.task_type_id FROM contract c WHERE c.contract_no = o.contract_no), 0) AS task_type_id,"
    "    o.region AS region, o.product_type_id AS product_type_id, o.quota AS quota,"
    "    o.updated_at AS updated_at, o.category_id AS category_id, o.contract_no AS contract_no"
    "  FROM task_quota_old o"
    ") GROUP BY year, task_type_id, region, product_type_id, category_id",
    "DROP TABLE task_quota_old",
)


# 老库需要补的列（表名 → [(列名, DDL), …]）；新库由 `_SCHEMA` 一次建全，这里只会空跑
_LATE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "region": [("keys", "ALTER TABLE region ADD COLUMN keys TEXT NOT NULL DEFAULT ''")],
    "sync_log": [("ignored", "ALTER TABLE sync_log ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0")],
    "task_quota": [("category_id", "ALTER TABLE task_quota ADD COLUMN category_id INTEGER NOT NULL DEFAULT 0")],
    "contract": [
        ("alias_of", "ALTER TABLE contract ADD COLUMN alias_of TEXT NOT NULL DEFAULT ''"),
        ("task_type_id", "ALTER TABLE contract ADD COLUMN task_type_id INTEGER NOT NULL DEFAULT 0"),
    ],
    "done_sample": [
        ("county", "ALTER TABLE done_sample ADD COLUMN county TEXT NOT NULL DEFAULT ''"),
        ("addr", "ALTER TABLE done_sample ADD COLUMN addr TEXT NOT NULL DEFAULT ''"),
        # 第二个地址：源库里"单位地址"常是垃圾值（`nullnullnull`），必须把"受检地址"也留着，
        # 否则切换区域判定优先项后重算时第二个地址用不上，会凭空多出"未映射"。
        ("addr2", "ALTER TABLE done_sample ADD COLUMN addr2 TEXT NOT NULL DEFAULT ''"),
        ("task_type_id", "ALTER TABLE done_sample ADD COLUMN task_type_id INTEGER NOT NULL DEFAULT 0"),
    ],
    "category": [("scheme_id", "ALTER TABLE category ADD COLUMN scheme_id INTEGER NOT NULL DEFAULT 0")],
    "product_type": [
        ("keywords", "ALTER TABLE product_type ADD COLUMN keywords TEXT NOT NULL DEFAULT ''"),
        ("category_id", "ALTER TABLE product_type ADD COLUMN category_id INTEGER NOT NULL DEFAULT 0"),
        ("is_other", "ALTER TABLE product_type ADD COLUMN is_other INTEGER NOT NULL DEFAULT 0"),
    ],
}



def _connect() -> sqlite3.Connection:
    path = db_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """结构升级（幂等）。

    老库是"单级表头"版本（`product_type` 就是表头列，没有 `category`），无法表达三级表头。
    此时**重建配置**：旧表头的名称就是现在的品类名，其成员按顿号拆成二级产品类型。
    旧的任务量/完成量随键的语义变化而失效，一并清空——完成量可重新同步，任务量需重新录入；
    这一步会打日志，不做静默处理。
    """
    cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(product_type)")}
    is_legacy = "category_id" not in cols      # 先判老库：下面补列后这个特征就没了

    # 老库缺的列先补齐（`CREATE TABLE IF NOT EXISTS` 对已存在的表不会加列）
    for table, columns in _LATE_COLUMNS.items():
        have = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in columns:
            if col not in have:
                conn.execute(ddl)
    conn.commit()

    # 任务量表主键：老库是 (年, 合同, 区域, 产品类型)，新库是 (年, **任务**, 区域, 产品类型/品类)。
    # SQLite 不能改主键，只能重建。触发条件 = 主键里还没有 task_type_id。
    info = list(conn.execute("PRAGMA table_info(task_quota)"))
    pk_cols = {str(r["name"]) for r in info if int(r["pk"] or 0) > 0}
    if pk_cols and "task_type_id" not in pk_cols:
        _LOG.warning("progress 库升级：重建 task_quota 主键（任务量从「按合同」改为「按任务」）")
        for stmt in _REBUILD_TASK_QUOTA:
            conn.execute(stmt)
        conn.commit()

    if is_legacy:
        if conn.execute("SELECT COUNT(*) AS n FROM product_type").fetchone()["n"] \
                and not conn.execute("SELECT COUNT(*) AS n FROM category").fetchone()["n"]:
            _LOG.warning("progress 库升级为三级表头：清空旧产品类型与旧任务量/完成量，配置需重新确认")
            for table in ("product_type", "task_quota", "done_sample"):
                conn.execute(f"DELETE FROM {table}")
        if "members" in cols:          # 老库的 members 语义 = 现在的 keywords
            conn.execute("UPDATE product_type SET keywords = IFNULL(members, '') WHERE IFNULL(keywords, '') = ''")
        # 老版本会在同步时**自动登记**区域（省例行抽到的南京各区等），新版本改为"人工挂载未映射区域"，
        # 这些 `source='auto'` 的行必须清掉——留着会让看板多出十来行外地行。
        gone = conn.execute("DELETE FROM region WHERE source = 'auto'").rowcount
        if gone:
            _LOG.warning("progress 库升级：清理 %s 个老版本自动登记的区域行（新版本改为人工挂载）", gone)
        conn.commit()

    # 二级表头允许不同品类下重名，唯一键是 (category_id, name)；
    # 老库的 name 全局唯一索引必须先删掉，否则「其他蔬菜」这类同名列插不进去。
    conn.execute("DROP INDEX IF EXISTS ux_product_type_name")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_product_type_cat_name ON product_type(category_id, name)")
    # 品类（表头列）改为挂在「列方案」下：不同方案允许同名（如两个任务都有「农产品—其他蔬菜」），
    # 因此老库的 name 全局唯一索引必须删掉，换成 (scheme_id, name)。
    conn.execute("DROP INDEX IF EXISTS ux_category_name")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_category_scheme_name ON category(scheme_id, name)")
    # 任务字段的索引也要等列补齐后才能建
    conn.execute("CREATE INDEX IF NOT EXISTS ix_done_sample_task ON done_sample(year, task_type_id)")
    conn.commit()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _check_year(year: int) -> int:
    y = int(year)
    if not (MIN_YEAR <= y <= MAX_YEAR):
        raise ValueError(f"年份超出范围（{MIN_YEAR}~{MAX_YEAR}）：{year}")
    return y


# ==================== 大类 ====================


def list_big_kinds(include_disabled: bool = True) -> list[dict]:
    where = "" if include_disabled else " WHERE enabled = 1"
    conn = _connect()
    try:
        return _rows(conn.execute("SELECT * FROM big_kind" + where + " ORDER BY sort_no, name"))
    finally:
        conn.close()


def upsert_big_kind(name: str, keywords: str | None = None,
                    sort_no: int | None = None, enabled: bool | None = None) -> dict:
    """新增大类 / 修改大类关键词（关键词用于把"判不出具体品种"的样品归到兜底桶）。"""
    nm = clean_field(name, MAX_BIG_KIND)
    if not nm:
        raise ValueError("大类名称不能为空")
    conn = _connect()
    try:
        with conn:
            row = conn.execute("SELECT * FROM big_kind WHERE name = ?", (nm,)).fetchone()
            if row is None:
                mx = conn.execute("SELECT IFNULL(MAX(sort_no), 0) AS m FROM big_kind").fetchone()
                conn.execute(
                    "INSERT INTO big_kind (name, keywords, sort_no, enabled, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (nm, clean_field(keywords, MAX_KEYS), int(sort_no) if sort_no is not None else int(mx["m"]) + 10,
                     1 if (enabled is None or enabled) else 0, _now()),
                )
            else:
                conn.execute(
                    "UPDATE big_kind SET keywords = ?, sort_no = ?, enabled = ?, updated_at = ? WHERE name = ?",
                    (
                        clean_field(keywords, MAX_KEYS) if keywords is not None else str(row["keywords"]),
                        int(sort_no) if sort_no is not None else int(row["sort_no"]),
                        (1 if enabled else 0) if enabled is not None else int(row["enabled"]),
                        _now(), nm,
                    ),
                )
    finally:
        conn.close()
    return next((r for r in list_big_kinds() if r["name"] == nm), {})


def delete_big_kind(name: str) -> None:
    nm = clean_field(name, MAX_BIG_KIND)
    conn = _connect()
    try:
        used = conn.execute("SELECT COUNT(*) AS n FROM category WHERE big_kind = ?", (nm,)).fetchone()["n"]
        if used:
            raise ValueError(f"大类「{nm}」下还有 {used} 个品类，不能删除")
        with conn:
            cur = conn.execute("DELETE FROM big_kind WHERE name = ?", (nm,))
            if not cur.rowcount:
                raise ValueError(f"大类不存在：{nm}")
    finally:
        conn.close()


# ==================== 任务类型（看板第一层选择） ====================


def list_task_types(include_disabled: bool = True) -> list[dict]:
    """任务类型清单（市例行 / 市监督 …），按 sort_no。"""
    where = "" if include_disabled else " WHERE enabled = 1"
    conn = _connect()
    try:
        return _rows(conn.execute("SELECT * FROM task_type" + where + " ORDER BY sort_no, id"))
    finally:
        conn.close()


def get_task_type(tid: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM task_type WHERE id = ?", (int(tid),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_task_type_by_name(name: str) -> dict | None:
    nm = clean_field(name, MAX_NAME)
    if not nm:
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM task_type WHERE name = ?", (nm,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_task_type(name: str, sort_no: int | None = None, enabled: bool | None = None) -> dict:
    """新增 / 修改任务类型。名称是业务标识（考核表的名字），建后不建议改。"""
    nm = clean_field(name, MAX_NAME)
    if not nm:
        raise ValueError("任务名称不能为空")
    conn = _connect()
    try:
        with conn:
            row = conn.execute("SELECT * FROM task_type WHERE name = ?", (nm,)).fetchone()
            if row is None:
                mx = conn.execute("SELECT IFNULL(MAX(sort_no), 0) AS m FROM task_type").fetchone()
                conn.execute(
                    "INSERT INTO task_type (name, sort_no, enabled, updated_at) VALUES (?, ?, ?, ?)",
                    (nm, int(sort_no) if sort_no is not None else int(mx["m"]) + 10,
                     1 if (enabled is None or enabled) else 0, _now()),
                )
            else:
                conn.execute("UPDATE task_type SET sort_no = ?, enabled = ?, updated_at = ? WHERE name = ?",
                             (int(sort_no) if sort_no is not None else int(row["sort_no"]),
                              (1 if enabled else 0) if enabled is not None else int(row["enabled"]),
                              _now(), nm))
    finally:
        conn.close()
    return get_task_type_by_name(nm) or {}


def delete_task_type(tid: int) -> None:
    """删除任务类型：已有列方案或任务量则拒绝（改为停用）。"""
    row = get_task_type(tid)
    if row is None:
        raise ValueError(f"任务不存在：{tid}")
    conn = _connect()
    try:
        used = conn.execute("SELECT COUNT(*) AS n FROM scheme WHERE task_type_id = ?", (int(tid),)).fetchone()["n"]
        q = conn.execute("SELECT COUNT(*) AS n FROM task_quota WHERE task_type_id = ?", (int(tid),)).fetchone()["n"]
        if used or q:
            raise ValueError(f"任务「{row['name']}」已有 {used} 个年度方案、{q} 条任务量，不能删除（可改为「停用」）")
        with conn:
            conn.execute("DELETE FROM task_type WHERE id = ?", (int(tid),))
    finally:
        conn.close()


# ==================== 列方案（任务 × 年份 = 一套表头列） ====================


def list_schemes(year: int | None = None, task_type_id: int | None = None) -> list[dict]:
    """列方案清单；附 `category_count`（列数）与 `quota_total`（任务量合计）、`done_total`（完成量）。"""
    where, params = [], []
    if year is not None:
        where.append("s.year = ?")
        params.append(_check_year(year))
    if task_type_id is not None:
        where.append("s.task_type_id = ?")
        params.append(int(task_type_id))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT s.*, t.name AS task_name, t.enabled AS task_enabled, "
        "(SELECT COUNT(*) FROM category c WHERE c.scheme_id = s.id) AS category_count, "
        "(SELECT COUNT(*) FROM category c WHERE c.scheme_id = s.id AND c.enabled = 1) AS category_enabled, "
        "(SELECT IFNULL(SUM(q.quota), 0) FROM task_quota q WHERE q.year = s.year "
        "  AND q.task_type_id = s.task_type_id) AS quota_total, "
        "(SELECT COUNT(*) FROM done_sample d WHERE d.year = s.year AND d.task_type_id = s.task_type_id) AS done_total "
        "FROM scheme s LEFT JOIN task_type t ON t.id = s.task_type_id" + clause + " ORDER BY s.year DESC, t.sort_no, s.id"
    )
    conn = _connect()
    try:
        return _rows(conn.execute(sql, tuple(params)))
    finally:
        conn.close()


def get_scheme(year: int, task_type_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM scheme WHERE year = ? AND task_type_id = ?",
                           (_check_year(year), int(task_type_id))).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_scheme_by_id(scheme_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM scheme WHERE id = ?", (int(scheme_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def ensure_scheme(year: int, task_type_id: int, note: str = "") -> dict:
    """取（没有就建）某任务某年的列方案。新方案是**空的**（列由 `copy_scheme` 或页面添加）。"""
    y = _check_year(year)
    if get_task_type(task_type_id) is None:
        raise ValueError(f"任务不存在：{task_type_id}")
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO scheme (year, task_type_id, note, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(year, task_type_id) DO NOTHING",
                (y, int(task_type_id), clean_field(note, MAX_NOTE), _now()),
            )
            if note:
                conn.execute("UPDATE scheme SET note = ?, updated_at = ? WHERE year = ? AND task_type_id = ?",
                             (clean_field(note, MAX_NOTE), _now(), y, int(task_type_id)))
    finally:
        conn.close()
    return get_scheme(y, int(task_type_id)) or {}


def copy_scheme(src_scheme_id: int, dst_scheme_id: int, note: str = "") -> dict:
    """把一套列方案**整体复制**到另一套（列名 / 大类 / 兜底桶 / 关键词 / 顺序），目标原列先清掉。

    只复制"表头定义"，**不动任务量**：任务量是下达量，换年份/换任务后要重新录；
    目标方案上已有的任务量若因清列而失去引用，会自然变成"无列的任务量"（页面按列查不到，
    因此这里直接拒绝：目标方案已有任务量时不允许覆盖复制）。
    """
    src = get_scheme_by_id(src_scheme_id)
    dst = get_scheme_by_id(dst_scheme_id)
    if src is None or dst is None:
        raise ValueError("列方案不存在")
    if int(src["id"]) == int(dst["id"]):
        raise ValueError("源方案与目标方案相同")
    conn = _connect()
    try:
        q = conn.execute("SELECT COUNT(*) AS n FROM task_quota WHERE year = ? AND task_type_id = ?",
                         (int(dst["year"]), int(dst["task_type_id"]))).fetchone()["n"]
        if q:
            raise ValueError(f"目标任务已有 {q} 条任务量，覆盖列方案会丢掉对应关系；请先清空任务量")
        copied_cat = 0
        copied_key = 0
        with conn:
            # 目标方案的旧列整批清掉（含其关键词载体）
            conn.execute("DELETE FROM product_type WHERE category_id IN (SELECT id FROM category WHERE scheme_id = ?)",
                         (int(dst["id"]),))
            conn.execute("DELETE FROM category WHERE scheme_id = ?", (int(dst["id"]),))
            for cat in _rows(conn.execute("SELECT * FROM category WHERE scheme_id = ? ORDER BY sort_no, id",
                                          (int(src["id"]),))):
                cur = conn.execute(
                    "INSERT INTO category (name, big_kind, is_other, sort_no, enabled, updated_at, scheme_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(cat["name"]), str(cat["big_kind"] or ""), int(cat["is_other"] or 0),
                     int(cat["sort_no"] or 0), int(cat["enabled"] or 0), _now(), int(dst["id"])),
                )
                new_cid = int(cur.lastrowid or 0)
                copied_cat += 1
                for p in _rows(conn.execute(
                        "SELECT * FROM product_type WHERE category_id = ? ORDER BY sort_no, id", (int(cat["id"]),))):
                    conn.execute(
                        "INSERT INTO product_type (category_id, name, keywords, is_other, sort_no, enabled, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (new_cid, str(p["name"]), str(p["keywords"] or ""), int(p["is_other"] or 0),
                         int(p["sort_no"] or 0), int(p["enabled"] or 0), _now()),
                    )
                    copied_key += 1
            if note:
                conn.execute("UPDATE scheme SET note = ?, updated_at = ? WHERE id = ?",
                             (clean_field(note, MAX_NOTE), _now(), int(dst["id"])))
    finally:
        conn.close()
    _LOG.info("列方案复制：src=%s dst=%s 列=%s 关键词单元=%s", src_scheme_id, dst_scheme_id, copied_cat, copied_key)
    return {"categories": copied_cat, "product_types": copied_key}


def assign_category_scheme(scheme_id: int, only_orphan: bool = True) -> int:
    """把「还没归属任何方案」的品类（scheme_id=0，老库遗留）整体挂到指定列方案；返回条数。

    老库升级用：老版本只有一套全局列（就是使用方市例行考核表的那 15 列），
    因此整体挂到「市例行」当年方案；其它任务再从这个方案复制一份。
    """
    if get_scheme_by_id(scheme_id) is None:
        raise ValueError(f"列方案不存在：{scheme_id}")
    conn = _connect()
    try:
        with conn:
            where = " WHERE scheme_id = 0" if only_orphan else ""
            cur = conn.execute(f"UPDATE category SET scheme_id = ?, updated_at = ?" + where,
                               (int(scheme_id), _now()))
            return int(cur.rowcount or 0)
    finally:
        conn.close()


def delete_scheme(scheme_id: int) -> None:
    """删除列方案（连同其列与关键词）；有任务量则拒绝。"""
    row = get_scheme_by_id(scheme_id)
    if row is None:
        raise ValueError(f"列方案不存在：{scheme_id}")
    conn = _connect()
    try:
        q = conn.execute("SELECT COUNT(*) AS n FROM task_quota WHERE year = ? AND task_type_id = ?",
                         (int(row["year"]), int(row["task_type_id"]))).fetchone()["n"]
        if q:
            raise ValueError(f"该方案已有 {q} 条任务量，不能删除（请先清空任务量）")
        with conn:
            conn.execute("DELETE FROM product_type WHERE category_id IN (SELECT id FROM category WHERE scheme_id = ?)",
                         (int(scheme_id),))
            conn.execute("DELETE FROM category WHERE scheme_id = ?", (int(scheme_id),))
            conn.execute("DELETE FROM scheme WHERE id = ?", (int(scheme_id),))
    finally:
        conn.close()


# ==================== 品类（看板一级表头） ====================


def list_categories(include_disabled: bool = True, year: int | None = None,
                    scheme_id: int | None = None) -> list[dict]:
    """品类列表（按 sort_no, id）；传 year 时附任务量/完成量合计，传 scheme_id 只看该列方案。

    `keywords` 取自该品类下的**归属单元**（见 `consolidate_categories()`）：界面上品类就是唯一的分类单元，
    关键词直接挂在品类这一层，用户不必知道底下还有一个内部表。
    """
    where, params = [], []
    if not include_disabled:
        where.append("c.enabled = 1")
    if scheme_id is not None:
        where.append("c.scheme_id = ?")
        params.append(int(scheme_id))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    y = _check_year(year) if year is not None else -1
    sql = (
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM product_type p WHERE p.category_id = c.id AND p.enabled = 1) AS product_count, "
        "(SELECT p.keywords FROM product_type p WHERE p.category_id = c.id AND p.enabled = 1 "
        "  ORDER BY p.sort_no, p.id LIMIT 1) AS keywords, "
        # 品类任务量 = 产品级之和 + 该品类"品类级"下达量（使用方的考核表是按品类下达的）
        "(SELECT IFNULL(SUM(q.quota), 0) FROM task_quota q LEFT JOIN product_type p ON p.id = q.product_type_id "
        "  WHERE q.year = ? AND (p.category_id = c.id OR q.category_id = c.id)) AS quota_total, "
        "(SELECT COUNT(*) FROM done_sample d JOIN product_type p ON p.id = d.product_type_id "
        "  WHERE p.category_id = c.id AND d.year = ?) AS done_total "
        "FROM category c" + clause + " ORDER BY c.sort_no, c.id"
    )
    conn = _connect()
    try:
        return _rows(conn.execute(sql, (y, y, *params)))
    finally:
        conn.close()


def category_rep_product(cat_id: int) -> dict | None:
    """品类的**归属单元**（内部表 product_type 里该品类下启用的那一条）。

    它是"这一列接住哪些样品"的载体：名称与关键词都在它身上。界面上不暴露这一层。
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM product_type WHERE category_id = ? AND enabled = 1 ORDER BY sort_no, id LIMIT 1",
            (int(cat_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_category_keywords(cat_id: int, keywords: str) -> dict:
    """写品类的匹配关键词（落到归属单元上）；没有归属单元就建一个。"""
    cat = get_category(cat_id)
    if cat is None:
        raise ValueError(f"品类不存在：{cat_id}")
    kw = clean_field(keywords, MAX_KEYS)
    rep = category_rep_product(cat_id)
    if rep is None:
        return create_product_type(int(cat_id), str(cat["name"]), keywords=kw,
                                   is_other=bool(int(cat["is_other"] or 0)))
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE product_type SET keywords = ?, updated_at = ? WHERE id = ?",
                         (kw, _now(), int(rep["id"])))
    finally:
        conn.close()
    return get_product_type(int(rep["id"])) or {}


def consolidate_categories() -> dict:
    """把每个品类收敛为**一个归属单元**（幂等）：关键词合并、历史数据改指、多余条目删除。

    为什么要做：使用方只需要"品类"这一层（考核表的 15 列），产品类型不再出现在界面上。
    合并规则：关键词 = 原各归属单元的名称 + 关键词的并集（去重），这样「豇豆、芹菜、辣椒」
    这一列仍然同时接住 豇豆/芹菜/辣椒/豆角/青椒 等写法，**完成量一分不差**。
    历史数据（完成量明细、任务量）会改指到保留的归属单元，因此不会丢数。
    """
    conn = _connect()
    try:
        merged = kept = removed = 0
        for cat in _rows(conn.execute("SELECT * FROM category ORDER BY sort_no, id")):
            cid = int(cat["id"])
            items = _rows(conn.execute(
                "SELECT * FROM product_type WHERE category_id = ? ORDER BY enabled DESC, sort_no, id", (cid,)))
            if not items:
                continue
            keep = items[0]
            keys: list[str] = []
            for it in items:
                for raw in [str(it["name"]), *str(it["keywords"] or "").split(",")]:
                    for seg in re.split(r"[,\n、，;；|/]+", str(raw)):
                        s = seg.strip()
                        if s and s not in keys:
                            keys.append(s)
            drop_ids = [int(it["id"]) for it in items[1:]]
            with conn:
                conn.execute("UPDATE product_type SET name = ?, keywords = ?, is_other = ?, enabled = 1, "
                             "updated_at = ? WHERE id = ?",
                             (str(cat["name"]), ",".join(keys), int(cat["is_other"] or 0),
                              _now(), int(keep["id"])))
                if drop_ids:
                    marks = ",".join("?" * len(drop_ids))
                    # 历史数据改指：完成量明细 + 产品级任务量
                    conn.execute(f"UPDATE done_sample SET product_type_id = ? WHERE product_type_id IN ({marks})",
                                 (int(keep["id"]), *drop_ids))
                    conn.execute(f"UPDATE task_quota SET product_type_id = ? WHERE product_type_id IN ({marks})",
                                 (int(keep["id"]), *drop_ids))
                    # 任务量合并后可能撞主键：先把重复的按 (year, contract, region, category_id) 汇总，再删旧行
                    conn.execute(
                        "DELETE FROM task_quota WHERE product_type_id = ? AND EXISTS ("
                        "  SELECT 1 FROM task_quota x WHERE x.product_type_id = ? AND x.year = task_quota.year"
                        "    AND x.contract_no = task_quota.contract_no AND x.region = task_quota.region"
                        "    AND x.category_id = task_quota.category_id AND x.rowid < task_quota.rowid)",
                        (int(keep["id"]), int(keep["id"])))
                    conn.execute(f"DELETE FROM product_type WHERE id IN ({marks})", tuple(drop_ids))
                    merged += 1
                    removed += len(drop_ids)
                kept += 1
    finally:
        conn.close()
    _LOG.info("品类收敛完成：保留 %s 个归属单元，合并 %s 个品类，删除多余条目 %s 条", kept, merged, removed)
    return {"categories": kept, "merged": merged, "removed": removed}


def get_category(cat_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM category WHERE id = ?", (int(cat_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_category(name: str, big_kind: str = "", is_other: bool = False,
                    sort_no: int | None = None, enabled: bool = True,
                    keywords: str = "", scheme_id: int = 0) -> dict:
    """新增品类（表头列）；同时建它的**归属单元**（关键词 = 品类名 + 传入关键词），界面上只见品类。

    `scheme_id` = 这一列属于哪个任务哪一年的表（必填：不同任务允许有同名但含义不同的列）。
    """
    nm = clean_field(name, MAX_NAME)
    if not nm:
        raise ValueError("品类名称不能为空")
    sid = int(scheme_id or 0)
    if sid <= 0:
        raise ValueError("新增品类必须指定所属列方案（任务 × 年份）")
    if get_scheme_by_id(sid) is None:
        raise ValueError(f"列方案不存在：{sid}")
    conn = _connect()
    try:
        with conn:
            dup = conn.execute("SELECT id FROM category WHERE name = ? AND scheme_id = ?", (nm, sid)).fetchone()
            if dup:
                raise ValueError(f"本方案下已有列「{nm}」")
            if sort_no is None:
                mx = conn.execute("SELECT IFNULL(MAX(sort_no), 0) AS m FROM category WHERE scheme_id = ?",
                                  (sid,)).fetchone()
                sort_no = int(mx["m"]) + 10
            cur = conn.execute(
                "INSERT INTO category (name, big_kind, is_other, sort_no, enabled, updated_at, scheme_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (nm, clean_field(big_kind, MAX_BIG_KIND), 1 if is_other else 0,
                 int(sort_no), 1 if enabled else 0, _now(), sid),
            )
            new_id = int(cur.lastrowid or 0)
    finally:
        conn.close()
    create_product_type(new_id, nm, keywords=clean_field(keywords, MAX_KEYS) or nm,
                        is_other=bool(is_other))
    return get_category(new_id) or {}


def update_category(cat_id: int, name: str | None = None, big_kind: str | None = None,
                    is_other: bool | None = None, sort_no: int | None = None,
                    enabled: bool | None = None, keywords: str | None = None) -> dict:
    """局部更新（None = 不改）。改名称只是换表头显示，历史数据靠 id 关联，不受影响。

    `keywords` 写的是"这一列接住哪些样品"（落在归属单元上）。
    """
    cur_row = get_category(cat_id)
    if cur_row is None:
        raise ValueError(f"品类不存在：{cat_id}")
    nm = clean_field(name, MAX_NAME) if name is not None else str(cur_row["name"])
    if not nm:
        raise ValueError("品类名称不能为空")
    conn = _connect()
    try:
        with conn:
            dup = conn.execute("SELECT id FROM category WHERE name = ? AND scheme_id = ? AND id <> ?",
                               (nm, int(cur_row["scheme_id"] or 0), int(cat_id))).fetchone()
            if dup:
                raise ValueError(f"本方案下已有列「{nm}」")
            conn.execute(
                "UPDATE category SET name = ?, big_kind = ?, is_other = ?, sort_no = ?, enabled = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    nm,
                    clean_field(big_kind, MAX_BIG_KIND) if big_kind is not None else str(cur_row["big_kind"]),
                    (1 if is_other else 0) if is_other is not None else int(cur_row["is_other"]),
                    int(sort_no) if sort_no is not None else int(cur_row["sort_no"]),
                    (1 if enabled else 0) if enabled is not None else int(cur_row["enabled"]),
                    _now(), int(cat_id),
                ),
            )
    finally:
        conn.close()
    if keywords is not None:
        set_category_keywords(cat_id, keywords)
    return get_category(cat_id) or {}


def delete_category(cat_id: int) -> None:
    """删除品类（连同它的归属单元）：被任务量引用 → 拒绝（可停用）。

    删除后原先归到这一列的完成量会变成「未归类」，重新同步或点「重算归类」即可看到提示。
    """
    cur_row = get_category(cat_id)
    if cur_row is None:
        raise ValueError(f"品类不存在：{cat_id}")
    conn = _connect()
    try:
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM task_quota q LEFT JOIN product_type p ON p.id = q.product_type_id "
            "WHERE p.category_id = ? OR q.category_id = ?", (int(cat_id), int(cat_id)),
        ).fetchone()["n"]
        if used:
            raise ValueError(f"品类「{cur_row['name']}」已有 {used} 条任务量记录，不能删除（可改为「停用」）")
        with conn:
            conn.execute("DELETE FROM product_type WHERE category_id = ?", (int(cat_id),))
            conn.execute("DELETE FROM category WHERE id = ?", (int(cat_id),))
    finally:
        conn.close()


def move_category(cat_id: int, direction: str) -> list[dict]:
    """上移 / 下移一个品类（与**同方案内**相邻项交换 sort_no）。"""
    if direction not in ("up", "down"):
        raise ValueError("direction 只能是 up / down")
    cur_row = get_category(cat_id)
    if cur_row is None:
        raise ValueError(f"品类不存在：{cat_id}")
    sid = int(cur_row["scheme_id"] or 0)
    items = list_categories(include_disabled=True, scheme_id=sid)
    idx = next((i for i, it in enumerate(items) if int(it["id"]) == int(cat_id)), -1)
    if idx < 0:
        raise ValueError(f"品类不存在：{cat_id}")
    swap_idx = idx - 1 if direction == "up" else idx + 1
    if swap_idx < 0 or swap_idx >= len(items):
        return items
    a, b = items[idx], items[swap_idx]
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE category SET sort_no = ?, updated_at = ? WHERE id = ?", (int(b["sort_no"]), _now(), int(a["id"])))
            conn.execute("UPDATE category SET sort_no = ?, updated_at = ? WHERE id = ?", (int(a["sort_no"]), _now(), int(b["id"])))
    finally:
        conn.close()
    return list_categories(include_disabled=True, scheme_id=sid)


# ==================== 产品类型（看板二级表头） ====================


def list_product_types(include_disabled: bool = True, year: int | None = None,
                       category_id: int | None = None, scheme_id: int | None = None) -> list[dict]:
    """产品类型列表（按 sort_no, id）；传 year 时附任务量/完成量合计，传 scheme_id 只看该列方案。"""
    where, params = [], []
    if not include_disabled:
        where.append("p.enabled = 1")
    if category_id is not None:
        where.append("p.category_id = ?")
        params.append(int(category_id))
    if scheme_id is not None:
        where.append("c.scheme_id = ?")
        params.append(int(scheme_id))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    y = _check_year(year) if year is not None else -1
    sql = (
        # ⚠️ 必须带出 category.scheme_id：服务层按它判断"这一列属于哪个任务的方案"
        "SELECT p.*, c.name AS category_name, c.big_kind AS big_kind, c.is_other AS category_is_other, "
        "c.scheme_id AS scheme_id, "
        "(SELECT IFNULL(SUM(q.quota), 0) FROM task_quota q WHERE q.product_type_id = p.id AND q.year = ?) AS quota_total, "
        "(SELECT COUNT(*) FROM done_sample d WHERE d.product_type_id = p.id AND d.year = ?) AS done_total "
        "FROM product_type p LEFT JOIN category c ON c.id = p.category_id" + clause +
        " ORDER BY p.sort_no, p.id"
    )
    conn = _connect()
    try:
        return _rows(conn.execute(sql, (y, y, *params)))
    finally:
        conn.close()


def get_product_type(pt_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM product_type WHERE id = ?", (int(pt_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_product_type_by_name(cat_id: int, name: str) -> dict | None:
    nm = clean_field(name, MAX_NAME)
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM product_type WHERE category_id = ? AND name = ?",
                           (int(cat_id), nm)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_product_type(category_id: int, name: str, keywords: str = "",
                        is_other: bool = False, sort_no: int | None = None,
                        enabled: bool = True) -> dict:
    """新增产品类型。`keywords` = 匹配用的品种名/别名（「豆角」也能命中「豇豆」这一列）。"""
    nm = clean_field(name, MAX_NAME)
    if not nm:
        raise ValueError("产品类型名称不能为空")
    if get_category(category_id) is None:
        raise ValueError(f"品类不存在：{category_id}")
    conn = _connect()
    try:
        with conn:
            if conn.execute("SELECT id FROM product_type WHERE category_id = ? AND name = ?",
                            (int(category_id), nm)).fetchone():
                raise ValueError(f"该品类下已有产品类型「{nm}」")
            if sort_no is None:
                mx = conn.execute("SELECT IFNULL(MAX(sort_no), 0) AS m FROM product_type WHERE category_id = ?",
                                  (int(category_id),)).fetchone()
                sort_no = int(mx["m"]) + 10
            cur = conn.execute(
                "INSERT INTO product_type (category_id, name, keywords, is_other, sort_no, enabled, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (int(category_id), nm, clean_field(keywords, MAX_KEYS), 1 if is_other else 0,
                 int(sort_no), 1 if enabled else 0, _now()),
            )
            new_id = int(cur.lastrowid or 0)
    finally:
        conn.close()
    return get_product_type(new_id) or {}


def update_product_type(pt_id: int, name: str | None = None, keywords: str | None = None,
                        category_id: int | None = None, is_other: bool | None = None,
                        sort_no: int | None = None, enabled: bool | None = None) -> dict:
    cur_row = get_product_type(pt_id)
    if cur_row is None:
        raise ValueError(f"产品类型不存在：{pt_id}")
    nm = clean_field(name, MAX_NAME) if name is not None else str(cur_row["name"])
    if not nm:
        raise ValueError("产品类型名称不能为空")
    cat_id = int(category_id) if category_id is not None else int(cur_row["category_id"])
    conn = _connect()
    try:
        with conn:
            if name is not None or category_id is not None:
                dup = conn.execute(
                    "SELECT id FROM product_type WHERE category_id = ? AND name = ? AND id <> ?",
                    (cat_id, nm, int(pt_id)),
                ).fetchone()
                if dup:
                    raise ValueError(f"目标品类下已有产品类型「{nm}」")
            conn.execute(
                "UPDATE product_type SET name = ?, keywords = ?, category_id = ?, is_other = ?, "
                "sort_no = ?, enabled = ?, updated_at = ? WHERE id = ?",
                (
                    nm,
                    clean_field(keywords, MAX_KEYS) if keywords is not None else str(cur_row["keywords"]),
                    cat_id,
                    (1 if is_other else 0) if is_other is not None else int(cur_row["is_other"]),
                    int(sort_no) if sort_no is not None else int(cur_row["sort_no"]),
                    (1 if enabled else 0) if enabled is not None else int(cur_row["enabled"]),
                    _now(), int(pt_id),
                ),
            )
    finally:
        conn.close()
    return get_product_type(pt_id) or {}


def delete_product_type(pt_id: int) -> None:
    """删除产品类型：被任务量引用则拒绝（改为停用），否则物理删除。"""
    cur_row = get_product_type(pt_id)
    if cur_row is None:
        raise ValueError(f"产品类型不存在：{pt_id}")
    conn = _connect()
    try:
        q = conn.execute("SELECT COUNT(*) AS n FROM task_quota WHERE product_type_id = ?", (int(pt_id),)).fetchone()["n"]
        if q:
            raise ValueError(f"「{cur_row['name']}」已有 {q} 条任务量记录，不能删除（可改为「停用」）")
        with conn:
            conn.execute("DELETE FROM product_type WHERE id = ?", (int(pt_id),))
            # 完成量明细里指向它的归类结果一并清空，下次重算会按新配置重新归集
            conn.execute("UPDATE done_sample SET product_type_id = 0, match_layer = '' WHERE product_type_id = ?",
                         (int(pt_id),))
    finally:
        conn.close()


def move_product_type(pt_id: int, direction: str) -> list[dict]:
    """上移 / 下移一列（只在**同一品类内**交换，避免把列挪到别的品类里）。"""
    if direction not in ("up", "down"):
        raise ValueError("direction 只能是 up / down")
    cur_row = get_product_type(pt_id)
    if cur_row is None:
        raise ValueError(f"产品类型不存在：{pt_id}")
    items = [p for p in list_product_types(include_disabled=True, category_id=int(cur_row["category_id"]))]
    idx = next((i for i, it in enumerate(items) if int(it["id"]) == int(pt_id)), -1)
    swap_idx = idx - 1 if direction == "up" else idx + 1
    if idx < 0 or swap_idx < 0 or swap_idx >= len(items):
        return items
    a, b = items[idx], items[swap_idx]
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE product_type SET sort_no = ?, updated_at = ? WHERE id = ?", (int(b["sort_no"]), _now(), int(a["id"])))
            conn.execute("UPDATE product_type SET sort_no = ?, updated_at = ? WHERE id = ?", (int(a["sort_no"]), _now(), int(b["id"])))
    finally:
        conn.close()
    return list_product_types(include_disabled=True, category_id=int(cur_row["category_id"]))


# ==================== 区域（看板行） ====================


def list_regions(include_disabled: bool = True, year: int | None = None) -> list[dict]:
    """区域列表（按 sort_no, name）；传 year 时附任务量与完成量合计。"""
    where = "" if include_disabled else " WHERE r.enabled = 1"
    y = _check_year(year) if year is not None else -1
    sql = (
        "SELECT r.*, "
        "(SELECT IFNULL(SUM(q.quota), 0) FROM task_quota q WHERE q.region = r.name AND q.year = ?) AS quota_total, "
        "(SELECT COUNT(*) FROM done_sample d WHERE d.region = r.name AND d.year = ?) AS done_total "
        "FROM region r" + where + " ORDER BY r.sort_no, r.name"
    )
    conn = _connect()
    try:
        return _rows(conn.execute(sql, (y, y)))
    finally:
        conn.close()


def upsert_region(name: str, keys: str | None = None, sort_no: int | None = None,
                  enabled: bool | None = None, source: str = SOURCE_MANUAL) -> dict:
    """新增 / 修改区域。`keys` = 地址里可能出现的写法（如「虎丘区/新区」→ 高新区）。"""
    nm = clean_field(name, MAX_REGION)
    if not nm:
        raise ValueError("区域名称不能为空")
    conn = _connect()
    try:
        with conn:
            row = conn.execute("SELECT * FROM region WHERE name = ?", (nm,)).fetchone()
            if row is None:
                mx = conn.execute("SELECT IFNULL(MAX(sort_no), 0) AS m FROM region").fetchone()
                conn.execute(
                    "INSERT INTO region (name, keys, sort_no, enabled, source, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (nm, clean_field(keys, MAX_KEYS), int(sort_no) if sort_no is not None else int(mx["m"]) + 10,
                     1 if (enabled is None or enabled) else 0, source, _now()),
                )
            else:
                conn.execute(
                    "UPDATE region SET keys = ?, sort_no = ?, enabled = ?, updated_at = ? WHERE name = ?",
                    (
                        clean_field(keys, MAX_KEYS) if keys is not None else str(row["keys"]),
                        int(sort_no) if sort_no is not None else int(row["sort_no"]),
                        (1 if enabled else 0) if enabled is not None else int(row["enabled"]),
                        _now(), nm,
                    ),
                )
    finally:
        conn.close()
    return next((r for r in list_regions() if r["name"] == nm), {})


def ensure_regions(names: list[str], source: str = SOURCE_AUTO) -> int:
    """批量登记区域（已存在则**不动 keys/启停/排序**）；返回新增条数。"""
    ts = _now()
    added = 0
    conn = _connect()
    try:
        with conn:
            sort_no = int(conn.execute("SELECT IFNULL(MAX(sort_no), 0) AS m FROM region").fetchone()["m"] or 0)
            for raw in names:
                nm = clean_field(raw, MAX_REGION)
                if not nm:
                    continue
                sort_no += 10
                cur = conn.execute(
                    "INSERT INTO region (name, keys, sort_no, enabled, source, updated_at) "
                    "VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT(name) DO NOTHING",
                    (nm, nm, sort_no, source, ts),
                )
                if cur.rowcount:
                    added += 1
    finally:
        conn.close()
    return added


def rename_region(old: str, new: str) -> int:
    """改看板行的显示名（业务改名，如「昆山市」→「昆山区」）：任务量、明细里的旧名一并改指。

    区域靠名字关联（`region.name` 是主键），所以改名必须同步改 `task_quota` 与 `done_sample`，
    否则历史数据会变成"查不到的行"。已存在同名区域时不做任何事（返回 0）。
    """
    a = clean_field(old, MAX_REGION)
    b = clean_field(new, MAX_REGION)
    if not a or not b or a == b:
        return 0
    conn = _connect()
    try:
        if conn.execute("SELECT 1 FROM region WHERE name = ?", (b,)).fetchone():
            return 0
        if not conn.execute("SELECT 1 FROM region WHERE name = ?", (a,)).fetchone():
            return 0
        with conn:
            conn.execute("UPDATE region SET name = ?, updated_at = ? WHERE name = ?", (b, _now(), a))
            conn.execute("UPDATE task_quota SET region = ? WHERE region = ?", (b, a))
            conn.execute("UPDATE done_sample SET region = ? WHERE region = ?", (b, a))
            _LOG.warning("progress 区域改名：%s → %s（任务量与明细已同步改指）", a, b)
        return 1
    finally:
        conn.close()


def delete_region(name: str) -> None:
    nm = clean_field(name, MAX_REGION)
    conn = _connect()
    try:
        q = conn.execute("SELECT COUNT(*) AS n FROM task_quota WHERE region = ?", (nm,)).fetchone()["n"]
        if q:
            raise ValueError(f"区域「{nm}」已有 {q} 条任务量记录，不能删除（可改为「停用」）")
        with conn:
            cur = conn.execute("DELETE FROM region WHERE name = ?", (nm,))
            if not cur.rowcount:
                raise ValueError(f"区域不存在：{nm}")
            # 明细里挂到这个区域的样品退回"未映射"，下次重算会重新提示
            conn.execute("UPDATE done_sample SET region = '' WHERE region = ?", (nm,))
    finally:
        conn.close()


# ==================== 合同 ====================


def list_contracts(year: int | None = None, include_disabled: bool = True) -> list[dict]:
    """合同列表；传 year 时附该年任务量条数/合计与样品数（供配置页展示影响面）。"""
    where = "" if include_disabled else " WHERE enabled = 1"
    y = _check_year(year) if year is not None else -1
    sql = (
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM task_quota q WHERE q.contract_no = c.contract_no AND q.year = ?) AS quota_rows, "
        "(SELECT IFNULL(SUM(q.quota), 0) FROM task_quota q WHERE q.contract_no = c.contract_no AND q.year = ?) AS quota_total, "
        "(SELECT COUNT(*) FROM done_sample d WHERE d.contract_no = c.contract_no AND d.year = ?) AS done_total "
        "FROM contract c" + where + " ORDER BY c.contract_no"
    )
    conn = _connect()
    try:
        return _rows(conn.execute(sql, (y, y, y)))
    finally:
        conn.close()


def get_contract(contract_no: str) -> dict | None:
    no = contract_key(contract_no)
    if not no:
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM contract WHERE contract_no = ?", (no,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_contract(contract_no: str, name: str | None = None, note: str = "",
                    enabled: bool = True, source: str = SOURCE_MANUAL,
                    alias_of: str | None = None, task_type_id: int | None = None) -> dict:
    """新增或更新合同。

    `name=None` 表示**不改名称**（传 None 才不会把已有值清空），`name=''` 才是显式清空。
    `name_locked`：名称一旦由人工填写即加锁，此后镜像自动发现不再覆盖。
    `task_type_id`：这份合同属于哪个任务（None = 不改动）；同属一个任务的合同会并入同一张表。
    """
    no = contract_key(contract_no)
    if not no:
        raise ValueError("合同编号不能为空")
    existing = get_contract(no) or {}
    nm = clean_field(name, MAX_NAME) if name is not None else str(existing.get("name") or "")
    # 归并目标：None = 不改；'' = 取消归并；其他 = 归并到该合同编号（不能归并到自己）
    if alias_of is None:
        target = contract_key(existing.get("alias_of"))
    else:
        target = contract_key(alias_of)
    if target == no:
        target = ""
    tt = int(task_type_id) if task_type_id is not None else int(existing.get("task_type_id") or 0)
    if tt and get_task_type(tt) is None:
        raise ValueError(f"任务不存在：{tt}")
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO contract (contract_no, name, note, source, enabled, name_locked, updated_at, alias_of,"
                " task_type_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(contract_no) DO UPDATE SET "
                "  name = excluded.name, note = excluded.note, "
                "  source = CASE WHEN contract.source = 'manual' THEN 'manual' ELSE excluded.source END, "
                "  enabled = excluded.enabled, "
                "  name_locked = CASE WHEN excluded.name <> '' THEN 1 ELSE contract.name_locked END, "
                "  alias_of = excluded.alias_of, "
                "  task_type_id = excluded.task_type_id, "
                "  updated_at = excluded.updated_at",
                # ⚠️ 这里必须绑 `target`（归一化后的归并目标），不能绑原始入参 `alias_of`：
                #   1) `alias_of` 默认是 None，直接绑会撞 NOT NULL（新建/只改备注都崩）；
                #   2) `alias_of` 未归一化（' C1 ' 原样入库），且"不能归并到自己"的防护失效。
                #   两个缺陷同一个根因：算好的 `target` 被丢掉没用，详见 tests 中的合同用例。
                (no, nm, clean_field(note, MAX_NOTE),
                 SOURCE_MANUAL if source != SOURCE_AUTO else SOURCE_AUTO,
                 1 if enabled else 0, 1 if nm else 0, _now(), target, tt),
            )
    finally:
        conn.close()
    return get_contract(no) or {}


def set_contract_task(contract_no: str, task_type_id: int) -> int:
    """只改「这份合同属于哪个任务」，**不动**名称/启停/归并（自动识别与人工设置都要用它）。"""
    no = contract_key(contract_no)
    if not no:
        raise ValueError("合同编号不能为空")
    if int(task_type_id or 0) and get_task_type(int(task_type_id)) is None:
        raise ValueError(f"任务不存在：{task_type_id}")
    conn = _connect()
    try:
        with conn:
            cur = conn.execute("UPDATE contract SET task_type_id = ?, updated_at = ? WHERE contract_no = ?",
                               (int(task_type_id or 0), _now(), no))
            return int(cur.rowcount or 0)
    finally:
        conn.close()


def upsert_auto_contracts(numbers: list[str]) -> int:
    """镜像自动发现写合同清单；返回新增条数。已存在的合同**一律不覆盖**（用户的手工调整必须活过每次同步）。"""
    added = 0
    ts = _now()
    conn = _connect()
    try:
        with conn:
            for raw in numbers:
                no = contract_key(raw)
                if not no:
                    continue
                cur = conn.execute(
                    "INSERT INTO contract (contract_no, name, note, source, enabled, name_locked, updated_at) "
                    "VALUES (?, '', '', 'auto', 1, 0, ?) ON CONFLICT(contract_no) DO NOTHING",
                    (no, ts),
                )
                if cur.rowcount:
                    added += 1
    finally:
        conn.close()
    return added


def delete_contract(contract_no: str) -> None:
    """删除合同：被任务量引用则拒绝；镜像自动发现的一律拒绝（下次同步会再出现，只能停用）。"""
    no = contract_key(contract_no)
    row = get_contract(no)
    if row is None:
        raise ValueError(f"合同不存在：{no}")
    conn = _connect()
    try:
        used = conn.execute("SELECT COUNT(*) AS n FROM task_quota WHERE contract_no = ?", (no,)).fetchone()["n"]
        if used:
            raise ValueError(f"该合同已有 {used} 条任务量记录，不能删除（可改为「停用」）")
        if str(row.get("source")) == SOURCE_AUTO:
            raise ValueError("该合同来自镜像自动发现，删除后下次同步会再次出现，请改为「停用」")
        with conn:
            conn.execute("DELETE FROM contract WHERE contract_no = ?", (no,))
    finally:
        conn.close()


# ==================== 任务量 ====================


def read_quotas(year: int, task_type_id: int | None = None) -> list[dict]:
    """任务量明细（键 = 年 + 任务 + 区域 + 产品类型/品类）。"""
    y = _check_year(year)
    sql = ("SELECT year, task_type_id, region, product_type_id, category_id, quota, updated_at, contract_no "
           "FROM task_quota WHERE year = ?")
    params: list = [y]
    if task_type_id is not None:
        sql += " AND task_type_id = ?"
        params.append(int(task_type_id))
    conn = _connect()
    try:
        return _rows(conn.execute(sql, tuple(params)))
    finally:
        conn.close()


def save_quotas(year: int, items: list[dict]) -> int:
    """批量写入任务量（upsert，同事务）；返回写入条数。

    条目必须带 `task_type_id`（任务量按**任务**下达，不再按合同）。
    整批一次事务：任何一条非法都整体回滚，**不会留下"改了一半"的任务量**。
    """
    y = _check_year(year)
    if not items:
        return 0
    ts = _now()
    rows: list[tuple] = []
    for it in items:
        region = clean_field(it.get("region"), MAX_REGION)
        if not region:
            raise ValueError("区域不能为空")
        task_id = int(it.get("task_type_id") or 0)
        if task_id and get_task_type(task_id) is None:
            raise ValueError(f"任务不存在：{task_id}")
        cat_id = int(it.get("category_id") or 0)
        pid = it.get("product_type_id")
        if pid in (None, "") and not cat_id:
            raise ValueError(f"区域「{region}」缺少产品类型或品类")
        # 两种粒度：品类级（pid=0 + category_id）/ 产品级（pid>0）
        product_type_id = int(pid) if pid not in (None, "") else 0
        raw = it.get("quota", 0)
        try:
            quota = int(float(str(raw).strip() or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"区域「{region}」的任务量不是数字：{raw!r}") from exc
        if quota < 0:
            raise ValueError(f"区域「{region}」的任务量不能为负数：{quota}")
        if quota > MAX_QUOTA:
            raise ValueError(f"区域「{region}」的任务量超出上限（{MAX_QUOTA}）：{quota}")
        rows.append((y, task_id, region, product_type_id, quota, ts,
                     0 if product_type_id else cat_id, contract_key(it.get("contract_no"))))
    conn = _connect()
    try:
        with conn:
            conn.executemany(
                "INSERT INTO task_quota (year, task_type_id, region, product_type_id, quota, updated_at,"
                " category_id, contract_no) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(year, task_type_id, region, product_type_id, category_id) DO UPDATE SET "
                "  quota = excluded.quota, updated_at = excluded.updated_at",
                rows,
            )
    finally:
        conn.close()
    return len(rows)


def clear_quotas(year: int, task_type_id: int) -> int:
    """清空某任务某年的全部任务量。"""
    y = _check_year(year)
    conn = _connect()
    try:
        with conn:
            cur = conn.execute("DELETE FROM task_quota WHERE year = ? AND task_type_id = ?",
                               (y, int(task_type_id)))
            return int(cur.rowcount or 0)
    finally:
        conn.close()


def remap_quotas_by_contract() -> int:
    """老库迁移补丁：重建 `task_quota` 主键时合同多半还没分配任务，旧行会落在 `task_type_id = 0`。

    等合同归好任务后，按行上保留的 `contract_no` 把这些行补挂到对应任务（幂等）。
    ⚠️ 目标键可能已存在（同任务同格后来又录过），此时**累加**而不是改挂，否则撞唯一键：
    任务量是"下达量"，两个来源各下达一次、合并后应相加。
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT rowid AS rid, year, region, product_type_id, category_id, quota, contract_no "
            "FROM task_quota WHERE task_type_id = 0 AND IFNULL(contract_no, '') <> ''").fetchall()
        if not rows:
            return 0
        moved = 0
        with conn:
            for r in rows:
                tid_row = conn.execute("SELECT task_type_id FROM contract WHERE contract_no = ?",
                                       (str(r["contract_no"]),)).fetchone()
                if tid_row is None or not int(tid_row["task_type_id"] or 0):
                    continue          # 合同没归任务：留在 0（= 未纳入），页面可见
                tid = int(tid_row["task_type_id"])
                dst = conn.execute(
                    "SELECT rowid AS rid, quota FROM task_quota WHERE year = ? AND task_type_id = ? "
                    "AND region = ? AND product_type_id = ? AND category_id = ? AND rowid <> ?",
                    (int(r["year"]), tid, str(r["region"]), int(r["product_type_id"]),
                     int(r["category_id"]), int(r["rid"]))).fetchone()
                if dst is not None:
                    conn.execute("UPDATE task_quota SET quota = ? WHERE rowid = ?",
                                 (int(dst["quota"] or 0) + int(r["quota"] or 0), int(dst["rid"])))
                    conn.execute("DELETE FROM task_quota WHERE rowid = ?", (int(r["rid"]),))
                else:
                    conn.execute("UPDATE task_quota SET task_type_id = ? WHERE rowid = ?",
                                 (tid, int(r["rid"])))
                moved += 1
        if moved:
            _LOG.warning("progress 库升级：%s 条旧任务量按合同补挂到任务", moved)
        return moved
    finally:
        conn.close()


# ==================== 完成量明细（逐样品投影） ====================


def replace_done_samples(year: int, rows: list[dict], synced_at: str) -> int:
    """整年替换完成量明细（DELETE + 批量 INSERT 同事务）；失败自动回滚，旧明细仍在。"""
    y = _check_year(year)
    ts = synced_at or _now()
    payload = [
        (
            y, int(r["sample_id"]), clean_field(r.get("detection_no"), 64),
            contract_key(r.get("contract_no")), clean_field(r.get("biz"), 64),
            clean_field(r.get("task_name"), 200), clean_field(r.get("region_key"), MAX_REGION),
            clean_field(r.get("region"), MAX_REGION), clean_field(r.get("sample_name"), MAX_NAME),
            clean_field(r.get("sample_category"), 64), clean_field(r.get("sample_date"), 16),
            int(r.get("task_type_id") or 0),
            int(r.get("product_type_id") or 0), clean_field(r.get("match_layer"), 32), ts,
            clean_field(r.get("county"), MAX_REGION), clean_field(r.get("addr"), 300),
            clean_field(r.get("addr2"), 300),
        )
        for r in rows
    ]
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM done_sample WHERE year = ?", (y,))
            conn.executemany(
                "INSERT INTO done_sample (year, sample_id, detection_no, contract_no, biz, task_name, "
                "region_key, region, sample_name, sample_category, sample_date, task_type_id, product_type_id,"
                " match_layer, synced_at, county, addr, addr2) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
    finally:
        conn.close()
    return len(payload)


def set_done_task_types(year: int, mapping: dict[str, int]) -> int:
    """按「合同编号 → 任务 id」回写明细的任务归属（改合同归属后本地重算，不用重连镜像）。

    合同编号不在 `mapping` 里的样品一律置 0（= 未纳入任何考核表）。
    """
    y = _check_year(year)
    conn = _connect()
    try:
        with conn:
            updated = 0
            if mapping:
                for cno, tid in mapping.items():
                    cur = conn.execute("UPDATE done_sample SET task_type_id = ? WHERE year = ? AND contract_no = ?",
                                       (int(tid), y, str(cno)))
                    updated += int(cur.rowcount or 0)
            cur = conn.execute(
                "UPDATE done_sample SET task_type_id = 0 WHERE year = ? AND contract_no NOT IN ("
                "  SELECT contract_no FROM contract WHERE task_type_id > 0) AND IFNULL(task_type_id, 0) <> 0",
                (y,))
            updated += int(cur.rowcount or 0)
            return updated
    finally:
        conn.close()


def done_years() -> list[int]:
    """库内已有明细的年份（用于 bootstrap 时按年份建方案、补任务归属）。"""
    conn = _connect()
    try:
        return [int(r["year"]) for r in conn.execute("SELECT DISTINCT year FROM done_sample ORDER BY year")]
    finally:
        conn.close()


def quota_years() -> list[int]:
    """库内已有任务量的年份。"""
    conn = _connect()
    try:
        return [int(r["year"]) for r in conn.execute("SELECT DISTINCT year FROM task_quota ORDER BY year")]
    finally:
        conn.close()


def read_done_samples(year: int) -> list[dict]:
    """当年全部样品明细（几千行，服务层在内存里做任意口径聚合）。"""
    conn = _connect()
    try:
        return _rows(conn.execute(
            "SELECT * FROM done_sample WHERE year = ? ORDER BY sample_date, sample_id", (_check_year(year),)))
    finally:
        conn.close()


def update_done_classification(year: int, sample_ids: list[int],
                               product_type_id: int | None = None,
                               region: str | None = None,
                               region_key: str | None = None,
                               match_layer: str | None = None,
                               task_type_id: int | None = None) -> int:
    """按最新配置回写归类结果（**不重连镜像**）；返回更新行数。"""
    y = _check_year(year)
    if not sample_ids:
        return 0
    sets, params = [], []
    if task_type_id is not None:
        sets.append("task_type_id = ?")
        params.append(int(task_type_id))
    if product_type_id is not None:
        sets.append("product_type_id = ?")
        params.append(int(product_type_id))
    if region is not None:
        sets.append("region = ?")
        params.append(clean_field(region, MAX_REGION))
    if region_key is not None:
        sets.append("region_key = ?")
        params.append(clean_field(region_key, MAX_REGION))
    if match_layer is not None:
        sets.append("match_layer = ?")
        params.append(clean_field(match_layer, 32))
    if not sets:
        return 0
    marks = ",".join("?" * len(sample_ids))
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                f"UPDATE done_sample SET {', '.join(sets)} WHERE year = ? AND sample_id IN ({marks})",
                (*params, y, *[int(s) for s in sample_ids]),
            )
            return int(cur.rowcount or 0)
    finally:
        conn.close()


def done_counts_by_name(year: int, task_type_id: int | None = None) -> list[dict]:
    """按样品名汇总当年样品数（"未归类清单"与"可挂载候选"都用它）；可按任务过滤。"""
    sql = ("SELECT sample_name, sample_category, COUNT(*) AS cnt, "
           "  SUM(CASE WHEN product_type_id = 0 THEN 1 ELSE 0 END) AS unmatched "
           "FROM done_sample WHERE year = ?")
    params: list = [_check_year(year)]
    if task_type_id is not None:
        sql += " AND task_type_id = ?"
        params.append(int(task_type_id))
    sql += " GROUP BY sample_name, sample_category ORDER BY cnt DESC"
    conn = _connect()
    try:
        return _rows(conn.execute(sql, tuple(params)))
    finally:
        conn.close()


def region_keys(year: int) -> list[dict]:
    """当年出现过的区域键及其样品数（用于"未映射区域"提示）。"""
    conn = _connect()
    try:
        return _rows(conn.execute(
            "SELECT region_key, COUNT(*) AS cnt, SUM(CASE WHEN region = '' THEN 1 ELSE 0 END) AS unmapped "
            "FROM done_sample WHERE year = ? GROUP BY region_key ORDER BY cnt DESC", (_check_year(year),)))
    finally:
        conn.close()


# ==================== 忽略名单 ====================


def list_ignore_names() -> list[dict]:
    conn = _connect()
    try:
        return _rows(conn.execute("SELECT * FROM ignore_name ORDER BY name"))
    finally:
        conn.close()


def add_ignore_name(name: str, note: str = "") -> None:
    nm = clean_field(name, MAX_NAME)
    if not nm:
        raise ValueError("样品名称不能为空")
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO ignore_name (name, note, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET note = excluded.note, updated_at = excluded.updated_at",
                (nm, clean_field(note, MAX_NOTE), _now()),
            )
    finally:
        conn.close()


def remove_ignore_name(name: str) -> None:
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM ignore_name WHERE name = ?", (clean_field(name, MAX_NAME),))
    finally:
        conn.close()


# ==================== 同步日志 ====================


_LOG_FIELDS = (
    "year", "started_at", "finished_at", "source", "data_deadline", "rows", "done_total",
    "contract_count", "unmatched", "unclassified", "no_contract", "ignored", "elapsed_ms",
    "status", "error",
)
_LOG_TEXT_FIELDS = {"started_at", "finished_at", "source", "data_deadline", "status", "error"}


def write_sync_log(entry: dict) -> int:
    """写一条同步日志（只增不删）；返回日志 id。"""
    values = tuple(entry.get(f, "" if f in _LOG_TEXT_FIELDS else 0) for f in _LOG_FIELDS)
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO sync_log (" + ", ".join(_LOG_FIELDS) + ") VALUES (" + ", ".join("?" * len(_LOG_FIELDS)) + ")",
                values,
            )
            return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_sync_logs(year: int | None = None, limit: int = 20) -> list[dict]:
    sql = "SELECT * FROM sync_log"
    params: list = []
    if year is not None:
        sql += " WHERE year = ?"
        params.append(_check_year(year))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    conn = _connect()
    try:
        return _rows(conn.execute(sql, tuple(params)))
    finally:
        conn.close()


def last_sync(year: int | None = None, only_ok: bool = False) -> dict | None:
    sql = "SELECT * FROM sync_log"
    where, params = [], []
    if year is not None:
        where.append("year = ?")
        params.append(_check_year(year))
    if only_ok:
        where.append("status = 'ok'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT 1"
    conn = _connect()
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ==================== 元信息 / 维护 ====================


def set_meta(k: str, v: str) -> None:
    """写元信息。值上限刻意放宽到 20000 字符：这里会缓存"未归类样品名 Top"这类 JSON。"""
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO app_meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (str(k)[:64], str(v)[:20000]),
            )
    finally:
        conn.close()


def get_meta(k: str, default: str = "") -> str:
    conn = _connect()
    try:
        row = conn.execute("SELECT v FROM app_meta WHERE k = ?", (str(k)[:64],)).fetchone()
        return str(row["v"]) if row else default
    finally:
        conn.close()


def stats() -> dict:
    """库内行数概览（诊断用，不暴露业务明细）。"""
    tables = ("task_type", "scheme", "big_kind", "category", "product_type", "region", "contract",
              "task_quota", "done_sample", "ignore_name", "sync_log")
    conn = _connect()
    try:
        return {t: int(conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]) for t in tables}
    finally:
        conn.close()
