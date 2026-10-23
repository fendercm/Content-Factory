"""
Атомарная загрузка файлов в кэш.

Агенты кэшируют скачанное по факту существования файла (pexels_<id>.mp4 и т.п.).
Если писать сразу в итоговый путь, оборванная загрузка оставляет битый файл,
который потом вечно берётся из кэша и роняет FFmpeg. Поэтому пишем в
<dest>.part и переименовываем только после полной загрузки.

URL приходят из ответов внешних API (Pexels, KIE, Suno), поэтому качаем только
http(s) и не больше max_bytes — битый или враждебный ответ не забьёт диск.

API:
    download_to(url, dest, timeout=60, headers=None, min_bytes=1, max_bytes=2 ГБ) → dest
    is_cached(path, min_bytes=1) → bool   — файл есть и не пустой; обновляет mtime
"""

import os
from urllib.parse import urlsplit

import requests

MAX_DOWNLOAD_BYTES = 2 * 1024 ** 3


def download_to(
    url: str,
    dest: str,
    timeout: float = 60,
    headers: dict | None = None,
    min_bytes: int = 1,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> str:
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        raise ValueError(f"Скачивание разрешено только по http(s): {url[:80]}")
    tmp = dest + ".part"
    try:
        with requests.get(url, stream=True, timeout=timeout, headers=headers) as r:
            r.raise_for_status()
            declared = int(r.headers.get("Content-Length") or 0)
            if declared > max_bytes:
                raise RuntimeError(f"Файл больше лимита ({declared} > {max_bytes} байт): {os.path.basename(dest)}")
            written = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    written += len(chunk)
                    if written > max_bytes:
                        raise RuntimeError(f"Файл больше лимита ({max_bytes} байт): {os.path.basename(dest)}")
                    f.write(chunk)
        size = os.path.getsize(tmp)
        if size < min_bytes:
            raise RuntimeError(f"Скачанный файл слишком мал ({size} байт): {os.path.basename(dest)}")
        os.replace(tmp, dest)
        return dest
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def is_cached(path: str, min_bytes: int = 1) -> bool:
    """
    True, если файл уже скачан. Обновляет mtime — housekeeping чистит
    кэш по давности последнего использования, а не скачивания.
    """
    try:
        if os.path.getsize(path) < min_bytes:
            return False
        os.utime(path, None)
        return True
    except OSError:
        return False
