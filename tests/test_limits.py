from dataclasses import replace
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from app.domain.job import JobType
from app.infrastructure.repository import InMemoryJobRepository
from app.main import create_app
from app.services.job_service import JobService, QueueFullError
from app.services.transcription import TranscriptionService
from tests.conftest import FakeAudioSource, FakeTranscriber


def test_capacity_counts_running_and_cancel_frees_slot():
    svc = JobService(InMemoryJobRepository(), jobs_window_hours=48, max_pending_jobs=1)
    spec = dict(job_type=JobType.FILE, source=FakeAudioSource(), topic=None, language=None, batch_size=0)
    job = svc.create(**spec)
    with pytest.raises(QueueFullError):
        svc.create(**spec)
    svc.cancel(job.id)
    assert svc.queue_size == 0
    assert svc.create(**spec)


def test_duration_rejected_before_gpu(transcripts):
    model = FakeTranscriber()
    svc = TranscriptionService(model, transcripts, default_language=None, default_batch_size=1,
                               audio_probe=lambda _: 100, max_audio_seconds=60)
    jobs = JobService(InMemoryJobRepository(), jobs_window_hours=48)
    job = jobs.create(job_type=JobType.FILE, source=FakeAudioSource(), topic=None, language=None, batch_size=0)
    with pytest.raises(ValueError, match='分段'):
        svc.process(job)
    assert not model.calls


def test_oversized_upload_leaves_no_file(settings, transcripts, tmp_path):
    app = create_app(replace(settings, max_upload_bytes=3), transcriber=FakeTranscriber(),
                     job_repository=InMemoryJobRepository(), transcript_repository=transcripts, run_worker=False)
    with TestClient(app) as client:
        assert client.post('/transcribe_file', files={'file': ('test.wav', b'1234')}).status_code == 413
        assert client.post('/transcribe_url', data={'url': 'https://example.com/x', 'batch_size': '-1'}).status_code == 422
    assert not list((tmp_path / 'uploads').glob('*'))


def test_rss_capacity_is_all_or_nothing(settings, transcripts):
    app = create_app(replace(settings, max_pending_jobs=1), transcriber=FakeTranscriber(),
                     job_repository=InMemoryJobRepository(), transcript_repository=transcripts, run_worker=False)
    from app.infrastructure.audio_sources import FeedEpisode
    with TestClient(app) as client, patch('app.api.routes.parse_feed', return_value=[FeedEpisode('one','https://a'), FeedEpisode('two','https://b')]):
        assert client.post('/transcribe_feed', data={'feed_url':'https://feed.example'}).status_code == 429
        assert client.get('/jobs').json()['jobs'] == []
