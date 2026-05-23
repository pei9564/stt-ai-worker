"""領域層對外部能力的抽象介面 (ports)。

services 只依賴這些 Protocol，infrastructure 提供實作。
測試時可用假物件替換，不需要 GPU / 磁碟 / 網路。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.domain.job import Job
from app.domain.transcript import TranscriptResult


class Transcriber(Protocol):
    """語音轉文字引擎。"""

    def transcribe(
        self, audio_path: Path, language: str | None, batch_size: int
    ) -> TranscriptResult:
        """把音檔轉成逐字稿。batch_size <= 1 表示不使用批次推論。"""
        ...


class JobRepository(Protocol):
    """job 紀錄的儲存。實作可選擇是否持久化。"""

    def save(self, job: Job) -> None:
        """新增或更新一筆 job。"""
        ...

    def get(self, job_id: str) -> Job | None:
        """取得單一 job；不存在回 None。"""
        ...

    def all(self) -> list[Job]:
        """所有 job，依建立順序由舊到新。"""
        ...

    def delete(self, job_id: str) -> None:
        """刪除一筆 job；不存在則無動作。"""
        ...


class TranscriptRepository(Protocol):
    """轉檔成品 (text + SRT) 的儲存。"""

    def save(
        self,
        *,
        topic: str,
        text: str,
        srt: str,
        language: str,
        duration: float,
    ) -> int:
        """寫入一筆 transcript，回傳 PK。"""
        ...

    def delete(self, transcript_id: int) -> None:
        """刪除一筆 transcript；不存在則無動作。"""
        ...
