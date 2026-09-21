"""AI工具合集 统一后端（FastAPI）。

分层：
- app.core      配置 / 数据访问 / 只读校验 / 异常 / 序列化
- app.modules   六个业务模块（nav / dm / sqlserver / board / personnel / cma）
- app.main      应用入口（:8080，挂载各模块路由 + 托管 frontend/dist）
"""
