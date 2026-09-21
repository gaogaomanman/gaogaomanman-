"""统一异常类型：供路由层映射为 HTTP 状态码，同时保持旧实现的错误文案风格。"""
from __future__ import annotations


class DbError(Exception):
    """数据访问失败（连接失败 / 查询失败）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ReadOnlyError(DbError):
    """只读 SQL 校验未通过（对应旧实现的"只允许查询"拦截）。"""


class ExternalError(Exception):
    """外部接口调用失败（如 CMA 平台）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
