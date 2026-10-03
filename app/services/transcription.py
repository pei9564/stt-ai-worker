"""轉檔使用情境:取得音檔 → 轉文字 → 寫入 PG transcripts。"""

from __future__ import annotations

import time
from typing import Callable
from pathlib import Path

from app.domain.job import Job, JobResult
from app.domain.ports import Transcriber, TranscriptRepository
from app.infrastructure.naming import safe_stem
from app.infrastructure.srt import render_srt


class TranscriptionService:
    """把一筆 Job 從音檔來源跑到逐字稿輸出。"""

    def __init__(
        self,
        transcriber: Transcriber,
        transcripts: TranscriptRepository,
        *,
        default_language: str | None,
        default_batch_size: int,
        audio_probe: Callable[[Path], float] | None = None,
        max_audio_seconds: int = 7200,
        max_job_seconds: int = 7200,
    ):
        self._audio_probe = audio_probe
        self._max_audio_seconds = max_audio_seconds
        self._max_job_seconds = max_job_seconds
        self._transcriber = transcriber
        self._transcripts = transcripts
        self._default_language = default_language
        self._default_batch_size = default_batch_size

    def process(self, job: Job, on_stage=None) -> JobResult:
        """處理一筆 job,回傳寫入 PG 後的 JobResult。

        job.source 為 None (服務重啟後的殘留紀錄) 會丟 RuntimeError。
        """
        if job.source is None:
            raise RuntimeError("job 沒有音檔來源,無法處理")

        report = on_stage or (lambda stage, progress=None: None)
        report('validating' if job.type.value == 'file' else 'downloading')
        started = time.monotonic()
        language = job.language or self._default_language
        batch_size = job.batch_size or self._default_batch_size

        with job.source.acquire() as audio_path:
            report('validating')
            if self._audio_probe and self._audio_probe(audio_path) > self._max_audio_seconds:
                raise ValueError('音訊長度超過限制，請分段後重新提交')
            report('transcribing', 0)
            progressive = getattr(self._transcriber, 'transcribe_with_progress', None)
            if progressive:
                result = progressive(audio_path, language, batch_size,
                                     lambda percent: report('transcribing', min(99, max(0, int(percent)))))
            else:
                result = self._transcriber.transcribe(audio_path, language, batch_size)

        if time.monotonic() - started > self._max_job_seconds:
            raise TimeoutError('任務處理逾時，請分段後重試')
        report('saving', 99)
        srt = render_srt(result.segments)
        topic = safe_stem(job.topic or job.source.display_name)
        tid = self._transcripts.save(
            topic=topic,
            text=result.text,
            srt=srt,
            language=result.language,
            duration=result.duration,
        )
        return JobResult(
            language=result.language,
            duration=result.duration,
            text=result.text,
            srt=srt,
            transcript_id=tid,
        )
