"""各種音檔來源的實作，以及 RSS feed 解析。

每個 AudioSource 子類別把「如何取得本地音檔」封裝起來：
  - YtDlpSource：YouTube / 各影音站 / podcast 單集頁，用 yt-dlp 抽音訊
  - DirectUrlSource：直連音檔 URL (RSS enclosure 多半如此)
"""

from __future__ import annotations

import subprocess
import os
import signal
import time
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import feedparser
import requests

from app.domain.audio_source import AudioSource
from app.infrastructure.limits import positive_env, require_disk
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
    maximum = positive_env('MAX_DOWNLOAD_BYTES', 512 * 1024 * 1024)
    deadline = time.monotonic() + positive_env('DOWNLOAD_TIMEOUT_SECONDS', 600)
    require_disk(dest_dir)
    cmd[1:1] = ['--max-filesize', str(maximum), '--socket-timeout', '20', '--retries', '2', '--fragment-retries', '2']
    # File-backed logs avoid unbounded PIPE buffers; terminate ffmpeg children as well.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(cmd, stdout=output, stderr=errors, start_new_session=True)
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise TimeoutError('下載逾時，請稍後重試或上傳本地音檔')
                if sum(p.stat().st_size for p in dest_dir.rglob('*') if p.is_file()) > maximum:
                    raise ValueError('下載音檔超過大小限制')
                require_disk(dest_dir)
                time.sleep(0.1)
            if process.returncode:
                raise RuntimeError('影音下載失敗，請確認來源仍可存取，或改用本地上傳')
            output.seek(0)
            lines = output.read().decode().strip().splitlines()
            if not lines:
                raise ValueError('未取得音檔，可能超過大小限制或來源不可用')
            path = Path(lines[-1])
            if path.stat().st_size > maximum:
                raise ValueError('下載音檔超過大小限制')
            return path
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


def download_file(url: str, dest_dir: Path, name_hint: str) -> Path:
    """直接下載音檔連結。"""
    dest = dest_dir / f"{safe_stem(name_hint)}.mp3"
    maximum = positive_env('MAX_DOWNLOAD_BYTES', 512 * 1024 * 1024)
    deadline = time.monotonic() + positive_env('DOWNLOAD_TIMEOUT_SECONDS', 600)
    require_disk(dest_dir)
    with requests.get(url, stream=True, timeout=(10, 20)) as r:
        r.raise_for_status()
        if int(r.headers.get('Content-Length', 0)) > maximum:
            raise ValueError('下載音檔超過大小限制')
        size = 0
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                size += len(chunk)
                if size > maximum:
                    raise ValueError('下載音檔超過大小限制')
                if time.monotonic() > deadline:
                    raise TimeoutError('下載逾時，請稍後重試')
                require_disk(dest_dir)
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

    def __init__(self, file_path: Path, display_name: str, sha256: str | None = None):
        self.sha256 = sha256
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
    with requests.get(feed_url, stream=True, timeout=(10, 20)) as response:
        response.raise_for_status()
        data = bytearray()
        deadline = time.monotonic() + 60
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > 5 * 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError('RSS 超過大小／時間限制')
    parsed = feedparser.parse(bytes(data))
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
        return {"kind": "file", "path": str(source._file_path), "title": source.display_name, "sha256": source.sha256}
    raise ValueError(f"來源不支援持久化: {type(source).__name__}")


def deserialize_source(data: dict | None) -> AudioSource | None:
    if not data:
        return None
    if data["kind"] == "ytdlp":
        return YtDlpSource(data["url"])
    if data["kind"] == "direct":
        return DirectUrlSource(data["url"], data["title"])
    if data["kind"] == "file":
        return LocalFileSource(Path(data["path"]), data["title"], data.get("sha256"))
    raise ValueError(f"未知音訊來源: {data['kind']}")
