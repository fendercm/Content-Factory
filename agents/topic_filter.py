"""
Topic Filter Agent — фильтрует темы: дедупликация по SQLite + safety-check через Gemini.

Вход:  ideas (list[dict]) — [{title, hook, angle, hashtags, source_topic, virality}]
         platform (str) — целевая платформа для safety-check
Выход: отфильтрованный список идей (те же поля)
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "pipeline.db")
DEDUP_DAYS = 30

_SAFETY_PROMPT = """Ты — модератор контента для социальных сетей ({platform}).
Проверь следующие идеи видео на безопасность для публикации.

СТОП-КАТЕГОРИИ — safe=false если идея попадает в любую:
1. ПОЛИТИКА: политики, выборы, войны, военные действия, оппозиция, санкции,
   геополитические конфликты, "Путин", "Зеленский", "Трамп" и т.п.
2. МЕДИЦИНСКИЕ СОВЕТЫ: лечение конкретных болезней, диагнозы,
   рекомендации дозировок лекарств, "вылечить рак/диабет/депрессию",
   замена врача. Общая популярная психология/привычки — ОК.
3. 18+ / ЖЁСТКИЙ КОНТЕНТ: секс, эротика, наркотики, суицид,
   подробное насилие, расчленение, экстремизм.

ОК — пропускай эти темы как safe=true:
- Психология поведения, привычки, общение, продуктивность, отношения
- Бизнес, карьера, деньги (без инвест-советов с конкретными активами)
- Наука, история, технологии, философия (классики)
- Здоровый образ жизни в общем виде (без диагностики)

Идеи:
{ideas_json}

Для каждой идеи (по полю title) ответь: safe (true/false) и reason (краткая причина, если false).
Верни JSON-массив:
[{{"title": "...", "safe": true/false, "reason": "..."}}]
Только JSON, без markdown."""

_NICHE_RELEVANCE_PROMPT = """Проверь, относится ли каждая идея видео к нише «{niche}».

Идеи:
{ideas_json}

Для каждой идеи ответь: relevant (true/false).
true — тема реально про «{niche}» (психология, поведение, мышление, эмоции и т.п. если ниша психология).
false — тема про что-то другое (технологии, политика, iPhone, спорт и т.п. если ниша психология).

Верни JSON-массив:
[{{"title": "...", "relevant": true/false}}]
Только JSON, без markdown."""


def _ensure_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topics_used (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            published_at TEXT NOT NULL
        )
    """)
    conn.commit()


def _is_duplicate(conn: sqlite3.Connection, topic: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM topics_used WHERE topic = ? AND published_at > ?",
        (topic.lower().strip(), cutoff),
    ).fetchone()
    return row is not None


def _parse_safety_json(raw: str) -> list[dict]:
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def _cerebras_safety(ideas: list[dict[str, Any]], platform: str) -> dict[str, bool]:
    """Cerebras fallback когда Gemini возвращает 429."""
    if not CEREBRAS_API_KEY:
        logger.warning("CEREBRAS_API_KEY не задан — пропускаем safety-check")
        return {idea["title"]: True for idea in ideas}

    ideas_json = json.dumps(
        [{"title": i["title"], "hook": i["hook"]} for i in ideas],
        ensure_ascii=False, indent=2,
    )
    prompt = _SAFETY_PROMPT.format(platform=platform, ideas_json=ideas_json)

    try:
        from utils.llm_client import cerebras_call
        response = cerebras_call(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=512,
            caller="topic_filter_safety",
        )
        results = _parse_safety_json(response.choices[0].message.content.strip())
        safety_map = {}
        for item in results:
            title = item.get("title", "")
            safe = bool(item.get("safe", True))
            if not safe:
                logger.info("Unsafe (Cerebras): '%s' — %s", title, item.get("reason", ""))
            safety_map[title] = safe
        return safety_map
    except Exception as exc:
        logger.warning("Cerebras safety-check ошибка: %s — считаем всё безопасным", exc)
        return {idea["title"]: True for idea in ideas}


def _check_safety(ideas: list[dict[str, Any]], platform: str) -> dict[str, bool]:
    """Возвращает {title: is_safe}."""
    from utils.providers import is_enabled, mark_dead

    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY не задан — пропускаем safety-check, считаем всё безопасным")
        return {idea["title"]: True for idea in ideas}
    if not is_enabled("gemini"):
        logger.info("Gemini выключен (квота) — safety-check через Cerebras")
        return _cerebras_safety(ideas, platform)

    client = genai.Client(api_key=GEMINI_API_KEY)

    ideas_json = json.dumps(
        [{"title": i["title"], "hook": i["hook"]} for i in ideas],
        ensure_ascii=False,
        indent=2,
    )
    prompt = _SAFETY_PROMPT.format(platform=platform, ideas_json=ideas_json)

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        raw = response.text.strip()
        results: list[dict] = _parse_safety_json(raw)
        safety_map = {}
        for item in results:
            title = item.get("title", "")
            safe = bool(item.get("safe", True))
            if not safe:
                logger.info("Unsafe: '%s' — %s", title, item.get("reason", ""))
            safety_map[title] = safe
        return safety_map

    except Exception as exc:
        err = str(exc)
        if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
            if "limit: 0" in err or "RESOURCE_EXHAUSTED" in err:
                mark_dead("gemini", err[:200])
            logger.warning("Gemini 429 — переключаемся на Cerebras для safety-check")
            return _cerebras_safety(ideas, platform)
        logger.warning("Gemini safety-check ошибка: %s — считаем всё безопасным", exc)
        return {idea["title"]: True for idea in ideas}


def _check_niche_relevance(ideas: list[dict[str, Any]], niche: str) -> dict[str, bool]:
    """Проверяет соответствие идей нише через Cerebras (быстрый и дешёвый)."""
    if not niche:
        return {idea["title"]: True for idea in ideas}

    if not CEREBRAS_API_KEY:
        logger.warning("CEREBRAS_API_KEY не задан — пропускаем niche-check")
        return {idea["title"]: True for idea in ideas}

    ideas_json = json.dumps([{"title": i["title"]} for i in ideas], ensure_ascii=False, indent=2)
    prompt = _NICHE_RELEVANCE_PROMPT.format(niche=niche, ideas_json=ideas_json)
    try:
        from utils.llm_client import cerebras_call
        response = cerebras_call(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=256,
            caller="topic_filter_niche",
        )
        results = _parse_safety_json(response.choices[0].message.content.strip())
        relevance_map = {}
        for item in results:
            title = item.get("title", "")
            relevant = bool(item.get("relevant", True))
            if not relevant:
                logger.info("Нерелевантно нише '%s': '%s' — пропускаем", niche, title)
            relevance_map[title] = relevant
        return relevance_map
    except Exception as exc:
        logger.warning("Niche-check ошибка: %s — считаем всё релевантным", exc)
        return {idea["title"]: True for idea in ideas}


def run(
    ideas: list[dict[str, Any]],
    platform: str = "tiktok",
    db_path: str | None = None,
    niche: str = "",
) -> list[dict[str, Any]]:
    """Фильтрует идеи: убирает дубли за 30 дней, нерелевантные нише и небезопасный контент."""
    if not ideas:
        return []

    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    _ensure_db(conn)

    # 1. Дедупликация по SQLite
    fresh: list[dict[str, Any]] = []
    for idea in ideas:
        topic_key = idea.get("source_topic", idea.get("title", ""))
        if _is_duplicate(conn, topic_key):
            logger.info("Дубль (30 дней): '%s' — пропускаем", topic_key)
        else:
            fresh.append(idea)

    conn.close()
    dedup_count = len(fresh)

    if not fresh:
        logger.info("Все темы уже публиковались за последние %d дней", DEDUP_DAYS)
        return []

    # 2. Niche-check — отсеиваем нерелевантные темы
    if niche:
        relevance_map = _check_niche_relevance(fresh, niche)
        relevant = [i for i in fresh if relevance_map.get(i["title"], True)]
        if not relevant:
            logger.warning("Niche-check: все идеи нерелевантны нише '%s' — пропускаем фильтр", niche)
            relevant = fresh  # если все отклонены — не блокируем пайплайн
        fresh = relevant

    niche_count = len(fresh)

    # 3. Safety-check через Gemini
    safety_map = _check_safety(fresh, platform)
    safe_ideas = [i for i in fresh if safety_map.get(i["title"], True)]

    logger.info(
        "Topic Filter: входило %d -> dedup %d -> niche %d -> safety %d",
        len(ideas), dedup_count, niche_count, len(safe_ideas),
    )
    return safe_ideas


def mark_published(topic: str, db_path: str | None = None) -> None:
    """Отмечает тему как опубликованную (вызывается после успешной публикации)."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    _ensure_db(conn)
    conn.execute(
        "INSERT INTO topics_used (topic, published_at) VALUES (?, ?)",
        (topic.lower().strip(), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    logger.info("Тема отмечена как опубликованная: '%s'", topic)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    sample_ideas = [
        {
            "title": "Почему мозг обманывает нас каждый день",
            "hook": "Знаешь ли ты, что твой мозг врёт тебе прямо сейчас?",
            "angle": "Нейробиология в доступном формате",
            "hashtags": ["психология", "мозг", "наука", "psychology", "brain"],
            "source_topic": "психология манипуляций",
            "virality": 0.85,
        }
    ]

    platform_arg = sys.argv[1] if len(sys.argv) > 1 else "tiktok"
    result = run(sample_ideas, platform=platform_arg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
