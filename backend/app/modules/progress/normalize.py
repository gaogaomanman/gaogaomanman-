"""抽采样进度统计：文本归一与区域解析（纯函数，无 IO，便于单测）。

为什么单独一个文件：这些规则**同时被 store（写库前归一）与 service（匹配样品）使用**，
放在任一侧都会造成另一侧反向依赖；且它们是"口径"本身，集中一处便于对照与复核。

- `normalize_text`：全角→半角、去全部空白、转小写。用于产品类型「名称/别名」与样品名称的匹配键。
  去空白是必须的：实测源库存在 `GB 5009.12` / `GB5009.12`、`豇豆 ` 这类写法差异。
- `contract_key`：合同编号键。**只去首尾空白、不做大小写折叠**——合同编号是人工下达的业务编号，
  大小写不同应视为不同合同（如 `PS2026001` 与 `ps2026001`），避免把两个合同悄悄合并。
- `parse_city_county`：前端 `provinceCommon.ts::parseCityCounty`（5 处副本一致的 2374 号副本）的
  Python 等价实现，逐条对应其正则与返回分支；仅在「看板区县口径（COUNTY + ADDRESS 关键词）」未命中时兜底。
"""
from __future__ import annotations

import re

# ==================== 全角 → 半角 ====================

# 只保留实际会遇到的符号（括号/连接符/空白等），不做全字符表映射
_FULLWIDTH_MAP = str.maketrans(
    {
        "（": "(",
        "）": ")",
        "〔": "(",
        "〕": ")",
        "［": "[",
        "］": "]",
        "【": "[",
        "】": "]",
        "，": ",",
        "、": ",",
        "；": ";",
        "：": ":",
        "－": "-",
        "—": "-",
        "–": "-",
        "―": "-",
        "−": "-",
        "ー": "-",
        "／": "/",
        "＼": "\\",
        "％": "%",
        "＋": "+",
        "．": ".",
        "。": ".",
        "　": " ",  # 全角空格
    }
)

_WS_RE = re.compile(r"\s+")
# 括号及内容（归一化后只可能是半角括号）——实测源库样品名大量带 `（KH）`/`（BD1）` 后缀
_BRACKET_RE = re.compile(r"[(\[][^(\[)\]]*[)\]]")


def normalize_text(value: object) -> str:
    """归一化匹配键：全角转半角、去全部空白、转小写。"""
    s = str(value if value is not None else "").strip().translate(_FULLWIDTH_MAP)
    return _WS_RE.sub("", s).lower()


def strip_brackets(s: str) -> str:
    """去掉全部括号及内容（用于产品类型匹配的**宽松层**）。

    实测数据：`豇豆（KH）`、`苹果（BD1）`、`鸡蛋（KH）` 这类带质控/点位后缀的名称很常见，
    严格全等会把这些样品全部判为"未匹配"，因此需要宽松层兜底。
    """
    return _BRACKET_RE.sub("", s)


def clean_field(value: object, limit: int = 200) -> str:
    """通用字段清洗：转字符串、去首尾空白、截断上限长度。"""
    return str(value if value is not None else "").strip()[:limit]


def contract_key(value: object, limit: int = 64) -> str:
    """合同编号键：去首尾空白 + 截断（**不折叠大小写**）。空值返回空串（= 未指定合同）。"""
    return clean_field(value, limit)


# ==================== 区县解析（parseCityCounty 等价实现） ====================

_PROV_RE = re.compile(r"(省|自治区)")
# 主规则：市 + （非"路/街道/数字"开头）的 区/县/市/园区/新区/开发区
_CITY_COUNTY_RE = re.compile(r"(.+?[市])([^路街道\d]+?(?:区|县|市|园区|新区|开发区))")
# 主规则未命中时的宽松规则（# 对应 JS 的 `|| s.match(/(.+?[市])(.+?[区县])/)`）
_CITY_COUNTY_LOOSE_RE = re.compile(r"(.+?[市])(.+?[区县])")
_CITY_RE = re.compile(r"(.+?[市])")
_PARK_RE = re.compile(r"^([^路街道\d]+?(?:园区|新区|开发区))")


def parse_city_county(addr: object) -> str:
    """从地址解析「市 + 区县」（等价前端 `parseCityCounty`）。

    解析不出来时**原样返回**（与前端行为一致）——由调用方决定是否算「未归类」。
    """
    s = str(addr if addr is not None else "")
    if not s:
        return ""

    m = _PROV_RE.search(s)
    if m:
        s = s[m.end():]

    m2 = _CITY_COUNTY_RE.search(s) or _CITY_COUNTY_LOOSE_RE.search(s)
    if m2:
        return m2.group(1) + m2.group(2)

    m3 = _CITY_RE.search(s)
    if m3:
        return m3.group(1)

    m4 = _PARK_RE.search(s)
    if m4:
        return m4.group(1)

    return s
