"""看板统计服务：8 类统计 + 年份列表。

统计口径与旧版 visualization.html 完全一致：
- 剔除测试样品编号：WC26030001 / WC26060001
- 排除练兵合同 PS2026001
- 按 DETECTION_NO 去重
- 检出判定：REPORT_VAL 非空、非'未检出%'、非'/'、非'阴性'、非'N.D.'
- 排除甲壳类（虾/蟹）呋喃西林检出项
- 区县归类：COUNTY 优先，缺失按 ADDRESS 地址兜底
"""
from __future__ import annotations

from datetime import datetime

from app.core import db


async def _q(sql: str) -> dict:
    """统一数据访问入口：沿用旧看板 `pool.dm_query` 的返回结构 {columns, rows}。

    原实现经 unified-pool(:4000) 转发达梦；统一工程改为后端直连达梦（W-3 结论），
    调用契约与返回结构保持不变，因此本文件其余统计口径代码零改动。
    """
    return await db.adm(sql)


async def _sql(sql: str) -> dict:
    """LIMS（SQL Server）侧查询入口——同样经镜像层路由，不直连源库。"""
    return await db.asql(sql)

SCHEMA = "DETECTION"
EXCLUDE_TEST_NO = ["WC26030001", "WC26060001"]
EXCLUDE_TEST_SQL = ", ".join(f"'{n}'" for n in EXCLUDE_TEST_NO)

# 苏州 9 区县（固定顺序）
REGIONS = ["张家港市", "常熟市", "昆山市", "太仓市", "吴江区", "吴中区", "相城区", "姑苏区", "高新区"]

# 地址兜底规则（COUNTY 缺失时按地址解析区县）
_ADDR_RULE = [
    ("张家港市", ["张家港市", "常阴沙", "杨舍镇"]),
    ("常熟市", ["常熟市"]),
    ("昆山市", ["昆山市"]),
    ("太仓市", ["太仓市", "浮桥镇"]),
    ("吴江区", ["吴江区", "吴江市"]),
    ("吴中区", ["吴中区"]),
    ("相城区", ["相城区"]),
    ("高新区", ["虎丘区", "高新区", "高新"]),
    ("姑苏区", ["姑苏区"]),
]


def _parse_region(county: str, address: str) -> str | None:
    if county:
        if county == "虎丘区":
            return "高新区"
        if county == "吴江市":
            return "吴江区"
        for region, keys in _ADDR_RULE:
            if county in keys:
                return region
        return None
    if address:
        for region, keys in _ADDR_RULE:
            for key in keys:
                if key in address:
                    return region
    return None


def _max_month(year: int) -> int:
    now = datetime.now()
    return (now.month + 1) if year == now.year else 12


def _row_val(row: list, idx: int, key: str) -> object:
    """兼容数组/对象行格式取值。中间池返回的 rows 均为数组。"""
    if isinstance(row, (list, tuple)):
        return row[idx] if idx < len(row) else None
    if isinstance(row, dict):
        if key in row:
            return row[key]
        vals = list(row.values())
        return vals[idx] if idx < len(vals) else None
    return None


def _num(v: object, default: int = 0) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ================= 顶部统计卡片 =================

async def get_summary(year: int) -> dict:
    """设备数 / 样品总数 / 统计月份数。"""
    # 设备数
    equip_rows = await _q(
        f"SELECT COUNT(*) AS V FROM {SCHEMA}.DT_EQUIPMENT_BILL WHERE IS_DELETED = 0"
    )
    equip = _num(_row_val(equip_rows["rows"][0], 0, "V") if equip_rows["rows"] else None)

    # 当年样品总数（按 DETECTION_NO 去重，剔除测试样品）
    month_data = await get_monthly(year)
    total = sum(month_data["values"])

    return {
        "equipment": equip,
        "sample_total": total,
        "month_count": _max_month(year),
        "year": year,
    }


# ================= 每月检测样品数量 =================

async def get_monthly(year: int) -> dict:
    """每月检测样品数量折线图（按 DETECTION_NO 去重，缺失月份补 0）。"""
    rows = (
        await _q(
            f"SELECT TO_CHAR(CREATE_DATETIME,'YYYY-MM') AS K, COUNT(DISTINCT DETECTION_NO) AS V "
            f"FROM {SCHEMA}.DT_SAMPLE "
            f"WHERE CREATE_DATETIME IS NOT NULL AND EXTRACT(YEAR FROM CREATE_DATETIME) = {year} "
            f"  AND DETECTION_NO NOT IN ({EXCLUDE_TEST_SQL}) "
            f"GROUP BY TO_CHAR(CREATE_DATETIME,'YYYY-MM') ORDER BY K"
        )
    )["rows"]

    max_month = _max_month(year)
    month_map: dict[str, int] = {}
    for row in rows:
        k = str(_row_val(row, 0, "K") or "")
        v = _num(_row_val(row, 1, "V"))
        if k:
            month_map[k] = v

    labels: list[str] = []
    values: list[int] = []
    for m in range(1, max_month + 1):
        key = f"{year}-{m:02d}"
        labels.append(key)
        values.append(month_map.get(key, 0))

    return {"labels": labels, "values": values, "year": year}


# ================= 样品产品类型 =================

async def get_product(year: int) -> dict:
    """样品产品类型分布（FULL_VALUE_PATH 拆 '-' 取第二层级大类）。"""
    rows = (
        await _q(
            f"SELECT c.FULL_VALUE_PATH AS K, COUNT(*) AS V FROM ( "
            f"  SELECT MIN(SAMPLE_CATEGORY_ID) AS SCID FROM {SCHEMA}.DT_SAMPLE "
            f"  WHERE CREATE_DATETIME IS NOT NULL AND EXTRACT(YEAR FROM CREATE_DATETIME) = {year} "
            f"    AND DETECTION_NO NOT IN ({EXCLUDE_TEST_SQL}) "
            f"  GROUP BY DETECTION_NO "
            f") s LEFT JOIN {SCHEMA}.DT_SAMPLE_CATEGORY c ON s.SCID = c.ID "
            f"GROUP BY c.FULL_VALUE_PATH"
        )
    )["rows"]

    groups: dict[str, int] = {}
    for row in rows:
        path = str(_row_val(row, 0, "K") or "")
        v = _num(_row_val(row, 1, "V"))
        parts = path.split("-")
        top = (parts[1] if len(parts) > 1 else parts[0]) or ""
        name = top.strip() or "其他"
        groups[name] = groups.get(name, 0) + v

    arr = sorted(groups.items(), key=lambda x: x[1], reverse=True)
    return {
        "labels": [k for k, _ in arr],
        "values": [v for _, v in arr],
        "year": year,
    }


# ================= 月度样品数量（农/畜/水/其他 堆叠） =================

async def get_completion(year: int) -> dict:
    """月度样品数量堆叠柱状图：农产品/畜产品/水产品/其他。"""
    rows = (
        await _q(
            f"SELECT TO_CHAR(s.CDT,'YYYY-MM') AS K, "
            f"CASE WHEN c.FULL_VALUE_PATH LIKE '%农产品%' THEN '农产品' "
            f"     WHEN c.FULL_VALUE_PATH LIKE '%畜产品%' THEN '畜产品' "
            f"     WHEN c.FULL_VALUE_PATH LIKE '%水产品%' THEN '水产品' "
            f"     ELSE '其他' END AS CAT, "
            f"COUNT(*) AS V "
            f"FROM ( "
            f"  SELECT DETECTION_NO, MIN(ROWID) AS RID, MIN(CREATE_DATETIME) AS CDT, MIN(SAMPLE_CATEGORY_ID) AS SCID "
            f"  FROM {SCHEMA}.DT_SAMPLE "
            f"  WHERE CREATE_DATETIME IS NOT NULL AND EXTRACT(YEAR FROM CREATE_DATETIME) = {year} "
            f"    AND DETECTION_NO NOT IN ({EXCLUDE_TEST_SQL}) "
            f"  GROUP BY DETECTION_NO "
            f") s "
            f"JOIN {SCHEMA}.DT_SAMPLE_CATEGORY c ON s.SCID = c.ID "
            f"GROUP BY TO_CHAR(s.CDT,'YYYY-MM'), "
            f"  CASE WHEN c.FULL_VALUE_PATH LIKE '%农产品%' THEN '农产品' "
            f"       WHEN c.FULL_VALUE_PATH LIKE '%畜产品%' THEN '畜产品' "
            f"       WHEN c.FULL_VALUE_PATH LIKE '%水产品%' THEN '水产品' "
            f"       ELSE '其他' END "
            f"ORDER BY K, CAT"
        )
    )["rows"]

    cats = ["农产品", "畜产品", "水产品", "其他"]
    max_month = _max_month(year)
    labels = [f"{m}月" for m in range(1, max_month + 1)]
    series: dict[str, list[int]] = {cat: [0] * max_month for cat in cats}

    for row in rows:
        k = str(_row_val(row, 0, "K") or "")
        cat = str(_row_val(row, 1, "CAT") or "")
        v = _num(_row_val(row, 2, "V"))
        if cat not in cats:
            continue
        month_num = int(k.split("-")[1]) if "-" in k else 0
        idx = month_num - 1
        if 0 <= idx < max_month:
            series[cat][idx] = v

    total = sum(sum(vals) for vals in series.values())
    return {"labels": labels, "series": series, "total": total, "year": year}


# ================= 月度检出率曲线 =================

_DETECT_FILTER = (
    f"REPORT_VAL IS NOT NULL AND REPORT_VAL NOT LIKE '未检出%' "
    f"AND REPORT_VAL <> '/' AND REPORT_VAL <> '' "
    f"AND REPORT_VAL <> '阴性' AND REPORT_VAL <> 'N.D.'"
)

async def get_detect_rate(year: int) -> dict:
    """月度检出率曲线：农产品/畜产品/水产品（检出样品/检测样品 按 DETECTION_NO 去重）。"""
    rows = (
        await _q(
            f"SELECT TO_CHAR(s.CREATE_DATETIME,'YYYY-MM') AS M, "
            f"CASE WHEN c.FULL_VALUE_PATH LIKE '%农产品%' THEN '农产品' "
            f"     WHEN c.FULL_VALUE_PATH LIKE '%畜产品%' THEN '畜产品' "
            f"     WHEN c.FULL_VALUE_PATH LIKE '%水产品%' THEN '水产品' "
            f"     ELSE '其他' END AS CAT, "
            f"COUNT(DISTINCT s.DETECTION_NO) AS TOTAL, "
            f"COUNT(DISTINCT CASE WHEN rc.REPORT_VAL IS NOT NULL AND rc.REPORT_VAL NOT LIKE '未检出%' "
            f"AND rc.REPORT_VAL <> '/' AND rc.REPORT_VAL <> '' AND rc.REPORT_VAL <> '阴性' AND rc.REPORT_VAL <> 'N.D.' "
            f"AND NOT ((s.NAME LIKE '%虾%' OR s.NAME LIKE '%蟹%') AND sp.DECIDE_PROJECT_NAME LIKE '%呋喃西林%') "
            f"THEN s.DETECTION_NO END) AS POS "
            f"FROM {SCHEMA}.DT_SAMPLE_PROJECT sp "
            f"JOIN {SCHEMA}.DT_SAMPLE s ON sp.SAMPLE_ID = s.ID "
            f"JOIN {SCHEMA}.DT_SAMPLE_CATEGORY c ON s.SAMPLE_CATEGORY_ID = c.ID "
            f"JOIN {SCHEMA}.DT_RESULT_CHECK_IN rc ON rc.SAMPLE_PROJECT_ID = sp.ID "
            f"JOIN {SCHEMA}.DT_DETECTION d ON s.DETECTION_ID = d.ID "
            f"WHERE s.CREATE_DATETIME IS NOT NULL AND EXTRACT(YEAR FROM s.CREATE_DATETIME) = {year} "
            f"  AND s.DETECTION_NO NOT IN ({EXCLUDE_TEST_SQL}) "
            f"  AND c.FULL_VALUE_PATH LIKE '%产品%' "
            f"  AND d.CONTRACTS_NO IS NOT NULL AND d.CONTRACTS_NO <> '' "
            f"  AND d.CONTRACTS_NO NOT LIKE 'PS2026001%' "
            f"GROUP BY TO_CHAR(s.CREATE_DATETIME,'YYYY-MM'), "
            f"  CASE WHEN c.FULL_VALUE_PATH LIKE '%农产品%' THEN '农产品' "
            f"       WHEN c.FULL_VALUE_PATH LIKE '%畜产品%' THEN '畜产品' "
            f"       WHEN c.FULL_VALUE_PATH LIKE '%水产品%' THEN '水产品' "
            f"       ELSE '其他' END "
            f"ORDER BY M, CAT"
        )
    )["rows"]

    cats = ["农产品", "畜产品", "水产品"]
    max_month = _max_month(year)
    labels = [f"{year}-{m:02d}" for m in range(1, max_month + 1)]
    series: dict[str, list[dict]] = {cat: [{"total": 0, "pos": 0} for _ in range(max_month)] for cat in cats}

    for row in rows:
        m = str(_row_val(row, 0, "M") or "")
        cat = str(_row_val(row, 1, "CAT") or "")
        total = _num(_row_val(row, 2, "TOTAL"))
        pos = _num(_row_val(row, 3, "POS"))
        if m in labels and cat in cats:
            idx = labels.index(m)
            series[cat][idx] = {"total": total, "pos": pos}

    return {"labels": labels, "series": series, "year": year}


# ================= 高频检出 Top5 =================

_TOP_BASE = (
    "FROM {SCHEMA}.DT_SAMPLE_PROJECT sp "
    "JOIN {SCHEMA}.DT_SAMPLE s ON sp.SAMPLE_ID = s.ID "
    "JOIN {SCHEMA}.DT_SAMPLE_CATEGORY c ON s.SAMPLE_CATEGORY_ID = c.ID "
    "JOIN {SCHEMA}.DT_RESULT_CHECK_IN rc ON rc.SAMPLE_PROJECT_ID = sp.ID "
    "JOIN {SCHEMA}.DT_DETECTION d ON s.DETECTION_ID = d.ID "
    "WHERE s.CREATE_DATETIME IS NOT NULL AND EXTRACT(YEAR FROM s.CREATE_DATETIME) = {year} "
    "  AND s.DETECTION_NO NOT IN ({EXCLUDE_TEST_SQL}) "
    "  AND d.CONTRACTS_NO IS NOT NULL AND d.CONTRACTS_NO <> '' "
    "  AND d.CONTRACTS_NO NOT LIKE 'PS2026001%' "
    "  AND rc.REPORT_VAL IS NOT NULL AND rc.REPORT_VAL NOT LIKE '未检出%' "
    "  AND rc.REPORT_VAL <> '/' AND rc.REPORT_VAL <> '' AND rc.REPORT_VAL <> '阴性' AND rc.REPORT_VAL <> 'N.D.' "
    "  AND NOT ((s.NAME LIKE '%虾%' OR s.NAME LIKE '%蟹%') AND sp.DECIDE_PROJECT_NAME LIKE '%呋喃西林%') "
)

_CAT_EXPR = (
    "CASE WHEN c.FULL_VALUE_PATH LIKE '%农产品%' THEN '农产品' "
    "WHEN c.FULL_VALUE_PATH LIKE '%畜产品%' THEN '畜产品' "
    "WHEN c.FULL_VALUE_PATH LIKE '%水产品%' THEN '水产品' ELSE '其他' END"
)


def _build_top_map(rows: list, name_idx: int, name_key: str) -> dict[str, list[dict]]:
    """按类别聚合 Top 列表。"""
    result: dict[str, list[dict]] = {}
    for row in rows:
        cat = str(_row_val(row, 0, "CAT") or "")
        if cat == "其他":
            continue
        name = str(_row_val(row, name_idx, name_key) or "")
        cnt = _num(_row_val(row, 2, "CNT"))
        if not name:
            continue
        result.setdefault(cat, []).append({"name": name, "cnt": cnt})
    # 每类取前 5
    for cat in result:
        result[cat] = sorted(result[cat], key=lambda x: x["cnt"], reverse=True)[:5]
    return result


async def get_top(year: int) -> dict:
    """高频检出 Top5：农产品/畜产品/水产品 各自的检出项目与检出样品。"""
    base = _TOP_BASE.format(SCHEMA=SCHEMA, EXCLUDE_TEST_SQL=EXCLUDE_TEST_SQL, year=year)
    r_proj = (
        await _q(
            f"SELECT {_CAT_EXPR} AS CAT, sp.DECIDE_PROJECT_NAME AS PROJECT, "
            f"COUNT(DISTINCT s.DETECTION_NO) AS CNT "
            + base
            + f" AND c.FULL_VALUE_PATH LIKE '%产品%' "
            f"GROUP BY {_CAT_EXPR}, sp.DECIDE_PROJECT_NAME ORDER BY CAT, CNT DESC"
        )
    )["rows"]
    r_sample = (
        await _q(
            f"SELECT {_CAT_EXPR} AS CAT, s.NAME AS SNAME, "
            f"COUNT(DISTINCT s.DETECTION_NO) AS CNT "
            + base
            + f" AND c.FULL_VALUE_PATH LIKE '%产品%' AND s.NAME IS NOT NULL AND s.NAME <> '' "
            f"GROUP BY {_CAT_EXPR}, s.NAME ORDER BY CAT, CNT DESC"
        )
    )["rows"]

    return {
        "categories": {
            cat: {
                "projects": _build_top_map(r_proj, 1, "PROJECT").get(cat, []),
                "samples": _build_top_map(r_sample, 1, "SNAME").get(cat, []),
            }
            for cat in ["农产品", "畜产品", "水产品"]
        },
        "year": year,
    }


# ================= 风险项目/样品 Top5（超标） =================

_RISK_BASE = (
    "FROM {SCHEMA}.DT_SAMPLE_PROJECT sp "
    "JOIN {SCHEMA}.DT_SAMPLE s ON sp.SAMPLE_ID = s.ID "
    "JOIN {SCHEMA}.DT_SAMPLE_CATEGORY c ON s.SAMPLE_CATEGORY_ID = c.ID "
    "JOIN {SCHEMA}.DT_DETECTION d ON s.DETECTION_ID = d.ID "
    "WHERE s.CREATE_DATETIME IS NOT NULL AND EXTRACT(YEAR FROM s.CREATE_DATETIME) = {year} "
    "  AND s.DETECTION_NO NOT IN ({EXCLUDE_TEST_SQL}) "
    "  AND d.CONTRACTS_NO IS NOT NULL AND d.CONTRACTS_NO <> '' "
    "  AND d.CONTRACTS_NO NOT LIKE 'PS2026001%' "
    "  AND sp.SINGLE_JUDGE IN ('不合格','不符合') "
    "  AND NOT ((s.NAME LIKE '%虾%' OR s.NAME LIKE '%蟹%') AND sp.DECIDE_PROJECT_NAME LIKE '%呋喃西林%') "
)


async def get_risk(year: int) -> dict:
    """风险项目/样品 Top5：SINGLE_JUDGE IN ('不合格','不符合')。"""
    base = _RISK_BASE.format(SCHEMA=SCHEMA, EXCLUDE_TEST_SQL=EXCLUDE_TEST_SQL, year=year)
    r_proj = (
        await _q(
            f"SELECT {_CAT_EXPR} AS CAT, sp.DECIDE_PROJECT_NAME AS PROJECT, "
            f"COUNT(DISTINCT s.DETECTION_NO) AS CNT "
            + base
            + f" AND c.FULL_VALUE_PATH LIKE '%产品%' "
            f"GROUP BY {_CAT_EXPR}, sp.DECIDE_PROJECT_NAME ORDER BY CAT, CNT DESC"
        )
    )["rows"]
    r_sample = (
        await _q(
            f"SELECT {_CAT_EXPR} AS CAT, s.NAME AS SNAME, "
            f"COUNT(DISTINCT s.DETECTION_NO) AS CNT "
            + base
            + f" AND c.FULL_VALUE_PATH LIKE '%产品%' AND s.NAME IS NOT NULL AND s.NAME <> '' "
            f"GROUP BY {_CAT_EXPR}, s.NAME ORDER BY CAT, CNT DESC"
        )
    )["rows"]

    return {
        "categories": {
            cat: {
                "projects": _build_top_map(r_proj, 1, "PROJECT").get(cat, []),
                "samples": _build_top_map(r_sample, 1, "SNAME").get(cat, []),
            }
            for cat in ["农产品", "畜产品", "水产品"]
        },
        "year": year,
    }


# ================= 苏州市不合格率分布雷达图 =================

async def get_region(year: int) -> dict:
    """苏州 9 区县不合格率：按受检单位 COUNTY 归类，缺 COUNTY 按 ADDRESS 兜底。"""
    rows = (
        await _q(
            f"SELECT s.DETECTION_NO, dc.COUNTY, dc.ADDRESS, "
            f"MAX(CASE WHEN sp.SINGLE_JUDGE IN ('不合格','不符合') THEN 1 ELSE 0 END) AS IS_FAIL "
            f"FROM {SCHEMA}.DT_SAMPLE s "
            f"JOIN {SCHEMA}.DT_DETECTION det ON s.DETECTION_ID = det.ID "
            f"JOIN {SCHEMA}.DT_DETECTED_COMPANY dc ON det.DETECTED_COMPANY_ID = dc.ID "
            f"JOIN {SCHEMA}.DT_SAMPLE_PROJECT sp ON sp.SAMPLE_ID = s.ID "
            f"JOIN {SCHEMA}.DT_SAMPLE_CATEGORY c ON s.SAMPLE_CATEGORY_ID = c.ID "
            f"WHERE s.CREATE_DATETIME IS NOT NULL AND EXTRACT(YEAR FROM s.CREATE_DATETIME) = {year} "
            f"  AND s.DETECTION_NO NOT IN ({EXCLUDE_TEST_SQL}) "
            f"  AND det.CONTRACTS_NO IS NOT NULL AND det.CONTRACTS_NO <> '' "
            f"  AND det.CONTRACTS_NO NOT LIKE 'PS2026001%' "
            f"  AND c.FULL_VALUE_PATH LIKE '%产品%' "
            f"GROUP BY s.DETECTION_NO, dc.COUNTY, dc.ADDRESS"
        )
    )["rows"]

    stat: dict[str, dict] = {}
    for row in rows:
        county = str(_row_val(row, 1, "COUNTY") or "")
        address = str(_row_val(row, 2, "ADDRESS") or "")
        is_fail = _num(_row_val(row, 3, "IS_FAIL"))
        region = _parse_region(county, address)
        if not region:
            continue
        s = stat.setdefault(region, {"tot": 0, "fail": 0})
        s["tot"] += 1
        if is_fail:
            s["fail"] += 1

    values: list[float] = []
    totals: list[int] = []
    fails: list[int] = []
    for region in REGIONS:
        s = stat.get(region, {"tot": 0, "fail": 0})
        totals.append(s["tot"])
        fails.append(s["fail"])
        values.append(round(s["fail"] / s["tot"] * 100, 2) if s["tot"] > 0 else 0)

    return {
        "labels": REGIONS,
        "values": values,
        "totals": totals,
        "fails": fails,
        "year": year,
    }


# ================= 产品不合格率雷达图 =================

async def get_product_risk(year: int) -> dict:
    """产品不合格率：农产品/畜产品/水产品 3 类。"""
    rows = (
        await _q(
            f"SELECT REGION, COUNT(DISTINCT DETECTION_NO) AS TOT, "
            f"COUNT(DISTINCT CASE WHEN IS_FAIL=1 THEN DETECTION_NO END) AS FAIL_CNT "
            f"FROM ( "
            f"  SELECT s.DETECTION_NO, "
            f"    CASE "
            f"         WHEN c.FULL_VALUE_PATH LIKE '%农产品%' THEN '农产品' "
            f"         WHEN c.FULL_VALUE_PATH LIKE '%畜产品%' THEN '畜产品' "
            f"         WHEN c.FULL_VALUE_PATH LIKE '%水产品%' THEN '水产品' "
            f"         ELSE NULL END AS REGION, "
            f"    CASE WHEN sp.SINGLE_JUDGE IN ('不合格','不符合') THEN 1 ELSE 0 END AS IS_FAIL "
            f"  FROM {SCHEMA}.DT_SAMPLE s "
            f"  JOIN {SCHEMA}.DT_DETECTION det ON s.DETECTION_ID = det.ID "
            f"  JOIN {SCHEMA}.DT_SAMPLE_PROJECT sp ON sp.SAMPLE_ID = s.ID "
            f"  JOIN {SCHEMA}.DT_SAMPLE_CATEGORY c ON s.SAMPLE_CATEGORY_ID = c.ID "
            f"  WHERE s.CREATE_DATETIME IS NOT NULL AND EXTRACT(YEAR FROM s.CREATE_DATETIME) = {year} "
            f"    AND s.DETECTION_NO NOT IN ({EXCLUDE_TEST_SQL}) "
            f"    AND det.CONTRACTS_NO IS NOT NULL AND det.CONTRACTS_NO <> '' "
            f"    AND det.CONTRACTS_NO NOT LIKE 'PS2026001%' "
            f"    AND c.FULL_VALUE_PATH LIKE '%产品%' "
            f") WHERE REGION IS NOT NULL GROUP BY REGION"
        )
    )["rows"]

    cats = ["农产品", "畜产品", "水产品"]
    stat: dict[str, dict] = {}
    for row in rows:
        region = str(_row_val(row, 0, "REGION") or "")
        tot = _num(_row_val(row, 1, "TOT"))
        fail = _num(_row_val(row, 2, "FAIL_CNT"))
        stat[region] = {"tot": tot, "fail": fail}

    values: list[float] = []
    totals: list[int] = []
    fails: list[int] = []
    for cat in cats:
        s = stat.get(cat, {"tot": 0, "fail": 0})
        totals.append(s["tot"])
        fails.append(s["fail"])
        values.append(round(s["fail"] / s["tot"] * 100, 2) if s["tot"] > 0 else 0)

    return {
        "labels": cats,
        "values": values,
        "totals": totals,
        "fails": fails,
        "year": year,
    }


# ================= 检测项次（同编号内项目名去重） =================

# 口径说明：
# 1) 样品集合与 get_completion 同源：同年份、剔除测试样品、按 DETECTION_NO 去重，
#    类别取该编号下 MIN(SAMPLE_CATEGORY_ID)、月份取 MIN(CREATE_DATETIME)。
# 2) 与 get_completion 的差异：同一 DETECTION_NO 下存在重复/分次登记（实测发现最多 9 行，
#    且含 0 项空行），故对 (DETECTION_NO, DECIDE_PROJECT_NAME) 去重后计数，
#    避免重复登记导致项次被放大。
_ITEM_COUNT_SQL = (
    "SELECT TO_CHAR(x.CDT,'YYYY-MM') AS K, x.CAT AS CAT, COUNT(*) AS V FROM ( "
    "  SELECT DISTINCT d.DNO AS DNO, d.CDT AS CDT, "
    "    CASE WHEN c.FULL_VALUE_PATH LIKE '%农产品%' THEN '农产品' "
    "         WHEN c.FULL_VALUE_PATH LIKE '%畜产品%' THEN '畜产品' "
    "         WHEN c.FULL_VALUE_PATH LIKE '%水产品%' THEN '水产品' "
    "         ELSE '其他' END AS CAT, "
    "    sp.DECIDE_PROJECT_NAME AS PJ "
    "  FROM ( "
    "    SELECT DETECTION_NO AS DNO, MIN(CREATE_DATETIME) AS CDT, "
    "           MIN(SAMPLE_CATEGORY_ID) AS SCID "
    "    FROM {SCHEMA}.DT_SAMPLE "
    "    WHERE CREATE_DATETIME IS NOT NULL AND EXTRACT(YEAR FROM CREATE_DATETIME) = {year} "
    "      AND DETECTION_NO IS NOT NULL AND DETECTION_NO NOT IN ({EXCLUDE_TEST_SQL}) "
    "    GROUP BY DETECTION_NO "
    "  ) d "
    "  JOIN {SCHEMA}.DT_SAMPLE s2 ON s2.DETECTION_NO = d.DNO "
    "  JOIN {SCHEMA}.DT_SAMPLE_PROJECT sp ON sp.SAMPLE_ID = s2.ID "
    "  JOIN {SCHEMA}.DT_SAMPLE_CATEGORY c ON c.ID = d.SCID "
    "  WHERE sp.DECIDE_PROJECT_NAME IS NOT NULL AND sp.DECIDE_PROJECT_NAME <> '' "
    ") x "
    "GROUP BY TO_CHAR(x.CDT,'YYYY-MM'), x.CAT ORDER BY K, CAT"
)


async def get_item_count(year: int) -> dict:
    """检测项次统计：按月 × 农/畜/水/其他 聚合，并输出分类合计与总项次。"""
    sql = _ITEM_COUNT_SQL.format(
        SCHEMA=SCHEMA, EXCLUDE_TEST_SQL=EXCLUDE_TEST_SQL, year=year
    )
    rows = (await _q(sql))["rows"]

    cats = ["农产品", "畜产品", "水产品", "其他"]
    max_month = _max_month(year)
    labels = [f"{m}月" for m in range(1, max_month + 1)]
    series: dict[str, list[int]] = {cat: [0] * max_month for cat in cats}

    for row in rows:
        k = str(_row_val(row, 0, "K") or "")
        cat = str(_row_val(row, 1, "CAT") or "")
        v = _num(_row_val(row, 2, "V"))
        if cat not in cats or "-" not in k:
            continue
        tail = k.split("-")[1]
        month_num = int(tail) if tail.isdigit() else 0
        idx = month_num - 1
        if 0 <= idx < max_month:
            series[cat][idx] += v

    cat_totals = {cat: sum(series[cat]) for cat in cats}
    return {
        "labels": labels,
        "series": series,
        "categories": {
            "labels": cats,
            "values": [cat_totals[cat] for cat in cats],
        },
        "total": sum(cat_totals.values()),
        "year": year,
    }


# ================= 可选年份列表 =================

async def get_years() -> list[int]:
    """可选年份列表（从库中取 DISTINCT 年份，含当前年）。"""
    rows = (
        await _q(
            f"SELECT DISTINCT EXTRACT(YEAR FROM CREATE_DATETIME) AS Y "
            f"FROM {SCHEMA}.DT_SAMPLE WHERE CREATE_DATETIME IS NOT NULL ORDER BY 1"
        )
    )["rows"]
    years = {_num(_row_val(r, 0, "Y")) for r in rows}
    years.discard(0)
    years.add(datetime.now().year)
    return sorted(years, reverse=True)


# ---------------- LIMS 样品品类归类（7 类，与 SQL Server 页「样品类型」口径一致） ----------------
# 归类优先级：
#   1) LIMS 的**样品类型细分**字段 `Sample_Kind_I`（16 种取值、100% 有值；SQL Server 页的
#      「样品类型」筛选就是由它归类的，见 frontend/src/views/SqlServerView.vue 的 TEMPLATE_KIND_I_MAP）；
#   2) 落进「其他」的极少数记录，再用**样品名称**关键词补判（使用方要求"按样品名称归类"）。
# 为何不纯按样品名称：样品名称实测有 **767 种**（猪肝/青菜/鲫鱼/土壤/水质…），纯关键词表既难维护、
#   也必然大量误判（试算时 1,829 条落进"其他"，其中池塘水/叶用莴苣/白鲢等明显应归水质/农产品/水产品）；
#   而细分类型字段本身就是源系统按品名维护好的归类，更可靠。
LIMS_KIND_MAP: dict[str, str] = {
    "农产品": "农产品", "蔬菜": "农产品", "水果": "农产品", "稻谷": "农产品",
    "小麦": "农产品", "食用菌": "农产品",
    "畜产品": "畜产品", "生鲜乳": "畜产品",
    "水产品": "水产品",
    "环境土壤": "土壤",
    "环境水质": "水质",
    "肥料": "肥料",
}
LIMS_CATEGORIES = ["农产品", "畜产品", "水产品", "土壤", "水质", "肥料", "其他"]

# LIMS 判定字段 TResult.Qualified 的取值：'符合' / '不符合'（空、'/' 为未判定）。
# 「高风险项目（超标）」= Qualified = LIMS_BAD 的项次。
LIMS_OK = "符合"
LIMS_BAD = "不符合"

# 样品名称补判规则（仅用于细分类型未覆盖的记录；顺序即优先级）
LIMS_NAME_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("土壤", ("土壤", "泥土", "底泥")),
    ("水质", ("水质", "水样", "饮用水", "污水")),
    ("肥料", ("肥料", "化肥")),
    ("水产品", ("鱼", "虾", "蟹", "贝", "螺", "鳖", "蛙", "鲈", "鲢", "鲤", "鲫", "鳊", "鳙",
                "鳝", "鳅", "鲟", "鲵", "豚", "海参", "藻")),
    ("畜产品", ("猪", "牛", "羊", "鸡", "鸭", "鹅", "鸽", "兔", "肉", "肝", "蛋", "奶", "乳",
                "尿", "禽", "蜂", "肾")),
    ("农产品", ("菜", "蔬", "瓜", "豆", "茄", "椒", "葱", "蒜", "姜", "萝卜", "薯", "芋", "藕",
                "笋", "菌", "菇", "耳", "稻", "米", "麦", "谷", "茶", "果", "桃", "梨", "枣",
                "柿", "莓", "葡萄", "柑", "橘", "橙", "柚")),
]


def _lims_category(kind: str, name: str) -> str:
    cat = LIMS_KIND_MAP.get((kind or "").strip())
    if cat:
        return cat
    n = (name or "").strip()
    for c, keys in LIMS_NAME_RULES:
        if any(k in n for k in keys):
            return c
    return "其他"


async def _lims_by_category(cy_span: str) -> list[dict]:
    """LIMS 样品按 7 大品类统计（送检单去重）。

    ⚠️ 口径说明：按 (样品类型细分, 样品名称) 分组后**各自对送检单去重**再加总。
    实测每张送检单只含一个样品名称（各品类合计 = 总体去重数 21,759），故不存在重复计数；
    若日后源库出现"一单多品名"，合计会略大于样品总数——届时应改为按送检单的主品类归属。
    """
    rows = (
        await _sql(
            f"SELECT Sample_Kind_I AS K, Sample_Name AS N, COUNT(DISTINCT Submission_ID) AS V "
            f"FROM VIEW_SubMisSampleResult WHERE {cy_span} GROUP BY Sample_Kind_I, Sample_Name"
        )
    )["rows"]
    agg = {c: 0 for c in LIMS_CATEGORIES}
    for r in rows:
        agg[_lims_category(str(_row_val(r, 0, "K") or ""), str(_row_val(r, 1, "N") or ""))] += _num(
            _row_val(r, 2, "V")
        )
    return [{"name": c, "cnt": agg[c]} for c in LIMS_CATEGORIES]


# ================= 历史统计（2020 起 · LIMS/SQL Server 镜像） =================
# 口径说明（基于镜像数据的实测形态，2026-09-18 与使用方确认）：
# - 达梦平台 2025 年才启用（DT_SAMPLE 仅 2025/2026 两年），2024 及之前只能来自 LIMS；
# - 年度：样品按 VIEW.Cy_Date（抽样日期）年份；项次按 TResult.Date_Test（检测日期）年份；
# - 样品数 = COUNT(DISTINCT Submission_ID)（送检单号；实测 2024 年 3,845，与
#   Submis_Serial_No 去重一致。VIEW.Sample_ID 几乎全空，不能作样品键）；
# - 检测项次 = TResult 记录数（每行 = 一个样品的一个检测项目结果）；
# - 判定 = TResult.Qualified（'符合'/'不符合'；空、'/' 计为未判定，不进符合率）。
#
# ⚠️ 明细卡片口径（2026-09-18 变更）：原「高频检测项目 Top10」按**全部项次**计数，
#    只反映"检什么检得多"，与风险无关；使用方要求改为**高风险项目（超标）项次统计**：
#    只统计 Qualified = '不符合' 的项次，再按检测项目分组取 Top10。
#    （实测 2024 年：全部项次 123,759 条，其中不符合 104 条 → 卡片由"常规药残大项"
#     变为"地西泮/砷/克伦特罗…"这类真正出问题的项目，正是要看的风险点。）
async def get_history(
    year_from: int = 2020,
    year_to: int = 2025,
    year: int | None = None,
    overview: bool = False,
) -> dict:
    """LIMS 历史统计。

    - `overview=False`（默认）：**只查 `year` 这一年**——走年份区间 + 覆盖索引，秒内返回；
    - `overview=True`：跨年对比表（扫 2020–2025 全量，约 2 秒），仅在用户点「加载历年对比」时调用。

    为什么默认不跨年：跨年要扫两张百万行**宽表**（VIEW 126 列 / TResult 92 列），
    而页面首屏只需要当前选中的年份。
    """
    if not overview:
        return await _history_one_year(year if year is not None else year_to)

    years = list(range(year_from, year_to + 1))
    # ⚠️ 用**日期区间**过滤，而不是 `CAST(STRFTIME('%Y', col) AS INTEGER) BETWEEN …`：
    #    后者把日期列包在函数里 → 索引完全用不上 → 百万行逐行算函数。
    #    实测该接口因此要 **28.3 秒**；改成区间后走 Date_Test / Cy_Date 上的索引。
    #    （镜像里日期是 ISO 文本，字典序 == 时间序，故字符串比较等价于时间比较。）
    lo, hi = f"{year_from}-01-01", f"{year_to + 1}-01-01"
    cy_span = f"Cy_Date >= '{lo}' AND Cy_Date < '{hi}'"
    dt_span = f"Date_Test >= '{lo}' AND Date_Test < '{hi}'"

    samples = {y: 0 for y in years}
    rows = (
        await _sql(
            f"SELECT CAST(STRFTIME('%Y', Cy_Date) AS INTEGER) AS Y, COUNT(DISTINCT Submission_ID) AS V "
            f"FROM VIEW_SubMisSampleResult WHERE {cy_span} GROUP BY Y"
        )
    )["rows"]
    for r in rows:
        y = _num(_row_val(r, 0, "Y"))
        if y in samples:
            samples[y] = _num(_row_val(r, 1, "V"))

    items = {y: 0 for y in years}
    ok_cnt = {y: 0 for y in years}
    bad_cnt = {y: 0 for y in years}
    rows = (
        await _sql(
            f"SELECT CAST(STRFTIME('%Y', Date_Test) AS INTEGER) AS Y, Qualified AS Q, COUNT(*) AS V "
            f"FROM TResult WHERE {dt_span} GROUP BY Y, Q"
        )
    )["rows"]
    for r in rows:
        y = _num(_row_val(r, 0, "Y"))
        if y not in items:
            continue
        q = str(_row_val(r, 1, "Q") or "")
        n = _num(_row_val(r, 2, "V"))
        items[y] += n
        if q == "符合":
            ok_cnt[y] += n
        elif q == "不符合":
            bad_cnt[y] += n

    risk_rows = (
        await _sql(
            f"SELECT Item, COUNT(*) AS V FROM TResult WHERE {dt_span} "
            f"AND Qualified = '{LIMS_BAD}' GROUP BY Item ORDER BY V DESC LIMIT 10"
        )
    )["rows"]

    detail = None
    if year is not None and year in years:
        # 指定年份的明细：高风险（超标）项目 + 样品类型分布（均按该年）
        y_lo, y_hi = f"{year}-01-01", f"{year + 1}-01-01"
        yr_test = f"Date_Test >= '{y_lo}' AND Date_Test < '{y_hi}'"
        yr_cy = f"Cy_Date >= '{y_lo}' AND Cy_Date < '{y_hi}'"
        det_rows = (
            await _sql(
                f"SELECT Item, COUNT(*) AS V FROM TResult WHERE {yr_test} "
                f"AND Qualified = '{LIMS_BAD}' GROUP BY Item ORDER BY V DESC LIMIT 10"
            )
        )["rows"]
        by_kind = await _lims_by_category(yr_cy)
        detail = {
            "year": year,
            "samples": samples[year],
            "items": items[year],
            "ok": ok_cnt[year],
            "bad": bad_cnt[year],
            "risk_items": [
                {"name": str(_row_val(r, 0, "Item") or ""), "cnt": _num(_row_val(r, 1, "V"))}
                for r in det_rows
            ],
            "by_kind": by_kind,
        }

    return {
        "years": years,
        "samples": [samples[y] for y in years],
        "items": [items[y] for y in years],
        "ok": [ok_cnt[y] for y in years],
        "bad": [bad_cnt[y] for y in years],
        "risk_items": [
            {"name": str(_row_val(r, 0, "Item") or ""), "cnt": _num(_row_val(r, 1, "V"))}
            for r in risk_rows
        ],
        "detail": detail,
        "source": "LIMS（SQL Server）镜像",
        "notes": "2025 年 LIMS 数据因系统切换仅存部分，2025 年起的完整统计请见上方达梦板块",
    }


async def _history_one_year(year: int) -> dict:
    """**只查一个年份**的历史统计（快：年份区间 + 覆盖索引，通常 1 秒内）。

    返回结构与 `get_history(overview=True)` 一致（汇总数组只含该年一项），
    便于前端共用同一套类型；`detail` 为该年明细。
    """
    lo, hi = f"{year}-01-01", f"{year + 1}-01-01"
    cy_span = f"Cy_Date >= '{lo}' AND Cy_Date < '{hi}'"
    dt_span = f"Date_Test >= '{lo}' AND Date_Test < '{hi}'"

    rows = (
        await _sql(
            f"SELECT COUNT(DISTINCT Submission_ID) AS V FROM VIEW_SubMisSampleResult WHERE {cy_span}"
        )
    )["rows"]
    samples = _num(_row_val(rows[0], 0, "V")) if rows else 0

    items = ok_cnt = bad_cnt = 0
    rows = (
        await _sql(
            f"SELECT Qualified AS Q, COUNT(*) AS V FROM TResult WHERE {dt_span} GROUP BY Q"
        )
    )["rows"]
    for r in rows:
        q = str(_row_val(r, 0, "Q") or "")
        n = _num(_row_val(r, 1, "V"))
        items += n
        if q == "符合":
            ok_cnt += n
        elif q == "不符合":
            bad_cnt += n

    # 高风险项目：只统计判定为「不符合」（即超标）的项次，按检测项目聚合取 Top10
    risk_rows = (
        await _sql(
            f"SELECT Item, COUNT(*) AS V FROM TResult WHERE {dt_span} "
            f"AND Qualified = '{LIMS_BAD}' GROUP BY Item ORDER BY V DESC LIMIT 10"
        )
    )["rows"]
    # 样品品类分布：按 7 大品类（口径见 _lims_by_category / LIMS_KIND_MAP）
    by_kind = await _lims_by_category(cy_span)

    return {
        "years": [year],
        "samples": [samples],
        "items": [items],
        "ok": [ok_cnt],
        "bad": [bad_cnt],
        "risk_items": [
            {"name": str(_row_val(r, 0, "Item") or ""), "cnt": _num(_row_val(r, 1, "V"))}
            for r in risk_rows
        ],
        "detail": {
            "year": year,
            "samples": samples,
            "items": items,
            "ok": ok_cnt,
            "bad": bad_cnt,
            "risk_items": [
                {"name": str(_row_val(r, 0, "Item") or ""), "cnt": _num(_row_val(r, 1, "V"))}
                for r in risk_rows
            ],
            "by_kind": by_kind,
        },
        "source": "LIMS（SQL Server）镜像",
        "notes": "2025 年 LIMS 数据因系统切换仅存部分，2025 年起的完整统计请见上方达梦板块",
    }
