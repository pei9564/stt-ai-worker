"""正式 PostgreSQL 路徑回歸測試；TEST_PG_DSN 指向隔離測試 DB。"""
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from app.domain.job import Job, JobStatus, JobType
from app.infrastructure.audio_sources import DirectUrlSource, LocalFileSource, YtDlpSource
from app.infrastructure.pg_job_repository import PostgresJobRepository
from app.infrastructure.transcript_repository import PostgresTranscriptRepository
from app.main import _apply_schema, create_app
from app.services.job_service import JobService
from tests.conftest import FakeTranscriber


@pytest.fixture
def pool():
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("set TEST_PG_DSN to an isolated PostgreSQL database")
    with ConnectionPool(dsn) as pool:
        _apply_schema(pool)
        _apply_schema(pool)  # migration is idempotent
        with pool.connection() as conn:
            conn.execute("TRUNCATE jobs, transcripts RESTART IDENTITY CASCADE")
        yield pool


@pytest.mark.parametrize("source", [YtDlpSource("https://youtube/x"), DirectUrlSource("https://audio/x.mp3", "集數"), LocalFileSource(Path("/tmp/audio.mp3"), "上傳")])
def test_source_survives_pg_roundtrip(pool, source):
    repo = PostgresJobRepository(pool)
    repo.save(Job("roundtrip", JobType.URL, "topic", "zh", 4, source=source))
    loaded = repo.get("roundtrip")
    assert type(loaded.source) is type(source)
    assert loaded.source.display_name == source.display_name
    repo.save(loaded)
    assert repo.all()[0].source is not None


def test_uploaded_job_recovers_after_restart_and_completes(pool, settings, tmp_path):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"audio")
    repo = PostgresJobRepository(pool)
    svc = JobService(repo, jobs_window_hours=48)
    job = svc.create(job_type=JobType.FILE, source=LocalFileSource(path, "上傳"), topic="上傳", language="zh", batch_size=4)
    svc.mark_running(job.id)
    # New app and repositories model a process restart; no shared source object.
    app = create_app(settings, transcriber=FakeTranscriber(), job_repository=PostgresJobRepository(pool), transcript_repository=PostgresTranscriptRepository(pool))
    with TestClient(app) as client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get(f"/job/{job.id}").json()
            if result["status"] in ("done", "error"):
                break
            time.sleep(0.02)
        assert result["status"] == "done", result
        assert result["text"] == "你好\n世界"
        assert result["transcript_id"] is not None
        assert client.get("/readyz").status_code == 200
    assert not path.exists()


def test_failed_upload_can_retry_without_losing_audio(pool, settings, tmp_path):
    class FailsOnce(FakeTranscriber):
        def transcribe(self, *args):
            if not hasattr(self, "failed"):
                self.failed = True
                raise RuntimeError("temporary failure")
            return super().transcribe(*args)

    path = tmp_path / "retry.mp3"
    path.write_bytes(b"audio")
    repo = PostgresJobRepository(pool)
    repo.save(Job("retry", JobType.FILE, "topic", "zh", 4, source=LocalFileSource(path, "retry")))
    app = create_app(settings, transcriber=FailsOnce(), job_repository=repo, transcript_repository=PostgresTranscriptRepository(pool))
    def wait_for(client, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get("/job/retry").json()
            if result["status"] == status:
                return result
            time.sleep(0.02)
        pytest.fail(str(result))
    with TestClient(app) as client:
        wait_for(client, "error")
        assert path.exists()
        assert client.post("/job/retry/retry").status_code == 202
        assert wait_for(client, "done")["text"] == "你好\n世界"
        assert client.post("/job/retry/retry").status_code == 409
    assert not path.exists()


def test_legacy_unfinished_job_becomes_error(pool):
    repo = PostgresJobRepository(pool)
    repo.save(Job("legacy", JobType.URL, None, None, 4))
    svc = JobService(repo, jobs_window_hours=48)
    svc.recover_interrupted()
    assert repo.get("legacy").status == JobStatus.ERROR
    assert svc.queue_size == 0
    assert not svc.retry("legacy")


def test_default_app_url_submission_completes_with_pg(pool, settings, monkeypatch):
    """正式 app 自行開 PG pool；只替換下載及 GPU 推論。"""
    from dataclasses import replace

    seen = []
    def download(url, dest):
        seen.append(url)
        path = dest / "episode.mp3"
        path.write_bytes(b"audio")
        return path
    monkeypatch.setattr(YtDlpSource.__init__, '__defaults__', (download,))
    app = create_app(replace(settings, db_url=os.environ["TEST_PG_DSN"]), transcriber=FakeTranscriber())
    with TestClient(app) as client:
        job_id = client.post("/transcribe_url", data={"url": "https://example.com/episode.mp3"}).json()["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get(f"/job/{job_id}").json()
            if result["status"] in ("done", "error"):
                break
            time.sleep(0.02)
        assert result["status"] == "done", result
        assert result["text"] == "你好\n世界"
    assert seen == ["https://example.com/episode.mp3"]


def test_progress_details_survive_pg_roundtrip(pool):
    repo = PostgresJobRepository(pool)
    job = Job('progress', JobType.URL, 'topic', 'zh', 4, source=YtDlpSource('https://video/x'),
              details={'stage': 'transcribing', 'progress': 45, 'retry_hint': '分段後重試'})
    repo.save(job)
    assert repo.get(job.id).details == job.details
