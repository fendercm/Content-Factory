"""
Music Agent — подбирает фоновую музыку по настроению сценария.

Вход:  script_text (str), mood (str | None)
Выход: {"music_path": str, "mood": str, "source": str}

Источники (приоритет):
  1. Локальная папка assets/music/{mood}/ или assets/music/
  2. Free Music Archive API (FMA_API_KEY)
  3. Тишина (fallback, не блокирует пайплайн)

Установка: pip install groq
"""

import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
from datetime import datetime
from typing import Literal

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
FMA_API_KEY = os.getenv("FMA_API_KEY", "")
KIE_API_KEY = os.getenv("KIE_API_KEY", "")

ASSETS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "music"))
OUTPUT_MUSIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "output", "music"))

_KIE_BASE_URL = "https://api.kie.ai/api/v1"

# Suno prompts по нише (приоритет над mood)
_NICHE_SUNO_PROMPTS: dict[str, str] = {
    "психология":     "calm ambient piano background for self-development, soft, no lyrics",
    "отношения":      "calm ambient piano background for self-development, soft, no lyrics",
    "медитация":      "peaceful nature ambient, relaxing, no lyrics",
    "образование":    "calm ambient piano background for self-development, soft, no lyrics",
    "бизнес":         "motivational corporate background, professional, no lyrics",
    "деньги":         "motivational corporate background, professional, no lyrics",
    "финансы":        "motivational corporate background, professional, no lyrics",
    "карьера":        "motivational corporate background, professional, no lyrics",
    "продуктивность": "inspiring uplifting instrumental, positive energy, no lyrics",
    "здоровье":       "peaceful nature ambient, relaxing, no lyrics",
    "питание":        "peaceful nature ambient, relaxing, no lyrics",
    "мотивация":      "energetic driving beat, pump up, no lyrics",
    "спорт":          "energetic driving beat, pump up, no lyrics",
    "саморазвитие":   "inspiring uplifting instrumental, positive energy, no lyrics",
    "наука":          "inspiring uplifting instrumental, positive energy, no lyrics",
    "технологии":     "inspiring uplifting instrumental, positive energy, no lyrics",
    "философия":      "calm ambient piano background for self-development, soft, no lyrics",
    "история":        "calm ambient piano background for self-development, soft, no lyrics",
}

# Fallback по mood
_MOOD_SUNO_PROMPTS: dict[str, str] = {
    "upbeat":   "energetic driving beat, pump up, no lyrics",
    "calm":     "calm ambient piano background for self-development, soft, no lyrics",
    "dramatic": "epic cinematic orchestral background, intense, no lyrics",
}

Mood = Literal["upbeat", "calm", "dramatic"]
_MOODS: list[str] = ["upbeat", "calm", "dramatic"]

# FMA genre IDs по настроению
_FMA_GENRE_IDS: dict[str, list[int]] = {
    "upbeat":   [76, 45, 8],    # Electronic, Dance, Pop
    "calm":     [21, 1, 15],    # Ambient, Classical, Folk
    "dramatic": [9, 38, 66],    # Soundtrack, Experimental, Jazz
}

_MOOD_PROMPT = """Проанализируй текст сценария и определи настроение для фоновой музыки.
Ответь строго одним словом: upbeat, calm или dramatic.

upbeat  — энергичный, мотивационный, позитивный
calm    — спокойный, образовательный, медитативный
dramatic — напряжённый, сенсационный, эмоциональный

Текст: {script_text}

Ответ (только одно слово):"""


def detect_mood(script_text: str) -> Mood:
    """Определяет настроение сценария через Groq."""
    if not GROQ_API_KEY:
        logger.warning("GROQ_API_KEY не задан — настроение по умолчанию: upbeat")
        return "upbeat"
    mood_messages = [{"role": "user", "content": _MOOD_PROMPT.format(script_text=script_text[:500])}]
    try:
        from utils.llm_client import groq_with_cerebras_fallback
        response = groq_with_cerebras_fallback(
            messages=mood_messages,
            temperature=0.1,
            max_tokens=10,
            caller="music_agent",
        )
    except Exception as exc:
        logger.warning("Mood detection ошибка: %s — upbeat по умолчанию", exc)
        return "upbeat"
    try:
        raw = response.choices[0].message.content.strip().lower()
        word = re.split(r"\W+", raw)[0] if raw else "upbeat"
        mood: Mood = word if word in _MOODS else "upbeat"  # type: ignore[assignment]
        logger.info("Определено настроение: %s", mood)
        return mood
    except Exception as exc:
        logger.warning("Mood parse ошибка: %s — upbeat по умолчанию", exc)
        return "upbeat"


def _kie_poll(task_id: str, headers: dict, max_wait: int = 300) -> dict | None:
    """Polls Kie.ai queryTaskDetail until succeeded/failed or timeout."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        try:
            resp = requests.get(
                f"{_KIE_BASE_URL}/jobs/recordInfo",
                headers=headers,
                params={"taskId": task_id},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Kie.ai poll error: %s", exc)
            continue
        inner = data.get("data") or data
        state = str(inner.get("state") or inner.get("status", "")).lower()
        if state in ("succeeded", "success", "completed"):
            return inner
        if state in ("failed", "fail", "error"):
            logger.warning("Kie.ai task failed: %s", inner)
            return None
        elapsed = int(max_wait - (deadline - time.time()))
        if elapsed % 30 < 3:
            logger.info("Kie.ai waiting... state=%s (%ds)", state, elapsed)
    logger.warning("Kie.ai timeout after %ds, task_id=%s", max_wait, task_id)
    return None


def _extract_kie_url(result: dict) -> str | None:
    """Extracts audio URL from Kie.ai succeeded task result."""
    for key in ("result", "output", "url", "audio_url", "audioUrl"):
        val = result.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
        if isinstance(val, dict):
            for sub in ("url", "audio_url", "audioUrl", "link"):
                u = val.get(sub)
                if isinstance(u, str) and u.startswith("http"):
                    return u
    for v in result.values():
        if isinstance(v, str) and v.startswith("http") and any(
            ext in v for ext in (".mp3", ".wav", ".ogg", ".m4a")
        ):
            return v
    return None


_SUNO_GENERATE_URL = "https://api.kie.ai/api/v1/generate"
_SUNO_RECORD_URL   = "https://api.kie.ai/api/v1/generate/record-info"
_SUNO_MODEL        = "V4_5"  # V3_5 | V4 | V4_5 | V4_5PLUS | V5 | V5_5


def _from_suno(mood: str, niche: str = "") -> str | None:
    """Генерирует трек через Suno via Kie.ai dedicated API. Возвращает путь к MP3 или None."""
    from utils.providers import is_enabled
    if not KIE_API_KEY or not is_enabled("kie"):
        return None

    prompt = _NICHE_SUNO_PROMPTS.get(niche.strip().lower(), "") or _MOOD_SUNO_PROMPTS.get(mood, "")
    if not prompt:
        return None

    headers = {
        "Authorization": f"Bearer {KIE_API_KEY}",
        "Content-Type": "application/json",
    }

    # 1. Создаём задачу через dedicated Suno endpoint
    try:
        resp = requests.post(
            _SUNO_GENERATE_URL,
            headers=headers,
            json={
                "prompt": prompt,
                "customMode": False,
                "instrumental": True,
                "model": _SUNO_MODEL,
                "callBackUrl": "https://example.com/kie-callback",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Suno: запуск задачи ошибка: %s", exc)
        return None

    if data.get("code") not in (200, None):
        logger.warning("Suno: ошибка API: %s", data.get("msg", data))
        return None

    inner = data.get("data") or data
    task_id = inner.get("taskId") or inner.get("task_id") or inner.get("id")
    if not task_id:
        logger.warning("Suno: taskId не найден: %s", data)
        return None

    logger.info("Suno: taskId=%s, prompt=%r", task_id, prompt[:60])

    # 2. Polling через dedicated record-info endpoint
    import json as _json
    deadline = time.time() + 300
    audio_url: str | None = None
    while time.time() < deadline:
        time.sleep(5)
        try:
            r = requests.get(_SUNO_RECORD_URL, headers=headers, params={"taskId": task_id}, timeout=15)
            r.raise_for_status()
            rd = r.json()
        except Exception as exc:
            logger.warning("Suno poll error: %s", exc)
            continue

        inner_r = rd.get("data") or rd
        # data может быть list (multiple clips) или dict
        if isinstance(inner_r, list):
            inner_r = inner_r[0] if inner_r else {}
        state = str(inner_r.get("status") or inner_r.get("state", "")).upper()
        elapsed = int(300 - (deadline - time.time()))
        if elapsed % 30 < 5:
            logger.info("Suno waiting... state=%s (%ds)", state, elapsed)

        if state in ("SUCCESS", "FIRST_SUCCESS", "COMPLETED"):
            # Extract audio URL from response
            audio_url = (
                inner_r.get("audioUrl")
                or inner_r.get("audio_url")
                or inner_r.get("url")
            )
            if not audio_url:
                # Try resultJson
                rj_str = inner_r.get("resultJson", "")
                if rj_str:
                    try:
                        rj = _json.loads(rj_str)
                        urls = rj.get("resultUrls") or rj.get("audioUrls") or []
                        audio_url = urls[0] if urls else None
                    except Exception:
                        pass
            if audio_url:
                break
        elif state in ("FAILED", "FAIL", "ERROR"):
            logger.warning("Suno task failed: %s", inner_r)
            return None

    if not audio_url:
        logger.warning("Suno: URL аудио не найден после timeout")
        return None

    os.makedirs(OUTPUT_MUSIC_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(OUTPUT_MUSIC_DIR, f"suno_{mood}_{timestamp}.mp3")

    try:
        dl = requests.get(audio_url, timeout=120, stream=True)
        dl.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in dl.iter_content(chunk_size=8192):
                f.write(chunk)
        if os.path.getsize(dest) < 5_000:
            raise RuntimeError("Suno: скачанный файл слишком мал")
        logger.info("Suno: трек сохранён → %s", dest)
        try:
            from utils.cost_tracker import log_cost
            log_cost("music_agent", "kie_suno", "track", 1)
        except Exception:
            pass
        return os.path.abspath(dest)
    except Exception as exc:
        logger.warning("Suno: скачивание ошибка: %s", exc)
        if os.path.exists(dest):
            os.remove(dest)
        return None


def _from_local(mood: str) -> str | None:
    """Ищет трек в assets/music/{mood}/ или assets/music/."""
    for search_dir in (os.path.join(ASSETS_DIR, mood), ASSETS_DIR):
        if not os.path.isdir(search_dir):
            continue
        tracks = [
            f for f in os.listdir(search_dir)
            if f.lower().endswith((".mp3", ".wav", ".ogg", ".aac", ".m4a"))
            and f.lower() != "silence.mp3"
        ]
        if tracks:
            chosen = random.choice(tracks)
            path = os.path.abspath(os.path.join(search_dir, chosen))
            logger.info("Трек из локальной папки: %s", chosen)
            return path
    return None


def _from_fma(mood: str) -> str | None:
    """Скачивает трек с Free Music Archive API."""
    if not FMA_API_KEY:
        return None

    genre_ids = _FMA_GENRE_IDS.get(mood, _FMA_GENRE_IDS["upbeat"])
    genre_id = random.choice(genre_ids)

    try:
        resp = requests.get(
            "https://freemusicarchive.org/api/get/tracks.json",
            params={
                "api_key": FMA_API_KEY,
                "genre_id": genre_id,
                "limit": 20,
                "page": random.randint(1, 3),
            },
            timeout=15,
        )
        resp.raise_for_status()
        tracks = resp.json().get("dataset", [])
        if not tracks:
            return None

        track = random.choice(tracks)
        track_url = track.get("track_url", "")
        track_title = track.get("track_title", "unknown")
        if not track_url:
            return None

        dest_dir = os.path.join(ASSETS_DIR, mood)
        os.makedirs(dest_dir, exist_ok=True)
        safe_name = re.sub(r"[^\w-]", "_", track_title)[:40]
        dest = os.path.join(dest_dir, f"{safe_name}.mp3")

        from utils.download import download_to, is_cached
        if is_cached(dest, min_bytes=5_000):
            logger.info("Трек уже скачан: %s", safe_name)
            return os.path.abspath(dest)

        logger.info("Скачиваю трек FMA: %s", track_title)
        download_to(track_url, dest, timeout=90, min_bytes=5_000)

        logger.info("Трек сохранён: %s", dest)
        return os.path.abspath(dest)

    except Exception as exc:
        logger.warning("FMA ошибка: %s", exc)
        return None


def _make_silence(mood: str, duration: int = 60) -> str:
    """Создаёт MP3-файл тишины через FFmpeg как fallback."""
    dest_dir = os.path.join(ASSETS_DIR, mood)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "silence.mp3")
    if not os.path.exists(dest):
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            subprocess.run(
                [ffmpeg, "-y", "-f", "lavfi",
                 "-i", f"anullsrc=r=44100:cl=stereo",
                 "-t", str(duration), dest],
                capture_output=True, check=True, timeout=300,
            )
    return os.path.abspath(dest)


def get_music(script_text: str, mood: Mood | None = None, niche: str = "") -> dict:
    """
    Подбирает фоновую музыку по настроению сценария.

    Args:
        script_text: Текст сценария (для определения настроения через Groq)
        mood:        Явно заданное настроение — пропускает Groq-детекцию
        niche:       Ниша (психология, бизнес...) — выбирает Suno-промпт

    Returns:
        dict с полями: music_path, mood, source
    """
    detected = mood or detect_mood(script_text)

    # 1. Suno via Kie.ai
    path = _from_suno(detected, niche)
    if path:
        return {"music_path": path, "mood": detected, "source": "suno"}

    # 2. Локальные файлы
    path = _from_local(detected)
    if path:
        return {"music_path": path, "mood": detected, "source": "local"}

    # 3. FMA
    path = _from_fma(detected)
    if path:
        return {"music_path": path, "mood": detected, "source": "fma"}

    logger.warning("Треки не найдены — используем тишину (mood=%s)", detected)
    return {"music_path": _make_silence(detected), "mood": detected, "source": "silence"}


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    text = sys.argv[1] if len(sys.argv) > 1 else "Знаешь ли ты что твой мозг принимает 35000 решений в день"
    result = get_music(text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
