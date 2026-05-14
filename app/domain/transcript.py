"""轉檔結果的值物件。純資料，不碰 I/O。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptSegment:
    """一段帶時間軸的逐字稿。start / end 單位為秒。"""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptResult:
    """transcriber 的輸出：偵測到的語言、音檔長度、所有逐字稿片段。"""

    language: str
    duration: float
    segments: tuple[TranscriptSegment, ...]

    @property
    def text(self) -> str:
        """把所有片段串成純文字逐字稿 (一段一行)。"""
        return "\n".join(seg.text for seg in self.segments)
