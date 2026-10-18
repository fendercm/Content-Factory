"""
Publisher Agent — публикует MP4 в социальные сети через Zernio API.

Вход:  video_path (str), title (str), description (str),
       hashtags (list[str]), platform (str), account_id (str)
Выход: {"post_id": str, "status": str, "platform": str}

Флоу:
  1. POST /media/presign   — получаем presigned URL для загрузки файла
  2. PUT <presigned_url>   — загружаем MP4
  3. POST /posts           — создаём пост с mediaItem URL

Шаги 1-2 идемпотентны и ретраятся здесь же. Шаг 3 — ровно одна попытка:
повтор после таймаута/обрыва соединения создаёт дубль поста. Orchestrator
тоже вызывает агента с max_retries=1.

Документация: https://docs.zernio.com/api/openapi
"""

import logging
import os
import mimetypes
import time
from typing import Literal

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE_URL = "https://zernio.com/api/v1"

_UPLOAD_ATTEMPTS = 3
_UPLOAD_BACKOFF = 5  # секунд, растёт линейно

# Статусы поста, при которых публикация считается неуспешной
_FAILED_STATUSES = {"failed", "error", "rejected", "cancelled", "canceled"}

Platform = Literal[
    "tiktok", "instagram", "youtube", "twitter", "linkedin",
    "facebook", "threads", "reddit", "pinterest", "bluesky",
    "googlebusiness", "telegram", "snapchat",
]


def _headers() -> dict:
    api_key = os.environ.get("ZERNIO_API_KEY", "")
    if not api_key:
        raise EnvironmentError("ZERNIO_API_KEY не задан в .env")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _with_retries(what: str, fn, *args):
    """Ретрай идемпотентных шагов (presign, PUT по presigned URL). 4xx не ретраим."""
    for attempt in range(1, _UPLOAD_ATTEMPTS + 1):
        try:
            return fn(*args)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if 400 <= status < 500 or attempt == _UPLOAD_ATTEMPTS:
                raise
            logger.warning("%s: HTTP %s — повтор %d/%d", what, status, attempt, _UPLOAD_ATTEMPTS)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == _UPLOAD_ATTEMPTS:
                raise
            logger.warning("%s: %s — повтор %d/%d", what, type(exc).__name__, attempt, _UPLOAD_ATTEMPTS)
        time.sleep(_UPLOAD_BACKOFF * attempt)


def _presign(filename: str, size: int, mime_type: str) -> dict:
    """Запрашивает presigned URL для загрузки файла."""
    resp = requests.post(
        f"{BASE_URL}/media/presign",
        headers=_headers(),
        json={"filename": filename, "size": size, "contentType": mime_type},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _upload_file(presign_url: str, video_path: str, mime_type: str) -> None:
    """Загружает файл по presigned URL через PUT."""
    with open(video_path, "rb") as f:
        resp = requests.put(
            presign_url,
            data=f,
            headers={"Content-Type": mime_type},
            timeout=300,
        )
    resp.raise_for_status()


def _create_post(
    media_url: str,
    filename: str,
    file_size: int,
    mime_type: str,
    title: str,
    description: str,
    hashtags: list[str],
    platform: str,
    account_id: str,
    thumbnail_url: str | None = None,
) -> dict:
    """Создаёт пост через Zernio API."""
    media_item = {
        "type": "video",
        "url": media_url,
        "filename": filename,
        "size": file_size,
        "mimeType": mime_type,
    }

    platform_entry: dict = {"platform": platform, "accountId": account_id}
    platform_specific: dict = {}
    if thumbnail_url and platform == "youtube":
        platform_specific["thumbnailUrl"] = thumbnail_url
    if platform_specific:
        platform_entry["platformSpecificData"] = platform_specific

    body: dict = {
        "title": title,
        "content": description,
        "mediaItems": [media_item],
        "hashtags": hashtags,
        "platforms": [platform_entry],
        "visibility": "public",
        "privacy": "public",
        "publishNow": True,
    }

    resp = requests.post(
        f"{BASE_URL}/posts",
        headers=_headers(),
        json=body,
        timeout=60,
    )
    if not resp.ok:
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text[:500]
        logger.error(
            "Zernio POST /posts %d: %s\nBody sent: %s",
            resp.status_code, err_body,
            {k: v for k, v in body.items() if k != "mediaItems"},
        )
    resp.raise_for_status()
    return resp.json()


def publish_video(
    video_path: str,
    title: str,
    description: str,
    hashtags: list[str],
    platform: Platform,
    account_id: str,
    thumbnail_path: str | None = None,
) -> dict:
    """
    Загружает MP4 и публикует пост в социальную сеть через Zernio.

    Args:
        video_path:  Абсолютный путь к MP4
        title:       Заголовок поста (обязателен для YouTube / TikTok)
        description: Описание / текст поста
        hashtags:    Список хэштегов без # (["лайфхак", "тренды"])
        platform:    Платформа публикации
        account_id:  ID аккаунта в Zernio (из дашборда)

    Returns:
        dict с полями: post_id, status, platform

    Raises:
        EnvironmentError: если ZERNIO_API_KEY не задан
        ValueError:       если файл не найден
        requests.HTTPError: если API вернул ошибку
    """
    if not os.path.exists(video_path):
        raise ValueError(f"Видео файл не найден: {video_path}")
    if not account_id:
        raise ValueError("account_id пустой — задайте ZERNIO_ACCOUNT_ID в .env")

    filename = os.path.basename(video_path)
    file_size = os.path.getsize(video_path)
    mime_type = mimetypes.guess_type(video_path)[0] or "video/mp4"

    logger.info("Запрашиваю presigned URL для %s (%d MB)...", filename, file_size // 1024 // 1024)
    presign_data = _with_retries("presign", _presign, filename, file_size, mime_type)
    presign_url = presign_data["uploadUrl"]
    media_url = presign_data["publicUrl"]

    logger.info("Загружаю видео на Zernio...")
    _with_retries("upload", _upload_file, presign_url, video_path, mime_type)

    # Загружаем thumbnail отдельно (только для YouTube)
    thumbnail_url: str | None = None
    if thumbnail_path and os.path.exists(thumbnail_path) and platform == "youtube":
        try:
            logger.info("Загружаю thumbnail для YouTube...")
            thumb_name = os.path.basename(thumbnail_path)
            thumb_size = os.path.getsize(thumbnail_path)
            thumb_presign = _presign(thumb_name, thumb_size, "image/jpeg")
            _upload_file(thumb_presign["uploadUrl"], thumbnail_path, "image/jpeg")
            thumbnail_url = thumb_presign["publicUrl"]
        except Exception as exc:
            logger.warning("Не удалось загрузить thumbnail: %s", exc)

    logger.info("Публикую на %s...", platform)
    result = _create_post(
        media_url=media_url,
        filename=filename,
        file_size=file_size,
        mime_type=mime_type,
        title=title,
        description=description,
        hashtags=hashtags,
        platform=platform,
        account_id=account_id,
        thumbnail_url=thumbnail_url,
    )

    post = result.get("post", result)
    post_id = post.get("_id") or post.get("id") or ""
    status = str(post.get("status", "unknown"))
    if not post_id:
        raise RuntimeError(f"Zernio не вернул id поста (status={status}) — проверьте дашборд вручную")
    if status.lower() in _FAILED_STATUSES:
        raise RuntimeError(f"Zernio: пост {post_id} в статусе {status}")
    if status.lower() == "draft":
        logger.warning("Zernio: пост %s создан как черновик, а не опубликован", post_id)
    return {
        "post_id": post_id,
        "status": status,
        "platform": platform,
    }


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 5:
        print(
            "Использование: py agents/publisher_agent.py "
            "<video_path> <title> <description> <platform> [account_id]"
        )
        sys.exit(1)

    result = publish_video(
        video_path=sys.argv[1],
        title=sys.argv[2],
        description=sys.argv[3],
        hashtags=["shorts", "viral"],
        platform=sys.argv[4],
        account_id=sys.argv[5] if len(sys.argv) > 5 else "default",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
