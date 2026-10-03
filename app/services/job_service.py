"""Job 的應用服務：建立、查詢、狀態轉移，並持有轉檔佇列。

所有對 JobRepository 的存取都在此處用一把鎖序列化，因為 FastAPI 的同步路由
與 worker thread 會並行存取。
"""

from __future__ import annotations

import datetime
import logging
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import queue
import threading
import uuid

from app.domain.audio_source import AudioSource
from app.observability import request_id_var
from app.domain.job import Job, JobResult, JobStatus, JobType
from app.domain.ports import JobRepository

log = logging.getLogger(__name__)


class QueueFullError(ValueError):
    pass


class JobService:
    """job 生命週期的單一進入點。"""

    def __init__(self, repo: JobRepository, *, jobs_window_hours: int, max_pending_jobs: int = 20, profile: str = "default", model_config: dict | None = None):
        self._model_config = model_config or {}
        self._profile = profile
        self._repo = repo
        self._window_hours = jobs_window_hours
        self._max_pending = max_pending_jobs
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
        return self.create_many([dict(type=job_type, source=source, topic=topic or None,
                                      language=language or None, batch_size=batch_size)])[0]

    def _key(self, spec: dict) -> str | None:
        from app.infrastructure.audio_sources import serialize_source
        try:
            data = serialize_source(spec['source'])
        except ValueError:
            return None  # injected test/custom sources have no stable identity
        if not data:
            return None
        if data['kind'] == 'file':
            identity = data.get('sha256')
            if not identity:
                return None
        else:
            parts = urlsplit(data['url'])
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                     if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
            identity = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                                  urlencode(sorted(query)), ''))
        payload = [data['kind'], identity, spec['language'], spec['batch_size'], spec['topic'], self._profile]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    def create_many(self, specs: list[dict]) -> list[Job]:
        with self._lock:
            existing = {j.details.get('dedupe_key'): j for j in self._repo.all()
                        if (j.status in (JobStatus.QUEUED, JobStatus.RUNNING) or
                            j.status == JobStatus.DONE and j.result is not None) and j.details.get('dedupe_key')}
            accepted, fresh, redundant = [], [], []
            for spec in specs:
                key = self._key(spec)
                if key and key in existing:
                    accepted.append(existing[key])
                    if spec['source'] is not existing[key].source:
                        redundant.append(spec['source'])
                    continue
                job = Job(id=uuid.uuid4().hex[:12],
                          details={'request_id': request_id_var.get(), 'dedupe_key': key,
                                   'model_profile': self._profile, 'model_config': self._model_config}, **spec)
                fresh.append(job)
                accepted.append(job)
                if key:
                    existing[key] = job
            self._require_capacity(len(fresh))
            if fresh:
                self._repo.save_many(fresh)
            for job in fresh:
                self._queue.put(job.id)
            for source in redundant:
                source.cleanup()
            return accepted

    def prune_failed(self, *, days: int = 7, upload_dir: Path | None = None, dry_run: bool = True) -> dict:
        if days < 1:
            raise ValueError('保留天數必須至少 1 天')
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        with self._lock:
            jobs = self._repo.all()
            expired = [j for j in jobs if j.status == JobStatus.ERROR
                       and (j.finished_at or j.created_at) < cutoff and j.result is None]
            from app.infrastructure.audio_sources import LocalFileSource
            referenced = {j.source._file_path.resolve() for j in jobs
                          if isinstance(j.source, LocalFileSource)}
            orphans = [p for p in upload_dir.glob('tmp*')
                       if p.is_file() and not p.is_symlink() and p.resolve() not in referenced
                       and p.stat().st_mtime < cutoff.timestamp()] if upload_dir and upload_dir.exists() else []
            if not dry_run:
                for job in expired:
                    if job.source:
                        job.source.cleanup()
                    self._repo.delete(job.id)
                for path in orphans:
                    path.unlink(missing_ok=True)
            return {'dry_run': dry_run, 'failed_jobs': [j.id for j in expired], 'orphan_files': len(orphans)}

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._repo.get(job_id)

    def list_recent(self, hours: int | None = None) -> list[Job]:
        """回傳時間窗內的 job，最新在前。"""
        window = self._window_hours if hours is None else hours
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=window)
        with self._lock:
            jobs = [j for j in self._repo.all() if j.created_at >= cutoff]
        jobs.reverse()
        return jobs

    # ---- 狀態轉移 (worker thread 使用) ----
    def mark_running(self, job_id: str) -> bool:
        with self._lock:
            job = self._repo.get(job_id)
            if not job or job.status != JobStatus.QUEUED:
                return False
            job.mark_running()
            self._repo.save(job)
            return True

    def mark_done(self, job_id: str, result: JobResult) -> None:
        self._transition(job_id, lambda job: job.mark_done(result) if job.status == JobStatus.RUNNING else None)

    def expire(self, job_id: str) -> bool:
        with self._lock:
            job = self._repo.get(job_id)
            if job is None or job.status != JobStatus.RUNNING:
                return False
            job.details.update(error_code='timeout', retry_hint='請重啟 STT，分段後重試')
            job.mark_error('任務處理逾時，服務需重啟；請分段後重試')
            self._repo.save(job)
            return True

    def mark_error(self, job_id: str, message: str, *, code='processing_failed', hint='請修正來源或設定後重試') -> None:
        def mutate(job):
            job.details.update(error_code=code, retry_hint=hint)
            job.mark_error(message)
        self._transition(job_id, mutate)

    def update_progress(self, job_id: str, stage: str, progress: int | None = None) -> None:
        def mutate(job):
            if job.status == JobStatus.RUNNING:
                job.details.update(stage=stage, progress=progress)
        self._transition(job_id, mutate)

    def _transition(self, job_id: str, mutate) -> None:
        with self._lock:
            job = self._repo.get(job_id)
            if job is None:
                return
            mutate(job)
            self._repo.save(job)

    def recover_interrupted(self) -> None:
        """恢復 queued / running 任務；舊版沒有來源的紀錄標記 error。"""
        with self._lock:
            for job in self._repo.all():
                if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                    if job.source is None:
                        job.mark_error("任務缺少音檔來源，請重新提交")
                    else:
                        self._reset(job)
                    self._repo.save(job)
                    if job.status == JobStatus.QUEUED:
                        self._queue.put(job.id)

    @staticmethod
    def _reset(job: Job) -> None:
        job.status = JobStatus.QUEUED
        job.started_at = None
        job.finished_at = None
        job.error = None
        job.result = None
        job.details.update(stage="queued", progress=None, error_code=None, retry_hint=None)

    def retry(self, job_id: str) -> bool:
        """僅重新排隊失敗任務，避免重複執行 queued / running / done。"""
        with self._lock:
            job = self._repo.get(job_id)
            if job is None or job.status != JobStatus.ERROR or job.source is None:
                return False
            self._require_capacity()
            self._reset(job)
            self._repo.save(job)
            self._queue.put(job.id)
        return True

    def delete(self, job_id: str) -> bool:
        """從 repo 刪除一筆 job 紀錄。回傳 True 表示存在並已刪除。"""
        with self._lock:
            job = self._repo.get(job_id)
            if job is None:
                return False
            if job.status == JobStatus.RUNNING:
                raise ValueError("job 執行中，無法刪除")
            if job.source is not None:
                job.source.cleanup()
            self._repo.delete(job_id)
        return True

    def cancel(self, job_id: str) -> bool:
        """把單一 queued job 標記為 error。回傳 True 表示成功取消，False 表示不在排隊中。"""
        with self._lock:
            job = self._repo.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                return False
            job.mark_error("使用者取消")
            self._repo.save(job)
        return True

    def cancel_all_queued(self) -> int:
        """把所有排隊中的 job 標記為 error。回傳取消筆數。

        正在執行中的 job 無法中斷 (faster-whisper 不支援)，會跑完。
        Worker 取到已取消的 job_id 時會自動跳過。
        """
        cancelled = 0
        with self._lock:
            for job in self._repo.all():
                if job.status == JobStatus.QUEUED:
                    job.mark_error("使用者取消")
                    self._repo.save(job)
                    cancelled += 1
        return cancelled

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
        with self._lock:
            return sum(j.status == JobStatus.QUEUED for j in self._repo.all())

    def _require_capacity(self, count: int = 1) -> None:
        active = sum(j.status in (JobStatus.QUEUED, JobStatus.RUNNING) for j in self._repo.all())
        if active + count > self._max_pending:
            raise QueueFullError('任務佇列已滿，請等現有任務完成後再提交')
