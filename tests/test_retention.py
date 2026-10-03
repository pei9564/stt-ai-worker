import datetime
from app.domain.job import JobType, JobStatus
from app.infrastructure.audio_sources import YtDlpSource, LocalFileSource
from app.infrastructure.repository import InMemoryJobRepository
from app.services.job_service import JobService


def test_duplicate_reuses_even_when_queue_full():
    svc = JobService(InMemoryJobRepository(), jobs_window_hours=48, max_pending_jobs=1)
    spec = dict(job_type=JobType.URL, topic=None, language=None, batch_size=0)
    first = svc.create(source=YtDlpSource('https://example.com/audio?utm_source=x'), **spec)
    assert svc.create(source=YtDlpSource('https://example.com/audio#part'), **spec).id == first.id
    assert svc.queue_size == 1


def test_file_duplicate_cleanup_and_options(tmp_path):
    svc = JobService(InMemoryJobRepository(), jobs_window_hours=48)
    a, b = tmp_path / 'a.wav', tmp_path / 'b.wav'
    a.write_bytes(b'a'); b.write_bytes(b'a')
    spec = dict(job_type=JobType.FILE, topic='audio', language=None, batch_size=0)
    first = svc.create(source=LocalFileSource(a, 'audio', 'digest'), **spec)
    assert svc.create(source=LocalFileSource(b, 'audio', 'digest'), **spec).id == first.id
    assert a.exists() and not b.exists()


def test_prune_preview_keeps_active_and_completed(tmp_path):
    repo = InMemoryJobRepository()
    svc = JobService(repo, jobs_window_hours=48)
    spec = dict(job_type=JobType.URL, topic=None, language=None, batch_size=0)
    failed = svc.create(source=YtDlpSource('https://example.com/fail'), **spec)
    svc.mark_error(failed.id, 'failure')
    failed.finished_at -= datetime.timedelta(days=8)
    active = svc.create(source=YtDlpSource('https://example.com/active'), **spec)
    done = svc.create(source=YtDlpSource('https://example.com/done'), **spec)
    done.status = JobStatus.DONE
    assert svc.prune_failed(upload_dir=tmp_path)['failed_jobs'] == [failed.id]
    assert svc.get(failed.id)
    svc.prune_failed(upload_dir=tmp_path, dry_run=False)
    assert svc.get(failed.id) is None
    assert svc.get(active.id) and svc.get(done.id)
