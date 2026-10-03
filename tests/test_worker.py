import time

from app.domain.job import JobStatus, JobType
from app.infrastructure.repository import InMemoryJobRepository
from app.infrastructure.transcript_repository import InMemoryTranscriptRepository
from app.services.job_service import JobService
from app.services.transcription import TranscriptionService
from app.services.worker import TranscriptionWorker
from tests.conftest import FakeAudioSource, FakeTranscriber


def _build():
    job_service = JobService(InMemoryJobRepository(), jobs_window_hours=48)
    transcription = TranscriptionService(
        FakeTranscriber(), InMemoryTranscriptRepository(),
        default_language="zh", default_batch_size=4,
    )
    return job_service, TranscriptionWorker(job_service, transcription)


def test_process_marks_job_done():
    job_service, worker = _build()
    job = job_service.create(job_type=JobType.FILE, source=FakeAudioSource(),
                             topic="t", language="zh", batch_size=4)

    worker._process(job.id)

    done = job_service.get(job.id)
    assert done.status == JobStatus.DONE
    assert done.result is not None


def test_process_marks_job_error_on_failure():
    job_service, worker = _build()
    job = job_service.create(job_type=JobType.FILE, source=FakeAudioSource(fail=True),
                             topic="t", language="zh", batch_size=4)

    worker._process(job.id)

    failed = job_service.get(job.id)
    assert failed.status == JobStatus.ERROR
    assert "boom" in failed.error


def test_process_unknown_job_is_noop():
    _, worker = _build()
    worker._process("ghost")  # 不該炸


def test_worker_thread_drains_queue():
    job_service, worker = _build()
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


def test_shutdown_does_not_start_remaining_jobs():
    import threading

    started = threading.Event()
    release = threading.Event()
    svc = JobService(InMemoryJobRepository(), jobs_window_hours=48)

    class BlockingTranscriber(FakeTranscriber):
        def transcribe(self, *args):
            started.set()
            assert release.wait(3)
            return super().transcribe(*args)

    transcription = TranscriptionService(BlockingTranscriber(), InMemoryTranscriptRepository(),
                                         default_language="zh", default_batch_size=4)
    worker = TranscriptionWorker(svc, transcription)
    first = svc.create(job_type=JobType.FILE, source=FakeAudioSource(), topic="first", language="zh", batch_size=4)
    second = svc.create(job_type=JobType.FILE, source=FakeAudioSource(), topic="second", language="zh", batch_size=4)
    worker.start()
    assert started.wait(3)
    stopped = threading.Thread(target=worker.stop)
    stopped.start()
    assert worker._stopping.wait(3)
    release.set()
    stopped.join(timeout=3)
    assert not worker.is_alive
    assert svc.get(first.id).status == JobStatus.DONE
    assert svc.get(second.id).status == JobStatus.QUEUED
