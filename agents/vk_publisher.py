"""
VK Publisher — публикация ролика в сообщество VK официальным методом video.save.

Доступ: ключ пользователя-администратора сообщества с правами video, wall, groups, offline
(VK_USER_TOKEN) и ID сообщества (VK_GROUP_ID, число без минуса). Право video новым приложениям
VK ID выдают только после согласования — запрос на devsupport@corp.vk.com. Пока ключа нет,
is_configured() = False и сериал пропускает VK с предупреждением.

Методы клипов (shortVideo.*) в документации VK не описаны — не используем. Видео уходит
в раздел видео сообщества с записью на стене (wallpost=1); попадёт ли вертикальный ролик
в ленту Клипов, проверяется на первой публикации.

Повторы: video.save создаёт запись видео, поэтому вызывается ровно один раз (иначе дубли);
загрузка файла на выданный upload_url повторяется при сетевых ошибках.

API:
    is_configured() → bool
    publish_video(video_path, title, description, hashtags) → {"post_id", "status", "platform", "url"}
"""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/"
API_VERSION = "5.199"

# Ошибки доступа: повтор не поможет, нужен другой ключ или настройка аккаунта
_ACCESS_ERRORS = {
    5: "ключ VK недействителен или истёк — получите новый VK_USER_TOKEN",
    15: "нет доступа: ключ без права video или пользователь не администратор сообщества",
    27: "ключ сообщества не подходит — нужен ключ пользователя-администратора",
    204: "доступ запрещён: проверьте права администратора в сообществе",
    704: "VK требует включить двухфакторную аутентификацию у владельца ключа",
    13000: "загрузка запрещена из-за действующих страйков сообщества",
}


class VkError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.getenv("VK_USER_TOKEN") and os.getenv("VK_GROUP_ID"))


def _call(method: str, **params) -> dict:
    # Ключ — в теле POST, не в URL: URL попадает в тексты исключений и логи
    data = {**params, "access_token": os.environ["VK_USER_TOKEN"], "v": API_VERSION}
    resp = requests.post(API_URL + method, data=data, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        code = body["error"].get("error_code")
        msg = body["error"].get("error_msg", "")
        hint = _ACCESS_ERRORS.get(code, "")
        raise VkError(f"VK {method}: {code} {msg}" + (f" — {hint}" if hint else ""))
    return body["response"]


def _upload(upload_url: str, video_path: str, attempts: int = 3) -> dict:
    for attempt in range(1, attempts + 1):
        try:
            with open(video_path, "rb") as f:
                resp = requests.post(upload_url, files={"video_file": f}, timeout=900)
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                raise VkError(f"VK upload: {body['error']}")
            return body
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == attempts:
                raise
            logger.warning("VK: загрузка файла оборвалась (%s) — повтор %d/%d", exc, attempt, attempts - 1)
            time.sleep(5 * attempt)
    raise VkError("VK upload: нет попыток")


def publish_video(video_path: str, title: str, description: str, hashtags: list[str]) -> dict:
    if not is_configured():
        raise VkError("VK не настроен: нужны VK_USER_TOKEN и VK_GROUP_ID в .env")
    group_id = os.environ["VK_GROUP_ID"].lstrip("-")
    tags = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
    text = f"{description}\n\n{tags}".strip()

    saved = _call("video.save", name=title[:128], description=text[:5000], group_id=group_id,
                  wallpost=1, repeat=0)
    logger.info("VK: video.save → video_id=%s, загружаю файл", saved.get("video_id"))
    uploaded = _upload(saved["upload_url"], video_path)

    owner_id = uploaded.get("owner_id") or saved.get("owner_id") or f"-{group_id}"
    video_id = uploaded.get("video_id") or saved.get("video_id")
    if not video_id:
        raise VkError(f"VK: загрузка без video_id: {uploaded}")
    post_id = f"{owner_id}_{video_id}"
    logger.info("VK: опубликовано video%s", post_id)
    return {"post_id": post_id, "status": "published", "platform": "vk",
            "url": f"https://vk.com/video{post_id}"}
