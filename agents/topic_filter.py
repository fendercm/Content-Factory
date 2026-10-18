"""
Topic Filter Agent — фильтрует темы: дедупликация по SQLite + safety-check через Gemini.

Вход:  ideas (list[dict]) — [{title, hook, angle, hashtags, source_topic, virality}]
         platform (str) — целевая платформа для safety-check
Выход: отфильтрованный список идей (те же поля)

Safety fail-closed: Gemini → Groq/Cerebras; если ни один движок не ответил
разборчиво — RuntimeError (Orchestrator ретраит шаг). Идея, по которой
движок не вернул вердикт, считается небезопасной.
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
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

Для каждой идеи (по полю id) ответь: safe (true/false) и reason (краткая причина, если false).
Верни JSON-массив, по одному элементу на КАЖДЫЙ id:
[{{"id": 1, "safe": true/false, "reason": "..."}}]
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
    data = json.loads(raw)
    # json_object-режим некоторых моделей заворачивает массив в объект
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    if not isinstance(data, list):
        raise ValueError(f"ожидался JSON-массив: {raw[:120]}")
    return data


def _as_bool(value: Any) -> bool | None:
    """LLM иногда присылает "false" строкой — а bool("false") == True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "1"):
            return True
        if v in ("false", "no", "0"):
            return False
    return None


def _safety_prompt(ideas: list[dict[str, Any]], platform: str) -> str:
    ideas_json = json.dumps(
        [{"id": n, "title": i["title"], "hook": i.get("hook", "")} for n, i in enumerate(ideas, 1)],
        ensure_ascii=False, indent=2,
    )
    return _SAFETY_PROMPT.format(platform=platform, ideas_json=ideas_json)


def _to_safety_list(results: list[dict], ideas: list[dict[str, Any]], engine: str) -> list[bool]:
    """
    Сопоставляет вердикты с идеями по id. Идея без однозначного вердикта —
    небезопасна (fail-closed): раньше совпадение шло по title, и любая
    перефразировка заголовка моделью означала «safe» по умолчанию.
    """
    verdicts: dict[int, bool] = {}
    for item in results:
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        safe = _as_bool(item.get("safe"))
        if safe is not None:
            verdicts[idx] = safe
            if not safe and 1 <= idx <= len(ideas):
                logger.info("Unsafe (%s): '%s' — %s", engine, ideas[idx - 1]["title"], item.get("reason", ""))

    flags = []
    for n, idea in enumerate(ideas, 1):
        if n not in verdicts:
            logger.warning("Safety (%s): нет вердикта для '%s' — считаем небезопасной", engine, idea["title"])
        flags.append(verdicts.get(n, False))
    return flags


def _llm_chain_safety(ideas: list[dict[str, Any]], platform: str) -> list[bool]:
    """Резерв к Gemini: Groq → Cerebras через общий клиент. Ошибка — наверх."""
    from utils.llm_client import groq_with_cerebras_fallback
    response = groq_with_cerebras_fallback(
        messages=[{"role": "user", "content": _safety_prompt(ideas, platform)}],
        temperature=0.1,
        max_tokens=512,
        caller="topic_filter_safety",
    )
    results = _parse_safety_json(response.choices[0].message.content.strip())
    return _to_safety_list(results, ideas, "Groq/Cerebras")


def _check_safety(ideas: list[dict[str, Any]], platform: str) -> list[bool]:
    """Возвращает список флагов is_safe в порядке ideas. Если проверить нечем — RuntimeError."""
    from utils.providers import is_enabled, mark_dead

    if GEMINI_API_KEY and is_enabled("gemini"):
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=_safety_prompt(ideas, platform),
            )
            results = _parse_safety_json(response.text.strip())
            return _to_safety_list(results, ideas, "Gemini")
        except Exception as exc:
            err = str(exc)
            if "limit: 0" in err or "RESOURCE_EXHAUSTED" in err:
                mark_dead("gemini", err[:200])
            logger.warning("Gemini safety-check ошибка: %s — переключаюсь на Groq/Cerebras", err[:200])
    else:
        logger.info("Gemini недоступен — safety-check через Groq/Cerebras")

    try:
        return _llm_chain_safety(ideas, platform)
    except Exception as exc:
        raise RuntimeError(f"Safety-check тем не выполнен ни одним движком: {exc}") from exc


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

    # 3. Safety-check (Gemini → Groq/Cerebras), fail-closed
    safety_flags = _check_safety(fresh, platform)
    safe_ideas = [i for i, safe in zip(fresh, safety_flags) if safe]

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
