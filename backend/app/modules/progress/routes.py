"""抽采样进度统计接口（前缀 `/api/progress`）。

分层约定：本文件**不碰数据源、不碰库**，只做参数校验与错误映射——
数据源在 `service.py`，库在 `store.py`（便于 S3 用临时目录 + 假数据做单测）。

错误映射：`ValueError` → 400（用户输入问题，消息可直接展示）；`DbError` → 502（取数失败）；
`sqlite3.Error` → 502；其余异常 → 500 且日志留摘要。响应统一 `{"success": ..., ...}`。
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.errors import DbError
from app.modules.progress import service, store
from app.modules.progress.normalize import clean_field

_LOG = logging.getLogger("app.progress.routes")
router = APIRouter(prefix="/api/progress", tags=["progress"])

YEAR_MIN, YEAR_MAX = 2000, 2100


def _year(year: int | None) -> int:
    if year is None:
        return datetime.now().year
    if not (YEAR_MIN <= int(year) <= YEAR_MAX):
        raise HTTPException(status_code=400, detail="year 参数非法")
    return int(year)


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, DbError):
        return HTTPException(status_code=502, detail=exc.message)
    if isinstance(exc, sqlite3.Error):
        return HTTPException(status_code=502, detail=f"进度库读写失败：{exc}")
    _LOG.exception("progress 接口异常")
    return HTTPException(status_code=500, detail=str(exc))


def _csv(value: str | None) -> list[str]:
    """逗号分隔的多选参数 → 列表（去空白、去重保序）。"""
    if not value:
        return []
    out: list[str] = []
    for seg in str(value).split(","):
        s = seg.strip()
        if s and s not in out:
            out.append(s)
    return out


# ==================== 请求体 ====================


class SyncBody(BaseModel):
    year: int | None = None


class BigKindBody(BaseModel):
    name: str = ""
    keywords: str | None = None
    sort_no: int | None = None
    enabled: bool | None = None


class CategoryBody(BaseModel):
    id: int | None = None
    name: str = ""
    # ⚠️ 必须是 None 而不是 "" —— 局部更新（只改关键词）时漏传该字段不能被当成"清空大类"
    big_kind: str | None = None
    is_other: bool | None = None
    sort_no: int | None = None
    enabled: bool | None = None
    # 匹配关键词（这一列接住哪些样品）；传 None = 不改动
    keywords: str | None = None
    # 新增时必填：这一列属于哪个「列方案」（任务 × 年份）
    scheme_id: int = 0


class TaskTypeBody(BaseModel):
    """任务类型（考核表的单位，如市例行 / 市监督）。"""
    id: int | None = None
    name: str = ""
    sort_no: int | None = None
    enabled: bool | None = None


class SchemeBody(BaseModel):
    """列方案 = 任务 × 年份；`from_year`/`from_task_type_id` 非空表示"复制过来"。"""
    year: int
    task_type_id: int
    note: str | None = None
    from_year: int | None = None
    from_task_type_id: int | None = None


class ProductTypeBody(BaseModel):
    id: int | None = None
    category_id: int | None = None
    name: str = ""
    # 名称/关键词：决定这一列接住哪些样品（「豇豆」列写 豇豆,豆角；「其他蔬菜」写 蔬菜,蔬果）
    keywords: str | None = None
    is_other: bool | None = None
    sort_no: int | None = None
    enabled: bool | None = None


class RegionBody(BaseModel):
    name: str = ""
    keys: str | None = None
    sort_no: int | None = None
    enabled: bool | None = None


class RegionMapBody(BaseModel):
    year: int
    region_key: str = ""
    region: str = ""


class ContractBody(BaseModel):
    contract_no: str = ""
    name: str | None = None      # 传 None = 不改动已有值（页面不再强制填名称）
    note: str = ""
    enabled: bool | None = None
    # 合同归并：把拼写变体（如 ps2026002-市例性）并入正式编号；传 None = 不改动，'' = 取消归并
    alias_of: str | None = None
    # 这份合同属于哪个任务（0 = 未纳入考核表）；传 None = 不改动
    task_type_id: int | None = None


class SettingsBody(BaseModel):
    """模块级设置。目前只有一项：区域判定优先项（county / address）。"""
    year: int | None = None
    region_priority: str = "county"
    # 切换设置后重算哪一年（默认当年）
    task_type_id: int | None = None


class KeyBody(BaseModel):
    """按主键操作（删除 / 启停 / 移动）的通用体。"""
    id: int | None = None
    key: str = ""
    direction: str = ""


class QuotaItem(BaseModel):
    """任务量条目：两种粒度二选一——`product_type_id`（产品级）或 `category_id`（品类级）。

    使用方的考核表是按**品类**下达任务量的（如「农产品-豇豆、芹菜、辣椒」= 10 批次），
    因此品类级是常用粒度；产品级留给以后细化到品种的场合。
    """
    region: str = ""
    product_type_id: int = 0
    category_id: int = 0
    quota: int = 0


class QuotaBody(BaseModel):
    """任务量保存：按**任务**下达（同任务的多份合同共用一份考核表）。"""
    year: int
    task_type_id: int = 0
    items: list[QuotaItem] = Field(default_factory=list)


class ImportRow(BaseModel):
    region: str = ""
    category: str = ""
    product_type: str = ""
    quota: str | int | float | None = 0


class ImportBody(BaseModel):
    year: int
    task_type_id: int = 0
    rows: list[ImportRow] = Field(default_factory=list)


class UnmatchedAssignBody(BaseModel):
    year: int
    sample_name: str = ""
    # 二选一：`category_id`（界面口径）或 `product_type_id`（兼容旧调用）
    category_id: int = 0
    product_type_id: int = 0


class IgnoreBody(BaseModel):
    name: str = ""
    note: str = ""


class BulkDefineBody(BaseModel):
    """批量定义品类与产品类型（写入**当前任务当年**的列方案）。

    每行一条，格式：`大类-品类名[：关键词|关键词]`，品类名里的顿号自动拆成产品类型：
        农产品-豇豆、芹菜、辣椒          → 品类「农产品-豇豆、芹菜、辣椒」，下挂 豇豆/芹菜/辣椒 三个产品
        农产品-其他蔬菜：蔬菜|蔬果        → 品类名含「其他」= 兜底桶，关键词用于接住未匹配的蔬菜
        水产品—其他水产：水产|鱼类|虾蟹类
    """
    text: str = ""
    dry_run: bool = False
    year: int | None = None
    task_type_id: int = 0


# ==================== 读接口 ====================


@router.get("/years")
async def years() -> dict:
    return {"success": True, "years": await asyncio.to_thread(service.available_years)}


@router.get("/task-types")
async def task_types() -> dict:
    """任务类型清单（看板页签 / 配置页下拉）。"""
    try:
        data = await asyncio.to_thread(service.task_type_list, True)
        schemes = await asyncio.to_thread(lambda: [
            {"id": int(s["id"]), "year": int(s["year"]), "task_type_id": int(s["task_type_id"]),
             "task_name": str(s.get("task_name") or ""), "note": str(s.get("note") or ""),
             "category_count": int(s.get("category_count") or 0),
             "quota_total": int(s.get("quota_total") or 0), "done_total": int(s.get("done_total") or 0)}
            for s in store.list_schemes()])
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "task_types": data, "schemes": schemes}


@router.get("/overview")
async def overview(
    year: int | None = Query(default=None),
    task_type_id: int = Query(default=0, description="任务类型 id；0=第一个启用的任务"),
    contracts: str = Query(default="", description="合同编号，逗号分隔；只影响完成量"),
    bizs: str = Query(default="", description="业务类别，逗号分隔；空=全部"),
    cutoff: str = Query(default="", description="截止日期 YYYY-MM-DD；空=全年"),
) -> dict:
    try:
        data = await asyncio.to_thread(
            service.build_overview, _year(year), int(task_type_id or 0) or None,
            _csv(contracts), _csv(bizs), cutoff)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, **data}


@router.get("/config")
async def config(year: int | None = Query(default=None),
                 task_type_id: int = Query(default=0)) -> dict:
    try:
        data = await asyncio.to_thread(service.config_panel, _year(year), int(task_type_id or 0) or None)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, **data}


@router.get("/quotas")
async def quotas(year: int | None = Query(default=None),
                 task_type_id: int = Query(default=0)) -> dict:
    try:
        data = await asyncio.to_thread(service.quota_panel, _year(year), int(task_type_id or 0) or None)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, **data}


@router.get("/export")
async def export(
    year: int | None = Query(default=None),
    task_type_id: int = Query(default=0),
    contracts: str = Query(default=""),
    bizs: str = Query(default=""),
    cutoff: str = Query(default=""),
) -> dict:
    try:
        data = await asyncio.to_thread(
            service.quota_matrix, _year(year), int(task_type_id or 0) or None,
            _csv(contracts), _csv(bizs), cutoff)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, **data}


@router.get("/sync-log")
async def sync_log(year: int | None = Query(default=None), limit: int = Query(default=20)) -> dict:
    try:
        rows = await asyncio.to_thread(store.list_sync_logs, None if year is None else _year(year), limit)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "rows": rows}


# ==================== 同步与重算 ====================


@router.post("/sync")
async def sync(body: SyncBody | None = None) -> dict:
    target = _year(body.year if body else None)
    try:
        return await service.sync_year(target)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/reclassify")
async def reclassify(body: SyncBody | None = None) -> dict:
    """按最新配置重算归类（改关键词/区域别名后调用，不重连镜像）。"""
    target = _year(body.year if body else None)
    try:
        return await asyncio.to_thread(service.reclassify, target)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# ==================== 任务类型与列方案 ====================


@router.post("/task-types")
async def save_task_type(body: TaskTypeBody) -> dict:
    try:
        row = await asyncio.to_thread(store.upsert_task_type, body.name, body.sort_no, body.enabled)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "task_type": row}


@router.post("/task-types/delete")
async def delete_task_type(body: KeyBody) -> dict:
    try:
        await asyncio.to_thread(store.delete_task_type, int(body.id or 0))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "message": "任务已删除"}


@router.post("/schemes")
async def save_scheme(body: SchemeBody) -> dict:
    """建/取列方案；带 `from_year`/`from_task_type_id` 时先**复制**源方案的列再返回。

    页面用法：新年份 → 复制上一年；市监督 → 从市例行复制一份再按本任务考核表改。
    """
    try:
        if body.from_year is not None or body.from_task_type_id is not None:
            result = await asyncio.to_thread(
                service.scheme_copy, _year(body.year), int(body.task_type_id),
                body.from_year, body.from_task_type_id)
            msg = (f"已从 {result['from_year']} 年任务「{(store.get_task_type(result['from_task_type_id']) or {}).get('name', '')}」"
                   f"复制 {result['categories']} 列")
            data = await asyncio.to_thread(service.config_panel, _year(body.year), int(body.task_type_id))
            return {"success": True, "message": msg, **result, "config": data}
        row = await asyncio.to_thread(store.ensure_scheme, _year(body.year), int(body.task_type_id), body.note or "")
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "scheme": row, "message": "列方案就绪"}


@router.post("/schemes/delete")
async def delete_scheme(body: KeyBody) -> dict:
    try:
        await asyncio.to_thread(store.delete_scheme, int(body.id or 0))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "message": "列方案已删除"}


# ==================== 大类 ====================


@router.post("/big-kinds")
async def save_big_kind(body: BigKindBody) -> dict:
    try:
        row = await asyncio.to_thread(
            store.upsert_big_kind, body.name, body.keywords, body.sort_no, body.enabled)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "big_kind": row}


@router.post("/big-kinds/delete")
async def delete_big_kind(body: KeyBody) -> dict:
    try:
        await asyncio.to_thread(store.delete_big_kind, body.key)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "message": f"大类「{body.key}」已删除"}


# ==================== 品类 ====================


@router.post("/categories")
async def save_category(body: CategoryBody) -> dict:
    try:
        if body.id:
            row = await asyncio.to_thread(
                store.update_category, int(body.id), body.name, body.big_kind,
                body.is_other, body.sort_no, body.enabled, body.keywords)
        else:
            row = await asyncio.to_thread(
                store.create_category, body.name, body.big_kind or "", bool(body.is_other),
                body.sort_no, True if body.enabled is None else body.enabled, body.keywords or "",
                int(body.scheme_id or 0))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "category": row}


@router.post("/categories/delete")
async def delete_category(body: KeyBody) -> dict:
    try:
        await asyncio.to_thread(store.delete_category, int(body.id or 0))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "message": "品类已删除"}


@router.post("/categories/move")
async def move_category(body: KeyBody) -> dict:
    try:
        rows = await asyncio.to_thread(store.move_category, int(body.id or 0), body.direction)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "categories": rows}


# ==================== 产品类型 ====================


@router.post("/product-types")
async def save_product_type(body: ProductTypeBody) -> dict:
    try:
        if body.id:
            row = await asyncio.to_thread(
                store.update_product_type, int(body.id), body.name, body.keywords,
                body.category_id, body.is_other, body.sort_no, body.enabled)
        else:
            if body.category_id is None:
                raise ValueError("新增产品类型必须指定所属品类")
            row = await asyncio.to_thread(
                store.create_product_type, int(body.category_id), body.name,
                body.keywords or "", bool(body.is_other), body.sort_no,
                True if body.enabled is None else body.enabled)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "product_type": row}


@router.post("/product-types/delete")
async def delete_product_type(body: KeyBody) -> dict:
    try:
        await asyncio.to_thread(store.delete_product_type, int(body.id or 0))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "message": "产品类型已删除"}


@router.post("/product-types/move")
async def move_product_type(body: KeyBody) -> dict:
    try:
        rows = await asyncio.to_thread(store.move_product_type, int(body.id or 0), body.direction)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "product_types": rows}


_DEF_LINE_RE = re.compile(r"^(?P<cat>[^\-—–－]*)[\-—–－](?P<rest>.+)$")
_DEF_LEAD_RE = re.compile(r"^[\s\d.、,，)）(（【】\[\]]+")
_DEF_QUOTE_RE = re.compile(r"^[\s“”\"'‘’「」《》]+|[\s“”\"'‘’「」《》]+$")
_DEF_SPLIT_RE = re.compile(r"[、,，/｜|]")


@router.post("/product-types/bulk")
async def bulk_define_product_types(body: BulkDefineBody) -> dict:
    """批量定义：解析文本 → 建/改品类与产品类型；`dry_run=True` 只解析不写库。"""
    items: list[dict] = []
    errors: list[dict] = []
    for raw_line in re.split(r"[\r\n\t]+", body.text or ""):
        line = _DEF_LEAD_RE.sub("", str(raw_line).strip())
        if not line:
            continue
        m = _DEF_LINE_RE.match(line)
        if not m:
            errors.append({"line": len(items) + len(errors) + 1, "text": line,
                           "reason": "缺少「大类-品类名」分隔符（支持 - — – －）"})
            continue
        big_kind = clean_field(m.group("cat"), store.MAX_BIG_KIND)
        rest = m.group("rest").strip()
        keywords = ""
        for colon in (":", "："):
            if colon in rest:
                rest, keywords = rest.split(colon, 1)
                break
        name = _DEF_QUOTE_RE.sub("", clean_field(rest, store.MAX_NAME))
        keywords = _DEF_QUOTE_RE.sub("", clean_field(keywords, store.MAX_KEYS))
        if not name or not big_kind:
            errors.append({"line": len(items) + len(errors) + 1, "text": line,
                           "reason": "大类或品类名为空"})
            continue
        products = [p for p in _DEF_SPLIT_RE.split(name) if p.strip()]
        items.append({"big_kind": big_kind, "name": f"{big_kind}-{name}",
                      "is_other": "其他" in name, "keywords": keywords,
                      "products": products})
    if body.dry_run:
        return {"success": True, "items": items, "errors": errors, "written": 0}

    created_cat = updated_cat = 0
    try:
        scheme = await asyncio.to_thread(service.scheme_of, _year(body.year),
                                         service.resolve_task_type(body.task_type_id or None)["id"])
        scheme_id = int(scheme["id"])
        for it in items:
            existing = next((c for c in store.list_categories(include_disabled=True, scheme_id=scheme_id)
                             if str(c["name"]) == it["name"]), None)
            if existing:
                cat = store.update_category(int(existing["id"]), big_kind=it["big_kind"],
                                            is_other=it["is_other"])
                updated_cat += 1
            else:
                cat = store.create_category(it["name"], big_kind=it["big_kind"], is_other=it["is_other"],
                                            scheme_id=scheme_id)
                created_cat += 1
            cid = int(cat["id"])
            for p_name in it["products"]:
                kw = it["keywords"] if it["keywords"] else p_name
                if it["is_other"]:
                    kw = it["keywords"] or p_name
                row = store.find_product_type_by_name(cid, p_name)
                if row:
                    store.update_product_type(int(row["id"]), keywords=kw,
                                              is_other=bool(it["is_other"]))
                else:
                    store.create_product_type(cid, p_name, keywords=kw, is_other=False)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "items": items, "errors": errors,
            "created_categories": created_cat, "updated_categories": updated_cat,
            "message": f"已写入：新增品类 {created_cat} 个、更新 {updated_cat} 个"}


# ==================== 区域 ====================


@router.post("/regions")
async def save_region(body: RegionBody) -> dict:
    try:
        row = await asyncio.to_thread(
            store.upsert_region, body.name, body.keys, body.sort_no, body.enabled)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "region": row}


@router.post("/regions/delete")
async def delete_region(body: KeyBody) -> dict:
    try:
        await asyncio.to_thread(store.delete_region, body.key)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "message": f"区域「{body.key}」已删除"}


@router.post("/regions/map")
async def map_region(body: RegionMapBody) -> dict:
    """把「未映射区域」（如 虎丘区、六合区）挂到某个看板行，并立刻重算该年归类。"""
    try:
        target = next((r for r in store.list_regions() if str(r["name"]) == clean_field(body.region, store.MAX_REGION)), None)
        if target is None:
            raise ValueError(f"区域不存在：{body.region}")
        key = clean_field(body.region_key, store.MAX_REGION)
        if not key:
            raise ValueError("区域键不能为空")
        keys = [k for k in re.split(r"[,\n、，;；|/]+", str(target["keys"] or "")) if k.strip()]
        if key not in keys:
            keys.append(key)
        await asyncio.to_thread(store.upsert_region, str(target["name"]), ",".join(keys), None, None)
        result = await asyncio.to_thread(service.reclassify, _year(body.year))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "message": f"「{key}」已挂到「{body.region}」", **result}


# ==================== 合同 ====================


@router.post("/contracts")
async def save_contract(body: ContractBody) -> dict:
    """保存合同（含「属于哪个任务」）；改了任务归属要重算完成量归属，这里顺手做掉。"""
    try:
        row = await asyncio.to_thread(
            store.upsert_contract, body.contract_no, body.name, body.note,
            True if body.enabled is None else body.enabled, store.SOURCE_MANUAL, body.alias_of,
            body.task_type_id)
        # 任务归属变了 → 明细里的任务字段要跟着变（本地重算，不重连镜像；跨年全算一遍）
        result = await asyncio.to_thread(service.reclassify_all)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "contract": row, **result}


@router.post("/settings")
async def save_settings(body: SettingsBody) -> dict:
    """改区域判定优先项后**本地重算**（不必重连镜像）。"""
    try:
        value = await asyncio.to_thread(service.set_region_priority, body.region_priority)
        result = await asyncio.to_thread(service.reclassify, _year(body.year))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    label = "抽样地址" if value == "address" else "受检单位所在区县（COUNTY）"
    return {"success": True, "region_priority": value, **result,
            "message": f"区域判定已改为「{label}优先」，重算 {result.get('updated', 0)} 条"}


@router.post("/contracts/delete")
async def delete_contract(body: KeyBody) -> dict:
    try:
        await asyncio.to_thread(store.delete_contract, body.key)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "message": f"合同「{body.key}」已删除"}


# ==================== 任务量 ====================


@router.post("/quotas")
async def save_quotas(body: QuotaBody) -> dict:
    """批量保存任务量（按任务保存；同任务的多份合同共用一张考核表）。"""
    try:
        n = await asyncio.to_thread(
            store.save_quotas, _year(body.year),
            [{"task_type_id": body.task_type_id, "region": it.region,
              "product_type_id": it.product_type_id, "category_id": it.category_id,
              "quota": it.quota} for it in body.items])
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "saved": n, "message": f"已保存 {n} 条任务量"}


@router.post("/quotas/clear")
async def clear_quotas(body: QuotaBody | None = None) -> dict:
    """清空某任务某年的任务量。"""
    try:
        task_id = int((body.task_type_id if body else 0) or 0)
        n = await asyncio.to_thread(store.clear_quotas, _year(body.year if body else None), task_id)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "cleared": n, "message": f"已清空 {n} 条任务量"}


@router.post("/quotas/import")
async def import_quotas(body: ImportBody) -> dict:
    """Excel/粘贴导入任务量：按「区域 + 品类/产品类型」定位列（在**当前任务**的列方案里找）。

    - 产品类型名先按「品类 + 名称」找，找不到再在本方案的列里按名称找；仍找不到则报错到 `errors`。
    """
    year = _year(body.year)
    scheme = await asyncio.to_thread(
        service.scheme_of, year, service.resolve_task_type(body.task_type_id or None)["id"])
    scheme_id = int(scheme["id"])
    products = store.list_product_types(include_disabled=True, scheme_id=scheme_id)
    by_cat_name = {(str(p.get("category_name") or ""), str(p["name"])): int(p["id"]) for p in products}
    by_name: dict[str, list[int]] = {}
    for p in products:
        by_name.setdefault(str(p["name"]), []).append(int(p["id"]))
    by_cat: dict[str, list[int]] = {}
    for c in store.list_categories(include_disabled=True, scheme_id=scheme_id):
        by_cat.setdefault(str(c["name"]), []).append(int(c["id"]))
    items: list[dict] = []
    errors: list[dict] = []
    for idx, row in enumerate(body.rows, start=1):
        region = clean_field(row.region, store.MAX_REGION)
        pname = clean_field(row.product_type, store.MAX_NAME)
        cname = clean_field(row.category, store.MAX_NAME)
        if not region or (not pname and not cname):
            errors.append({"row": idx, "reason": "区域为空，或「产品类型」与「品类」都没填"})
            continue
        raw = row.quota
        try:
            quota = int(float(str(raw).strip() or 0))
        except (TypeError, ValueError):
            errors.append({"row": idx, "reason": f"任务量不是数字：{raw!r}"})
            continue
        if not pname:
            # 只有品类 → 品类级任务量（与考核表的列一致）
            cand = by_cat.get(cname) or []
            if len(cand) != 1:
                errors.append({"row": idx, "reason": f"本任务没有唯一的列「{cname}」"})
                continue
            items.append({"task_type_id": int(scheme["task_type_id"]), "region": region,
                          "category_id": cand[0], "quota": quota})
            continue
        pid = by_cat_name.get((cname, pname))
        if pid is None:
            cand = by_name.get(pname) or []
            pid = cand[0] if len(cand) == 1 else None
        if pid is None:
            errors.append({"row": idx, "reason": f"本任务找不到唯一的产品类型「{pname}」"
                                                 + (f"（品类「{cname}」）" if cname else "")})
            continue
        items.append({"task_type_id": int(scheme["task_type_id"]), "region": region,
                      "product_type_id": pid, "quota": quota})
    try:
        n = await asyncio.to_thread(store.save_quotas, year, items)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "saved": n, "errors": errors,
            "message": f"导入完成：成功 {n} 行，失败 {len(errors)} 行"}


# ==================== 未归类处理 ====================


@router.post("/unmatched/assign")
async def assign_unmatched(body: UnmatchedAssignBody) -> dict:
    """把某个未归类样品名挂到某个**品类**：等于给该列追加一个关键词，然后立刻重算归类。"""
    try:
        name = clean_field(body.sample_name, store.MAX_NAME)
        if not name:
            raise ValueError("样品名称不能为空")
        if body.category_id:
            cat = store.get_category(int(body.category_id))
            if cat is None:
                raise ValueError(f"品类不存在：{body.category_id}")
            rep = store.category_rep_product(int(body.category_id)) or {}
            keys = [k for k in re.split(r"[,\n、，;；|/]+", str(rep.get("keywords") or "")) if k.strip()]
            if name not in keys:
                keys.append(name)
            await asyncio.to_thread(store.set_category_keywords, int(body.category_id), ",".join(keys))
            label = str(cat["name"])
        else:
            row = store.get_product_type(int(body.product_type_id))
            if row is None:
                raise ValueError(f"产品类型不存在：{body.product_type_id}")
            keys = [k for k in re.split(r"[,\n、，;；|/]+", str(row["keywords"] or "")) if k.strip()]
            if name not in keys:
                keys.append(name)
            await asyncio.to_thread(store.update_product_type, int(body.product_type_id),
                                    None, ",".join(keys), None, None, None, None)
            label = str(row["name"])
        result = await asyncio.to_thread(service.reclassify, _year(body.year))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "message": f"「{name}」已挂到「{label}」", **result}


@router.post("/unmatched/ignore")
async def ignore_sample(body: IgnoreBody) -> dict:
    """把样品名加入忽略名单（水质/土壤/肥料等不参与统计），随后重算归类。"""
    try:
        await asyncio.to_thread(store.add_ignore_name, body.name, body.note)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "message": f"「{body.name}」已加入忽略名单"}


@router.post("/ignore/remove")
async def remove_ignore(body: KeyBody) -> dict:
    try:
        await asyncio.to_thread(store.remove_ignore_name, body.key)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"success": True, "message": f"「{body.key}」已移出忽略名单"}


@router.get("/stats")
async def stats() -> dict:
    try:
        return {"success": True, "stats": await asyncio.to_thread(store.stats)}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
