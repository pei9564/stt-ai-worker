"""各種音檔來源的實作，以及 RSS feed 解析。

每個 AudioSource 子類別把「如何取得本地音檔」封裝起來：
  - YtDlpSource：YouTube / 各影音站 / podcast 單集頁，用 yt-dlp 抽音訊
  - DirectUrlSource：直連音檔 URL (RSS enclosure 多半如此)
"""

from __future__ import annotations

import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import feedparser
import requests

from app.domain.audio_source import AudioSource
from app.infrastructure.naming import safe_stem

_AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".wav")


# ---- 下載函式 (可在測試中以注入方式替換) ----
def download_with_ytdlp(url: str, dest_dir: Path) -> Path:
    """用 yt-dlp 從 YouTube / 各影音站 / podcast 單集頁抽出音訊。"""
    out_tmpl = str(dest_dir / "%(title).80s.%(ext)s")
    cmd = [
        "yt-dlp", "-x", "--audio-format", "mp3", "--no-playlist",
        "--no-progress", "-o", out_tmpl, "--print", "after_move:filepath", url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp 失敗: {result.stderr.strip()[-400:]}")
    return Path(result.stdout.strip().splitlines()[-1])


def download_file(url: str, dest_dir: Path, name_hint: str) -> Path:
    """直接下載音檔連結。"""
    dest = dest_dir / f"{safe_stem(name_hint)}.mp3"
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    return dest


Downloader = Callable[..., Path]


# ---- AudioSource 實作 ----
class YtDlpSource(AudioSource):
    """YouTube / 影音 / podcast 單集網址，下載後的檔名 (通常是影片標題) 即 display_name。"""

    def __init__(self, url: str, downloader: Downloader = download_with_ytdlp):
        self._url = url
        self._downloader = downloader
        self._name = url  # 下載完成後更新成實際檔名

    @property
    def display_name(self) -> str:
        return self._name

    @contextmanager
    def acquire(self) -> Iterator[Path]:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._downloader(self._url, Path(tmp))
            self._name = path.name
            yield path


class DirectUrlSource(AudioSource):
    """直連音檔 URL，搭配一個已知的標題 (例如 RSS 單集標題)。"""

    def __init__(self, audio_url: str, title: str, downloader: Downloader = download_file):
        self._audio_url = audio_url
        self._title = title
        self._downloader = downloader

    @property
    def display_name(self) -> str:
        return self._title

    @contextmanager
    def acquire(self) -> Iterator[Path]:
        with tempfile.TemporaryDirectory() as tmp:
            yield self._downloader(self._audio_url, Path(tmp), self._title)


class LocalFileSource(AudioSource):
    """本地音檔來源（例如直接上傳的檔案）。

    檔案已由 API 寫入本地暫存位置，acquire() 只需要 yield 該路徑，
    保留檔案供失敗重試；成功或刪除任務後由 cleanup() 清理。
    """

    def __init__(self, file_path: Path, display_name: str):
        self._file_path = Path(file_path)
        self._display_name = display_name

    @property
    def display_name(self) -> str:
        return self._display_name

    @contextmanager
    def acquire(self) -> Iterator[Path]:
        if not self._file_path.is_file():
            raise RuntimeError("上傳音檔已不存在，請重新上傳")
        yield self._file_path

    def cleanup(self) -> None:
        self._file_path.unlink(missing_ok=True)



# ---- RSS feed 解析 ----
@dataclass(frozen=True)
class FeedEpisode:
    """RSS 中的一集：標題 + 音檔 URL。"""

    title: str
    audio_url: str


def _find_audio_url(entry) -> str | None:
    """從一個 RSS entry 的 enclosures 找出音檔 URL。"""
    for enc in entry.get("enclosures", []):
        href = enc.get("href", "")
        is_audio = "audio" in enc.get("type", "") or href.lower().split("?")[0].endswith(
            _AUDIO_EXTS
        )
        if is_audio:
            return href
    return None


def extract_episodes(entries, limit: int) -> list[FeedEpisode]:
    """從 RSS entries 取出有音檔的集數。limit=0 表示全抓。純函式，方便測試。"""
    episodes = [
        FeedEpisode(entry.get("title", "episode"), audio_url)
        for entry in entries
        if (audio_url := _find_audio_url(entry))
    ]
    return episodes[:limit] if limit else episodes


def parse_feed(feed_url: str, limit: int) -> list[FeedEpisode]:
    """解析 RSS feed URL，回傳集數清單。解析失敗丟 RuntimeError。"""
    parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"RSS 解析失敗: {parsed.bozo_exception}")
    return extract_episodes(parsed.entries, limit)


def serialize_source(source: AudioSource | None) -> dict | None:
    """只持久化來源描述，不儲存 downloader 或 Python 物件。"""
    if source is None:
        return None
    if isinstance(source, YtDlpSource):
        return {"kind": "ytdlp", "url": source._url}
    if isinstance(source, DirectUrlSource):
        return {"kind": "direct", "url": source._audio_url, "title": source._title}
    if isinstance(source, LocalFileSource):
        return {"kind": "file", "path": str(source._file_path), "title": source.display_name}
    raise ValueError(f"來源不支援持久化: {type(source).__name__}")


def deserialize_source(data: dict | None) -> AudioSource | None:
    if not data:
        return None
    if data["kind"] == "ytdlp":
        return YtDlpSource(data["url"])
    if data["kind"] == "direct":
        return DirectUrlSource(data["url"], data["title"])
    if data["kind"] == "file":
        return LocalFileSource(Path(data["path"]), data["title"])
    raise ValueError(f"未知音訊來源: {data['kind']}")
