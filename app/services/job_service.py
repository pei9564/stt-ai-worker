"""Job 的應用服務：建立、查詢、狀態轉移，並持有轉檔佇列。

所有對 JobRepository 的存取都在此處用一把鎖序列化，因為 FastAPI 的同步路由
與 worker thread 會並行存取。
"""

from __future__ import annotations

import datetime
import logging
import queue
import threading
import uuid

from app.domain.audio_source import AudioSource
from app.domain.job import Job, JobResult, JobStatus, JobType
from app.domain.ports import JobRepository

log = logging.getLogger(__name__)


class JobService:
    """job 生命週期的單一進入點。"""

    def __init__(self, repo: JobRepository, *, jobs_window_hours: int):
        self._repo = repo
        self._window_hours = jobs_window_hours
        self._lock = threading.Lock()
        self._queue: "queue.Queue[str | None]" = queue.Queue()

    # ---- 建立 / 查詢 ----
    def create(
        self,
        *,
        job_type: JobType,
        source: AudioSource,
        topic: str | None,
        language: str | None,
        batch_size: int,
    ) -> Job:
        """建立一筆 job、持久化、排進佇列，回傳 Job。"""
        job = Job(
            id=uuid.uuid4().hex[:12],
            type=job_type,
            topic=topic or None,
            language=language or None,
            batch_size=batch_size,
            source=source,
        )
        with self._lock:
            self._repo.save(job)
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._repo.get(job_id)

    def list_recent(self, hours: int | None = None) -> list[Job]:
        """回傳時間窗內的 job，最新在前。"""
        window = self._window_hours if hours is None else hours
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=window)
        with self._lock:
            jobs = [j for j in self._repo.all() if j.created_at >= cutoff]
        jobs.reverse()
        return jobs

    # ---- 狀態轉移 (worker thread 使用) ----
    def mark_running(self, job_id: str) -> None:
        self._transition(job_id, lambda job: job.mark_running())

    def mark_done(self, job_id: str, result: JobResult) -> None:
        self._transition(job_id, lambda job: job.mark_done(result))

    def mark_error(self, job_id: str, message: str) -> None:
        self._transition(job_id, lambda job: job.mark_error(message))

    def _transition(self, job_id: str, mutate) -> None:
        with self._lock:
            job = self._repo.get(job_id)
            if job is None:
                return
            mutate(job)
            self._repo.save(job)

    def recover_interrupted(self) -> None:
        """把載入時仍為 queued / running 的 job 標記為 error (服務重啟未完成)。"""
        with self._lock:
            for job in self._repo.all():
                if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                    job.mark_error("服務重啟，job 未完成")
                    self._repo.save(job)

    # ---- 佇列 ----
    def dequeue(self) -> str | None:
        """阻塞取出下一個 job_id；取到 None 代表收到關閉訊號。"""
        return self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    def request_shutdown(self) -> None:
        """通知 worker 結束。"""
        self._queue.put(None)

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()
