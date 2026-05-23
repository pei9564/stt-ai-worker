from pathlib import Path

from app.infrastructure.audio_sources import (
    DirectUrlSource,
    YtDlpSource,
)


def test_ytdlp_source_updates_display_name_after_download(tmp_path):
    def fake_downloader(url, dest_dir):
        path = Path(dest_dir) / "影片標題.mp3"
        path.write_bytes(b"audio")
        return path

    source = YtDlpSource("http://youtube/watch?v=x", downloader=fake_downloader)
    assert source.display_name == "http://youtube/watch?v=x"  # 下載前

    with source.acquire() as path:
        assert path.exists()
        assert source.display_name == "影片標題.mp3"  # 下載後變成檔名
        workdir = path.parent
    assert not workdir.exists()  # 暫存資料夾已清掉


def test_direct_url_source_passes_title_to_downloader(tmp_path):
    seen = {}

    def fake_downloader(url, dest_dir, name_hint):
        seen["url"] = url
        seen["name_hint"] = name_hint
        path = Path(dest_dir) / "ep.mp3"
        path.write_bytes(b"audio")
        return path

    source = DirectUrlSource("http://feed/ep.mp3", "第一集", downloader=fake_downloader)
    assert source.display_name == "第一集"

    with source.acquire() as path:
        assert path.exists()
    assert seen == {"url": "http://feed/ep.mp3", "name_hint": "第一集"}
