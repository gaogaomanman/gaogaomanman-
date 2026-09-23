"""LIMS 查询模板规则：本地 SQLite 存储层（本模块**唯一写库的地方**）。

设计约定（沿用 `progress/store.py` 与 `personnel/ability_store.py` 的既有范式）：

- 标准库 `sqlite3`，无额外依赖；库文件 `<backend>/data/lims_rules/lims_rules.sqlite`；
- 建表幂等（每次连接 `executescript`），写入一律 `with conn:` 单事务，异常自动回滚；
- **版本只增不删**：每次保存 = 新增一个版本号；「回滚」不是删记录，而是把旧版本内容
  复制成一个新版本并置为生效。这样任何一次口径变化都能追溯到"谁、什么时候、改了什么"；
- 从未保存过规则的模板，返回 `defaults.py` 里的内置默认值（`source='builtin'`），
  保证新装环境 / 清空数据后行为与旧版完全一致；
- 出问题时**旧规则不丢**：保存失败会回滚，`rule_active` 仍指向原来的版本。

## 表结构

```
rule_version  规则版本  只增不删；(template_id, version) 唯一
rule_active   生效指针  一个模板一行，指向当前生效的版本号
```
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import BACKEND_DIR
from app.modules.limsrules.defaults import (
    COMBINE_VALUES,
    DUP_KEY_VALUES,
    DUP_POLICY_VALUES,
    GLOBAL_TEMPLATE_ID,
    TEMPLATE_LABELS,
    TEMPLATES,
    all_template_ids,
    default_payload,
)

# ==================== 保护性上限 ====================
MAX_NAME_MAP = 2000          # 别名条目上限（项目名别名可能很多）
MAX_GROUPS = 300             # 合并组上限
MAX_MEMBERS = 50             # 单个合并组的成员上限
MAX_KEY_LEN = 200            # 单个项目名长度上限
MAX_NOTE_LEN = 500
MAX_OPERATOR_LEN = 64

# 折算系数只允许「数字」或「分子/分母」，禁止任意表达式（前端据此直接求值，不用 eval）
_FACTOR_RE = re.compile(r"^\d+(\.\d+)?(\s*/\s*\d+(\.\d+)?)?$")


def rules_dir() -> Path:
    """规则数据目录：`<backend>/data/lims_rules`。"""
    return BACKEND_DIR / "data" / "lims_rules"


def db_file() -> Path:
    return rules_dir() / "lims_rules.sqlite"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_version (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id TEXT NOT NULL,
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    operator    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_rule_version ON rule_version(template_id, version);

CREATE TABLE IF NOT EXISTS rule_active (
    template_id TEXT PRIMARY KEY,
    version     INTEGER NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    d = rules_dir()
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ==================== 校验 ====================


def _clean_text(value: Any, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def validate(template_id: str, payload: Any) -> str | None:
    """校验规则内容，返回错误信息（None = 通过）。宽松但拒脏：只认白名单字段。"""
    if template_id not in TEMPLATES:
        return f"未知模板：{template_id}"
    if not isinstance(payload, dict):
        return "规则内容必须是对象"

    name_map = payload.get("nameMap")
    if name_map is not None:
        if not isinstance(name_map, dict):
            return "nameMap 必须是「数据库项目名 → 模板列名」的对象"
        if len(name_map) > MAX_NAME_MAP:
            return f"别名条目过多（上限 {MAX_NAME_MAP} 条）"
        for k, v in name_map.items():
            if not isinstance(k, str) or not isinstance(v, str):
                return "nameMap 的键和值都必须是字符串"
            if not k.strip() or not v.strip():
                return "nameMap 里存在空的原始项目名或目标项目名"
            if len(k) > MAX_KEY_LEN or len(v) > MAX_KEY_LEN:
                return f"项目名过长（上限 {MAX_KEY_LEN} 字）"

    groups = payload.get("groups")
    if groups is not None:
        if not isinstance(groups, list):
            return "groups 必须是数组"
        if len(groups) > MAX_GROUPS:
            return f"合并组过多（上限 {MAX_GROUPS} 组）"
        for gi, g in enumerate(groups, 1):
            if not isinstance(g, dict):
                return f"第 {gi} 个合并组格式错误"
            target = g.get("target")
            if not isinstance(target, str) or not target.strip():
                return f"第 {gi} 个合并组缺少列名（target）"
            if len(target) > MAX_KEY_LEN:
                return f"第 {gi} 个合并组列名过长"
            members = g.get("members")
            if not isinstance(members, list) or not members:
                return f"合并组「{target}」至少要有一个成员项目"
            if len(members) > MAX_MEMBERS:
                return f"合并组「{target}」成员过多（上限 {MAX_MEMBERS} 个）"
            for m in members:
                if not isinstance(m, dict):
                    return f"合并组「{target}」的成员格式错误"
                mname = m.get("name")
                if not isinstance(mname, str) or not mname.strip():
                    return f"合并组「{target}」存在空的成员项目名"
                factor = m.get("factor", "1")
                if isinstance(factor, (int, float)):
                    continue
                if not isinstance(factor, str) or not _FACTOR_RE.match(factor.strip()):
                    return (
                        f"合并组「{target}」成员「{mname}」的折算系数不合法："
                        f"只允许数字（如 1、0.89）或分数（如 260.38/292.38）"
                    )
                ftext = factor.strip()
                if "/" in ftext:
                    # 分母为 0 会让折算结果变成 Infinity 并污染整列——必须拦下
                    den = ftext.split("/", 1)[1].strip()
                    try:
                        if float(den) == 0:
                            return f"合并组「{target}」成员「{mname}」的折算系数分母不能为 0"
                    except ValueError:
                        return (
                            f"合并组「{target}」成员「{mname}」的折算系数不合法："
                            f"只允许数字（如 1、0.89）或分数（如 260.38/292.38）"
                        )
            combine = g.get("combine")
            if combine is not None and combine not in COMBINE_VALUES:
                return f"合并组「{target}」的合并方式只能是：{' / '.join(COMBINE_VALUES)}"
            dup_key = g.get("dupKey")
            if dup_key is not None and dup_key not in DUP_KEY_VALUES:
                return f"合并组「{target}」的判重方式只能是：{' / '.join(DUP_KEY_VALUES)}"
            dup_policy = g.get("dupPolicy")
            if dup_policy is not None and dup_policy not in DUP_POLICY_VALUES:
                return f"合并组「{target}」的重复策略只能是：{' / '.join(DUP_POLICY_VALUES)}"

    default_combine = payload.get("defaultCombine")
    if default_combine is not None and default_combine not in COMBINE_VALUES:
        return f"默认合并方式只能是：{' / '.join(COMBINE_VALUES)}"

    return None


def sanitize(template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把提交内容规整成标准结构（丢未知字段、补默认值），保证落库内容可控。"""
    base = default_payload(template_id) if template_id in TEMPLATES else {}
    name_map: dict[str, str] = {}
    raw_map = payload.get("nameMap") or {}
    if isinstance(raw_map, dict):
        for k, v in raw_map.items():
            key = _clean_text(k, MAX_KEY_LEN)
            val = _clean_text(v, MAX_KEY_LEN)
            if key and val:
                name_map[key] = val

    groups: list[dict[str, Any]] = []
    raw_groups = payload.get("groups") or []
    if isinstance(raw_groups, list):
        for g in raw_groups:
            if not isinstance(g, dict):
                continue
            members: list[dict[str, str]] = []
            for m in g.get("members") or []:
                if not isinstance(m, dict):
                    continue
                mname = _clean_text(m.get("name"), MAX_KEY_LEN)
                if not mname:
                    continue
                factor = m.get("factor", "1")
                members.append({"name": mname, "factor": _clean_text(factor, 32) or "1"})
            target = _clean_text(g.get("target"), MAX_KEY_LEN)
            if not target or not members:
                continue
            groups.append(
                {
                    "target": target,
                    "members": members,
                    "combine": g.get("combine") or base.get("defaultCombine", "sum"),
                    # 判重方式固定为「按项目名」：历史版本若残留 group 也在此归一，保证行为一致
                    "dupKey": "dn",
                    "dupPolicy": g.get("dupPolicy") or "keepLines",
                }
            )

    out: dict[str, Any] = {
        "name": base.get("name", template_id),
        "scope": base.get("scope", "template"),
        "note": base.get("note", ""),
        "nameMap": name_map,
        "groups": groups,
        "defaultCombine": payload.get("defaultCombine") or base.get("defaultCombine", "sum"),
    }
    if "forcedDupKey" in base:
        out["forcedDupKey"] = base["forcedDupKey"]
    return out


# ==================== 读 ====================


def _active_version(conn: sqlite3.Connection, template_id: str) -> int | None:
    row = conn.execute(
        "SELECT version FROM rule_active WHERE template_id = ?", (template_id,)
    ).fetchone()
    return int(row["version"]) if row else None


def get_effective(template_id: str) -> dict[str, Any]:
    """取模板当前生效规则。无任何保存记录 → 内置默认（source='builtin'）。"""
    if template_id not in TEMPLATES:
        raise KeyError(template_id)
    default = default_payload(template_id)
    with _conn() as conn:
        version = _active_version(conn, template_id)
        if version is None:
            return {
                "templateId": template_id,
                "version": 0,
                "source": "builtin",
                "updatedAt": "",
                "updatedBy": "",
                "note": "",
                "payload": default,
            }
        row = conn.execute(
            "SELECT * FROM rule_version WHERE template_id = ? AND version = ?",
            (template_id, version),
        ).fetchone()
        if row is None:  # 指针悬空（极端情况）：退回默认，不报错
            return {
                "templateId": template_id,
                "version": 0,
                "source": "builtin",
                "updatedAt": "",
                "updatedBy": "",
                "note": "",
                "payload": default,
            }
        return {
            "templateId": template_id,
            "version": int(row["version"]),
            "source": "custom",
            "updatedAt": row["created_at"],
            "updatedBy": row["operator"],
            "note": row["note"],
            "payload": json.loads(row["payload"]),
        }


def list_all() -> list[dict[str, Any]]:
    """全部模板的生效规则（页面设置页一次拉全）。"""
    return [get_effective(t) for t in all_template_ids()]


def get_rule_payload(template_id: str) -> dict[str, Any]:
    """前端取数用的精简接口：只返回 payload（不含版本元信息）。"""
    return get_effective(template_id)["payload"]


def list_versions(template_id: str, limit: int = 100) -> list[dict[str, Any]]:
    if template_id not in TEMPLATES:
        raise KeyError(template_id)
    with _conn() as conn:
        active = _active_version(conn, template_id)
        rows = conn.execute(
            "SELECT id, version, note, operator, created_at, LENGTH(payload) AS size "
            "FROM rule_version WHERE template_id = ? ORDER BY version DESC LIMIT ?",
            (template_id, max(1, min(limit, 500))),
        ).fetchall()
    return [
        {
            "version": int(r["version"]),
            "note": r["note"],
            "operator": r["operator"],
            "createdAt": r["created_at"],
            "size": int(r["size"] or 0),
            "active": int(r["version"]) == active,
        }
        for r in rows
    ]


def get_version(template_id: str, version: int) -> dict[str, Any] | None:
    if template_id not in TEMPLATES:
        raise KeyError(template_id)
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM rule_version WHERE template_id = ? AND version = ?",
            (template_id, version),
        ).fetchone()
    if row is None:
        return None
    return {
        "version": int(row["version"]),
        "note": row["note"],
        "operator": row["operator"],
        "createdAt": row["created_at"],
        "payload": json.loads(row["payload"]),
    }


# ==================== 写 ====================


def _insert_version(
    conn: sqlite3.Connection,
    template_id: str,
    payload: dict[str, Any],
    note: str,
    operator: str,
) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM rule_version WHERE template_id = ?", (template_id,)
    ).fetchone()
    next_version = int(row["v"] or 0) + 1
    now = _now()
    conn.execute(
        "INSERT INTO rule_version (template_id, version, payload, note, operator, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            template_id,
            next_version,
            json.dumps(payload, ensure_ascii=False, sort_keys=False),
            _clean_text(note, MAX_NOTE_LEN),
            _clean_text(operator, MAX_OPERATOR_LEN),
            now,
        ),
    )
    conn.execute(
        "INSERT INTO rule_active (template_id, version, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(template_id) DO UPDATE SET version = excluded.version, "
        "updated_at = excluded.updated_at",
        (template_id, next_version, now),
    )
    return next_version


def save_version(
    template_id: str, payload: dict[str, Any], note: str = "", operator: str = ""
) -> dict[str, Any]:
    """保存为新版本并立即生效。校验失败抛 ValueError（页面直接显示该文案）。"""
    err = validate(template_id, payload)
    if err:
        raise ValueError(err)
    clean = sanitize(template_id, payload)
    with _conn() as conn:
        version = _insert_version(conn, template_id, clean, note, operator)
    return {"templateId": template_id, "version": version, "payload": clean}


def activate_version(template_id: str, version: int, operator: str = "") -> dict[str, Any]:
    """回滚把指定版本的内容复制成一个**新版本**（历史只增不删，便于追溯）。"""
    if template_id not in TEMPLATES:
        raise KeyError(template_id)
    old = get_version(template_id, int(version))
    if old is None:
        raise ValueError(f"版本 {version} 不存在")
    note = f"回滚自 v{version}"
    with _conn() as conn:
        new_version = _insert_version(conn, template_id, old["payload"], note, operator)
    return {"templateId": template_id, "version": new_version, "payload": old["payload"]}


def reset_default(template_id: str, operator: str = "") -> dict[str, Any]:
    """恢复内置默认规则（同样记为一个新版本，可再回滚回来）。"""
    if template_id not in TEMPLATES:
        raise KeyError(template_id)
    payload = default_payload(template_id)
    with _conn() as conn:
        version = _insert_version(conn, template_id, payload, "恢复内置默认", operator)
    return {"templateId": template_id, "version": version, "payload": payload}


def initial_defaults(template_id: str) -> dict[str, Any]:
    """供页面「恢复默认」按钮预览用。"""
    if template_id not in TEMPLATES:
        raise KeyError(template_id)
    return default_payload(template_id)


__all__ = [
    "GLOBAL_TEMPLATE_ID",
    "TEMPLATE_LABELS",
    "db_file",
    "get_effective",
    "get_rule_payload",
    "get_version",
    "initial_defaults",
    "list_all",
    "list_versions",
    "reset_default",
    "rules_dir",
    "activate_version",
    "save_version",
    "sanitize",
    "validate",
]
