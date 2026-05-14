"""轉檔使用情境：取得音檔 → 轉文字 → 寫出檔案。"""

from __future__ import annotations

from app.domain.job import Job, JobResult
from app.domain.ports import Transcriber
from app.infrastructure.output_writer import OutputWriter


class TranscriptionService:
    """把一筆 Job 從音檔來源跑到逐字稿輸出。"""

    def __init__(
        self,
        transcriber: Transcriber,
        output_writer: OutputWriter,
        *,
        default_language: str | None,
        default_batch_size: int,
    ):
        self._transcriber = transcriber
        self._output_writer = output_writer
        self._default_language = default_language
        self._default_batch_size = default_batch_size

    def process(self, job: Job) -> JobResult:
        """處理一筆 job，回傳寫檔後的 JobResult。

        job.source 為 None (服務重啟後的殘留紀錄) 會丟 RuntimeError。
        """
        if job.source is None:
            raise RuntimeError("job 沒有音檔來源，無法處理")

        language = job.language or self._default_language
        batch_size = job.batch_size or self._default_batch_size

        with job.source.acquire() as audio_path:
            result = self._transcriber.transcribe(audio_path, language, batch_size)
            name = job.topic or job.source.display_name
            return self._output_writer.write(result, name)
