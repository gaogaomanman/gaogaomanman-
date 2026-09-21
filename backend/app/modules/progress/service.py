"""抽采样进度统计：镜像取数、归类与统计组装（本模块**唯一读数据源的地方**）。

## 口径（改这里必须同步改 `01_问题定义.md`）

- **任务类型**（市例行 / 市监督）= 看板的第一层选择，一个任务一张考核表；
  每个任务**每年一套列方案**（表头列可不同、可逐年调整）；
- **样品 → 任务**由合同决定（`contract.task_type_id`）；没归属任何任务的合同 →
  样品进「未纳入清单」，**不进任何考核表**；
- **任务量** = 按任务下达（`years × 任务 × 区域 × 列`），同任务的多个合同编号共用一套下达量；
- **完成量** = 样品条数（`COUNT(DISTINCT s.ID)`），粒度 = **任务 × 区域 × 列**；
- **年份归属** = `d.SAMPLING_DATE`（抽样日期）优先，为空回退 `d.ACCEPT_TIME`；
- **统计范围可配置**：年份 + 合同多选 + 业务类别多选 + 截止日期（默认全选/全年）；
- **区域** = `DT_DETECTED_COMPANY.COUNTY` 优先，其次地址关键词/`parse_city_county`；
  解析结果叫「区域键」，再按 `region.keys`（别名）映射到看板行；
  **映射不上的区域不静默丢弃**，汇总进「未映射区域」清单提示；
- **产品类型（归类）**分四层，逐层更宽松：
  1. 样品名 ↔ 产品类型的名称/关键词（精确，归一化后全等）；
  2. 样品名 ↔ 产品类型关键词（宽松：去掉 `（KH）` 这类括号后缀，多字关键词按包含匹配）；
  3. 样品类别名（`DT_SAMPLE_CATEGORY.NAME`）↔ 产品类型关键词；
  4. **兜底桶**：仍未命中 → 匹配「其他XX」类兜底产品自身的名称/关键词；
  命中最长的关键词优先（「其他水产」的 鲈鱼 优先于 鱼类）；
- **未归类**（四层都没命中）= `product_type_id = 0`：**不计入任何列与合计**，
  但进「未归类清单」，可一键挂到某个产品类型（挂载 = 给该列加关键词 + 立刻重算归类）；
- **忽略名单**（水质/土壤/肥料等不参与统计的样品名）：进 `ignore_name`，统计时直接排除并计数；
- **完成率** = 完成量 / 任务量；任务量为 0 显示空（前端渲染「—」）。

## 为什么在 Python 侧聚合

归类是"规则 + 归一化"逻辑，无法用可翻译的 SQL 表达；而单年明细只有几千行，
一次全取 + 内存汇总比在数据库里绕圈子更简单也更可控。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime

from app.core import db
from app.mirror import store as mirror_store
from app.modules.progress import store
from app.modules.progress.normalize import (
    clean_field, contract_key, normalize_text, parse_city_county, strip_brackets,
)

_LOG = logging.getLogger("app.progress.service")

# ==================== 常量（口径字面量） ====================

UNMAPPED_REGION = "未映射区域"
LAYER_EXACT = "品种精确"
LAYER_LOOSE = "品种(忽略括号后缀)"
LAYER_CATEGORY = "样品类别"
LAYER_BUCKET = "兜底桶"
LAYER_BIG_KIND = "大类兜底"
LAYER_KEEP = "手工指定"

_MUNI_RE = re.compile(r"^.+?市(.+(?:区|县|市|园区|新区|开发区))$")
_REGION_JUNK_RE = re.compile(r"null|-{2,}|[\d路街道号楼层室]|^\W+$", re.I)
_REGION_MAX_LEN = 12

META_SYNC_SUMMARY = "sync_summary_{year}"
UNMATCHED_TOP_LIMIT = 300

# 区域判定优先项（设置项，存 app_meta）：
#   county  = 受检单位所在区县（DT_DETECTED_COMPANY.COUNTY）优先（默认）
#   address = 抽样地址优先（与"按抽样地点归属"的台账口径一致）
META_REGION_PRIORITY = "region_priority"
REGION_PRIORITY_COUNTY = "county"
REGION_PRIORITY_ADDRESS = "address"

# 品类单层化：把"一个品类多个产品类型"收敛为每品类一个归属单元（关键词合并、数据改指）
META_CONSOLIDATED = "category_consolidated_v"
CATEGORY_CONSOLIDATED_VERSION = "1"

# 「未指定合同」的筛选哨兵：合同键本身是空串，而空串在逗号分隔的多选参数里会被丢掉，
# 因此用一个显式哨兵传递，服务端再翻回空串。
UNASSIGNED_SENTINEL = "__none__"


def contract_filter_set(contracts: list[str] | None) -> set[str]:
    """多选合同参数 → 合同键集合（空集合 = 不筛选）。"""
    return {
        ("" if str(c).strip() == UNASSIGNED_SENTINEL else contract_key(c))
        for c in (contracts or [])
        if str(c).strip()
    }

# ==================== 种子配置（首次使用写入，之后以页面配置为准） ====================

# 大类：兜底归集的分组依据；关键词命中顺序 = 列表顺序（水产 → 畜产 → 农产，避免「牛蛙」被判成畜产品）
# ⚠️ 引擎里**单字关键词只认全等**（防「桃」误中「樱桃番茄」、「牛」误中「牛蛙」），
#    所以大类判定不能只靠"猪/牛/肉/蛋"这类单字——必须把常见双字词也列上（v2 实测补：猪肉/禽蛋/牛蛙…），
#    否则「牛肉」「鸡蛋」这类样品判不出大类，整类掉进未归类（市监督 26 条的教训）。
BASE_BIG_KINDS: list[tuple[str, str]] = [
    ("水产品", "水产,鱼类,虾蟹类,鱼,虾,蟹,贝,鳝,鳅,蛙,螯,鳖,螺,牛蛙,泥鳅,黄鳝,乌鳢,黑鱼,对虾,沼虾,龙虾,河蟹,螃蟹"),
    ("畜产品", "畜产品,猪,牛,羊,鸡,鸭,鹅,蛋,乳,肉,肝,禽,猪肉,牛肉,羊肉,鸡肉,鸭肉,鹅肉,禽肉,"
               "猪肝,牛肝,羊肝,鸡蛋,鸭蛋,鹅蛋,鹌鹑蛋,鸽子蛋,禽蛋,蛋类,肉类,生鲜乳,牛乳,乳品"),
    ("农产品", "农产品,蔬菜,蔬果,水果,茶叶,小麦,食用菌,菌菇"),
]
BASE_BIG_KINDS_VERSION = "2"
META_BIG_KINDS = "big_kind_seed_v"

# 九个区市（看板行）；keys = 源库 COUNTY / 地址里可能出现的写法
# ⚠️ keys 既做**精确匹配**（COUNTY 值）也做**子串匹配**（地址含区名，如「吴中大道1399号」→ 吴中区），
#    因此不要放"会命中外地"的短键——实测放「新区」会把南京「江北新区」误算进苏州高新区。
BASE_REGIONS: list[tuple[str, str]] = [
    ("张家港市", "张家港市,张家港"),
    ("常熟市", "常熟市,常熟"),
    ("太仓市", "太仓市,太仓"),
    # 显示名与考核表一致用「昆山区」（源库地址里写作昆山市/昆山）
    ("昆山区", "昆山区,昆山市,昆山"),
    ("吴江区", "吴江区,吴江市,吴江"),
    ("吴中区", "吴中区,吴中"),
    ("相城区", "相城区,相城"),
    ("高新区", "高新区,虎丘区,苏州高新区"),
    ("姑苏区", "姑苏区,姑苏,沧浪区,平江区,金阊区"),
]
BASE_REGION_KEYS_VERSION = "4"

# 区域改名（业务口径变化）：把旧名改成新名，历史任务量与明细一并改指；只做一次
BASE_REGION_RENAMES: list[tuple[str, str]] = [("昆山市", "昆山区")]
BASE_REGION_RENAME_VERSION = "1"
META_REGION_RENAME = "region_rename_v"

# ---- 任务类型（考核表的单位） ----
# 使用方 2026 年只考核这两张表；以后增删任务在「任务/方案」页维护。
BASE_TASK_TYPES: list[str] = ["市例行", "市监督"]
# 合同编号 → 任务：源库里市例行一度拼成「市例性」（同一份合同的笔误），必须一并归到市例行
BASE_TASK_CONTRACT_KEYS: dict[str, list[str]] = {
    "市例行": ["市例行", "市例性"],
    "市监督": ["市监督"],
}
META_TASK_SEED = "task_seed_v"
TASK_SEED_VERSION = "1"

# 15 个品类（看板一级表头）与其下产品类型（二级表头）
# 结构：(品类名, 大类, 是否兜底桶, [(产品类型名, 关键词), …])
BASE_CATEGORIES: list[tuple[str, str, bool, list[tuple[str, str]]]] = [
    ("农产品-豇豆、芹菜、辣椒", "农产品", False, [
        ("豇豆", "豇豆,豆角"), ("芹菜", "芹菜"), ("辣椒", "辣椒,青椒,甜椒"),
    ]),
    ("农产品-其他蔬菜", "农产品", True, [
        ("其他蔬菜", "蔬菜,蔬果"),
    ]),
    ("农产品-食用菌", "农产品", False, [
        ("食用菌", "食用菌,香菇,平菇,金针菇,杏鲍菇,蘑菇,木耳,银耳,菌菇,草菇,秀珍菇,双孢菇"),
    ]),
    ("农产品—水果", "农产品", False, [
        ("水果", "水果,草莓,葡萄,杨梅,桃,梨,苹果,枇杷,西瓜,甜瓜,樱桃,柑橘,橙,香蕉,猕猴桃,蓝莓,火龙果,哈密瓜,柚,枣,柿,李"),
    ]),
    ("农产品—茶叶", "农产品", False, [
        ("茶叶", "茶叶,绿茶,红茶,白茶,乌龙茶"),
    ]),
    ("农产品—小麦", "农产品", False, [
        ("小麦", "小麦"),
    ]),
    ("畜产品—猪肉、猪肝", "畜产品", False, [
        ("猪肉", "猪肉"), ("猪肝", "猪肝"),
    ]),
    ("畜产品—牛肉、牛肝", "畜产品", False, [
        ("牛肉", "牛肉"), ("牛肝", "牛肝"),
    ]),
    ("畜产品—羊肉、羊肝", "畜产品", False, [
        ("羊肉", "羊肉"), ("羊肝", "羊肝"),
    ]),
    ("畜产品—禽肉", "畜产品", False, [
        ("禽肉", "禽肉,鸡肉,鸭肉,鹅肉,鸡,鸭,鹅"),
    ]),
    ("畜产品—鸡蛋、鹌鹑蛋", "畜产品", False, [
        ("鸡蛋", "鸡蛋"), ("鹌鹑蛋", "鹌鹑蛋"),
    ]),
    ("畜产品—其他禽蛋", "畜产品", True, [
        ("其他禽蛋", "禽蛋,蛋"),
    ]),
    ("畜产品—生鲜牛乳", "畜产品", False, [
        ("生鲜牛乳", "生鲜牛乳,牛乳,生鲜乳,牛奶"),
    ]),
    ("水产品—“七条鱼”", "水产品", False, [
        ("七条鱼", "青鱼,草鱼,鲢鱼,鳙鱼,鲫鱼,鳊鱼,鲤鱼"),
    ]),
    ("水产品—其他水产", "水产品", True, [
        ("其他水产", "水产,鱼类,虾蟹类,鲈鱼,黄鳝,泥鳅,牛蛙,乌鳢,小龙虾,罗氏沼虾,克氏原螯虾,中华绒螯蟹,"
                   "螃蟹,青虾,白虾,河虾,虾,蟹,贝,螺,鳅,鳝,蛙,螯,鳖"),
    ]),
]


# ==================== 基础数据（幂等） ====================

_bootstrapped = False


def bootstrap() -> None:
    """首次使用写入种子配置（大类 / 九区 / 任务类型 / 各年列方案与 15 列）；幂等，不覆盖已有配置。"""
    global _bootstrapped
    if _bootstrapped:
        return
    for name, keywords in BASE_BIG_KINDS:
        if not any(k["name"] == name for k in store.list_big_kinds()):
            store.upsert_big_kind(name, keywords)
    # 大类关键词按版本刷新一次（v2 补双字词；单字在全等规则下接不住「牛肉/禽蛋」这类样品）
    if store.get_meta(META_BIG_KINDS, "") != BASE_BIG_KINDS_VERSION:
        for name, keywords in BASE_BIG_KINDS:
            store.upsert_big_kind(name, keywords)
        store.set_meta(META_BIG_KINDS, BASE_BIG_KINDS_VERSION)

    # 区域改名（如「昆山市」→「昆山区」）必须在 ensure_regions 之前：否则新名字会被当成新区域建，
    # 旧名那一行还留着，看板会多出一行。
    if store.get_meta(META_REGION_RENAME, "") != BASE_REGION_RENAME_VERSION:
        for old, new in BASE_REGION_RENAMES:
            store.rename_region(old, new)
        store.set_meta(META_REGION_RENAME, BASE_REGION_RENAME_VERSION)

    store.ensure_regions([n for n, _ in BASE_REGIONS], source=store.SOURCE_BASE)
    # 基础区域的别名按版本刷新一次（老版本把「新区」当过高新区的键，会误命中南京「江北新区」）；
    # 刷新只作用于这 9 个基础区域，用户自建的区域不受影响。
    keys_version = store.get_meta("base_region_keys_version", "")
    if keys_version != BASE_REGION_KEYS_VERSION:
        existing = {r["name"]: r for r in store.list_regions()}
        for idx, (name, keys) in enumerate(BASE_REGIONS, start=1):
            if name in existing:
                # 同时统一行顺序：与使用方考核表的县区市顺序一致
                # （张家港市 / 常熟市 / 太仓市 / 昆山区 / 吴江区 / 吴中区 / 相城区 / 高新区 / 姑苏区）
                store.upsert_region(name, keys=keys, sort_no=idx * 10, source=store.SOURCE_BASE)
        store.set_meta("base_region_keys_version", BASE_REGION_KEYS_VERSION)

    # ---- 任务类型：市例行 / 市监督 ----
    for name in BASE_TASK_TYPES:
        if store.get_task_type_by_name(name) is None:
            store.upsert_task_type(name)
    # 合同归任务：只填「还没归属」的，人工设置过的一律不动
    assign_contract_tasks()
    # 合同归好任务后，把迁移时落在 task_type_id=0 的旧任务量按合同补挂（幂等；日志在 store 里打）
    store.remap_quotas_by_contract()

    # ---- 列方案（任务 × 年份）----
    # 有明细/任务量的年份都要有方案（历史年份的表按当年列显示）；再保证当年也有。
    years = {datetime.now().year, *store.done_years(), *store.quota_years()}
    plans = {y: ensure_year_schemes(y, seed_columns=False) for y in sorted(years)}
    # ① 老库遗留列（scheme_id=0）先挂到「市例行」方案：老版本只有一套全局列，
    #    那 15 列就是使用方市例行考核表的列。
    orphan_year = min(store.done_years() or years)
    first = (plans.get(orphan_year) or {}).get(BASE_TASK_TYPES[0])
    if first is not None:
        moved = store.assign_category_scheme(int(first["id"]), only_orphan=True)
        if moved:
            _LOG.warning("progress 库升级：%s 个既有表头列已挂到「%s」%s 年方案", moved, BASE_TASK_TYPES[0], orphan_year)
    # ② 再按需灌种子列：只在「市例行」方案仍为空时写入，并给其它任务（市监督）复制一份
    ensure_year_schemes(max(years), seed_columns=True)
    # ③ 老库收敛：把「一个品类多个产品类型」合并为每品类一个归属单元（幂等、数据改指不丢）
    if store.get_meta(META_CONSOLIDATED, "") != CATEGORY_CONSOLIDATED_VERSION:
        store.consolidate_categories()
        for cat in store.list_categories(include_disabled=True):
            if not store.category_rep_product(int(cat["id"])):
                store.set_category_keywords(int(cat["id"]), str(cat["name"]))
        store.set_meta(META_CONSOLIDATED, CATEGORY_CONSOLIDATED_VERSION)

    # ---- 明细的任务归属（老库补写：原先没这个字段）----
    task_map = contract_task_map()
    for y in store.done_years():
        store.set_done_task_types(y, task_map)

    store.set_meta(META_TASK_SEED, TASK_SEED_VERSION)
    _bootstrapped = True


# ==================== 任务类型与列方案 ====================


def assign_contract_tasks() -> int:
    """按合同编号里的关键词把合同归到任务（`ps2026002-市例行`/`ps2026002-市例性` → 市例行）。

    **只处理还没有归属的合同**：人工在「合同」页改过的（含改成"未纳入"）一律不动。
    """
    assigned = 0
    for c in store.list_contracts(include_disabled=True):
        if int(c.get("task_type_id") or 0):
            continue
        no = str(c["contract_no"])
        for task_name, keys in BASE_TASK_CONTRACT_KEYS.items():
            if any(k in no for k in keys):
                tt = store.get_task_type_by_name(task_name)
                if tt is not None:
                    store.set_contract_task(no, int(tt["id"]))
                    assigned += 1
                break
    if assigned:
        _LOG.info("合同归任务：自动归属 %s 份合同", assigned)
    return assigned


def contract_task_map() -> dict[str, int]:
    """`{合同编号: 任务 id}`（只含有归属的合同）。"""
    return {str(c["contract_no"]): int(c["task_type_id"] or 0)
            for c in store.list_contracts(include_disabled=True)
            if int(c.get("task_type_id") or 0)}


def task_type_list(include_disabled: bool = False) -> list[dict]:
    """任务类型清单（含该任务在库里的规模，供页面做页签/配置）。"""
    out = []
    for t in store.list_task_types(include_disabled=True):
        if not include_disabled and not int(t["enabled"]):
            continue
        out.append({"id": int(t["id"]), "name": str(t["name"]), "sort_no": int(t["sort_no"]),
                    "enabled": bool(t["enabled"])})
    return out


def resolve_task_type(task_type_id: int | None) -> dict:
    """把页面传的任务 id 解析成任务；空/非法 → 回落第一个启用的任务（保证页面永远有内容）。"""
    tasks = task_type_list()
    if not tasks:
        tasks = task_type_list(include_disabled=True)
    if not tasks:
        raise ValueError("还没有任何任务类型，请先在「任务/方案」页维护")
    if task_type_id:
        hit = next((t for t in tasks if int(t["id"]) == int(task_type_id)), None)
        if hit is None:
            hit = next((t for t in task_type_list(include_disabled=True) if int(t["id"]) == int(task_type_id)), None)
        if hit:
            return hit
        _LOG.warning("任务 %s 不存在，回落到「%s」", task_type_id, tasks[0]["name"])
    return tasks[0]


def ensure_year_schemes(year: int, seed_columns: bool = False) -> dict[str, dict]:
    """给某年每个任务建列方案（不存在则建，空方案）。

    `seed_columns=True`：把 15 个基础列灌进「市例行」方案，其余任务**复制**一份
    （副本只是起点，使用方按各自考核表在页面上增删改名；市监督方案已按此建好，待核对）。
    """
    year = int(year)
    out: dict[str, dict] = {}
    tasks = task_type_list(include_disabled=True)
    first_name = BASE_TASK_TYPES[0]
    for t in tasks:
        out[str(t["name"])] = store.ensure_scheme(year, int(t["id"]))
    if not seed_columns:
        return out
    src = out.get(first_name)
    if src is None:
        return out
    if not store.list_categories(include_disabled=True, scheme_id=int(src["id"])):
        for cat_name, big_kind, is_other, products in BASE_CATEGORIES:
            keys: list[str] = []
            for p_name, p_keys in products:
                for raw in [p_name, *str(p_keys).split(",")]:
                    for seg in str(raw).split(","):
                        s = seg.strip()
                        if s and s not in keys:
                            keys.append(s)
            store.create_category(cat_name, big_kind=big_kind, is_other=is_other,
                                  keywords=",".join(keys), scheme_id=int(src["id"]))
        _LOG.info("%s 年「%s」列方案已写入 %s 个基础列", year, first_name, len(BASE_CATEGORIES))
    # 其余任务的方案若还是空的 → 从市例行复制一份（只复制列定义，任务量另行录入）
    for name, sch in out.items():
        if name == first_name:
            continue
        if store.list_categories(include_disabled=True, scheme_id=int(sch["id"])):
            continue
        store.copy_scheme(int(src["id"]), int(sch["id"]),
                          note=f"由「{first_name}」复制，请按本任务考核表核对列名")
    return out


def scheme_of(year: int, task_type_id: int) -> dict | None:
    """取某任务某年的列方案（不存在则建**空**方案，返回方案行）。"""
    return store.ensure_scheme(int(year), int(task_type_id))


def scheme_copy(year: int, task_type_id: int, from_year: int | None = None,
                from_task_type_id: int | None = None) -> dict:
    """把另一套方案复制过来（默认：本任务的上一年；也可指定另一任务）。

    用途：① 新年份一键沿用去年再改；② 市监督从市例行复制一份再改。
    """
    dst = store.ensure_scheme(int(year), int(task_type_id))
    src = store.get_scheme(int(from_year if from_year is not None else int(year) - 1),
                           int(from_task_type_id if from_task_type_id is not None else task_type_id))
    if src is None:
        raise ValueError(f"源方案不存在：{int(from_year if from_year is not None else int(year) - 1)} 年"
                         f"「{(store.get_task_type(int(from_task_type_id or task_type_id)) or {}).get('name', '')}」")
    if not store.list_categories(include_disabled=True, scheme_id=int(src["id"])):
        raise ValueError("源方案还没有任何列，无法复制")
    result = store.copy_scheme(int(src["id"]), int(dst["id"]))
    _LOG.info("列方案复制：%s 年任务%s ← %s 年任务%s（列 %s 个）", year, task_type_id,
              src["year"], src["task_type_id"], result["categories"])
    return {"year": int(year), "task_type_id": int(task_type_id),
            "from_year": int(src["year"]), "from_task_type_id": int(src["task_type_id"]), **result}


def build_classify_index_for() -> dict[int, dict]:
    """**每个任务一套**归类索引：`{任务 id: 索引}`（列与关键词按任务各自的方案）。"""
    big_kinds = store.list_big_kinds()
    schemes = {int(s["id"]): s for s in store.list_schemes()}
    by_task: defaultdict[int, list[dict]] = defaultdict(list)
    for p in store.list_product_types(include_disabled=True):
        s = schemes.get(int(p.get("scheme_id") or 0))
        if s is None:
            continue
        by_task[int(s["task_type_id"])].append(p)
    return {tid: build_classify_index(ps, big_kinds) for tid, ps in by_task.items()}


def classify_for_task(task_type_id: int, sample_name: object, sample_category: object,
                      indexes: dict[int, dict]) -> tuple[int, str]:
    """按**样品所属任务**的那套列做归类；任务没方案 / 未纳入任何任务 → 不归类 `(0, '')`。"""
    index = indexes.get(int(task_type_id or 0))
    if not index:
        return 0, ""
    return classify_sample(sample_name, sample_category, index)


# ==================== 明细取数（镜像，只读） ====================


def detail_sql(year: int) -> str:
    """当年样品明细 SQL（用 `NVL` 写，direct 模式也能跑；镜像侧翻译层会转 `IFNULL`）。

    列顺序固定（位置数组，**新列只能往后加**）：
    `[0] SID, [1] 样品名, [2] 检测编号, [3] 合同号, [4] 任务名, [5] COUNTY, [6] 单位地址,
     [7] 受检地址, [8] 样品类别名, [9] 业务类别名, [10] 抽样日期, [11] 受理时间`
    """
    year = int(year)
    lo, hi = f"{year}-01-01", f"{year + 1}-01-01"
    return (
        "SELECT s.ID AS SID, s.NAME AS SNAME, s.DETECTION_NO AS DNO, "
        "NVL(d.CONTRACTS_NO, '') AS CNO, NVL(d.TASK_NAME, '') AS TASK, "
        "NVL(dc.COUNTY, '') AS COUNTY, NVL(dc.ADDRESS, '') AS CADDR, "
        "NVL(d.DETECTED_COMPANY_ADDRESS, '') AS DADDR, "
        "NVL(sc.NAME, '') AS CATNAME, NVL(d.BUSINESS_CATEGORY_NAME, '') AS BIZ, "
        "NVL(d.SAMPLING_DATE, '') AS SDATE, NVL(d.ACCEPT_TIME, '') AS ATIME "
        "FROM DT_DETECTION d "
        "LEFT JOIN DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 "
        "LEFT JOIN DT_DETECTED_COMPANY dc ON dc.ID = d.DETECTED_COMPANY_ID "
        "LEFT JOIN DT_SAMPLE_CATEGORY sc ON sc.ID = s.SAMPLE_CATEGORY_ID "
        "WHERE d.IS_DELETED = 0 "
        f"AND ((d.SAMPLING_DATE >= '{lo}' AND d.SAMPLING_DATE < '{hi}') "
        f"OR (d.SAMPLING_DATE IS NULL AND d.ACCEPT_TIME >= '{lo}' AND d.ACCEPT_TIME < '{hi}'))"
    )


# ==================== 区域解析与映射 ====================


def _district_only(parsed: str) -> str:
    """`南京市六合区` → `六合区`。"""
    m = _MUNI_RE.match(parsed)
    return m.group(1) if m else parsed


def clean_region_name(name: object) -> str:
    """区域键合法性清洗：不像区县名就返回空串（源库实测混有 `--`、`nullnull…`、街道门牌）。"""
    s = clean_field(name, 32)
    if not s or len(s) > _REGION_MAX_LEN or _REGION_JUNK_RE.search(s):
        return ""
    return s


def resolve_region_key(county: object, company_address: object, detection_address: object) -> str:
    """解析「区域键」：COUNTY 优先，其次地址解析。返回空串 = 判不出（进未映射清单）。"""
    raw = clean_region_name(county)
    if raw:
        return raw
    for addr in (company_address, detection_address):
        text = str(addr or "")
        if not text:
            continue
        parsed = clean_region_name(_district_only(parse_city_county(text)))
        if parsed:
            return parsed
    return ""


def _region_by_county(county: object, rules: dict) -> str:
    """按受检单位所在区县（`DT_DETECTED_COMPANY.COUNTY`）判定，判不出返回空串。"""
    raw = clean_region_name(county)
    return rules["exact"].get(normalize_text(raw), "") if raw else ""


def _region_by_addr(company_address: object, detection_address: object, rules: dict) -> str:
    """按抽样地址判定：先"地址含区名"，再"地址解析出的区县名"。判不出返回空串。"""
    for addr in (company_address, detection_address):
        text = normalize_text(addr)
        if not text:
            continue
        for key, name in rules["substring"]:
            if key in text:
                return name
    key = resolve_region_key("", company_address, detection_address)
    return rules["exact"].get(normalize_text(key), "") if key else ""


def classify_region(county: object, company_address: object, detection_address: object,
                    rules: dict, priority: str = REGION_PRIORITY_COUNTY) -> tuple[str, str]:
    """样品 → (区域键, 区域名)；区域名为空 = 未映射（进「未映射区域」清单等人工挂载）。

    两个来源都可能出现、也可能互相矛盾（实测 18 条：`COUNTY=张家港市` 而地址在相城区）。
    谁优先由设置决定（默认 `COUNTY`，可在「区域配置」页切到"抽样地址"，切完本地重算即可，不用重连镜像）：

    - `county` 优先：COUNTY 精确命中 → 地址含区名 → 地址解析出的区县名 → 未映射；
    - `address` 优先：地址含区名 → 地址解析出的区县名 → COUNTY 精确命中 → 未映射。
    """
    county_region = _region_by_county(county, rules)
    addr_region = _region_by_addr(company_address, detection_address, rules)
    county_key = clean_region_name(county)

    if priority == REGION_PRIORITY_ADDRESS:
        if addr_region:
            return addr_region, addr_region
        if county_region:
            return county_key, county_region
    else:
        if county_region:
            return county_key, county_region
        if addr_region:
            return addr_region, addr_region
    # 都没命中：返回原始键（用于「未映射区域」清单提示）
    key = resolve_region_key(county, company_address, detection_address)
    return key, ""


def region_priority() -> str:
    """当前区域判定优先项（`county` / `address`）。"""
    v = store.get_meta(META_REGION_PRIORITY, REGION_PRIORITY_COUNTY)
    return REGION_PRIORITY_ADDRESS if v == REGION_PRIORITY_ADDRESS else REGION_PRIORITY_COUNTY


def set_region_priority(value: str) -> str:
    v = REGION_PRIORITY_ADDRESS if str(value) == REGION_PRIORITY_ADDRESS else REGION_PRIORITY_COUNTY
    store.set_meta(META_REGION_PRIORITY, v)
    return v


def _split_keys(raw: object) -> list[str]:
    """把「关键词 / 别名」串拆成多个键：支持 `、` `,` `，` `|` `/` `；` `;` 与换行分隔。"""
    text = str(raw or "")
    for sep in ("、", "，", ",", "|", "/", "；", ";", "\n", "\t"):
        text = text.replace(sep, "\n")
    return [seg.strip() for seg in text.split("\n") if seg.strip()]


def build_region_rules(items: list[dict]) -> dict:
    """区域匹配规则：`exact`（COUNTY/解析值精确匹配）+ `substring`（地址含区名，长的优先）。"""
    exact: dict[str, str] = {}
    sub: dict[str, str] = {}
    for r in items:
        name = str(r["name"])
        for key in [name, *_split_keys(r.get("keys"))]:
            nk = normalize_text(key)
            if nk:
                exact.setdefault(nk, name)
                sub.setdefault(nk, name)
    ordered = sorted(sub.items(), key=lambda kv: len(kv[0]), reverse=True)
    return {"exact": exact, "substring": ordered}


# ==================== 归类 ====================


def _kw_hit(text_norm: str, kw_norm: str) -> bool:
    """关键词是否命中文本：单字关键词**只认全等**（否则「桃」会命中「樱桃番茄」、「牛」会命中「牛蛙」）。"""
    if not text_norm or not kw_norm:
        return False
    if text_norm == kw_norm:
        return True
    return len(kw_norm) >= 2 and kw_norm in text_norm


def build_classify_index(products: list[dict], big_kinds: list[dict]) -> dict:
    """归类索引：特定产品 / 兜底桶 / 大类关键词。"""
    specific: list[tuple[str, int]] = []
    buckets: list[tuple[str, int, str]] = []      # (关键词, pid, 大类)
    for p in products:
        pid = int(p["id"])
        keys = [str(p["name"]), *_split_keys(p.get("keywords"))]
        normed = [(normalize_text(k), normalize_text(strip_brackets(normalize_text(k)))) for k in keys if str(k).strip()]
        if int(p.get("is_other") or 0) or int(p.get("category_is_other") or 0):
            big = str(p.get("big_kind") or "")
            for nk, lk in normed:
                buckets.append((nk, pid, big))
                if lk and lk != nk:
                    buckets.append((lk, pid, big))
        else:
            for nk, lk in normed:
                specific.append((nk, pid))
                if lk and lk != nk:
                    specific.append((lk, pid))
    big_list: list[tuple[str, str]] = []
    for bk in big_kinds:
        for key in _split_keys(bk.get("keywords")):
            nk = normalize_text(key)
            if nk:
                big_list.append((nk, str(bk["name"])))
    return {"specific": specific, "buckets": buckets, "big_kinds": big_list}


def _best(texts: list[str], pairs: list[tuple[str, int]]) -> tuple[int, str] | None:
    """在 `texts`（样品名/类别名）里找命中最长的关键词；返回 (pid, 命中的关键词)。"""
    best: tuple[int, str] | None = None
    for text in texts:
        if not text:
            continue
        for kw, pid in pairs:
            if _kw_hit(text, kw) and (best is None or len(kw) > len(best[1])):
                best = (pid, kw)
    return best


def classify_sample(sample_name: object, sample_category: object, index: dict) -> tuple[int, str]:
    """样品 → (产品类型 id, 命中层级)。未命中返回 `(0, '')`。"""
    raw_name = normalize_text(sample_name)
    loose_name = strip_brackets(raw_name)
    raw_cat = normalize_text(sample_category)
    loose_cat = strip_brackets(raw_cat)

    hit = _best([raw_name], index["specific"])
    if hit:
        return hit[0], LAYER_EXACT
    hit = _best([loose_name], index["specific"])
    if hit:
        return hit[0], LAYER_LOOSE
    hit = _best([raw_cat, loose_cat], index["specific"])
    if hit:
        return hit[0], LAYER_CATEGORY

    bucket_pairs = [(kw, pid) for kw, pid, _big in index["buckets"]]
    hit = _best([raw_name, loose_name], bucket_pairs)
    if hit:
        return hit[0], LAYER_BUCKET
    hit = _best([raw_cat, loose_cat], bucket_pairs)
    if hit:
        return hit[0], LAYER_BUCKET

    # 最后一层：先判大类，再落到该大类的兜底桶（如类别就是「水产品」这类笼统写法）
    big = None
    for kw, name in index["big_kinds"]:
        if _kw_hit(raw_cat, kw) or _kw_hit(loose_cat, kw):
            big = name
            break
    if big is None:
        for kw, name in index["big_kinds"]:
            if _kw_hit(raw_name, kw) or _kw_hit(loose_name, kw):
                big = name
                break
    if big:
        for _kw, pid, bk in index["buckets"]:
            if bk == big:
                return pid, LAYER_BIG_KIND
    return 0, ""


# ==================== 同步与重算 ====================


def _build_rows(raw_rows: list, indexes: dict[int, dict], task_map: dict[str, int],
                regions: list[dict], ignore: set[str],
                priority: str = REGION_PRIORITY_COUNTY) -> dict:
    """把镜像明细投影成待落库的样品行（纯函数，便于单测）。

    `indexes` = 每个任务一套归类索引（列与关键词按任务各自的方案）；
    `task_map` = 合同 → 任务；合同没有归属 → 样品 `task_type_id=0`（进「未纳入清单」，不归类）。
    """
    region_rules = build_region_rules(regions)
    rows: list[dict] = []
    seen: set[object] = set()
    stats = Counter()
    unmatched = Counter()
    unmapped = Counter()
    ignored = Counter()
    contracts: set[str] = set()

    for r in raw_rows:
        sid = r[0]
        if sid in (None, "") or sid in seen:
            stats["dup"] += 1
            continue
        seen.add(sid)
        name = clean_field(r[1], store.MAX_NAME)
        if name in ignore:
            ignored[name] += 1
            stats["ignored"] += 1
            continue
        cno = contract_key(r[3])
        tid = int(task_map.get(cno, 0)) if cno else 0
        raw_county = clean_field(r[5], 64)
        raw_addr = clean_field(r[6], 300)
        raw_addr2 = clean_field(r[7], 300)
        region_key, region = classify_region(r[5], r[6], r[7], region_rules, priority)
        pid, layer = classify_for_task(tid, name, r[8] if len(r) > 8 else "", indexes)
        if not tid:
            stats["no_task"] += 1
        elif not pid:
            # 只对"要进表"的样品报未归类：未纳入任务的样品本来就不进任何列
            unmatched[name or "(空样品名)"] += 1
            stats["unmatched"] += 1
        if not region:
            unmapped[region_key or "(空地址)"] += 1
            stats["unmapped"] += 1
        if cno:
            contracts.add(cno)
        else:
            stats["no_contract"] += 1
        rows.append({
            "sample_id": int(sid), "detection_no": clean_field(r[2], 64), "contract_no": cno,
            "biz": clean_field(r[9] if len(r) > 9 else "", 64),
            "task_name": clean_field(r[4], 200), "task_type_id": tid,
            "region_key": region_key, "region": region, "sample_name": name,
            "sample_category": clean_field(r[8] if len(r) > 8 else "", 64),
            "sample_date": clean_field((r[10] if len(r) > 10 else "") or (r[11] if len(r) > 11 else ""), 16),
            "product_type_id": pid, "match_layer": layer,
            "county": raw_county, "addr": raw_addr, "addr2": raw_addr2,
        })
    return {
        "rows": rows, "stats": dict(stats), "contracts": sorted(contracts),
        "unmatched": unmatched.most_common(UNMATCHED_TOP_LIMIT), "unmapped": unmapped.most_common(200),
    }


def _persist(year: int, built: dict, synced_at: str) -> None:
    """落库（在线程里跑：sqlite3 是阻塞 IO）。"""
    store.upsert_auto_contracts(built["contracts"])
    # 同步时**不自动新增区域**：看板行由区域配置决定，解析出来的新键进「未映射区域」等人工确认，
    # 否则省外/省内的杂地址会被自动登记成一行行垃圾区域。
    store.replace_done_samples(year, built["rows"], synced_at)
    store.set_meta(META_SYNC_SUMMARY.format(year=year), json.dumps({
        "stats": built["stats"], "unmatched": built["unmatched"], "unmapped": built["unmapped"],
    }, ensure_ascii=False))


async def sync_year(year: int) -> dict:
    """按年抓取完成量明细。成功与失败都写同步日志；失败时**旧明细保持不动**。"""
    year = int(year)
    bootstrap()
    started = datetime.now()
    t0 = time.perf_counter()
    deadline = mirror_store.data_deadline() or ""
    base = {"year": year, "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "mirror", "data_deadline": deadline}
    try:
        res = await db.adm(detail_sql(year))
        raw = res.get("rows") or []
        indexes = build_classify_index_for()
        task_map = contract_task_map()
        regions = store.list_regions()
        ignore = {str(x["name"]) for x in store.list_ignore_names()}
        built = await asyncio.to_thread(_build_rows, raw, indexes, task_map, regions, ignore,
                                        region_priority())
        synced_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await asyncio.to_thread(_persist, year, built, synced_at)
    except Exception as exc:  # noqa: BLE001  失败也要留痕（页面/日志可查）
        elapsed = int((time.perf_counter() - t0) * 1000)
        _LOG.warning("完成量同步失败 year=%s：%s", year, exc)
        log_id = await asyncio.to_thread(store.write_sync_log, {
            **base, "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_ms": elapsed, "status": "failed", "error": str(exc)[:300]})
        return {"success": False, "year": year, "log_id": log_id, "elapsed_ms": elapsed,
                "message": f"完成量同步失败（旧数据保留）：{exc}"}

    st = built["stats"]
    elapsed = int((time.perf_counter() - t0) * 1000)
    log_id = await asyncio.to_thread(store.write_sync_log, {
        **base, "finished_at": synced_at, "rows": len(built["rows"]),
        "done_total": len(built["rows"]), "contract_count": len(built["contracts"]),
        "unmatched": st.get("unmatched", 0), "unclassified": st.get("unmapped", 0),
        "no_contract": st.get("no_contract", 0), "ignored": st.get("ignored", 0),
        "elapsed_ms": elapsed, "status": "ok", "error": ""})
    _LOG.info("完成量同步完成 year=%s 样品=%s 合同=%s 未归类=%s 未纳入=%s 未映射区域=%s 忽略=%s 用时=%sms",
              year, len(built["rows"]), len(built["contracts"]), st.get("unmatched", 0),
              st.get("no_task", 0), st.get("unmapped", 0), st.get("ignored", 0), elapsed)
    return {"success": True, "year": year, "log_id": log_id, "elapsed_ms": elapsed,
            "data_deadline": deadline, "synced_at": synced_at, "rows": len(built["rows"]),
            "contract_count": len(built["contracts"]), **st,
            "message": (f"完成量同步完成：{len(built['rows'])} 个样品、{len(built['contracts'])} 个合同、"
                        f"{st.get('unmatched', 0)} 个未归类、{st.get('no_task', 0)} 个未纳入考核表，"
                        f"用时 {elapsed / 1000:.1f} 秒")}


def reclassify(year: int) -> dict:
    """按**最新配置**重算归类与区域映射（不重连镜像，秒级）。改关键词/别名后调用。"""
    year = int(year)
    bootstrap()
    rows = store.read_done_samples(year)
    if not rows:
        return {"success": True, "year": year, "updated": 0, "unmatched": 0, "unmapped": 0}
    indexes = build_classify_index_for()
    task_map = contract_task_map()
    regions = store.list_regions()
    region_rules = build_region_rules(regions)
    priority = region_priority()
    updated = unmatched = unmapped = 0
    for r in rows:
        cno = str(r["contract_no"] or "")
        tid = int(task_map.get(cno, 0)) if cno else 0
        pid, layer = classify_for_task(tid, r["sample_name"], r["sample_category"], indexes)
        # 区域用库存的原始字段重算（COUNTY + 两个地址都在库里），因此切换"区域判定优先"后
        # 只要重算即可生效，不必重新连镜像。
        region_key, region = classify_region(r["county"], r["addr"], r["addr2"], region_rules, priority)
        if tid and not pid:
            unmatched += 1
        if not region:
            unmapped += 1
        if (int(r.get("task_type_id") or 0) != tid or int(r["product_type_id"]) != pid
                or str(r["region"]) != region or str(r["region_key"]) != region_key
                or str(r["match_layer"]) != layer):
            store.update_done_classification(year, [int(r["sample_id"])],
                                             product_type_id=pid, region=region,
                                             region_key=region_key, match_layer=layer,
                                             task_type_id=tid)
            updated += 1
    _LOG.info("重算归类 year=%s 更新=%s 未归类=%s 未映射=%s", year, updated, unmatched, unmapped)
    return {"success": True, "year": year, "updated": updated, "unmatched": unmatched, "unmapped": unmapped}


def reclassify_all() -> dict:
    """所有有明细的年份都重算归类（改「合同→任务」归属、区域别名这类跨年改动用它）。"""
    years = store.done_years()
    total = 0
    detail = []
    for y in years:
        r = reclassify(int(y))
        total += int(r.get("updated") or 0)
        detail.append({"year": int(y), "updated": int(r.get("updated") or 0),
                       "unmatched": int(r.get("unmatched") or 0), "unmapped": int(r.get("unmapped") or 0)})
    return {"success": True, "years": [int(y) for y in years], "updated": total, "detail": detail}


# ==================== 统计组装（只读本地 SQLite，毫秒级） ====================


def contract_alias_map() -> dict[str, str]:
    """合同归并表 `{变体编号: 正式编号}`（源库存在 `ps2026002-市例性` 这类拼写漂移）。"""
    return {str(c["contract_no"]): str(c["alias_of"])
            for c in store.list_contracts(include_disabled=True)
            if str(c.get("alias_of") or "")}


def _canonical_contracts(rows: list[dict], alias_map: dict[str, str]) -> list[dict]:
    """把行上的合同编号折算成正式编号（返回新行，不改调用方数据）。"""
    if not alias_map:
        return rows
    out = []
    for r in rows:
        cno = str(r.get("contract_no") or "")
        out.append({**r, "contract_no": alias_map[cno]} if cno in alias_map else r)
    return out


def contract_rows_merged(year: int) -> list[dict]:
    """合同清单（已归并）：变体的任务量/完成量并入正式合同，`merged` 列出被并入的编号。

    筛选器与看板用这份清单；「合同管理」页用 `store.list_contracts` 的原始清单（要能改归并关系）。
    """
    out: dict[str, dict] = {}
    for c in store.list_contracts(year=year, include_disabled=True):
        no = str(c["contract_no"])
        tgt = str(c.get("alias_of") or "")
        key = tgt or no
        item = out.get(key)
        if item is None:
            item = {"contract_no": key, "name": str(c.get("name") or ""), "note": str(c.get("note") or ""),
                    "enabled": bool(c["enabled"]), "source": str(c["source"]),
                    "quota_rows": 0, "quota_total": 0, "done_total": 0, "merged": [],
                    "task_type_id": int(c.get("task_type_id") or 0)}
            out[key] = item
        # 任务归并以**正式编号**那份为准（变体自己填的任务归属不参与判断）
        if not tgt:
            item["task_type_id"] = int(c.get("task_type_id") or 0)
        elif not int(item.get("task_type_id") or 0):
            item["task_type_id"] = int(c.get("task_type_id") or 0)
        item["quota_rows"] += int(c.get("quota_rows") or 0)
        item["quota_total"] += int(c.get("quota_total") or 0)
        item["done_total"] += int(c.get("done_total") or 0)
        if tgt:
            item["merged"].append(no)
            item["enabled"] = item["enabled"] or bool(c["enabled"])
    return list(out.values())


def _rate(done: int, quota: int) -> float | None:
    if quota <= 0:
        return None
    return round(done / quota * 100, 1)


def _scope_filter(rows: list[dict], contracts: list[str] | None,
                  bizs: list[str] | None, cutoff: str) -> list[dict]:
    """按统计范围筛选明细。三个参数都为空 = 全选/全年。"""
    cs = contract_filter_set(contracts)
    bs = {str(b).strip() for b in (bizs or []) if str(b).strip()}
    cut = clean_field(cutoff, 16)
    out = []
    for r in rows:
        if cs and str(r["contract_no"]) not in cs:
            continue
        if bs and str(r["biz"]) not in bs:
            continue
        if cut and str(r["sample_date"]) and str(r["sample_date"])[:10] > cut:
            continue
        out.append(r)
    return out


def _sync_summary(year: int) -> dict:
    raw = store.get_meta(META_SYNC_SUMMARY.format(year=year), "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def build_overview(year: int, task_type_id: int | None = None,
                   contracts: list[str] | None = None,
                   bizs: list[str] | None = None, cutoff: str = "") -> dict:
    """看板主接口：一次组装卡片 + 表头矩阵 + 图表 + 提示。

    ⚠️ 任务量按**任务**下达，所以「合同」筛选只作用于完成量（用于核查某份合同的样品），
    此时 `summary.contract_filtered=True`，页面会提示"完成率不随合同筛选变化"。
    """
    year = int(year)
    bootstrap()
    task = resolve_task_type(task_type_id)
    tid = int(task["id"])
    scheme = store.ensure_scheme(year, tid)
    scheme_id = int(scheme["id"])
    categories = store.list_categories(include_disabled=True, year=year, scheme_id=scheme_id)
    products = store.list_product_types(include_disabled=True, year=year, scheme_id=scheme_id)
    regions = store.list_regions(include_disabled=True, year=year)
    contracts_cfg = contract_rows_merged(year)
    quota_rows = store.read_quotas(year, tid)
    alias_map = contract_alias_map()
    # 合同筛选值也要先折算成正式编号：明细里已经归并过（ps2026002-市例性 → ps2026002-市例行），
    # 若按变体编号去筛会一条都命中不到（页面下拉给的是正式编号，手改 URL 时才会遇到）。
    if contracts:
        contracts = [c if str(c).strip() == UNASSIGNED_SENTINEL
                     else alias_map.get(contract_key(c), contract_key(c)) for c in contracts]
        contracts = [c for c in contracts if str(c).strip()] or None
    ignore = {str(x["name"]) for x in store.list_ignore_names()}
    # 归并合同：把拼写变体（如 ps2026002-市例性）先折算成正式编号，再做筛选与统计
    all_done = _canonical_contracts(store.read_done_samples(year), alias_map)
    done_rows = _scope_filter([r for r in all_done if int(r.get("task_type_id") or 0) == tid],
                              contracts, bizs, cutoff)
    # 「未纳入清单」：合同没归到任何任务的样品（看板不统计，但要让使用方看见有多少）
    no_task_rows = [r for r in all_done
                    if not int(r.get("task_type_id") or 0) and str(r["sample_name"]) not in ignore]
    other_task_rows = [r for r in all_done
                       if int(r.get("task_type_id") or 0) not in (0, tid)
                       and str(r["sample_name"]) not in ignore]

    region_names = [str(r["name"]) for r in regions if int(r["enabled"])]
    region_set = set(region_names)

    # ---- 列：启用的品类（按 sort_no）→ 其下启用的产品类型 ----
    flat_products: list[dict] = []
    cat_cols: list[dict] = []
    for c in categories:
        cid = int(c["id"])
        subs = [p for p in products if int(p["category_id"]) == cid and int(p["enabled"])]
        if not int(c["enabled"]) or not subs:
            continue
        refs = []
        for p in subs:
            pid = int(p["id"])
            refs.append(pid)
            flat_products.append({"id": pid, "name": str(p["name"]), "category_id": cid,
                                  "is_other": bool(int(p.get("is_other") or 0))})
        cat_cols.append({"id": cid, "name": str(c["name"]), "big_kind": str(c["big_kind"] or ""),
                         "is_other": bool(int(c["is_other"] or 0)), "product_ids": refs})

    # ---- 统计范围（口径统一：只统计"区域已映射 + 品类已归类"的样品） ----
    quota_map: defaultdict[tuple[str, int], int] = defaultdict(int)
    # 品类级任务量（使用方的考核表按品类下达：product_type_id=0 + category_id）
    cat_quota: defaultdict[tuple[str, int], int] = defaultdict(int)
    cs = contract_filter_set(contracts)
    for q in quota_rows:
        # 任务量按任务下达，不随「合同筛选」变化（否则筛单份合同时完成率会失真）
        if str(q["region"]) not in region_set:
            continue
        pid = int(q["product_type_id"])
        cid = int(q.get("category_id") or 0)
        if pid:
            quota_map[(str(q["region"]), pid)] += int(q["quota"] or 0)
        elif cid:
            cat_quota[(str(q["region"]), cid)] += int(q["quota"] or 0)

    done_map: defaultdict[tuple[str, int], int] = defaultdict(int)
    unmatched: Counter = Counter()
    unmapped: Counter = Counter()
    ignored: Counter = Counter()
    for r in done_rows:
        rg = str(r["region"])
        pid = int(r["product_type_id"])
        if str(r["sample_name"]) in ignore:
            ignored[str(r["sample_name"])] += 1
            continue
        if not rg:
            unmapped[str(r["region_key"]) or "(空地址)"] += 1
            continue
        if rg not in region_set:
            unmapped[f"{rg}(区域未启用)"] += 1
            continue
        if not pid:
            unmatched[str(r["sample_name"]) or "(空样品名)"] += 1
            continue
        done_map[(rg, pid)] += 1

    # ---- 矩阵 ----
    rows: list[dict] = []
    col_quota = {p["id"]: 0 for p in flat_products}
    col_done = {p["id"]: 0 for p in flat_products}
    for name in region_names:
        cells = []
        for p in flat_products:
            pid = int(p["id"])
            q = quota_map.get((name, pid), 0)
            d = done_map.get((name, pid), 0)
            col_quota[pid] += q
            col_done[pid] += d
            cells.append({"product_type_id": pid, "quota": q, "done": d, "rate": _rate(d, q)})
        # 品类级格子：**本区域**该品类下各产品之和 + 本区域的品类级下达量。
        # 前端"品类视图"直接用它渲染，不能再拿 category_totals（那是全区域合计，
        # 会让每一行显示同一个数——这是实测发现并修掉的 bug）。
        ccells = []
        for c in cat_cols:
            cq = sum(quota_map.get((name, pid), 0) for pid in c["product_ids"]) + cat_quota.get((name, c["id"]), 0)
            cd = sum(done_map.get((name, pid), 0) for pid in c["product_ids"])
            ccells.append({"category_id": c["id"], "quota": cq, "done": cd, "rate": _rate(cd, cq)})
        q_sum = sum(c["quota"] for c in cells) + sum(cat_quota.get((name, c["id"]), 0) for c in cat_cols)
        d_sum = sum(c["done"] for c in cells)
        rows.append({"region": name, "cells": cells, "category_cells": ccells, "quota": q_sum, "done": d_sum,
                     "rate": _rate(d_sum, q_sum)})

    cat_level = {c["id"]: sum(cat_quota.get((nm, c["id"]), 0) for nm in region_names) for c in cat_cols}
    cat_totals = [
        {"category_id": c["id"],
         "quota": sum(col_quota[pid] for pid in c["product_ids"]) + cat_level[c["id"]],
         "done": sum(col_done[pid] for pid in c["product_ids"])}
        for c in cat_cols
    ]
    for ct in cat_totals:
        ct["rate"] = _rate(ct["done"], ct["quota"])
    grand_quota = sum(col_quota.values()) + sum(cat_level.values())
    grand_done = sum(col_done.values())

    summary = {
        "task_type_id": tid, "task_name": str(task["name"]),
        "quota_total": grand_quota, "done_total": grand_done, "rate": _rate(grand_done, grand_quota),
        "region_count": len(region_names), "category_count": len(cat_cols),
        "product_count": len(flat_products),
        "done_unmatched": sum(unmatched.values()), "done_unmapped_region": sum(unmapped.values()),
        "done_ignored": sum(ignored.values()),
        "done_in_scope": len(done_rows),
        # 合同筛选只影响完成量（任务量按任务下达），页面据此提示"完成率不随筛选变化"
        "contract_filtered": bool(cs),
        # 未纳入任何考核表的样品数（看板不统计，只提示）
        "done_no_task": len(no_task_rows),
        "done_other_task": len(other_task_rows),
        # 该年该任务的列还是空的（新年份需要"复制上一年的方案"）
        "scheme_empty": not cat_cols,
        "contract_count": len(cs) if cs else len(
            [c for c in contracts_cfg if int(c.get("task_type_id") or 0) == tid and int(c["enabled"])]),
    }

    # ---- 图表 ----
    chart_rows = [r for r in rows if r["quota"] > 0 or r["done"] > 0]
    chart_rows.sort(key=lambda r: ((r["rate"] if r["rate"] is not None else 0), r["quota"]), reverse=True)
    region_chart = {
        "labels": [r["region"] for r in chart_rows],
        "rate": [r["rate"] for r in chart_rows],
        "done": [r["done"] for r in chart_rows],
        "quota": [r["quota"] for r in chart_rows],
    }
    category_chart = {
        "labels": [c["name"] for c in cat_cols],
        "quota": [ct["quota"] for ct, c in zip(cat_totals, cat_cols)],
        "done": [ct["done"] for ct, c in zip(cat_totals, cat_cols)],
    }
    product_pairs = sorted(
        ((int(p["id"]), col_done[int(p["id"])], col_quota[int(p["id"])]) for p in flat_products),
        key=lambda t: (t[1], t[2]), reverse=True)
    name_of = {int(p["id"]): p["name"] for p in flat_products}
    product_chart = {
        "labels": [name_of[pid] for pid, _d, _q in product_pairs if _d or _q],
        "done": [_d for _pid, _d, _q in product_pairs if _d or _q],
        "quota": [_q for _pid, _d, _q in product_pairs if _d or _q],
    }

    scope_stats = _sync_summary(year).get("stats") or {}
    last_ok = store.last_sync(year=year, only_ok=True)
    last_any = store.last_sync(year=year)
    return {
        "year": year,
        "task": task,
        "tasks": task_type_list(include_disabled=True),
        "scheme": {"id": scheme_id, "year": year, "task_type_id": tid,
                   "note": str(scheme.get("note") or ""), "category_count": len(cat_cols)},
        "schemes": [
            {"id": int(s["id"]), "year": int(s["year"]), "task_type_id": int(s["task_type_id"]),
             "task_name": str(s.get("task_name") or ""), "note": str(s.get("note") or ""),
             "category_count": int(s.get("category_count") or 0),
             "category_enabled": int(s.get("category_enabled") or 0),
             "quota_total": int(s.get("quota_total") or 0), "done_total": int(s.get("done_total") or 0)}
            for s in store.list_schemes(year=year)
        ],
        "filters": {"contracts": sorted(cs), "bizs": sorted({str(b) for b in (bizs or []) if str(b).strip()}),
                    "cutoff": clean_field(cutoff, 16)},
        "categories": cat_cols,
        "products": flat_products,
        "rows": rows,
        "category_totals": cat_totals,
        "product_totals": [{"product_type_id": p["id"], "quota": col_quota[p["id"]],
                            "done": col_done[p["id"]], "rate": _rate(col_done[p["id"]], col_quota[p["id"]])}
                           for p in flat_products],
        "grand_total": {"quota": grand_quota, "done": grand_done, "rate": _rate(grand_done, grand_quota)},
        "summary": summary,
        "charts": {"region_rate": region_chart, "category": category_chart, "product": product_chart},
        "unmatched": [{"name": n, "count": c} for n, c in unmatched.most_common(200)],
        "unmapped_regions": [{"region_key": k, "count": c} for k, c in unmapped.most_common(100)],
        "ignored": [{"name": n, "count": c} for n, c in ignored.most_common(100)],
        # 未纳入清单：合同没归到任何任务的样品（其它任务的样品不计入本表，也不列在这里）
        "no_task": {
            "total": len(no_task_rows),
            "names": [{"name": n, "count": c} for n, c in Counter(
                str(r["sample_name"]) or "(空样品名)" for r in no_task_rows).most_common(100)],
            "contracts": [{"contract_no": n, "count": c} for n, c in Counter(
                str(r["contract_no"]) or "(无合同)" for r in no_task_rows).most_common(50)],
        },
        "contracts": [
            {"contract_no": str(c["contract_no"]), "name": str(c["name"] or ""), "note": str(c["note"] or ""),
             "enabled": bool(c["enabled"]), "source": str(c["source"]),
             "quota_rows": int(c.get("quota_rows") or 0), "quota_total": int(c.get("quota_total") or 0),
             "done_total": int(c.get("done_total") or 0), "merged": list(c.get("merged") or [])}
            for c in contracts_cfg
        ],
        "regions": [
            {"name": str(r["name"]), "keys": str(r["keys"] or ""), "enabled": bool(r["enabled"]),
             "source": str(r["source"]), "sort_no": int(r["sort_no"]),
             "quota_total": int(r.get("quota_total") or 0), "done_total": int(r.get("done_total") or 0)}
            for r in regions
        ],
        "biz_options": sorted({str(r["biz"]) for r in store.read_done_samples(year) if str(r["biz"])}),
        "sync": {
            "data_deadline": mirror_store.data_deadline(),
            "synced_at": (last_ok or {}).get("finished_at") or "",
            "last_status": (last_any or {}).get("status") or "",
            "last_error": (last_any or {}).get("error") if (last_any or {}).get("status") == "failed" else "",
            "rows": int((last_ok or {}).get("rows") or 0),
            "unmatched": int((last_ok or {}).get("unmatched") or 0),
            "unmapped": int((last_ok or {}).get("unclassified") or 0),
            "ignored": int((last_ok or {}).get("ignored") or 0),
            "no_contract": int((last_ok or {}).get("no_contract") or 0),
            "raw_stats": scope_stats,
        },
    }


def _region_conflicts(year: int) -> dict:
    """COUNTY 判定与地址判定不一致的样品汇总（供「区域配置」页提示，可据此决定优先项）。

    返回 `{total, items:[{county_region, addr_region, count}]}`；只统计两者都判得出、且不同的。
    """
    rules = build_region_rules(store.list_regions(include_disabled=True))
    counter: Counter = Counter()
    for r in store.read_done_samples(year):
        c = _region_by_county(r.get("county"), rules)
        a = _region_by_addr(r.get("addr"), r.get("addr2"), rules)
        if c and a and c != a:
            counter[(c, a)] += 1
    items = [{"county_region": k[0], "addr_region": k[1], "count": v}
             for k, v in counter.most_common()]
    return {"total": sum(counter.values()), "items": items}


def config_panel(year: int, task_type_id: int | None = None) -> dict:
    """配置页数据：**当前任务**的表头列（品类 + 产品类型）/ 任务与各年方案 / 区域 / 合同 /
    忽略名单 / 未归类（本任务）与未映射区域清单 / 未纳入清单。"""
    year = int(year)
    bootstrap()
    task = resolve_task_type(task_type_id)
    tid = int(task["id"])
    scheme = store.ensure_scheme(year, tid)
    scheme_id = int(scheme["id"])
    products = store.list_product_types(include_disabled=True, year=year, scheme_id=scheme_id)
    alias_map = contract_alias_map()
    done_rows = _canonical_contracts(store.read_done_samples(year), alias_map)
    unmatched_counter: Counter = Counter()
    unmapped_counter: Counter = Counter()
    no_task_counter: Counter = Counter()
    name_cat: dict[str, Counter] = defaultdict(Counter)
    for r in done_rows:
        row_task = int(r.get("task_type_id") or 0)
        if not row_task:
            no_task_counter[str(r["sample_name"]) or "(空样品名)"] += 1
            continue
        if row_task != tid:
            continue
        if not int(r["product_type_id"]):
            unmatched_counter[str(r["sample_name"]) or "(空样品名)"] += 1
            name_cat[str(r["sample_name"])][str(r["sample_category"])] += 1
        if not str(r["region"]):
            unmapped_counter[str(r["region_key"]) or "(空地址)"] += 1
    ignore = store.list_ignore_names()
    task_names = {int(t["id"]): str(t["name"]) for t in store.list_task_types(include_disabled=True)}
    return {
        "year": year,
        "task": task,
        "tasks": task_type_list(include_disabled=True),
        "scheme": {"id": scheme_id, "year": year, "task_type_id": tid,
                   "note": str(scheme.get("note") or "")},
        "schemes": [
            {"id": int(s["id"]), "year": int(s["year"]), "task_type_id": int(s["task_type_id"]),
             "task_name": str(s.get("task_name") or ""), "note": str(s.get("note") or ""),
             "category_count": int(s.get("category_count") or 0),
             "category_enabled": int(s.get("category_enabled") or 0),
             "quota_total": int(s.get("quota_total") or 0), "done_total": int(s.get("done_total") or 0)}
            for s in store.list_schemes(year=year)
        ],
        "big_kinds": [{"name": str(b["name"]), "keywords": str(b["keywords"] or ""),
                       "sort_no": int(b["sort_no"]), "enabled": bool(b["enabled"])}
                      for b in store.list_big_kinds()],
        "categories": [
            {"id": int(c["id"]), "name": str(c["name"]), "big_kind": str(c["big_kind"] or ""),
             "is_other": bool(int(c["is_other"] or 0)), "sort_no": int(c["sort_no"]),
             "enabled": bool(c["enabled"]), "quota_total": int(c.get("quota_total") or 0),
             "done_total": int(c.get("done_total") or 0),
             "keywords": str(c.get("keywords") or ""),
             "key_count": len(_split_keys(c.get("keywords")))}
            for c in store.list_categories(include_disabled=True, year=year, scheme_id=scheme_id)
        ],
        "product_types": [
            {"id": int(p["id"]), "name": str(p["name"]), "category_id": int(p["category_id"]),
             "category_name": str(p.get("category_name") or ""), "big_kind": str(p.get("big_kind") or ""),
             "keywords": str(p.get("keywords") or ""), "is_other": bool(int(p.get("is_other") or 0)),
             "sort_no": int(p["sort_no"]), "enabled": bool(p["enabled"]),
             "key_count": len(_split_keys(p.get("keywords"))), "quota_total": int(p.get("quota_total") or 0),
             "done_total": int(p.get("done_total") or 0)}
            for p in products
        ],
        "regions": [
            {"name": str(r["name"]), "keys": str(r["keys"] or ""), "enabled": bool(r["enabled"]),
             "source": str(r["source"]), "sort_no": int(r["sort_no"]),
             "quota_total": int(r.get("quota_total") or 0), "done_total": int(r.get("done_total") or 0)}
            for r in store.list_regions(include_disabled=True, year=year)
        ],
        "contracts": [
            {"contract_no": str(c["contract_no"]), "name": str(c["name"] or ""), "note": str(c["note"] or ""),
             "enabled": bool(c["enabled"]), "source": str(c["source"]),
             "quota_rows": int(c.get("quota_rows") or 0), "quota_total": int(c.get("quota_total") or 0),
             "done_total": int(c.get("done_total") or 0), "alias_of": str(c.get("alias_of") or ""),
             "task_type_id": int(c.get("task_type_id") or 0),
             "task_name": str(task_names.get(int(c.get("task_type_id") or 0)) or "")}
            for c in store.list_contracts(year=year, include_disabled=True)
        ],
        "region_priority": region_priority(),
        "region_conflicts": _region_conflicts(year),
        "ignore_names": [{"name": str(x["name"]), "note": str(x["note"] or "")} for x in ignore],
        "unmatched": [
            {"name": n, "count": c,
             "category": (name_cat[n].most_common(1)[0][0] if name_cat.get(n) else "")}
            for n, c in unmatched_counter.most_common(300)
        ],
        "unmapped_regions": [{"region_key": k, "count": c} for k, c in unmapped_counter.most_common(100)],
        # 未纳入清单：合同没归到任何任务的样品（在「合同」页把它们挂到任务即可）
        "no_task": [{"name": n, "count": c} for n, c in no_task_counter.most_common(100)],
        "sync": {
            "data_deadline": mirror_store.data_deadline(),
            "synced_at": (store.last_sync(year=year, only_ok=True) or {}).get("finished_at") or "",
            "last_error": (store.last_sync(year=year) or {}).get("error") or "",
        },
    }


def quota_panel(year: int, task_type_id: int | None = None) -> dict:
    """任务量录入页：区域行 + 列方案的表头列（品类 → 产品类型）+ 已填任务量与完成量参考。

    任务量按**任务**下达（一张考核表一份），因此同任务下的多份合同共用这一份录入。
    """
    year = int(year)
    bootstrap()
    task = resolve_task_type(task_type_id)
    tid = int(task["id"])
    scheme = store.ensure_scheme(year, tid)
    scheme_id = int(scheme["id"])
    regions = [r for r in store.list_regions(include_disabled=True, year=year) if int(r["enabled"])]
    quotas: dict[tuple[str, int], int] = {}
    cat_quotas: dict[tuple[str, int], int] = {}
    for r in store.read_quotas(year, tid):
        key = (str(r["region"]), int(r["product_type_id"]))
        cid = int(r.get("category_id") or 0)
        if int(r["product_type_id"]):
            quotas[key] = int(r["quota"])
        elif cid:
            cat_quotas[(str(r["region"]), cid)] = int(r["quota"])
    dones: defaultdict[tuple[str, int], int] = defaultdict(int)
    for r in store.read_done_samples(year):
        if int(r.get("task_type_id") or 0) == tid and str(r["region"]):
            dones[(str(r["region"]), int(r["product_type_id"]))] += 1
    cats = [c for c in store.list_categories(include_disabled=True, year=year, scheme_id=scheme_id)
            if int(c["enabled"])]
    products = [p for p in store.list_product_types(include_disabled=True, year=year, scheme_id=scheme_id)
                if int(p["enabled"])]
    columns = [
        {"category_id": int(c["id"]), "category": str(c["name"]), "big_kind": str(c["big_kind"] or ""),
         "products": [{"id": int(p["id"]), "name": str(p["name"]), "is_other": bool(int(p.get("is_other") or 0))}
                      for p in products if int(p["category_id"]) == int(c["id"])]}
        for c in cats
    ]
    columns = [c for c in columns if c["products"]]
    rows = [{"region": str(r["name"])} for r in regions]
    return {
        "year": year, "task": task, "tasks": task_type_list(include_disabled=True),
        "scheme": {"id": scheme_id, "task_type_id": tid, "note": str(scheme.get("note") or "")},
        "columns": columns,
        "rows": rows,
        "values": [
            {"region": str(r["name"]), "product_type_id": int(p["id"]),
             "quota": quotas.get((str(r["name"]), int(p["id"])), 0),
             "done": int(dones.get((str(r["name"]), int(p["id"])), 0))}
            for r in regions for p in products
        ],
        # 品类级的完成量 = 该品类下所有产品类型的样品数之和（按品类录入时用它做参考）
        "category_values": [
            {"region": str(r["name"]), "category_id": int(c["category_id"]),
             "quota": cat_quotas.get((str(r["name"]), int(c["category_id"])), 0),
             "done": sum(int(dones.get((str(r["name"]), int(p["id"])), 0)) for p in c["products"])}
            for r in regions for c in columns
        ],
        "meta": {
            "data_deadline": mirror_store.data_deadline(),
            "synced_at": (store.last_sync(year=year, only_ok=True) or {}).get("finished_at") or "",
        },
    }


def quota_matrix(year: int, task_type_id: int | None = None, contracts: list[str] | None = None,
                 bizs: list[str] | None = None, cutoff: str = "") -> dict:
    """导出用：区域 × （品类/产品类型）的任务量、完成量、完成率（含合计行）。"""
    data = build_overview(year, task_type_id=task_type_id, contracts=contracts, bizs=bizs, cutoff=cutoff)
    return {"year": int(year), "task": data["task"], "scheme": data["scheme"],
            "filters": data["filters"], "summary": data["summary"],
            "categories": data["categories"], "products": data["products"],
            "rows": data["rows"], "category_totals": data["category_totals"],
            "grand_total": data["grand_total"]}


def available_years() -> list[int]:
    """可选年份：当前年 + 库内已有明细的年份。"""
    this_year = datetime.now().year
    years = {this_year, this_year - 1, this_year - 2}
    try:
        for meta in mirror_store.read_meta():
            ts = str(meta.get("finished_at") or "")
            if len(ts) >= 4 and ts[:4].isdigit():
                years.add(int(ts[:4]))
    except Exception:  # noqa: BLE001  年份列表失败不影响主流程
        pass
    return sorted(years, reverse=True)
