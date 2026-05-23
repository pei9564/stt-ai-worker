"""應用設定:集中從環境變數讀取,其他模組一律透過 Settings 取得設定。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """不可變的執行期設定。由 load_settings() 從環境變數建立。"""

    model: str
    device: str
    compute_type: str
    language: str | None
    batch_size: int
    db_url: str
    jobs_window_hours: int


def load_settings() -> Settings:
    """從環境變數組出 Settings;未設定的項目用合理預設值。"""
    return Settings(
        model=os.getenv("MODEL", "medium"),
        device=os.getenv("DEVICE", "cuda"),
        compute_type=os.getenv("COMPUTE_TYPE", "int8"),
        language=(os.getenv("LANGUAGE") or "").strip() or None,
        batch_size=int(os.getenv("BATCH_SIZE", "4")),
        db_url=os.environ["DB_URL"],
        jobs_window_hours=int(os.getenv("JOBS_WINDOW_HOURS", "48")),
    )
