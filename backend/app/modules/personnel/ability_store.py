"""人员能力表比对：上传的《检验检测能力表》持久化与匹配。

**为什么要有它**：卡1「人员能力表」只回答"某人登记过哪些项目和标准"，
回答不了"这些项目/标准是否落在机构已取得的能力范围内"。后者要拿
《检验检测能力表》（CMA 能力附表）逐条比对——人工翻 1200+ 条能力项极易漏判，
所以把能力表存下来，在查询结果上逐行判定。

**设计约定**：

- 存储为 SQLite（标准库 sqlite3，无额外依赖），默认 `<backend>/data/personnel/ability.sqlite`，
  目录可用 `PERSONNEL_ABILITY_DIR` 覆盖（与 `cma_ledger_dir` 同一写法）；
- 上传 = **整表替换**：`DELETE` + 批量 `INSERT` 在**同一个事务**里完成，异常自动回滚，
  保证"上一次已生效的能力表"不会被半截数据破坏（这是页面上"直接替换、失败保留旧表"的后端依据）；
- 匹配键与卡1 的清洗**同源**：复用 `service.clean_project` / `clean_method` /
  `split_method_year`，避免"入库一套规则、比对另一套规则"的隐性偏差；
- 匹配分两级：先**精确**（项目+方法全等），未命中再**宽松**（年号不同 / 项目名别名 /
  两者皆有），结论带上差异原因，便于人工复核而不是简单的是/否；
- 索引常驻内存（千条规模），以"文件 mtime + 大小"为指纹失效，查询路径不产生额外 I/O。

结论字面量（`IN` / `NOT_IN`）与原因字面量（`REASON_*`）会原样出现在前端展示与导出 Excel 中，
**不要随意改动**。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from app.core.config import BACKEND_DIR, settings
from app.modules.personnel import service

_LOG = logging.getLogger("app.personnel.ability")

# ==================== 结论 / 原因字面量 ====================

IN = "在"
NOT_IN = "不在"
REASON_YEAR = "年号不同"
REASON_ALIAS = "项目名别名"
REASON_BOTH = "别名与年号均不同"

# ==================== 保护性上限 ====================

MAX_ITEMS = 50000
MAX_FIELD = 200
MAX_FILE_NAME = 200

# ==================== 文本归一化 ====================

# 全角 / 特殊符号 → 半角（含各类连接符与括号，实测能力表与数据库两边的写法不一致）
_FULLWIDTH_MAP = str.maketrans(
    {
        "（": "(",
        "）": ")",
        "〔": "(",
        "〕": ")",
        "［": "[",
        "］": "]",
        "【": "[",
        "】": "]",
        "，": ",",
        "、": ",",
        "；": ";",
        "：": ":",
        "－": "-",
        "—": "-",
        "–": "-",
        "―": "-",
        "−": "-",
        "ー": "-",
        "／": "/",
        "＼": "\\",
        "％": "%",
        "＋": "+",
        "．": ".",
        "。": ".",
        "　": " ",
    }
)

# 前导噪声符号（与 service.clean_project 的处理保持一致，再兜一层）
_LEAD_NOISE_RE = re.compile(r"^[﹡△*☆★\s]+")
# 空白：连内部空格一起去掉，使 "GB 5009.12-2023" 与 "GB5009.12-2023" 等价
_WS_RE = re.compile(r"\s+")
# 括号别名：`克百威（呋喃丹，包括3-羟基克百威）` → `克百威`（归一化后只可能是半角括号）
_ALIAS_RE = re.compile(r"[(\[][^(\[)\]]*[)\]]")


def normalize_text(value: object) -> str:
    """归一化：全角转半角、剥前导噪声符号、去全部空白、统一小写。"""
    s = str(value if value is not None else "").strip()
    s = s.translate(_FULLWIDTH_MAP)
    s = _LEAD_NOISE_RE.sub("", s)
    s = _WS_RE.sub("", s)
    return s.lower()


def project_key(value: object) -> str:
    """项目键：复用卡1 的 clean_project（去 `﹡△` 前缀、循环剥末尾括号简写）后再归一化。"""
    return normalize_text(service.clean_project(value))


def project_loose_key(value: object) -> str:
    """项目宽松键：在 project_key 基础上再去掉**全部**括号及内容。

    兜住 `克百威（呋喃丹，包括3-羟基克百威）` 这类——`clean_project` 因含"包括"会**保留**括号，
    而人员侧记录的项目名通常是括号前的 `克百威`。
    """
    return _ALIAS_RE.sub("", project_key(value))


def method_key(value: object) -> str:
    """方法键：复用卡1 的 clean_method（取标准号段）后再归一化（含年号）。"""
    return normalize_text(service.clean_method(value))


def method_base_key(value: object) -> str:
    """方法宽松键：在 method_key 基础上去掉年号（`GB 23200.121-2026` → `gb23200.121`）。"""
    cleaned = service.clean_method(value)
    base, _year = service.split_method_year(cleaned)
    return normalize_text(base)


# ==================== 存储路径 ====================


def ability_dir() -> Path:
    """能力表存储目录：配置优先，留空则 `<backend>/data/personnel`。"""
    configured = (settings.personnel_ability_dir or "").strip()
    return Path(configured) if configured else BACKEND_DIR / "data" / "personnel"


def db_file() -> Path:
    return ability_dir() / "ability.sqlite"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ability_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    file_name   TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL DEFAULT '',
    item_count  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ability_item (
    project TEXT NOT NULL,
    method  TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    path = db_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.executescript(_SCHEMA)
    return conn


def _write(conn: sqlite3.Connection, meta: tuple[str, str, int], rows: list[tuple[str, str]]) -> None:
    """整表替换（事务内，由调用方 `with conn:` 控制提交/回滚）。

    单独抽成函数是为了让测试可以直接注入"写入中途失败"，验证回滚后旧表仍在。
    """
    conn.execute("DELETE FROM ability_item")
    conn.executemany("INSERT INTO ability_item (project, method) VALUES (?, ?)", rows)
    conn.execute(
        "INSERT INTO ability_meta (id, file_name, uploaded_at, item_count) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "file_name = excluded.file_name, uploaded_at = excluded.uploaded_at, item_count = excluded.item_count",
        meta,
    )


# ==================== 内存索引（带指纹缓存）====================


class _Index:
    """能力表索引：四个哈希集合 + 统计值。"""

    __slots__ = ("exact", "year", "alias", "both", "projects", "pairs", "item_count")

    def __init__(self) -> None:
        # 精确：(project_key, method_key)
        self.exact: set[tuple[str, str]] = set()
        # 宽松1：同项目名、年号不同
        self.year: set[tuple[str, str]] = set()
        # 宽松2：同方法（含年号）、项目名去别名后相同
        self.alias: set[tuple[str, str]] = set()
        # 宽松3：项目名去别名 + 年号忽略
        self.both: set[tuple[str, str]] = set()
        self.projects: set[str] = set()
        self.pairs: set[tuple[str, str]] = set()
        self.item_count = 0

    @property
    def unique_pairs(self) -> int:
        return len(self.pairs)

    @property
    def unique_projects(self) -> int:
        return len(self.projects)


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, object] = {"fingerprint": None, "index": None}


def invalidate() -> None:
    """使索引缓存失效（上传 / 清除后调用）。"""
    with _CACHE_LOCK:
        _CACHE["fingerprint"] = None
        _CACHE["index"] = None


def _fingerprint() -> tuple[str, int, int] | None:
    path = db_file()
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def _build_index() -> _Index | None:
    """读库建索引；无库/无数据时返回 None（前端据此不显示比对列）。"""
    fp = _fingerprint()
    if fp is None:
        return None
    if _CACHE["fingerprint"] == fp and _CACHE["index"] is not None:
        return _CACHE["index"]  # type: ignore[return-value]

    idx = _Index()
    rows: list[tuple[str, str]] = []
    try:
        conn = _connect()
        try:
            rows = [(str(r[0]), str(r[1])) for r in conn.execute("SELECT project, method FROM ability_item")]
        finally:
            conn.close()
    except sqlite3.Error as exc:  # 读失败按"未加载"处理，不让比对拖垮查询
        _LOG.warning("读取能力表失败：%s", exc)
        return None

    if not rows:
        with _CACHE_LOCK:
            _CACHE["fingerprint"] = fp
            _CACHE["index"] = idx
        return idx

    for project, method in rows:
        pk = project_key(project)
        mk = method_key(method)
        if not pk or not mk:
            continue
        pl = project_loose_key(project)
        mb = method_base_key(method)
        idx.item_count += 1
        idx.projects.add(pk)
        idx.pairs.add((pk, mk))
        idx.exact.add((pk, mk))
        if mb:
            idx.year.add((pk, mb))
        if pl:
            idx.alias.add((pl, mk))
            idx.both.add((pl, mb))

    with _CACHE_LOCK:
        _CACHE["fingerprint"] = fp
        _CACHE["index"] = idx
    return idx


def _index() -> _Index | None:
    return _build_index()


def is_loaded() -> bool:
    idx = _index()
    return bool(idx is not None and idx.item_count > 0)


# ==================== 匹配 ====================


def match(project: object, method: object) -> tuple[str, str]:
    """判定「项目 + 方法」是否在能力表内，返回 `(结论, 原因)`。

    - 未上传能力表 → `("", "")`（前端据此不渲染比对列）
    - 精确命中 → `("在", "")`
    - 宽松命中 → `("在", "年号不同" | "项目名别名" | "别名与年号均不同")`
    - 未命中 → `("不在", "")`

    口径（与用户确认一致）：**项目与方法必须同时命中**，任一项对不上即「不在」。
    """
    idx = _index()
    if idx is None or idx.item_count == 0:
        return ("", "")
    pk = project_key(project)
    mk = method_key(method)
    if not pk or not mk:
        return (NOT_IN, "")
    if (pk, mk) in idx.exact:
        return (IN, "")
    mb = method_base_key(method)
    if mb and (pk, mb) in idx.year:
        return (IN, REASON_YEAR)
    pl = project_loose_key(project)
    if pl and (pl, mk) in idx.alias:
        return (IN, REASON_ALIAS)
    if pl and mb and (pl, mb) in idx.both:
        return (IN, REASON_BOTH)
    return (NOT_IN, "")


# ==================== 对外：保存 / 状态 / 清除 ====================


def _clean_items(items: list[dict] | list[tuple[str, str]]) -> list[tuple[str, str]]:
    """清洗上传条目：截断、去空。返回按原顺序的 (project, method) 列表（**不去重**）。"""
    if not items:
        raise ValueError("未收到任何能力表条目，请确认上传的是《检验检测能力表》")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"能力表条目过多（{len(items)} 条，上限 {MAX_ITEMS} 条），请确认文件是否正确")

    rows: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            project = item.get("project")
            method = item.get("method")
        else:
            project, method = item
        p = str(project if project is not None else "").strip()[:MAX_FIELD]
        m = str(method if method is not None else "").strip()[:MAX_FIELD]
        if not p or not m:
            continue
        rows.append((p, m))
    if not rows:
        raise ValueError("能力表中未解析到有效的「项目 + 标准」条目，请确认表头与列内容")
    return rows


def save(file_name: str, items: list[dict] | list[tuple[str, str]]) -> dict:
    """整表替换保存能力表，返回最新状态。失败抛 ValueError / sqlite3.Error（旧表保持不变）。"""
    rows = _clean_items(items)
    name = str(file_name or "").strip()[:MAX_FILE_NAME] or "(未命名).xlsx"
    meta = (name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(rows))

    conn = _connect()
    try:
        with conn:  # 成功提交 / 异常回滚：替换失败不会破坏已生效的旧表
            _write(conn, meta, rows)
    finally:
        conn.close()

    invalidate()
    _LOG.info("能力表已更新：%s（%d 条）", name, len(rows))
    return status()


def status() -> dict:
    """能力表状态：未上传时 loaded=False，其余字段为空/0。"""
    idx = _index()
    if idx is None or idx.item_count == 0:
        return {
            "loaded": False,
            "fileName": "",
            "uploadedAt": "",
            "count": 0,
            "uniquePairs": 0,
            "uniqueProjects": 0,
        }

    file_name = ""
    uploaded_at = ""
    try:
        conn = _connect()
        try:
            row = conn.execute("SELECT file_name, uploaded_at FROM ability_meta WHERE id = 1").fetchone()
            if row:
                file_name, uploaded_at = str(row[0]), str(row[1])
        finally:
            conn.close()
    except sqlite3.Error as exc:  # 元信息读失败不影响比对能力
        _LOG.warning("读取能力表元信息失败：%s", exc)

    return {
        "loaded": True,
        "fileName": file_name,
        "uploadedAt": uploaded_at,
        "count": idx.item_count,
        "uniquePairs": idx.unique_pairs,
        "uniqueProjects": idx.unique_projects,
    }


def clear() -> None:
    """清除已生效的能力表（页面上点「清除」）。"""
    path = db_file()
    if path.exists():
        conn = _connect()
        try:
            with conn:
                conn.execute("DELETE FROM ability_item")
                conn.execute("DELETE FROM ability_meta")
        finally:
            conn.close()
    invalidate()
