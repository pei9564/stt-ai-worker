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
    max_upload_bytes: int = 512 * 1024 * 1024
    max_audio_seconds: int = 7200
    max_pending_jobs: int = 20
    max_feed_episodes: int = 20
    max_batch_size: int = 16
    max_job_seconds: int = 7200
    vllm_base_url: str = ''
    vllm_model: str = ''
    vllm_api_key: str = ''
    llm_timeout_seconds: int = 120
    scraper_public_url: str = ''


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
        vllm_base_url=os.getenv('VLLM_BASE_URL', ''),
        vllm_model=os.getenv('VLLM_MODEL', ''),
        vllm_api_key=os.getenv('VLLM_API_KEY', ''),
        llm_timeout_seconds=_positive('LLM_TIMEOUT_SECONDS', 120),
        scraper_public_url=os.getenv('SCRAPER_PUBLIC_URL', ''),
        **{name: _positive(env, default) for name, env, default in (
            ('max_upload_bytes', 'MAX_UPLOAD_BYTES', 512 * 1024 * 1024),
            ('max_audio_seconds', 'MAX_AUDIO_SECONDS', 7200),
            ('max_pending_jobs', 'MAX_PENDING_JOBS', 20),
            ('max_feed_episodes', 'MAX_FEED_EPISODES', 20),
            ('max_batch_size', 'MAX_BATCH_SIZE', 16),
            ('max_job_seconds', 'MAX_JOB_SECONDS', 7200),
        )},
    )


def _positive(name: str, default: int) -> int:
    value = int(os.getenv(name, default))
    if value <= 0:
        raise ValueError(f'{name} must be positive')
    return value
