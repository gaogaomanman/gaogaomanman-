"""跨库人员能力聚合：逐字移植 `unified-pool/templates.js` 的清洗与合并规则。

⚠️ 两个模板的规则**不同，不可统一**：
- `person_capability`（按姓名）：按「项目名 + 标准主体(去年号)」去重，
  并把同一标准号统一到跨库最大年份（`baseMaxYear`）。
- `project_person`（按项目/标准）：按「项目名 + 标准号 + 人员」去重，
  **不做年份归一化**；同一键冲突时取字符串更大的 lastDate。

排序：旧实现为 `localeCompare(a.project, 'zh-CN')`（拼音序），此处用 pypinyin 还原。
"""
from __future__ import annotations

import asyncio
import re
from functools import lru_cache

from app.core import db
from app.core.config import settings

# ==================== 字段清洗（对应 cleanProject / cleanMethod / splitMethodYear）====================

_LEAD_NOISE = re.compile(r"^[﹡△*☆★]+")
_TRAILING_GROUP = re.compile(r"(.*?)\s*[（(\[][^（(\[）)\]]*[）)\]]\s*$")
_BRACKET_GROUP = re.compile(r"[（(\[][^（(\[）)\]]*[）)\]]")
_METHOD_RE = re.compile(
    r"([A-Z][A-Z0-9]*(?:/[A-Z][A-Z0-9]*)?\s*\d+(?:\.\d+)*\s*[-—–]\s*\d{4})"
)
_YEAR_RE = re.compile(r"^(.*?)\s*[-—–]\s*(\d{4})$")
_HAS_DIGIT = re.compile(r"[0-9]")
_HAS_SIGN = re.compile(r"[+\-]")
_HAS_ALPHA = re.compile(r"[A-Za-z]")
_HAS_CJK = re.compile(r"[\u4e00-\u9fa5]")


def clean_project(value: object) -> str:
    """剥离前导噪声符号，并循环剥离末尾括号后缀（含保护条件）。"""
    s = _LEAD_NOISE.sub("", str(value if value is not None else "")).strip()
    changed = True
    while changed:
        changed = False
        m = _TRAILING_GROUP.match(s)
        if not m:
            continue
        groups = _BRACKET_GROUP.findall(m.group(0))
        if not groups:
            continue
        inner = groups[-1]
        content = inner[1:-1].strip()
        protect = (
            "包括" in content
            or bool(_HAS_DIGIT.search(content))
            or bool(_HAS_SIGN.search(content))
            or (bool(_HAS_ALPHA.search(content)) and bool(_HAS_CJK.search(content)))
        )
        if not protect:
            s = m.group(1).strip()
            changed = True
    return s.strip()


def clean_method(value: object) -> str:
    s = str(value if value is not None else "").strip()
    if not s:
        return ""
    m = _METHOD_RE.search(s)
    if m:
        return m.group(1).strip()
    idx = s.find("---")
    if idx > 0:
        s = s[:idx].strip()
    return s


def split_method_year(method: object) -> tuple[str, int]:
    s = str(method if method is not None else "").strip()
    m = _YEAR_RE.match(s)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return s, 0


@lru_cache(maxsize=16384)
def _zh_key(text: str) -> tuple:
    """中文排序键：还原 JS `localeCompare(x, 'zh-CN')`（ICU zh-CN）的实测行为。

    实测结论（用旧 Node 实现输出反推）：
    - 标点**参与**排序，且优先级最低（连字符 `-` 排在逗号 `,` 之前）；
    - 数字 < 字母 < 汉字；汉字按拼音音节比较。
    例如旧顺序为 `1,1-二氯乙烷` → `1,1-二氯乙烯` → `1,1,1-三氯乙烷` → `1,1,1,2-四氯乙烷`。
    """
    return tuple(_char_unit(c) for c in str(text or ""))


# 标点权重顺序（越靠前越小）：实测 '-' 小于 ','
_PUNCT_ORDER = "-—–_·./\\,，、;；:：|!'\"?？!！()（）[]【】{}《》<>"
_PUNCT_INDEX = {ch: i for i, ch in enumerate(_PUNCT_ORDER)}


@lru_cache(maxsize=65536)
def _char_unit(ch: str) -> tuple:
    """单字符排序单元：标点(0) < 数字(1) < (拉丁字母 与 汉字拼音)(2) < 其他字母(3)。

    实测结论：ICU 的 zh-CN（拼音）排序把**汉字按拼音与拉丁字母放在同一层级**比较，
    因此「阿维菌素(a…)」排在「C10:0(c…)」之前；希腊字母等非拉丁字母排在其后。
    """
    if ch.isdigit():
        return (1, int(ch))
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
        return (2, ch.lower())
    if 0x4E00 <= ord(ch) <= 0x9FFF or 0x3400 <= ord(ch) <= 0x4DBF:
        try:
            from pypinyin import Style, lazy_pinyin

            # 带声调（TONE3）：实测 ICU 排序中「矮壮素(ai3)」先于「艾氏剂(ai4)」，说明声调参与比较
            syl = lazy_pinyin(ch, style=Style.TONE3)
            if syl:
                return (2, syl[0])
        except Exception:  # noqa: BLE001
            pass
        return (2, ch)
    if ch.isalpha():
        return (3, ch.lower())
    idx = _PUNCT_INDEX.get(ch)
    return (0, idx if idx is not None else 500 + ord(ch))


# ==================== 查询辅助 ====================


def _rows_as_dicts(result: dict) -> list[dict]:
    cols = [str(c).upper() for c in result.get("columns") or []]
    out: list[dict] = []
    for row in result.get("rows") or []:
        out.append({cols[i]: row[i] for i in range(min(len(cols), len(row)))})
    return out


async def _capture(coro) -> tuple[list[dict], str | None]:
    """执行查询并捕获异常：返回 (rows, error)。

    注意：内部查询函数已把结果转成 dict 列表，此处**不可再次转换**。
    """
    try:
        rows = await coro
        return (rows or []), None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


def _annotate_ability(rows: list[dict]) -> None:
    """给卡1 的每行附加「是否在能力表」判定（未上传能力表时两字段为空串）。

    口径：**项目与方法同时命中**才算「在」；先精确、未命中再宽松（见 `ability_store.match`）。
    注意 `ability_store` 会复用本模块的清洗函数，为避免模块级循环依赖，这里**延迟导入**。
    """
    from app.modules.personnel import ability_store

    for row in rows:
        verdict, reason = ability_store.match(row.get("project"), row.get("method"))
        row["inAbility"] = verdict
        row["inAbilityNote"] = reason


# ==================== 模板 1：按姓名查项目 + 标准 ====================


async def _dm_person_rows(name: str, like: str) -> list[dict]:
    sql = (
        "SELECT DISTINCT sp.DECIDE_PROJECT_NAME AS PROJECT, sp.STANDARD_NO AS METHOD "
        f"FROM {settings.dm_schema}.DT_SAMPLE_PROJECT sp "
        "WHERE sp.DETECTION_USER_NAME {op} ? "
        "AND sp.DECIDE_PROJECT_NAME IS NOT NULL AND sp.STANDARD_NO IS NOT NULL"
    )
    result = await db.adm(sql.format(op="="), (name,))
    if result["rows"]:
        return _rows_as_dicts(result)
    result = await db.adm(sql.format(op="LIKE"), (like,))
    return _rows_as_dicts(result)


async def _sql_person_rows(name: str, like: str) -> list[dict]:
    sql = (
        "SELECT DISTINCT Item AS PROJECT, Item_Method AS METHOD FROM TResult "
        "WHERE On_Duty {op} ? AND Item IS NOT NULL AND Item_Method IS NOT NULL"
    )
    result = await db.asql(sql.format(op="="), (name,))
    if result["rows"]:
        return _rows_as_dicts(result)
    result = await db.asql(sql.format(op="LIKE"), (like,))
    return _rows_as_dicts(result)


async def person_capability(name: str) -> dict:
    like = f"%{name}%"
    (dm_rows, dm_err), (sql_rows, sql_err) = await asyncio.gather(
        _capture(_dm_person_rows(name, like)),
        _capture(_sql_person_rows(name, like)),
    )
    dm_rows = dm_rows or []
    sql_rows = sql_rows or []

    all_rows: list[dict] = []
    base_max_year: dict[str, int] = {}
    for source_rows in (dm_rows, sql_rows):
        for r in source_rows:
            p = clean_project(r.get("PROJECT"))
            m = clean_method(r.get("METHOD"))
            if not p or not m:
                continue
            base, year = split_method_year(m)
            all_rows.append({"project": p, "method": m, "base": base, "year": year})
            if year > base_max_year.get(base, 0):
                base_max_year[base] = year

    merged: dict[str, dict] = {}
    for row in all_rows:
        max_year = base_max_year.get(row["base"], 0)
        final_method = (
            f"{row['base']}-{max_year}" if max_year > 0 and row["year"] > 0 else row["method"]
        )
        key = f"{row['project']}\u0000{row['base']}"
        if key not in merged:
            merged[key] = {"project": row["project"], "method": final_method}

    rows = sorted(merged.values(), key=lambda x: _zh_key(x["project"]))
    _annotate_ability(rows)
    return {
        "rows": rows,
        "dbCount": {"dm": len(dm_rows), "sql": len(sql_rows)},
        "errors": {"dm": dm_err, "sql": sql_err},
    }


# ==================== 模板 2：按项目/标准查人员 ====================


async def _dm_project_rows(like: str) -> list[dict]:
    sql = (
        "SELECT sp.DECIDE_PROJECT_NAME AS PROJECT, sp.STANDARD_NO AS METHOD, "
        "sp.DETECTION_USER_NAME AS PERSON, "
        "TO_CHAR(MAX(sp.CREATE_DATETIME),'YYYY-MM-DD') AS LAST_DATE "
        f"FROM {settings.dm_schema}.DT_SAMPLE_PROJECT sp "
        "WHERE (sp.DECIDE_PROJECT_NAME LIKE ? OR sp.STANDARD_NO LIKE ?) "
        "AND sp.DECIDE_PROJECT_NAME IS NOT NULL AND sp.STANDARD_NO IS NOT NULL "
        "AND sp.DETECTION_USER_NAME IS NOT NULL AND sp.DETECTION_USER_NAME<>'' "
        "GROUP BY sp.DECIDE_PROJECT_NAME, sp.STANDARD_NO, sp.DETECTION_USER_NAME"
    )
    return _rows_as_dicts(await db.adm(sql, (like, like)))


async def _sql_project_rows(like: str) -> list[dict]:
    sql = (
        "SELECT Item AS PROJECT, Item_Method AS METHOD, On_Duty AS PERSON, "
        "CONVERT(varchar(10), MAX(Date_Test), 120) AS LAST_DATE FROM TResult "
        "WHERE (Item LIKE ? OR Item_Method LIKE ?) "
        "AND Item IS NOT NULL AND Item_Method IS NOT NULL "
        "AND On_Duty IS NOT NULL AND On_Duty<>'' "
        "GROUP BY Item, Item_Method, On_Duty"
    )
    return _rows_as_dicts(await db.asql(sql, (like, like)))


async def project_person(keyword: str) -> dict:
    like = f"%{keyword}%"
    (dm_rows, dm_err), (sql_rows, sql_err) = await asyncio.gather(
        _capture(_dm_project_rows(like)),
        _capture(_sql_project_rows(like)),
    )
    dm_rows = dm_rows or []
    sql_rows = sql_rows or []

    merged: dict[str, dict] = {}
    for source_rows in (dm_rows, sql_rows):
        for r in source_rows:
            p = clean_project(r.get("PROJECT"))
            m = clean_method(r.get("METHOD"))
            person = str(r.get("PERSON") or "").strip()
            if not p or not m or not person:
                continue
            key = f"{p}\u0000{m}\u0000{person}"
            date = str(r.get("LAST_DATE") or "").strip()
            if key not in merged or date > merged[key]["lastDate"]:
                merged[key] = {"project": p, "method": m, "person": person, "lastDate": date}

    rows = sorted(merged.values(), key=lambda x: _zh_key(x["project"]))
    return {
        "rows": rows,
        "dbCount": {"dm": len(dm_rows), "sql": len(sql_rows)},
        "errors": {"dm": dm_err, "sql": sql_err},
    }
