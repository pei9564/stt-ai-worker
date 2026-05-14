from app.infrastructure.audio_sources import FeedEpisode, extract_episodes


def _entry(title, enclosures):
    return {"title": title, "enclosures": enclosures}


def test_extract_picks_audio_enclosure_by_type():
    entries = [_entry("Ep1", [{"href": "http://x/a", "type": "audio/mpeg"}])]
    assert extract_episodes(entries, 0) == [FeedEpisode("Ep1", "http://x/a")]


def test_extract_picks_audio_enclosure_by_extension():
    entries = [_entry("Ep1", [{"href": "http://x/a.mp3?token=1", "type": ""}])]
    assert extract_episodes(entries, 0) == [FeedEpisode("Ep1", "http://x/a.mp3?token=1")]


def test_extract_skips_non_audio_entries():
    entries = [
        _entry("HasImage", [{"href": "http://x/cover.jpg", "type": "image/jpeg"}]),
        _entry("NoEnclosure", []),
    ]
    assert extract_episodes(entries, 0) == []


def test_extract_respects_limit():
    entries = [_entry(f"Ep{i}", [{"href": f"http://x/{i}.mp3", "type": ""}]) for i in range(5)]
    assert len(extract_episodes(entries, 2)) == 2
    assert len(extract_episodes(entries, 0)) == 5


def test_extract_defaults_missing_title():
    entries = [{"enclosures": [{"href": "http://x/a.mp3", "type": ""}]}]
    assert extract_episodes(entries, 0)[0].title == "episode"
