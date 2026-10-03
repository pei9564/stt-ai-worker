from fastapi.testclient import TestClient
from app.domain.job import JobResult, JobType
from app.infrastructure.audio_sources import LocalFileSource
from app.infrastructure.repository import InMemoryJobRepository
from app.main import create_app
from tests.conftest import FakeTranscriber


def test_export_requires_done_and_omits_filesystem_path(settings, transcripts, tmp_path):
    app = create_app(settings, transcriber=FakeTranscriber(), job_repository=InMemoryJobRepository(),
                     transcript_repository=transcripts, run_worker=False)
    with TestClient(app) as client:
        svc = app.state.container.job_service
        job = svc.create(job_type=JobType.FILE, source=LocalFileSource(tmp_path / 'private.wav', '中文'),
                         topic='中文', language='zh', batch_size=1)
        assert client.get(f'/job/{job.id}/export').status_code == 409
        svc.mark_running(job.id)
        svc.mark_done(job.id, JobResult('zh', 1, '逐字稿', '1\n00:00:00,000 --> 00:00:01,000\n逐字稿\n', 1))
        response = client.get(f'/job/{job.id}/export')
        assert response.json()['text'] == '逐字稿'
        assert 'path' not in response.json()['provenance']['source']
        assert str(tmp_path) not in response.text
        assert 'attachment' in response.headers['content-disposition']
        assert '逐字稿' in client.get(f'/job/{job.id}/export?format=markdown').text
        assert '-->' in client.get(f'/job/{job.id}/export?format=srt').text
        assert client.get(f'/job/{job.id}/export?format=bad').status_code == 422
