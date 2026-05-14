"""把轉檔結果寫成 output/{日期}/{topic}.{txt,srt}。"""

from __future__ import annotations

import datetime
import pathlib

from app.domain.job import JobResult
from app.domain.transcript import TranscriptResult
from app.infrastructure.naming import safe_stem, unique_stem
from app.infrastructure.srt import render_srt


class OutputWriter:
    """負責逐字稿落地。檔名以日期分資料夾，撞檔自動加序號。"""

    def __init__(self, output_dir: pathlib.Path):
        self._output_dir = output_dir

    def write(self, result: TranscriptResult, name: str) -> JobResult:
        """寫出 txt + srt，回傳含相對路徑的 JobResult。

        `name` 為 topic 或來源名稱，會經過 safe_stem 清理。
        """
        day = datetime.date.today().isoformat()
        day_dir = self._output_dir / day
        day_dir.mkdir(parents=True, exist_ok=True)

        stem = unique_stem(day_dir, safe_stem(name), suffix=".txt")
        (day_dir / f"{stem}.txt").write_text(result.text, encoding="utf-8")
        (day_dir / f"{stem}.srt").write_text(render_srt(result.segments), encoding="utf-8")

        return JobResult(
            language=result.language,
            duration=result.duration,
            text=result.text,
            txt_file=f"{day}/{stem}.txt",
            srt_file=f"{day}/{stem}.srt",
        )
