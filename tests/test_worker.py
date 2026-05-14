import time

from app.domain.job import JobStatus, JobType
from app.infrastructure.output_writer import OutputWriter
from app.infrastructure.repository import InMemoryJobRepository
from app.services.job_service import JobService
from app.services.transcription import TranscriptionService
from app.services.worker import TranscriptionWorker
from tests.conftest import FakeAudioSource, FakeTranscriber


def _build(tmp_path):
    job_service = JobService(InMemoryJobRepository(), jobs_window_hours=48)
    transcription = TranscriptionService(
        FakeTranscriber(), OutputWriter(tmp_path),
        default_language="zh", default_batch_size=4,
    )
    return job_service, TranscriptionWorker(job_service, transcription)


def test_process_marks_job_done(tmp_path):
    job_service, worker = _build(tmp_path)
    job = job_service.create(job_type=JobType.FILE, source=FakeAudioSource(),
                             topic="t", language="zh", batch_size=4)

    worker._process(job.id)

    done = job_service.get(job.id)
    assert done.status == JobStatus.DONE
    assert done.result is not None


def test_process_marks_job_error_on_failure(tmp_path):
    job_service, worker = _build(tmp_path)
    job = job_service.create(job_type=JobType.FILE, source=FakeAudioSource(fail=True),
                             topic="t", language="zh", batch_size=4)

    worker._process(job.id)

    failed = job_service.get(job.id)
    assert failed.status == JobStatus.ERROR
    assert "boom" in failed.error


def test_process_unknown_job_is_noop(tmp_path):
    _, worker = _build(tmp_path)
    worker._process("ghost")  # 不該炸


def test_worker_thread_drains_queue(tmp_path):
    job_service, worker = _build(tmp_path)
    job = job_service.create(job_type=JobType.FILE, source=FakeAudioSource(),
                             topic="t", language="zh", batch_size=4)

    worker.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not job_service.get(job.id).is_terminal:
            time.sleep(0.02)
    finally:
        worker.stop()

    assert job_service.get(job.id).status == JobStatus.DONE
