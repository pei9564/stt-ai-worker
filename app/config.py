"""應用設定：集中從環境變數讀取，其他模組一律透過 Settings 取得設定。"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """不可變的執行期設定。由 load_settings() 從環境變數建立。"""

    model: str
    device: str
    compute_type: str
    language: str | None
    batch_size: int
    output_dir: pathlib.Path
    max_jobs: int
    jobs_window_hours: int

    @property
    def jobs_dir(self) -> pathlib.Path:
        """job 紀錄持久化目錄 (每個 job 一個 JSON)。"""
        return self.output_dir / ".jobs"


def load_settings() -> Settings:
    """從環境變數組出 Settings；未設定的項目用合理預設值。"""
    return Settings(
        model=os.getenv("MODEL", "medium"),
        device=os.getenv("DEVICE", "cuda"),
        compute_type=os.getenv("COMPUTE_TYPE", "int8"),
        language=(os.getenv("LANGUAGE") or "").strip() or None,
        batch_size=int(os.getenv("BATCH_SIZE", "4")),
        output_dir=pathlib.Path(os.getenv("OUTPUT_DIR", "/data/output")),
        max_jobs=int(os.getenv("MAX_JOBS", "200")),
        jobs_window_hours=int(os.getenv("JOBS_WINDOW_HOURS", "48")),
    )
