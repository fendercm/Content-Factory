"""
Shared LLM client helpers: Yandex AI Studio → Groq → Cerebras fallback chain.
"""
import logging
import os
import time
import threading
from types import SimpleNamespace

import requests as _requests
from groq import Groq
from openai import OpenAI

logger = logging.getLogger(__name__)

# ── Yandex AI Studio (native REST API) ───────────────────────────────────────

_YANDEX_COMPLETION_URL = (
    "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
)

_YANDEX_MODELS = {
    # alias → modelUri suffix (used with gpt://{folder_id}/{model}/latest)
    "yandexgpt-lite": "yandexgpt-lite",
    "yandexgpt":      "yandexgpt",
    "yandexgpt-32k":  "yandexgpt-32k",
    # deepseek-v3 is not available on Yandex → map to best available model
    "deepseek-v3":    "yandexgpt",
}


def _wrap_yandex_response(text: str) -> object:
    """Wraps Yandex response text into an OpenAI-like response object."""
    msg = SimpleNamespace(content=text, role="assistant")
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def yandex_call(
    messages: list[dict],
    model: str = "yandexgpt-lite",
    temperature: float = 0.3,
    max_tokens: int = 512,
    response_format: dict | None = None,
    caller: str = "",
) -> object:
    """
    Call Yandex AI Studio via native REST API.
    Returns an OpenAI-compatible response object (.choices[0].message.content).
    """
    api_key = os.environ.get("YANDEX_API_KEY", "")
    folder_id = os.environ.get("YANDEX_FOLDER_ID", "")
    if not api_key:
        raise EnvironmentError("YANDEX_API_KEY не задан в .env")
    if not folder_id:
        raise EnvironmentError("YANDEX_FOLDER_ID не задан в .env")

    model_name = _YANDEX_MODELS.get(model, model)
    model_uri = f"gpt://{folder_id}/{model_name}/latest"
    need_json = bool(response_format and response_format.get("type") == "json_object")

    # Конвертируем messages: OpenAI format → Yandex format
    yandex_messages: list[dict] = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            continue
        # Если JSON нужен — добавляем инструкцию к system-сообщению (или создаём его)
        if need_json and m.get("role") == "system":
            content = content + " Отвечай строго валидным JSON без markdown-блоков и пояснений."
        yandex_messages.append({"role": m["role"], "text": content})

    # Нет system-сообщения, а JSON нужен — добавляем в начало
    if need_json and not any(m["role"] == "system" for m in yandex_messages):
        yandex_messages.insert(0, {
            "role": "system",
            "text": "Отвечай строго валидным JSON без markdown-блоков и пояснений.",
        })

    body = {
        "modelUri": model_uri,
        "completionOptions": {
            "stream": False,
            "temperature": temperature,
            "maxTokens": str(max_tokens),
        },
        "messages": yandex_messages,
    }

    resp = _requests.post(
        _YANDEX_COMPLETION_URL,
        headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    if resp.status_code == 429:
        raise Exception("429 rate_limit: Yandex RPM exceeded")
    resp.raise_for_status()
    text = resp.json()["result"]["alternatives"][0]["message"]["text"]

    # Снимаем markdown-обёртку если модель всё равно добавила ```json ... ```
    if need_json:
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            # убираем первую строку (```json или ```) и последнюю (```)
            inner = lines[1:] if lines[0].startswith("```") else lines
            if inner and inner[-1].strip() == "```":
                inner = inner[:-1]
            text = "\n".join(inner).strip()

    return _wrap_yandex_response(text)


# ── Groq (cached client) ──────────────────────────────────────────────────────

_groq_client: "Groq | None" = None
_groq_lock = threading.Lock()


def _get_groq() -> "Groq":
    global _groq_client
    if _groq_client is None:
        with _groq_lock:
            if _groq_client is None:
                _groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""), max_retries=0)
    return _groq_client


# ── Cerebras ──────────────────────────────────────────────────────────────────

_cerebras_client: OpenAI | None = None
_cerebras_last_call: float = 0.0
_cerebras_lock = threading.Lock()
_CEREBRAS_MIN_INTERVAL = 3.0   # секунды между вызовами — защита от RPM (~20 RPM max)


def _get_cerebras() -> OpenAI:
    global _cerebras_client
    if _cerebras_client is None:
        _cerebras_client = OpenAI(
            base_url="https://api.cerebras.ai/v1",
            api_key=os.environ.get("CEREBRAS_API_KEY", ""),
            max_retries=0,
        )
    return _cerebras_client


def _cerebras_throttle() -> None:
    """Enforces minimum interval between Cerebras calls within the same process."""
    global _cerebras_last_call
    with _cerebras_lock:
        now = time.monotonic()
        wait = _CEREBRAS_MIN_INTERVAL - (now - _cerebras_last_call)
        if wait > 0:
            time.sleep(wait)
        _cerebras_last_call = time.monotonic()


def cerebras_call(
    messages: list[dict],
    model: str = "gpt-oss-120b",
    temperature: float = 0.3,
    max_tokens: int = 512,
    response_format: dict | None = None,
    rpm_retries: int = 3,
    rpm_sleep: float = 8.0,
    caller: str = "",
) -> object:
    """Call Cerebras with RPM-429 retry."""
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        kwargs["response_format"] = response_format

    for attempt in range(rpm_retries):
        _cerebras_throttle()
        try:
            return _get_cerebras().chat.completions.create(**kwargs)
        except Exception as exc:
            if "429" not in str(exc) and "rate_limit" not in str(exc).lower():
                raise
            if attempt < rpm_retries - 1:
                tag = f" ({caller})" if caller else ""
                logger.warning("Cerebras RPM 429%s — ожидаю %.0f сек (попытка %d/%d)",
                               tag, rpm_sleep, attempt + 1, rpm_retries)
                time.sleep(rpm_sleep)
            else:
                raise


def groq_with_cerebras_fallback(
    messages: list[dict],
    groq_model: str = "llama-3.3-70b-versatile",
    cb_model: str = "gpt-oss-120b",
    yandex_model: str = "yandexgpt-lite",
    temperature: float = 0.3,
    max_tokens: int = 512,
    response_format: dict | None = None,
    caller: str = "",
) -> object:
    """
    Yandex AI Studio → Groq → Cerebras fallback chain.

    Yandex is tried first when YANDEX_API_KEY is set.
    On any Yandex error falls back to Groq.
    On Groq 429 falls back to Cerebras with RPM retry.
    """
    tag = f" ({caller})" if caller else ""

    def _track(provider: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        try:
            from utils.cost_tracker import log_cost
            total = prompt_tokens + completion_tokens
            log_cost(caller or "llm", provider, "tokens", total)
        except Exception:
            pass

    # ── 1. Yandex AI Studio (платный — гейтится providers) ──────────────────
    from utils.providers import is_enabled, mark_dead, looks_like_no_credits

    if (
        is_enabled("yandex_llm")
        and os.environ.get("YANDEX_API_KEY")
        and os.environ.get("YANDEX_FOLDER_ID")
    ):
        try:
            resp = yandex_call(
                messages=messages,
                model=yandex_model,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                caller=caller,
            )
            _track("yandex_ai", sum(len(m.get("content", "")) // 4 for m in messages), max_tokens // 2)
            return resp
        except Exception as exc:
            if looks_like_no_credits(exc) or "401" in str(exc) or "403" in str(exc):
                mark_dead("yandex_llm", str(exc)[:200])
            logger.warning("Yandex%s ошибка: %s — переключаюсь на Groq", tag, exc)

    # ── 2. Groq ───────────────────────────────────────────────────────────────
    groq_client = _get_groq()
    kwargs: dict = {
        "model": groq_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        kwargs["response_format"] = response_format

    try:
        resp = groq_client.chat.completions.create(**kwargs)
        _track("groq", 0, 0)  # Groq free tier — $0
        return resp
    except Exception as exc:
        # Любая ошибка Groq (429, невалидная модель, сеть) → пробуем Cerebras.
        # Обрывать цепочку на не-429 нельзя: Cerebras — последний рабочий резерв.
        logger.warning("Groq%s ошибка: %s — переключаюсь на Cerebras", tag, str(exc)[:200])

    # ── 3. Cerebras ───────────────────────────────────────────────────────────
    return cerebras_call(
        messages=messages,
        model=cb_model,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        caller=caller,
    )
