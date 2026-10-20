"""
Тесты LLM-цепочки (utils/llm_client.py) и точности промптовой обвязки:
fallback между провайдерами, circuit breaker, разбор ответов Gemini,
выравнивание субтитров по сценарию, защиты hook A/B и видеозапросов.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


def _gemini_http(status: int, payload: dict) -> MagicMock:
    r = MagicMock(status_code=status)
    r.json.return_value = payload
    r.text = json.dumps(payload)
    return r


@pytest.fixture(autouse=True)
def _fresh_providers(monkeypatch):
    """Каждый тест — с чистым circuit breaker и предсказуемым окружением."""
    import utils.llm_client as llm_client
    import utils.providers as providers
    providers._dead.clear()
    # Учёт стоимости пишет в боевой pipeline.db — в тестах не нужен
    monkeypatch.setattr(llm_client, "_track", lambda *a, **k: None)
    monkeypatch.setenv("BUDGET_MODE", "free")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("GROQ_API_KEY", "q-key")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DISABLED_PROVIDERS", raising=False)
    yield
    providers._dead.clear()


def test_gemini_answer_skips_thought_parts_and_code_fence():
    from utils import llm_client
    body = {"candidates": [{"content": {"parts": [
        {"text": "рассуждаю...", "thought": True},
        {"text": "```json\n{\"ok\": true}\n```"},
    ]}}]}
    with patch.object(llm_client._requests, "post", return_value=_gemini_http(200, body)):
        resp = llm_client.chat([{"role": "user", "content": "x"}], json_mode=True)
    assert resp.provider == "gemini"
    assert json.loads(resp.choices[0].message.content) == {"ok": True}


def test_daily_quota_marks_model_dead_and_falls_back_to_groq():
    from utils import llm_client, providers
    quota = {"error": {"code": 429, "message": "Quota exceeded",
                       "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    groq_resp = MagicMock()
    groq_resp.choices = [MagicMock(message=MagicMock(content="ответ groq"))]
    client = MagicMock()
    client.chat.completions.create.return_value = groq_resp
    with patch.object(llm_client._requests, "post", return_value=_gemini_http(429, quota)), \
         patch.object(llm_client, "_openai_client", return_value=client):
        resp = llm_client.chat([{"role": "user", "content": "x"}], tier="fast")
    assert resp.provider == "groq"
    assert not providers.is_enabled(f"gemini:{llm_client.gemini_fast_model()}")
    # gpt-oss: запас токенов под рассуждение и низкий reasoning_effort
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-oss-120b"
    assert kwargs["reasoning_effort"] == "low"


def test_overload_503_does_not_kill_model():
    from utils import llm_client, providers
    busy = {"error": {"code": 503, "message": "high demand"}}
    ok = {"candidates": [{"content": {"parts": [{"text": "готово"}]}}]}
    with patch.object(llm_client._requests, "post", side_effect=[_gemini_http(503, busy), _gemini_http(200, ok)]):
        resp = llm_client.chat([{"role": "user", "content": "x"}], tier="quality")
    # quality-модель перегружена → ответила fast-модель, но quality не выключена
    assert resp.model == llm_client.gemini_fast_model() or resp.provider == "gemini"
    assert providers.is_enabled(f"gemini:{llm_client.gemini_quality_model()}")


def test_all_providers_failed_raises():
    from utils import llm_client
    bad = {"error": {"code": 500, "message": "boom"}}
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("Error code: 401 invalid api key")
    with patch.object(llm_client._requests, "post", return_value=_gemini_http(500, bad)), \
         patch.object(llm_client, "_openai_client", return_value=client):
        with pytest.raises(llm_client.AllProvidersFailed):
            llm_client.chat([{"role": "user", "content": "x"}])


def test_cerebras_is_paid_in_budget_mode():
    from utils.providers import is_enabled
    assert is_enabled("cerebras") is False


def test_subtitle_alignment_uses_script_spelling():
    from agents.subtitle_agent import _align_to_script
    asr = [{"word": w, "start": i * 0.4, "end": i * 0.4 + 0.35} for i, w in enumerate(
        ["Ты", "откладывал", "дела", "сто", "двадцать", "раз", "потому", "что", "боишься", "оценки"])]
    words = _align_to_script(asr, "Ты откладываешь дела 120 раз, потому что боишься оценки.")
    assert [w["word"] for w in words] == ["Ты", "откладываешь", "дела", "120", "раз,", "потому",
                                          "что", "боишься", "оценки."]
    # «120» занимает отрезок «сто двадцать»
    w120 = words[3]
    assert w120["start"] == pytest.approx(1.2) and w120["end"] == pytest.approx(1.95)
    assert all(b["start"] >= a["start"] for a, b in zip(words, words[1:]))


def test_subtitle_alignment_rejects_unrelated_audio():
    from agents.subtitle_agent import _align_to_script
    asr = [{"word": w, "start": 0, "end": 0.3} for w in ["совсем", "другой", "текст", "здесь"]]
    assert _align_to_script(asr, "Ты откладываешь дела не из-за лени.") is None


def test_hook_ab_rejects_invented_numbers():
    from agents import script_writer
    script = "Ты откладываешь дела не из-за лени. Мозг защищается. А ты?"
    llm = MagicMock()
    llm.choices = [MagicMock(message=MagicMock(content=json.dumps({
        "alternatives": ["94% людей откладывают важное каждый день", "Перестань винить себя"],
        "all_hooks": ["Ты откладываешь дела не из-за лени", "94% людей откладывают важное каждый день",
                      "Перестань винить себя"],
        "chosen_index": 1,
    }, ensure_ascii=False)))]
    with patch("utils.llm_client.groq_with_cerebras_fallback", return_value=llm):
        res = script_writer.improve_hook(script, "прокрастинация")
    assert res["chosen_index"] == 0
    assert res["script"] == script


def test_visual_queries_filter_dark_and_text_scenes():
    from agents.visual_agent import _query_ok
    assert _query_ok("person thinking daylight")
    for q in ["dark night street", "man silhouette window", "people holding signs",
              "scientist with documents", "crowded market"]:
        assert not _query_ok(q), q


def test_script_writer_prompt_includes_brief_and_avoid_hooks():
    from agents import script_writer
    prose = ("Ты прокрастинируешь не потому что ленивый — мозг защищается. " * 14).strip()
    first = MagicMock(); first.choices = [MagicMock(message=MagicMock(content=prose))]
    second = MagicMock(); second.choices = [MagicMock(message=MagicMock(content='{"hook": "h", "body": "b", "cta": "c"}'))]
    with patch("utils.llm_client.groq_with_cerebras_fallback", side_effect=[first, second]) as llm:
        script_writer.write_script("тема", "youtube", idea_hook="Крючок брифа", idea_angle="Угол брифа",
                                   avoid_hooks=["Старый hook канала"])
    prompt = llm.call_args_list[0].kwargs["messages"][1]["content"]
    assert "Крючок брифа" in prompt and "Угол брифа" in prompt
    assert "Старый hook канала" in prompt
