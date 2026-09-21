"""只读 SQL 校验：移植自 unified-pool/server.js 的 assertReadOnly，并修正其缺陷。

旧实现缺陷：先**整体剥离注释**再检测关键词，会把字符串字面量里的 `--`、`/* */`
一并剥掉（例如 `SELECT '--' AS X`），造成误判。这里改为按引号状态扫描，只剥离真正的注释。

有效行为与旧链路保持一致：旧链路里 3001 页面层允许 SELECT/WITH/DESC/EXPLAIN，
但中间池只允许 SELECT/WITH，因此端到端实际只放行 SELECT/WITH —— 本实现同样只放行
SELECT/WITH，并额外保留写操作黑名单作为纵深防御。
"""
from __future__ import annotations

import re

from app.core.errors import ReadOnlyError

_HEAD_PATTERN = re.compile(r"^(SELECT|WITH)\b", re.IGNORECASE)

_WRITE_PATTERN = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\b|DELETE\s+FROM|DROP\s+TABLE|DROP\s+VIEW|ALTER\s+TABLE|ALTER\s+VIEW"
    r"|TRUNCATE\s+TABLE|CREATE\s+TABLE|CREATE\s+VIEW|CREATE\s+INDEX|MERGE\s+INTO|GRANT\b|REVOKE\b"
    r"|EXEC\b|EXECUTE\b|CALL\b|SELECT\s+INTO)",
    re.IGNORECASE,
)


def strip_comments(sql: str) -> str:
    """按引号状态剥离 SQL 注释（`--` 行注释与 `/* */` 块注释），保护字符串字面量。"""
    out: list[str] = []
    i = 0
    n = len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote is not None:
            out.append(ch)
            if ch == quote:
                # 连续两个引号是转义
                if i + 1 < n and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] not in "\r\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def assert_read_only(sql: str, empty_message: str = "SQL 语句不能为空") -> str:
    """校验并返回剥离注释后的 SQL；不通过则抛 ReadOnlyError。"""
    text = sql or ""
    if not text.strip():
        raise ReadOnlyError(empty_message)

    clean = strip_comments(text)
    head = clean.strip()
    if not _HEAD_PATTERN.match(head):
        raise ReadOnlyError("只允许 SELECT 查询，已拒绝写入/其他语句")
    if _WRITE_PATTERN.search(clean):
        raise ReadOnlyError("检测到写操作（INSERT/UPDATE/DELETE/DROP 等），已拒绝")
    return clean


def assert_dm_readonly(sql: str) -> str:
    return assert_read_only(sql)


def assert_mssql_readonly(sql: str) -> str:
    return assert_read_only(sql)
