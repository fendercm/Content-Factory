import json
import pytest
from unittest.mock import MagicMock, patch

from agents.script_writer import write_script, recommend_duration, PLATFORM_GUIDELINES

# 108 слов — попадает в диапазон пресета 45с (100-115 ±10%)
PROSE_SCRIPT = ("Ты прокрастинируешь не потому что ленивый — мозг защищается. " * 12).strip()

VALID_PARSE = {
    "hook": "Ты прокрастинируешь не потому что ленивый.",
    "body": "Мозг защищается от стресса.",
    "cta": "Подпишись — узнаешь как обмануть мозг!",
}


def _resp(content: str) -> MagicMock:
    """Имитирует ответ chat.completions: resp.choices[0].message.content."""
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


# ── Happy path ───────────────────────────────────────────────────────────────

@patch("utils.llm_client.groq_with_cerebras_fallback")
def test_write_script_returns_required_keys(mock_llm):
    mock_llm.side_effect = [
        _resp(PROSE_SCRIPT),
        _resp(json.dumps(VALID_PARSE, ensure_ascii=False)),
    ]
    result = write_script("психология прокрастинации", "tiktok")
    assert {"hook", "body", "cta", "full_script"} <= result.keys()
    assert result["full_script"] == PROSE_SCRIPT


@pytest.mark.parametrize("platform", list(PLATFORM_GUIDELINES.keys()))
@patch("utils.llm_client.groq_with_cerebras_fallback")
def test_write_script_all_platforms(mock_llm, platform):
    mock_llm.side_effect = [
        _resp(PROSE_SCRIPT),
        _resp(json.dumps(VALID_PARSE, ensure_ascii=False)),
    ]
    result = write_script("тема", platform)
    assert "hook" in result


@patch("utils.llm_client.groq_with_cerebras_fallback")
def test_write_script_prompt_includes_platform_name(mock_llm):
    mock_llm.side_effect = [
        _resp(PROSE_SCRIPT),
        _resp(json.dumps(VALID_PARSE, ensure_ascii=False)),
    ]
    write_script("тема", "youtube")

    first_call_kwargs = mock_llm.call_args_list[0][1]
    user_msg = next(
        m["content"] for m in first_call_kwargs["messages"] if m["role"] == "user"
    )
    assert "YOUTUBE" in user_msg


@patch("utils.llm_client.groq_with_cerebras_fallback")
def test_write_script_parse_stage_uses_json_response_format(mock_llm):
    mock_llm.side_effect = [
        _resp(PROSE_SCRIPT),
        _resp(json.dumps(VALID_PARSE, ensure_ascii=False)),
    ]
    write_script("тема", "tiktok")

    parse_call_kwargs = mock_llm.call_args_list[-1][1]
    assert parse_call_kwargs["response_format"] == {"type": "json_object"}


@patch("utils.llm_client.groq_with_cerebras_fallback")
def test_write_script_regenerates_when_too_short(mock_llm):
    short_text = "Слишком короткий сценарий."
    mock_llm.side_effect = [
        _resp(short_text),
        _resp(PROSE_SCRIPT),
        _resp(json.dumps(VALID_PARSE, ensure_ascii=False)),
    ]
    result = write_script("тема", "tiktok")
    assert result["full_script"] == PROSE_SCRIPT
    # 2 генерации + 1 парсинг
    assert mock_llm.call_count == 3


# ── Fallback-ветки ───────────────────────────────────────────────────────────

@patch("utils.llm_client.groq_with_cerebras_fallback")
def test_write_script_invalid_parse_json_synthesizes_fields(mock_llm):
    """Невалидный JSON на этапе парсинга → поля синтезируются из full_script."""
    mock_llm.side_effect = [
        _resp(PROSE_SCRIPT),
        _resp("не JSON {"),
    ]
    result = write_script("тема", "tiktok")
    assert result["full_script"] == PROSE_SCRIPT
    assert result["hook"]
    assert "body" in result
    assert "cta" in result


@patch("utils.llm_client.groq_with_cerebras_fallback")
def test_write_script_missing_fields_synthesized(mock_llm):
    """JSON без cta → синтез из текста сценария."""
    mock_llm.side_effect = [
        _resp(PROSE_SCRIPT),
        _resp(json.dumps({"hook": "только хук", "body": "тело"}, ensure_ascii=False)),
    ]
    result = write_script("тема", "tiktok")
    assert result["cta"]
    assert result["full_script"] == PROSE_SCRIPT


# ── Validation errors ────────────────────────────────────────────────────────

def test_write_script_invalid_platform_raises():
    with pytest.raises(ValueError, match="Неподдерживаемая платформа"):
        write_script("тема", "facebook")


@patch("utils.llm_client.groq_with_cerebras_fallback")
def test_write_script_stop_topic_raises(mock_llm):
    mock_llm.return_value = _resp("STOP: политика")
    with pytest.raises(ValueError, match="Сценарист отказался"):
        write_script("выборы президента", "tiktok")


# ── recommend_duration ───────────────────────────────────────────────────────

def test_recommend_duration_returns_preset():
    dur = recommend_duration("привычки утром", platform="tiktok")
    assert dur in (45, 60, 75, 90)
