"""
Image Agent — генерация иллюстраций через Pollinations.ai (FLUX, бесплатно).

Используется story-форматом: сцены эпизода рендерятся с фиксированным seed
персонажа + единым стилевым суффиксом мира → консистентный визуальный ряд.

API:
    generate_image(prompt, seed, width, height) → path | None
    generate_scenes(scenes, style_suffix, seed, workers=3) → list[path | None]

Pollinations: https://image.pollinations.ai/prompt/{prompt}?width=&height=&seed=&model=flux&nologo=true
Без ключа работает; POLLINATIONS_API_KEY (опционально) снимает лимиты.
"""

import hashlib
import logging
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://image.pollinations.ai/prompt/"
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "story_images")

_TIMEOUT = 120          # FLUX может рендерить 20-60 сек
_RETRIES = 3
_RETRY_SLEEP = 8


def _cache_path(prompt: str, seed: int, width: int, height: int) -> str:
    sig = hashlib.md5(f"{prompt}|{seed}|{width}x{height}".encode()).hexdigest()[:16]
    return os.path.abspath(os.path.join(CACHE_DIR, f"scene_{sig}.jpg"))


def generate_image(
    prompt: str,
    seed: int = 42,
    width: int = 1080,
    height: int = 1920,
) -> str | None:
    """Генерирует изображение через Pollinations FLUX. Возвращает путь или None."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    dest = _cache_path(prompt, seed, width, height)
    if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
        logger.info("Image Agent: кэш → %s", os.path.basename(dest))
        return dest

    params = {
        "width": width,
        "height": height,
        "seed": seed,
        "model": "flux",
        "nologo": "true",
        "enhance": "false",
    }
    headers = {}
    api_key = os.getenv("POLLINATIONS_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = _BASE_URL + urllib.parse.quote(prompt[:800], safe="")

    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if "image" not in content_type or len(resp.content) < 10_000:
                raise RuntimeError(
                    f"не изображение (type={content_type}, {len(resp.content)} байт)"
                )
            with open(dest, "wb") as f:
                f.write(resp.content)
            logger.info("Image Agent: сгенерировано (%d КБ) → %s",
                        len(resp.content) // 1024, os.path.basename(dest))
            return dest
        except Exception as exc:
            logger.warning("Image Agent: попытка %d/%d ошибка: %s", attempt, _RETRIES, exc)
            if attempt < _RETRIES:
                time.sleep(_RETRY_SLEEP * attempt)

    return None


def generate_scenes(
    scene_prompts: list[str],
    style_suffix: str = "",
    seed: int = 42,
    width: int = 1080,
    height: int = 1920,
    workers: int = 3,
) -> list[str | None]:
    """
    Генерирует изображения для сцен эпизода параллельно.
    Один seed на все сцены + единый стиль → консистентный персонаж/палитра.
    Возвращает список путей в порядке сцен (None для проваленных).
    """
    full_prompts = [
        f"{p.strip().rstrip('.')}, {style_suffix}" if style_suffix else p
        for p in scene_prompts
    ]

    results: list[str | None] = [None] * len(full_prompts)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="img") as ex:
        futures = {
            ex.submit(generate_image, p, seed, width, height): i
            for i, p in enumerate(full_prompts)
        }
        for fut in futures:
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:
                logger.warning("Image Agent: сцена %d провалена: %s", i, exc)

    ok = sum(1 for r in results if r)
    logger.info("Image Agent: %d/%d сцен готово", ok, len(results))
    return results


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    prompt = sys.argv[1] if len(sys.argv) > 1 else (
        "a lone hooded wanderer standing on a cliff overlooking a ruined city, "
        "dark fantasy digital painting, dramatic lighting, cinematic"
    )
    path = generate_image(prompt, seed=7)
    print(f"Результат: {path}")
