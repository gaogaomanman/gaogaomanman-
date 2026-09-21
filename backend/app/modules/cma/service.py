"""CMA 标准核对：逐字移植 `cma-checker/server.js` 的匹配 / 重试 / 并发规则。

保持等价的行为：
- 抓取：`GET {CMA_BASE}?pageNum=1&pageSize=100&standardCode=<code>`，UA `Mozilla/5.0`，
  `Accept: application/json`，超时 20s（配置化）。
- 重试：最多 3 次，仅对「请求超时 / 响应解析失败 / 连接被重置」重试，间隔 1s；
  耗尽后返回 `请求超时`。
- 判定：先按原样精确匹配（`normStd` 相等）→ 再按候选（去空格 → 去版本号）模糊匹配
  （`normBase` 相等）；`versionMismatch`：输入有年号且平台有年号则比较是否相等，
  **输入无年号时直接视为 true**。
- 批量：并发 5，输入去重（trim 后保序）。
"""
from __future__ import annotations

import asyncio
import re
import ssl
from typing import Any, Awaitable, Callable

import httpx

from app.core.config import settings


def _ssl_context() -> ssl.SSLContext:
    """CMA 平台使用的加密套件较弱，OpenSSL 3 默认安全级别(2)会被服务端拒绝握手
    （实测报 `SSLV3_ALERT_HANDSHAKE_FAILURE`）。降低到 SECLEVEL=1 后可正常握手，
    且**证书校验保持开启**。TLS 最低版本设为 1.2。
    """
    ctx = ssl.create_default_context()
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except ssl.SSLError:  # pragma: no cover
        pass
    with_suppress = ssl.TLSVersion.TLSv1_2
    try:
        ctx.minimum_version = with_suppress
    except Exception:  # noqa: BLE001  # pragma: no cover
        pass
    return ctx


_SSL_CTX = _ssl_context()

_WS = re.compile(r"\s+")
_BASE_SUFFIX = re.compile(r"-\d{4}(?:[-/][A-Za-z0-9]+)*$")
_YEAR_IN = re.compile(r"-(\d{4})(?:[-/]|$)")


# ==================== 规范化 ====================


def norm_std(code: Any) -> str:
    return _WS.sub("", str(code if code is not None else "")).upper()


def norm_base(code: Any) -> str:
    return _BASE_SUFFIX.sub("", norm_std(code))


def extract_year(code: Any) -> str:
    m = _YEAR_IN.search(norm_std(code))
    return m.group(1) if m else ""


def build_candidates(code: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()

    def add(value: str, type_: str) -> None:
        if not value or value in seen:
            return
        seen.add(value)
        out.append({"v": value, "type": type_})

    no_space = _WS.sub("", str(code or ""))
    add(no_space, "去空格匹配")
    no_ver = norm_base(no_space)
    if no_ver and no_ver != no_space:
        add(no_ver, "去版本号匹配")
    return out


# ==================== 抓取 ====================


async def _fetch_single(code: str) -> dict:
    params = {"pageNum": 1, "pageSize": 100, "standardCode": code}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=settings.cma_timeout, verify=_SSL_CTX) as client:
            resp = await client.get(settings.cma_base, params=params, headers=headers)
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return {"ok": False, "total": 0, "rows": [], "error": "响应解析失败"}
        if not isinstance(data, dict):
            return {"ok": False, "total": 0, "rows": [], "error": "响应解析失败"}
        rows = data.get("rows")
        return {
            "ok": True,
            "total": data.get("total") or 0,
            "rows": rows if isinstance(rows, list) else [],
            "error": "",
        }
    except httpx.TimeoutException:
        return {"ok": False, "total": 0, "rows": [], "error": "请求超时"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "total": 0, "rows": [], "error": str(exc)}


def _is_transient(error: str) -> bool:
    if error in ("请求超时", "响应解析失败"):
        return True
    low = (error or "").lower()
    return "econnreset" in low or "reset" in low


async def fetch_cma(code: str) -> dict:
    attempts = max(1, settings.cma_retry)
    for attempt in range(1, attempts + 1):
        result = await _fetch_single(code)
        if result["ok"]:
            return result
        if not _is_transient(result.get("error", "")):
            return result
        if attempt < attempts:
            await asyncio.sleep(settings.cma_retry_interval)
    return {"ok": False, "total": 0, "rows": [], "error": "请求超时"}


# ==================== 判定 ====================


def _remark(row: dict) -> str:
    return (row.get("remark") or row.get("arrangeRemark") or "") or ""


def _blank(code: str, error: str = "") -> dict:
    return {
        "found": False,
        "fuzzy": False,
        "versionMismatch": False,
        "matchType": "",
        "standardCode": code,
        "standardMethod": "",
        "remark": "",
        "error": error,
    }


async def query_cma(code: str) -> dict:
    in_year = extract_year(code)

    exact = await fetch_cma(code)
    if exact["ok"] and exact["total"] > 0:
        for row in exact["rows"]:
            if not isinstance(row, dict):
                continue
            if norm_std(row.get("standardCode") or "") == norm_std(code):
                return {
                    "found": True,
                    "fuzzy": False,
                    "versionMismatch": False,
                    "matchType": "精确匹配",
                    "standardCode": row.get("standardCode") or code,
                    "standardMethod": row.get("standardMethod") or "",
                    "remark": _remark(row),
                    "error": "",
                }

    exact_err = "" if exact["ok"] else exact.get("error", "")

    candidates = build_candidates(code)
    if candidates:
        fetched = await asyncio.gather(*[fetch_cma(c["v"]) for c in candidates])
        for cand, res in zip(candidates, fetched):
            if not res["ok"] or not res["total"]:
                continue
            for row in res["rows"]:
                if not isinstance(row, dict):
                    continue
                if norm_base(row.get("standardCode") or "") != norm_base(cand["v"]):
                    continue
                plat_year = extract_year(row.get("standardCode") or "")
                if in_year and plat_year:
                    version_mismatch = in_year != plat_year
                elif not in_year:
                    version_mismatch = True
                else:
                    version_mismatch = False
                return {
                    "found": True,
                    "fuzzy": True,
                    "versionMismatch": version_mismatch,
                    "matchType": cand["type"],
                    "standardCode": row.get("standardCode") or cand["v"],
                    "standardMethod": row.get("standardMethod") or "",
                    "remark": _remark(row),
                    "error": "",
                }

    return _blank(code, exact_err)


# ==================== 并发工具（等价 runWithConcurrency）====================


async def run_with_concurrency(
    tasks: list[Callable[[], Awaitable[Any]]], limit: int | None = None
) -> list[Any]:
    if not tasks:
        return []
    limit = limit or settings.cma_concurrency
    results: list[Any] = [None] * len(tasks)
    idx = 0
    lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal idx
        while True:
            async with lock:
                if idx >= len(tasks):
                    return
                current = idx
                idx += 1
            results[current] = await tasks[current]()

    n = max(1, min(limit, len(tasks)))
    await asyncio.gather(*[worker() for _ in range(n)])
    return results


async def check_standards(codes: list[Any]) -> dict:
    """批量核对：去重保序 + 并发 5。返回 {success, results?, total?, error?}。"""
    if not isinstance(codes, list) or len(codes) == 0:
        return {"success": False, "error": "请提供要核对的标准号清单"}
    seen: set[str] = set()
    unique: list[str] = []
    for c in codes:
        s = str(c if c is not None else "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        unique.append(s)
    if not unique:
        return {"success": False, "error": "有效标准号为空"}
    tasks = [lambda code=code: query_cma(code) for code in unique]
    results = await run_with_concurrency(tasks, settings.cma_concurrency)
    return {"success": True, "results": results, "total": len(unique)}


async def check_one(code: str) -> dict:
    result = await query_cma(code)
    result["_input"] = code
    return {"success": True, "result": result}
