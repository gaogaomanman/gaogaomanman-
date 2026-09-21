"""镜像表结构构建：按源库系统表自建，不依赖 SQLAlchemy 方言反射。

为什么不用反射（实测结论，见 `01_问题定义.md` 9.1）：
  达梦方言（dmSQLAlchemy）连接与 SQL 编译正常，但**反射不可靠**——
  `Table(..., autoload_with=dm_engine)` 抛 `NoSuchTableError`，
  `inspect().get_table_names(schema=...)` 返回的表集合也与预期 schema 不符。
因此改为：查系统表拿到「列名 + 类型 + 长度 + 精度 + 可空」，
按**显式类型映射**建镜像表。这样同时获得两个好处：
  1. 不依赖方言实现细节（更可控）；
  2. 类型明确，规避 SQLite 动态类型/亲和性带来的比较与排序偏差（风险 R-2）。

日期统一映射为 DateTime（SQLite 存 ISO 文本），
SQLAlchemy 的 SQLite 方言会把 `extract('year', col)` 编译为 `CAST(STRFTIME('%Y', col) AS INTEGER)`，
与业务 SQL 的年份过滤口径一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, Integer, LargeBinary, MetaData,
    Numeric, Table, Text, text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.types import TypeDecorator


class MirrorDecimal(TypeDecorator):
    """镜像侧的定点数：**入库转 float，读回转 Decimal**。

    为什么需要它（实测）：SQLite 的 DBAPI 不支持绑定 `Decimal` 对象，
    直连模式下 pyodbc 返回的 `Decimal` 会让镜像写入抛
    `sqlite3.ProgrammingError: type 'decimal.Decimal' is not supported`
    （`TResult`、`VIEW_SubMisSampleResult` 因此整表失败）。

    设计取舍：
      - 若存成 TEXT 可精确往返，但会**破坏 SQL 层面的数值语义**
        （`SUM`/`AVG`/`>`/`ORDER BY` 会按字符串比较），业务查询会算错，因此不采用；
      - 改存 REAL（IEEE754 双精度，有效位 15~17 位），本库数值为检测结果，
        有效位远低于该精度，不会产生可见偏差；
      - **读回时仍返回 Decimal**（由 `Numeric` 的结果处理器按 scale 量化），
        使业务层拿到的数据类型与直连模式一致，不需要改业务代码。
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, precision=None, scale=None):
        super().__init__(precision=precision, scale=scale)
        self._src_scale = scale

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        if not isinstance(value, Decimal):
            return value
        # 标度为 0：按整数绑定（精确，且 SQLite 的 NUMERIC 亲和性会存成 INTEGER）。
        # ⚠️ 不能用 float——`decimal(38,0)` 这类大整数会丢有效位（ID 一旦失真，
        #    关联查询会静默错配，属最危险的一类偏差）。
        if self._src_scale == 0:
            return int(value)
        # 标度未知：整数值走整数（如 NUMBER 型 ID），其余走浮点。
        if self._src_scale is None and value == value.to_integral_value():
            return int(value)
        return float(value)


@dataclass
class ColumnMeta:
    name: str
    src_type: str
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    nullable: bool = True


# ---------- 类型映射 ----------

def map_type(src_type: str, length: int | None = None,
             precision: int | None = None, scale: int | None = None):
    """源库类型 -> SQLAlchemy 通用类型（面向 SQLite 镜像）。"""
    t = (src_type or "").strip().upper()
    base = t.split("(")[0].strip()

    # 整数族
    if base in ("INT", "INTEGER", "INT4", "MEDIUMINT"):
        return Integer()
    if base in ("BIGINT", "INT8", "LONG"):
        return BigInteger()
    if base in ("SMALLINT", "INT2", "TINYINT"):
        return Integer()

    # 定点/浮点
    if base in ("NUMBER", "DECIMAL", "NUMERIC", "MONEY", "SMALLMONEY"):
        # ⚠️ 不再因为 scale=0 就映射成 BigInteger：pyodbc 对 decimal 列**始终返回 Decimal**
        #    （实测列 `Fee` = decimal(18,0) 返回的是 Decimal 而非 int），
        #    声明为 BigInteger 会失去 Decimal→float 的绑定转换，导致整表写入失败。
        # 精度/标度齐全时按原样声明（读回按源 scale 量化，与直连模式一致）；
        # 缺失时不要硬套默认 scale——那会把源值**四舍五入**成可见偏差。
        if precision and scale is not None:
            return MirrorDecimal(precision, scale)
        return MirrorDecimal()
    if base in ("FLOAT", "DOUBLE", "DOUBLE PRECISION", "REAL", "BINARY_DOUBLE", "BINARY_FLOAT"):
        return Float()

    # 文本族
    if base in ("VARCHAR", "VARCHAR2", "NVARCHAR", "NVARCHAR2", "CHAR", "NCHAR",
                "CLOB", "NCLOB", "TEXT", "NTEXT", "LONGVARCHAR", "LONG", "UNIQUEIDENTIFIER", "TIME"):
        return Text()

    # 日期时间族
    if base in ("DATE", "DATETIME", "DATETIME2", "SMALLDATETIME", "DATETIMEOFFSET", "TIMESTAMP"):
        return DateTime()

    # 布尔
    if base in ("BIT", "BOOLEAN", "BOOL"):
        return Boolean()

    # 二进制
    if base in ("BLOB", "IMAGE", "BINARY", "VARBINARY", "RAW", "LONGRAW", "BYTEA"):
        return LargeBinary()

    # 未识别：按文本处理（可读、不丢内容；如需精确类型再补映射）
    _ = length
    return Text()


# ==================== 索引（性能关键） ====================
# 镜像表默认无索引 → 页面里"年份过滤 + 多表 JOIN + 分组"的聚合查询会对百万行做全表扫描。
# 实测（未建索引时，纯镜像取数）：看板历史统计接口 **28.3 秒**、检出率 3.2 秒、检测项次 3.0 秒。
# 这里按"业务 SQL 实际用到的过滤/关联列"建索引。
#
# ⚠️ 配套改动：日期列建了索引，但 `EXTRACT(YEAR FROM x) = N` 这类写法**用不上**（函数包住了列），
#    因此翻译层同时把"年份等值"改写成日期区间（见 app/query/__init__.py），两者必须成对生效。
INDEX_SPECS: dict[str, list[tuple[str, ...]]] = {
    # 达梦：看板 / 人员能力 / 达梦页的过滤与关联列
    "DT_SAMPLE": [("CREATE_DATETIME",), ("DETECTION_ID",), ("SAMPLE_CATEGORY_ID",), ("DETECTION_NO",)],
    "DT_SAMPLE_PROJECT": [("SAMPLE_ID",), ("DECIDE_PROJECT_NAME",), ("SINGLE_JUDGE",)],
    "DT_RESULT_CHECK_IN": [("SAMPLE_PROJECT_ID",)],
    "DT_DETECTION": [("CONTRACTS_NO",), ("ACCEPT_TIME",)],
    "DT_SAMPLE_CATEGORY": [("ID",)],
    "DT_DETECTED_COMPANY": [("ID",)],
    "DT_EQUIPMENT_BILL": [("ID",)],
    # SQL Server：LIMS 历史统计（年份过滤 / 送检单去重 / 项目分组）
    # ⚠️ 这里刻意用**覆盖索引**（把查询用到的列都放进索引）：
    #    这两张表极宽（VIEW 126 列 / TResult 92 列），只建单列索引时，
    #    范围过滤虽能命中索引，但**仍要回表读整行**——实测扫 112 万行要 6~9 秒。
    #    覆盖索引只需扫索引本身（2 列），I/O 降一到两个数量级。
    "TResult": [("Submission_ID",), ("Date_Test", "Qualified"), ("Date_Test", "Item")],
    "VIEW_SubMisSampleResult": [
        ("Submission_ID",),
        ("Sample_Kind_I",),
        ("Cy_Date", "Submission_ID"),
        ("Cy_Date", "Sample_Kind_I"),
    ],
    "TResult_Data": [("Result_ID",)],
    "TBe_Checked": [("Be_Checked",)],
}


def ensure_indexes(conn, table: str) -> int:
    """为镜像表建索引（幂等，`CREATE INDEX IF NOT EXISTS`）。返回本次声明的索引数。

    在同步流程中调用；索引失败**不阻断同步**（例如源表列名与规格不一致时静默跳过）。
    """
    created = 0
    for cols in INDEX_SPECS.get(table, []):
        name = f"idx_{table}_{'_'.join(cols)}".lower()[:60]
        collist = ", ".join(f'"{c}"' for c in cols)
        try:
            conn.exec_driver_sql(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({collist})')
            created += 1
        except Exception:  # noqa: BLE001
            pass
    return created


def build_mirror_table(name: str, cols: list[ColumnMeta], md: MetaData) -> Table:
    """按列元数据构建镜像表（不带 schema，列顺序与源表一致）。"""
    columns = [
        Column(c.name, map_type(c.src_type, c.length, c.precision, c.scale), nullable=True)
        for c in cols
    ]
    return Table(name, md, *columns)


# ---------- 元数据读取 ----------

def read_dm_columns(engine: Engine, owner: str, table: str) -> list[ColumnMeta]:
    """读达梦列元数据（ALL_TAB_COLUMNS）。"""
    sql = text(
        "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE "
        "FROM ALL_TAB_COLUMNS WHERE OWNER = :owner AND TABLE_NAME = :table ORDER BY COLUMN_ID"
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"owner": owner, "table": table}).fetchall()
    out: list[ColumnMeta] = []
    for r in rows:
        out.append(ColumnMeta(
            name=str(r[0]).strip(),
            src_type=str(r[1] or "").strip(),
            length=int(r[2]) if r[2] is not None else None,
            precision=int(r[3]) if r[3] is not None else None,
            scale=int(r[4]) if r[4] is not None else None,
            nullable=str(r[5] or "Y").strip().upper() in ("Y", "YES", "TRUE", "1"),
        ))
    return out


def read_sql_columns(engine: Engine, table: str) -> tuple[str | None, list[ColumnMeta]]:
    """读 SQL Server 列元数据（INFORMATION_SCHEMA.COLUMNS），返回 (schema, columns)。"""
    sql = text(
        "SELECT TABLE_SCHEMA, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
        "NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :table ORDER BY ORDINAL_POSITION"
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"table": table}).fetchall()
    schema = str(rows[0][0]) if rows else None
    out: list[ColumnMeta] = []
    for r in rows:
        out.append(ColumnMeta(
            name=str(r[1]).strip(),
            src_type=str(r[2] or "").strip(),
            length=int(r[3]) if r[3] is not None else None,
            precision=int(r[4]) if r[4] is not None else None,
            scale=int(r[5]) if r[5] is not None else None,
            nullable=str(r[6] or "YES").strip().upper() in ("YES", "TRUE", "1"),
        ))
    return schema, out


def quote(name: str, kind: str) -> str:
    """标识符引用：达梦用双引号，SQL Server 用方括号。"""
    if kind == "sqlserver":
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'


def source_select_sql(kind: str, schema: str | None, table: str, cols: list[ColumnMeta]) -> str:
    """构造源侧读取 SQL（简单列读取，不做复杂编译）。"""
    col_sql = ", ".join(quote(c.name, kind) for c in cols)
    tbl_sql = (quote(schema, kind) + "." if schema else "") + quote(table, kind)
    return f"SELECT {col_sql} FROM {tbl_sql}"
