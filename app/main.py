"""組裝根 (composition root)：把各層的元件接起來，建立 FastAPI app。

`create_app()` 是工廠函式，可注入假的 transcriber、關掉 worker，方便測試。
正式啟動的進入點在 app/asgi.py。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.api.web import WebUI
from app.config import Settings, load_settings
from app.container import Container
from app.domain.ports import Transcriber
from app.infrastructure.output_writer import OutputWriter
from app.infrastructure.repository import JsonFileJobRepository
from app.logging_config import configure_logging
from app.services.job_service import JobService
from app.services.transcription import TranscriptionService
from app.services.worker import TranscriptionWorker

log = logging.getLogger(__name__)

_DESCRIPTION = (
    "語音轉文字服務。所有轉檔請求 (檔案 / YouTube 連結 / RSS) 都會排進單一佇列，"
    "由一個 worker 依序處理，不會因為「使用中」而被拒絕。\n\n"
    "**使用流程**：POST 任一轉檔端點 → 取得 `job_id` → 輪詢 `GET /job/{job_id}` "
    "直到 `status` 變成 `done`，即可從 `text` 取得逐字稿。逐字稿同時會存到 "
    "`output/{日期}/{topic}.txt`。"
)


def create_app(
    settings: Settings | None = None,
    *,
    transcriber: Transcriber | None = None,
    run_worker: bool = True,
) -> FastAPI:
    """組裝並回傳 FastAPI app。

    settings   : 省略則由環境變數載入。
    transcriber: 省略則建立 WhisperTranscriber (會載入模型)；測試可注入假物件。
    run_worker : 是否在 lifespan 啟動背景 worker thread。
    """
    configure_logging()
    settings = settings or load_settings()
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    repo = JsonFileJobRepository(settings.jobs_dir, settings.max_jobs)
    job_service = JobService(repo, jobs_window_hours=settings.jobs_window_hours)

    if transcriber is None:
        from app.infrastructure.transcriber import WhisperTranscriber

        transcriber = WhisperTranscriber(
            settings.model, settings.device, settings.compute_type
        )

    transcription_service = TranscriptionService(
        transcriber,
        OutputWriter(settings.output_dir),
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
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loaded = repo.load()
        if loaded:
            log.info("載入 %d 筆歷史 job 紀錄", loaded)
        job_service.recover_interrupted()
        if run_worker:
            worker.start()
        yield
        if run_worker:
            worker.stop()

    app = FastAPI(
        title="STT Service",
        version="2.0.0",
        description=_DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.container = container
    app.include_router(router)
    return app
