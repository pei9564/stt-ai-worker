"""Job 實體：一次轉檔請求的完整生命週期狀態。"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum

from app.domain.audio_source import AudioSource


class JobType(str, Enum):
    """job 的來源類型。"""

    FILE = "file"
    URL = "url"
    FEED_EPISODE = "feed_episode"


class JobStatus(str, Enum):
    """job 的處理狀態。"""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True)
class JobResult:
    """成功轉檔後的結果。txt_file / srt_file 是相對於 output_dir 的路徑。"""

    language: str
    duration: float
    text: str
    txt_file: str
    srt_file: str


def _now() -> datetime.datetime:
    return datetime.datetime.now()


@dataclass
class Job:
    """一筆轉檔任務。

    `source` 是處理時才需要的音檔來源，不會被持久化 —— 服務重啟後從磁碟
    載入的 job 其 `source` 為 None，因此無法被重跑 (會被標記為 error)。
    """

    id: str
    type: JobType
    topic: str | None
    language: str | None
    batch_size: int
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime.datetime = field(default_factory=_now)
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None
    result: JobResult | None = None
    error: str | None = None
    source: AudioSource | None = field(default=None, repr=False, compare=False)

    def mark_running(self) -> None:
        self.status = JobStatus.RUNNING
        self.started_at = _now()

    def mark_done(self, result: JobResult) -> None:
        self.status = JobStatus.DONE
        self.result = result
        self.finished_at = _now()

    def mark_error(self, message: str) -> None:
        self.status = JobStatus.ERROR
        self.error = message
        self.finished_at = _now()

    @property
    def is_terminal(self) -> bool:
        """是否已結束 (done 或 error)，可被淘汰。"""
        return self.status in (JobStatus.DONE, JobStatus.ERROR)
