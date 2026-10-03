"""相依組裝的容器：把所有服務集中存放，供路由透過 app.state 取用。"""

from __future__ import annotations

from dataclasses import dataclass

from app.api.web import WebUI
from app.config import Settings
from app.services.notes import NotesService
from app.domain.ports import TranscriptRepository
from app.services.job_service import JobService
from app.services.transcription import TranscriptionService
from app.services.worker import TranscriptionWorker


@dataclass
class Container:
    """一次請求生命週期共用的所有元件。由 create_app() 組裝。"""

    settings: Settings
    web_ui: WebUI
    job_service: JobService
    transcription_service: TranscriptionService
    worker: TranscriptionWorker
    transcripts: TranscriptRepository
    notes: NotesService
