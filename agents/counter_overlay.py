"""
Counter Overlay Agent — анимированные счётчики для статистики в сценарии.

Когда в сценарии встречается "80%", "23 минуты", "94 из 100" и т.п.,
генерирует MP4-оверлей с анимацией числа от 0 до N (за 0.8 сек).

API:
    find_counters(script_text, audio_duration) → list[dict]
    render_counter_clip(value, suffix, duration, width, height) → str (path)

Каждый counter-dict:
    {value, suffix, start_sec, duration, position}

Интеграция: передаётся в assembly_agent как counter_overlays.
Рендер выполняется через Pillow + ffmpeg (аналогично entity overlays).
"""

import hashlib
import logging
import os
import re
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

COUNTER_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "clips", "counters")

# Паттерны для детекции чисел + единиц измерения
_PATTERNS = [
    # Процент: "80%", "94%", "7%"
    (r"\b(\d{1,3})%", "%"),
    # Число + минуты: "23 минуты", "20 минут", "45 мин"
    (r"\b(\d{1,3})\s+(?:минут[аыуе]?|мин\.?)\b", " мин"),
    # Число + раза: "в 3 раза", "в 10 раз"
    (r"\bв\s+(\d{1,2})\s+раз[а]?\b", "×"),
    # Число + часов: "8 часов", "24 часа"
    (r"\b(\d{1,2})\s+(?:часов|часа|час)\b", " ч"),
    # Число + дней: "21 день", "30 дней"
    (r"\b(\d{1,3})\s+(?:дней|дня|день)\b", " д"),
    # Большие числа с единицей: "1000 человек", "500 мл"
    (r"\b(\d{3,6})\s+(?:человек|людей|пациент|испытуем)\b", " чел"),
]

# Максимум оверлеев — слишком много мешают
_MAX_COUNTERS = 3


def find_counters(script_text: str, audio_duration: float) -> list[dict[str, Any]]:
    """
    Сканирует сценарий и возвращает список счётчиков для анимации.

    Args:
        script_text:    Полный текст сценария
        audio_duration: Длительность аудио в секундах

    Returns:
        list[{value, suffix, start_sec, duration}]
    """
    if not script_text or audio_duration <= 0:
        return []

    text_len = max(len(script_text), 1)
    counters: list[dict] = []
    seen_positions: list[int] = []

    for pattern, suffix in _PATTERNS:
        for m in re.finditer(pattern, script_text, re.IGNORECASE):
            if len(counters) >= _MAX_COUNTERS:
                break
            value_str = m.group(1)
            try:
                value = int(value_str)
            except ValueError:
                continue

            # Не анимируем слишком маленькие или нереалистичные числа
            if value <= 1 or value > 999_999:
                continue

            char_pos = m.start()
            # Не создаём два счётчика слишком близко (< 10% от длины текста)
            min_gap = int(text_len * 0.1)
            if any(abs(char_pos - p) < min_gap for p in seen_positions):
                continue

            # Линейная оценка тайм-кода
            start_sec = max(1.0, (char_pos / text_len) * audio_duration - 0.5)
            # Не заходим в последние 3 секунды (там end-card)
            if start_sec > audio_duration - 3.0:
                continue

            counters.append({
                "value": value,
                "suffix": suffix,
                "start_sec": round(start_sec, 2),
                "duration": 1.2,  # длительность анимации счётчика
            })
            seen_positions.append(char_pos)

        if len(counters) >= _MAX_COUNTERS:
            break

    logger.info("Counter Overlay: найдено %d счётчиков", len(counters))
    return counters


def render_counter_clip(
    value: int,
    suffix: str,
    duration: float,
    width: int,
    height: int,
    fps: int = 30,
) -> str:
    """
    Генерирует MP4 с анимацией числа 0 → value за первые 0.8 сек,
    потом статичное число до конца duration.

    Стиль: большой жёлтый Impact, тёмный полупрозрачный фон-плашка внизу.
    Кешируется по (value + suffix + duration).

    Returns:
        Путь к MP4
    """
    os.makedirs(COUNTER_DIR, exist_ok=True)
    sig = hashlib.md5(
        f"counter|{value}|{suffix}|{duration:.2f}|{width}x{height}|v1".encode()
    ).hexdigest()[:10]
    out = os.path.abspath(os.path.join(COUNTER_DIR, f"counter_{sig}.mp4"))
    if os.path.exists(out) and os.path.getsize(out) > 10_000:
        return out

    # Генерируем кадры через Pillow и склеиваем ffmpeg
    try:
        _render_counter_frames(value, suffix, duration, width, height, fps, out)
    except Exception as exc:
        logger.warning("Counter render failed %d%s: %s", value, suffix, exc)
        raise
    return out


def _render_counter_frames(
    value: int,
    suffix: str,
    duration: float,
    width: int,
    height: int,
    fps: int,
    out_path: str,
) -> None:
    """Генерирует PNG-кадры счётчика и склеивает в MP4."""
    from PIL import Image, ImageDraw, ImageFont
    import tempfile, shutil

    total_frames = max(int(duration * fps), fps)
    anim_frames = min(int(0.8 * fps), total_frames)  # анимация занимает 0.8 сек

    # Шрифт
    font_paths = [
        ("C:/Windows/Fonts/impact.ttf", 160),
        ("C:/Windows/Fonts/ariblk.ttf", 140),
        ("C:/Windows/Fonts/arialbd.ttf", 140),
    ]
    font = None
    for fp, sz in font_paths:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, sz)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    tmp_dir = tempfile.mkdtemp(prefix="counter_frames_")
    try:
        for frame_i in range(total_frames):
            if frame_i < anim_frames:
                # Easing: ease-out (квадратичная)
                t = frame_i / anim_frames
                t_eased = 1 - (1 - t) ** 2
                current_value = int(t_eased * value)
            else:
                current_value = value

            text = f"{current_value}{suffix}"

            # Прозрачный фон (RGBA)
            img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            # Плашка снизу-по-центру
            bbox = font.getbbox(text)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            pad_x, pad_y = 40, 20
            bar_w = text_w + pad_x * 2
            bar_h = text_h + pad_y * 2
            bar_x = (width - bar_w) // 2
            bar_y = height - 320  # над зоной субтитров (margin_v=760 → ~1160px, мы выше)

            # Тёмная полупрозрачная плашка
            bar_img = Image.new("RGBA", (bar_w, bar_h), (10, 18, 30, 200))
            img.paste(bar_img, (bar_x, bar_y), bar_img)

            # Текст: жёлтый, центрированный
            tx = bar_x + pad_x
            ty = bar_y + pad_y
            # Лёгкая тень
            draw.text((tx + 3, ty + 3), text, font=font, fill=(0, 0, 0, 180))
            draw.text((tx, ty), text, font=font, fill=(255, 220, 0, 255))

            frame_path = os.path.join(tmp_dir, f"frame_{frame_i:05d}.png")
            img.save(frame_path, "PNG")

        # Склеиваем в MP4 через ffmpeg
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", os.path.join(tmp_dir, "frame_%05d.png"),
            "-vf", f"scale={width}:{height}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuva420p",
            "-t", f"{duration:.4f}",
            "-an", out_path,
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            # yuva420p не везде поддерживается — fallback yuv420p
            cmd[-3] = "yuv420p"
            subprocess.run(cmd, capture_output=True, check=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    import json, sys, logging
    logging.basicConfig(level=logging.INFO)
    script = sys.argv[1] if len(sys.argv) > 1 else (
        "94% людей делают это каждый день. Мозгу нужно 23 минуты на восстановление."
    )
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    counters = find_counters(script, dur)
    print(json.dumps(counters, ensure_ascii=False, indent=2))
