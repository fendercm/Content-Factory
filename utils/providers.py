"""
Provider gate — единая точка решения «можно ли звать этого провайдера».

Два механизма:
  1. BUDGET_MODE=free в .env — статически выключает все платные провайдеры.
     Пайплайн работает на 100% бесплатном стеке:
     Groq/Cerebras (LLM) + edge-tts (голос) + Pexels (видео/фото) +
     Whisper local (субтитры) + FFmpeg (монтаж) + Pillow (thumbnail).
  2. mark_dead(provider) — runtime circuit breaker: провайдер, ответивший
     402 (нет кредитов) / quota=0 / 401, выключается до конца процесса,
     чтобы не спамить ретраями каждый шаг пайплайна.

Дополнительно DISABLED_PROVIDERS=kie,gemini в .env выключает провайдеров поимённо.

API:
    is_enabled("kie") → bool
    mark_dead("kie", "402 credits insufficient")
    budget_mode() → bool
"""

import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

# Провайдеры, которые стоят денег — выключаются в BUDGET_MODE=free
PAID_PROVIDERS: frozenset[str] = frozenset({
    "kie",          # kie.ai: Kling video, ElevenLabs TTS, Suno music
    "elevenlabs",   # прямой ElevenLabs API
    "yandex_tts",   # Yandex SpeechKit
    "yandex_llm",   # Yandex AI Studio (YandexGPT)
    "yandexart",    # YandexART thumbnail
    "dashscope",    # CosyVoice (бесплатный tier есть, но ключа нет)
})

_dead: dict[str, str] = {}
_dead_lock = threading.Lock()


def budget_mode() -> bool:
    """True — работаем только на бесплатных провайдерах."""
    return os.getenv("BUDGET_MODE", "").strip().lower() in ("free", "1", "true", "yes")


def _disabled_by_env() -> set[str]:
    raw = os.getenv("DISABLED_PROVIDERS", "")
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def is_enabled(provider: str) -> bool:
    """Можно ли звать провайдера прямо сейчас."""
    name = provider.strip().lower()
    if budget_mode() and name in PAID_PROVIDERS:
        return False
    if name in _disabled_by_env():
        return False
    with _dead_lock:
        return name not in _dead


def mark_dead(provider: str, reason: str = "") -> None:
    """Выключает провайдера до конца процесса (нет кредитов / квота = 0)."""
    name = provider.strip().lower()
    with _dead_lock:
        if name not in _dead:
            _dead[name] = reason
            logger.warning("Provider '%s' выключен до конца процесса: %s", name, reason)


def dead_reason(provider: str) -> str | None:
    with _dead_lock:
        return _dead.get(provider.strip().lower())


# HTTP 402 как статус, а не любые «402» внутри id/таймингов/URL
_HTTP_402_RE = re.compile(
    r"(\b402 (client error|payment)|\bstatus(_code)?[\s:=]+402\b|['\"]code['\"]\s*:\s*402\b|\bhttp[ /]402\b|^402\b)"
)


def looks_like_no_credits(error_text: str) -> bool:
    """Эвристика: ошибка означает «нет денег/квоты», а не временный сбой."""
    t = str(error_text).lower()
    if any(marker in t for marker in (
        "credits insufficient",
        "insufficient credits",
        "insufficient balance",
        "insufficient_quota",
        "payment required",
        "limit: 0",
        "quota exceeded for metric",
        "billing account",
        "billing is not enabled",
        "billing not enabled",
    )):
        return True
    return bool(_HTTP_402_RE.search(t))
