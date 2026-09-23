"""LIMS 查询模板规则：**内置默认规则**（= 迁移前各块硬编码常量，逐条对应）。

⚠️ 本文件是「零口径变化」基线：默认值必须与旧代码完全一致。
某模板从未在页面上保存过规则时，后端就返回这里的默认值，
因此配置化上线后，导出结果与旧版逐格相同。

对应关系（旧位置 → 本文件）：

| 旧位置 | 本文件 |
| --- | --- |
| `frontend/src/views/dm/helpers.ts:17` `PESTICIDE_NAME_MAP` | `TEMPLATES[GLOBAL_TEMPLATE_ID].nameMap` |
| `frontend/src/views/dm/blocks/provinceAgri.ts:39` `PROVINCE_ROUTINE_AGRI_GROUPS` + `:50` `..._FACTORS` | `TEMPLATES['provinceAgri'].groups` |
| `frontend/src/views/dm/blocks/provinceLivestock.ts:34` `..._GROUPS` + `:40` `..._MAP` | `TEMPLATES['provinceLivestock']` |
| `frontend/src/views/dm/blocks/provinceAquatic.ts:34` `..._MAP` | `TEMPLATES['provinceAquatic'].nameMap` |
| `frontend/src/views/dm/blocks/yearlyStats.ts:26` `YEARLY_GROUPS` + `:43` `normalizeYearlyItem` 的 aliasMap | `TEMPLATES['yearlyStats']` |

## 字段说明

- `nameMap`  数据库项目名 → 模板列名。全局表先应用、模板表后应用（模板优先），
  与旧代码「畜产品 map 覆盖 PESTICIDE_NAME_MAP」的行为一致。
- `groups`   合并列：`target` 是模板列名，`members` 是参与合并的数据库项目名 + 折算系数。
  `factor` 只允许「数字」或「分子/分母」两种写法（后端校验，前端解析，不用 eval）。
- `combine`  正常情况（同一子项目只有一条记录）怎么合：
  `sum` 加权求和 / `first` 取首个检出值 / `max` 取最大 / `min` 取最小。
- `dupKey`   判重分组键：只保留 `dn`（按数据库项目名分组，即只把"同一项目多条"视为重复）。
  `group`（整组一个桶）曾用于复刻年度块的旧缺陷，**已于 2026-09-22 取消**。
- `dupPolicy` 同项目多条的展示策略，目前统一 `keepLines`（分行列出 + 标红）。

## 2026-09-22 口径变更记录

年度统计块原先把合并组当作"整组判重"（旧代码分组时读 `x.dn`，但对象字段名是 `subName`，
键恒为 `undefined`，等价于组内只要超过 1 条即算重复），后果是**组内多条只分行标红、不做合并求和**。
经确认后取消该口径：年度块现与农产品块一致，统一「按项目名判重」+「按系数合并求和」。
因此**年度表的导出结果会与改造前不同**（实测 584 个样品中有 88 格），这是有意的口径修正；
其余三个模板（农产品 / 畜产品 / 水产品）与改造前仍逐格一致。
"""
from __future__ import annotations

from typing import Any

# 全局别名（所有模板共用）；单独成一条"伪模板"记录，便于页面集中维护
GLOBAL_TEMPLATE_ID = "__global__"

COMBINE_VALUES = ("sum", "first", "max", "min")
# 2026-09-22 起只保留「按项目名判重」：原先为复刻年度块旧缺陷而保留的「整组判重」已取消，
# 页面不再提供该选项，后端也不接受（历史版本回滚不受影响，读取时统一按 dn 处理）。
DUP_KEY_VALUES = ("dn",)
DUP_POLICY_VALUES = ("keepLines", "latest", "preferReported")

# 各模板的展示名与用途说明（页面设置里显示）
TEMPLATE_LABELS: dict[str, str] = {
    GLOBAL_TEMPLATE_ID: "全局别名（所有模板共用）",
    "provinceAgri": "省例行农产品",
    "provinceLivestock": "省例行畜产品",
    "provinceAquatic": "省例行水产品",
    "yearlyStats": "年度数据快速统计",
}


def _m(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    """成员列表简写：("甲拌磷", "1") → {"name": "甲拌磷", "factor": "1"}"""
    return [{"name": n, "factor": f} for n, f in pairs]


def _group(
    target: str,
    members: list[dict[str, str]],
    combine: str = "sum",
    dup_key: str = "dn",
    dup_policy: str = "keepLines",
) -> dict[str, Any]:
    return {
        "target": target,
        "members": members,
        "combine": combine,
        "dupKey": dup_key,
        "dupPolicy": dup_policy,
    }


# 合并列（农产品 / 年度）共用的 7 组，折算系数 = 主项目分子量 / 子项目分子量
_AGRI_LIKE_GROUPS: list[dict[str, Any]] = [
    _group("甲拌磷（包括甲拌磷砜和甲拌磷亚砜）", _m(
        ("甲拌磷", "1"), ("甲拌磷砜", "260.38/292.38"), ("甲拌磷亚砜", "260.38/276.38"))),
    _group("克百威（包括3-羟基克百威）", _m(
        ("克百威", "1"), ("3-羟基克百威", "221.25/237.25"))),
    _group("涕灭威（包括涕灭威砜和涕灭威亚砜）", _m(
        ("涕灭威", "1"), ("涕灭威砜", "190.26/222.26"), ("涕灭威亚砜", "190.26/206.26"))),
    _group("氟虫腈（包括氟甲腈氟虫腈硫醚氟虫腈砜）", _m(
        ("氟虫腈", "1"), ("氟虫腈砜", "437.15/453.15"), ("氟虫腈亚砜", "437.15/421.15"),
        ("氟甲腈", "437.15/389.08"))),
    _group("乙基多杀菌素", _m(
        ("乙基多杀菌素", "1"), ("乙基多杀菌素J", "1"), ("乙基多杀菌素L", "1"))),
    _group("多杀霉素", _m(
        ("多杀霉素", "1"), ("多杀霉素A", "1"), ("多杀霉素D", "1"))),
    _group("三唑酮", _m(("三唑酮", "1"), ("三唑醇", "1"))),
]

# 农产品块：GROUPS 里比上面多一个「氟虫腈硫醚」，而 FACTORS 未定义它（旧代码回退为 1）
_AGRI_GROUPS: list[dict[str, Any]] = [
    *[dict(g) for g in _AGRI_LIKE_GROUPS[:2]],
    _group("涕灭威（包括涕灭威砜和涕灭威亚砜）", _m(
        ("涕灭威", "1"), ("涕灭威砜", "190.26/222.26"), ("涕灭威亚砜", "190.26/206.26"))),
    _group("氟虫腈（包括氟甲腈氟虫腈硫醚氟虫腈砜）", _m(
        ("氟虫腈", "1"), ("氟甲腈", "437.15/389.08"), ("氟虫腈硫醚", "1"),
        ("氟虫腈砜", "437.15/453.15"), ("氟虫腈亚砜", "437.15/421.15"))),
    *[dict(g) for g in _AGRI_LIKE_GROUPS[4:]],
]

# 畜产品块：合并列只有两组（氟苯尼考按旧代码走"加和"，其余走"取首个检出值"）
_LIVESTOCK_GROUPS: list[dict[str, Any]] = [
    _group("氟苯尼考（氟苯尼考+氟苯尼考胺）", _m(("氟苯尼考", "1"), ("氟苯尼考胺", "1"))),
    _group("β-内酰胺酶(单位：U/ml)", _m(("β-内酰胺酶", "1")), combine="first"),
]

# 年度块：在农产品 7 组基础上多了畜产品两组；注意其氟虫腈组没有「氟虫腈硫醚」（旧代码如此）
# 【2026-09-22 口径变更】原先为复刻旧代码缺陷，年度块每个组都设了「整组判重」，导致
# 组内只要有多条记录就分行标红而不做合并求和。经确认后取消该口径：年度块与农产品一致，
# 统一按「项目名判重」——组内各子项按系数合并为一个值。
_YEARLY_GROUPS: list[dict[str, Any]] = [
    *[dict(g) for g in _AGRI_LIKE_GROUPS],
    _group("氟苯尼考（氟苯尼考+氟苯尼考胺）", _m(("氟苯尼考", "1"), ("氟苯尼考胺", "1"))),
    _group("β-内酰胺酶", _m(("β-内酰胺酶", "1")), combine="first"),
]


TEMPLATES: dict[str, dict[str, Any]] = {
    GLOBAL_TEMPLATE_ID: {
        "name": TEMPLATE_LABELS[GLOBAL_TEMPLATE_ID],
        "scope": "global",
        "note": "所有模板共用；模板自己的别名优先于这里。",
        "nameMap": {
            "呋喃唑酮代谢物[AOZ]": "呋喃唑酮代谢物",
            "呋喃它酮代谢物[AMOZ]": "呋喃它酮代谢物",
            "呋喃妥因代谢物[AHD]": "呋喃妥因代谢物",
            "呋喃西林代谢物[SEM]": "呋喃西林代谢物",
            "磺胺甲基异噁唑（磺胺甲噁唑）": "磺胺甲基异噁唑",
            "磺胺多辛（磺胺邻二甲氧嘧啶）": "磺胺多辛",
            "磺胺间甲氧嘧啶（磺胺-6-甲氧嘧啶）": "磺胺间甲氧嘧啶",
            "磺胺二甲氧嘧啶（磺胺间二甲氧嘧啶、磺胺二甲氧哒嗪）": "磺胺间二甲氧嘧啶",
            "碱类物质": "碱性物质",
            "克伦特罗": "克仑特罗",
        },
        "groups": [],
        "defaultCombine": "sum",
    },
    "provinceAgri": {
        "name": TEMPLATE_LABELS["provinceAgri"],
        "scope": "template",
        "note": "73 个固定项目列；合并组按分子量比值折算后求和。",
        "nameMap": {},
        "groups": _AGRI_GROUPS,
        "defaultCombine": "sum",
    },
    "provinceLivestock": {
        "name": TEMPLATE_LABELS["provinceLivestock"],
        "scope": "template",
        "note": "38 个固定项目列；氟苯尼考与氟苯尼考胺加和，其余取首个检出值。",
        "nameMap": {
            "强力霉素": "多西环素",
            "磺胺间二甲氧嘧啶": "磺胺二甲氧嘧啶",
            "磺胺二甲氧嘧啶（磺胺间二甲氧嘧啶、磺胺二甲氧哒嗪）": "磺胺二甲氧嘧啶",
            "磺胺间甲氧嘧啶（磺胺-6-甲氧嘧啶）": "磺胺间甲氧嘧啶",
            "磺胺甲基异噁唑（磺胺甲噁唑）": "磺胺甲噁唑",
            "克伦特罗": "克仑特罗",
            "呋喃唑酮代谢物[AOZ]": "呋喃唑酮代谢物",
            "二甲硝咪唑": "地美硝唑",
            "二甲硝咪唑（地美硝唑）": "地美硝唑",
            "羟基二甲硝咪唑（羟基地美硝唑）": "羟基地美硝唑",
            # 覆盖全局别名里「碱类物质→碱性物质」的映射，保证模板列「碱类物质」能匹配
            "碱类物质": "碱类物质",
        },
        "groups": _LIVESTOCK_GROUPS,
        "defaultCombine": "first",
    },
    "provinceAquatic": {
        "name": TEMPLATE_LABELS["provinceAquatic"],
        "scope": "template",
        "note": "33 个固定兽药列，均为独立列（不合并），取值取首个检出。",
        "nameMap": {
            "磺胺间甲氧嘧啶（磺胺-6-甲氧嘧啶）": "磺胺间甲氧嘧啶",
            "磺胺二甲氧嘧啶（磺胺间二甲氧嘧啶、磺胺二甲氧哒嗪）": "磺胺间二甲氧嘧啶",
            "磺胺甲基异噁唑（磺胺甲噁唑）": "磺胺甲基异噁唑",
            "呋喃唑酮代谢物[AOZ]": "呋喃唑酮代谢物",
            "呋喃西林代谢物[SEM]": "呋喃西林代谢物",
            "呋喃妥因代谢物[AHD]": "呋喃妥因代谢物",
            "呋喃它酮代谢物[AMOZ]": "呋喃它酮代谢物",
            "强力霉素": "多西环素",
            "磺胺多辛（磺胺邻二甲氧嘧啶）": "磺胺多辛",
        },
        "groups": [],
        "defaultCombine": "first",
    },
    "yearlyStats": {
        "name": TEMPLATE_LABELS["yearlyStats"],
        "scope": "template",
        "note": "动态项目列；合并组同农产品，另含畜产品两组。",
        "nameMap": {
            "克伦特罗": "克仑特罗",
            "盐酸克伦特罗": "克仑特罗",
        },
        "groups": _YEARLY_GROUPS,
        "defaultCombine": "sum",
    },
}


def default_payload(template_id: str) -> dict[str, Any]:
    """取某模板的内置默认规则（深拷贝，避免调用方改动全局常量）。"""
    import copy

    if template_id not in TEMPLATES:
        raise KeyError(template_id)
    return copy.deepcopy(TEMPLATES[template_id])


def all_template_ids() -> list[str]:
    return list(TEMPLATES.keys())
