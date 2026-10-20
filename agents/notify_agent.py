"""
Notify Agent — отправляет уведомления через Telegram Bot API.

API:
  send(message, chat_id?, level?)          — текстовое сообщение
  send_photo(photo_bytes, caption, chat_id?)  — фото с подписью
  alert_error(step, error, attempt)        — алерт об ошибке агента
  alert_success(title, platforms, post_id, duration?, video_path?)
                                           — карточка об успешной публикации
"""

import html
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_API_BASE = "https://api.telegram.org/bot{token}"

_ICONS = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌"}


def _esc(text: str) -> str:
    """HTML-escape для безопасной вставки в Telegram HTML parse_mode."""
    return html.escape(str(text)) if text else ""


def _describe_error(exc: Exception) -> str:
    """
    Описание ошибки без URL: str(HTTPError) содержит адрес запроса,
    а в адресе Telegram Bot API — токен бота.
    """
    resp = getattr(exc, "response", None)
    if resp is not None:
        detail = ""
        try:
            detail = resp.json().get("description", "")
        except Exception:
            pass
        return f"HTTP {resp.status_code}{': ' + detail if detail else ''}"
    return type(exc).__name__


def _extract_frame(video_path: str) -> bytes | None:
    """Извлекает кадр из видео на 2-й секунде через ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not os.path.exists(video_path):
        return None
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        subprocess.run(
            [
                ffmpeg, "-y", "-i", video_path,
                "-ss", "2", "-vframes", "1",
                "-vf", "scale=540:-2", "-strict", "unofficial",
                tmp_path,
            ],
            capture_output=True, check=True, timeout=30,
        )
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            with open(tmp_path, "rb") as f:
                data = f.read()
            return data
    except Exception as exc:
        logger.debug("Ошибка извлечения кадра: %s", exc)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return None


def _token_and_chat(chat_id: str | None) -> tuple[str, str] | tuple[None, None]:
    token = TELEGRAM_BOT_TOKEN
    target = chat_id or TELEGRAM_CHAT_ID
    if not token or not target:
        logger.warning(
            "TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы — уведомление пропущено"
        )
        return None, None
    return token, target


def send(
    message: str,
    chat_id: str | None = None,
    level: str = "info",
    parse_mode: str = "HTML",
) -> dict[str, Any]:
    """Отправляет текстовое сообщение в Telegram."""
    token, target = _token_and_chat(chat_id)
    if not token:
        return {"ok": False, "message_id": None, "error": "credentials missing"}

    icon = _ICONS.get(level, "")
    full_message = f"{icon} {message}".strip() if icon else message

    url = f"{_API_BASE.format(token=token)}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": target, "text": full_message, "parse_mode": parse_mode},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            msg_id = data["result"].get("message_id")
            logger.info("Telegram: отправлено (message_id=%s)", msg_id)
            return {"ok": True, "message_id": msg_id, "error": None}
        err = data.get("description", "unknown")
        logger.warning("Telegram ok=false: %s", err)
        return {"ok": False, "message_id": None, "error": err}
    except requests.RequestException as exc:
        err = _describe_error(exc)
        logger.error("Telegram send ошибка: %s", err)
        return {"ok": False, "message_id": None, "error": err}


def send_photo(
    photo_bytes: bytes,
    caption: str,
    chat_id: str | None = None,
) -> dict[str, Any]:
    """Отправляет фото с подписью в Telegram."""
    token, target = _token_and_chat(chat_id)
    if not token:
        return {"ok": False, "error": "credentials missing"}

    url = f"{_API_BASE.format(token=token)}/sendPhoto"
    try:
        resp = requests.post(
            url,
            data={"chat_id": target, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("preview.jpg", photo_bytes, "image/jpeg")},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            return {"ok": True, "message_id": data["result"].get("message_id")}
        return {"ok": False, "error": data.get("description")}
    except Exception as exc:
        logger.error("Telegram sendPhoto ошибка: %s", _describe_error(exc))
        # Fallback: отправляем текст без фото
        return send(caption, chat_id=chat_id, level="success")


def alert_error(step: str, error: str, attempt: int | None = None) -> dict[str, Any]:
    """Алерт об ошибке в пайплайне: агент + причина + номер попытки."""
    from utils.log_redact import redact
    attempt_line = f"Попытка: {attempt}\n" if attempt else ""
    text = (
        f"<b>Ошибка пайплайна</b>\n"
        f"Агент: <code>{_esc(step)}</code>\n"
        f"{attempt_line}"
        f"Причина: <code>{_esc(redact(error)[:400])}</code>"
    )
    return send(text, level="error")


def alert_success(
    title: str,
    platforms: list[str],
    post_id: str,
    duration: float | None = None,
    video_path: str | None = None,
    dry_run: bool = False,
    status: str | None = None,
) -> dict[str, Any]:
    """
    Уведомление об успешной публикации (или о собранном видео в dry-run).

    Если передан video_path — отправляет карточку с превью кадром из видео.
    """
    platforms_str = ", ".join(platforms)
    dur_str = f"\nДлительность: <b>{duration:.1f} сек</b>" if duration else ""
    status_str = f"\nСтатус: <b>{_esc(status)}</b>" if status and not dry_run else ""
    header = "Dry-run: видео собрано (не опубликовано)" if dry_run else "Видео опубликовано!"
    caption = (
        f"<b>{header}</b>\n"
        f"Тема: <b>{_esc(title)}</b>\n"
        f"Платформа: {_esc(platforms_str)}{dur_str}\n"
        f"ID поста: <code>{_esc(post_id)}</code>"
        f"{status_str}"
    )

    # Пробуем отправить с превью из видео
    if video_path and os.path.exists(video_path):
        frame = _extract_frame(video_path)
        if frame:
            return send_photo(frame, caption)

    return send(caption, level="success")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    msg = sys.argv[1] if len(sys.argv) > 1 else "Тестовое сообщение от Content Factory"
    result = send(msg, level="info")
    print(result)
