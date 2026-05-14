"""SRT 字幕格式的純函式。沒有 I/O，方便直接測試。"""

from __future__ import annotations

from typing import Iterable

from app.domain.transcript import TranscriptSegment


def format_timestamp(seconds: float) -> str:
    """把秒數轉成 SRT 時間碼 `HH:MM:SS,mmm`。"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def render_srt(segments: Iterable[TranscriptSegment]) -> str:
    """把逐字稿片段組成完整的 SRT 內容。"""
    blocks = [
        f"{i}\n{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}\n{seg.text}\n"
        for i, seg in enumerate(segments, 1)
    ]
    return "\n".join(blocks)
