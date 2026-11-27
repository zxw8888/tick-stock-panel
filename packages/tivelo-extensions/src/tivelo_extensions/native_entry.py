"""Load an already authenticated native entry in a dedicated worker process."""

import importlib.util
import os
import re
from pathlib import Path


def _loadable_path(path: Path) -> str:
    r"""Windows 的 LoadLibraryExW 不接受超 MAX_PATH 的普通路径。

    版本目录用完整 sha256 命名 (research/engine/extensions/versions/<digest>),
    叠加用户数据目录后 .pyd 路径可能越过 260 字符, 加载会报
    "文件名或扩展名太长" (WinError 206)。扩展前缀路径由 LoadLibraryExW 原样接受,
    所以超长时给绝对路径加 \\?\ 前缀; 普通长度保持原样 (前缀会关闭路径规范化)。
    """
    text = str(path)
    if os.name != "nt":
        return text
    absolute = os.path.abspath(text)
    if len(absolute) < 250:
        return text
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def load_native_entry(path: Path):
    """The caller must verify the release, inventory, platform and digest first."""
    name = path.name.split(".", 1)[0]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or path.suffix not in (".so", ".pyd"):
        raise ValueError("Invalid native entry name")
    spec = importlib.util.spec_from_file_location(name, _loadable_path(path))
    if spec is None or spec.loader is None:
        raise ValueError("Python cannot load the native entry")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
