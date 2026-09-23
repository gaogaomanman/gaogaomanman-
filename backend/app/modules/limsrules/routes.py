"""LIMS 查询模板规则 API：`/api/lims-rules/*`。

页面读取路径刻意做成"取数前一次拉全"（`GET /api/lims-rules`），
导出时不再逐模板请求；保存/回滚才走 POST。

响应风格与其它模块一致：业务错误返回 200 + `{success:false, error}`。
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.modules.limsrules import store
from app.modules.limsrules.defaults import TEMPLATES

router = APIRouter(prefix="/api/lims-rules", tags=["lims-rules"])


class SaveBody(BaseModel):
    payload: dict = {}
    note: str = ""
    operator: str = ""


class ActivateBody(BaseModel):
    version: int = 0
    operator: str = ""


def _fail(message: str) -> dict:
    return {"success": False, "error": message}


@router.get("")
async def list_rules() -> dict:
    """全部模板的当前生效规则（含内置默认）。"""
    try:
        return {"success": True, "templates": store.list_all()}
    except Exception as exc:  # noqa: BLE001  读取失败不该让页面白屏
        return _fail(f"读取规则失败：{exc}")


@router.get("/templates")
async def template_list() -> dict:
    """模板清单（设置页左侧导航用，不含内容）。"""
    return {
        "success": True,
        "templates": [
            {"templateId": tid, "name": meta["name"], "scope": meta["scope"], "note": meta.get("note", "")}
            for tid, meta in TEMPLATES.items()
        ],
    }


@router.get("/{template_id}")
async def get_rule(template_id: str) -> dict:
    if template_id not in TEMPLATES:
        return _fail(f"未知模板：{template_id}")
    try:
        return {"success": True, **store.get_effective(template_id)}
    except Exception as exc:  # noqa: BLE001
        return _fail(f"读取规则失败：{exc}")


@router.get("/{template_id}/default")
async def get_default(template_id: str) -> dict:
    """内置默认规则（「恢复默认」按钮先看差异再点）。"""
    if template_id not in TEMPLATES:
        return _fail(f"未知模板：{template_id}")
    return {"success": True, "templateId": template_id, "payload": store.initial_defaults(template_id)}


@router.get("/{template_id}/versions")
async def versions(template_id: str, limit: int = 100) -> dict:
    if template_id not in TEMPLATES:
        return _fail(f"未知模板：{template_id}")
    return {"success": True, "versions": store.list_versions(template_id, limit)}


@router.get("/{template_id}/versions/{version}")
async def version_detail(template_id: str, version: int) -> dict:
    if template_id not in TEMPLATES:
        return _fail(f"未知模板：{template_id}")
    data = store.get_version(template_id, version)
    if data is None:
        return _fail(f"版本 {version} 不存在")
    return {"success": True, "templateId": template_id, **data}


@router.post("/{template_id}")
async def save_rule(template_id: str, body: SaveBody) -> dict:
    if template_id not in TEMPLATES:
        return _fail(f"未知模板：{template_id}")
    try:
        result = store.save_version(template_id, body.payload, body.note, body.operator)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"保存失败：{exc}")
    return {"success": True, "message": f"已保存为 v{result['version']}", **result}


@router.post("/{template_id}/activate")
async def activate(template_id: str, body: ActivateBody) -> dict:
    """回滚：把指定历史版本复制为新版本并生效（不删任何历史）。"""
    if template_id not in TEMPLATES:
        return _fail(f"未知模板：{template_id}")
    try:
        result = store.activate_version(template_id, body.version, body.operator)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"回滚失败：{exc}")
    return {"success": True, "message": f"已回滚为 v{body.version}（记为 v{result['version']}）", **result}


@router.post("/{template_id}/reset")
async def reset(template_id: str, body: ActivateBody) -> dict:
    if template_id not in TEMPLATES:
        return _fail(f"未知模板：{template_id}")
    try:
        result = store.reset_default(template_id, body.operator)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"恢复默认失败：{exc}")
    return {"success": True, "message": f"已恢复内置默认（记为 v{result['version']}）", **result}
