"""
LLM-клиент: единая цепочка провайдеров с автоматическим fallback.

Состояние провайдеров на 2026-10 (проверено живыми вызовами):
  - Gemini API — бесплатный tier работает. Пиновые 2.x-модели отдают 404
    «no longer available to new users», поэтому используем алиасы *-latest.
    flash-lite ≈ 500 RPD, flash ≈ 20 RPD (поэтому flash — только для tier="quality").
  - Groq — llama-3.3-70b-versatile снята 16.08.2026; замена openai/gpt-oss-120b
    (≈1K RPD бесплатно), мультимодальная qwen/qwen3.8-27b.
  - Cerebras — бесплатного tier больше нет (402) → платный провайдер.
  - Gemma 4 через Gemini API — бесплатно, но медленно (до 30-40 с) → последний резерв.
  - NVIDIA NIM / OpenRouter :free — подключаются, если заданы ключи.

Порядок цепочки:
  tier="quality": Yandex* → Gemini quality → Groq → Gemini fast → NVIDIA → OpenRouter → Gemma → Cerebras*
  tier="fast":    Yandex* → Gemini fast → Groq → NVIDIA → OpenRouter → Gemma → Cerebras*
  (* — платные, пропускаются в BUDGET_MODE=free)

Каждый провайдер/модель при исчерпании дневной квоты, неверном ключе или 404
выключается до конца процесса (utils.providers.mark_dead) — без спама ретраями.

API:
    chat(messages, tier="fast", temperature, max_tokens, json_mode, caller) → response
        response.choices[0].message.content, response.provider, response.model
    groq_with_cerebras_fallback(...) — совместимый со старым кодом враппер над chat()
"""

import base64
import json
import logging
import os
import re
import threading
import time
from types import SimpleNamespace

import requests as _requests

logger = logging.getLogger(__name__)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


# Модели по умолчанию (переопределяются через .env)
def gemini_fast_model() -> str:
    return _env("GEMINI_MODEL", "gemini-flash-lite-latest")


def gemini_quality_model() -> str:
    return _env("GEMINI_QUALITY_MODEL", "gemini-flash-latest")


def gemma_model() -> str:
    return _env("GEMMA_MODEL", "gemma-4-26b-a4b-it")


def groq_model() -> str:
    return _env("GROQ_MODEL", "openai/gpt-oss-120b")


def groq_vision_model() -> str:
    return _env("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")


_REASONING_HEADROOM = 1024  # gpt-oss тратит часть max_tokens на рассуждение


class AllProvidersFailed(RuntimeError):
    pass


class _ProviderError(Exception):
    """kind: dead (выключить до конца процесса) | skip (перейти к следующему)."""

    def __init__(self, message: str, kind: str = "skip"):
        super().__init__(message)
        self.kind = kind


def _wrap(text: str, provider: str, model: str) -> SimpleNamespace:
    msg = SimpleNamespace(content=text, role="assistant")
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], provider=provider, model=model)


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


_THOUGHT_RE = re.compile(r"<thought>.*?</thought>", re.S)


def _text_of(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type", "text") == "text")
    return str(content or "")


# ── Yandex AI Studio (native REST API, платный) ──────────────────────────────

_YANDEX_COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

_YANDEX_MODELS = {
    # alias → modelUri suffix (gpt://{folder_id}/{model}/latest)
    "yandexgpt-lite": "yandexgpt-lite",
    "yandexgpt":      "yandexgpt",
    "yandexgpt-32k":  "yandexgpt-32k",
    # deepseek-v3 в Yandex AI Studio нет → лучшая доступная
    "deepseek-v3":    "yandexgpt",
}


def yandex_call(
    messages: list[dict],
    model: str = "yandexgpt-lite",
    temperature: float = 0.3,
    max_tokens: int = 512,
    response_format: dict | None = None,
    caller: str = "",
) -> object:
    """Yandex AI Studio. Возвращает OpenAI-совместимый объект (.choices[0].message.content)."""
    api_key = os.environ.get("YANDEX_API_KEY", "")
    folder_id = os.environ.get("YANDEX_FOLDER_ID", "")
    if not api_key:
        raise EnvironmentError("YANDEX_API_KEY не задан в .env")
    if not folder_id:
        raise EnvironmentError("YANDEX_FOLDER_ID не задан в .env")

    model_name = _YANDEX_MODELS.get(model, model)
    need_json = bool(response_format and response_format.get("type") == "json_object")
    json_hint = " Отвечай строго валидным JSON без markdown-блоков и пояснений."

    yandex_messages: list[dict] = []
    for m in messages:
        content = _text_of(m.get("content", ""))
        if not content:
            continue
        if need_json and m.get("role") == "system":
            content += json_hint
        yandex_messages.append({"role": m["role"], "text": content})
    if need_json and not any(m["role"] == "system" for m in yandex_messages):
        yandex_messages.insert(0, {"role": "system", "text": json_hint.strip()})

    resp = _requests.post(
        _YANDEX_COMPLETION_URL,
        headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
        json={
            "modelUri": f"gpt://{folder_id}/{model_name}/latest",
            "completionOptions": {"stream": False, "temperature": temperature, "maxTokens": str(max_tokens)},
            "messages": yandex_messages,
        },
        timeout=60,
    )
    if resp.status_code == 429:
        raise Exception("429 rate_limit: Yandex RPM exceeded")
    resp.raise_for_status()
    text = resp.json()["result"]["alternatives"][0]["message"]["text"]
    if need_json:
        text = _strip_code_fence(text)
    return _wrap(text, "yandex", model_name)


# ── Gemini / Gemma (native REST, бесплатный tier) ────────────────────────────

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _gemini_parts(content) -> list[dict]:
    if not isinstance(content, list):
        return [{"text": str(content or "")}]
    parts = []
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type", "text") == "text":
            parts.append({"text": p.get("text", "")})
        elif p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            m = re.match(r"data:([^;]+);base64,(.*)", url, re.S)
            if m:
                parts.append({"inline_data": {"mime_type": m.group(1), "data": m.group(2)}})
    return parts


def _gemini_error(resp: _requests.Response) -> _ProviderError:
    try:
        err = resp.json().get("error", {})
    except ValueError:
        err = {}
    message = err.get("message", resp.text[:200])
    details = json.dumps(err.get("details", []))
    status = resp.status_code
    if status in (401, 403) or status == 404 or "no longer available" in message:
        return _ProviderError(f"HTTP {status}: {message[:200]}", "dead")
    if status == 429:
        # Дневная квота — выключаем модель; минутная — просто идём дальше по цепочке
        per_day = "PerDay" in details or "per day" in message.lower() or "limit: 0" in message
        return _ProviderError(f"HTTP 429: {message[:200]}", "dead" if per_day else "skip")
    return _ProviderError(f"HTTP {status}: {message[:200]}", "skip")


def gemini_call(
    messages: list[dict],
    model: str,
    temperature: float = 0.3,
    max_tokens: int = 512,
    json_mode: bool = False,
    thinking_level: str | None = None,
    timeout: float = 90,
) -> SimpleNamespace:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise _ProviderError("GEMINI_API_KEY не задан", "dead")

    is_gemma = model.startswith("gemma")
    system_text = "\n\n".join(_text_of(m["content"]) for m in messages if m.get("role") == "system")
    contents = []
    for m in messages:
        if m.get("role") == "system":
            continue
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": _gemini_parts(m.get("content", ""))})

    gen_cfg: dict = {
        "temperature": temperature,
        # thinking-модели тратят выходные токены на рассуждение — даём запас
        "maxOutputTokens": max_tokens + 4096,
    }
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"
    if thinking_level and not is_gemma:
        gen_cfg["thinkingConfig"] = {"thinkingLevel": thinking_level}

    body: dict = {"contents": contents, "generationConfig": gen_cfg}
    if system_text:
        body["systemInstruction"] = {"parts": [{"text": system_text}]}

    for attempt in range(2):
        try:
            resp = _requests.post(_GEMINI_URL.format(model=model), headers={"X-Goog-Api-Key": api_key},
                                  json=body, timeout=timeout)
        except _requests.RequestException as exc:
            raise _ProviderError(f"{type(exc).__name__}", "skip") from exc
        if resp.status_code == 400 and "thinking" in resp.text.lower() and "thinkingConfig" in gen_cfg:
            gen_cfg.pop("thinkingConfig")  # модель за алиасом не поддерживает thinkingLevel
            continue
        break
    if resp.status_code != 200:
        raise _gemini_error(resp)

    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise _ProviderError(f"Gemini: пустой ответ ({reason})", "skip")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    text = _THOUGHT_RE.sub("", text).strip()
    if not text:
        raise _ProviderError(f"Gemini: нет текста (finishReason={candidates[0].get('finishReason')})", "skip")
    if json_mode:
        text = _strip_code_fence(text)
    return _wrap(text, "gemma" if is_gemma else "gemini", model)


# ── OpenAI-совместимые провайдеры: Groq, NVIDIA NIM, OpenRouter, Cerebras ────

_OPENAI_COMPAT = {
    # name: (base_url, key_env, model_fn)
    "groq":       ("https://api.groq.com/openai/v1", "GROQ_API_KEY", groq_model),
    "nvidia":     ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
                   lambda: _env("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b")),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                   lambda: _env("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")),
    "cerebras":   ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY",
                   lambda: _env("CEREBRAS_MODEL", "gpt-oss-120b")),
}

_clients: dict[str, object] = {}
_clients_lock = threading.Lock()


def _openai_client(name: str):
    from openai import OpenAI
    base_url, key_env, _ = _OPENAI_COMPAT[name]
    with _clients_lock:
        if name not in _clients:
            _clients[name] = OpenAI(base_url=base_url, api_key=os.environ.get(key_env, ""), max_retries=0)
        return _clients[name]


def openai_compat_call(
    name: str,
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 512,
    json_mode: bool = False,
) -> SimpleNamespace:
    _, key_env, model_fn = _OPENAI_COMPAT[name]
    if not os.environ.get(key_env):
        raise _ProviderError(f"{key_env} не задан", "dead")
    model = model or model_fn()
    kwargs: dict = {"model": model, "messages": messages, "temperature": temperature,
                    "max_tokens": max_tokens}
    if "gpt-oss" in model:
        kwargs["max_tokens"] = max_tokens + _REASONING_HEADROOM
        if name in ("groq", "cerebras"):
            kwargs["reasoning_effort"] = "low"
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if name == "cerebras":
        _cerebras_throttle()
    try:
        resp = _openai_client(name).chat.completions.create(**kwargs)
    except Exception as exc:
        text = str(exc)
        status = getattr(exc, "status_code", None)
        from utils.providers import looks_like_no_credits
        if status in (401, 403, 404) or looks_like_no_credits(text) or "invalid api key" in text.lower() \
                or "decommissioned" in text.lower() or "model_not_found" in text.lower():
            raise _ProviderError(text[:200], "dead") from exc
        raise _ProviderError(text[:200], "skip") from exc
    content = _THOUGHT_RE.sub("", resp.choices[0].message.content or "").strip()
    if not content:
        raise _ProviderError(f"{name}: пустой ответ", "skip")
    if json_mode:
        content = _strip_code_fence(content)
    return _wrap(content, name, model)


_cerebras_last_call = 0.0
_cerebras_lock = threading.Lock()
_CEREBRAS_MIN_INTERVAL = 3.0


def _cerebras_throttle() -> None:
    global _cerebras_last_call
    with _cerebras_lock:
        wait = _CEREBRAS_MIN_INTERVAL - (time.monotonic() - _cerebras_last_call)
        if wait > 0:
            time.sleep(wait)
        _cerebras_last_call = time.monotonic()


# ── Цепочка ──────────────────────────────────────────────────────────────────

def _chain(tier: str, needs_vision: bool) -> list[tuple[str, str, object]]:
    """[(provider_gate_name, label, callable(messages, temperature, max_tokens, json_mode))]."""
    fast, quality, gemma = gemini_fast_model(), gemini_quality_model(), gemma_model()

    def gem(model, level=None):
        return lambda m, t, mt, j: gemini_call(m, model, t, mt, j, thinking_level=level)

    def compat(name, model=None):
        return lambda m, t, mt, j: openai_compat_call(name, m, model, t, mt, j)

    groq = ("groq", "groq", compat("groq", groq_vision_model() if needs_vision else None))
    chain: list[tuple[str, str, object]] = []
    if not needs_vision:
        chain.append(("yandex_llm", "yandex", None))  # обрабатывается отдельно (свои модели)
    if tier == "quality":
        chain += [
            (f"gemini:{quality}", quality, gem(quality)),
            groq,
            (f"gemini:{fast}", fast, gem(fast, "low")),
        ]
    else:
        chain += [
            (f"gemini:{fast}", fast, gem(fast, "low")),
            groq,
        ]
    if not needs_vision:
        chain += [
            ("nvidia", "nvidia", compat("nvidia")),
            ("openrouter", "openrouter", compat("openrouter")),
        ]
    chain.append((f"gemini:{gemma}", gemma, gem(gemma)))
    if not needs_vision:
        chain.append(("cerebras", "cerebras", compat("cerebras")))
    return chain


def chat(
    messages: list[dict],
    tier: str = "fast",
    temperature: float = 0.3,
    max_tokens: int = 512,
    json_mode: bool = False,
    caller: str = "",
    yandex_model: str = "yandexgpt-lite",
) -> SimpleNamespace:
    """
    Отправляет messages (формат OpenAI, content может содержать image_url data-URI)
    по цепочке провайдеров. Возвращает первый успешный ответ, иначе AllProvidersFailed.
    """
    from utils.providers import is_enabled, mark_dead

    tag = f" ({caller})" if caller else ""
    needs_vision = any(
        isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages
    )
    errors: list[str] = []
    for gate, label, fn in _chain(tier, needs_vision):
        if not is_enabled(gate) or not is_enabled(gate.split(":")[0]):
            continue
        try:
            if gate == "yandex_llm":
                if not (os.environ.get("YANDEX_API_KEY") and os.environ.get("YANDEX_FOLDER_ID")):
                    continue
                resp = yandex_call(messages, model=yandex_model, temperature=temperature,
                                   max_tokens=max_tokens,
                                   response_format={"type": "json_object"} if json_mode else None,
                                   caller=caller)
            else:
                resp = fn(messages, temperature, max_tokens, json_mode)
        except _ProviderError as exc:
            errors.append(f"{label}: {exc}")
            if exc.kind == "dead":
                mark_dead(gate, str(exc)[:200])
            logger.warning("LLM%s: %s недоступен (%s) — следующий провайдер", tag, label, str(exc)[:160])
            continue
        except Exception as exc:  # Yandex и неожиданные ошибки
            from utils.providers import looks_like_no_credits
            errors.append(f"{label}: {exc}")
            if looks_like_no_credits(exc) or "401" in str(exc) or "403" in str(exc):
                mark_dead(gate, str(exc)[:200])
            logger.warning("LLM%s: %s ошибка (%s) — следующий провайдер", tag, label, str(exc)[:160])
            continue
        _track(caller, resp)
        if errors:
            logger.info("LLM%s: ответил %s/%s после %d отказов", tag, resp.provider, resp.model, len(errors))
        return resp
    raise AllProvidersFailed(f"LLM{tag}: все провайдеры недоступны: " + " | ".join(errors[-6:]))


def _track(caller: str, resp: SimpleNamespace) -> None:
    try:
        from utils.cost_tracker import log_cost
        provider = "yandex_ai" if resp.provider == "yandex" else resp.provider
        log_cost(caller or "llm", provider, "request", 1)
    except Exception:
        pass


def groq_with_cerebras_fallback(
    messages: list[dict],
    groq_model: str | None = None,
    cb_model: str | None = None,
    yandex_model: str = "yandexgpt-lite",
    temperature: float = 0.3,
    max_tokens: int = 512,
    response_format: dict | None = None,
    caller: str = "",
    tier: str | None = None,
) -> SimpleNamespace:
    """
    Совместимый враппер для старых вызовов. groq_model/cb_model игнорируются
    (модели задаются в .env). tier по умолчанию: quality для deepseek-v3/yandexgpt.
    """
    if tier is None:
        tier = "quality" if yandex_model in ("deepseek-v3", "yandexgpt", "yandexgpt-32k") else "fast"
    return chat(
        messages,
        tier=tier,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=bool(response_format and response_format.get("type") == "json_object"),
        caller=caller,
        yandex_model=yandex_model,
    )


def cerebras_call(
    messages: list[dict],
    model: str = "gpt-oss-120b",
    temperature: float = 0.3,
    max_tokens: int = 512,
    response_format: dict | None = None,
    caller: str = "",
    **_ignored,
) -> SimpleNamespace:
    """Прямой вызов Cerebras (платный с 2026). Оставлен для совместимости."""
    try:
        return openai_compat_call("cerebras", messages, model, temperature, max_tokens,
                                  bool(response_format and response_format.get("type") == "json_object"))
    except _ProviderError as exc:
        raise RuntimeError(str(exc)) from exc


def image_to_data_url(path: str, mime: str = "image/jpeg") -> str:
    with open(path, "rb") as f:
        return f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"
