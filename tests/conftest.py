"""共用測試夾具：假的 transcriber / audio source，以及測試用 Settings。"""

from __future__ import annotations

import pathlib
from contextlib import contextmanager

import pytest

from app.config import Settings
from app.domain.audio_source import AudioSource
from app.domain.transcript import TranscriptResult, TranscriptSegment


class FakeTranscriber:
    """不碰 GPU 的 Transcriber：回固定結果並記錄呼叫參數。"""

    def __init__(self, result: TranscriptResult | None = None):
        self.result = result or TranscriptResult(
            language="zh",
            duration=12.5,
            segments=(
                TranscriptSegment(0.0, 2.0, "你好"),
                TranscriptSegment(2.0, 4.0, "世界"),
            ),
        )
        self.calls: list[tuple] = []

    def transcribe(self, audio_path, language, batch_size) -> TranscriptResult:
        self.calls.append((audio_path, language, batch_size))
        return self.result


class FakeAudioSource(AudioSource):
    """不碰網路 / 檔案系統的 AudioSource，可設定 acquire 失敗。"""

    def __init__(self, display_name: str = "fake.mp3", path: pathlib.Path | None = None,
                 fail: bool = False):
        self._name = display_name
        self._path = path or pathlib.Path("/tmp/fake-audio.mp3")
        self._fail = fail
        self.acquired = False
        self.released = False

    @property
    def display_name(self) -> str:
        return self._name

    @contextmanager
    def acquire(self):
        self.acquired = True
        try:
            if self._fail:
                raise RuntimeError("acquire boom")
            yield self._path
        finally:
            self.released = True


@pytest.fixture
def settings() -> Settings:
    return Settings(
        model="tiny",
        device="cpu",
        compute_type="int8",
        language="zh",
        batch_size=4,
        db_url="postgresql://test:test@localhost/test",  # 測試不會真連線
        jobs_window_hours=48,
    )


@pytest.fixture
def fake_transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def transcripts():
    """InMemoryTranscriptRepository fixture，給單元測試替代 PG。"""
    from app.infrastructure.transcript_repository import InMemoryTranscriptRepository

    return InMemoryTranscriptRepository()


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
