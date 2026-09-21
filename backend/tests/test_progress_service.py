"""抽采样进度统计 · 服务层（service）单元测试。

覆盖：SQL 构造是否踩到镜像翻译层的禁用地雷、区域归类四层兜底、产品类型精确/别名/去括号匹配、
聚合的剔除与去重、跨合同重复检测、看板组装（完成率分母、未匹配列不计入合计、未指定合同视图）。
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.modules.progress import service, store

PTS = [
    {"id": 1, "name": "豇豆", "members": "", "category": "蔬菜"},
    {"id": 2, "name": "辣椒", "members": "", "category": "蔬菜"},
    {"id": 3, "name": "小龙虾", "members": "青虾|龙虾", "category": "水产品"},
]

# 明细行的位置：SID, SNAME, DNO, CNO, TASK, COUNTY, CADDR, DADDR
ROWS = [
    [1, "豇豆", "D1", "C1", "任务A", "常熟市", "", ""],
    [2, "豇豆（KH）", "D2", "C1", "任务A", "虎丘区", "", ""],
    [3, "青虾", "D3", "C2", "任务B", "", "江苏省苏州市吴江区盛泽镇", ""],
    [4, "苹果（BD1）", "D4", "C2", "任务B", "", "", "南京市六合区龙池路1号"],
    [5, "未知品种", "D5", "", "质控", "", "", ""],
    [6, "辣椒", "D6", "C1", "任务A", "昆山市", "", ""],
    [7, "辣椒", "D7", "PS2026001-扩项练兵", "练兵", "昆山市", "", ""],
    [8, "豇豆", "WC26030001", "C1", "任务A", "常熟市", "", ""],
    [1, "豇豆", "D1", "CX", "任务A", "常熟市", "", ""],  # 同一 SID 挂到另一个合同（异常检测）
]


# ==================== SQL 构造 ====================


def test_detail_sql_uses_date_range_and_nvl() -> None:
    sql = service.detail_sql(2026)
    assert "NVL(d.CONTRACTS_NO, '')" in sql
    assert "d.SAMPLING_DATE >= '2026-01-01'" in sql and "d.SAMPLING_DATE < '2027-01-01'" in sql
    assert "d.SAMPLING_DATE IS NULL AND d.ACCEPT_TIME" in sql
    assert "IS_DELETED = 0" in sql
    # 第三层匹配要靠样品类别名 → 必须 join DT_SAMPLE_CATEGORY（不用 s.SAMPLE_CATEGORY_FULL_VALUE_PATH：
    # 实测它只填了 947/8032 行）
    assert "LEFT JOIN DT_SAMPLE_CATEGORY sc ON sc.ID = s.SAMPLE_CATEGORY_ID" in sql
    assert "NVL(sc.NAME, '')" in sql


def test_detail_sql_avoids_untranslatable_constructs() -> None:
    """镜像模式的 SQL 由翻译层解释：命中这些构造会直接报错（不是慢，而是查不出来）。"""
    sql = service.detail_sql(2026).upper()
    for bad in ("CONNECT BY", "DECODE(", "ROWNUM", "LISTAGG", "WM_CONCAT", "MERGE INTO", "SELECT TOP", " OVER("):
        assert bad not in sql, bad
    # 元数据查询会被镜像层拒绝
    for bad in ("ALL_TABLES", "ALL_TAB_COLUMNS", "INFORMATION_SCHEMA", "DBCC"):
        assert bad not in sql, bad
    # 年份过滤不得用函数包住日期列（会导致索引失效）
    assert "EXTRACT(YEAR" not in sql and "YEAR(" not in sql


# ==================== 区域归类 ====================


@pytest.mark.parametrize(
    "county,company_addr,det_addr,expect",
    [
        ("常熟市", "", "", "常熟市"),
        ("虎丘区", "", "", "高新区"),          # 别名归一
        ("吴江市", "", "", "吴江区"),
        ("六合区", "", "", "六合区"),          # 非苏州区县保留为扩展区域
        ("", "江苏省苏州市吴江区盛泽镇", "", "吴江区"),  # 看板的 9 区县关键词兜底
        ("", "", "南京市六合区龙池路 1 号", "六合区"),   # 地址解析出区县名
        ("", "", "某个说不清的地方", "某个说不清的地方"),  # 解析不出来 → 原样返回，由聚合算「未归类」
        ("", "", "", ""),
        # 脏值必须被挡住（实测源库里就有这些内容，不挡会变成垃圾"区域"行）
        ("--", "", "", ""),
        ("nullnullnullnull", "", "", ""),
        ("", "", "江苏省苏州市吴中区吴中大道1399号农发大厦15楼", "吴中区"),  # 先命中 9 区县关键词
        ("", "", "吴中大道1399号农发大厦15楼", ""),                        # 无关键词 → 判为未归类
    ],
)
def test_resolve_region(county, company_addr, det_addr, expect) -> None:
    assert service.resolve_region(county, company_addr, det_addr) == expect


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("常熟市", "常熟市"),
        ("六合区", "六合区"),
        ("--", ""),
        ("nullnullnullnull", ""),
        ("吴中大道1399号农发大厦15楼", ""),
        ("", ""),
        ("很长的区域名称超过十二个字的就该被挡掉", ""),
    ],
)
def test_clean_region_name(raw, expect) -> None:
    assert service.clean_region_name(raw) == expect


def test_resolve_region_prefers_county_over_address() -> None:
    assert service.resolve_region("昆山市", "江苏省苏州市吴中区某路", "") == "昆山市"


# ==================== 产品类型匹配 ====================


def test_product_index_exact_alias_and_bracket_loose() -> None:
    index = service.build_product_index(PTS)
    assert service.match_product_type("豇豆", index) == 1
    assert service.match_product_type(" 豇 豆 ", index) == 1          # 去空白
    assert service.match_product_type("豇豆（KH）", index) == 1       # 去全角括号后缀
    assert service.match_product_type("豇豆(kh)", index) == 1         # 半角括号同样生效
    assert service.match_product_type("青虾", index) == 3             # 命中别名
    assert service.match_product_type("龙虾", index) == 3
    assert service.match_product_type("苹果", index) is None
    assert service.match_product_type("", index) is None
    assert service.match_product_type(None, index) is None


def test_product_index_splits_enumeration_names() -> None:
    """列名里的顿号必须拆开（使用方的列名就是「豇豆、芹菜、辣椒」这种分组名）。"""
    index = service.build_product_index(
        [{"id": 1, "name": "豇豆、芹菜、辣椒", "members": "", "category": "农产品"}]
    )
    for name in ("豇豆", "芹菜", "辣椒"):
        assert service.match_product_type(name, index) == 1
    assert service.match_product_type("豇豆（KH）", index) == 1
    assert service.match_product_type("番茄", index) is None


def test_category_layer_is_third_and_name_wins() -> None:
    """第三层：样品名不命中时，用**样品类别名**命中列名/别名（「其他X」列就靠这个兜底）。"""
    index = service.build_product_index(
        [
            {"id": 1, "name": "豇豆、芹菜、辣椒", "members": "", "category": "农产品"},
            {"id": 2, "name": "其他蔬菜", "members": "蔬菜|蔬果", "category": "农产品"},
            {"id": 3, "name": "其他水产", "members": "鱼类|虾蟹类", "category": "水产品"},
        ]
    )
    assert service.match_product_type("番茄", index, "蔬菜") == 2
    assert service.match_product_type("草莓", index, "蔬果") == 2
    assert service.match_product_type("鲢鱼", index, "鱼类") == 3
    # 样品名优先：豇豆属于「豇豆、芹菜、辣椒」，不能被「其他蔬菜」抢走
    assert service.match_product_type("豇豆", index, "蔬菜") == 1
    assert service.match_product_type("未知物", index, "土壤") is None

    assert service.match_with_layer("豇豆", index, "蔬菜")[1] == service.LAYER_NAME
    assert service.match_with_layer("豇豆（KH）", index, "")[1] == service.LAYER_NAME_LOOSE
    assert service.match_with_layer("番茄", index, "蔬菜")[1] == service.LAYER_CATEGORY
    assert service.match_with_layer("未知物", index, "土壤") == (None, "")


def test_compute_counts_matched_layers_with_category() -> None:
    pts = [{"id": 1, "name": "其他蔬菜", "members": "蔬菜", "category": "农产品"}]
    rows = [[1, "番茄", "D1", "C1", "任务A", "常熟市", "", "", "蔬菜"]]
    r = service.compute(2026, rows, pts)
    assert r["stats"]["unmatched"] == 0
    assert r["stats"]["matched_layers"] == {service.LAYER_CATEGORY: 1}
    assert r["snapshot"] == [{"contract_no": "C1", "region": "常熟市", "product_type_id": 1, "done": 1}]


def test_parse_definition_text_handles_real_paste() -> None:
    """粘贴使用方给的那段文本（Tab 分隔、两种连字符、带成员声明）应能全部解析。"""
    text = (
        "农产品-豇豆、芹菜、辣椒\t农产品—其他蔬菜:蔬菜|蔬果\n"
        "\n水产品—“七条鱼”:鲫鱼|鳊鱼|鲈鱼|乌鳢|泥鳅|黄鳝|牛蛙\n"
        "1. 畜产品-猪肉、猪肝\n"
        "这一行没有分隔符"
    )
    r = service.parse_definition_text(text)
    names = [i["name"] for i in r["items"]]
    assert names == ["豇豆、芹菜、辣椒", "其他蔬菜", "七条鱼", "猪肉、猪肝"]
    assert r["items"][0]["category"] == "农产品" and r["items"][0]["members"] == ""
    assert r["items"][1]["members"] == "蔬菜|蔬果"
    assert r["items"][2]["members"] == "鲫鱼|鳊鱼|鲈鱼|乌鳢|泥鳅|黄鳝|牛蛙"
    assert r["items"][3]["category"] == "畜产品"          # 行首序号被剥掉
    assert len(r["errors"]) == 1 and "分隔符" in r["errors"][0]["reason"]


def test_parse_definition_text_flags_duplicates_and_empty() -> None:
    r = service.parse_definition_text("农产品-水果\n农产品—水果")
    assert any("重复" in e["reason"] for e in r["errors"])
    empty = service.parse_definition_text("   \n\t\n")
    assert empty["items"] == [] and empty["errors"] == []
    bad = service.parse_definition_text("农产品-")
    assert bad["items"] == [] and "名称为空" in bad["errors"][0]["reason"]


def test_compute_without_category_column_still_works() -> None:
    """明细只有 8 列（老单测/降级场景）时不应报错。"""
    r = service.compute(2026, [[1, "豇豆", "D1", "C1", "", "常熟市", "", ""]], PTS)
    assert r["stats"]["done_total"] == 1


def test_exact_match_wins_over_loose() -> None:
    """两个品种在"去括号"后会撞车时，精确匹配优先（不能被宽松层抢走）。"""
    index = service.build_product_index(
        [
            {"id": 1, "name": "苹果", "members": "", "category": ""},
            {"id": 2, "name": "苹果（BD1）", "members": "", "category": ""},
        ]
    )
    assert service.match_product_type("苹果（BD1）", index) == 2
    assert service.match_product_type("苹果", index) == 1


# ==================== 聚合 ====================


def test_compute_counts_buckets_and_exclusions() -> None:
    r = service.compute(2026, ROWS, PTS)
    st = r["stats"]
    assert st["rows"] == 9
    assert st["excluded"] == 2          # 练兵合同 + 测试样品编号
    assert st["dup_across_contracts"] == 1
    assert st["done_total"] == 6        # 9 - 2 剔除 - 1 同 SID 重复
    assert st["unmatched"] == 2         # 苹果（BD1）+ 未知品种
    assert st["unclassified"] == 1      # 未知品种那行没有任何地址
    assert st["no_contract"] == 1

    snap = {(x["contract_no"], x["region"], x["product_type_id"]): x["done"] for x in r["snapshot"]}
    assert snap[("C1", "常熟市", 1)] == 1
    assert snap[("C1", "高新区", 1)] == 1
    assert snap[("C2", "吴江区", 3)] == 1
    assert snap[("C2", "六合区", 0)] == 1        # 未匹配 → 虚拟列 0
    assert snap[("", "未归类", 0)] == 1
    assert snap[("C1", "昆山市", 2)] == 1
    assert sum(snap.values()) == 6              # 快照总和 = 有效样品数（可对拍）


def test_compute_snapshot_total_matches_done_total() -> None:
    r = service.compute(2026, ROWS, PTS)
    assert sum(x["done"] for x in r["snapshot"]) == r["stats"]["done_total"]


def test_compute_returns_contracts_and_regions_for_registration() -> None:
    r = service.compute(2026, ROWS, PTS)
    # 只登记合同编号，**不再从任务名推断合同名**（达梦侧合同名称为空，用户确认不需要名称）
    assert [c for c, _ in r["contracts"]] == ["C1", "C2"]
    assert all(name == "" for _, name in r["contracts"])
    assert "任务A" not in dict(r["contracts"]).values()
    assert set(r["regions"]) == {"常熟市", "高新区", "吴江区", "六合区", "昆山市"}
    assert r["unmatched_names"][0][0] in ("苹果（BD1）", "未知品种")


def test_compute_skips_rows_without_sample_id() -> None:
    r = service.compute(2026, [[None, "豇豆", "D9", "C1", "", "常熟市", "", ""]], PTS)
    assert r["stats"]["done_total"] == 0 and r["snapshot"] == []


# ==================== 看板组装 ====================


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """临时进度库 + 一份手工构造的配置/任务量/快照，用于验证看板组装口径。"""
    monkeypatch.setattr(settings, "progress_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(service, "_bootstrapped", False, raising=False)
    service.bootstrap()  # 基础区域 + 种子产品类型（已含 豇豆/芹菜/辣椒，故按名取用而非重复创建）
    by_name = {p["name"]: p for p in store.list_product_types()}
    pt1 = by_name.get("豇豆") or store.create_product_type("豇豆", category="蔬菜")
    pt2 = by_name.get("辣椒") or store.create_product_type("辣椒", category="蔬菜")
    store.ensure_regions(["常熟市", "昆山市"], source="base")
    store.save_quotas(
        2026,
        [
            {"contract_no": "C1", "region": "常熟市", "product_type_id": pt1["id"], "quota": 10},
            {"contract_no": "C1", "region": "昆山市", "product_type_id": pt1["id"], "quota": 0},
            {"contract_no": "", "region": "常熟市", "product_type_id": pt2["id"], "quota": 5},
        ],
    )
    store.replace_done_snapshot(
        2026,
        [
            {"contract_no": "C1", "region": "常熟市", "product_type_id": pt1["id"], "done": 4},
            {"contract_no": "C1", "region": "昆山市", "product_type_id": pt1["id"], "done": 3},
            {"contract_no": "C1", "region": "常熟市", "product_type_id": service.UNMATCHED_ID, "done": 9},
            {"contract_no": "", "region": "常熟市", "product_type_id": pt2["id"], "done": 2},
        ],
        "2026-09-21 10:00:00",
    )
    return {"pt1": pt1, "pt2": pt2}


def test_overview_summary_excludes_unmatched(env) -> None:
    ov = service.build_overview(2026, None)
    s = ov["summary"]
    assert s["quota_total"] == 10
    assert s["done_total"] == 7            # 4 + 3，未匹配的 9 个不计入
    assert s["done_unmatched"] == 9
    assert s["done_all"] == 16
    assert s["rate"] == 70.0
    assert s["contract_count"] == 1        # 汇总视图不含「未指定合同」
    assert s["product_type_count"] == len(ov["columns"]) - 1  # 去掉虚拟列


def test_overview_unmatched_column_is_virtual_and_not_summed(env) -> None:
    ov = service.build_overview(2026, None)
    virt = [c for c in ov["columns"] if c["virtual"]]
    assert len(virt) == 1 and virt[0]["name"] == service.UNMATCHED_NAME and virt[0]["done"] == 9
    # 合计行里虚拟列有值（供查看），但行合计/总计不含它
    idx = ov["columns"].index(virt[0])
    assert ov["totals"]["done"][idx] == 9
    row = next(r for r in ov["rows"] if r["region"] == "常熟市")
    assert row["done"] == 4 and row["quota"] == 10
    assert sum(ov["totals"]["done"]) == 7 + 9
    assert ov["summary"]["done_total"] == 7


def test_overview_quota_zero_shows_no_rate(env) -> None:
    ov = service.build_overview(2026, None)
    row = next(r for r in ov["rows"] if r["region"] == "昆山市")
    assert row["quota"] == 0 and row["rate"] is None
    cell = next(c for c in row["cells"] if c["product_type_id"] == env["pt1"]["id"])
    assert cell["done"] == 3 and cell["quota"] == 0 and cell["rate"] is None


def test_overview_single_contract_equals_breakdown(env) -> None:
    agg = service.build_overview(2026, None)
    one = service.build_overview(2026, "C1")
    part = next(b for b in agg["by_contract"] if b["contract_no"] == "C1")
    assert one["summary"]["done_total"] == part["done"] == 7
    assert one["summary"]["quota_total"] == part["quota"] == 10
    assert one["summary"]["contract_count"] == 1
    assert one["warnings"]["has_unassigned"] is True


def test_overview_unassigned_contract_view(env) -> None:
    ov = service.build_overview(2026, "")
    assert ov["contract"] == ""
    assert ov["summary"]["done_total"] == 2 and ov["summary"]["quota_total"] == 5
    assert ov["summary"]["rate"] == 40.0


def test_overview_charts_skip_virtual_column(env) -> None:
    ov = service.build_overview(2026, None)
    cats = ov["charts"]["category"]
    assert "未匹配" not in cats["labels"]           # 虚拟列不进品类分布
    assert sum(cats["done"]) == 4 + 3               # 只有已匹配的 7 个样品
    assert cats["labels"][0] == "蔬菜" and cats["done"][0] == 7
    assert ov["charts"]["region_rate"]["labels"] == ["常熟市"]  # 只显示有任务量的区域


def test_overview_rows_include_regions_with_data_only(env) -> None:
    ov = service.build_overview(2026, None)
    names = [r["region"] for r in ov["rows"]]
    assert "常熟市" in names and "昆山市" in names
    # 基础区域（种子里有 9 个）也都会出现在矩阵里（否则没法给它们录任务量）
    assert "张家港市" in names
    assert all(r["region"] != service.UNCLASSIFIED for r in ov["rows"])


def test_quota_panel_and_matrix(env) -> None:
    panel = service.quota_panel(2026, "C1")
    assert panel["contract"] == "C1"
    assert {c["name"] for c in panel["columns"]} >= {"豇豆", "辣椒"}
    got = {(v["region"], v["product_type_id"]): (v["quota"], v["done"]) for v in panel["values"]}
    assert got[("常熟市", env["pt1"]["id"])] == (10, 4)
    assert got[("常熟市", env["pt2"]["id"])] == (0, 0)  # 别的合同的任务量不会被算进来

    mx = service.quota_matrix(2026, "C1")
    row = next(r for r in mx["rows"] if r["region"] == "常熟市")
    assert row["done"] in (4, 0) and row["quota"] == 10
    assert len(row["cells"]) == len(mx["columns"])


def test_available_years_includes_current() -> None:
    import datetime

    years = service.available_years()
    assert datetime.datetime.now().year in years
    assert years == sorted(years, reverse=True)
