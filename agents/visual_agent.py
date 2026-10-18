"""
Visual Agent — генерирует видеоклипы для монтажа.

Режимы (приоритет):
  1. Kling story mode (KIE_API_KEY задан) — AI image-to-video с единым персонажем:
       a. YandexART генерирует опорное фото персонажа (один раз)
       b. Сценарий делится на 4-5 нарративных актов
       c. Kling image-to-video анимирует персонажа под каждый акт
  2. Kling text-to-video — fallback если image-to-video не поддерживается
  3. Pexels — stock footage (если нет KIE_API_KEY)

Вход:  topic (str), platform (str), num_clips (int), script_text (str|None), niche (str)
Выход: {"clips": ["path1.mp4", ...], "source": "kling_story"|"kling"|"pexels", "query": "..."}
"""

import base64
import logging
import os
import re
import time
import json
import requests
from typing import Literal
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

Platform = Literal["tiktok", "youtube", "instagram", "linkedin", "twitter"]

PLATFORM_ORIENTATION = {
    "tiktok":    "portrait",
    "instagram": "portrait",
    "youtube":   "portrait",   # YouTube Shorts — вертикальный формат
    "linkedin":  "landscape",
    "twitter":   "landscape",
}

PLATFORM_PREFERRED_SIZE = {
    "portrait":  (1080, 1920),
    "landscape": (1920, 1080),
}

# Kling ratio per orientation
KLING_RATIO = {
    "portrait":  "9:16",
    "landscape": "16:9",
}

CLIPS_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "clips")
PHOTOS_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "photos")
CHARS_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "characters")
PEXELS_API_URL = "https://api.pexels.com/videos/search"
PEXELS_PHOTOS_URL = "https://api.pexels.com/v1/search"
KIE_BASE_URL = "https://api.kie.ai/api/v1"
UNSPLASH_API_URL = "https://api.unsplash.com/search/photos"

# Нишевые ключевые слова для релевантных Pexels-запросов
NICHE_KEYWORDS: dict[str, str] = {
    "психология":    "psychology mind brain thinking",
    "бизнес":        "business office entrepreneur success",
    "здоровье":      "fitness health wellness lifestyle",
    "продуктивность": "productivity focus work desk",
    "мотивация":     "motivation success achievement goal",
    "деньги":        "money finance wealth investment",
    "отношения":     "relationships couple communication",
    "саморазвитие":  "self improvement personal growth",
    "технологии":    "technology innovation digital future",
    "наука":         "science research laboratory discovery",
    "спорт":         "sports training athlete performance",
    "питание":       "nutrition food healthy eating",
    "медитация":     "meditation mindfulness calm peace",
    "карьера":       "career professional workplace growth",
    "образование":   "education learning student knowledge",
}


# ---------------------------------------------------------------------------
# Story mode — персонаж + нарративные акты
# ---------------------------------------------------------------------------

# Промпты для YandexART — один персонаж под нишу
NICHE_CHARACTER_PROMPTS: dict[str, str] = {
    "психология":     "Young thoughtful woman, 27 years old, warm natural look, light casual top, soft neutral indoor background, gentle daylight, photorealistic portrait, vertical format",
    "бизнес":         "Young ambitious professional man, 30 years old, modern casual business attire, bright modern office background, confident expression, photorealistic portrait, vertical format",
    "здоровье":       "Athletic woman, 26 years old, sportswear, bright outdoor park background, energetic natural expression, photorealistic portrait, vertical format",
    "продуктивность": "Focused young man, 28 years old, minimalist outfit, clean bright home office background, determined expression, photorealistic portrait, vertical format",
    "мотивация":      "Inspiring young woman, 25 years old, casual bright outfit, outdoor sunlight background, motivated radiant expression, photorealistic portrait, vertical format",
    "деньги":         "Confident young professional, 29 years old, smart casual outfit, modern bright city background, calm successful expression, photorealistic portrait, vertical format",
    "отношения":      "Warm smiling young woman, 26 years old, casual outfit, cozy bright home background, open friendly expression, photorealistic portrait, vertical format",
    "саморазвитие":   "Thoughtful young man, 28 years old, casual modern outfit, bright nature background, reflective inspired expression, photorealistic portrait, vertical format",
    "медитация":      "Peaceful young woman, 27 years old, comfortable casual clothes, bright nature outdoor background, serene calm expression, photorealistic portrait, vertical format",
    "карьера":        "Ambitious young professional woman, 28 years old, smart outfit, bright modern office background, determined focused expression, photorealistic portrait, vertical format",
    "образование":    "Curious young student, 22 years old, casual outfit, bright library or campus background, engaged inquisitive expression, photorealistic portrait, vertical format",
}
_DEFAULT_CHARACTER = "Young relatable person, 27 years old, casual modern outfit, bright neutral background, natural friendly expression, photorealistic portrait, vertical format"

# Базовые движения для каждого нарративного акта (Kling получает это как action prompt)
ACT_SCENE_TEMPLATES: dict[str, str] = {
    "hook":      "character looks directly at camera with a surprised or provocative expression, slight lean forward, bright natural light, cinematic",
    "problem":   "character sits pensively, hand near chin, slightly concerned look, glancing away in thought, soft daylight",
    "insight":   "character has a realization — eyes widen slightly, sits up, subtle smile begins to form, warm indoor light",
    "contrast":  "character stands confidently, shoulders back, purposeful gaze forward, golden hour outdoor light",
    "cta":       "character looks warmly at camera with a knowing smile, slight nod, open inviting hand gesture, bright light",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _niche_override(topic: str) -> str | None:
    topic_lower = topic.lower()
    for niche, keywords in NICHE_KEYWORDS.items():
        if niche in topic_lower:
            return keywords
    return None


def is_cached(path: str) -> bool:
    from utils.download import is_cached as _is_cached
    return _is_cached(path, min_bytes=1_000)


def _download_clip(url: str, dest_path: str) -> None:
    # Через .part: оборванная загрузка не должна остаться в кэше как «готовый» клип
    from utils.download import download_to
    download_to(url, dest_path, timeout=60, min_bytes=1_000)


# ---------------------------------------------------------------------------
# Kling 2.6 via kie.ai
# ---------------------------------------------------------------------------

def _kie_poll_video(task_id: str, headers: dict, max_wait: int = 300) -> dict | None:
    """Polls queryTaskDetail until succeeded/failed or timeout. Returns inner data dict."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        try:
            resp = requests.get(
                f"{KIE_BASE_URL}/jobs/recordInfo",
                headers=headers,
                params={"taskId": task_id},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Kling poll error: %s", exc)
            continue
        inner = data.get("data") or data
        state = str(inner.get("state") or inner.get("status", "")).lower()
        if state in ("succeeded", "success", "completed"):
            return inner
        if state in ("failed", "fail", "error"):
            logger.warning("Kling task failed: %s", inner)
            return None
        elapsed = int(max_wait - (deadline - time.time()))
        if elapsed % 30 < 3:
            logger.info("Kling waiting... state=%s (%ds)", state, elapsed)
    logger.warning("Kling timeout after %ds, task_id=%s", max_wait, task_id)
    return None


def _extract_video_url(result: dict) -> str | None:
    """Extracts video URL from Kie.ai succeeded task result.
    Handles resultJson (JSON string) with resultUrls array."""
    import json as _json

    # Parse resultJson — primary format: {"resultUrls": ["https://..."]}
    result_json_str = result.get("resultJson", "")
    if result_json_str:
        try:
            rj = _json.loads(result_json_str)
            urls = rj.get("resultUrls") or rj.get("urls") or []
            if urls and isinstance(urls[0], str):
                return urls[0]
        except Exception:
            pass

    # Fallback: scan top-level fields
    for key in ("result", "output", "url", "videoUrl", "video_url"):
        val = result.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
        if isinstance(val, dict):
            for sub in ("url", "video_url", "videoUrl", "link"):
                u = val.get(sub)
                if isinstance(u, str) and u.startswith("http"):
                    return u
    for v in result.values():
        if isinstance(v, str) and v.startswith("http") and ".mp4" in v:
            return v
    return None


def _kling_generate(prompt: str, ratio: str, dest_path: str) -> bool | None:
    """
    Генерирует клип через Kling 2.6 via Kie.ai jobs API.
    Polling каждые 3 сек до succeeded/failed или 5 мин.
    Возвращает True при успехе, False при ошибке, None при 404.
    """
    from utils.providers import is_enabled, mark_dead, looks_like_no_credits

    api_key = os.environ.get("KIE_API_KEY", "")
    if not api_key or not is_enabled("kie"):
        return False

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 1. Создаём задачу через универсальный jobs/createTask
    try:
        resp = requests.post(
            f"{KIE_BASE_URL}/jobs/createTask",
            headers=headers,
            json={
                "model": "kling-2.6/text-to-video",
                "input": {
                    "prompt": prompt,
                    "aspect_ratio": ratio,
                    "duration": "5",
                    "sound": False,
                },
            },
            timeout=30,
        )
        if resp.status_code == 404:
            logger.warning("Kling: 404 — эндпоинт не найден")
            return None
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        logger.warning("Kling: HTTP ошибка: %s", exc)
        return False
    except Exception as exc:
        logger.warning("Kling: ошибка запуска: %s", exc)
        return False

    inner = data.get("data") or data
    task_id = inner.get("taskId") or inner.get("task_id") or inner.get("id")
    if not task_id:
        # 402 «Credits insufficient» → выключаем kie до конца процесса, не спамим
        if data.get("code") == 402 or looks_like_no_credits(data.get("msg", "")):
            mark_dead("kie", str(data.get("msg", ""))[:200])
        logger.warning("Kling: taskId не найден: %s", data)
        return False

    logger.info("Kling: taskId=%s, ожидаю...", task_id)

    # 2. Polling через queryTaskDetail
    result = _kie_poll_video(task_id, headers)
    if not result:
        return False

    video_url = _extract_video_url(result)
    if not video_url:
        logger.warning("Kling: video_url не найден в результате: %s", result)
        return False

    logger.info("Kling: готово → скачиваю %s", video_url)
    try:
        _download_clip(video_url, dest_path)
    except Exception as exc:
        logger.warning("Kling: скачивание ошибка: %s", exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Story mode helpers
# ---------------------------------------------------------------------------

def _generate_character_image(niche: str, topic: str) -> str | None:
    """Генерирует опорное фото персонажа через YandexART. Кэширует по hash темы."""
    os.makedirs(CHARS_DIR, exist_ok=True)
    cache_key = abs(hash(f"{niche}:{topic}")) % 10**8
    dest = os.path.abspath(os.path.join(CHARS_DIR, f"char_{cache_key}.jpg"))
    if os.path.exists(dest) and os.path.getsize(dest) > 5_000:
        logger.info("Story: персонаж из кэша — %s", os.path.basename(dest))
        return dest

    api_key = os.getenv("YANDEX_API_KEY", "")
    folder_id = os.getenv("YANDEX_FOLDER_ID", "")
    if not api_key or not folder_id:
        logger.warning("Story: YANDEX_API_KEY/FOLDER_ID не заданы — пропускаем генерацию персонажа")
        return None

    niche_clean = niche.strip().lower()
    char_prompt = NICHE_CHARACTER_PROMPTS.get(niche_clean, _DEFAULT_CHARACTER)

    body = {
        "modelUri": f"art://{folder_id}/yandex-art/latest",
        "generationOptions": {
            "seed": cache_key % 1_000_000,
            "aspectRatio": {"widthRatio": 9, "heightRatio": 16},
        },
        "messages": [{"weight": 1, "text": char_prompt}],
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
            return None
        for _ in range(30):
            time.sleep(2)
            r2 = requests.get(
                f"https://operation.api.cloud.yandex.net/operations/{operation_id}",
                headers={"Authorization": f"Api-Key {api_key}"},
                timeout=15,
            )
            data = r2.json()
            if data.get("done"):
                img_b64 = data.get("response", {}).get("image", "")
                if img_b64:
                    with open(dest, "wb") as f:
                        f.write(base64.b64decode(img_b64))
                    logger.info("Story: персонаж сгенерирован → %s", os.path.basename(dest))
                    return dest
                return None
        logger.warning("Story: YandexART timeout для персонажа")
        return None
    except Exception as exc:
        logger.warning("Story: ошибка генерации персонажа: %s", exc)
        return None


def _split_script_acts(script_text: str, niche: str) -> list[dict]:
    """Разбивает сценарий на нарративные акты через LLM.
    Возвращает список {act: 'hook'|'problem'|'insight'|'contrast'|'cta', text: str}.
    """
    raw = _llm_query(
        messages=[{"role": "user", "content": (
            "Split this video script into exactly 5 narrative acts: hook, problem, insight, contrast, cta.\n"
            "Return JSON array: [{\"act\": \"hook\", \"text\": \"...\"}, ...]\n"
            "Each text is the exact script fragment for that act. Cover all text, no overlap.\n"
            "Return ONLY valid JSON, no explanation.\n\n"
            f"Script:\n{script_text}"
        )}],
        temperature=0.2,
        max_tokens=600,
    )
    try:
        # Strip markdown code fences if present
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        acts = json.loads(cleaned)
        if isinstance(acts, list) and all("act" in a and "text" in a for a in acts):
            logger.info("Story: %d актов из сценария", len(acts))
            return acts
    except Exception as exc:
        logger.warning("Story: не удалось распарсить акты (%s) — fallback на равные части", exc)

    # Fallback: равномерное разбиение на 5 частей
    sentences = re.split(r'(?<=[.!?…])\s+', script_text.strip())
    n = len(sentences)
    chunk = max(1, n // 5)
    act_names = ["hook", "problem", "insight", "contrast", "cta"]
    acts = []
    for i, name in enumerate(act_names):
        start = i * chunk
        end = start + chunk if i < 4 else n
        text = " ".join(sentences[start:end]).strip()
        if text:
            acts.append({"act": name, "text": text})
    return acts


def _act_to_scene_prompt(act: dict, character_desc: str, niche: str) -> str:
    """Генерирует промпт для Kling image-to-video по нарративному акту."""
    act_name = act.get("act", "hook")
    act_text = act.get("text", "")[:200]
    base_action = ACT_SCENE_TEMPLATES.get(act_name, ACT_SCENE_TEMPLATES["hook"])
    niche_hint = f", {niche} context" if niche else ""

    raw = _llm_query(
        messages=[{"role": "user", "content": (
            f"Create a short Kling video prompt for this script act ({act_name}):\n\"{act_text}\"\n\n"
            f"The character: {character_desc}\n"
            f"Base action: {base_action}\n"
            f"Style: photorealistic{niche_hint}, bright daylight, no text, no watermarks, vertical 9:16.\n"
            "Max 60 words. Return ONLY the prompt."
        )}],
        temperature=0.4,
        max_tokens=90,
    )
    prompt = raw.strip().strip('"')
    return prompt[:400] if len(prompt) > 400 else prompt


def _kling_image_to_video(image_path: str, prompt: str, ratio: str, dest_path: str) -> bool | None:
    """Генерирует клип Kling image-to-video. Персонаж с image_path анимируется под prompt.
    Возвращает True при успехе, False при ошибке, None при 404/422 (модель недоступна).
    """
    from utils.providers import is_enabled, mark_dead, looks_like_no_credits

    api_key = os.environ.get("KIE_API_KEY", "")
    if not api_key or not is_enabled("kie"):
        return False

    # Кодируем изображение в base64
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        ext = os.path.splitext(image_path)[1].lower().lstrip(".")
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        image_data_url = f"data:{mime};base64,{img_b64}"
    except Exception as exc:
        logger.warning("Story: не удалось прочитать image_path: %s", exc)
        return False

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(
            f"{KIE_BASE_URL}/jobs/createTask",
            headers=headers,
            json={
                "model": "kling-2.0/image-to-video",
                "input": {
                    "image": image_data_url,
                    "prompt": prompt,
                    "aspect_ratio": ratio,
                    "duration": "5",
                },
            },
            timeout=30,
        )
        if resp.status_code in (404, 422):
            logger.warning("Story: image-to-video не поддерживается (HTTP %d) — fallback на text-to-video", resp.status_code)
            return None
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        logger.warning("Story: image-to-video HTTP ошибка: %s", exc)
        return False
    except Exception as exc:
        logger.warning("Story: image-to-video ошибка: %s", exc)
        return False

    inner = data.get("data") or data
    task_id = inner.get("taskId") or inner.get("task_id") or inner.get("id")
    if not task_id:
        if data.get("code") == 402 or looks_like_no_credits(data.get("msg", "")):
            mark_dead("kie", str(data.get("msg", ""))[:200])
        logger.warning("Story: taskId не найден: %s", data)
        return False

    logger.info("Story: Kling i2v taskId=%s ожидаю...", task_id)
    result = _kie_poll_video(task_id, headers)
    if not result:
        return False

    video_url = _extract_video_url(result)
    if not video_url:
        logger.warning("Story: video_url не найден: %s", result)
        return False

    logger.info("Story: готово → скачиваю %s", video_url)
    try:
        _download_clip(video_url, dest_path)
        return True
    except Exception as exc:
        logger.warning("Story: скачивание ошибка: %s", exc)
        return False


def _kling_story_clips(
    acts: list[dict],
    character_image_path: str | None,
    character_desc: str,
    niche: str,
    ratio: str,
) -> list[str]:
    """Генерирует story-клипы: один Kling image-to-video клип на каждый акт.
    Если image-to-video недоступен — fallback на text-to-video с описанием персонажа.
    """
    os.makedirs(CLIPS_DIR, exist_ok=True)
    clips: list[str] = []
    i2v_supported: bool | None = True  # предполагаем поддержку, пока не получим 404/422

    for i, act in enumerate(acts):
        act_name = act.get("act", f"act{i}")
        prompt = _act_to_scene_prompt(act, character_desc, niche)
        cache_key = abs(hash(f"{act_name}:{prompt}")) % 10**8
        dest = os.path.abspath(os.path.join(CLIPS_DIR, f"story_{cache_key}.mp4"))

        if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
            logger.info("Story: клип из кэша — %s", os.path.basename(dest))
            clips.append(dest)
            continue

        logger.info("Story [%d/%d] акт=%s: %r", i + 1, len(acts), act_name, prompt[:80])

        result = False
        if character_image_path and i2v_supported is not False:
            result = _kling_image_to_video(character_image_path, prompt, ratio, dest)
            if result is None:
                i2v_supported = False  # image-to-video недоступен — переключаемся на t2v

        if result is None or result is False:
            # text-to-video fallback с явным описанием персонажа в промпте
            t2v_prompt = f"{character_desc}, {prompt}"[:400]
            result = _kling_generate(t2v_prompt, ratio, dest)

        if result:
            clips.append(dest)
            try:
                from utils.cost_tracker import log_cost
                log_cost("visual_agent", "kling_story", "clip", 1)
            except Exception:
                pass
        else:
            logger.warning("Story: не удалось сгенерировать клип для акта '%s'", act_name)

    return clips


def _split_script_segments(script_text: str, max_segments: int) -> list[str]:
    """Разбивает сценарий на сегменты по 3-4 предложения каждый."""
    sentences = re.split(r'(?<=[.!?…])\s+', script_text.strip())
    sentences = [s for s in sentences if s.strip()]
    if not sentences:
        return [script_text[:300]]

    chunk_size = max(1, len(sentences) // max_segments)
    segments: list[str] = []
    for i in range(0, len(sentences), chunk_size):
        seg = " ".join(sentences[i : i + chunk_size]).strip()
        if seg:
            segments.append(seg)
        if len(segments) >= max_segments:
            break
    return segments or [script_text[:300]]


def _segment_to_kling_prompt(segment: str, niche: str = "") -> str:
    """Генерирует короткий кинематографический промпт для Kling через Groq."""
    niche_hint = f", {niche} themed" if niche else ""
    raw = _llm_query(
        messages=[{"role": "user", "content": (
            f"Generate a short cinematic video prompt in English for this script segment: \"{segment[:200]}\".\n"
            f"Style: professional{niche_hint}, no text, no watermarks, photorealistic, bright daylight.\n"
            "Max 50 words. Return ONLY the prompt, no explanations."
        )}],
        temperature=0.5,
        max_tokens=80,
    )
    prompt = raw.strip().strip('"')
    # Enforce hard cap (Kling has prompt length limits)
    if len(prompt) > 300:
        prompt = prompt[:300]
    return prompt


KLING_MAX_CLIPS = 8  # AI-генерация дорогая (~90 сек/клип) — ограничиваем

def _kling_prompts_from_script(script_text: str, niche: str) -> list[str]:
    """Разбивает сценарий на сегменты и генерирует уникальный промпт для каждого через Groq."""
    segments = _split_script_segments(script_text, max_segments=KLING_MAX_CLIPS)
    prompts: list[str] = []
    for seg in segments:
        try:
            p = _segment_to_kling_prompt(seg, niche)
            if p:
                prompts.append(p)
        except Exception as exc:
            logger.warning("Kling: промпт для сегмента ошибка: %s", exc)
    return prompts  # только реальные сегменты, без паддинга


def _kling_clips(queries: list[str], ratio: str, num_clips: int) -> list[str]:
    """Генерирует клипы через Kling по списку запросов."""
    os.makedirs(CLIPS_DIR, exist_ok=True)
    clips: list[str] = []

    for i, query in enumerate(queries):
        if len(clips) >= num_clips:
            break
        dest = os.path.abspath(
            os.path.join(CLIPS_DIR, f"kling_{abs(hash(query)) % 10**8:08d}.mp4")
        )
        if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
            logger.info("Kling клип уже есть: %s", os.path.basename(dest))
            clips.append(dest)
            continue
        logger.info("Kling [%d/%d] Генерирую: %r", i + 1, len(queries), query)
        result = _kling_generate(query, ratio, dest)
        if result is None:
            # 404 — эндпоинт недоступен, нет смысла пробовать остальные запросы
            logger.warning("Kling: эндпоинт недоступен — прекращаем попытки, переходим на Pexels")
            break
        if result:
            clips.append(dest)
            try:
                from utils.cost_tracker import log_cost
                log_cost("visual_agent", "kling", "clip", 1)
            except Exception:
                pass
        else:
            logger.warning("Kling: не удалось сгенерировать клип для '%s'", query)

    return clips


# ---------------------------------------------------------------------------
# Pexels
# ---------------------------------------------------------------------------

def _llm_query(messages: list[dict], temperature: float, max_tokens: int) -> str:
    """Call Groq with throttled Cerebras fallback on 429."""
    from utils.llm_client import groq_with_cerebras_fallback
    resp = groq_with_cerebras_fallback(
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        caller="visual_agent",
    )
    content = resp.choices[0].message.content
    if not content:
        # reasoning-модели (gpt-oss на Cerebras) иногда отдают content=None —
        # понятная ошибка вместо AttributeError, orchestrator сделает retry
        raise ValueError("LLM вернул пустой content для visual-запроса")
    return content


def _extract_query(topic: str) -> str:
    override = _niche_override(topic)
    if override:
        return override
    raw = _llm_query(
        messages=[{"role": "user", "content": (
            f'Stock video search query (3-4 English words) for topic: "{topic}". '
            "Avoid: office desk laptop, handshake, people smiling at camera, generic meeting. "
            "Prefer: emotions, nature, science, abstract concepts. "
            "Return ONLY the search string."
        )}],
        temperature=0.3,
        max_tokens=30,
    )
    return raw.strip().strip('"')


def _to_english_query(q: str) -> str:
    """Оставляет только ASCII-символы — Pexels ищет только на английском."""
    cleaned = re.sub(r'[^\x20-\x7E]', '', q).strip()
    cleaned = re.sub(r'[.,;:!?]+$', '', cleaned).strip()
    return cleaned


def _script_to_queries(script_text: str, topic: str = "", n: int = 5) -> list[str]:
    """Генерирует n разнообразных English-only поисковых запросов по блокам сценария."""
    niche_hint = _niche_override(topic)
    niche_guidance = f"The video topic is: '{topic}'."
    if niche_hint:
        niche_guidance += f" IMPORTANT: use visuals related to: {niche_hint}."

    raw = _llm_query(
        messages=[{"role": "user", "content": (
            f"{niche_guidance}\n\n"
            f"Read this video script and generate {n} diverse ENGLISH stock video search queries "
            "(3-4 English words each, NO Russian or other languages) "
            "for different visual moments in the script. "
            "AVOID: dark night street, rain city night, lonely figure darkness, dark alley, silhouette rain, "
            "night crowd concert, festival crowd night, protest crowd, crowd with phones night, "
            "generic urban crowd, anonymous office meeting, handshake, "
            "stressful urban environment, dark studio portrait, dark background person, isolated on black, "
            "food flatlay, diet choice, healthy food, medical equipment, glucose meter, "
            "past due bills, debt letter, financial stress, eviction notice, tax form, "
            "photo with large text, movie title card, magazine headline, film poster, book cover, "
            "person holding sign with text, people holding placard, handwritten sign board, hashtag sign, "
            "holding paper with writing, holding whiteboard text, sign saying, banner with words, "
            "dark market stall, outdoor bazaar dim, street vendor price sign, developing world market, "
            "indoor market crowded, rice market vendor, wet market, night market asia, "
            "business document flatlay, marketing strategy paper, sticky notes overhead, "
            "desk papers overhead, notebook flatlay, agenda printout, report document desk, "
            "neon sign text, glowing word sign, neon light quote, wall neon text, "
            "decorative quote sign, inspirational word tile, text on wooden block, letter tile, "
            "chalkboard quote, whiteboard text, motivational poster text, word art sign, "
            "dark gym interior, indoor gym night, moody gym lighting, bodybuilding dark room, dim gym weights, "
            "brain scan, MRI machine, hospital scanner, dark medical room, radiology equipment, "
            "person silhouette window dark, backlit silhouette indoor, man silhouette dark room, "
            "moody silhouette dramatic, dark room person shadow, shadow figure dark, "
            "person leaning wall dark, distressed man dark outdoor, overwhelmed person dark. "
            "PREFER: people in DAYLIGHT or bright indoor lighting, outdoor nature, modern bright office. "
            "REPLACE dark queries: 'stressful' → 'worried person daylight', 'dark' → 'dramatic sunlight', "
            "'overwhelmed' → 'focused person bright', 'staring blankly' → 'person thinking daylight', "
            "'distressed' → 'person reflecting outdoor sunlight', 'anxiety' → 'calm person outdoor'. "
            "REPLACE medical queries: 'brain scan' → 'thinking person portrait daylight', 'neuron' → 'abstract light network'. "
            "REPLACE market/price queries: 'price market' → 'shopping mall bright', 'store pricing' → 'modern retail store'. "
            "MANDATORY: every query MUST describe bright/daylight conditions. "
            "FORBIDDEN: any query implying darkness, shadow, silhouette, dim, moody, or night. "
            "PREFER BRIGHT WELL-LIT FOOTAGE. "
            "For psychology/mind: person thinking daylight portrait, human face emotion bright, calm person outdoor, focused mind outdoor, thinking person sunlight. "
            "For business: entrepreneur bright office, success daylight, professional growth outdoor. "
            "For health: fitness workout bright, healthy lifestyle outdoor, wellness nature sunlight. "
            f"Return exactly {n} queries in ENGLISH ONLY, one per line, no numbers or bullets.\n\n"
            f"Script: {script_text[:600]}"
        )}],
        temperature=0.7,
        max_tokens=120,
    )
    lines = raw.strip().split('\n')
    queries_raw = [re.sub(r'^[\d\.\-\*\s]+', '', l).strip().strip('"') for l in lines if l.strip()]
    # Drop preamble lines ("Here are 5 queries...") — too long to be a search query
    queries_raw = [q for q in queries_raw if len(q.split()) <= 6]
    # Ensure ASCII-only (filter out any Cyrillic that snuck in)
    queries = [_to_english_query(q) for q in queries_raw]
    queries = [q for q in queries if len(q) > 3]
    # Pad with niche fallback combos (parsed niche keywords + well-lit abstracts)
    if len(queries) < n:
        fallback_pool = [
            "person thinking daylight",
            "abstract motion bright",
            "slow motion person walking sunlight",
            "sunset golden hour landscape",
            "nature forest sunlight bright",
            "concept visualization colorful",
        ]
        if niche_hint:
            kws = niche_hint.split()
            fallback_pool = [f"{kws[0]} closeup", f"{kws[-1]} cinematic"] + fallback_pool
        for fb in fallback_pool:
            if len(queries) >= n:
                break
            if fb not in queries:
                queries.append(fb)
    return queries[:n]


def _search_pexels(query: str, orientation: str, per_page: int = 10) -> list[dict]:
    api_key = os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        raise EnvironmentError("PEXELS_API_KEY не задан в .env")
    resp = requests.get(
        PEXELS_API_URL,
        headers={"Authorization": api_key},
        params={"query": query, "orientation": orientation, "per_page": per_page},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("videos", [])


def _pick_best_file(video: dict, preferred_width: int, preferred_height: int) -> str | None:
    """
    Выбирает наилучший рендер клипа.
    Для portrait — отбрасываем все landscape-файлы (где width >= height).
    Для landscape — наоборот.
    """
    files = video.get("video_files", [])
    if not files:
        return None

    want_portrait = preferred_height > preferred_width
    filtered = [
        f for f in files
        if f.get("width") and f.get("height")
        and ((f["height"] > f["width"]) == want_portrait)
    ]
    if not filtered:
        return None  # не нашли файлы нужной ориентации — пропускаем видео целиком

    def score(f: dict) -> int:
        # минимизируем разницу с целевым разрешением, но штрафуем слишком большие (>2x)
        w_diff = abs(f.get("width", 0) - preferred_width)
        h_diff = abs(f.get("height", 0) - preferred_height)
        oversize = max(0, f.get("width", 0) - preferred_width * 2) * 2
        return w_diff + h_diff + oversize

    return min(filtered, key=score).get("link")


def _is_portrait_clip(video: dict) -> bool:
    """Pexels иногда отдаёт landscape под portrait-запросом. Фильтруем по top-level метаданным."""
    w = video.get("width", 0)
    h = video.get("height", 0)
    if not w or not h:
        return True  # без метаданных — даём шанс
    return h > w


def _is_landscape_clip(video: dict) -> bool:
    w = video.get("width", 0)
    h = video.get("height", 0)
    if not w or not h:
        return True
    return w > h


def _pexels_clips(
    queries: list[str],
    orientation: str,
    pref_w: int,
    pref_h: int,
    num_clips: int,
    per_query: bool = True,
) -> tuple[list[str], str]:
    """Скачивает клипы с Pexels по списку запросов. Возвращает (clips, used_query).

    Жёстко фильтрует ориентацию (Pexels часто отдаёт landscape на portrait-запрос).
    Берёт клипы 5-15 сек (слишком длинные — лишний траффик, слишком короткие — мало материала).
    """
    os.makedirs(CLIPS_DIR, exist_ok=True)
    clips: list[str] = []
    used_ids: set[int] = set()  # дедупликация — каждый клип только один раз

    want_portrait = orientation == "portrait"
    orient_filter = _is_portrait_clip if want_portrait else _is_landscape_clip

    for query in queries:
        if len(clips) >= num_clips:
            break
        try:
            # Запрашиваем больше — будем отбрасывать неправильную ориентацию
            videos = _search_pexels(query, orientation, per_page=10 if per_query else 25)
        except Exception as exc:
            logger.warning("Pexels поиск '%s' ошибка: %s", query, exc)
            continue

        picked_for_query = 0
        for video in videos:
            vid_id = video.get("id")
            if vid_id in used_ids:
                continue
            if not orient_filter(video):
                continue  # отбраковываем landscape под portrait и наоборот
            dur = video.get("duration", 0)
            if dur and (dur < 4 or dur > 25):
                continue  # слишком короткие/длинные клипы
            url = _pick_best_file(video, pref_w, pref_h)
            if not url:
                continue
            clip_path = os.path.abspath(os.path.join(CLIPS_DIR, f"pexels_{vid_id}.mp4"))
            used_ids.add(vid_id)
            if is_cached(clip_path):
                logger.debug("Клип %s уже скачан", vid_id)
                clips.append(clip_path)
            else:
                logger.info("[%s] Скачиваю клип %s (%s сек)...", query, vid_id, dur or "?")
                try:
                    _download_clip(url, clip_path)
                    clips.append(clip_path)
                except Exception as exc:
                    logger.warning("Не удалось скачать клип %s: %s", vid_id, exc)
                    used_ids.discard(vid_id)
                    continue
            picked_for_query += 1
            # With many queries (≥ num_clips/2), limit 1 per query for maximum diversity.
            # With fewer queries, allow 2 per query as a fallback to fill the pool.
            per_query_limit = 1 if (per_query and len(queries) >= num_clips // 2) else 2
            if per_query and picked_for_query >= per_query_limit:
                break
            if len(clips) >= num_clips:
                break

    return clips, ", ".join(queries[:3])


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def get_visuals(
    topic: str,
    platform: Platform,
    num_clips: int = 14,
    script_text: str | None = None,
    niche: str = "",
) -> dict:
    """
    Подбирает видеоклипы для темы.

    Логика:
      1. Если KIE_API_KEY задан → Kling 2.6 генерирует AI-клипы (сегментные промпты)
      2. Иначе → Pexels stock footage
         a. С script_text → запросы по блокам сценария
         b. Без script_text → один запрос по теме/нише

    Returns:
        {"clips": [...], "source": "kling"|"pexels", "query": "..."}
    """
    if platform not in PLATFORM_ORIENTATION:
        raise ValueError(f"Неподдерживаемая платформа: {platform}")

    orientation = PLATFORM_ORIENTATION[platform]
    pref_w, pref_h = PLATFORM_PREFERRED_SIZE[orientation]
    os.makedirs(CLIPS_DIR, exist_ok=True)

    niche_clean = niche.strip().lower() if niche else ""

    # ── Kling story mode (если ключ есть + сценарий; платный — гейтится) ──
    from utils.providers import is_enabled
    kie_key = os.environ.get("KIE_API_KEY", "") if is_enabled("kie") else ""
    if kie_key and script_text:
        ratio = KLING_RATIO[orientation]

        # 1. Генерируем опорный образ персонажа (кэшируется)
        character_image = _generate_character_image(niche_clean, topic)
        niche_char_prompt = NICHE_CHARACTER_PROMPTS.get(niche_clean, _DEFAULT_CHARACTER)
        # Компактное описание для text-to-video fallback
        character_desc = niche_char_prompt.split(",")[:3]
        character_desc = ", ".join(character_desc).strip()

        # 2. Делим сценарий на нарративные акты
        logger.info("Story mode: разбиваю сценарий на нарративные акты...")
        acts = _split_script_acts(script_text, niche_clean)
        logger.info("Story mode: %d актов: %s", len(acts), [a["act"] for a in acts])

        # 3. Генерируем клипы — по одному на акт
        clips = _kling_story_clips(acts, character_image, character_desc, niche_clean, ratio)

        if clips:
            act_labels = ", ".join(a["act"] for a in acts[:3])
            return {"clips": clips, "source": "kling_story", "query": act_labels}

        logger.warning("Story mode: 0 клипов — переключаюсь на Pexels")

    elif kie_key and not script_text:
        # Без сценария — старый text-to-video по теме
        ratio = KLING_RATIO[orientation]
        hint = _niche_override(topic)
        base = hint or topic
        prompts = [f"{base} cinematic bright daylight photorealistic" for _ in range(KLING_MAX_CLIPS)]
        clips = _kling_clips(prompts, ratio, len(prompts))
        if clips:
            return {"clips": clips, "source": "kling", "query": ", ".join(prompts[:3])}
        logger.warning("Kling вернул 0 клипов — переключаюсь на Pexels")

    # ── Pexels fallback ────────────────────────────────────────────────────
    if script_text:
        logger.info("Pexels: генерирую %d запросов по сценарию...", num_clips)
        queries = _script_to_queries(script_text, topic=topic, n=num_clips)
        logger.info("Pexels запросы: %s", queries)
        clips, used_query = _pexels_clips(queries, orientation, pref_w, pref_h, num_clips, per_query=True)
        if clips:
            return {"clips": clips, "source": "pexels", "query": used_query}

    # Один запрос по теме/нише
    query = _extract_query(topic)
    logger.info("Pexels: ищу клипы для '%s' (запрос: '%s')", topic, query)
    videos = _search_pexels(query, orientation, per_page=num_clips * 2)
    if not videos:
        raise RuntimeError(f"Pexels не вернул результатов по запросу: '{query}'")

    clips = []
    for video in videos:
        if len(clips) >= num_clips:
            break
        url = _pick_best_file(video, pref_w, pref_h)
        if not url:
            continue
        clip_path = os.path.abspath(os.path.join(CLIPS_DIR, f"pexels_{video['id']}.mp4"))
        if is_cached(clip_path):
            logger.debug("Клип %s уже скачан", video["id"])
            clips.append(clip_path)
        else:
            logger.info("Скачиваю клип %s (%s сек)...", video["id"], video.get("duration", "?"))
            _download_clip(url, clip_path)
            clips.append(clip_path)

    if not clips:
        raise RuntimeError("Не удалось скачать ни одного клипа")

    return {"clips": clips, "source": "pexels", "query": query}


# ---------------------------------------------------------------------------
# Фото для photo-burst (Pexels Photos + Unsplash)
# ---------------------------------------------------------------------------

# Минимальное разрешение для портретного фото — иначе после crop'а будет мыло
MIN_PHOTO_W = 800
MIN_PHOTO_H = 1100  # ~3:4 минимум


def _ahash_8(path: str) -> int | None:
    """
    Perceptual hash 8x8 для дедупликации фото.
    Возвращает 64-битный hash или None при ошибке.
    Близкие фото отличаются на <8 бит (Hamming distance).
    """
    try:
        from PIL import Image
        with Image.open(path) as img:
            small = img.convert("L").resize((8, 8))
        pixels = list(small.getdata())
        avg = sum(pixels) / len(pixels)
        bits = 0
        for i, p in enumerate(pixels):
            if p > avg:
                bits |= (1 << i)
        return bits
    except Exception:
        return None


def _photo_quality_ok(path: str) -> bool:
    """Проверяет min-resolution, ориентацию, яркость и отсутствие знаков/плакатов."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
            if w < MIN_PHOTO_W or h < MIN_PHOTO_H:
                return False
            if w >= h:
                return False
            # Reject dark photos (avg brightness < 55/255)
            gray = img.convert("L").resize((64, 64), Image.LANCZOS)
            gray_data = list(gray.getdata())
            avg_brightness = sum(gray_data) / len(gray_data)
            if avg_brightness < 55:
                return False
            # Reject sign-holder / placard photos: large near-white rectangle in lower half.
            # Check bottom 50% of image — if >45% pixels are near-white (>220) it's a sign.
            rgb_small = img.convert("RGB").resize((32, 64), Image.LANCZOS)
            pixels = list(rgb_small.getdata())
            bottom_half = pixels[32 * 32:]  # lower 32 rows out of 64
            near_white = sum(1 for r, g, b in bottom_half if r > 220 and g > 220 and b > 220)
            if near_white / len(bottom_half) > 0.45:
                return False
        return True
    except Exception:
        return False


def _dedup_filter_photos(paths: list[str], threshold_bits: int = 6) -> list[str]:
    """
    Возвращает дедуплицированный (perceptual hash) и качественный (min-res)
    список фото. threshold_bits=6 — два фото считаются дубликатами, если
    отличаются в ≤6 битах из 64 (примерно 90% похожести).
    """
    result: list[str] = []
    hashes: list[int] = []
    for p in paths:
        if not _photo_quality_ok(p):
            logger.debug("Photo dropped (low quality): %s", os.path.basename(p))
            continue
        h = _ahash_8(p)
        if h is None:
            continue
        # Проверяем расстояние Хэмминга против всех уже принятых
        is_dup = any(bin(h ^ prev).count("1") <= threshold_bits for prev in hashes)
        if is_dup:
            logger.debug("Photo dropped (duplicate): %s", os.path.basename(p))
            continue
        result.append(p)
        hashes.append(h)
    if len(result) < len(paths):
        logger.info("Photo dedup/filter: %d → %d", len(paths), len(result))
    return result

def _pexels_photos(query: str, n: int, orientation: str = "portrait") -> list[str]:
    """Скачивает n фото с Pexels Photos API (тот же ключ что и для видео)."""
    key = os.environ.get("PEXELS_API_KEY", "")
    if not key:
        return []
    try:
        resp = requests.get(
            PEXELS_PHOTOS_URL,
            headers={"Authorization": key},
            params={"query": query, "per_page": n * 2, "orientation": orientation},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        photos = resp.json().get("photos", [])
    except Exception as exc:
        logger.warning("Pexels Photos error: %s", exc)
        return []

    paths: list[str] = []
    for ph in photos:
        if len(paths) >= n:
            break
        src = ph.get("src", {}).get("large2x") or ph.get("src", {}).get("large")
        if not src:
            continue
        vid = ph.get("id")
        dest = os.path.abspath(os.path.join(PHOTOS_DIR, f"pexels_photo_{vid}.jpg"))
        if os.path.exists(dest) and os.path.getsize(dest) > 5_000:
            paths.append(dest)
            continue
        try:
            _download_clip(src, dest)
            if os.path.getsize(dest) > 5_000:
                paths.append(dest)
        except Exception as exc:
            logger.warning("Pexels photo download fail %s: %s", vid, exc)
    return paths


def _unsplash_photos(query: str, n: int, orientation: str = "portrait") -> list[str]:
    """Скачивает n фото с Unsplash API (требует UNSPLASH_ACCESS_KEY)."""
    key = os.environ.get("UNSPLASH_ACCESS_KEY", "")
    if not key:
        return []
    try:
        resp = requests.get(
            UNSPLASH_API_URL,
            params={
                "query": query,
                "per_page": n * 2,
                "orientation": orientation,
                "client_id": key,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        results = resp.json().get("results", [])
    except Exception as exc:
        logger.warning("Unsplash error: %s", exc)
        return []

    paths: list[str] = []
    for ph in results:
        if len(paths) >= n:
            break
        src = ph.get("urls", {}).get("regular")
        if not src:
            continue
        pid = ph.get("id")
        dest = os.path.abspath(os.path.join(PHOTOS_DIR, f"unsplash_{pid}.jpg"))
        if os.path.exists(dest) and os.path.getsize(dest) > 5_000:
            paths.append(dest)
            continue
        try:
            _download_clip(src, dest)
            if os.path.getsize(dest) > 5_000:
                paths.append(dest)
        except Exception as exc:
            logger.warning("Unsplash download fail %s: %s", pid, exc)
    return paths


def get_photos(
    topic: str,
    platform: Platform = "tiktok",
    n: int = 6,
    script_text: str | None = None,
) -> dict:
    """
    Подбирает n фото для photo-burst.

    Источники: Pexels Photos → Unsplash (если задан UNSPLASH_ACCESS_KEY).
    Если script_text задан — генерирует n разнообразных запросов по сценарию.
    Иначе — один запрос по теме.

    Returns:
        {"photos": [paths], "source": "pexels"|"unsplash"|"mixed", "queries": [...]}
    """
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    orientation = PLATFORM_ORIENTATION.get(platform, "portrait")

    if script_text:
        queries = _script_to_queries(script_text, topic=topic, n=min(n, 6))
    else:
        hint = _niche_override(topic)
        base = hint or topic
        queries = [base] + [f"{base} closeup", f"{base} cinematic"]

    photos: list[str] = []
    sources_used: set[str] = set()

    # Pexels Photos — основной источник
    for q in queries:
        if len(photos) >= n:
            break
        new = _pexels_photos(q, n=2, orientation=orientation)
        for p in new:
            if p not in photos:
                photos.append(p)
                sources_used.add("pexels")
            if len(photos) >= n:
                break

    # Unsplash — добор если не хватает
    if len(photos) < n:
        for q in queries:
            if len(photos) >= n:
                break
            new = _unsplash_photos(q, n=2, orientation=orientation)
            for p in new:
                if p not in photos:
                    photos.append(p)
                    sources_used.add("unsplash")
                if len(photos) >= n:
                    break

    # Дедуп + quality filter
    photos = _dedup_filter_photos(photos)

    source_label = (
        "mixed" if len(sources_used) > 1
        else (next(iter(sources_used)) if sources_used else "none")
    )
    logger.info("get_photos: %d фото из %s", len(photos), source_label)
    return {"photos": photos[:n], "source": source_label, "queries": queries[:3]}


if __name__ == "__main__":
    import sys
    import logging

    logging.basicConfig(level=logging.INFO)
    topic = sys.argv[1] if len(sys.argv) > 1 else "психология прокрастинации"
    platform = sys.argv[2] if len(sys.argv) > 2 else "tiktok"
    num_clips = int(sys.argv[3]) if len(sys.argv) > 3 else 3

    print(f"Ищу визуалы: '{topic}' для {platform} ({num_clips} клипа)...\n")
    result = get_visuals(topic, platform, num_clips)
    print(json.dumps(result, ensure_ascii=False, indent=2))
