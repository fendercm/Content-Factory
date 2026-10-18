import pytest
from unittest.mock import patch

from agents.voice_agent import (
    generate_voice,
    voice_for_niche,
    PLATFORM_SETTINGS,
    SILERO_SPEAKER,
)


# ── voice_for_niche ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("niche,expected", [
    ("психология", "alena"),
    ("бизнес", "filipp"),
    ("здоровье", "jane"),
    ("философия", "ermil"),
    ("наука", "lea"),
])
def test_voice_for_niche_known(niche, expected):
    assert voice_for_niche(niche) == expected


def test_voice_for_niche_unknown_returns_none():
    assert voice_for_niche("кулинария") is None


def test_voice_for_niche_empty_returns_none():
    assert voice_for_niche("") is None


def test_voice_for_niche_case_and_spaces():
    assert voice_for_niche("  Бизнес  ") == "filipp"


# ── Validation errors ────────────────────────────────────────────────────────

def test_generate_voice_invalid_platform_raises():
    with pytest.raises(ValueError, match="Неподдерживаемая платформа"):
        generate_voice("Текст", "facebook")


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
def test_generate_voice_empty_text_raises(text):
    with pytest.raises(ValueError, match="full_script не может быть пустым"):
        generate_voice(text, "tiktok")


# ── Engine chain (внешние движки отключены через пустые ключи) ───────────────

def _no_cloud_engines():
    """Отключает ElevenLabs/CosyVoice/Yandex — остаются Silero и edge-tts."""
    return [
        patch("agents.voice_agent.KIE_API_KEY", ""),
        patch("agents.voice_agent.DASHSCOPE_API_KEY", ""),
        patch.dict("os.environ", {"YANDEX_API_KEY": ""}),
        patch("agents.voice_agent.os.makedirs"),
        patch("agents.voice_agent._normalize_volume"),
        patch("agents.voice_agent.os.path.getsize", return_value=2048),
    ]


def test_generate_voice_silero_happy_path():
    patches = _no_cloud_engines()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patch("agents.voice_agent._silero") as mock_silero, \
         patch("agents.voice_agent.os.path.exists", return_value=True):
        result = generate_voice("Текст сценария", "tiktok")

    mock_silero.assert_called_once()
    assert {"audio_path", "voice", "platform", "engine"} <= result.keys()
    assert result["platform"] == "tiktok"
    assert result["engine"] == "silero"
    assert result["voice"] == SILERO_SPEAKER[PLATFORM_SETTINGS["tiktok"]["gender"]]
    assert result["audio_path"].endswith(".mp3")


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "instagram", "linkedin", "twitter"])
def test_generate_voice_all_platforms(platform):
    patches = _no_cloud_engines()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patch("agents.voice_agent._silero"), \
         patch("agents.voice_agent.os.path.exists", return_value=True):
        result = generate_voice("Текст", platform)
    assert result["platform"] == platform


def test_generate_voice_falls_back_to_edge_tts_when_silero_fails():
    patches = _no_cloud_engines()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patch("agents.voice_agent._silero", side_effect=RuntimeError("torch недоступен")), \
         patch("agents.voice_agent._edge_tts") as mock_edge, \
         patch("agents.voice_agent.os.path.exists", return_value=True):
        result = generate_voice("Текст", "tiktok")

    mock_edge.assert_called_once()
    assert result["engine"] == "edge-tts"
    assert result["voice"] == PLATFORM_SETTINGS["tiktok"]["edge_voice"]


def test_generate_voice_edge_voice_override():
    custom_voice = "ru-RU-DmitryNeural"
    patches = _no_cloud_engines()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patch("agents.voice_agent._silero", side_effect=RuntimeError("fail")), \
         patch("agents.voice_agent._edge_tts"), \
         patch("agents.voice_agent.os.path.exists", return_value=True):
        result = generate_voice("Текст", "tiktok", voice=custom_voice)
    assert result["voice"] == custom_voice


def test_generate_voice_silero_speaker_override():
    patches = _no_cloud_engines()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patch("agents.voice_agent._silero"), \
         patch("agents.voice_agent.os.path.exists", return_value=True):
        result = generate_voice("Текст", "tiktok", voice="aidar")
    assert result["voice"] == "aidar"


def test_generate_voice_no_file_created_raises():
    # Цепочка: edge-tts → Silero (офлайн-резерв последним). Если файла нет
    # после обоих движков — ошибка последнего, Silero.
    patches = _no_cloud_engines()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patch("agents.voice_agent._silero") as mock_silero, \
         patch("agents.voice_agent._edge_tts") as mock_edge, \
         patch("agents.voice_agent.os.path.exists", return_value=False):
        with pytest.raises(RuntimeError, match="Silero не создал файл"):
            generate_voice("Текст", "tiktok")
    mock_edge.assert_called_once()
    mock_silero.assert_called_once()
