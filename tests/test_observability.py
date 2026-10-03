from fastapi.testclient import TestClient
from app.infrastructure.repository import InMemoryJobRepository
from app.main import create_app
from app.observability import Observability
from tests.conftest import FakeTranscriber


def test_request_correlation_and_metrics_have_no_job_id_labels(settings, transcripts):
    app = create_app(settings, transcriber=FakeTranscriber(), job_repository=InMemoryJobRepository(),
                     transcript_repository=transcripts, run_worker=False)
    with TestClient(app) as client:
        response = client.post('/transcribe_url', data={'url':'https://example.com/a'}, headers={'X-Request-ID':'trace-one'})
        assert response.headers['X-Request-ID'] == 'trace-one'
        job_id = response.json()['job_id']
        assert app.state.container.job_service.get(job_id).details['request_id'] == 'trace-one'
        client.get('/job/' + job_id)
        metrics = client.get('/metrics').text
        assert 'route="/job/{job_id}"' in metrics
        assert job_id not in metrics


def test_webhook_alerts_are_optional_and_coalesced(monkeypatch):
    monkeypatch.setenv('ALERT_WEBHOOK_URL','')
    disabled = Observability('test')
    disabled.alert('failure')
    assert disabled._alerts.empty()
    monkeypatch.setenv('ALERT_WEBHOOK_URL','https://webhook.example/token')
    enabled = Observability('test')
    enabled.alert('failure', job_id='job-one')
    enabled.alert('failure', job_id='job-one')
    assert enabled._alerts.qsize() == 1
    assert enabled._alerts.get_nowait() == {'service':'test','code':'failure','job_id':'job-one'}
