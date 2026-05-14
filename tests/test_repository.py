import datetime

from app.domain.job import Job, JobResult, JobStatus, JobType
from app.infrastructure.repository import InMemoryJobRepository, JsonFileJobRepository


def _job(job_id, status=JobStatus.QUEUED, result=None):
    return Job(
        id=job_id,
        type=JobType.FILE,
        topic="主題",
        language="zh",
        batch_size=4,
        status=status,
        created_at=datetime.datetime(2026, 5, 15, 10, 0, 0),
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


# ---- JsonFileJobRepository ----
def test_json_repo_save_writes_file(tmp_path):
    repo = JsonFileJobRepository(tmp_path, max_jobs=10)
    repo.save(_job("abc"))
    assert (tmp_path / "abc.json").exists()


def test_json_repo_load_round_trips_job(tmp_path):
    result = JobResult(
        language="zh", duration=12.0, text="逐字稿",
        txt_file="2026-05-15/x.txt", srt_file="2026-05-15/x.srt",
    )
    original = _job("done1", status=JobStatus.DONE, result=result)
    original.started_at = datetime.datetime(2026, 5, 15, 10, 0, 5)
    original.finished_at = datetime.datetime(2026, 5, 15, 10, 1, 0)

    JsonFileJobRepository(tmp_path, max_jobs=10).save(original)

    reloaded = JsonFileJobRepository(tmp_path, max_jobs=10)
    assert reloaded.load() == 1
    assert reloaded.get("done1") == original


def test_json_repo_evicts_oldest_terminal_job(tmp_path):
    repo = JsonFileJobRepository(tmp_path, max_jobs=2)
    repo.save(_job("old", status=JobStatus.DONE))
    repo.save(_job("mid", status=JobStatus.ERROR))
    repo.save(_job("new", status=JobStatus.DONE))

    ids = {j.id for j in repo.all()}
    assert ids == {"mid", "new"}
    assert not (tmp_path / "old.json").exists()


def test_json_repo_does_not_evict_unfinished_jobs(tmp_path):
    repo = JsonFileJobRepository(tmp_path, max_jobs=2)
    repo.save(_job("q1", status=JobStatus.QUEUED))
    repo.save(_job("q2", status=JobStatus.RUNNING))
    repo.save(_job("q3", status=JobStatus.QUEUED))

    # 沒有可淘汰的已結束 job，三筆都留著
    assert len(repo.all()) == 3


def test_json_repo_load_skips_corrupt_files(tmp_path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    JsonFileJobRepository(tmp_path, max_jobs=10).save(_job("good"))

    reloaded = JsonFileJobRepository(tmp_path, max_jobs=10)
    assert reloaded.load() == 1
    assert reloaded.get("good") is not None
