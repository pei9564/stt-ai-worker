from app.infrastructure.naming import safe_stem, unique_stem


def test_safe_stem_drops_extension():
    assert safe_stem("hello world.mp3") == "hello world"


def test_safe_stem_keeps_cjk():
    assert safe_stem("記憶體類股還可以買嗎.mp3") == "記憶體類股還可以買嗎"


def test_safe_stem_replaces_unsafe_chars():
    assert safe_stem("foo!@#bar.mp3") == "foo___bar"


def test_safe_stem_caps_length_at_80():
    assert len(safe_stem("a" * 200)) == 80


def test_safe_stem_empty_falls_back_to_audio():
    assert safe_stem("") == "audio"
    assert safe_stem("   .mp3") == "audio"


def test_unique_stem_no_collision(tmp_path):
    assert unique_stem(tmp_path, "ep") == "ep"


def test_unique_stem_increments_on_collision(tmp_path):
    (tmp_path / "ep.txt").touch()
    assert unique_stem(tmp_path, "ep") == "ep_2"
    (tmp_path / "ep_2.txt").touch()
    assert unique_stem(tmp_path, "ep") == "ep_3"
