"""
QA Agent — проверяет MP4 через ffprobe без LLM.

Вход:  video_path (str), platform (str), expected_duration (float | None)
Выход: {"status": "approved"} или {"status": "rejected", "reason": "..."}

Проверки:
  1. Файл существует и не пустой
  2. ffprobe читает контейнер без ошибок
  3. Длительность > 3 сек и не расходится с expected_duration более чем на 10%
  4. Соотношение сторон соответствует платформе (9:16 / 16:9 / 1:1)
  5. Видео-стрим присутствует
"""

import json
import logging
import os
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# Допустимые соотношения сторон на платформу (ширина / высота)
_PLATFORM_RATIOS: dict[str, tuple[float, float]] = {
    "tiktok":    (9 / 16, 9 / 16),   # вертикальное 9:16
    "instagram": (9 / 16, 9 / 16),   # вертикальное 9:16
    "youtube":   (9 / 16, 9 / 16),   # вертикальное 9:16 (Shorts)
    "linkedin":  (9 / 16, 9 / 16),   # вертикальное 9:16
    "twitter":   (9 / 16, 9 / 16),   # вертикальное 9:16
}
_RATIO_TOLERANCE = 0.05   # ±5% отклонение допустимо
_MIN_DURATION = 3.0        # секунды
_DURATION_TOLERANCE = 0.10 # ±10% от expected


def _ffprobe(video_path: str) -> dict[str, Any]:
    """Запускает ffprobe и возвращает JSON с метаданными."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise EnvironmentError("ffprobe не найден — установите FFmpeg")

    cmd = [
        ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe вернул ошибку: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _check_file(video_path: str) -> str | None:
    if not os.path.exists(video_path):
        return f"Файл не найден: {video_path}"
    if os.path.getsize(video_path) == 0:
        return "Файл пустой (0 байт)"
    return None


def _check_streams(probe: dict) -> tuple[str | None, dict | None]:
    """Возвращает (ошибку | None, video_stream | None)."""
    streams = probe.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        return "Видео-стрим не найден", None
    return None, video_streams[0]


def _check_duration(probe: dict, expected: float | None) -> str | None:
    duration_str = probe.get("format", {}).get("duration")
    if duration_str is None:
        return "Не удалось определить длительность"

    duration = float(duration_str)
    if duration < _MIN_DURATION:
        return f"Длительность {duration:.1f} сек < минимума {_MIN_DURATION} сек"

    if expected is not None:
        deviation = abs(duration - expected) / max(expected, 0.001)
        if deviation > _DURATION_TOLERANCE:
            return (
                f"Длительность {duration:.1f} сек расходится с ожидаемой "
                f"{expected:.1f} сек (отклонение {deviation*100:.0f}%)"
            )
    return None


def _check_ratio(video_stream: dict, platform: str) -> str | None:
    width = video_stream.get("width")
    height = video_stream.get("height")
    if not width or not height:
        return "Не удалось определить разрешение видео"

    actual_ratio = width / height
    allowed = _PLATFORM_RATIOS.get(platform)
    if allowed is None:
        return None  # неизвестная платформа — пропускаем проверку

    min_ratio, max_ratio = min(allowed), max(allowed)
    # Платформа с одним значением: проверяем точное совпадение с допуском
    if min_ratio == max_ratio:
        if abs(actual_ratio - min_ratio) > _RATIO_TOLERANCE:
            expected_str = f"{min_ratio:.3f}"
            return (
                f"Соотношение сторон {width}x{height} ({actual_ratio:.3f}) "
                f"не соответствует платформе {platform} (ожидается ~{expected_str})"
            )
    else:
        # Диапазон — достаточно попасть в любое из допустимых
        in_range = any(
            abs(actual_ratio - r) <= _RATIO_TOLERANCE for r in allowed
        )
        if not in_range:
            return (
                f"Соотношение сторон {width}x{height} ({actual_ratio:.3f}) "
                f"не входит в допустимые для {platform}"
            )
    return None


def check(
    video_path: str,
    platform: str = "tiktok",
    expected_duration: float | None = None,
) -> dict[str, Any]:
    """
    Проверяет MP4 и возвращает статус QA.

    Returns:
        {"status": "approved"} или {"status": "rejected", "reason": "..."}
    """
    # 1. Файл
    err = _check_file(video_path)
    if err:
        logger.warning("QA rejected: %s", err)
        return {"status": "rejected", "reason": err}

    # 2. ffprobe
    try:
        probe = _ffprobe(video_path)
    except Exception as exc:
        reason = f"ffprobe ошибка: {exc}"
        logger.warning("QA rejected: %s", reason)
        return {"status": "rejected", "reason": reason}

    # 3. Стримы
    err, video_stream = _check_streams(probe)
    if err:
        logger.warning("QA rejected: %s", err)
        return {"status": "rejected", "reason": err}

    # 4. Длительность
    err = _check_duration(probe, expected_duration)
    if err:
        logger.warning("QA rejected: %s", err)
        return {"status": "rejected", "reason": err}

    # 5. Соотношение сторон
    err = _check_ratio(video_stream, platform)
    if err:
        logger.warning("QA rejected: %s", err)
        return {"status": "rejected", "reason": err}

    duration = float(probe.get("format", {}).get("duration", 0))
    width = video_stream.get("width", 0)
    height = video_stream.get("height", 0)
    logger.info(
        "QA approved: %s (%dx%d, %.1f сек, платформа=%s)",
        os.path.basename(video_path), width, height, duration, platform,
    )
    return {"status": "approved", "duration": duration, "resolution": f"{width}x{height}"}


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Использование: py agents/qa_agent.py <video.mp4> [platform]")
        sys.exit(1)

    path = sys.argv[1]
    plat = sys.argv[2] if len(sys.argv) > 2 else "tiktok"
    result = check(path, platform=plat)
    print(json.dumps(result, ensure_ascii=False, indent=2))
