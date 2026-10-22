"""
Subtitle Agent — транскрибирует аудио через Whisper, генерирует стилизованные ASS субтитры.

Вход:  audio_path (str), platform (str), script_text (str | None)
Выход: {"subtitle_path": str, "segment_count": int, "words": [...], "aligned": bool}

Если передан script_text, Whisper даёт только тайминги, а текст слов берётся
из сценария (выравнивание difflib): озвучивается ровно сценарий, а Whisper base
на русском ошибается в окончаниях и числах — в субтитрах это выглядит как брак.
"""

import difflib
import logging
import os
import re
import uuid
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
        name = os.getenv("WHISPER_MODEL", "base")
        logger.info("Загружаю Whisper %s (первый раз)...", name)
        _whisper_model = whisper.load_model(name)
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


_MIN_ALIGN_RATIO = 0.6  # ниже — озвучка заметно разошлась со сценарием, верим Whisper


def _norm(token: str) -> str:
    return re.sub(r"[^\w]", "", token.lower().replace("ё", "е"))


def _align_to_script(words: list[dict], script_text: str) -> list[dict] | None:
    """
    Переносит тайминги Whisper на слова сценария. Возвращает
    [{"word", "start", "end"}] в написании сценария или None, если совпадение слабое.
    """
    script_tokens = [t for t in re.findall(r"\S+", script_text) if _norm(t)]
    asr = [w for w in words if _norm(w.get("word", ""))]
    if not script_tokens or not asr:
        return None

    a = [_norm(w["word"]) for w in asr]
    b = [_norm(t) for t in script_tokens]
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if matcher.ratio() < _MIN_ALIGN_RATIO:
        return None

    timed: list[list[float] | None] = [None] * len(script_tokens)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                timed[j1 + k] = [float(asr[i1 + k]["start"]), float(asr[i1 + k]["end"])]
        elif op == "replace":
            # Например «120» ↔ «сто двадцать»: делим отрезок пропорционально длине слов
            t0, t1 = float(asr[i1]["start"]), float(asr[i2 - 1]["end"])
            lengths = [max(len(b[j]), 1) for j in range(j1, j2)]
            total, cursor = sum(lengths), t0
            for j, ln in zip(range(j1, j2), lengths):
                step = (t1 - t0) * ln / total
                timed[j] = [cursor, cursor + step]
                cursor += step
        # delete — лишние слова Whisper отбрасываем; insert — заполним ниже

    # Слова сценария, которых Whisper не услышал, — в паузу между соседями
    n = len(timed)
    j = 0
    while j < n:
        if timed[j] is not None:
            j += 1
            continue
        k = j
        while k < n and timed[k] is None:
            k += 1
        left = timed[j - 1][1] if j > 0 else (timed[k][0] if k < n else 0.0)
        right = timed[k][0] if k < n else left + 0.3 * (k - j)
        step = max(right - left, 0.0) / (k - j)
        for m in range(j, k):
            timed[m] = [left + step * (m - j), left + step * (m - j + 1)]
        j = k

    return [
        {"word": tok.strip("«»\"()"), "start": t[0], "end": max(t[1], t[0] + 0.05)}
        for tok, t in zip(script_tokens, timed)
    ]


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


def generate_subtitles(
    audio_path: str,
    platform: Platform = "tiktok",
    script_text: str | None = None,
) -> dict:
    """
    Транскрибирует аудио через Whisper и генерирует ASS субтитры.

    Args:
        audio_path:  Путь к MP3
        platform:    Платформа — определяет размер шрифта и позицию
        script_text: Текст, который озвучивался. Если задан — слова в субтитрах
                     берутся из него, от Whisper только тайминги.

    Returns:
        dict с полями: subtitle_path, segment_count, words, aligned

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
    # initial_prompt со сценарием НЕ передаём: на Whisper base он вызывает
    # галлюцинации (проверено 2026-10: совпадение со сценарием падает с 0.93 до 0.1)
    result = model.transcribe(audio_path, language="ru", word_timestamps=True)

    all_words = [w for seg in result["segments"] for w in seg.get("words", [])]
    aligned = False
    if script_text and all_words:
        script_words = _align_to_script(all_words, script_text)
        if script_words:
            all_words, aligned = script_words, True
        else:
            logger.warning("Субтитры: озвучка слабо совпала со сценарием — оставляю текст Whisper")

    if all_words:
        chunks = _chunk_words(all_words, style["max_chars"])
    else:
        chunks = [
            {"text": seg["text"].strip(), "start": seg["start"], "end": seg["end"]}
            for seg in result["segments"]
        ]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    subtitle_path = os.path.abspath(os.path.join(OUTPUT_DIR, f"{ts}_{platform}_{uuid.uuid4().hex[:6]}.ass"))

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
        "aligned": aligned,
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


# ---------------------------------------------------------------------------
# Диалоговые субтитры сериала: тайминги известны из озвучки по ролям
# ---------------------------------------------------------------------------

_PHRASE_END = re.compile(r"[.,!?:;…—]$")


def _group_words(words: list[str], max_chars: int, max_words: int) -> list[list[int]]:
    """Индексы слов по кускам субтитров: до max_words слов и max_chars символов, разрыв после знака препинания."""
    groups: list[list[int]] = []
    for i, w in enumerate(words):
        if groups:
            cur = [words[j] for j in groups[-1]]
            if (len(" ".join(cur + [w])) <= max_chars and len(cur) < max_words
                    and not _PHRASE_END.search(cur[-1])):
                groups[-1].append(i)
                continue
        groups.append([i])
    return groups


def _dialogue_chunks(text: str, start: float, end: float, max_chars: int, max_words: int = 3,
                     words: list[dict] | None = None) -> list[dict]:
    """
    Реплика → куски субтитров. words — слова Whisper, выровненные на сценарий ({word, start, end}):
    кусок появляется, когда звучит его первое слово. Без words время делится пропорционально длине.
    """
    if words:
        tokens = [w["word"] for w in words]
        groups = _group_words(tokens, max_chars, max_words)
        out = []
        for k, g in enumerate(groups):
            s = start if k == 0 else max(float(words[g[0]]["start"]), start)
            e = float(words[groups[k + 1][0]]["start"]) if k + 1 < len(groups) else end
            out.append({"text": " ".join(tokens[i] for i in g), "start": s, "end": max(e, s + 0.05)})
        return out

    tokens = text.split()
    groups = [[tokens[i] for i in g] for g in _group_words(tokens, max_chars, max_words)]
    total = sum(len(" ".join(g)) for g in groups) or 1
    out, t = [], start
    for g in groups:
        chunk = " ".join(g)
        dur = (end - start) * len(chunk) / total
        out.append({"text": chunk, "start": t, "end": t + dur})
        t += dur
    return out


def write_dialogue_ass(
    lines: list[dict],
    path: str,
    colors: dict[str, str],
    default_color: str = "&H00FFFFFF&",
    brand_text: str | None = None,
    brand_seconds: float = 3.0,
    fontsize: int = 92,
    margin_v: int = 620,
    max_chars: int = 18,
    labels: dict[str, str] | None = None,
    intro_labels: dict[str, str] | None = None,
    label_fontsize: int = 46,
    play_res: tuple[int, int] = (1080, 1920),
) -> int:
    """
    lines: [{role, text, start, end}] — из dialogue_voice. Цвет — по роли (ASS BGR, «&HAABBGGRR&»).
    brand_text: плашка сериала вверху кадра на первые brand_seconds секунд.
    labels: {роль: подпись} — имя говорящего над субтитрами, пока звучит его реплика;
    intro_labels: подпись при первом появлении роли (имя + кто это). Роль без подписи — без плашки.
    Возвращает число событий.
    """
    labels, intro_labels = labels or {}, intro_labels or {}
    label_margin = margin_v + fontsize + 40
    header = (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {play_res[0]}\nPlayResY: {play_res[1]}\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Impact,{fontsize},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,8,0,2,60,60,{margin_v},1\n"
        "Style: Brand,Arial,46,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,6,0,3,4,0,8,60,60,150,1\n"
        f"Style: Speaker,Arial,{label_fontsize},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,4,0,1,5,0,2,60,60,{label_margin},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    pop = r"\fscx85\fscy85\t(0,90,\fscx112\fscy112)\t(90,170,\fscx100\fscy100)"
    events = []
    if brand_text:
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(brand_seconds)},Brand,,0,0,0,,{{\\fad(200,300)}}{brand_text.upper()}")
    introduced: set[str] = set()
    for line in lines:
        color = colors.get(line["role"], default_color)
        label = labels.get(line["role"])
        if line["role"] in intro_labels and line["role"] not in introduced:
            label = intro_labels[line["role"]]
            introduced.add(line["role"])
        if label:
            events.append(f"Dialogue: 2,{_ts(line['start'])},{_ts(line['end'])},Speaker,,0,0,{label_margin},,"
                          f"{{\\fad(120,120)\\c{color}}}{label.upper()}")
        for c in _dialogue_chunks(line["text"], line["start"], line["end"], max_chars, words=line.get("words")):
            events.append(
                f"Dialogue: 0,{_ts(c['start'])},{_ts(c['end'])},Default,,0,0,{margin_v},,"
                f"{{{pop}\\c{color}}}{c['text'].upper()}"
            )
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")
    return len(events)
