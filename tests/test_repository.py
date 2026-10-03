import datetime

from app.domain.job import Job, JobStatus, JobType
from app.infrastructure.repository import InMemoryJobRepository


def _job(job_id, status=JobStatus.QUEUED, result=None):
    return Job(
        id=job_id,
        type=JobType.FILE,
        topic="主題",
        language="zh",
        batch_size=4,
        status=status,
        created_at=datetime.datetime(2026, 5, 15, 10, 0, 0, tzinfo=datetime.timezone.utc),
        result=result,
    )


# ---- InMemoryJobRepository ----
def test_in_memory_save_get_all_delete():
    repo = InMemoryJobRepository()
    job = _job("a")
    repo.save(job)

    assert repo.get("a") is job
    assert repo.all() == [job]
    assert repo.get("missing") is None

    repo.delete("a")
    assert repo.get("a") is None
    repo.delete("a")  # 重複刪除不該炸
