"""The UI must reflect runtime limits rather than advertise invalid defaults."""
import re

from app.api.web import WebUI
from app.config import Settings


def test_ui_reflects_restricted_runtime_limits():
    settings = Settings(
        model='medium', device='cpu', compute_type='int8', language=None,
        batch_size=8, db_url='unused', jobs_window_hours=12,
        max_upload_bytes=1048576, max_audio_seconds=60,
        max_feed_episodes=1, max_batch_size=2,
    )
    html = WebUI(settings).render_index()
    assert not re.search(r'__[A-Z]+__', html)
    assert '近 12 小時的任務' in html
    assert '單檔上限 1.0 MB' in html
    assert re.search(r'id="feedLimit"\s+value="1"', html)
    batches = re.findall(r'<select id="batch-[^"]+">(.*?)</select>', html, re.S)
    assert len(batches) == 3
    for batch in batches:
        assert 'value="2" selected' in batch
        assert 'value="4"' not in batch
        assert 'value="8"' not in batch


def test_model_name_is_rendered_as_text():
    settings = Settings(
        model='<script>bad()</script>', device='cpu', compute_type='int8',
        language=None, batch_size=4, db_url='unused', jobs_window_hours=48,
    )
    html = WebUI(settings).render_index()
    assert '<script>bad()</script>' not in html
    assert '&lt;script&gt;bad()&lt;/script&gt;' in html
