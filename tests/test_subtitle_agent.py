import pytest
from unittest.mock import MagicMock, patch

from agents.subtitle_agent import generate_subtitles, _ts, _chunk_words


# ── Вспомогательные функции ──────────────────────────────────────────────────

def test_ts_zero():
    assert _ts(0) == "0:00:00.00"

def test_ts_minutes():
    assert _ts(90.5) == "0:01:30.50"

def test_ts_hours():
    assert _ts(3661.25) == "1:01:01.25"


def test_chunk_words_splits_by_max_chars():
    words = [
        {"word": "один", "start": 0.0, "end": 0.3},
        {"word": "два", "start": 0.3, "end": 0.6},
        {"word": "три", "start": 0.6, "end": 0.9},
        {"word": "четыре", "start": 0.9, "end": 1.3},
        {"word": "пять", "start": 1.3, "end": 1.6},
    ]
    chunks = _chunk_words(words, max_chars=10)
    assert len(chunks) >= 2
    for c in chunks:
        assert "text" in c and "start" in c and "end" in c


def test_chunk_words_single_chunk_when_short():
    words = [
        {"word": "ок", "start": 0.0, "end": 0.5},
        {"word": "да", "start": 0.5, "end": 1.0},
    ]
    chunks = _chunk_words(words, max_chars=50)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "ок да"


def test_chunk_words_skips_empty_words():
    words = [
        {"word": "", "start": 0.0, "end": 0.1},
        {"word": "слово", "start": 0.1, "end": 0.8},
    ]
    chunks = _chunk_words(words, max_chars=20)
    assert chunks[0]["text"] == "слово"


# ── Validation errors ────────────────────────────────────────────────────────

def test_generate_subtitles_invalid_platform():
    with pytest.raises(ValueError, match="Неподдерживаемая платформа"):
        generate_subtitles("/fake/audio.mp3", "facebook")


def test_generate_subtitles_whisper_not_installed():
    import sys
    # Сбрасываем кэш модели и блокируем import whisper
    with patch("agents.subtitle_agent._whisper_model", None), \
         patch.dict(sys.modules, {"whisper": None}):
        with pytest.raises(EnvironmentError, match="openai-whisper не установлен"):
            generate_subtitles("/fake/audio.mp3", "tiktok")


# ── Happy path ───────────────────────────────────────────────────────────────

def _mock_model(segments):
    model = MagicMock()
    model.transcribe.return_value = {"segments": segments}
    return model


@patch("agents.subtitle_agent._write_ass")
@patch("agents.subtitle_agent.os.makedirs")
def test_generate_subtitles_word_timestamps(mock_makedirs, mock_write_ass):
    model = _mock_model([{
        "text": "Привет мир",
        "start": 0.0,
        "end": 1.5,
        "words": [
            {"word": "Привет", "start": 0.0, "end": 0.7},
            {"word": "мир",    "start": 0.7, "end": 1.5},
        ],
    }])

    with patch("agents.subtitle_agent._get_whisper_model", return_value=model):
        result = generate_subtitles("/fake/audio.mp3", "tiktok")

    assert {"subtitle_path", "segment_count", "words"} <= result.keys()
    assert result["segment_count"] >= 1
    assert result["subtitle_path"].endswith(".ass")
    # word-level тайминги пробрасываются для entity_visuals
    assert result["words"][0] == {"text": "Привет", "start": 0.0, "end": 0.7}
    mock_write_ass.assert_called_once()


@patch("agents.subtitle_agent._write_ass")
@patch("agents.subtitle_agent.os.makedirs")
def test_generate_subtitles_fallback_no_words(mock_makedirs, mock_write_ass):
    model = _mock_model([
        {"text": "Первое предложение.", "start": 0.0, "end": 2.0},
        {"text": "Второе предложение.", "start": 2.0, "end": 4.0},
    ])

    with patch("agents.subtitle_agent._get_whisper_model", return_value=model):
        result = generate_subtitles("/fake/audio.mp3", "youtube")

    assert result["segment_count"] == 2
    assert result["words"] == []


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "instagram"])
@patch("agents.subtitle_agent._write_ass")
@patch("agents.subtitle_agent.os.makedirs")
def test_generate_subtitles_all_platforms(mock_makedirs, mock_write_ass, platform):
    model = _mock_model([{
        "text": "Текст", "start": 0.0, "end": 1.0,
        "words": [{"word": "Текст", "start": 0.0, "end": 1.0}],
    }])

    with patch("agents.subtitle_agent._get_whisper_model", return_value=model):
        result = generate_subtitles("/fake/audio.mp3", platform)

    assert result["subtitle_path"].endswith(f"{platform}.ass")
