"""背景 worker：單一 thread 依序處理佇列中的 job，序列化 GPU 存取。"""

from __future__ import annotations

import logging
import threading

from app.domain.job import JobStatus
from app.services.job_service import JobService
from app.services.transcription import TranscriptionService

log = logging.getLogger(__name__)


class TranscriptionWorker:
    """從 JobService 的佇列取 job，交給 TranscriptionService 處理。"""

    def __init__(self, job_service: JobService, transcription_service: TranscriptionService):
        self._jobs = job_service
        self._transcription = transcription_service
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """啟動 worker thread。"""
        self._thread = threading.Thread(target=self._loop, daemon=True, name="stt-ai-worker")
        self._thread.start()

    def stop(self) -> None:
        """通知 worker 結束並等待 thread 收尾。"""
        self._jobs.request_shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        log.info("worker thread started")
        while True:
            job_id = self._jobs.dequeue()
            try:
                if job_id is None:  # 關閉訊號
                    break
                self._process(job_id)
            finally:
                self._jobs.task_done()
        log.info("worker thread stopped")

    def _process(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.status != JobStatus.QUEUED:
            return  # 已取消或重複處理，跳過
        self._jobs.mark_running(job_id)
        log.info("job %s 開始 (%s)", job_id, job.type.value)
        try:
            result = self._transcription.process(job)
            self._jobs.mark_done(job_id, result)
            log.info("job %s 完成 → transcript #%d", job_id, result.transcript_id)
        except Exception as e:
            self._jobs.mark_error(job_id, str(e))
            log.error("job %s 失敗: %s", job_id, e)
