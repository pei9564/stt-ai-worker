"""faster-whisper 的轉檔引擎實作。

`faster_whisper` 是很重的相依 (CUDA 函式庫)，因此延遲到建構 WhisperTranscriber
時才 import —— 這樣測試只要不實例化它，就不需要安裝整套 GPU 套件。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.domain.transcript import TranscriptResult, TranscriptSegment

log = logging.getLogger(__name__)


class WhisperTranscriber:
    """以 faster-whisper 實作的 Transcriber port。"""

    def __init__(self, model: str, device: str, compute_type: str):
        from faster_whisper import BatchedInferencePipeline, WhisperModel

        log.info("loading model=%s device=%s compute_type=%s", model, device, compute_type)
        self._model = WhisperModel(model, device=device, compute_type=compute_type)
        self._batched = BatchedInferencePipeline(model=self._model)
        log.info("model ready")

    def transcribe(
        self, audio_path: Path, language: str | None, batch_size: int, on_progress=None
    ) -> TranscriptResult:
        lang = (language or "").strip() or None
        if batch_size and batch_size > 1:
            segments, info = self._batched.transcribe(
                str(audio_path), language=lang, batch_size=batch_size, vad_filter=True
            )
        else:
            segments, info = self._model.transcribe(
                str(audio_path), language=lang, vad_filter=True
            )

        parsed_list = []
        for seg in segments:
            parsed_list.append(TranscriptSegment(start=seg.start, end=seg.end, text=seg.text.strip()))
            if on_progress and info.duration:
                on_progress(100 * seg.end / info.duration)
        parsed = tuple(parsed_list)
        return TranscriptResult(
            language=info.language, duration=info.duration, segments=parsed
        )

    def transcribe_with_progress(self, audio_path, language, batch_size, on_progress):
        return self.transcribe(audio_path, language, batch_size, on_progress)
