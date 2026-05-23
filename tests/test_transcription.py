import pytest

from app.domain.job import Job, JobType
from app.infrastructure.transcript_repository import InMemoryTranscriptRepository
from app.services.transcription import TranscriptionService
from tests.conftest import FakeAudioSource, FakeTranscriber


def _job(source, *, topic=None, language=None, batch_size=0):
    return Job(id="j", type=JobType.FILE, topic=topic, language=language,
               batch_size=batch_size, source=source)


def test_process_transcribes_and_persists():
    transcriber = FakeTranscriber()
    repo = InMemoryTranscriptRepository()
    svc = TranscriptionService(transcriber, repo,
                               default_language="en", default_batch_size=8)
    source = FakeAudioSource()
    job = _job(source, topic="我的主題", language="zh", batch_size=4)

    result = svc.process(job)

    assert result.text == "你好\n世界"
    assert result.srt.startswith("1\n")  # render_srt 編號從 1 開始
    assert result.transcript_id == 1
    row = repo.get(1)
    assert row["topic"] == "我的主題"
    assert row["text"] == "你好\n世界"
    assert source.acquired and source.released
    # transcriber 收到 job 指定的語言與批次
    assert transcriber.calls[0][1] == "zh"
    assert transcriber.calls[0][2] == 4


def test_process_falls_back_to_defaults():
    transcriber = FakeTranscriber()
    svc = TranscriptionService(transcriber, InMemoryTranscriptRepository(),
                               default_language="en", default_batch_size=8)
    svc.process(_job(FakeAudioSource(), language=None, batch_size=0))

    assert transcriber.calls[0][1] == "en"
    assert transcriber.calls[0][2] == 8


def test_process_uses_topic_as_persisted_topic():
    repo = InMemoryTranscriptRepository()
    svc = TranscriptionService(FakeTranscriber(), repo,
                               default_language="zh", default_batch_size=4)
    svc.process(_job(FakeAudioSource(display_name="原始檔名"), topic="指定主題"))
    assert repo.get(1)["topic"] == "指定主題"


def test_process_falls_back_to_source_display_name():
    repo = InMemoryTranscriptRepository()
    svc = TranscriptionService(FakeTranscriber(), repo,
                               default_language="zh", default_batch_size=4)
    svc.process(_job(FakeAudioSource(display_name="來源名稱"), topic=None))
    assert repo.get(1)["topic"] == "來源名稱"


def test_process_without_source_raises():
    svc = TranscriptionService(FakeTranscriber(), InMemoryTranscriptRepository(),
                               default_language="zh", default_batch_size=4)
    with pytest.raises(RuntimeError, match="沒有音檔來源"):
        svc.process(_job(None))
