"""抽采样进度统计模块（/progress）。

- `store.py`    ：本地 SQLite 存储层（唯一写库的地方）
- `service.py`  ：镜像聚合与统计组装（唯一读数据源的地方）
- `routes.py`   ：HTTP 接口（不直接碰数据源与库）
- `normalize.py`：文本归一与区县解析（纯函数口径）
"""
