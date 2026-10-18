"""
Assembly Agent — склеивает клипы + аудио в финальный MP4 через FFmpeg.

Вход:  audio_path (str), platform (str), clips (list[str]), subtitle_path (str),
       music_path (str)
Выход: {"video_path": "...", "platform": "...", "resolution": "...", "duration": float,
        "background": "clips"|"black"}

Эффекты:
  - Ken Burns: анимированный zoom/pan на каждом клипе
  - Crossfade: плавный переход 0.3 сек между сегментами
  - Gradient overlay: полупрозрачный градиент снизу кадра под субтитры
  - Background music: голос 100% + музыка 15% через amix
"""

import hashlib
import json
import logging
import math
import os
import random
import shutil
import subprocess
import uuid
from datetime import datetime
from typing import Literal

logger = logging.getLogger(__name__)

Platform = Literal["tiktok", "youtube", "instagram", "linkedin", "twitter"]

PLATFORM_RESOLUTION = {
    "tiktok":    (1080, 1920),
    "instagram": (1080, 1920),
    "youtube":   (1080, 1920),  # Shorts
    "linkedin":  (1080, 1920),
    "twitter":   (1080, 1920),
}

_SUBTITLE_STYLE = (
    "FontName=Arial,"
    "FontSize=48,"
    "PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,"
    "BorderStyle=1,"
    "Outline=3,"
    "Shadow=1,"
    "Alignment=2,"
    "MarginV=60"
)

# --- Viral pacing ---
SEG_SIZE = 2.0        # средняя длина сегмента (TikTok ~2-2.3 сек)
HOOK_DUR = 1.3        # длительность hook-кадра (первый, с зум-бёрстом)
MIN_SEG_DUR = 1.2     # не короче (иначе субтитры/zoompan дёргаются)

# Cinematic color grade (slight saturation + contrast boost) + лёгкая виньетка
# Без градиента — он только глушит картинку.
_COLOR_GRADE = "eq=saturation=1.18:contrast=1.06:brightness=0.02:gamma=0.98"
_VIGNETTE = "vignette=PI/6"  # ослабленная виньетка вместо PI/4

# Типы переходов и их веса: hard cut доминирует, иногда punch/whip/flash
_TRANSITIONS = [
    ("fade",       0.05, 0.60),   # near hard cut
    ("smoothleft", 0.18, 0.10),   # whip pan
    ("smoothright",0.18, 0.10),   # whip pan
    ("circleopen", 0.20, 0.05),   # punch-in
    ("fadewhite",  0.12, 0.05),   # flash
    ("slideup",    0.18, 0.05),   # vertical slide
    ("dissolve",   0.20, 0.05),   # dissolve
]

_KB_EFFECTS = ["pan_right", "pan_left", "pan_down", "pan_up",
               "center", "zoom_out", "zoom_in_hard"]

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "video")


def _check_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise EnvironmentError(
            "ffmpeg/ffprobe не найден. Установи FFmpeg: https://ffmpeg.org/download.html "
            "и добавь в PATH."
        )


def _run_ffmpeg(cmd: list[str], timeout: float = 600, what: str = "ffmpeg"):
    from utils.ffmpeg import run_ffmpeg
    return run_ffmpeg(cmd, timeout=timeout, what=what)


def _get_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True, timeout=60,
    )
    data = json.loads(result.stdout)
    # format-level duration — надёжнее всего, есть почти всегда
    fmt_dur = data.get("format", {}).get("duration")
    if fmt_dur:
        return float(fmt_dur)
    # fallback: duration из первого аудио/видео стрима
    for stream in data.get("streams", []):
        if stream.get("codec_type") in ("audio", "video"):
            d = stream.get("duration")
            if d:
                return float(d)
    raise RuntimeError(f"Не удалось определить длительность: {path}")


def _segment_durations(n: int, total: float) -> list[float]:
    """Hook (1.3 сек) + остальные равномерно. Не короче MIN_SEG_DUR."""
    if n <= 1:
        return [total]
    hook = min(HOOK_DUR, total * 0.25)
    rest = total - hook
    base = rest / (n - 1)
    if base < MIN_SEG_DUR:
        # пересчёт без hook-оптимизации, если видео слишком короткое
        base = total / n
        return [base] * (n - 1) + [total - base * (n - 1)]
    durs = [hook] + [base] * (n - 1)
    durs[-1] = total - hook - base * (n - 2)
    return durs


def _pick_transitions(n_gaps: int) -> list[tuple[str, float]]:
    """Случайные переходы по весам из _TRANSITIONS."""
    names = [t[0] for t in _TRANSITIONS]
    durs = {t[0]: t[1] for t in _TRANSITIONS}
    weights = [t[2] for t in _TRANSITIONS]
    picks = random.choices(names, weights=weights, k=n_gaps)
    return [(name, durs[name]) for name in picks]


def _esc(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:")


def _ken_burns_filter(width: int, height: int, seg_d: float, is_hook: bool = False) -> str:
    """
    FFmpeg zoompan Ken Burns с агрессивным zoom-диапазоном.
    is_hook=True → лёгкий "punch in" zoom-burst для hook-кадра.
    """
    frames = max(int(seg_d * 30), 2)

    if is_hook:
        zoom_end = 1.22
        effect = "zoom_in_hard"
    else:
        zoom_end = round(random.uniform(1.12, 1.28), 4)
        effect = random.choice(_KB_EFFECTS)

    inc = round((zoom_end - 1.0) / frames, 8)
    base_scale = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}"
    )

    if effect == "pan_right":
        x = f"(iw-iw/zoom)*on/{frames}"
        y = "ih/2-(ih/zoom/2)"
    elif effect == "pan_left":
        x = f"iw-iw/zoom-(iw-iw/zoom)*on/{frames}"
        y = "ih/2-(ih/zoom/2)"
    elif effect == "pan_down":
        x = "iw/2-(iw/zoom/2)"
        y = f"(ih-ih/zoom)*on/{frames}"
    elif effect == "pan_up":
        x = "iw/2-(iw/zoom/2)"
        y = f"ih-ih/zoom-(ih-ih/zoom)*on/{frames}"
    elif effect == "zoom_out":
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
        return (
            f"{base_scale},"
            f"zoompan=z='max({zoom_end}-{inc:.8f}*on,1.0)'"
            f":x='{x}':y='{y}':d=1:s={width}x{height}:fps=30"
        )
    else:  # center / zoom_in_hard
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"

    return (
        f"{base_scale},"
        f"zoompan=z='min(1+{inc:.8f}*on,{zoom_end})'"
        f":x='{x}':y='{y}':d=1:s={width}x{height}:fps=30"
    )


def _xfade_chain(n: int, seg_durs: list[float], transitions: list[tuple[str, float]]) -> list[str]:
    """Строит список xfade-фильтров с переменными типами и длительностями переходов."""
    if n <= 1:
        return []

    filters: list[str] = []
    cum = seg_durs[0]

    for i in range(1, n):
        trans_name, trans_dur = transitions[i - 1]
        in_a = "v0" if i == 1 else f"vx{i - 1}"
        in_b = f"v{i}"
        out = "vcat_raw" if i == n - 1 else f"vx{i}"
        offset = round(max(cum - trans_dur, 0.0), 4)
        filters.append(
            f"[{in_a}][{in_b}]xfade=transition={trans_name}"
            f":duration={trans_dur}:offset={offset}[{out}]"
        )
        cum += seg_durs[i] - trans_dur

    return filters


def _align_segments_to_beats(
    seg_durs: list[float],
    music_path: str | None,
    total_dur: float,
    tolerance: float = 0.4,
) -> list[float]:
    """
    Выравнивает границы сегментов к ближайшим beat'ам музыки (если librosa установлен).
    Каждая внутренняя граница смещается к ближайшему beat в окне ±tolerance.

    При недоступности librosa или ошибке beat detection — возвращает seg_durs без изменений.
    """
    if not music_path or len(seg_durs) < 3:
        return seg_durs
    try:
        import librosa
    except ImportError:
        logger.debug("Beat-sync: librosa не установлен — равномерный pacing")
        return seg_durs

    try:
        # Загружаем только нужный отрезок музыки
        y, sr = librosa.load(music_path, duration=total_dur + 2.0)
        _, beats = librosa.beat.beat_track(y=y, sr=sr)
        beat_times = librosa.frames_to_time(beats, sr=sr).tolist()
    except Exception as exc:
        logger.warning("Beat detection failed: %s — равномерный pacing", exc)
        return seg_durs

    if len(beat_times) < 4:
        return seg_durs

    # Кумулятивные текущие границы
    cum = [0.0]
    for d in seg_durs:
        cum.append(cum[-1] + d)

    aligned = list(cum)
    shifts = 0
    # Двигаем только ВНУТРЕННИЕ границы (не первую и не последнюю)
    for i in range(1, len(aligned) - 1):
        target = aligned[i]
        nearest = min(beat_times, key=lambda b: abs(b - target))
        diff = abs(nearest - target)
        if diff > tolerance:
            continue
        # Не сдвигаем назад от предыдущей границы (минимум MIN_SEG_DUR между ними)
        min_allowed = aligned[i - 1] + MIN_SEG_DUR
        max_allowed = aligned[i + 1] - MIN_SEG_DUR
        if min_allowed <= nearest <= max_allowed:
            aligned[i] = nearest
            shifts += 1

    # Восстанавливаем seg_durs из обновлённых границ
    new_durs = [aligned[i] - aligned[i - 1] for i in range(1, len(aligned))]
    # Финальный сегмент добирает остаток до total_dur
    new_durs[-1] = total_dur - sum(new_durs[:-1])

    if shifts:
        logger.info("Beat-sync: выровнено %d/%d границ к ударам", shifts, len(seg_durs) - 1)
    return new_durs


def _shuffle_no_adjacent(clips: list[str], n: int) -> list[str]:
    """Возвращает n клипов так, чтобы один и тот же не шёл подряд."""
    if len(clips) <= 1:
        return [clips[0]] * n if clips else []
    result: list[str] = []
    pool = list(clips)
    random.shuffle(pool)
    idx = 0
    for _ in range(n):
        # пытаемся не повторить предыдущий
        attempt = 0
        while attempt < len(pool):
            cand = pool[idx % len(pool)]
            idx += 1
            attempt += 1
            if not result or cand != result[-1]:
                result.append(cand)
                break
        else:
            result.append(pool[idx % len(pool)])
            idx += 1
    return result


def _build_audio_filters(
    filter_parts: list[str],
    voice_idx: int,
    music_path: str | None,
    input_args: list[str],
    sfx_events: list[tuple[str, float]] | None = None,
) -> tuple[list[str], str]:
    """
    Голос: loudnorm к TikTok-стандарту (-16 LUFS).
    С музыкой: side-chain compression — музыка автоматически приглушается под голос.
    С SFX: SFX-треки подмешиваются поверх с offset через adelay.
    """
    # Голос нормализуем по громкости — кончится одинаково "громко" вне зависимости от TTS
    voice_chain = f"[{voice_idx}:a]loudnorm=I=-16:LRA=11:tp=-1.5,volume=1.0"

    if music_path:
        music_idx = voice_idx + 1
        input_args = input_args + ["-stream_loop", "-1", "-i", music_path]
        # Сплитим голос: одна копия в mix, вторая — sidechain-сигнал
        filter_parts.append(f"{voice_chain},asplit=2[voice_main][voice_sc]")
        filter_parts.append(f"[{music_idx}:a]volume=0.28[music_raw]")
        filter_parts.append(
            "[music_raw][voice_sc]sidechaincompress="
            "threshold=0.04:ratio=10:attack=5:release=350:makeup=1[music_ducked]"
        )
        filter_parts.append(
            "[voice_main][music_ducked]amix=inputs=2:duration=first:normalize=0[a_voice_music]"
        )
        base_label = "[a_voice_music]"
        next_idx = music_idx + 1
    else:
        filter_parts.append(f"{voice_chain}[a_voice_music]")
        base_label = "[a_voice_music]"
        next_idx = voice_idx + 1

    # SFX overlay (если есть события)
    if sfx_events:
        sfx_labels: list[str] = []
        for j, (sfx_path, offset_sec) in enumerate(sfx_events):
            input_args = input_args + ["-i", sfx_path]
            offset_ms = max(0, int(offset_sec * 1000))
            idx = next_idx + j
            filter_parts.append(
                f"[{idx}:a]adelay={offset_ms}|{offset_ms},apad[sfx{j}]"
            )
            sfx_labels.append(f"[sfx{j}]")

        # Всё SFX в один трек
        if len(sfx_labels) == 1:
            sfx_mix = sfx_labels[0]
        else:
            mix_inputs = "".join(sfx_labels)
            filter_parts.append(
                f"{mix_inputs}amix=inputs={len(sfx_labels)}:duration=longest:normalize=0[sfx_mix]"
            )
            sfx_mix = "[sfx_mix]"

        # Финальный mix: голос+музыка + SFX
        filter_parts.append(
            f"{base_label}{sfx_mix}amix=inputs=2:duration=first:normalize=0[a_out]"
        )
        return input_args, "[a_out]"

    # Без SFX — отдаём base_label напрямую
    return input_args, base_label


PHOTO_CACHE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "output", "clips", "photo_cache"
)
SFX_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "sfx")


def _ensure_sfx_library() -> dict[str, str]:
    """
    Процедурно генерирует библиотеку SFX через ffmpeg lavfi если их нет.
    Возвращает {name: path}.

    SFX:
      whoosh — лёгкий шум-пасс для whip-pan и slide transitions
      bass   — sub-bass импульс для flash (fadewhite) переходов
      click  — короткий тон для zoom-punch (circleopen)
    """
    os.makedirs(SFX_DIR, exist_ok=True)
    sfx_specs = {
        # name: (lavfi source, audio filter chain, duration)
        "whoosh": (
            "anoisesrc=d=0.4:c=brown:r=48000",
            "bandpass=f=700:w=1400,afade=t=in:d=0.05,afade=t=out:d=0.2,volume=0.45",
            0.40,
        ),
        "bass": (
            "sine=f=65:d=0.5:r=48000",
            "afade=t=in:d=0.03,afade=t=out:d=0.35,lowpass=f=180,volume=0.65",
            0.50,
        ),
        "click": (
            "sine=f=1600:d=0.06:r=48000",
            "afade=t=in:d=0.005,afade=t=out:d=0.05,volume=0.3",
            0.06,
        ),
    }
    paths: dict[str, str] = {}
    for name, (src, af, dur) in sfx_specs.items():
        path = os.path.abspath(os.path.join(SFX_DIR, f"{name}.wav"))
        if not os.path.exists(path) or os.path.getsize(path) < 1000:
            try:
                _run_ffmpeg(
                    [
                        "ffmpeg", "-y", "-f", "lavfi", "-i", src,
                        "-af", af, "-t", f"{dur:.2f}",
                        "-c:a", "pcm_s16le", path,
                    ],
                    timeout=60,
                )
            except RuntimeError as exc:
                logger.warning("SFX %s gen failed: %s", name, exc)
                continue
        paths[name] = path
    return paths


# Каким transition'ам какой SFX накладывать
_SFX_FOR_TRANSITION: dict[str, str | None] = {
    "fade":         None,      # near-hard-cut — частый, без SFX
    "smoothleft":   "whoosh",
    "smoothright":  "whoosh",
    "slideup":      "whoosh",
    "circleopen":   "click",
    "fadewhite":    "bass",    # flash → bass-drop
    "dissolve":     None,      # мягкий, без SFX
}


def _compute_sfx_events(
    transitions: list[tuple[str, float]],
    seg_durs: list[float],
    sfx_paths: dict[str, str],
) -> list[tuple[str, float]]:
    """Возвращает список (sfx_path, offset_sec) для каждого подходящего transition'а."""
    if not transitions or not sfx_paths:
        return []
    cum: list[float] = [0.0]
    for d in seg_durs:
        cum.append(cum[-1] + d)

    events: list[tuple[str, float]] = []
    for i, (trans_name, trans_dur) in enumerate(transitions):
        sfx_name = _SFX_FOR_TRANSITION.get(trans_name)
        if not sfx_name or sfx_name not in sfx_paths:
            continue
        # Запускаем SFX чуть раньше визуального cut'а (на половину transition_dur)
        offset_sec = max(0.0, cum[i + 1] - trans_dur)
        events.append((sfx_paths[sfx_name], round(offset_sec, 3)))
    return events


def _wrap_hook_text(text: str, font=None, max_px: int = 960) -> list[str]:
    """Разбивает hook на строки по ширине в пикселях (если задан font).
    Без font — по символам (char-fallback). Максимум 5 строк."""
    text = (text or "").strip().strip('"').strip("«»")
    text = text.replace("—", " ").replace("–", " ").replace("\n", " ")
    text = " ".join(text.split()).upper()

    words = text.split()
    lines: list[str] = []
    current = ""

    def _line_w(s: str) -> float:
        if font is None:
            return len(s) * 60  # грубая оценка: 60px/символ при fontsize~100
        try:
            return font.getlength(s)
        except AttributeError:
            # Старые версии Pillow используют getsize
            return font.getsize(s)[0]

    for w in words:
        candidate = (current + " " + w).strip() if current else w
        if _line_w(candidate) > max_px:
            if current:
                lines.append(current)
            current = w
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines[:5]


def _render_hook_text_image(
    hook_text: str, width: int, height: int, png_path: str
) -> None:
    """Рендерит PNG с большим bold-текстом на тёмном фоне (Pillow)."""
    from PIL import Image, ImageDraw, ImageFont

    font_file = None
    for fp in ["C:/Windows/Fonts/impact.ttf", "C:/Windows/Fonts/ariblk.ttf",
               "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"]:
        if os.path.exists(fp):
            font_file = fp
            break

    def _load_font(size: int):
        if font_file:
            try:
                return ImageFont.truetype(font_file, size)
            except Exception:
                pass
        return ImageFont.load_default()

    # At HOOK_ZOOM=1.22 the zoompan crops ~width*(1-1/1.22)/2 ≈ 97px per side.
    # We must keep text inside the visible crop zone + 20px safety.
    _HOOK_ZOOM = 1.22
    zoom_crop = int(width * (1 - 1 / _HOOK_ZOOM) / 2) + 20  # ≈ 117px per side
    max_allowed_w = width - 2 * zoom_crop  # ≈ 846px

    # Phase 1: wrap with a neutral fontsize to determine line count
    font = _load_font(120)
    lines = _wrap_hook_text(hook_text, font=font, max_px=max_allowed_w)
    if not lines:
        lines = ["..."]

    # Phase 2: choose fontsize based on line count
    if len(lines) <= 2:
        fontsize = 150
    elif len(lines) == 3:
        fontsize = 120
    elif len(lines) == 4:
        fontsize = 100
    else:
        fontsize = 84

    font = _load_font(fontsize)
    # Re-wrap with the actual fontsize for accurate pixel measurement
    lines = _wrap_hook_text(hook_text, font=font, max_px=max_allowed_w)
    if not lines:
        lines = ["..."]

    # Phase 3: safety shrink — if any line still overflows, reduce fontsize and re-wrap
    for _ in range(15):
        max_line_w = max(font.getbbox(l)[2] - font.getbbox(l)[0] for l in lines)
        if max_line_w <= max_allowed_w or fontsize <= 56:
            break
        fontsize -= 6
        font = _load_font(fontsize)
        lines = _wrap_hook_text(hook_text, font=font, max_px=max_allowed_w)

    # Тёмный фон с градиентом top→bottom (numpy быстрее Python-цикла в 100x)
    try:
        import numpy as np
        r_chan = (10 + np.linspace(0, 5, height)).astype(np.uint8)
        g_chan = (25 - np.linspace(0, 15, height)).astype(np.uint8)
        b_chan = (41 - np.linspace(0, 25, height)).astype(np.uint8)
        arr = np.stack([
            np.tile(r_chan[:, None], (1, width)),
            np.tile(g_chan[:, None], (1, width)),
            np.tile(b_chan[:, None], (1, width)),
        ], axis=2).astype(np.uint8)
        img = Image.fromarray(arr, "RGB")
    except ImportError:
        img = Image.new("RGB", (width, height), (10, 25, 41))
        draw_tmp = ImageDraw.Draw(img)
        for y_i in range(height):
            ratio = y_i / height
            draw_tmp.line([(0, y_i), (width, y_i)],
                          fill=(int(10 + ratio*5), int(25 - ratio*15), int(41 - ratio*25)))
    draw = ImageDraw.Draw(img)

    # Вычисляем общую высоту блока текста
    bbox_h = font.getbbox("Тg")
    line_h = (bbox_h[3] - bbox_h[1]) + 24
    total_h = line_h * len(lines)
    # Размещаем в верхней трети — субтитры пайплайна сидят на margin_v=760
    # (y ≈ 1160), поэтому начало текста на 18% сверху даёт хороший зазор.
    y0 = max(int(height * 0.18), (height - total_h) // 3)

    outline_w = 7
    for i, line in enumerate(lines):
        bbox = font.getbbox(line)
        text_w = bbox[2] - bbox[0]
        x = (width - text_w) // 2
        y = y0 + i * line_h

        # Жирная чёрная обводка — рисуем сдвинутыми копиями
        for dx in range(-outline_w, outline_w + 1, 2):
            for dy in range(-outline_w, outline_w + 1, 2):
                if dx == 0 and dy == 0:
                    continue
                draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))

        # Цвет: жёлтый если содержит цифру/процент, иначе белый
        has_digit = any(c.isdigit() or c == "%" for c in line)
        color = (255, 220, 0) if has_digit else (255, 255, 255)
        draw.text((x, y), line, font=font, fill=color)

    img.save(png_path, "PNG", optimize=True)


def _render_endcard_image(
    width: int, height: int, png_path: str,
    main_text: str = "ПОДПИШИСЬ", accent_text: str = "ЕСЛИ ЗАЙДЁТ",
    arrow: str = "↑",
) -> None:
    """Рендерит end-screen карточку: большой основной текст + жёлтый акцент + стрелка."""
    from PIL import Image, ImageDraw, ImageFont

    # Тёмный фон с градиентом (numpy быстрее Python-цикла в 100x)
    try:
        import numpy as np
        r_chan = (10 + np.linspace(0, 5, height)).astype(np.uint8)
        g_chan = (25 - np.linspace(0, 15, height)).astype(np.uint8)
        b_chan = (41 - np.linspace(0, 25, height)).astype(np.uint8)
        arr = np.stack([
            np.tile(r_chan[:, None], (1, width)),
            np.tile(g_chan[:, None], (1, width)),
            np.tile(b_chan[:, None], (1, width)),
        ], axis=2).astype(np.uint8)
        img = Image.fromarray(arr, "RGB")
    except ImportError:
        img = Image.new("RGB", (width, height), (10, 25, 41))
        draw_tmp = ImageDraw.Draw(img)
        for y_i in range(height):
            ratio = y_i / height
            draw_tmp.line([(0, y_i), (width, y_i)],
                          fill=(int(10 + ratio*5), int(25 - ratio*15), int(41 - ratio*25)))
    draw = ImageDraw.Draw(img)

    font_paths_main = [
        ("C:/Windows/Fonts/impact.ttf", 180),
        ("C:/Windows/Fonts/ariblk.ttf", 160),
        ("C:/Windows/Fonts/arialbd.ttf", 150),
    ]
    font_paths_sub = [
        ("C:/Windows/Fonts/impact.ttf", 90),
        ("C:/Windows/Fonts/ariblk.ttf", 80),
        ("C:/Windows/Fonts/arialbd.ttf", 80),
    ]

    def _load(paths):
        for fp, size in paths:
            if os.path.exists(fp):
                try:
                    return ImageFont.truetype(fp, size)
                except Exception:
                    continue
        return ImageFont.load_default()

    font_main = _load(font_paths_main)
    font_sub  = _load(font_paths_sub)

    outline_w = 8

    def _draw_centered(text: str, font, y: int, color):
        bbox = font.getbbox(text)
        text_w = bbox[2] - bbox[0]
        x = (width - text_w) // 2
        # Outline
        for dx in range(-outline_w, outline_w + 1, 2):
            for dy in range(-outline_w, outline_w + 1, 2):
                if dx == 0 and dy == 0:
                    continue
                draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0))
        draw.text((x, y), text, font=font, fill=color)

    # Layout: main text сверху ~30% от верха, под ним стрелка, ниже жёлтый sub
    main_bbox = font_main.getbbox(main_text)
    main_h = main_bbox[3] - main_bbox[1]
    arrow_bbox = font_main.getbbox(arrow)
    arrow_h = arrow_bbox[3] - arrow_bbox[1]
    sub_bbox = font_sub.getbbox(accent_text)
    sub_h = sub_bbox[3] - sub_bbox[1]

    y_main = int(height * 0.28)
    y_arrow = y_main + main_h + 40
    y_sub = y_arrow + arrow_h + 40

    _draw_centered(main_text, font_main, y_main, (255, 255, 255))
    _draw_centered(arrow, font_main, y_arrow, (255, 220, 0))
    _draw_centered(accent_text, font_sub, y_sub, (255, 220, 0))

    img.save(png_path, "PNG", optimize=True)


def _render_endcard_clip(duration: float, width: int, height: int) -> str:
    """
    End-screen card на последний сегмент: «ПОДПИШИСЬ ↑ ЕСЛИ ЗАЙДЁТ».
    Slide-up анимация (текст въезжает снизу за 0.4 сек).
    """
    os.makedirs(PHOTO_CACHE_DIR, exist_ok=True)
    sig = hashlib.md5(
        f"endcard|{duration:.2f}|{width}x{height}|v2".encode()
    ).hexdigest()[:10]
    png_out = os.path.abspath(os.path.join(PHOTO_CACHE_DIR, f"endcard_{sig}.png"))
    mp4_out = os.path.abspath(os.path.join(PHOTO_CACHE_DIR, f"endcard_{sig}.mp4"))
    if os.path.exists(mp4_out) and os.path.getsize(mp4_out) > 30_000:
        return mp4_out

    _render_endcard_image(width, height, png_out)

    frames = max(int(duration * 30), 30)
    slide_frames = max(int(frames * 0.30), 5)
    # Slide-up: изображение в самом начале сдвинуто вниз на 100px, потом съезжает в позицию
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", png_out,
        "-vf", (
            f"zoompan=z='1.0'"
            f":x='iw/2-(iw/zoom/2)'"
            f":y='ih/2-(ih/zoom/2)+if(lt(on,{slide_frames}),(({slide_frames}-on)/{slide_frames})*80,0)'"
            f":d=1:s={width}x{height}:fps=30,"
            f"vignette=PI/8"
        ),
        "-t", f"{duration:.4f}",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-an", mp4_out,
    ]
    _run_ffmpeg(cmd)
    return mp4_out


def _render_hook_clip(
    hook_text: str, duration: float, width: int, height: int
) -> str:
    """
    Рендерит первый сегмент видео — big-text hook overlay с zoom-burst.
    Возвращает путь к закешированному MP4.
    """
    os.makedirs(PHOTO_CACHE_DIR, exist_ok=True)
    sig = hashlib.md5(
        f"hook|{hook_text}|{duration:.2f}|{width}x{height}|v4".encode()
    ).hexdigest()[:10]
    png_out = os.path.abspath(os.path.join(PHOTO_CACHE_DIR, f"hook_text_{sig}.png"))
    mp4_out = os.path.abspath(os.path.join(PHOTO_CACHE_DIR, f"hook_{sig}.mp4"))
    if os.path.exists(mp4_out) and os.path.getsize(mp4_out) > 30_000:
        return mp4_out

    _render_hook_text_image(hook_text, width, height, png_out)

    frames = max(int(duration * 30), 30)
    burst_frames = max(int(frames * 0.40), 5)
    # Zoom 1.0 → 1.22 за первые 40% длительности — "punch in" эффект.
    # Важно: z должен быть >= 1.0 иначе zoompan даёт чёрные рамки.
    inc = round(0.22 / burst_frames, 8)
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", png_out,
        "-vf", (
            f"zoompan="
            f"z='if(lt(on,{burst_frames}),min(1.0+{inc}*on,1.22),1.22)'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={width}x{height}:fps=30,"
            f"vignette=PI/8"
        ),
        "-t", f"{duration:.4f}",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-an", mp4_out,
    ]
    _run_ffmpeg(cmd)
    return mp4_out


# Cinematic pre-grade для фото: лёгкий контраст-буст + увеличенная насыщенность.
# Применяется в _render_photo_clip ДО общего grade в основном render.
# Идея: stock-фото обычно "плоские" по сравнению со стоковым видео — этот
# pre-grade подтягивает их к "филmic" стилю клипов, и цвет/яркость не
# прыгают при склейке фото↔видео.
_PHOTO_PREGRADE = (
    "eq=saturation=1.12:contrast=1.08,"
    "curves=preset=increase_contrast"
)


def _render_captioned_photo_clip(
    photo_path: str, caption: str, duration: float, width: int, height: int
) -> str:
    """
    Превращает фото в MP4 с lower-third badge внизу:
      - Полупрозрачная тёмная полоса (180px)
      - Тонкая жёлтая полоска слева (8px)
      - Caption text белым Impact 56pt
    """
    os.makedirs(PHOTO_CACHE_DIR, exist_ok=True)
    sig = hashlib.md5(
        f"capped|{photo_path}|{caption}|{duration:.2f}|{width}x{height}|v1".encode()
    ).hexdigest()[:10]
    png_out = os.path.abspath(os.path.join(PHOTO_CACHE_DIR, f"capped_{sig}.png"))
    mp4_out = os.path.abspath(os.path.join(PHOTO_CACHE_DIR, f"capped_{sig}.mp4"))
    if os.path.exists(mp4_out) and os.path.getsize(mp4_out) > 30_000:
        return mp4_out

    # Step 1: Композиция фото + lower-third через Pillow
    try:
        from PIL import Image, ImageDraw, ImageFont
        # Load and fit photo into width x height (cover crop)
        with Image.open(photo_path) as src:
            src = src.convert("RGB")
            sw, sh = src.size
            scale = max(width / sw, height / sh)
            new_w, new_h = int(sw * scale), int(sh * scale)
            src = src.resize((new_w, new_h), Image.LANCZOS)
            # Center crop
            left = (new_w - width) // 2
            top = (new_h - height) // 2
            img = src.crop((left, top, left + width, top + height))

        # Lower-third strip (bottom 180px)
        strip_h = 180
        strip = Image.new("RGBA", (width, strip_h), (10, 18, 30, 200))
        img.paste(strip, (0, height - strip_h - 100), strip)

        draw = ImageDraw.Draw(img)
        # Жёлтая вертикальная полоска слева strip'а
        bar_x = 40
        bar_top = height - strip_h - 100 + 30
        bar_bot = height - 100 - 30
        draw.rectangle([bar_x, bar_top, bar_x + 8, bar_bot], fill=(255, 220, 0))

        # Caption text
        font_paths = [
            ("C:/Windows/Fonts/impact.ttf", 56),
            ("C:/Windows/Fonts/ariblk.ttf", 50),
            ("C:/Windows/Fonts/arialbd.ttf", 50),
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

        text_x = bar_x + 35
        text_y = height - strip_h - 100 + (strip_h - 56) // 2
        # Лёгкая чёрная обводка (2px)
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            draw.text((text_x + dx, text_y + dy), caption, font=font, fill=(0, 0, 0))
        draw.text((text_x, text_y), caption, font=font, fill=(255, 255, 255))

        img.convert("RGB").save(png_out, "PNG", optimize=True)
    except Exception as exc:
        logger.warning("Captioned photo composition failed: %s — fallback uncaptioned", exc)
        return _render_photo_clip(photo_path, duration, width, height)

    # Step 2: PNG → MP4 (статика, Ken Burns придёт в основном render'е)
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", png_out,
        "-vf", (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,"
            f"{_PHOTO_PREGRADE}"
        ),
        "-t", f"{duration:.4f}",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-an", mp4_out,
    ]
    _run_ffmpeg(cmd)
    return mp4_out


def _render_photo_clip(
    photo_path: str, duration: float, width: int, height: int
) -> str:
    """
    Превращает одно фото в статичный MP4 длительности duration.
    Применяет cinematic pre-grade чтобы фото визуально матчилось со стоковыми клипами.
    Сам Ken Burns применяется уже в основном render (общая логика сегментов).
    Кешируется по (photo + duration + resolution).
    """
    os.makedirs(PHOTO_CACHE_DIR, exist_ok=True)
    sig = hashlib.md5(
        f"{photo_path}|{duration:.2f}|{width}x{height}|v2".encode()
    ).hexdigest()[:10]
    out = os.path.abspath(os.path.join(PHOTO_CACHE_DIR, f"photo_{sig}.mp4"))
    if os.path.exists(out) and os.path.getsize(out) > 30_000:
        return out
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", photo_path,
        "-vf", (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,"
            f"{_PHOTO_PREGRADE}"
        ),
        "-t", f"{duration:.4f}",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-an", out,
    ]
    _run_ffmpeg(cmd)
    return out


def _render_burst_clip(
    photo_paths: list[str], duration: float, width: int, height: int
) -> str:
    """
    Склеивает N фото в один MP4 с быстрыми хард-катами (каждое фото duration/N сек).
    Лёгкий jitter в crop'е — даёт ощущение живой пачки.
    """
    if not photo_paths:
        raise ValueError("photo_paths пустой")
    os.makedirs(PHOTO_CACHE_DIR, exist_ok=True)
    n = len(photo_paths)
    per_dur = duration / n
    sig = hashlib.md5(
        ("|".join(photo_paths) + f"|{duration:.2f}|{width}x{height}").encode()
    ).hexdigest()[:10]
    out = os.path.abspath(os.path.join(PHOTO_CACHE_DIR, f"burst_{sig}.mp4"))
    if os.path.exists(out) and os.path.getsize(out) > 30_000:
        return out

    inputs: list[str] = []
    filter_parts: list[str] = []
    for i, p in enumerate(photo_paths):
        inputs += [
            "-loop", "1", "-framerate", "30", "-t", f"{per_dur:.4f}", "-i", p
        ]
        # Лёгкий random zoom для каждой фотки — 1.0 → 1.10
        zoom = round(random.uniform(1.04, 1.12), 3)
        filter_parts.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,"
            f"{_PHOTO_PREGRADE},"
            f"zoompan=z='min(1+{(zoom - 1) / max(int(per_dur * 30), 2):.6f}*on,{zoom})'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={width}x{height}:fps=30,"
            f"trim=duration={per_dur:.4f},setpts=PTS-STARTPTS[v{i}]"
        )

    concat_inputs = "".join(f"[v{i}]" for i in range(n))
    filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[vout]")

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]",
        "-t", f"{duration:.4f}",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-an", out,
    ]
    _run_ffmpeg(cmd)
    return out


def _apply_photo_overrides(
    seg_clips: list[str],
    seg_durs: list[float],
    width: int,
    height: int,
    entity_overlays: list[dict] | None,
    photo_burst: dict | None,
    photo_pool: list[str] | None = None,
    hook_text: str | None = None,
    end_card: bool = True,
) -> tuple[list[str], list[str]]:
    """
    Подменяет сегменты на фото/burst по приоритету:
      1. Entity overlays (фото классиков/институтов) — фиксированный таймкод.
      2. Photo burst (одна пачка) — посередине видео.
      3. 50/50 weave из photo_pool — чётные слоты (кроме hook) становятся фото,
         нечётные остаются клипами. Если photo_pool пустой — все остаются клипами.

    Возвращает (updated seg_clips, seg_kinds):
      "clip"  — обычный видео-клип (random offset, Ken Burns)
      "photo" — фото-MP4 (offset=0, Ken Burns)
      "burst" — burst-MP4 (offset=0, без Ken Burns поверх)
    """
    n = len(seg_clips)
    seg_kinds: list[str] = ["clip"] * n
    if n == 0:
        return seg_clips, seg_kinds

    # Кумулятивные времена начала каждого сегмента
    cum: list[float] = [0.0]
    for d in seg_durs[:-1]:
        cum.append(cum[-1] + d)

    def _slot_at(t: float) -> int:
        for i in range(n):
            if cum[i] <= t < cum[i] + seg_durs[i]:
                return i
        return n - 1

    used: set[int] = set()

    # 0. Hook big-text overlay — самый высокий приоритет, slot 0
    if hook_text and n >= 1:
        try:
            hook_mp4 = _render_hook_clip(hook_text, seg_durs[0], width, height)
            seg_clips[0] = hook_mp4
            seg_kinds[0] = "hook"
            used.add(0)
            logger.info("Hook overlay (big text) → slot 0, dur=%.2fs", seg_durs[0])
        except Exception as exc:
            logger.warning("Hook overlay render failed: %s", exc)

    # 0a. End-screen card — последний слот (если включён и видео >3 сегментов)
    last_slot = n - 1
    if end_card and n >= 3 and last_slot not in used:
        try:
            endcard_mp4 = _render_endcard_clip(seg_durs[last_slot], width, height)
            seg_clips[last_slot] = endcard_mp4
            seg_kinds[last_slot] = "hook"  # рендерим как hook — plain scale без Ken Burns
            used.add(last_slot)
            logger.info("End-card → slot %d, dur=%.2fs", last_slot, seg_durs[last_slot])
        except Exception as exc:
            logger.warning("End-card render failed: %s", exc)

    # 1. Entity overlays — фото классиков/институтов с lower-third badge
    for ov in entity_overlays or []:
        slot = _slot_at(ov.get("start_sec", 0))
        # Не трогаем hook-кадр (slot 0) — он критичен для удержания
        if slot == 0 or slot in used:
            continue
        try:
            caption = ov.get("caption")
            if caption:
                photo_mp4 = _render_captioned_photo_clip(
                    ov["photo_path"], caption, seg_durs[slot], width, height,
                )
            else:
                photo_mp4 = _render_photo_clip(
                    ov["photo_path"], seg_durs[slot], width, height
                )
            seg_clips[slot] = photo_mp4
            seg_kinds[slot] = "photo"
            used.add(slot)
            logger.info(
                "Entity overlay slot=%d for '%s' (caption=%s) @ %.1fs",
                slot, ov.get("name", "?"), bool(caption), ov.get("start_sec", 0),
            )
        except Exception as exc:
            logger.warning("Entity overlay render failed: %s", exc)

    # 2. Photo burst — пачка фото в середине
    if photo_burst and photo_burst.get("photo_paths"):
        start = photo_burst.get("start_sec")
        if start is None:
            total = sum(seg_durs)
            start = total * 0.5
        slot = _slot_at(start)
        if slot != 0 and slot not in used:
            try:
                burst_mp4 = _render_burst_clip(
                    photo_burst["photo_paths"], seg_durs[slot], width, height
                )
                seg_clips[slot] = burst_mp4
                seg_kinds[slot] = "burst"
                used.add(slot)
                logger.info(
                    "Photo burst slot=%d @ %.1fs (%d фото)",
                    slot, start, len(photo_burst["photo_paths"]),
                )
            except Exception as exc:
                logger.warning("Photo burst render failed: %s", exc)

    # 3. 50/50 weave — чётные слоты подменяются фото из pool'а
    if photo_pool:
        pool = list(photo_pool)
        random.shuffle(pool)
        photos_used = 0
        # Идём по чётным слотам начиная с 2 (slot 0 = hook, не трогаем)
        for slot in range(2, n, 2):
            if slot in used or not pool:
                continue
            photo = pool.pop()
            try:
                photo_mp4 = _render_photo_clip(photo, seg_durs[slot], width, height)
                seg_clips[slot] = photo_mp4
                seg_kinds[slot] = "photo"
                used.add(slot)
                photos_used += 1
            except Exception as exc:
                logger.warning("Photo weave slot %d failed: %s", slot, exc)
        # Если ещё остались фото — заполняем нечётные слоты (но не hook)
        for slot in range(1, n, 2):
            if slot in used or not pool:
                continue
            photo = pool.pop()
            try:
                photo_mp4 = _render_photo_clip(photo, seg_durs[slot], width, height)
                seg_clips[slot] = photo_mp4
                seg_kinds[slot] = "photo"
                used.add(slot)
                photos_used += 1
            except Exception as exc:
                logger.warning("Photo weave fill slot %d failed: %s", slot, exc)
        if photos_used:
            logger.info("Photo weave: %d/%d слотов под фото", photos_used, n)

    return seg_clips, seg_kinds


def _delay_subtitle_past_hook(subtitle_path: str, hook_dur: float) -> str:
    """
    Возвращает путь к модифицированному ASS-файлу, где все события,
    стартующие раньше hook_dur сек, сдвинуты на hook_dur.
    Нужно чтобы субтитры не накладывались на big-text hook в первые HOOK_DUR секунд.
    Если файл не читается — возвращает оригинал без изменений.
    """
    def _secs_to_ass(s: float) -> str:
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sc = s % 60
        cs = int(round((sc - int(sc)) * 100))
        return f"{h}:{m:02d}:{int(sc):02d}.{cs:02d}"

    def _ass_to_secs(t: str) -> float:
        try:
            parts = t.split(":")
            h = int(parts[0])
            m = int(parts[1])
            s_cs = parts[2].split(".")
            return h * 3600 + m * 60 + int(s_cs[0]) + (int(s_cs[1]) if len(s_cs) > 1 else 0) / 100
        except Exception:
            return 0.0

    try:
        with open(subtitle_path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
    except Exception:
        return subtitle_path

    modified = False
    out_lines: list[str] = []
    for line in lines:
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) >= 10:
                start_secs = _ass_to_secs(parts[1])
                end_secs = _ass_to_secs(parts[2])
                if start_secs < hook_dur:
                    modified = True
                    if end_secs <= hook_dur:
                        # Event entirely within hook period — skip it
                        continue
                    # Event straddles the hook boundary — shift Start to hook_dur
                    parts[1] = _secs_to_ass(hook_dur)
                    line = ",".join(parts)
        out_lines.append(line)

    if not modified:
        return subtitle_path

    delayed_path = subtitle_path.replace(".ass", "_hooked.ass")
    try:
        with open(delayed_path, "w", encoding="utf-8") as f:
            f.writelines(out_lines)
        return delayed_path
    except Exception:
        return subtitle_path


def _assemble_with_clips(
    clips: list[str],
    audio_path: str,
    width: int,
    height: int,
    duration: float,
    output_path: str,
    subtitle_path: str | None = None,
    music_path: str | None = None,
    entity_overlays: list[dict] | None = None,
    photo_burst: dict | None = None,
    photo_pool: list[str] | None = None,
    hook_text: str | None = None,
    counter_overlays: list[dict] | None = None,
    ordered: bool = False,
) -> None:
    n = max(1, math.ceil(duration / SEG_SIZE))
    if ordered:
        # Нарративный порядок (story mode): слот i получает клип пропорционально
        # позиции в повествовании — без перемешивания
        seg_clips = [clips[min(int(i * len(clips) / n), len(clips) - 1)] for i in range(n)]
    else:
        seg_clips = _shuffle_no_adjacent(clips, n)
    seg_durs = _segment_durations(n, duration)
    # Beat-sync: подгоняем границы сегментов к ударам музыки
    seg_durs = _align_segments_to_beats(seg_durs, music_path, duration)
    transitions = _pick_transitions(max(n - 1, 0))

    # Подмена сегментов на фото-overlay'и, burst и 50/50 weave
    seg_clips, seg_kinds = _apply_photo_overrides(
        seg_clips, seg_durs, width, height,
        entity_overlays, photo_burst, photo_pool, hook_text,
    )

    # Слоты-изображения (story mode передаёт сцены как .jpg/.png) →
    # рендерим в photo-клип с Ken Burns ровно под длительность слота
    _IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
    for i, (path, kind) in enumerate(zip(seg_clips, seg_kinds)):
        if kind == "clip" and path.lower().endswith(_IMG_EXT):
            seg_clips[i] = _render_photo_clip(path, seg_durs[i], width, height)
            seg_kinds[i] = "photo"

    input_args: list[str] = []
    filter_parts: list[str] = []

    # --- Видео: Ken Burns или плоский scale в зависимости от типа сегмента ---
    for i, (clip_path, seg_d, kind) in enumerate(zip(seg_clips, seg_durs, seg_kinds)):
        is_hook = (i == 0)
        if kind == "hook":
            # Hook-MP4 уже содержит zoom-burst и финальный grade — НЕ применяем
            # глобальный color grade повторно (он будет позже)
            offset = 0.0
            input_args += ["-i", clip_path]
            filter_parts.append(
                f"[{i}:v]trim=start=0:duration={seg_d:.4f},"
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps=30,setpts=PTS-STARTPTS[v{i}]"
            )
        elif kind == "burst":
            # Burst-MP4 уже содержит готовую цепочку с зумом — просто масштабируем
            offset = 0.0
            input_args += ["-i", clip_path]
            filter_parts.append(
                f"[{i}:v]trim=start=0:duration={seg_d:.4f},"
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps=30,setpts=PTS-STARTPTS[v{i}]"
            )
        elif kind == "photo":
            # Фото-MP4 (статичный) — даём Ken Burns в основном рендере
            offset = 0.0
            input_args += ["-i", clip_path]
            kb = _ken_burns_filter(width, height, seg_d, is_hook=is_hook)
            filter_parts.append(
                f"[{i}:v]trim=start=0:duration={seg_d:.4f},{kb},"
                f"fps=30,setpts=PTS-STARTPTS[v{i}]"
            )
        else:
            # Обычный видео-клип — случайный offset + Ken Burns
            offset = round(random.uniform(0.0, 4.0), 2)
            input_args += ["-stream_loop", "-1", "-i", clip_path]
            kb = _ken_burns_filter(width, height, seg_d, is_hook=is_hook)
            filter_parts.append(
                f"[{i}:v]trim=start={offset}:duration={seg_d:.4f},{kb},"
                f"fps=30,setpts=PTS-STARTPTS[v{i}]"
            )

    # --- Микс переходов или одиночный сегмент ---
    if n == 1:
        pre_grade = "v0"
    else:
        filter_parts.extend(_xfade_chain(n, seg_durs, transitions))
        pre_grade = "vcat_raw"

    # --- Color grade (saturation + contrast) ---
    filter_parts.append(f"[{pre_grade}]{_COLOR_GRADE}[vgr]")

    # --- Лёгкая виньетка ---
    filter_parts.append(f"[vgr]{_VIGNETTE}[vcat_vig]")

    # --- Субтитры (с задержкой — не перекрывают hook-кадр) ---
    pre_sub_label = "[vcat_vig]"
    if subtitle_path:
        delayed_sub = _delay_subtitle_past_hook(subtitle_path, HOOK_DUR)
        filter_parts.append(
            f"[vcat_vig]subtitles='{_esc(delayed_sub)}'"
            f":force_style='{_SUBTITLE_STYLE}'[vwithsub]"
        )
        pre_sub_label = "[vwithsub]"

    # --- Animated counter overlays via FFmpeg drawtext ---
    # Использует встроенный drawtext вместо дополнительных input-файлов.
    # Анимация: ease-out от 0 до value за 0.8 сек, потом статично до end.
    if counter_overlays:
        current_v = pre_sub_label
        font_path = ""
        for fp in ["C:/Windows/Fonts/impact.ttf", "C:/Windows/Fonts/ariblk.ttf",
                   "C:/Windows/Fonts/arialbd.ttf"]:
            if os.path.exists(fp):
                font_path = fp.replace("\\", "/").replace(":", "\\:")
                break
        try:
            for ci, co in enumerate(counter_overlays):
                val = int(co.get("value", 1))
                suffix = (co.get("suffix") or "").replace(":", "\\:").replace("'", "")
                t0 = float(co.get("start_sec", 0.0))
                dur = float(co.get("duration", 1.2))
                t1 = t0 + dur
                anim_dur = min(0.8, dur)
                next_v = f"[vcounter{ci}]"
                dt = f"(t-{t0:.3f})"
                progress = f"min(1,{dt}/{anim_dur:.3f})"
                cur_val = f"floor({val}*{progress})"
                text_expr = f"%{{eif\\:{cur_val}\\:d}}{suffix}"
                font_arg = f":fontfile='{font_path}'" if font_path else ""
                # fontsize=75 — читаемо, но не конкурирует с субтитрами внизу
                # y=h*0.62 — середина нижней части кадра (выше субтитров)
                filter_parts.append(
                    f"{current_v}drawtext=text='{text_expr}'"
                    f"{font_arg}"
                    f":fontsize=75:fontcolor=yellow@0.90"
                    f":shadowcolor=black@0.8:shadowx=3:shadowy=3"
                    f":x=(w-tw)/2:y=h*0.62"
                    f":enable='between(t\\,{t0:.3f}\\,{t1:.3f})'"
                    f"{next_v}"
                )
                current_v = next_v
            video_map = current_v
            logger.info("Counter overlays: %d drawtext фильтров добавлено", len(counter_overlays))
        except Exception as exc:
            logger.warning("Counter overlay drawtext failed: %s — skipping", exc)
            video_map = pre_sub_label
    else:
        video_map = pre_sub_label

    # --- SFX events: whoosh / bass / click на подходящих переходах ---
    sfx_paths = _ensure_sfx_library()
    sfx_events = _compute_sfx_events(transitions, seg_durs, sfx_paths)
    if sfx_events:
        logger.info("Sound design: %d SFX events", len(sfx_events))

    # --- Аудио: голос (+ музыка опционально) + SFX ---
    voice_idx = n
    input_args += ["-i", audio_path]
    input_args, audio_map = _build_audio_filters(
        filter_parts, voice_idx, music_path, input_args, sfx_events,
    )

    _run_ffmpeg(
        [
            "ffmpeg", "-y",
            *input_args,
            "-filter_complex", ";".join(filter_parts),
            "-map", video_map,
            "-map", audio_map,
            "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
            "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
            "-bf", "2", "-refs", "4", "-g", "60",
            "-c:a", "aac", "-ar", "48000", "-b:a", "192k",
            "-t", str(duration),
            "-movflags", "+faststart",
            output_path,
        ],
        timeout=1800,
        what="ffmpeg final render",
    )


def _assemble_black(
    audio_path: str,
    width: int,
    height: int,
    duration: float,
    output_path: str,
    subtitle_path: str | None = None,
    music_path: str | None = None,
) -> None:
    """Собирает видео с чёрным фоном + градиентный оверлей (fallback)."""
    input_args: list[str] = [
        "-f", "lavfi",
        "-i", f"color=c=black:s={width}x{height}:r=30:d={duration}",
        "-i", audio_path,
    ]
    filter_parts: list[str] = []

    # Лёгкая виньетка на чёрном фоне (без градиента — он лишний)
    filter_parts.append(f"[0:v]{_VIGNETTE}[vgrad_vig]")

    if subtitle_path:
        filter_parts.append(
            f"[vgrad_vig]subtitles='{_esc(subtitle_path)}'"
            f":force_style='{_SUBTITLE_STYLE}'[vout]"
        )
        video_map = "[vout]"
    else:
        video_map = "[vgrad_vig]"

    voice_idx = 1  # 0=lavfi color, 1=audio
    input_args, audio_map = _build_audio_filters(filter_parts, voice_idx, music_path, input_args)

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", ";".join(filter_parts),
        "-map", video_map,
        "-map", audio_map,
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
        "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
        "-bf", "2", "-refs", "4", "-g", "60",
        "-c:a", "aac", "-ar", "48000", "-b:a", "192k",
        "-t", str(duration),
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(cmd)


def assemble_video(
    audio_path: str,
    platform: Platform,
    clips: list[str] | None = None,
    subtitle_path: str | None = None,
    music_path: str | None = None,
    entity_overlays: list[dict] | None = None,
    photo_burst: dict | None = None,
    photo_pool: list[str] | None = None,
    hook_text: str | None = None,
    counter_overlays: list[dict] | None = None,
    ordered: bool = False,
) -> dict:
    """
    Собирает финальный MP4.

    ordered=True — story mode: клипы/изображения раскладываются по слотам
    в исходном порядке (нарратив), без перемешивания. Элементы clips могут
    быть изображениями (.jpg/.png) — они рендерятся в photo-клипы.

    Args:
        audio_path:      Путь к MP3 от Voice Agent
        platform:        Целевая платформа — определяет разрешение
        clips:           Список путей к видеоклипам. None → чёрный фон.
        subtitle_path:   Путь к ASS файлу субтитров. Опционально.
        music_path:      Путь к фоновой музыке. Опционально (15% громкость).
        entity_overlays: list[{photo_path, start_sec, duration, name}] — фото
                         классиков (Юнг, Фрейд...) поверх клипа в момент их
                         упоминания. Подменяет ближайший сегмент.
        photo_burst:     {photo_paths: [str], start_sec: float} — пачка фото
                         с быстрыми хард-катами. Подменяет один сегмент в
                         start_sec (по умолчанию середина видео).

    Returns:
        dict с полями: video_path, platform, resolution, duration, background

    Raises:
        EnvironmentError: если FFmpeg не установлен
        ValueError:       если platform не поддерживается или файлы не найдены
    """
    _check_ffmpeg()

    if platform not in PLATFORM_RESOLUTION:
        raise ValueError(
            f"Неподдерживаемая платформа: {platform}. "
            f"Допустимые: {list(PLATFORM_RESOLUTION.keys())}"
        )

    if not os.path.exists(audio_path):
        raise ValueError(f"Аудио файл не найден: {audio_path}")

    if clips:
        missing = [c for c in clips if not os.path.exists(c)]
        if missing:
            raise ValueError(f"Клипы не найдены: {missing}")

    width, height = PLATFORM_RESOLUTION[platform]
    duration = _get_duration(audio_path)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Суффикс uuid: параллельные ниши в одну секунду не перезаписывают файлы друг друга
    output_path = os.path.abspath(os.path.join(OUTPUT_DIR, f"{timestamp}_{platform}_{uuid.uuid4().hex[:6]}.mp4"))

    if clips:
        extras = []
        if entity_overlays:
            extras.append(f"{len(entity_overlays)} entity overlays")
        if photo_burst:
            extras.append(f"burst x{len(photo_burst.get('photo_paths', []))}")
        if photo_pool:
            extras.append(f"{len(photo_pool)} photo weave")
        if hook_text:
            extras.append("hook overlay")
        if counter_overlays:
            extras.append(f"{len(counter_overlays)} counters")
        extras_str = f" + {' + '.join(extras)}" if extras else ""
        logger.info(
            "Монтаж %d клипов%s (Ken Burns + crossfade) → %dx%d",
            len(clips), extras_str, width, height,
        )
        _assemble_with_clips(
            clips, audio_path, width, height, duration, output_path,
            subtitle_path, music_path,
            entity_overlays=entity_overlays,
            photo_burst=photo_burst,
            photo_pool=photo_pool,
            hook_text=hook_text,
            counter_overlays=counter_overlays,
            ordered=ordered,
        )
        background = "clips"
    else:
        logger.info("Чёрный фон + audio → %dx%d", width, height)
        _assemble_black(
            audio_path, width, height, duration, output_path,
            subtitle_path, music_path,
        )
        background = "black"

    if not os.path.exists(output_path):
        raise RuntimeError(f"FFmpeg не создал файл: {output_path}")

    return {
        "video_path": output_path,
        "platform": platform,
        "resolution": f"{width}x{height}",
        "duration": round(duration, 2),
        "background": background,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Использование: py agents/assembly_agent.py <audio_path> [platform]")
        sys.exit(1)

    audio = sys.argv[1]
    plat = sys.argv[2] if len(sys.argv) > 2 else "tiktok"
    print(f"Собираю видео: {audio} -> {plat}...\n")
    result = assemble_video(audio, plat)
    print(json.dumps(result, ensure_ascii=False, indent=2))
