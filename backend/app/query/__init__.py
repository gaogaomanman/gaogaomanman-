"""统一查询层（W-7）：让**同一段业务 SQL** 能跑在「SQLite 镜像」或「源库直连」上。

## 为什么是"翻译"而不是"改写成 SQLAlchemy Core"

清点后发现：4 个模块里 **只有 2 个的 SQL 在后端**（看板 `board/service.py`、人员能力
`personnel/service.py`），另 2 个页面的 SQL **在前端 TS/Vue 里**（达梦页 `dm/blocks/*.ts`、
SQL Server 页 `SqlServerView.vue`），它们经 `POST /api/{dm,sqlserver}/query` 把 SQL 原文
交给后端通用执行器执行（实测 `dm/routes.py` 的 `SqlBody{sql: str}`）。

要把这些 SQL 改写成 Core，就得先把前端 11 个块 + 6 类样品的 SQL 全搬回后端并重做前端数据层，
改动面远大于收益。因此这里改为**在执行器入口做方言翻译**：

- **SQL 原文逐字保留** → 口径不可能因"重写"而漂移（这就是我们要保的东西）；
- 改动面极小（只在执行器一处），前端零改动；
- 翻译不了的构造**明确拒绝并回退直连**（`NotTranslatable`），绝不猜测——
  猜错会静默算出错数，回退只会慢一点。

> 与"Core 化"的关系：Core 方案对后端那 2 个模块仍可用，但为了 4 个模块口径一致、便于对拍，
> 统一走本层。若后续要把看板/人员能力 Core 化，本层可并存（`execute()` 支持传入 Core 语句）。

## 模式（DATA_MODE）

- `mirror`（默认）：翻译后跑在 `current.sqlite`，毫秒级、不占源库连接、部署机不需要达梦驱动；
- `direct`：原样跑源库（原代码路径，零回归风险），用于核对最新数据与调试。

**安全降级**：`mirror` 模式下若无快照（从未同步）或该 SQL 不可翻译，**自动回退 direct**，
并在返回值里带出 `mode`/`fallback` 说明，由调用方透出——绝不静默降级。
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import text

from app.core.config import settings
from app.core.errors import DbError
from app.mirror import store


class MirrorUnavailable(DbError):
    """镜像模式下无法执行该查询（附原因）。

    **刻意继承 DbError**：路由层统一按 `DbError` 处理，前端会收到明确的失败信息。
    镜像模式下**不回退直连**——这是架构约定：应用层只碰镜像，源库只允许同步进程连接。
    """


def is_mirror_mode() -> bool:
    """当前是否为镜像模式（应用层只读镜像、不连源库）。"""
    return (settings.data_mode or "mirror").strip().lower() == "mirror"


# ======================================================================
# 一、模式解析
# ======================================================================


def resolve_mode() -> tuple[str, str | None]:
    """返回 (mode, fallback_reason)。

    mirror 模式下快照不存在同样返回 mirror（由调用方收到明确错误），
    **不再静默降级直连**——降级会让"工具其实连了源库"这件事变得不可见。
    """
    if not is_mirror_mode():
        return "direct", None
    if not store.CURRENT_DB.exists():
        return "mirror", "镜像快照不存在（尚未同步）"
    return "mirror", None


def mirror_status() -> dict:
    """镜像状态（供导航页/看板展示"数据截止时间"与模式）。"""
    mode, reason = resolve_mode()
    return {
        "mode": mode,
        "fallback": reason,
        "data_deadline": store.data_deadline() if mode == "mirror" else None,
    }


# ======================================================================
# 二、方言翻译（源库 SQL → SQLite）
# ======================================================================


class NotTranslatable(Exception):
    """遇到无法确定语义等价的构造——交由调用方回退直连，不做猜测。"""


# 明确不支持、且**绝不猜**的构造（出现即拒绝）
_UNSUPPORTED = {
    "CONNECT BY": re.compile(r"\bCONNECT\s+BY\b", re.I),
    "DECODE()": re.compile(r"\bDECODE\s*\(", re.I),
    "LISTAGG/WM_CONCAT": re.compile(r"\b(LISTAGG|WM_CONCAT)\s*\(", re.I),
    "ROWNUM": re.compile(r"\bROWNUM\b", re.I),
    "SELECT TOP n": re.compile(r"\bSELECT\s+TOP\s+\d+", re.I),
    "MERGE/OVER": re.compile(r"\b(MERGE\s+INTO|OVER\s*\()", re.I),
}

_EXTRACT_UNIT = {"YEAR": "%Y", "MONTH": "%m", "DAY": "%d"}

# DM TO_CHAR 格式串 → SQLite strftime 格式串（只映射能确定等价的）
_TO_CHAR_FMT = {
    "YYYY-MM-DD": "%Y-%m-%d",
    "YYYY-MM": "%Y-%m",
    "YYYY/MM/DD": "%Y/%m/%d",
    "YYYYMMDD": "%Y%m%d",
    "YYYY": "%Y",
    "MM": "%m",
    "DD": "%d",
}


def _strip_schema(sql: str) -> str:
    """SQLite 无 schema 概念：去掉 `DETECTION.` / `dbo.` 前缀。

    只处理本项目实际使用的两个 schema 名，避免误伤列名。
    """
    sql = re.sub(r"\bDETECTION\s*\.", "", sql, flags=re.I)
    sql = re.sub(r"\bdbo\s*\.", "", sql, flags=re.I)
    return sql


def translate(sql: str, source: str) -> tuple[str, list[str]]:
    """把源库 SQL 翻译为 SQLite 可执行形式；返回 (sql, 命中规则名列表)。

    遇到不可确定等价的构造抛 `NotTranslatable`（调用方回退直连）。
    """
    for name, pat in _UNSUPPORTED.items():
        if pat.search(sql):
            raise NotTranslatable(f"含不支持的构造：{name}")

    rules: list[str] = []
    out = sql

    # ---- EXTRACT(UNIT FROM x) ----
    def _extract(m: re.Match) -> str:
        unit = m.group(1).upper()
        if unit not in _EXTRACT_UNIT:
            raise NotTranslatable(f"EXTRACT({unit} FROM …) 无等价实现")
        return f"CAST(STRFTIME('{_EXTRACT_UNIT[unit]}', {m.group(2).strip()}) AS INTEGER)"

    # ---- 年份等值 → 日期区间（**必须先于下面的 EXTRACT→STRFTIME**）----
    # `EXTRACT(YEAR FROM col) = N` 与 `col >= 'N-01-01' AND col < 'N+1-01-01'` 语义等价，
    # 但**后者能走 col 上的索引**；前者把列包在函数里，百万行只能逐行算（索引彻底失效）。
    # 实测（看板-检出率，同一条 SQL）：逐行算 3.2 秒 → 区间 0.2 秒。
    # 只匹配**简单列引用**；表达式含括号时不匹配，自动落到下面的通用规则，行为不变。
    # ⚠️ 与 `app/mirror/schema.py` 的 INDEX_SPECS 是**成对**的：只建索引不做这个改写，
    #    索引仍用不上（这一点是实测踩出来的）。
    def _year_eq(m: re.Match) -> str:
        expr, year = m.group(1).strip(), int(m.group(2))
        return f"({expr} >= '{year}-01-01' AND {expr} < '{year + 1}-01-01')"

    for pat, label in (
        (r"\bEXTRACT\s*\(\s*YEAR\s+FROM\s+([A-Za-z_][\w.]*)\s*\)\s*=\s*(\d{4})", "EXTRACT"),
        (r"\bYEAR\s*\(\s*([A-Za-z_][\w.]*)\s*\)\s*=\s*(\d{4})", "YEAR()"),
    ):
        out, n = re.subn(pat, _year_eq, out, flags=re.I)
        if n:
            rules.append(f"{label}年份等值→日期区间×{n}")

    out, n = re.subn(r"\bEXTRACT\s*\(\s*(\w+)\s+FROM\s+([^()]+?)\s*\)", _extract, out, flags=re.I)
    if n:
        rules.append(f"EXTRACT→STRFTIME×{n}")

    # ---- TO_CHAR(x, 'fmt') ----
    def _to_char(m: re.Match) -> str:
        fmt = m.group(2).strip().strip("'\"")
        mapped = _TO_CHAR_FMT.get(fmt.upper())
        if mapped is None:
            raise NotTranslatable(f"TO_CHAR 格式 '{fmt}' 无等价映射")
        return f"STRFTIME('{mapped}', {m.group(1).strip()})"

    out, n = re.subn(
        r"\bTO_CHAR\s*\(\s*([^,()]+?)\s*,\s*('([^']*)'|\"([^\"]*)\")\s*\)", _to_char, out, flags=re.I
    )
    if n:
        rules.append(f"TO_CHAR→STRFTIME×{n}")

    # ---- TO_DATE('d','fmt') [+ n]  （DM） ----
    def _to_date_plus(m: re.Match) -> str:
        date_lit, sign, days = m.group(1), m.group(2), m.group(3)
        return f"DATE('{date_lit}', '{sign}{days} day')"

    out, n = re.subn(
        r"\bTO_DATE\s*\(\s*'([^']+)'\s*,\s*'[^']*'\s*\)\s*([+-])\s*(\d+)",
        _to_date_plus,
        out,
        flags=re.I,
    )
    if n:
        rules.append(f"TO_DATE()+n→DATE(…)×{n}")

    out, n = re.subn(r"\bTO_DATE\s*\(\s*'([^']+)'\s*,\s*'[^']*'\s*\)", r"'\1'", out, flags=re.I)
    if n:
        rules.append(f"TO_DATE→字面量×{n}")

    # ---- SQL Server 的 YEAR()/MONTH()/DAY() ----
    # 这三个是**全量扫查才发现的漏网构造**：初见只扫了 EXTRACT/TO_CHAR/DATEADD 等，
    # 漏掉了 SQL Server 的「函数式」日期提取，导致 `no such function: YEAR`。
    # 注意顺序：EXTRACT 规则已在前面把 `EXTRACT(YEAR FROM x)` 消耗掉，
    # 且 `YEAR FROM` 后没有 `(`，不会被下面的正则误伤。
    for fn, fmt in (("YEAR", "%Y"), ("MONTH", "%m"), ("DAY", "%d")):
        out, n = re.subn(
            rf"\b{fn}\s*\(\s*([^()]+?)\s*\)",
            lambda m, _f=fmt: f"CAST(STRFTIME('{_f}', {m.group(1).strip()}) AS INTEGER)",
            out,
            flags=re.I,
        )
        if n:
            rules.append(f"{fn}()→STRFTIME×{n}")

    # ---- NVL / ISNULL → IFNULL ----
    out, n = re.subn(
        r"\b(?:NVL|ISNULL)\s*\(\s*([^,()]+?)\s*,\s*([^()]+?)\s*\)", r"IFNULL(\1, \2)", out, flags=re.I
    )
    if n:
        rules.append(f"NVL/ISNULL→IFNULL×{n}")

    # ---- SQL Server 专有 ----
    def _dateadd(m: re.Match) -> str:
        unit = m.group(1).lower()
        if unit not in ("day", "dd", "d"):
            raise NotTranslatable(f"DATEADD({unit}, …) 无等价实现")
        n2 = m.group(2).strip()
        sign = "-" if n2.startswith("-") else "+"
        return f"DATE({m.group(3).strip()}, '{sign}{n2.lstrip('+-')} day')"

    out, n = re.subn(
        r"\bDATEADD\s*\(\s*(\w+)\s*,\s*([^,()]+?)\s*,\s*([^()]+?)\s*\)", _dateadd, out, flags=re.I
    )
    if n:
        rules.append(f"DATEADD→DATE(…)×{n}")

    # 第二参数允许是函数调用（实测 `CONVERT(varchar(10), MAX(Date_Test), 120)`），
    # 故不能用 `[^,()]+?`——那样会匹配不到、静默漏译，最终在 SQLite 上以
    # `no such function: varchar` 报错（对拍时正是如此暴露的）。
    out, n = re.subn(
        r"\bCONVERT\s*\(\s*(?:var|nvar)char\s*\(\s*\d+\s*\)\s*,\s*(.+?)\s*,\s*120\s*\)",
        lambda m: f"STRFTIME('%Y-%m-%d', {m.group(1).strip()})",
        out,
        flags=re.I,
    )
    if n:
        rules.append(f"CONVERT(varchar,·,120)→STRFTIME×{n}")

    out, n = re.subn(r"\bGETDATE\s*\(\s*\)", "DATE('now','localtime')", out, flags=re.I)
    if n:
        rules.append(f"GETDATE→DATE(now)×{n}")

    out, n = re.subn(r"\bLEN\s*\(", "LENGTH(", out, flags=re.I)
    if n:
        rules.append(f"LEN→LENGTH×{n}")

    # ---- SQLite 保留字作列名 ----
    # 实测：`tr.Limit`（SQL Server 允许、SQLite 报 `near "Limit": syntax error`）。
    # 处理 `别名.保留字` 形式 → `别名."保留字"`；裸保留字列（无别名）无法安全识别，未处理。
    _KW = (
        "LIMIT|ORDER|GROUP|INDEX|TABLE|COLUMN|CHECK|UNIQUE|PRIMARY|DEFAULT|VALUES|KEY|"
        "DESC|ASC|CASE|END|WHEN|THEN|ELSE|ACTION|CURRENT|MATCH|NATURAL|PARTITION|PRECEDING|"
        "RANGE|ROWS|GROUPS|EXCLUDE|NULLS|FIRST|LAST|OTHERS|TIES"
    )
    out, n = re.subn(
        rf"\.(({_KW})\b)(?!\s*\()",
        lambda m: '."' + m.group(1) + '"',
        out,
        flags=re.I,
    )
    if n:
        rules.append(f"保留字列名加引号×{n}")

    # ---- 去 schema 前缀 ----
    stripped = _strip_schema(out)
    if stripped != out:
        rules.append("去 schema 前缀")
        out = stripped

    return out, rules


# ======================================================================
# 三、执行（镜像 / 直连）
# ======================================================================

_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$")
_DT_FMT = "%Y-%m-%d %H:%M:%S"


def _normalize(v):
    """镜像读值归一化。

    SQLAlchemy 的 SQLite 方言把 DateTime 存成 `YYYY-MM-DD HH:MM:SS.ffffff` 文本，
    而直连（dmPython/pyodbc）返回的是 datetime 对象、经 `json_safe` 后为
    `YYYY-MM-DD HH:MM:SS`。这里把"看起来是时间戳"的文本按同一格式归一，
    使两种模式的 JSON 输出逐字节一致（否则会在对拍里表现为大量假差异）。
    """
    if isinstance(v, str) and _DT_RE.match(v):
        try:
            return datetime.strptime(v.split(".")[0], _DT_FMT)
        except ValueError:  # pragma: no cover
            return v
    return v


_FALLBACKS: list[dict] = []
_FALLBACK_MAX = 50


def _record_fallback(source: str, sql: str, translated: str, error: str) -> None:
    """记录"本应走镜像但回退直连"的原因（有界环形记录，供诊断/对拍查看）。"""
    _FALLBACKS.append({
        "source": source,
        "error": error[:300],
        "sql": sql.strip().replace("\n", " ")[:300],
        "translated": translated.strip().replace("\n", " ")[:300],
    })
    del _FALLBACKS[:-_FALLBACK_MAX]


def fallback_log() -> list[dict]:
    """最近的镜像→直连回退记录（诊断用；也为后续在管理页透出留接口）。"""
    return list(_FALLBACKS)


def _metadata_sql(sql: str) -> bool:
    """元数据查询（表结构/库列表）必须走直连——镜像里没有这些系统视图。"""
    return bool(
        re.search(
            r"ALL_TABLES|ALL_TAB_COLUMNS|USER_TAB|INFORMATION_SCHEMA|\bsys\.|DBCC\b",
            sql,
            re.I,
        )
    )


def try_mirror(sql: str, source: str, params=None, strict: bool = False) -> dict | None:
    """在镜像上执行查询；返回结构与 `db.adm/db.asql` 完全一致（`{columns, rows}`）。

    strict=False（历史行为）：不可用时返回 None，由调用方回退直连。
    strict=True（镜像模式，现行默认）：不可用时抛 `MirrorUnavailable`（DbError 子类），
    **绝不回退直连**——保证"应用层只连镜像"这一架构约定可被验证。
    """
    mode, _reason = resolve_mode()
    if mode != "mirror":
        return None
    if _metadata_sql(sql):
        if strict:
            raise MirrorUnavailable("镜像模式不提供数据库元数据查询（快照不含系统视图）")
        return None

    # 只读校验必须保留：非只读语句原样抛错（口径与直连路径一致）
    from app.core.readonly import assert_dm_readonly, assert_mssql_readonly

    try:
        (assert_dm_readonly if source == "dm" else assert_mssql_readonly)(sql)
    except Exception:
        if strict:
            raise
        return None

    try:
        translated, rules = translate(sql, source)
    except NotTranslatable as exc:
        if strict:
            raise MirrorUnavailable(f"SQL 含镜像不支持的方言构造：{exc}") from exc
        return None

    eng = store.current_engine()
    try:
        with eng.connect() as conn:
            result = conn.exec_driver_sql(translated, tuple(params) if params else None)
            columns = list(result.keys())
            rows = [[_normalize(v) for v in r] for r in result.fetchall()]
    except Exception as exc:  # noqa: BLE001
        # 翻译漏网（如未识别的方言函数）或快照缺表：记录原因供诊断。
        # strict（镜像模式）下抛明确错误而非回退——应用层不碰源库。
        _record_fallback(source, sql, translated, str(exc))
        if strict:
            raise MirrorUnavailable(f"镜像执行失败：{exc}") from exc
        return None

    # 与直连保持同一序列化口径
    from app.core.serialize import json_safe_row

    return {
        "columns": columns,
        "rows": [json_safe_row(r) for r in rows],
        "mode": f"mirror:{source}",
        "translated": rules,
    }
