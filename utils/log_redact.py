"""
Маскирование секретов в логах и текстах ошибок.

Исключения requests содержат полный URL запроса: у Telegram Bot API в URL
лежит токен бота, у Google API — ?key=. Без маскировки они попадают в logs/,
в pipeline.db (error_log / pipeline_state) и в Telegram-алерты.

API:
    redact(text) → str            — маскирует секреты в строке
    install_log_redaction()       — вешает фильтр на все handlers root-логгера
"""

import logging
import os
import re

# Значения переменных окружения с такими суффиксами считаются секретами
_SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
_MIN_SECRET_LEN = 8

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Telegram: https://api.telegram.org/bot<id>:<secret>/sendMessage
    (re.compile(r"bot\d{6,12}:[A-Za-z0-9_-]{30,}"), "bot***"),
    (re.compile(r"\b\d{6,12}:AA[A-Za-z0-9_-]{30,}"), "***"),
    # Ключи в query string: ?key=..., &api_key=..., &token=...
    (re.compile(r"([?&](?:key|api_key|apikey|access_key|token)=)[^&\s'\"]+", re.I), r"\1***"),
    # Заголовки авторизации, попавшие в текст
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}", re.I), r"\1***"),
    (re.compile(r"(Api-Key\s+)[A-Za-z0-9._~+/=-]{12,}", re.I), r"\1***"),
    # Google API key
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "AIza***"),
]


def _env_secrets() -> list[str]:
    values = {
        v for k, v in os.environ.items()
        if k.upper().endswith(_SECRET_ENV_SUFFIXES) and v and len(v) >= _MIN_SECRET_LEN
    }
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
        return True


def install_log_redaction() -> None:
    """Добавляет RedactingFilter ко всем handlers root-логгера (идемпотентно)."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
