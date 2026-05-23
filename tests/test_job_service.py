import datetime

from app.domain.job import Job, JobResult, JobStatus, JobType
from app.infrastructure.repository import InMemoryJobRepository
from app.services.job_service import JobService
from tests.conftest import FakeAudioSource


def _service(window_hours=48):
    return JobService(InMemoryJobRepository(), jobs_window_hours=window_hours)


def test_create_queues_and_persists_job():
    svc = _service()
    job = svc.create(
        job_type=JobType.FILE,
        source=FakeAudioSource(),
        topic="主題",
        language="zh",
        batch_size=4,
    )

    assert job.status == JobStatus.QUEUED
    assert svc.get(job.id) is job
    assert svc.queue_size == 1
    assert svc.dequeue() == job.id


def test_create_normalizes_blank_topic_and_language():
    svc = _service()
    job = svc.create(
        job_type=JobType.URL, source=FakeAudioSource(), topic="", language="", batch_size=0
    )
    assert job.topic is None
    assert job.language is None


def test_get_missing_returns_none():
    assert _service().get("nope") is None


def test_list_recent_filters_by_window_and_newest_first():
    repo = InMemoryJobRepository()
    svc = JobService(repo, jobs_window_hours=24)

    old = Job(id="old", type=JobType.FILE, topic=None, language=None, batch_size=4,
              created_at=datetime.datetime.now() - datetime.timedelta(hours=48))
    recent = Job(id="recent", type=JobType.FILE, topic=None, language=None, batch_size=4,
                 created_at=datetime.datetime.now() - datetime.timedelta(hours=1))
    newest = Job(id="newest", type=JobType.FILE, topic=None, language=None, batch_size=4,
                 created_at=datetime.datetime.now())
    for job in (old, recent, newest):
        repo.save(job)

    listed = svc.list_recent()
    assert [j.id for j in listed] == ["newest", "recent"]


def test_mark_transitions_update_status_and_persist():
    svc = _service()
    job = svc.create(job_type=JobType.FILE, source=FakeAudioSource(),
                     topic="t", language="zh", batch_size=4)

    svc.mark_running(job.id)
    assert svc.get(job.id).status == JobStatus.RUNNING
    assert svc.get(job.id).started_at is not None

    result = JobResult(language="zh", duration=1.0, text="x",
                       srt="1\n00:00:00,000 --> 00:00:01,000\nx\n", transcript_id=42)
    svc.mark_done(job.id, result)
    assert svc.get(job.id).status == JobStatus.DONE
    assert svc.get(job.id).result == result


def test_mark_error_records_message():
    svc = _service()
    job = svc.create(job_type=JobType.FILE, source=FakeAudioSource(),
                     topic="t", language="zh", batch_size=4)
    svc.mark_error(job.id, "下載失敗")
    assert svc.get(job.id).status == JobStatus.ERROR
    assert svc.get(job.id).error == "下載失敗"


def test_mark_unknown_job_is_noop():
    _service().mark_running("ghost")  # 不該炸


def test_recover_interrupted_fails_unfinished_jobs():
    repo = InMemoryJobRepository()
    svc = JobService(repo, jobs_window_hours=48)
    repo.save(Job(id="q", type=JobType.FILE, topic=None, language=None,
                  batch_size=4, status=JobStatus.QUEUED))
    repo.save(Job(id="r", type=JobType.FILE, topic=None, language=None,
                  batch_size=4, status=JobStatus.RUNNING))
    repo.save(Job(id="d", type=JobType.FILE, topic=None, language=None,
                  batch_size=4, status=JobStatus.DONE))

    svc.recover_interrupted()

    assert repo.get("q").status == JobStatus.ERROR
    assert repo.get("r").status == JobStatus.ERROR
    assert repo.get("d").status == JobStatus.DONE  # 已完成的不動


def test_request_shutdown_unblocks_dequeue():
    svc = _service()
    svc.request_shutdown()
    assert svc.dequeue() is None
