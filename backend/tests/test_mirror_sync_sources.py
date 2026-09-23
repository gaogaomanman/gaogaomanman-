# -*- coding: utf-8 -*-
"""`sources` 归一化回归测试。

背景（2026-09-23）：`/api/mirror/sync` 会把 sources 转成列表，而 `sync.py` 原先
直接对入参调 `.split(",")`，于是"显式指定 sources 的手动同步"必然抛
`'list' object has no attribute 'split'`。页面按钮平时不传 sources（走配置默认），
所以这个缺陷一直没暴露，直到定时同步故障排查时用 API 显式传参才踩到。
"""
from app.core.config import settings
from app.mirror.sync import want_sources_of


def test_none_falls_back_to_config() -> None:
    """不传 sources → 取配置 `MIRROR_SYNC_SOURCES`（当前为 dm）。"""
    assert want_sources_of(None) == [
        s.strip().lower() for s in settings.mirror_sync_sources.split(",") if s.strip()
    ]


def test_string_form_is_split() -> None:
    """配置/表单里的字符串形式仍按逗号切分。"""
    assert want_sources_of("dm,sqlserver") == ["dm", "sqlserver"]
    assert want_sources_of(" DM , SqlServer ") == ["dm", "sqlserver"]


def test_list_form_is_accepted() -> None:
    """API 传列表（历史崩溃点）不再报错。"""
    assert want_sources_of(["dm"]) == ["dm"]
    assert want_sources_of(["dm", "sqlserver"]) == ["dm", "sqlserver"]


def test_blank_falls_back_to_config() -> None:
    """空串 / 空列表按"未指定"处理 → 退回配置默认。

    与 API 层一致：`/api/mirror/sync` 在 sources 为空时传 None（走配置），
    否则手动同步会把"空来源"误解成"哪个来源都不搬"。
    """
    assert want_sources_of("") == want_sources_of(None)
    assert want_sources_of([]) == want_sources_of(None)


def test_separators_only_yield_nothing() -> None:
    """只有逗号/空白的字符串 → 归一后为空（调用方据此判断没有可搬来源）。"""
    assert want_sources_of(" , ") == []
    assert want_sources_of(",") == []
