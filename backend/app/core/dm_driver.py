"""达梦驱动引导：**必须在 import dmPython 之前**把驱动自带 DLL 目录加入搜索路径。

原因（W-3 实测结论）：dmPython 2.5.38 的 wheel 自带 dmdpi/dmcalc/... 等 DLL，
但加密模块依赖位于 `<site-packages>/dmssl/` 下的 libeay32.dll / ssleay32.dll；
不在默认 DLL 搜索路径时会报：
    dmPython.DatabaseError: [CODE:-70089]加密模块加载失败
把 `<site-packages>` 与 `<site-packages>/dmssl` 加入搜索路径后连接正常。

另注：dmPython 的 connect() **不支持** loginEncrypt 参数（传入会抛
`SystemError: ... returned a result with an exception set`），不要沿用 Node 侧写法。
"""
from __future__ import annotations

import os
import site
import sys
from pathlib import Path

_dll_dirs: list[str] = []
_bootstrapped = False


def _candidate_dirs() -> list[str]:
    dirs: list[str] = []
    try:
        for p in site.getsitepackages():
            dirs.append(p)
            dirs.append(str(Path(p) / "dmssl"))
    except Exception:  # pragma: no cover - 极端环境下 site 不可用
        pass
    for p in (
        Path(sys.prefix) / "Lib" / "site-packages",
        Path(sys.prefix) / "lib" / "site-packages",
    ):
        dirs.append(str(p))
        dirs.append(str(p / "dmssl"))
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def bootstrap() -> list[str]:
    """把达梦驱动所需目录加入 DLL 搜索路径（幂等）。返回已添加的目录。"""
    global _bootstrapped
    if _bootstrapped:
        return _dll_dirs
    dirs = _candidate_dirs()
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except Exception:  # pragma: no cover
            pass
        _dll_dirs.append(d)
    # PATH 兜底：部分间接加载不经过 add_dll_directory
    os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
    _bootstrapped = True
    return _dll_dirs


bootstrap()

_import_error: Exception | None = None
try:  # noqa: SIM105
    import dmPython  # type: ignore  # noqa: E402
except Exception as exc:  # pragma: no cover
    dmPython = None  # type: ignore[assignment]
    _import_error = exc


def get_driver():
    """取得 dmPython 模块；不可用时抛带上下文的异常。"""
    if dmPython is None:
        raise RuntimeError(
            f"达梦驱动 dmPython 导入失败：{_import_error}；"
            f"已尝试的 DLL 目录：{_dll_dirs}"
        )
    return dmPython


def dll_dirs() -> list[str]:
    return list(_dll_dirs)
