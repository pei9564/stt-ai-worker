from dataclasses import replace
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.domain.job import JobType, JobResult
from app.infrastructure.repository import InMemoryJobRepository
from app.main import create_app
from app.services.job_service import JobService
from app.services.notes import NotesService, NotesError
from tests.conftest import FakeAudioSource


def completed(jobs, text='one two'):
    job = jobs.create(job_type=JobType.URL, source=FakeAudioSource(), topic='test', language='zh', batch_size=1)
    jobs.mark_running(job.id)
    jobs.mark_done(job.id, JobResult(language='zh', duration=1, text=text, srt='', transcript_id=1))
    return job.id


def finish(service):
    deadline = time.monotonic() + 3
    while service.is_alive and time.monotonic() < deadline:
        time.sleep(.01)
    assert not service.is_alive


class Client:
    enabled = True
    def __init__(self):
        self.calls = []
    def chat(self, text, mode):
        self.calls.append(text)
        return '整理結果'


def test_chunking_preserves_tail_and_deduplicates(settings):
    jobs = JobService(InMemoryJobRepository(), jobs_window_hours=48)
    text = 'a' * 9000 + 'IMPORTANT TAIL'
    jid = completed(jobs, text)
    client = Client()
    service = NotesService(jobs, settings, client)
    service.start(jid, 'actions'); finish(service)
    assert 'IMPORTANT TAIL' in ''.join(client.calls)
    assert len(client.calls) == 4
    note = jobs.get(jid).details['notes']['actions']
    assert note['status'] == 'done' and note['chunks'] == 3
    assert note['input_chars'] == len(text) and not note['input_truncated']
    assert jobs.get(jid).result.text == text
    service.start(jid, 'actions'); assert len(client.calls) == 4


def test_failure_retry_and_restart_recovery(settings):
    jobs = JobService(InMemoryJobRepository(), jobs_window_hours=48)
    jid = completed(jobs)
    class Fails(Client):
        def chat(self, text, mode):
            raise NotesError('模型失敗')
    service = NotesService(jobs, settings, Fails())
    service.start(jid, 'summary'); finish(service)
    assert jobs.get(jid).details['notes']['summary']['status'] == 'error'
    service.client = Client(); service.start(jid, 'summary'); finish(service)
    assert jobs.get(jid).details['notes']['summary']['status'] == 'done'
    jobs.update_note(jid, 'minutes', {'status':'running'})
    jobs.recover_interrupted()
    assert jobs.get(jid).details['notes']['minutes']['status'] == 'error'
    assert jobs.get(jid).result.text == 'one two'


def test_busy_and_deleted_job_is_not_resurrected(settings):
    jobs = JobService(InMemoryJobRepository(), jobs_window_hours=48)
    jid = completed(jobs); second = completed(jobs)
    gate = threading.Event()
    class Held(Client):
        def chat(self, text, mode):
            gate.wait(2)
            return 'done'
    service = NotesService(jobs, settings, Held())
    try:
        service.start(jid, 'summary')
        with pytest.raises(NotesError, match='另一份'):
            service.start(second, 'summary')
        jobs.delete(jid)
    finally:
        gate.set(); finish(service)
    assert jobs.get(jid) is None


def test_notes_api_export_and_unconfigured(settings, fake_transcriber, transcripts):
    app = create_app(settings, transcriber=fake_transcriber, job_repository=InMemoryJobRepository(), transcript_repository=transcripts, run_worker=False, notes_client=Client())
    with TestClient(app) as client:
        jid = completed(app.state.container.job_service)
        assert client.post(f'/job/{jid}/notes/bad').status_code == 422
        assert client.post(f'/job/{jid}/notes/summary').status_code == 202
        finish(app.state.container.notes)
        result = client.get(f'/job/{jid}/notes/summary/export?format=json').json()
        assert result['text'] == '整理結果' and result['job_id'] == jid
        assert 'AI 整理' in client.get(f'/job/{jid}/notes/summary/export').text
        app.state.container.notes.client.enabled = False
        assert client.post(f'/job/{jid}/notes/actions').status_code == 503


def test_llm_rejects_truncated_results_and_redacts_provider_errors(settings, monkeypatch):
    from app.services.notes import NotesClient
    import requests
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'choices': [{'message': {'content':'partial'}, 'finish_reason':'length'}]}
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: Response())
    client = NotesClient(replace(settings, vllm_base_url='https://fixture.invalid/v1', vllm_model='fixture'))
    with pytest.raises(NotesError, match='上限'):
        client.chat('data', 'summary')
    def failure(*a, **kw):
        raise requests.RequestException('secret-provider-details')
    monkeypatch.setattr(requests, 'post', failure)
    with pytest.raises(NotesError) as error:
        client.chat('data', 'summary')
    assert 'secret-provider-details' not in str(error.value)
