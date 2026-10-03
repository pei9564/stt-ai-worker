"""組裝根 (composition root):把各層的元件接起來,建立 FastAPI app。

`create_app()` 是工廠函式,可注入假的 transcriber / repository、關掉 worker,方便測試。
正式啟動的進入點在 app/asgi.py。
"""

from __future__ import annotations

import logging
import asyncio
import os
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool

from app.api.routes import router
from app.api.web import WebUI
from app.config import Settings, load_settings
from app.container import Container
from app.domain.ports import JobRepository, Transcriber, TranscriptRepository
from app.infrastructure.pg_job_repository import PostgresJobRepository
from app.infrastructure.transcript_repository import PostgresTranscriptRepository
from app.logging_config import configure_logging
from app.observability import Observability
from app.services.job_service import JobService, QueueFullError
from app.services.transcription import TranscriptionService
from app.services.worker import TranscriptionWorker

log = logging.getLogger(__name__)

_SCHEMA_PATH = pathlib.Path(__file__).parent / "infrastructure" / "schema.sql"

_DESCRIPTION = (
    "語音轉文字服務。所有轉檔請求 (YouTube 連結 / RSS) 都會排進單一佇列,"
    "由一個 worker 依序處理,佇列滿時回傳 429。\n\n"
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
    observer = Observability('stt')
    settings = settings or load_settings()

    # 只在「兩個 repo 都沒被注入」時才開 PG pool;測試路徑完全不碰 PG。
    pool: ConnectionPool | None = None
    if job_repository is None or transcript_repository is None:
        pool = ConnectionPool(
            settings.db_url, min_size=1, max_size=4, timeout=3,
            kwargs={"connect_timeout": 3, "options": "-c statement_timeout=3000"},
            open=False,
        )

    if job_repository is None:
        job_repository = PostgresJobRepository(pool)  # type: ignore[arg-type]
    if transcript_repository is None:
        transcript_repository = PostgresTranscriptRepository(pool)  # type: ignore[arg-type]

    job_service = JobService(job_repository, jobs_window_hours=settings.jobs_window_hours,
                             max_pending_jobs=settings.max_pending_jobs,
                             profile=f"{settings.model}|{settings.compute_type}|{settings.language}|{settings.batch_size}",
                             model_config={'model': settings.model, 'compute_type': settings.compute_type,
                                           'default_language': settings.language,
                                           'default_batch_size': settings.batch_size})
    audio_probe = None

    if transcriber is None:
        from app.infrastructure.limits import probe_duration
        audio_probe = probe_duration
        from app.infrastructure.transcriber import WhisperTranscriber

        transcriber = WhisperTranscriber(
            settings.model, settings.device, settings.compute_type
        )

    transcription_service = TranscriptionService(
        transcriber,
        transcript_repository,
        default_language=settings.language,
        default_batch_size=settings.batch_size,
        audio_probe=audio_probe,
        max_audio_seconds=settings.max_audio_seconds,
        max_job_seconds=settings.max_job_seconds,
    )
    worker = TranscriptionWorker(job_service, transcription_service, max_job_seconds=settings.max_job_seconds, observer=observer)

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
        observer.start()
        job_service.recover_interrupted()
        if run_worker:
            worker.start()
        async def monitor():
            while True:
                await asyncio.sleep(60)
                observer.disk(os.getenv('UPLOAD_DIR', '/data/uploads'))
                observer.disk('/tmp')
                if run_worker and not worker.is_alive:
                    observer.alert('worker_unavailable')
                try:
                    await asyncio.to_thread(job_service.get, '__monitor__')
                except Exception:
                    observer.alert('database_unavailable')
        monitor_task = asyncio.create_task(monitor())
        yield
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        observer.stop()
        if run_worker:
            worker.stop()
        if pool is not None and not worker.is_alive:
            pool.close()
        elif pool is not None:
            # 長任務尚在執行時不要提早關閉它使用的連線池。
            # daemon thread 隨 process 終止；未完成任務由下一次啟動恢復。
            log.warning("worker still running at shutdown; leaving pool open until process exit")

    app = FastAPI(
        title="STT Service",
        version="2.0.0",
        description=_DESCRIPTION,
        lifespan=lifespan,
    )
    @app.exception_handler(QueueFullError)
    async def full_queue(request: Request, exc: QueueFullError):
        return JSONResponse(status_code=429, content={'detail': str(exc)}, headers={'Retry-After': '30'})

    @app.middleware('http')
    async def upload_header_limit(request: Request, call_next):
        length = request.headers.get('content-length')
        if length:
            try:
                if int(length) > settings.max_upload_bytes + 1024 * 1024:
                    return JSONResponse(status_code=413, content={'detail': '上傳超過大小限制'})
            except ValueError:
                return JSONResponse(status_code=400, content={'detail': '無效的 Content-Length'})
        return await call_next(request)

    from app.infrastructure.limits import BodyLimitMiddleware
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_upload_bytes + 1024 * 1024)
    app.state.observer = observer
    observer.attach(app)
    from fastapi.responses import Response
    @app.get('/metrics')
    def metrics():
        disk = observer.disk(os.getenv('UPLOAD_DIR', '/data/uploads'))
        return Response(observer.render({'queued_jobs': job_service.queue_size,
                                         'worker_up': worker.is_alive,
                                         'uploads_disk_free_bytes': disk['free_bytes']}),
                        media_type='text/plain; version=0.0.4')

    @app.get('/diagnostics')
    def diagnostics():
        healthy = worker.is_alive
        if not healthy:
            observer.alert('worker_unavailable')
        return {'worker_up': healthy, 'queued_jobs': job_service.queue_size,
                'uploads_disk': observer.disk(os.getenv('UPLOAD_DIR', '/data/uploads')),
                'temporary_disk': observer.disk('/tmp'), 'notifications_enabled': bool(observer.webhook)}

    app.state.container = container
    app.include_router(router)
    return app
