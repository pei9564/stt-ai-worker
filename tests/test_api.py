import io
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


def test_transcribe_file_queues_job(client):
    resp = client.post(
        "/transcribe",
        files={"file": ("test.mp3", io.BytesIO(b"audio-bytes"), "audio/mpeg")},
        data={"topic": "上傳測試"},
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"


def test_end_to_end_file_transcription(settings):
    """跑滿整條路徑:上傳 → 佇列 → worker → 完成,逐字稿可取回。"""
    app = create_app(
        settings,
        transcriber=FakeTranscriber(),
        job_repository=InMemoryJobRepository(),
        transcript_repository=InMemoryTranscriptRepository(),
        run_worker=True,
    )
    with TestClient(app) as client:
        job_id = client.post(
            "/transcribe",
            files={"file": ("ep.mp3", io.BytesIO(b"audio-bytes"), "audio/mpeg")},
            data={"topic": "端到端"},
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
