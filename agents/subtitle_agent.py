"""
Subtitle Agent — транскрибирует аудио через Whisper, генерирует стилизованные ASS субтитры.

Вход:  audio_path (str), platform (str)
Выход: {"subtitle_path": str, "segment_count": int}
"""

import logging
import os
from datetime import datetime
from typing import Literal

logger = logging.getLogger(__name__)

_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        try:
            import whisper
        except ImportError:
            raise EnvironmentError("openai-whisper не установлен: pip install openai-whisper")
        logger.info("Загружаю Whisper base (первый раз)...")
        _whisper_model = whisper.load_model("base")
    return _whisper_model

Platform = Literal["tiktok", "youtube", "instagram", "linkedin", "twitter"]

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "subtitles")

PLATFORM_STYLE = {
    "tiktok":    {"fontsize": 96, "margin_v": 760, "max_chars": 10},
    "youtube":   {"fontsize": 96, "margin_v": 760, "max_chars": 10},
    "instagram": {"fontsize": 96, "margin_v": 760, "max_chars": 10},
    "linkedin":  {"fontsize": 72, "margin_v": 500, "max_chars": 24},
    "twitter":   {"fontsize": 96, "margin_v": 760, "max_chars": 12},
}

# Каждое N-ное слово подсвечиваем жёлтым (highlight) — добавляет ритм
HIGHLIGHT_EVERY = 3


def _ts(seconds: float) -> str:
    """Секунды → формат времени ASS (H:MM:SS.cc)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _chunk_words(words: list[dict], max_chars: int) -> list[dict]:
    """Группирует слова в субтитр-чанки не длиннее max_chars символов."""
    chunks: list[dict] = []
    buf: list[str] = []
    buf_len = 0
    t0: float | None = None

    for w in words:
        text = w["word"].strip()
        if not text:
            continue
        if t0 is None:
            t0 = w["start"]
        if buf and buf_len + len(text) + 1 > max_chars:
            chunks.append({"text": " ".join(buf), "start": t0, "end": w["start"]})
            buf, buf_len, t0 = [text], len(text), w["start"]
        else:
            buf.append(text)
            buf_len += len(text) + 1

    if buf and t0 is not None:
        chunks.append({"text": " ".join(buf), "start": t0, "end": words[-1]["end"]})

    return chunks


def _write_ass(chunks: list[dict], path: str, fontsize: int, margin_v: int) -> None:
    """
    Kinetic captions:
      - каждое chunk появляется с pop-bounce (scale 100→115→100 за 160мс)
      - каждое N-ное chunk — жёлтое (для ритма)
      - белый текст, жирная чёрная обводка (8px), без тени
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Жирная белая обводка — TikTok viral caption
        f"Style: Default,Impact,{fontsize},&H00FFFFFF,&H000000FF,"
        f"&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,8,0,2,40,40,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    # Pop-bounce: масштаб 80→118 за 90мс, потом 118→100 за 90мс
    pop_anim = r"\fscx80\fscy80\t(0,90,\fscx118\fscy118)\t(90,180,\fscx100\fscy100)"
    # Жёлтый акцент (ASS BGR: 00FFFF = yellow)
    yellow_color = r"\c&H00FFFF&"

    lines = [header]
    for i, c in enumerate(chunks):
        is_hi = (i + 1) % HIGHLIGHT_EVERY == 0
        text = c["text"].upper().replace("\n", " ")
        overrides = pop_anim + (yellow_color if is_hi else "")
        lines.append(
            f"Dialogue: 0,{_ts(c['start'])},{_ts(c['end'])},Default,,0,0,{margin_v},,"
            f"{{{overrides}}}{text}"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def generate_subtitles(audio_path: str, platform: Platform = "tiktok") -> dict:
    """
    Транскрибирует аудио через Whisper и генерирует ASS субтитры.

    Args:
        audio_path: Путь к MP3
        platform:   Платформа — определяет размер шрифта и позицию

    Returns:
        dict с полями: subtitle_path, segment_count

    Raises:
        EnvironmentError: если openai-whisper не установлен
        ValueError:       если platform не поддерживается
    """
    if platform not in PLATFORM_STYLE:
        raise ValueError(
            f"Неподдерживаемая платформа: {platform}. Допустимые: {list(PLATFORM_STYLE.keys())}"
        )

    style = PLATFORM_STYLE[platform]

    model = _get_whisper_model()
    logger.info("Транскрибирую аудио: %s", os.path.basename(audio_path))
    result = model.transcribe(audio_path, language="ru", word_timestamps=True)

    all_words = [w for seg in result["segments"] for w in seg.get("words", [])]

    if all_words:
        chunks = _chunk_words(all_words, style["max_chars"])
    else:
        chunks = [
            {"text": seg["text"].strip(), "start": seg["start"], "end": seg["end"]}
            for seg in result["segments"]
        ]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    subtitle_path = os.path.abspath(os.path.join(OUTPUT_DIR, f"{ts}_{platform}.ass"))

    _write_ass(chunks, subtitle_path, style["fontsize"], style["margin_v"])

    # Возвращаем word-level тайминги для entity_visuals (точные оверлеи)
    words_out = [
        {"text": (w.get("word") or "").strip(), "start": float(w.get("start", 0)), "end": float(w.get("end", 0))}
        for w in all_words
        if (w.get("word") or "").strip()
    ]

    return {
        "subtitle_path": subtitle_path,
        "segment_count": len(chunks),
        "words": words_out,
    }


if __name__ == "__main__":
    import sys
    import json

    audio = sys.argv[1] if len(sys.argv) > 1 else None
    if not audio:
        print("Использование: py agents/subtitle_agent.py <audio_path> [platform]")
        sys.exit(1)

    plat = sys.argv[2] if len(sys.argv) > 2 else "tiktok"
    print(f"Генерирую субтитры: {audio} для {plat}...\n")
    result = generate_subtitles(audio, plat)
    print(json.dumps(result, ensure_ascii=False, indent=2))
