"""
Thumbnail Agent — генерирует превью для YouTube через Pixazo API (Flux Schnell).

Вход:  title (str), platform (str), video_path (str | None)
Выход: {"thumbnail_path": str, "source": str}

Приоритет методов:
  1. Pixazo API (PIXAZO_API_KEY)
  2. Pillow — локальная генерация с градиентом и текстом
  3. Кадр из видео через ffmpeg

Установка: pip install Pillow
"""

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

PIXAZO_API_KEY = os.getenv("PIXAZO_API_KEY", "")
PIXAZO_BASE_URL = os.getenv("PIXAZO_BASE_URL", "https://api.pixazo.com/v1")

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "thumbnails")

_THUMB_PROMPT = (
    "YouTube Shorts thumbnail 2026, ultra-vibrant high contrast cinematic, "
    "bold large white text, dramatic shocked expression, 4K photorealistic, "
    "topic: {title}"
)


def _yandex_art(title: str, output_path: str) -> bool:
    """Генерирует thumbnail через YandexART ($0.018/изображение)."""
    api_key = os.getenv("YANDEX_API_KEY", "")
    folder_id = os.getenv("YANDEX_FOLDER_ID", "")
    if not api_key or not folder_id:
        return False

    body = {
        "modelUri": f"art://{folder_id}/yandex-art/latest",
        "generationOptions": {
            "seed": abs(hash(title)) % 1_000_000,
            "aspectRatio": {"widthRatio": 16, "heightRatio": 9},
        },
        "messages": [{
            "weight": 1,
            "text": f"YouTube thumbnail: {title[:120]}, яркий, контрастный, профессиональный",
        }],
    }

    try:
        resp = requests.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync",
            headers={"Authorization": f"Api-Key {api_key}"},
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        operation_id = resp.json().get("id", "")
        if not operation_id:
            logger.warning("YandexART: нет operation id в ответе")
            return False

        # Асинхронная генерация — поллинг до 60 сек
        for _ in range(30):
            time.sleep(2)
            poll = requests.get(
                f"https://llm.api.cloud.yandex.net/operations/{operation_id}",
                headers={"Authorization": f"Api-Key {api_key}"},
                timeout=15,
            )
            poll.raise_for_status()
            data = poll.json()
            if data.get("done"):
                img_b64 = data.get("response", {}).get("image", "")
                if img_b64:
                    with open(output_path, "wb") as f:
                        f.write(base64.b64decode(img_b64))
                    logger.info("Thumbnail via YandexART: %s", output_path)
                    return True
                logger.warning("YandexART: done=true но нет image в ответе")
                return False

        logger.warning("YandexART: таймаут ожидания результата (60 сек)")
        return False

    except Exception as exc:
        logger.warning("YandexART ошибка: %s — используем Pixazo/Pillow", exc)
        return False


def _pixazo(title: str, output_path: str) -> bool:
    """Генерирует thumbnail через Pixazo / Flux Schnell API."""
    if not PIXAZO_API_KEY:
        return False

    prompt = _THUMB_PROMPT.format(title=title[:80])
    try:
        resp = requests.post(
            f"{PIXAZO_BASE_URL}/generate",
            headers={
                "Authorization": f"Bearer {PIXAZO_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": "flux-schnell", "prompt": prompt, "width": 1280, "height": 720},
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()

        # Пробуем получить URL изображения
        image_url = (
            data.get("url")
            or data.get("image_url")
            or (data.get("images") or [{}])[0].get("url")
        )
        if image_url:
            img = requests.get(image_url, timeout=30)
            img.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(img.content)
            logger.info("Thumbnail via Pixazo: %s", output_path)
            return True

        # Пробуем base64
        b64 = data.get("b64_json") or (data.get("data") or [{}])[0].get("b64_json")
        if b64:
            with open(output_path, "wb") as f:
                f.write(base64.b64decode(b64))
            logger.info("Thumbnail (base64) via Pixazo: %s", output_path)
            return True

    except Exception as exc:
        logger.warning("Pixazo API ошибка: %s — используем Pillow", exc)

    return False


_PLATFORM_THUMB_SIZE = {
    "tiktok":    (1080, 1920),
    "instagram": (1080, 1920),
    "youtube":   (1280,  720),
    "linkedin":  (1280,  720),
    "twitter":   (1280,  720),
}

# Цветовые схемы по нише (угадываем по словам в заголовке)
_COLOR_SCHEMES = [
    ((220, 40, 40),   (140, 0, 80)),    # красный → малиновый
    ((255, 120, 0),   (200, 0, 100)),   # оранжевый → розовый
    ((0, 120, 220),   (60, 0, 180)),    # синий → фиолетовый
    ((0, 160, 80),    (0, 60, 120)),    # зелёный → синий
    ((180, 0, 200),   (60, 0, 120)),    # фиолетовый → тёмный
]


def _pillow(title: str, output_path: str, platform: str = "tiktok") -> bool:
    """Генерирует платформо-специфичный thumbnail через Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError:
        logger.warning("Pillow не установлен: pip install Pillow")
        return False

    try:
        W, H = _PLATFORM_THUMB_SIZE.get(platform, (1080, 1920))
        portrait = H > W

        img = Image.new("RGB", (W, H))
        draw = ImageDraw.Draw(img)

        # Диагональный градиент (для разнообразия — берём схему по хешу заголовка)
        scheme = _COLOR_SCHEMES[abs(hash(title)) % len(_COLOR_SCHEMES)]
        c1, c2 = scheme
        for y in range(H):
            t = y / H
            r = int(c1[0] * (1 - t) + c2[0] * t)
            g = int(c1[1] * (1 - t) + c2[1] * t)
            b = int(c1[2] * (1 - t) + c2[2] * t)
            draw.line([(0, y), (W, y)], fill=(r, g, b))

        # Тёмный полупрозрачный блок под текст (низ для portrait, середина для landscape)
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ov_draw = ImageDraw.Draw(overlay)
        if portrait:
            ov_draw.rectangle([0, H // 2, W, H], fill=(0, 0, 0, 160))
        else:
            ov_draw.rectangle([0, H // 3, W, H], fill=(0, 0, 0, 140))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        # Яркая горизонтальная полоска сверху
        draw.rectangle([0, 0, W, 8], fill=c1)

        # Шрифты
        fs_main = 90 if portrait else 72
        fs_sub = 44 if portrait else 36
        try:
            font = ImageFont.truetype("arialbd.ttf", fs_main)
            font_sub = ImageFont.truetype("arial.ttf", fs_sub)
        except Exception:
            try:
                font = ImageFont.truetype("arial.ttf", fs_main)
                font_sub = font
            except Exception:
                font = ImageFont.load_default()
                font_sub = font

        max_w = W - 80
        # Перенос текста
        words = title.split()
        lines: list[str] = []
        current = ""
        for word in words:
            test = (current + " " + word).strip()
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] > max_w and current:
                lines.append(current)
                current = word
            else:
                current = test
        if current:
            lines.append(current)

        lh = fs_main + 16
        total_h = len(lines) * lh
        if portrait:
            start_y = H * 55 // 100  # нижняя половина для portrait
        else:
            start_y = (H - total_h) // 2

        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            x = (W - tw) // 2
            y = start_y + i * lh
            # Жирная тень (4px смещение + размытие эффект: рисуем несколько раз)
            for dx, dy in [(-3, -3), (3, -3), (-3, 3), (3, 3), (4, 4)]:
                draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
            draw.text((x, y), line, font=font, fill=(255, 255, 255))

        img.save(output_path, "JPEG", quality=93)
        logger.info("Thumbnail via Pillow: %s", output_path)
        return True

    except Exception as exc:
        logger.warning("Pillow thumbnail ошибка: %s", exc)
        return False


def _from_video_frame(video_path: str, output_path: str) -> bool:
    """Извлекает кадр из видео через FFmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not video_path or not os.path.exists(video_path):
        return False
    try:
        subprocess.run(
            [
                ffmpeg, "-y", "-i", video_path,
                "-ss", "00:00:02", "-vframes", "1",
                "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
                output_path,
            ],
            capture_output=True, check=True, timeout=30,
        )
        ok = os.path.exists(output_path) and os.path.getsize(output_path) > 0
        if ok:
            logger.info("Thumbnail из кадра видео: %s", output_path)
        return ok
    except Exception as exc:
        logger.warning("Ошибка извлечения кадра: %s", exc)
        return False


def _pillow_variant(title: str, output_path: str, platform: str, variant_idx: int) -> bool:
    """
    Генерирует один вариант thumbnail с уникальной цветовой схемой и макетом.
    variant_idx 0..2 — разные стили.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    try:
        W, H = _PLATFORM_THUMB_SIZE.get(platform, (1080, 1920))
        portrait = H > W

        # Три разных стиля
        layouts = [
            # Вариант 0: тёмный фон + яркая полоска слева
            {"bg": (12, 12, 18), "accent": (255, 220, 0), "text_color": (255, 255, 255), "stripe": "left"},
            # Вариант 1: красный градиент снизу
            {"bg": (180, 15, 30), "accent": (255, 255, 255), "text_color": (255, 240, 60), "stripe": "bottom"},
            # Вариант 2: синий градиент
            {"bg": (15, 50, 140), "accent": (255, 200, 0), "text_color": (255, 255, 255), "stripe": "top"},
        ]
        layout = layouts[variant_idx % len(layouts)]

        img = Image.new("RGB", (W, H), layout["bg"])
        draw = ImageDraw.Draw(img)

        # Градиент фона
        for yi in range(H):
            ratio = yi / H
            bg = layout["bg"]
            r = max(0, min(255, int(bg[0] * (1 - ratio * 0.4))))
            g = max(0, min(255, int(bg[1] * (1 - ratio * 0.4))))
            b = max(0, min(255, int(bg[2] * (1 - ratio * 0.2))))
            draw.line([(0, yi), (W, yi)], fill=(r, g, b))

        # Акцентный элемент
        if layout["stripe"] == "left":
            draw.rectangle([0, 0, 12, H], fill=layout["accent"])
        elif layout["stripe"] == "bottom":
            draw.rectangle([0, H - 10, W, H], fill=layout["accent"])
        else:
            draw.rectangle([0, 0, W, 10], fill=layout["accent"])

        # Шрифт
        fs = 90 if portrait else 64
        font = None
        for fp in ["C:/Windows/Fonts/impact.ttf", "C:/Windows/Fonts/ariblk.ttf", "C:/Windows/Fonts/arialbd.ttf"]:
            if os.path.exists(fp):
                try:
                    font = ImageFont.truetype(fp, fs)
                    break
                except Exception:
                    continue
        if font is None:
            font = ImageFont.load_default()

        # Перенос текста
        max_w = W - 80
        words = title.split()
        lines: list[str] = []
        cur = ""
        for word in words:
            test = (cur + " " + word).strip()
            bbox = font.getbbox(test)
            if bbox[2] - bbox[0] > max_w and cur:
                lines.append(cur)
                cur = word
            else:
                cur = test
        if cur:
            lines.append(cur)

        lh = fs + 16
        total_h = len(lines) * lh
        start_y = (H // 2) - (total_h // 2) if not portrait else int(H * 0.5)

        ow = 5  # outline width
        for i, line in enumerate(lines):
            bbox = font.getbbox(line)
            tw = bbox[2] - bbox[0]
            x = (W - tw) // 2
            y = start_y + i * lh
            for dx in range(-ow, ow + 1, 2):
                for dy in range(-ow, ow + 1, 2):
                    if dx == 0 and dy == 0:
                        continue
                    draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
            draw.text((x, y), line, font=font, fill=layout["text_color"])

        img.save(output_path, "JPEG", quality=93)
        return True
    except Exception as exc:
        logger.warning("Pillow variant %d error: %s", variant_idx, exc)
        return False


def ab_test_thumbnails(
    title: str,
    platform: str = "youtube",
    video_path: str | None = None,
) -> dict[str, Any]:
    """
    Генерирует 3 варианта thumbnail, отправляет в Gemini Vision для сравнения,
    возвращает лучший. При ошибке Gemini — возвращает первый вариант.

    Returns:
        dict: {"thumbnail_path": ..., "source": ..., "ab_chosen": 0|1|2}
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w-]", "_", title)[:30]

    # Генерируем 3 Pillow-варианта
    candidates: list[str] = []
    for vi in range(3):
        path = os.path.abspath(os.path.join(OUTPUT_DIR, f"{ts}_ab{vi}_{safe}.jpg"))
        if _pillow_variant(title, path, platform, vi):
            candidates.append(path)

    if len(candidates) < 2:
        # Недостаточно вариантов — отдаём наверх, generate_thumbnail продолжит
        # цепочку (_pillow single). Вызов generate_thumbnail отсюда давал
        # взаимную рекурсию generate_thumbnail ↔ ab_test_thumbnails.
        raise RuntimeError("A/B: получилось меньше 2 Pillow-вариантов")

    from utils.providers import is_enabled, mark_dead, looks_like_no_credits

    gemini_key = os.getenv("GEMINI_API_KEY", "")
    chosen_idx = 0
    if gemini_key and is_enabled("gemini"):
        try:
            from google import genai
            from google.genai import types as genai_types
            client = genai.Client(api_key=gemini_key)

            parts: list[Any] = [
                f"""Ты — эксперт по YouTube Shorts thumbnail'ам.
Сравни {len(candidates)} варианта thumbnail для видео: "{title}"
Выбери САМЫЙ кликабельный (максимум CTR на мобильном экране).
Критерии: читаемость текста, контраст, эмоциональный отклик, нестандартность.
Верни ТОЛЬКО число: 0, 1 или {"2" if len(candidates) > 2 else "1"} (индекс лучшего варианта)."""
            ]
            for fp in candidates:
                with open(fp, "rb") as f:
                    img_bytes = f.read()
                parts.append(genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))

            resp = client.models.generate_content(
                model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                contents=parts,
            )
            raw = (resp.text or "").strip()
            m = re.search(r"\b([012])\b", raw)
            if m:
                chosen_idx = int(m.group(1))
                if chosen_idx >= len(candidates):
                    chosen_idx = 0
            logger.info("Thumbnail A/B: Gemini выбрал вариант %d из %d", chosen_idx, len(candidates))
        except Exception as exc:
            if looks_like_no_credits(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                mark_dead("gemini", str(exc)[:200])
            logger.warning("Thumbnail A/B: Gemini ошибка (%s) — берём вариант 0", exc)

    chosen_path = candidates[chosen_idx]

    # Переименовываем winner, удаляем остальные
    final_path = os.path.abspath(os.path.join(OUTPUT_DIR, f"{ts}_thumb_{safe}.jpg"))
    os.rename(chosen_path, final_path)
    for i, p in enumerate(candidates):
        if i != chosen_idx and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    return {"thumbnail_path": final_path, "source": "pillow_ab", "ab_chosen": chosen_idx}


def generate_thumbnail(
    title: str,
    platform: str = "youtube",
    video_path: str | None = None,
) -> dict[str, Any]:
    """
    Генерирует thumbnail изображение.

    Args:
        title:       Заголовок видео
        platform:    Платформа (прежде всего актуально для youtube)
        video_path:  Путь к видео (для извлечения кадра как последний fallback)

    Returns:
        dict с полями: thumbnail_path, source

    Raises:
        RuntimeError: если ни один метод не сработал
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    safe = re.sub(r"[^\w-]", "_", title)[:40]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.abspath(os.path.join(OUTPUT_DIR, f"{ts}_{safe}.jpg"))

    from utils.providers import is_enabled

    if is_enabled("yandexart") and _yandex_art(title, output_path):
        try:
            from utils.cost_tracker import log_cost
            log_cost("thumbnail_agent", "yandexart", "image", 1)
        except Exception:
            pass
        return {"thumbnail_path": output_path, "source": "yandex_art"}

    if _pixazo(title, output_path):
        return {"thumbnail_path": output_path, "source": "pixazo"}

    # A/B test with Pillow variants — выбирает лучший через Gemini Vision
    try:
        return ab_test_thumbnails(title, platform, video_path)
    except Exception as exc:
        logger.warning("Thumbnail A/B failed: %s — fallback single", exc)

    if _pillow(title, output_path, platform=platform):
        return {"thumbnail_path": output_path, "source": "pillow"}

    if video_path and _from_video_frame(video_path, output_path):
        return {"thumbnail_path": output_path, "source": "video_frame"}

    raise RuntimeError("Не удалось сгенерировать thumbnail ни одним из методов")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    title_arg = sys.argv[1] if len(sys.argv) > 1 else "5 фактов о мозге которые изменят тебя"
    video_arg = sys.argv[2] if len(sys.argv) > 2 else None
    result = generate_thumbnail(title_arg, "youtube", video_arg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
