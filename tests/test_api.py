import time

import pytest
from fastapi.testclient import TestClient

from app.infrastructure.repository import InMemoryJobRepository
from app.infrastructure.transcript_repository import InMemoryTranscriptRepository
from app.main import create_app
from tests.conftest import FakeTranscriber


@pytest.fixture
def client(settings, fake_transcriber):
    app = create_app(
        settings,
        transcriber=fake_transcriber,
        job_repository=InMemoryJobRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        run_worker=False,
    )
    with TestClient(app) as c:
        yield c


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["model"] == "tiny"
    assert body["queue_size"] == 0


def test_index_renders_settings(client):
    html = client.get("/").text
    assert "tiny" in html  # 模型名稱被填進模板
    assert "<html" in html


def test_get_unknown_job_returns_404(client):
    assert client.get("/job/does-not-exist").status_code == 404


def test_jobs_empty(client):
    body = client.get("/jobs").json()
    assert body == {"jobs": [], "queue_size": 0}


def test_transcribe_url_queues_job(client):
    resp = client.post("/transcribe_url", data={"url": "http://youtube/x", "language": "zh"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"

    # job 出現在清單與單筆查詢中
    listed = client.get("/jobs").json()
    assert [j["id"] for j in listed["jobs"]] == [body["job_id"]]
    assert client.get(f"/job/{body['job_id']}").json()["status"] == "queued"


def test_end_to_end_url_transcription(settings, monkeypatch):
    """跑滿整條路徑:URL → 佇列 → worker → 完成,逐字稿可取回。

    routes 內的 YtDlpSource 換成 FakeAudioSource,測試不打網路也不裝 yt-dlp。
    """
    from app.api import routes
    from tests.conftest import FakeAudioSource

    monkeypatch.setattr(
        routes, "YtDlpSource", lambda url: FakeAudioSource(display_name=url)
    )

    app = create_app(
        settings,
        transcriber=FakeTranscriber(),
        job_repository=InMemoryJobRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        run_worker=True,
    )
    with TestClient(app) as client:
        job_id = client.post(
            "/transcribe_url",
            data={"url": "http://youtube/x", "topic": "端到端"},
        ).json()["job_id"]

        deadline = time.time() + 5
        status = "queued"
        while time.time() < deadline and status in ("queued", "running"):
            time.sleep(0.02)
            status = client.get(f"/job/{job_id}").json()["status"]

        result = client.get(f"/job/{job_id}").json()
        assert result["status"] == "done"
        assert result["text"] == "你好\n世界"
        assert result["transcript_id"] is not None


def test_transcribe_file_queues_job(client):
    import io
    file_content = b"fake audio content"
    resp = client.post(
        "/transcribe_file",
        data={"language": "zh", "topic": "測試上傳"},
        files={"file": ("test.m4a", io.BytesIO(file_content), "audio/mp4")},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"

    # job 出現在清單與單筆查詢中
    listed = client.get("/jobs").json()
    assert [j["id"] for j in listed["jobs"]] == [body["job_id"]]
    assert client.get(f"/job/{body['job_id']}").json()["status"] == "queued"


def test_end_to_end_file_transcription(settings):
    """測試上傳檔案 → 佇列 → worker → 完成的端到端路徑。"""
    import io
    app = create_app(
        settings,
        transcriber=FakeTranscriber(),
        job_repository=InMemoryJobRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        run_worker=True,
    )
    with TestClient(app) as client:
        file_content = b"fake audio content"
        job_id = client.post(
            "/transcribe_file",
            data={"language": "zh", "topic": "測試上傳"},
            files={"file": ("test.m4a", io.BytesIO(file_content), "audio/mp4")},
        ).json()["job_id"]

        deadline = time.time() + 5
        status = "queued"
        while time.time() < deadline and status in ("queued", "running"):
            time.sleep(0.02)
            status = client.get(f"/job/{job_id}").json()["status"]

        result = client.get(f"/job/{job_id}").json()
        assert result["status"] == "done"
        assert result["text"] == "你好\n世界"
        assert result["transcript_id"] is not None



def test_probes_reject_missing_worker(client):
    assert client.get("/healthz").status_code == 503
    assert client.get("/readyz").status_code == 503


def test_probes_separate_db_failure_from_worker_health(settings, monkeypatch):
    repo = InMemoryJobRepository()
    app = create_app(settings, transcriber=FakeTranscriber(), job_repository=repo,
                     transcript_repository=InMemoryTranscriptRepository())
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        def unavailable(job_id):
            raise RuntimeError("DB unavailable")
        monkeypatch.setattr(repo, "get", unavailable)
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200


def test_retry_rejects_unknown_or_queued_job(client):
    assert client.post("/job/missing/retry").status_code == 404
    job_id = client.post("/transcribe_url", data={"url": "https://youtube/x"}).json()["job_id"]
    assert client.post(f"/job/{job_id}/retry").status_code == 409
