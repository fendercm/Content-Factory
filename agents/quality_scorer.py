"""
Quality Scorer Agent — оценивает финальное видео через Gemini Vision (мультимодальный).

Извлекает 4-6 кадров из видео, передаёт их в Gemini 2.0 Flash, получает score 1-10
с обоснованием. Используется для авто-отбраковки слабых видео перед публикацией.

API: score_video(video_path, hook_text?) → {
    "score": int 1-10,
    "verdict": "approved"|"rejected"|"warning",
    "reasoning": str,
    "frames_analyzed": int,
}

Threshold default: 6 (видео ниже считается слабым).
"""

import base64
import logging
import os
import shutil
import subprocess
import tempfile
import json as _json
import re as _re
from typing import Any

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 6
FRAMES_TO_SAMPLE = 5

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


_SCORE_PROMPT = """Ты — эксперт по виральному TikTok/Shorts контенту.
Оцени видео по 5 кадрам ниже. Это короткое вертикальное видео для TikTok/Shorts.

Контекст:
- Тема hook'а: "{hook_text}"
- Целевая аудитория: широкая, эмоция — шок/инсайт
- Длительность: ~45 секунд

КРИТЕРИИ ОЦЕНКИ (1-10):
1. Визуальное разнообразие (нет ли повторов кадров)
2. Релевантность визуала теме
3. Качество композиции (фокус, лица, эмоции)
4. Читаемость субтитров (контраст, размер, позиция)
5. Hook-сила первого кадра (привлекает ли внимание)

Дай общий score от 1 до 10:
- 9-10: вирусный потенциал высокий, опубликовать обязательно
- 7-8: хорошее видео, можно публиковать
- 6: проходной, спорно
- 4-5: слабое, рискованно публиковать
- 1-3: брак, не публиковать

Верни JSON:
{{
  "score": <число 1-10>,
  "verdict": "approved"|"warning"|"rejected",
  "reasoning": "<2-3 предложения почему этот score>"
}}

Только JSON, без markdown."""


def _extract_frames(video_path: str, num_frames: int = 5) -> list[str]:
    """Извлекает N кадров равномерно по timeline видео.
    Возвращает список путей к JPEG."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe or not os.path.exists(video_path):
        return []

    # Узнаём длительность
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout.strip()
        duration = float(out)
    except Exception as exc:
        logger.warning("ffprobe duration error: %s", exc)
        return []

    if duration <= 0:
        return []

    # Равномерно распределяем точки выборки: первый кадр на 1с (после hook), затем равномерно
    timestamps = []
    if num_frames == 1:
        timestamps = [duration * 0.5]
    else:
        # Начинаем с 1.5с (после hook), заканчиваем за 2с до конца
        start = min(1.5, duration * 0.1)
        end = max(duration - 2.0, duration * 0.9)
        if end <= start:
            end = duration * 0.9
        step = (end - start) / max(num_frames - 1, 1)
        timestamps = [start + step * i for i in range(num_frames)]

    tmp_dir = tempfile.mkdtemp(prefix="qs_frames_")
    paths: list[str] = []
    for i, t in enumerate(timestamps):
        out_path = os.path.join(tmp_dir, f"frame_{i}.jpg")
        try:
            subprocess.run(
                [ffmpeg, "-y", "-ss", f"{t:.2f}", "-i", video_path,
                 "-vframes", "1", "-q:v", "3", out_path],
                capture_output=True, check=True, timeout=15,
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                paths.append(out_path)
        except Exception as exc:
            logger.warning("Frame extract %.2fs failed: %s", t, exc)

    # Record tmp_dir so score_video can wipe the whole directory
    _extract_frames._last_tmp_dir = tmp_dir  # type: ignore[attr-defined]
    return paths


_GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


def _groq_vision_score(prompt_text: str, frame_paths: list[str]) -> str:
    """Оценка кадров через Groq Llama-4 Scout (мультимодальный, бесплатный tier).
    Возвращает сырой текст ответа. Groq принимает до 5 изображений на запрос."""
    from utils.llm_client import _get_groq

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for fp in frame_paths[:5]:
        with open(fp, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    resp = _get_groq().chat.completions.create(
        model=_GROQ_VISION_MODEL,
        messages=[{"role": "user", "content": content}],
        temperature=0.2,
        max_tokens=400,
    )
    raw = (resp.choices[0].message.content or "").strip()
    logger.info("Quality Scorer: оценка через Groq %s", _GROQ_VISION_MODEL)
    return raw


def _skipped(reason: str, frames_analyzed: int) -> dict[str, Any]:
    return {"score": None, "verdict": "skipped", "reasoning": reason,
            "frames_analyzed": frames_analyzed}


def score_video(
    video_path: str,
    hook_text: str = "",
    threshold: int = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """
    Оценивает видео через Gemini Vision. Возвращает score + verdict.

    Args:
        video_path: путь к финальному MP4
        hook_text:  текст hook'а для контекста (опционально)
        threshold:  минимальный score для verdict=approved (default 6)

    Returns:
        {score: int | None, verdict: str, reasoning: str, frames_analyzed: int}

    Verdict логика:
        score >= 8 → approved
        threshold <= score < 8 → warning (можно публиковать, но не идеально)
        score < threshold → rejected
        оценка не получена → skipped, score=None (раньше подставлялось 7/approved,
        и статистика оценок была фиктивной)
    """
    if not GEMINI_API_KEY and not os.getenv("GROQ_API_KEY", ""):
        logger.warning("Quality Scorer: нет ни GEMINI_API_KEY, ни GROQ_API_KEY — пропускаем оценку")
        return _skipped("no API key", 0)

    if not os.path.exists(video_path):
        return {"score": 0, "verdict": "rejected", "reasoning": "video file missing",
                "frames_analyzed": 0}

    frames = _extract_frames(video_path, FRAMES_TO_SAMPLE)
    if len(frames) < 2:
        logger.warning("Quality Scorer: не удалось извлечь кадры — пропускаем")
        return _skipped("frame extract failed", 0)

    from utils.providers import is_enabled, mark_dead

    prompt_text = _SCORE_PROMPT.format(hook_text=hook_text or "не указан")
    raw = ""
    try:
        # ── Engine 1: Gemini Vision ──────────────────────────────────────────
        if GEMINI_API_KEY and is_enabled("gemini"):
            try:
                from google import genai
                from google.genai import types as genai_types

                client = genai.Client(api_key=GEMINI_API_KEY)
                parts: list[Any] = [prompt_text]
                for fp in frames:
                    with open(fp, "rb") as f:
                        img_bytes = f.read()
                    parts.append(genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))

                resp = client.models.generate_content(
                    model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                    contents=parts,
                )
                raw = (resp.text or "").strip()
            except Exception as exc:
                err = str(exc)
                if "RESOURCE_EXHAUSTED" in err or "limit: 0" in err:
                    mark_dead("gemini", err[:200])
                logger.warning("Quality Scorer Gemini error: %s — пробую Groq vision", exc)

        # ── Engine 2: Groq Llama-4 Scout (vision, бесплатный tier) ──────────
        if not raw:
            try:
                raw = _groq_vision_score(prompt_text, frames)
            except Exception as exc:
                logger.warning("Quality Scorer Groq vision error: %s — оценка пропущена", exc)
                return _skipped(f"scorer error: {exc}", len(frames))
    finally:
        # Cleanup temp frames directory (not just individual files)
        tmp_dir = getattr(_extract_frames, "_last_tmp_dir", None)
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # Парсим JSON из ответа
    m = _re.search(r"\{.*\}", raw, _re.DOTALL)
    if not m:
        logger.warning("Quality Scorer: невалидный JSON — оценка пропущена. Ответ: %s", raw[:100])
        return _skipped("json parse failed", len(frames))
    try:
        data = _json.loads(m.group())
        score = int(data["score"])
    except (_json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _skipped("json decode failed / no score", len(frames))

    reasoning = str(data.get("reasoning", ""))[:300]

    if score >= 8:
        verdict = "approved"
    elif score >= threshold:
        verdict = "warning"
    else:
        verdict = "rejected"

    # Если LLM сам прислал verdict — учитываем но не выше нашего расчёта.
    # rejected блокирует публикацию, поэтому он выставляется только по score:
    # вердикт модели может понизить approved → warning, но не до rejected.
    llm_verdict = data.get("verdict", "")
    if llm_verdict in ("approved", "warning", "rejected"):
        order = {"rejected": 0, "warning": 1, "approved": 2}
        if order.get(llm_verdict, 2) < order.get(verdict, 2):
            verdict = "warning" if verdict == "approved" else verdict

    logger.info(
        "Quality Scorer: score=%d/10 verdict=%s — %s",
        score, verdict, reasoning[:120],
    )
    return {
        "score": score,
        "verdict": verdict,
        "reasoning": reasoning,
        "frames_analyzed": len(frames),
    }


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    video = sys.argv[1] if len(sys.argv) > 1 else "output/video/20260523_172431_youtube.mp4"
    hook = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"Scoring: {video}\n")
    result = score_video(video, hook)
    print(json.dumps(result, ensure_ascii=False, indent=2))
