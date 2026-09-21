"""SQLite 镜像层：把达梦与 SQL Server 的数据同步到本地 SQLite，供依赖数据库的模块取数。

模块结构：
- `store.py`  快照存储与读取引擎（current.sqlite 原子切换、历史快照保留、sync_meta）
- `sync.py`   全量同步器（反射建表、分批复制、元信息回写）

配置（环境变量）：`MIRROR_ENABLED` / `MIRROR_DIR` / `MIRROR_KEEP` / `MIRROR_BATCH`
"""
