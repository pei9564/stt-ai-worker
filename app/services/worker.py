"""背景 worker：單一 thread 依序處理佇列中的 job，序列化 GPU 存取。"""

from __future__ import annotations

import logging
import threading
import time

from app.services.errors import describe_error

from app.domain.job import JobStatus
from app.services.job_service import JobService
from app.services.transcription import TranscriptionService

log = logging.getLogger(__name__)


class TranscriptionWorker:
    """從 JobService 的佇列取 job，交給 TranscriptionService 處理。"""

    def __init__(self, job_service: JobService, transcription_service: TranscriptionService, *, max_job_seconds: int = 7200, observer=None):
        self._observer = observer
        self._max_job_seconds = max_job_seconds
        self._timed_out = threading.Event()
        self._jobs = job_service
        self._transcription = transcription_service
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._timed_out.is_set()

    def start(self) -> None:
        """啟動 worker thread。"""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="stt-ai-worker")
        self._thread.start()

    def stop(self) -> None:
        """通知 worker 結束並等待 thread 收尾。"""
        self._stopping.set()
        self._jobs.request_shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        log.info("worker thread started")
        while not self._stopping.is_set():
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
        started = time.monotonic()
        self._jobs.mark_running(job_id)
        log.info("job %s 開始 (%s)", job_id, job.type.value)
        def expired():
            try:
                if self._jobs.expire(job_id):
                    if self._observer:
                        self._observer.alert("job_timeout", job_id=job_id)
                    self._timed_out.set()
                    self._stopping.set()
            except Exception:
                self._timed_out.set()
                self._stopping.set()
                log.exception('job %s timeout persistence failed', job_id)
        watchdog = threading.Timer(self._max_job_seconds, expired)
        watchdog.daemon = True
        watchdog.start()
        try:
            last = [None, -10.0, -1]
            def report(stage, progress=None):
                now = time.monotonic()
                if stage != last[0] or progress is not None and (now - last[1] >= 5 or progress - last[2] >= 5):
                    self._jobs.update_progress(job_id, stage, progress)
                    last[:] = [stage, now, progress or 0]
            result = self._transcription.process(job, on_stage=report)
            if self._timed_out.is_set():
                return
            watchdog.cancel()
            self._jobs.mark_done(job_id, result)
            log.info("job %s 完成 → transcript #%d", job_id, result.transcript_id)
            if job.source is not None:
                try:
                    job.source.cleanup()
                except OSError:
                    log.exception("job %s 音檔清理失敗", job_id)
        except Exception as e:
            code, message, hint = describe_error(e)
            self._jobs.mark_error(job_id, message, code=code, hint=hint)
            if self._observer:
                self._observer.alert(code, job_id=job_id)
            log.error("job %s 失敗: %s", job_id, e)

        finally:
            watchdog.cancel()
            elapsed = time.monotonic() - started
            final_job = self._jobs.get(job_id)
            final_status = final_job.status.value if final_job else 'deleted'
            if self._observer:
                self._observer.record_operation(final_status, elapsed)
            log.info('job_id=%s request_id=%s status=%s duration_seconds=%.3f', job_id, job.details.get('request_id', ''), final_status, elapsed)
