"""集中配置：环境变量 / .env 驱动，启动时校验关键项（fail fast）。

安全约定：数据库账号只允许出现在 backend/.env，代码内不硬编码任何口令。
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> parents[3] = 工程根（AI工具合集）
ROOT_DIR = Path(__file__).resolve().parents[3]
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"
LOGS_DIR = ROOT_DIR / "_logs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "AI工具合集"
    port: int = 8080

    # ---- 后台定时任务总开关 ----
    # 生产实例（:8080）保持 True。
    # 开发/调试实例（如另起 :8090 只调某个模块）必须设为 False：
    # 否则两个进程会各自跑镜像同步与完成量重算，重复搬数、互相抢库。
    # 在环境变量里设 SCHEDULER_ENABLED=false 即可覆盖，不用改 .env。
    scheduler_enabled: bool = True

    # ---- 达梦 ----
    dm_host: str = "127.0.0.1"
    dm_port: int = 5236
    dm_user: str = ""
    dm_password: str = ""
    dm_schema: str = "DETECTION"
    dm_pool_min: int = 1
    dm_pool_max: int = 6

    # ---- SQL Server ----
    sql_server: str = "127.0.0.1"
    sql_port: int = 1433
    sql_database: str = ""
    sql_user: str = ""
    sql_password: str = ""
    sql_odbc_driver: str = "SQL Server"
    sql_timeout: int = 30

    # ---- 查询 ----
    query_timeout: float = 60.0

    # ---- 数据来源模式 ----
    # mirror（默认）：业务查询走 SQLite 镜像（毫秒级、部署机不需要达梦驱动）；
    # direct        ：走源库直连（原路径，用于核对最新数据与调试）。
    # mirror 不可用时（无快照 / SQL 不可翻译）自动回退 direct，并在响应里带出原因。
    data_mode: str = "mirror"

    # ---- SQLite 镜像 ----
    # ⚠️ 镜像需要约 1.5~2 GB（快照本身 + 切换时的瞬时副本）。
    # 本机 C: 盘已满（可用 0），因此 MIRROR_DIR 必须指向其他盘（如 F:/aitools-mirror），
    # 留空时才回退到 <backend>/data/mirror。
    mirror_enabled: bool = False
    mirror_dir: str = ""
    mirror_keep: int = 10
    mirror_batch: int = 10000
    # 每次同步要重搬的来源（逗号分隔）。
    # "dm"：只重搬达梦，SQL Server 表以旧快照为底保留（数据已冻结，不重搬）——约 1 分钟；
    # "dm,sqlserver"：全量重搬（约 8 分钟）。SQL Server 数据若真的变了才需要改回这个。
    mirror_sync_sources: str = "dm"
    # 定时同步时刻（本地时间 HH:MM），留空关闭定时同步。
    # 镜像模式下这是数据新鲜度的唯一来源——应用层不连源库，全靠这里定期搬数。
    mirror_sync_at: str = "19:00"

    # ---- 一单一库核对（CMA） ----
    cma_base: str = "https://cma.caqit.org.cn/cma-admin/system/standardData/list"
    cma_timeout: float = 20.0
    cma_retry: int = 3
    cma_retry_interval: float = 1.0
    cma_concurrency: int = 5

    # ---- CMA 核对台账（留痕） ----
    # 每次核对都留痕入库（批次 + 明细），供资质认定"能力持续符合/体系持续有效运行"取证。
    # 默认开启；目录留空时落到 <backend>/data/cma_ledger。
    cma_ledger_enabled: bool = True
    cma_ledger_dir: str = ""

    # ---- 人员能力表：上传的《检验检测能力表》 ----
    # 页面上传的能力表落本地 SQLite，用于比对人员登记的「项目 + 方法」是否在能力范围内。
    # 目录留空时落到 <backend>/data/personnel（与 cma_ledger_dir 同一种写法）。
    personnel_ability_dir: str = ""

    # ---- 抽采样进度统计 ----
    # 产品类型配置 / 合同 / 任务量 / 完成量快照 / 同步日志统一落本地 SQLite。
    # 目录留空时落到 <backend>/data/progress（与 cma_ledger_dir 同一种写法）。
    progress_dir: str = ""
    # 完成量定时重算时刻（本地时间 HH:MM）。默认 19:40——**必须晚于镜像同步 19:00**，
    # 否则每天重算的仍是昨天的快照。留空 = 只允许手动同步。
    progress_sync_at: str = "19:40"

    def validate_required(self) -> None:
        """启动时校验必填配置，缺失即抛错（fail fast）。"""
        missing: list[str] = []
        if not self.dm_host:
            missing.append("DM_HOST")
        if not self.dm_user:
            missing.append("DM_USER")
        if not self.dm_password:
            missing.append("DM_PASSWORD")
        if not self.sql_server:
            missing.append("SQL_SERVER")
        if not self.sql_database:
            missing.append("SQL_DATABASE")
        if not self.sql_user:
            missing.append("SQL_USER")
        if not self.sql_password:
            missing.append("SQL_PASSWORD")
        if missing:
            raise RuntimeError(
                f"缺少必填配置：{', '.join(missing)}（请在 {BACKEND_DIR / '.env'} 中配置，参考 .env.example）"
            )


settings = Settings()
