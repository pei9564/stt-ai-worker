import datetime

from app.domain.transcript import TranscriptResult, TranscriptSegment
from app.infrastructure.output_writer import OutputWriter


def _result():
    return TranscriptResult(
        language="zh",
        duration=10.0,
        segments=(
            TranscriptSegment(0.0, 1.0, "第一句"),
            TranscriptSegment(1.0, 2.0, "第二句"),
        ),
    )


def test_write_creates_txt_and_srt_in_dated_dir(tmp_path):
    writer = OutputWriter(tmp_path)
    result = writer.write(_result(), "我的主題")

    day = datetime.date.today().isoformat()
    assert result.txt_file == f"{day}/我的主題.txt"
    assert result.srt_file == f"{day}/我的主題.srt"

    txt = (tmp_path / result.txt_file).read_text(encoding="utf-8")
    assert txt == "第一句\n第二句"
    srt = (tmp_path / result.srt_file).read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:01,000" in srt


def test_write_carries_result_metadata(tmp_path):
    result = OutputWriter(tmp_path).write(_result(), "topic")
    assert result.language == "zh"
    assert result.duration == 10.0
    assert result.text == "第一句\n第二句"


def test_write_dedups_filename_on_collision(tmp_path):
    writer = OutputWriter(tmp_path)
    first = writer.write(_result(), "重複")
    second = writer.write(_result(), "重複")

    assert first.txt_file.endswith("重複.txt")
    assert second.txt_file.endswith("重複_2.txt")


def test_write_sanitizes_unsafe_name(tmp_path):
    result = OutputWriter(tmp_path).write(_result(), "a/b*c.mp3")
    # 路徑分隔與星號都被換成底線
    assert "/" not in result.txt_file.split("/", 1)[1]
    assert "*" not in result.txt_file
