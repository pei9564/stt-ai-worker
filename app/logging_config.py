"""統一的 logging 設定，沿用原本 `[stt] HH:MM:SS msg` 的輸出格式。"""

from __future__ import annotations

import logging


def configure_logging() -> None:
    """設定 root logger。應在程式啟動時呼叫一次。"""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[stt] %(asctime)s %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
