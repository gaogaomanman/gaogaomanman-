"""人员能力表比对（ability_store / routes）单元测试。

覆盖：文本归一化与键构造、精确/宽松四级命中、未上传能力表、保存与统计、
持久化重读、**替换失败回滚**、清除、上限保护、接口契约、卡1 行附加字段。
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.modules.personnel import ability_store, routes, service


@pytest.fixture()
def stor(monkeypatch, tmp_path):
    """把能力表落到临时目录（不污染 backend/data），并在用例结束后清缓存。"""
    monkeypatch.setattr(settings, "personnel_ability_dir", str(tmp_path), raising=False)
    ability_store.invalidate()
    yield ability_store
    ability_store.invalidate()


SAMPLE = [
    # 项目名带别名括号（clean_project 会剥掉的 / 因"包括"而保留的）
    ("腐霉利（速克灵）", "GB 23200.121-2026"),
    ("克百威（呋喃丹，包括3-羟基克百威）", "GB 23200.121-2026"),
    ("氧乐果（氧化乐果）", "NY/T 761-2008"),
    ("铅", "GB 5009.12-2023"),
    # 非标准号格式的方法
    ("啶虫脒", "农业部1077号公告-1-2008"),
]
FILE_NAME = "检验检测能力表 (2026.5.30批准).xlsx"


def _save(file_name: str = FILE_NAME) -> dict:
    return ability_store.save(file_name, [{"project": p, "method": m} for p, m in SAMPLE])


# ==================== 归一化与键 ====================


def test_normalize_text_handles_fullwidth_and_whitespace() -> None:
    assert ability_store.normalize_text("  GB 5009.12－2023 ") == "gb5009.12-2023"
    assert ability_store.normalize_text("﹡△毒死蜱") == "毒死蜱"
    assert ability_store.normalize_text("腐霉利（速克灵）") == "腐霉利(速克灵)"


def test_keys_reuse_card1_cleaning() -> None:
    # 复用了卡1 的 clean_project：非"包括"括号被剥离
    assert ability_store.project_key("腐霉利（速克灵）") == "腐霉利"
    # "包括"括号被 clean_project 保留，但宽松键会去掉全部括号内容
    assert ability_store.project_key("克百威（呋喃丹，包括3-羟基克百威）") != "克百威"
    assert ability_store.project_loose_key("克百威（呋喃丹，包括3-羟基克百威）") == "克百威"
    # 方法键保留年号，宽松键去年号
    assert ability_store.method_key("GB 23200.121-2026") == "gb23200.121-2026"
    assert ability_store.method_base_key("GB 23200.121-2026") == "gb23200.121"


# ==================== 未上传 ====================


def test_no_table_returns_blank(stor) -> None:
    assert stor.status()["loaded"] is False
    assert stor.match("铅", "GB 5009.12-2023") == ("", "")


# ==================== 保存与统计 ====================


def test_save_and_status(stor) -> None:
    state = _save()
    assert state["loaded"] is True
    assert state["fileName"] == FILE_NAME
    assert state["count"] == 5
    assert state["uniquePairs"] == 5
    assert state["uniqueProjects"] == 5
    assert state["uploadedAt"]  # 形如 2026-09-18 10:00:00


def test_save_is_whole_table_replace(stor) -> None:
    _save()
    ability_store.save("另一份.xlsx", [{"project": "铅", "method": "GB 5009.12-2023"}])
    state = ability_store.status()
    assert state["count"] == 1
    assert state["fileName"] == "另一份.xlsx"
    # 旧条目已被整表替换
    assert ability_store.match("腐霉利", "GB 23200.121-2026") == ("不在", "")


def test_persist_after_cache_invalidate(stor) -> None:
    _save()
    stor.invalidate()  # 模拟进程重启后的首次读取
    state = stor.status()
    assert state["loaded"] is True and state["count"] == 5
    assert stor.match("铅", "GB 5009.12-2023") == ("在", "")


# ==================== 四级命中 ====================


def test_exact_hit(stor) -> None:
    _save()
    assert stor.match("铅", "GB 5009.12-2023") == ("在", "")
    # 前后空格 / 全角连接符不影响精确命中
    assert stor.match(" 铅 ", "GB5009.12－2023") == ("在", "")
    # 能力表侧别名的括号被 clean_project 剥掉后精确命中
    assert stor.match("腐霉利", "GB 23200.121-2026") == ("在", "")


def test_year_differs(stor) -> None:
    _save()
    assert stor.match("铅", "GB 5009.12-2017") == ("在", ability_store.REASON_YEAR)
    # 人员侧方法未写年号时也算"年号不同"（而不是判不在）
    assert stor.match("铅", "GB 5009.12") == ("在", ability_store.REASON_YEAR)


def test_project_alias(stor) -> None:
    _save()
    # 能力表侧是"克百威（呋喃丹，包括3-羟基克百威）"，人员侧只有"克百威"
    assert stor.match("克百威", "GB 23200.121-2026") == ("在", ability_store.REASON_ALIAS)


def test_alias_and_year_both_differ(stor) -> None:
    _save()
    assert stor.match("克百威", "GB 23200.121-2021") == ("在", ability_store.REASON_BOTH)


def test_not_in_ability_table(stor) -> None:
    _save()
    # 项目在、方法不在
    assert stor.match("铅", "GB 9999.999-2099") == ("不在", "")
    # 方法在、项目不在（组合口径：任一不符即不在）
    assert stor.match("不存在的项目", "GB 23200.121-2026") == ("不在", "")
    assert stor.match("", "GB 23200.121-2026") == ("不在", "")


def test_non_standard_no_method_format(stor) -> None:
    _save()
    assert stor.match("啶虫脒", "农业部1077号公告-1-2008") == ("在", "")
    assert stor.match("啶虫脒", "农业部1077号公告-1-2010") == ("在", ability_store.REASON_YEAR)


# ==================== 替换失败回滚 ====================


def test_replace_failure_keeps_previous_table(stor) -> None:
    _save()
    before = stor.status()

    # 单独用一个 MonkeyPatch：不能复用 fixture 的那个，否则 undo() 会把
    # settings.personnel_ability_dir 的临时目录补丁一起撤销，测试会去读真实目录。
    mp = pytest.MonkeyPatch()

    def boom(conn, meta, rows):
        conn.execute("DELETE FROM ability_item")
        # NOT NULL 违约 → 事务回滚
        conn.execute("INSERT INTO ability_item (project, method) VALUES (NULL, NULL)")

    mp.setattr(stor, "_write", boom)
    try:
        with pytest.raises(Exception):
            stor.save("坏文件.xlsx", [{"project": "铅", "method": "GB 5009.12-2023"}])
    finally:
        mp.undo()

    # 写入失败后索引缓存未失效，状态仍是失败前的那一份
    assert stor.status()["count"] == before["count"]

    # 重新从库里读（模拟重启）：回滚生效，旧表完好
    stor.invalidate()
    after = stor.status()
    assert after["count"] == before["count"]
    assert after["fileName"] == before["fileName"]
    assert stor.match("铅", "GB 5009.12-2023") == ("在", "")


# ==================== 清除 ====================


def test_clear(stor) -> None:
    _save()
    stor.clear()
    state = stor.status()
    assert state["loaded"] is False and state["count"] == 0
    assert stor.match("铅", "GB 5009.12-2023") == ("", "")


# ==================== 上限与空值保护 ====================


def test_empty_items_rejected(stor) -> None:
    with pytest.raises(ValueError, match="未收到任何能力表条目"):
        stor.save("空.xlsx", [])


def test_blank_rows_rejected(stor) -> None:
    with pytest.raises(ValueError, match="未解析到有效"):
        stor.save("空白.xlsx", [{"project": "", "method": ""}, {"project": "  ", "method": " "}])


def test_too_many_items_rejected(stor) -> None:
    items = [{"project": f"P{i}", "method": "GB 1-2020"} for i in range(stor.MAX_ITEMS + 1)]
    with pytest.raises(ValueError, match="条目过多"):
        stor.save("超大.xlsx", items)


def test_field_truncated(stor) -> None:
    long_project = "长" * 500
    state = stor.save("长.xlsx", [{"project": long_project, "method": "GB 1-2020"}])
    assert state["count"] == 1
    assert len(long_project[: stor.MAX_FIELD]) == stor.MAX_FIELD


# ==================== 卡1 行附加字段 ====================


def test_annotate_rows_with_table(stor) -> None:
    _save()
    rows = [
        {"project": "腐霉利", "method": "GB 23200.121-2026"},
        {"project": "克百威", "method": "GB 23200.121-2021"},
        {"project": "不存在", "method": "GB 1-2020"},
    ]
    service._annotate_ability(rows)
    assert (rows[0]["inAbility"], rows[0]["inAbilityNote"]) == ("在", "")
    assert (rows[1]["inAbility"], rows[1]["inAbilityNote"]) == ("在", ability_store.REASON_BOTH)
    assert (rows[2]["inAbility"], rows[2]["inAbilityNote"]) == ("不在", "")


def test_annotate_rows_without_table_is_blank(stor) -> None:
    rows = [{"project": "铅", "method": "GB 5009.12-2023"}]
    service._annotate_ability(rows)
    assert (rows[0]["inAbility"], rows[0]["inAbilityNote"]) == ("", "")


# ==================== 接口契约 ====================


def test_routes_upload_status_clear(stor) -> None:
    body = routes.AbilityTableBody(fileName=FILE_NAME, items=[{"project": p, "method": m} for p, m in SAMPLE])
    up = asyncio.run(routes.upload_ability_table(body))
    assert up["success"] is True
    assert up["count"] == 5 and up["loaded"] is True
    assert up["fileName"] == FILE_NAME
    assert "5 条" in up["message"]

    st = asyncio.run(routes.ability_table_status())
    assert st["success"] is True and st["loaded"] is True and st["count"] == 5

    cl = asyncio.run(routes.clear_ability_table())
    assert cl["success"] is True and cl["cleared"] is True and cl["loaded"] is False


def test_routes_upload_rejects_empty(stor) -> None:
    resp = asyncio.run(routes.upload_ability_table(routes.AbilityTableBody(fileName="空.xlsx", items=[])))
    assert resp["success"] is False
    assert "未收到任何能力表条目" in resp["error"]


def test_routes_upload_rejects_too_many(stor) -> None:
    items = [{"project": f"P{i}", "method": "GB 1-2020"} for i in range(stor.MAX_ITEMS + 1)]
    resp = asyncio.run(routes.upload_ability_table(routes.AbilityTableBody(fileName="超大.xlsx", items=items)))
    assert resp["success"] is False and "条目过多" in resp["error"]
