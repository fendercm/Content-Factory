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

# Веса критериев в итоговом score. Первый кадр (hook) весит больше всего:
# 30-50% зрителей Shorts уходят в первые 1-3 секунды.
_WEIGHTS = {
    "hook_frame": 0.30,
    "relevance": 0.20,
    "subtitles": 0.20,
    "composition": 0.15,
    "variety": 0.15,
}

_SCORE_PROMPT = """Ты — продюсер коротких вертикальных видео (TikTok / Reels / Shorts).
Перед тобой {n} кадров одного ролика в хронологическом порядке: кадр 1 — первая
секунда (hook с крупным текстом), остальные — равномерно по таймлайну.

Hook ролика: "{hook_text}"

Оцени каждый критерий по шкале 1-10, используя якоря:
- hook_frame — остановит ли кадр 1 скролл: крупный читаемый текст, контраст,
  лицо/эмоция или яркий объект. 9: читается за полсекунды и интригует;
  5: обычный стоковый кадр с мелким текстом; 2: тёмный/размытый/пустой.
- relevance — связан ли видеоряд с hook и темой. 9: каждый кадр иллюстрирует мысль;
  5: общий сток «про жизнь»; 2: случайные несвязанные кадры.
- subtitles — субтитры: читаются на телефоне, не перекрывают лица, не вылезают
  за край. 9: крупно и контрастно; 5: читаемо, но мелко/перекрывает; 2: не читаются
  или отсутствуют.
- composition — фокус, освещение, вертикальный кроп без чёрных полос и обрезанных голов.
- variety — кадры разные по плану и содержанию; 2 — один и тот же кадр/сцена повторяются.

В issues перечисли конкретные дефекты (номер кадра + что не так). Не выдумывай
дефекты, которых не видно.

Верни JSON:
{{"criteria": {{"hook_frame": 7, "relevance": 7, "subtitles": 7, "composition": 7, "variety": 7}},
  "issues": ["кадр 1: ..."],
  "reasoning": "2-3 предложения"}}"""


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

    # Кадр 1 — внутри hook (0.5с), остальные равномерно до конца без финальной заставки
    timestamps = []
    if num_frames == 1:
        timestamps = [min(0.5, duration * 0.1)]
    else:
        start = min(0.5, duration * 0.05)
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


def _vision_score(prompt_text: str, frame_paths: list[str]) -> str:
    """
    Оценка кадров через общую LLM-цепочку с картинками
    (Gemini Flash-Lite → Gemini Flash → Groq qwen3.8 vision → Gemma).
    Возвращает сырой текст ответа.
    """
    from utils.llm_client import chat

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for fp in frame_paths[:5]:
        with open(fp, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    resp = chat([{"role": "user", "content": content}], tier="fast", temperature=0.2,
                max_tokens=600, json_mode=True, caller="quality_scorer")
    logger.info("Quality Scorer: оценка через %s/%s", resp.provider, resp.model)
    return (resp.choices[0].message.content or "").strip()


def _weighted_score(criteria: dict) -> int:
    total, weight_sum = 0.0, 0.0
    for name, weight in _WEIGHTS.items():
        try:
            value = float(criteria[name])
        except (KeyError, TypeError, ValueError):
            continue
        total += max(1.0, min(10.0, value)) * weight
        weight_sum += weight
    if weight_sum < 0.5:  # модель вернула меньше половины критериев
        raise ValueError("недостаточно критериев")
    return int(round(total / weight_sum))


def _skipped(reason: str, frames_analyzed: int) -> dict[str, Any]:
    return {"score": None, "verdict": "skipped", "reasoning": reason,
            "frames_analyzed": frames_analyzed}


def score_video(
    video_path: str,
    hook_text: str = "",
    threshold: int = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """
    Оценивает видео по кадрам через vision-LLM. Score — взвешенное среднее
    критериев (_WEIGHTS), а не «общее впечатление» модели.

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
    if not os.getenv("GEMINI_API_KEY", "") and not os.getenv("GROQ_API_KEY", ""):
        logger.warning("Quality Scorer: нет ни GEMINI_API_KEY, ни GROQ_API_KEY — пропускаем оценку")
        return _skipped("no API key", 0)

    if not os.path.exists(video_path):
        return {"score": 0, "verdict": "rejected", "reasoning": "video file missing",
                "frames_analyzed": 0}

    frames = _extract_frames(video_path, FRAMES_TO_SAMPLE)
    if len(frames) < 2:
        logger.warning("Quality Scorer: не удалось извлечь кадры — пропускаем")
        return _skipped("frame extract failed", 0)

    prompt_text = _SCORE_PROMPT.format(hook_text=hook_text or "не указан", n=len(frames))
    try:
        try:
            raw = _vision_score(prompt_text, frames)
        except Exception as exc:
            logger.warning("Quality Scorer: vision-LLM недоступен: %s — оценка пропущена", str(exc)[:200])
            return _skipped(f"scorer error: {str(exc)[:200]}", len(frames))
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
        if isinstance(data.get("criteria"), dict):
            score = _weighted_score(data["criteria"])
        else:
            score = int(data["score"])  # совместимость со старым форматом ответа
    except (_json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _skipped("json decode failed / no score", len(frames))

    issues = [str(i) for i in (data.get("issues") or [])][:6]
    reasoning = str(data.get("reasoning", ""))[:300]

    if score >= 8:
        verdict = "approved"
    elif score >= threshold:
        verdict = "warning"
    else:
        verdict = "rejected"

    # rejected блокирует публикацию, поэтому выставляется только по score.
    # Нечитаемые субтитры или пустой hook-кадр понижают approved → warning.
    criteria = data.get("criteria") if isinstance(data.get("criteria"), dict) else {}
    try:
        weak = [k for k in ("hook_frame", "subtitles") if float(criteria.get(k, 10)) <= 4]
    except (TypeError, ValueError):
        weak = []
    if weak and verdict == "approved":
        verdict = "warning"

    logger.info(
        "Quality Scorer: score=%d/10 verdict=%s — %s",
        score, verdict, reasoning[:120],
    )
    return {
        "score": score,
        "verdict": verdict,
        "reasoning": reasoning,
        "criteria": criteria,
        "issues": issues,
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
