"""topic / 顯示名稱的清理。純函式。"""

from __future__ import annotations

import pathlib
import re

_UNSAFE = re.compile(r"[^\w一-鿿 -]")


def safe_stem(name: str) -> str:
    """把任意檔名 / 標題清成安全的識別字串。

    保留中英數字、空白、連字號,其餘換成底線;長度上限 80;空字串回 "audio"。
    用於 transcripts.topic 與顯示用途。
    """
    stem = pathlib.Path(name).stem
    stem = _UNSAFE.sub("_", stem).strip()
    return stem[:80] or "audio"
