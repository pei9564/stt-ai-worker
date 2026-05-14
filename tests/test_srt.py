from app.domain.transcript import TranscriptSegment
from app.infrastructure.srt import format_timestamp, render_srt


def test_format_timestamp_zero():
    assert format_timestamp(0) == "00:00:00,000"


def test_format_timestamp_hours_minutes_seconds_millis():
    assert format_timestamp(3661.5) == "01:01:01,500"


def test_format_timestamp_rounds_millis():
    assert format_timestamp(0.0014) == "00:00:00,001"


def test_render_srt_numbers_and_joins_blocks():
    segments = [
        TranscriptSegment(0.0, 2.0, "你好"),
        TranscriptSegment(2.0, 4.0, "世界"),
    ]
    assert render_srt(segments) == (
        "1\n00:00:00,000 --> 00:00:02,000\n你好\n"
        "\n"
        "2\n00:00:02,000 --> 00:00:04,000\n世界\n"
    )


def test_render_srt_empty():
    assert render_srt([]) == ""
