"""Job 紀錄的儲存實作。

- InMemoryJobRepository：純記憶體,給測試或不需持久化的情境用。
- 正式環境用 PostgresJobRepository (見 pg_repository.py)。
"""

from __future__ import annotations

from collections import OrderedDict

from app.domain.job import Job


class InMemoryJobRepository:
    """純記憶體儲存,不落地。"""

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
