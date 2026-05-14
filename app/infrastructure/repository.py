"""Job 紀錄的儲存實作。

- InMemoryJobRepository：純記憶體，給測試或不需持久化的情境用。
- JsonFileJobRepository：每個 job 一個 JSON 檔，服務重啟後仍能載回歷史紀錄。
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
from collections import OrderedDict

from app.domain.job import Job, JobResult, JobStatus, JobType

log = logging.getLogger(__name__)


# ---- 序列化 (Job <-> dict)；source 不持久化 ----
def _job_to_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "type": job.type.value,
        "status": job.status.value,
        "topic": job.topic,
        "language": job.language,
        "batch_size": job.batch_size,
        "created_at": job.created_at.isoformat(timespec="seconds"),
        "started_at": job.started_at.isoformat(timespec="seconds") if job.started_at else None,
        "finished_at": (
            job.finished_at.isoformat(timespec="seconds") if job.finished_at else None
        ),
        "result": _result_to_dict(job.result),
        "error": job.error,
    }


def _result_to_dict(result: JobResult | None) -> dict | None:
    if result is None:
        return None
    return {
        "language": result.language,
        "duration": result.duration,
        "text": result.text,
        "txt_file": result.txt_file,
        "srt_file": result.srt_file,
    }


def _parse_dt(value: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(value) if value else None


def _job_from_dict(data: dict) -> Job:
    result_data = data.get("result")
    return Job(
        id=data["id"],
        type=JobType(data["type"]),
        topic=data.get("topic"),
        language=data.get("language"),
        batch_size=data["batch_size"],
        status=JobStatus(data["status"]),
        created_at=_parse_dt(data["created_at"]),
        started_at=_parse_dt(data.get("started_at")),
        finished_at=_parse_dt(data.get("finished_at")),
        result=JobResult(**result_data) if result_data else None,
        error=data.get("error"),
    )


class InMemoryJobRepository:
    """純記憶體儲存，不落地。"""

    def __init__(self) -> None:
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()

    def save(self, job: Job) -> None:
        self._jobs[job.id] = job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def delete(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)


class JsonFileJobRepository:
    """每個 job 一個 JSON 檔，記憶體中同時保留一份快取。

    超過 max_jobs 時，淘汰最舊的「已結束」job (done / error)。
    """

    def __init__(self, jobs_dir: pathlib.Path, max_jobs: int):
        self._dir = jobs_dir
        self._max_jobs = max_jobs
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> int:
        """啟動時從磁碟載回歷史 job。回傳載入筆數。"""
        files = sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for stale in files[: -self._max_jobs] if self._max_jobs else []:
            stale.unlink(missing_ok=True)
        for f in files[-self._max_jobs :] if self._max_jobs else files:
            try:
                job = _job_from_dict(json.loads(f.read_text(encoding="utf-8")))
            except Exception as e:
                log.warning("略過損毀的 job 檔 %s: %s", f.name, e)
                continue
            self._jobs[job.id] = job
        return len(self._jobs)

    def save(self, job: Job) -> None:
        self._jobs[job.id] = job
        self._write(job)
        self._evict()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def delete(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        (self._dir / f"{job_id}.json").unlink(missing_ok=True)

    def _write(self, job: Job) -> None:
        try:
            (self._dir / f"{job.id}.json").write_text(
                json.dumps(_job_to_dict(job), ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            log.error("job %s 寫入失敗: %s", job.id, e)

    def _evict(self) -> None:
        """超過上限就刪最舊的已結束 job；沒有可刪的就停手。"""
        while len(self._jobs) > self._max_jobs:
            victim = next(
                (jid for jid, job in self._jobs.items() if job.is_terminal), None
            )
            if victim is None:
                return
            self.delete(victim)
