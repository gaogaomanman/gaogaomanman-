"""一单一库核对接口：等价原 cma-checker/server.js（:3002）+ 台账留痕。

旧实现的错误处理为 200 + `{success:false, error}`，此处保持一致，
以便前端页面的提示文案与判定逻辑原样保留。

**台账留痕**（本次新增）：每次核对都会写入本地台账（`backend/data/cma_ledger/ledger.sqlite`），
形成"谁、何时、核对了什么、结论如何"的完整记录，支撑资质认定关于
"能力持续符合 / 体系持续有效运行"的取证。设计要点：

- 页面逐条调用 `check-one`（5 并发），因此**批次号由前端生成**并随每条请求带上，
  后端按批次号把明细归集到同一批次（`seq` 为批次内序号，`total` 为该批次应有条数）；
- 不传批次信息时（如直接调接口/单条核对）自动开一个单条批次——**任何一次核对都留痕**；
- 留痕失败不影响核对本身，失败原因随响应 `ledger.error` 返回。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.modules.cma import ledger, service

router = APIRouter(prefix="/api/cma", tags=["cma"])


class LedgerMeta(BaseModel):
    """台账上下文（字段名与前端 JSON 一致，避免别名转换带来的歧义）。"""

    batchId: str = ""
    seq: int = 0
    total: int = 0
    operator: str = ""
    dept: str = ""
    purpose: str = ""
    source: str = ""


class CodesBody(BaseModel):
    codes: list[str] = []
    ledger: LedgerMeta | None = None


class CodeBody(BaseModel):
    code: str = ""
    ledger: LedgerMeta | None = None


class NoteBody(BaseModel):
    note: str = ""


class DispositionItem(BaseModel):
    seq: int = 0
    disposition: str = ""


class DispositionsBody(BaseModel):
    """逐条处理意见（一次可提交多条，供"保存全部"与单条保存共用）。"""

    items: list[DispositionItem] = []


def _client_ip(request: Request) -> str:
    """客户端 IP：内网可能是反代/直连，优先取 X-Forwarded-For 首段。"""
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "")


def _meta(meta: LedgerMeta | None, request: Request) -> dict:
    return {
        "operator": (meta.operator if meta else "") or "",
        "dept": (meta.dept if meta else "") or "",
        "purpose": (meta.purpose if meta else "") or "",
        "source": (meta.source if meta else "") or "",
        "client_ip": _client_ip(request),
    }


def _unique_codes(codes: list[str]) -> list[str]:
    """与 service.check_standards 完全一致的去重保序规则，用于把入参对齐到结果。"""
    seen: set[str] = set()
    out: list[str] = []
    for c in codes or []:
        s = str(c if c is not None else "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


@router.post("/check-standards")
async def check_standards(body: CodesBody, request: Request) -> dict:
    try:
        resp = await service.check_standards(body.codes)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc) or "核对失败"}

    if resp.get("success"):
        meta = _meta(body.ledger, request)
        batch_id = (body.ledger.batchId.strip() if body.ledger and body.ledger.batchId.strip() else ledger.new_batch_id())
        unique = _unique_codes(body.codes)
        items = list(zip(unique, resp.get("results") or []))
        info = await asyncio.to_thread(ledger.record_batch, batch_id, items, meta)
        resp["ledger"] = {"batchId": batch_id, **info}
    return resp


@router.post("/check-one")
async def check_one(body: CodeBody, request: Request) -> dict:
    code = (body.code or "").strip()
    if not code:
        return {"success": False, "error": "缺少标准号"}

    try:
        resp = await service.check_one(code)
        result = resp.get("result") or {}
    except Exception as exc:  # noqa: BLE001
        # 核对异常同样要留痕（"查询异常"本身是需要被管理与跟踪的事实）
        message = str(exc) or "查询失败"
        resp = {"success": False, "error": message, "result": {"error": message, "found": False}}
        result = resp["result"]

    meta = _meta(body.ledger, request)
    if body.ledger and body.ledger.batchId.strip():
        batch_id = body.ledger.batchId.strip()
        seq = int(body.ledger.seq or 0)
        expected = int(body.ledger.total or 0)
    else:
        batch_id = ledger.new_batch_id()
        seq = 1
        expected = 1
        meta["source"] = meta["source"] or "单条核对"
    info = await asyncio.to_thread(ledger.record_item, batch_id, seq, expected, code, result, meta)
    resp["ledger"] = {"batchId": batch_id, **info}
    return resp


# ==================== 台账（留痕）查询 ====================


@router.get("/ledger/batches")
async def ledger_batches(
    page: int = 1,
    pageSize: int = 20,
    start: str = "",
    end: str = "",
    keyword: str = "",
    conclusion: str = "",
    operator: str = "",
    pending: int = 0,
) -> dict:
    return await asyncio.to_thread(
        ledger.list_batches, page, pageSize, start, end, keyword, conclusion, operator, pending
    )


@router.get("/ledger/history")
async def ledger_history(kind: str = "all", keyword: str = "", limit: int = 200) -> dict:
    """历史候选（逐条处理意见 / 批次处置说明），供前端下拉选取。"""
    return await asyncio.to_thread(ledger.history, kind, keyword, limit)


@router.get("/ledger/stats")
async def ledger_stats(start: str = "", end: str = "", operator: str = "") -> dict:
    return await asyncio.to_thread(ledger.stats, start, end, operator)


@router.get("/ledger/export")
async def ledger_export(
    start: str = "",
    end: str = "",
    keyword: str = "",
    conclusion: str = "",
    operator: str = "",
    pending: int = 0,
) -> dict:
    return await asyncio.to_thread(ledger.export_all, start, end, keyword, conclusion, operator, pending)


@router.get("/ledger/batches/{batch_id}")
async def ledger_batch(batch_id: str) -> dict:
    return await asyncio.to_thread(ledger.get_batch, batch_id)


@router.post("/ledger/batches/{batch_id}/note")
async def ledger_note(batch_id: str, body: NoteBody) -> dict:
    return await asyncio.to_thread(ledger.set_note, batch_id, body.note)


@router.post("/ledger/batches/{batch_id}/dispositions")
async def ledger_dispositions(batch_id: str, body: DispositionsBody) -> dict:
    """填写/修改逐条处理意见（核对结果表与台账明细共用此接口）。"""
    items = [(int(it.seq), it.disposition) for it in body.items]
    return await asyncio.to_thread(ledger.set_dispositions, batch_id, items)
