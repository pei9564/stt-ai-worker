"""輸出檔名的清理與去重。純函式。"""

from __future__ import annotations

import pathlib
import re

_UNSAFE = re.compile(r"[^\w一-鿿 -]")


def safe_stem(name: str) -> str:
    """把任意檔名 / 標題清成安全的檔名主幹。

    保留中英數字、空白、連字號，其餘換成底線；長度上限 80；空字串回 "audio"。
    """
    stem = pathlib.Path(name).stem
    stem = _UNSAFE.sub("_", stem).strip()
    return stem[:80] or "audio"


def unique_stem(directory: pathlib.Path, base: str, suffix: str = ".txt") -> str:
    """在 directory 內找一個不會撞檔的主幹：base, base_2, base_3 ...。"""
    stem, n = base, 2
    while (directory / f"{stem}{suffix}").exists():
        stem = f"{base}_{n}"
        n += 1
    return stem
