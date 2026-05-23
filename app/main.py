"""組裝根 (composition root):把各層的元件接起來,建立 FastAPI app。

`create_app()` 是工廠函式,可注入假的 transcriber / repository、關掉 worker,方便測試。
正式啟動的進入點在 app/asgi.py。
"""

from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from app.api.routes import router
from app.api.web import WebUI
from app.config import Settings, load_settings
from app.container import Container
from app.domain.ports import JobRepository, Transcriber, TranscriptRepository
from app.infrastructure.pg_job_repository import PostgresJobRepository
from app.infrastructure.transcript_repository import PostgresTranscriptRepository
from app.logging_config import configure_logging
from app.services.job_service import JobService
from app.services.transcription import TranscriptionService
from app.services.worker import TranscriptionWorker

log = logging.getLogger(__name__)

_SCHEMA_PATH = pathlib.Path(__file__).parent / "infrastructure" / "schema.sql"

_DESCRIPTION = (
    "語音轉文字服務。所有轉檔請求 (YouTube 連結 / RSS) 都會排進單一佇列,"
    "由一個 worker 依序處理,不會因為「使用中」而被拒絕。\n\n"
    "**使用流程**:POST 任一轉檔端點 → 取得 `job_id` → 輪詢 `GET /job/{job_id}` "
    "直到 `status` 變成 `done`,即可從 `text` 取得逐字稿。逐字稿同時會存進 "
    "PG 的 `transcripts` 表,可用 `transcript_id` 引用。"
)


def _apply_schema(pool: ConnectionPool) -> None:
    """執行 schema.sql。idempotent — 多次執行不會出錯。"""
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    with pool.connection() as conn:
        conn.execute(sql)


def create_app(
    settings: Settings | None = None,
    *,
    transcriber: Transcriber | None = None,
    job_repository: JobRepository | None = None,
    transcript_repository: TranscriptRepository | None = None,
    run_worker: bool = True,
) -> FastAPI:
    """組裝並回傳 FastAPI app。

    settings              : 省略則由環境變數載入。
    transcriber           : 省略則建立 WhisperTranscriber (會載入模型);測試可注入假物件。
    job_repository        : 省略則用 PostgresJobRepository;測試可注入 InMemory 版。
    transcript_repository : 省略則用 PostgresTranscriptRepository;測試可注入 InMemory 版。
    run_worker            : 是否在 lifespan 啟動背景 worker thread。
    """
    configure_logging()
    settings = settings or load_settings()

    # 只在「兩個 repo 都沒被注入」時才開 PG pool;測試路徑完全不碰 PG。
    pool: ConnectionPool | None = None
    if job_repository is None or transcript_repository is None:
        pool = ConnectionPool(settings.db_url, min_size=1, max_size=4, open=False)

    if job_repository is None:
        job_repository = PostgresJobRepository(pool)  # type: ignore[arg-type]
    if transcript_repository is None:
        transcript_repository = PostgresTranscriptRepository(pool)  # type: ignore[arg-type]

    job_service = JobService(job_repository, jobs_window_hours=settings.jobs_window_hours)

    if transcriber is None:
        from app.infrastructure.transcriber import WhisperTranscriber

        transcriber = WhisperTranscriber(
            settings.model, settings.device, settings.compute_type
        )

    transcription_service = TranscriptionService(
        transcriber,
        transcript_repository,
        default_language=settings.language,
        default_batch_size=settings.batch_size,
    )
    worker = TranscriptionWorker(job_service, transcription_service)

    container = Container(
        settings=settings,
        web_ui=WebUI(settings),
        job_service=job_service,
        transcription_service=transcription_service,
        worker=worker,
        transcripts=transcript_repository,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if pool is not None:
            pool.open()
            _apply_schema(pool)
            log.info("PG schema applied")
        job_service.recover_interrupted()
        if run_worker:
            worker.start()
        yield
        if run_worker:
            worker.stop()
        if pool is not None:
            pool.close()

    app = FastAPI(
        title="STT Service",
        version="2.0.0",
        description=_DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.container = container
    app.include_router(router)
    return app
