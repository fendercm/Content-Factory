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
    monkeypatch.delenv("LLM_TRANSIENT_RETRIES", raising=False)
    monkeypatch.delenv("LLM_RETRY_DELAYS", raising=False)
    # Паузы между проходами цепочки — без реального ожидания
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
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


# ── Повторные проходы при временных сбоях ────────────────────────────────────

_OK = {"candidates": [{"content": {"parts": [{"text": "готово"}]}}]}


def _gemini_by_model(script: dict):
    """side_effect для requests.post: модель из URL → очередь ответов/исключений; лог вызовов в .calls."""
    calls: list[str] = []

    def post(url, **kwargs):
        model = url.split("/models/")[1].split(":")[0]
        calls.append(model)
        item = script[model].pop(0) if len(script[model]) > 1 else script[model][0]
        if isinstance(item, Exception):
            raise item
        return item

    post.calls = calls
    return post


@pytest.fixture
def free_chain(monkeypatch):
    """Только Gemini/Gemma: Groq выключен, NVIDIA/OpenRouter без ключей, платные в BUDGET_MODE=free."""
    monkeypatch.setenv("DISABLED_PROVIDERS", "groq")
    monkeypatch.delenv("BUDGET_ALLOW", raising=False)
    sleeps: list[float] = []
    from utils import llm_client
    monkeypatch.setattr(llm_client.time, "sleep", sleeps.append)
    return sleeps


def test_transient_outage_recovers_on_second_pass(free_chain):
    """Сценарий 2026-10-23: flash 503, flash-lite ConnectTimeout, gemma 500 → через паузу проходит."""
    import requests
    from utils import llm_client
    q, f, g = llm_client.gemini_quality_model(), llm_client.gemini_fast_model(), llm_client.gemma_model()
    post = _gemini_by_model({
        q: [_gemini_http(503, {"error": {"message": "high demand"}}), _gemini_http(200, _OK)],
        f: [requests.ConnectTimeout("connect timeout")],
        g: [_gemini_http(500, {"error": {"message": "internal"}})],
    })
    with patch.object(llm_client._requests, "post", side_effect=post):
        resp = llm_client.chat([{"role": "user", "content": "x"}], tier="quality", caller="anthology_shots")
    assert resp.choices[0].message.content == "готово" and resp.model == q
    assert free_chain == [20.0]
    assert post.calls == [q, f, g, q]


def test_transient_retries_exhausted_raise_after_all_passes(free_chain):
    from utils import llm_client
    busy = _gemini_http(503, {"error": {"message": "high demand"}})
    with patch.object(llm_client._requests, "post", return_value=busy) as post:
        with pytest.raises(llm_client.AllProvidersFailed, match="проходов: 3"):
            llm_client.chat([{"role": "user", "content": "x"}], tier="quality")
    assert free_chain == [20.0, 60.0]
    assert post.call_count == 9  # 3 модели × 3 прохода


def test_retry_policy_from_env(free_chain, monkeypatch):
    from utils import llm_client
    monkeypatch.setenv("LLM_TRANSIENT_RETRIES", "3")
    monkeypatch.setenv("LLM_RETRY_DELAYS", "5")
    busy = _gemini_http(503, {"error": {"message": "busy"}})
    with patch.object(llm_client._requests, "post", return_value=busy):
        with pytest.raises(llm_client.AllProvidersFailed):
            llm_client.chat([{"role": "user", "content": "x"}])
    assert free_chain == [5.0, 5.0, 5.0]  # пауз меньше, чем повторов, — последняя повторяется

    free_chain.clear()
    monkeypatch.setenv("LLM_TRANSIENT_RETRIES", "0")
    with patch.object(llm_client._requests, "post", return_value=busy) as post:
        with pytest.raises(llm_client.AllProvidersFailed):
            llm_client.chat([{"role": "user", "content": "x"}])
    assert free_chain == [] and post.call_count == 2  # fast + gemma, без повторов


def test_dead_and_skip_providers_are_not_retried(free_chain):
    from utils import llm_client, providers
    q, f, g = llm_client.gemini_quality_model(), llm_client.gemini_fast_model(), llm_client.gemma_model()
    post = _gemini_by_model({
        q: [_gemini_http(404, {"error": {"message": "not found"}})],
        f: [_gemini_http(400, {"error": {"message": "bad request"}})],
        g: [_gemini_http(503, {"error": {"message": "busy"}}), _gemini_http(200, _OK)],
    })
    with patch.object(llm_client._requests, "post", side_effect=post):
        resp = llm_client.chat([{"role": "user", "content": "x"}], tier="quality")
    assert resp.model == g
    assert post.calls == [q, f, g, g]  # 404 выключен, 400 не временный — во втором проходе только gemma
    assert not providers.is_enabled(f"gemini:{q}")


def test_no_retry_without_transient_failures(free_chain):
    from utils import llm_client
    quota = {"error": {"code": 429, "message": "Quota exceeded, limit: 0"}}
    with patch.object(llm_client._requests, "post", return_value=_gemini_http(429, quota)) as post:
        with pytest.raises(llm_client.AllProvidersFailed, match="проходов: 1"):
            llm_client.chat([{"role": "user", "content": "x"}])
    assert free_chain == [] and post.call_count == 2


def test_minute_429_is_transient_daily_429_is_dead():
    from utils import llm_client
    minute = llm_client._gemini_error(_gemini_http(429, {"error": {"message": "Resource exhausted"}}))
    daily = llm_client._gemini_error(_gemini_http(429, {"error": {
        "message": "Quota exceeded", "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]}}))
    assert minute.kind == "transient" and daily.kind == "dead"
    assert llm_client._gemini_error(_gemini_http(403, {"error": {"message": "denied"}})).kind == "dead"


@pytest.mark.parametrize("make_exc, kind", [
    (lambda r: r.ConnectTimeout("connect timed out"), "transient"),
    (lambda r: r.ConnectionError("HTTPSConnectionPool: NewConnectionError: Failed to establish"), "transient"),
    (lambda r: r.ReadTimeout("read timed out"), "skip"),
    (lambda r: r.ConnectionError("('Connection aborted.', ConnectionResetError(10054))"), "skip"),
    (lambda r: RuntimeError("HTTP 524: таймаут шлюза KIE/Cloudflare"), "skip"),
    (lambda r: RuntimeError("HTTP 502: таймаут шлюза KIE/Cloudflare"), "transient"),
    (lambda r: RuntimeError("chat gpt-6-astra: 500 internal error, try again"), "transient"),
    (lambda r: RuntimeError("chat gpt-6-astra: 402 credits insufficient"), "dead"),
    (lambda r: RuntimeError("KIE_API_KEY не задан в .env"), "dead"),
    (lambda r: RuntimeError("chat gpt-6-astra: 422 invalid input"), "skip"),
])
def test_paid_error_retries_only_before_generation(make_exc, kind):
    import requests
    from utils import llm_client
    assert llm_client._paid_error(make_exc(requests)).kind == kind


def test_kie_critic_retried_only_when_request_never_reached_kie(free_chain, monkeypatch):
    """Критик: Astra не достучалась (ConnectTimeout) — повторяем; Fable получил 524 — мог списать, не повторяем."""
    import requests
    from utils import llm_client
    monkeypatch.setenv("BUDGET_ALLOW", "kie_series")
    astra, fable = llm_client.kie_critic_models()
    kie_calls: list[str] = []

    def kie_chat(spec, messages, max_tokens, json_mode):
        kie_calls.append(spec)
        if spec == astra and kie_calls.count(astra) == 1:
            raise requests.ConnectTimeout("connect timeout")
        if spec == fable:
            raise RuntimeError("HTTP 524: таймаут шлюза KIE/Cloudflare")
        return '{"score": 8}', 4.0

    busy = _gemini_http(503, {"error": {"message": "busy"}})
    with patch("utils.kie_client.chat", side_effect=kie_chat), \
         patch.object(llm_client._requests, "post", return_value=busy):
        resp = llm_client.chat([{"role": "user", "content": "x"}], tier="critic", json_mode=True)
    assert resp.provider == "kie" and resp.model == astra
    assert kie_calls == [astra, fable, astra]
    assert free_chain == [20.0]


def test_kie_read_timeout_is_not_repeated(free_chain, monkeypatch):
    import requests
    from utils import llm_client
    monkeypatch.setenv("BUDGET_ALLOW", "kie_series")
    busy = _gemini_http(503, {"error": {"message": "busy"}})
    with patch("utils.kie_client.chat", side_effect=requests.ReadTimeout("read timed out")) as kie_chat, \
         patch.object(llm_client._requests, "post", return_value=busy):
        with pytest.raises(llm_client.AllProvidersFailed):
            llm_client.chat([{"role": "user", "content": "x"}], tier="premium")
    assert kie_chat.call_count == 2  # Argon и Astra по разу — повторные проходы только по бесплатным
    assert free_chain == [20.0, 60.0]


def test_openai_compat_classifies_transient_and_daily_limits(monkeypatch):
    from utils import llm_client

    class StatusError(Exception):
        def __init__(self, msg, status_code):
            super().__init__(msg)
            self.status_code = status_code

    def kind_of(exc, name="groq"):
        client = MagicMock()
        client.chat.completions.create.side_effect = exc
        with patch.object(llm_client, "_openai_client", return_value=client):
            with pytest.raises(llm_client._ProviderError) as info:
                llm_client.openai_compat_call(name, [{"role": "user", "content": "x"}])
        return info.value.kind

    monkeypatch.setenv("CEREBRAS_API_KEY", "c-key")
    monkeypatch.setattr(llm_client, "_cerebras_throttle", lambda: None)
    assert kind_of(StatusError("Service unavailable", 503)) == "transient"
    assert kind_of(StatusError("Rate limit reached: requests per minute", 429)) == "transient"
    assert kind_of(StatusError("Rate limit reached: tokens per day (TPD)", 429)) == "dead"
    assert kind_of(type("APITimeoutError", (Exception,), {})("timed out")) == "transient"
    assert kind_of(type("APITimeoutError", (Exception,), {})("timed out"), "cerebras") == "skip"


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
