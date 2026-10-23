"""
Маскирование секретов в логах и текстах ошибок.

Исключения requests содержат полный URL запроса: у Telegram Bot API в URL
лежит токен бота, у Google API — ?key=, у Unsplash — ?client_id=. Без
маскировки они попадают в logs/, в pipeline.db (error_log / pipeline_state),
в Telegram-алерты и в ответы админ-панели.

Три слоя защиты:
  1. значения секретных переменных окружения и .env (точное совпадение);
  2. известные форматы ключей провайдеров (sk-…, gsk_…, AIza…, vk1.a.… и т.д.);
  3. ключи в URL (?key=), заголовках (Authorization: …, xi-api-key: …)
     и в dict/JSON ({'api_key': '…'}).

API:
    redact(text) → str            — маскирует секреты в строке
    install_log_redaction()       — вешает фильтр на все handlers root-логгера
"""

import logging
import os
import re
from functools import lru_cache
from pathlib import Path

# Значения переменных окружения с такими суффиксами считаются секретами
_SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
_MIN_SECRET_LEN = 8
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Имена полей dict/JSON и заголовков, значение которых — секрет
_FIELD_NAMES = (
    r"api[_-]?key|apikey|access[_-]?key|access[_-]?token|auth[_-]?token|"
    r"bot[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|client[_-]?secret|"
    r"password|passwd|xi-api-key|x-api-key|x-goog-api-key|x-subscription-token|authorization"
)
# В query string ещё ?key= (Google) и ?client_id= (Unsplash: это и есть ключ).
# В dict их не маскируем: {'key': 'voice'} — обычный ключ этапа, не секрет.
_QUERY_NAMES = _FIELD_NAMES + r"|key|client[_-]?id|sig|signature"

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Telegram: https://api.telegram.org/bot<id>:<secret>/sendMessage
    (re.compile(r"bot\d{6,12}:[A-Za-z0-9_-]{30,}"), "bot***"),
    (re.compile(r"\b\d{6,12}:AA[A-Za-z0-9_-]{30,}"), "***"),
    # Ключи в query string: ?key=..., &api_key=..., &client_id=... (Unsplash)
    (re.compile(rf"([?&](?:{_QUERY_NAMES})=)[^&\s'\"<>]+", re.I), r"\1***"),
    # Заголовки авторизации, попавшие в текст: «Bearer x», «Api-Key x», «OAuth x»
    (re.compile(r"\b((?:Bearer|Api-Key|OAuth)\s+)[A-Za-z0-9._~+/=-]{12,}", re.I), r"\1***"),
    # dict/JSON: {'api_key': '...'}, "xi-api-key": "...", 'Authorization': '...'
    (re.compile(rf"""(["'](?:{_FIELD_NAMES})["']\s*:\s*["'])(?!\*\*\*)[^"'\s]{{8,}}(["'])""", re.I),
     r"\1***\2"),
    # Заголовок в сыром виде: «xi-api-key: abc», «Authorization: abc»
    (re.compile(r"^(\s*(?:authorization|xi-api-key|x-api-key|x-goog-api-key|x-subscription-token)\s*:\s*)"
                r"(?!(?:Bearer|Api-Key|OAuth)\s)\S{8,}", re.I | re.M), r"\1***"),
    # Google API key
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "AIza***"),
    # OpenRouter / DashScope / OpenAI-подобные: sk-or-v1-…, sk-…; ElevenLabs: sk_…
    (re.compile(r"\bsk-(?:or-v1-|proj-)?[A-Za-z0-9_-]{20,}"), "sk-***"),
    (re.compile(r"\bsk_[A-Za-z0-9]{20,}"), "sk_***"),
    # Groq, NVIDIA, Cerebras, Tavily
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "gsk_***"),
    (re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}"), "nvapi-***"),
    (re.compile(r"\bcsk-[A-Za-z0-9]{20,}"), "csk-***"),
    (re.compile(r"\btvly-[A-Za-z0-9_-]{16,}"), "tvly-***"),
    # VK user token
    (re.compile(r"\bvk1\.a\.[A-Za-z0-9_-]{20,}"), "vk1.a.***"),
    # Yandex Cloud: API-ключ AQVN…, IAM-токен t1.…, OAuth y0_…
    (re.compile(r"\bAQVN[A-Za-z0-9_-]{20,}"), "AQVN***"),
    (re.compile(r"\bt1\.[A-Za-z0-9_.-]{40,}"), "t1.***"),
    (re.compile(r"\by0_[A-Za-z0-9_-]{30,}"), "y0_***"),
]


@lru_cache(maxsize=1)
def _dotenv_secrets() -> frozenset[str]:
    """Секреты из .env проекта — на случай, если load_dotenv ещё не вызывали.

    Файл только читается; os.environ не меняется.
    """
    try:
        from dotenv import dotenv_values
        values = dotenv_values(_ENV_FILE) if _ENV_FILE.is_file() else {}
    except Exception:
        return frozenset()
    return frozenset(
        v for k, v in values.items()
        if k.upper().endswith(_SECRET_ENV_SUFFIXES) and v and len(v) >= _MIN_SECRET_LEN
    )


def _env_secrets() -> list[str]:
    values = {
        v for k, v in os.environ.items()
        if k.upper().endswith(_SECRET_ENV_SUFFIXES) and v and len(v) >= _MIN_SECRET_LEN
    }
    values |= _dotenv_secrets()
    # Длинные первыми — чтобы не оставить хвост от частичной замены
    return sorted(values, key=len, reverse=True)


def redact(text: object) -> str:
    """Возвращает строку, в которой секреты заменены на ***."""
    s = str(text)
    if not s:
        return s
    for secret in _env_secrets():
        if secret in s:
            s = s.replace(secret, "***")
    for pattern, repl in _PATTERNS:
        s = pattern.sub(repl, s)
    return s


class RedactingFilter(logging.Filter):
    """logging.Filter, который маскирует секреты в сообщении и traceback."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install_log_redaction() -> None:
    """Добавляет RedactingFilter ко всем handlers root-логгера (идемпотентно)."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
