"""
Topic Filter Agent — фильтрует темы: дедупликация по SQLite + niche-check + safety-check.

Вход:  ideas (list[dict]) — [{title, hook, angle, hashtags, source_topic, virality}]
         platform (str) — целевая платформа для safety-check
Выход: отфильтрованный список идей (те же поля)

LLM — общая цепочка utils.llm_client.chat (Gemini Flash-Lite → Groq → … → Gemma).
Safety fail-closed: если цепочка не ответила разборчиво — RuntimeError
(Orchestrator ретраит шаг). Идея без вердикта считается небезопасной.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "pipeline.db")
DEDUP_DAYS = 30

_SAFETY_PROMPT = """Ты — модератор идей для коротких видео ({platform}). Оцени каждую идею ДО написания сценария.

safe=false, если идея по сути про одно из:
1. ПОЛИТИКА: политики, выборы, войны и военные действия, оппозиция, санкции, геополитика.
2. МЕДИЦИНСКИЕ СОВЕТЫ: диагнозы, лечение конкретных болезней, лекарства и дозировки,
   «вылечить депрессию/диабет», замена врача.
3. ФИНАНСОВЫЕ СОВЕТЫ: купить/продать конкретные активы, обещания дохода, схемы заработка.
4. 18+ И ВРЕД: секс, наркотики, суицид и самоповреждение, жёсткое насилие, опасные челленджи.
5. ВРАЖДА к группам людей; шок и запугивание ради просмотров.

safe=true для: психологии поведения, привычек, общения, продуктивности, отношений,
карьеры и денег без советов по активам, науки, истории, технологий, философии,
здорового образа жизни в общем виде.
Сомневаешься — safe=false: у нас есть другие идеи.

Идеи:
{ideas_json}

Верни JSON-объект с вердиктом для КАЖДОГО id:
{{"results": [{{"id": 1, "safe": true, "reason": "кратко, если false"}}]}}"""

_NICHE_RELEVANCE_PROMPT = """Ниша канала: «{niche}». Зритель подписан ради этой ниши.

Для каждой идеи реши: relevant=true, если зритель ниши «{niche}» воспримет её как «своё»
(основная мысль видео — про нишу, а не про случайный инфоповод).
relevant=false, если ниша упомянута для галочки, а суть — про гаджеты, знаменитостей,
спорт, политику или новость без связи с нишей.

Идеи:
{ideas_json}

Верни JSON-объект: {{"results": [{{"id": 1, "relevant": true}}]}}"""


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
    raw = raw.strip()
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


def _ideas_json(ideas: list[dict[str, Any]]) -> str:
    return json.dumps(
        [{"id": n, "title": i["title"], "hook": i.get("hook", ""), "angle": i.get("angle", "")}
         for n, i in enumerate(ideas, 1)],
        ensure_ascii=False, indent=2,
    )


def _safety_prompt(ideas: list[dict[str, Any]], platform: str) -> str:
    return _SAFETY_PROMPT.format(platform=platform, ideas_json=_ideas_json(ideas))


def _ask(prompt: str, caller: str) -> tuple[list[dict], str]:
    from utils.llm_client import chat
    resp = chat([{"role": "user", "content": prompt}], tier="fast", temperature=0.0,
                max_tokens=600, json_mode=True, caller=caller)
    return _parse_safety_json(resp.choices[0].message.content), f"{resp.provider}/{resp.model}"


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


def _check_safety(ideas: list[dict[str, Any]], platform: str) -> list[bool]:
    """Возвращает список флагов is_safe в порядке ideas. Если проверить нечем — RuntimeError."""
    last_error: Exception | None = None
    for attempt in range(2):  # повтор, если ответ не разобрался или вердиктов не хватает
        try:
            results, engine = _ask(_safety_prompt(ideas, platform), "topic_filter_safety")
        except Exception as exc:
            last_error = exc
            logger.warning("Safety-check тем: %s", str(exc)[:200])
            continue
        if attempt == 0 and not _has_all_verdicts(results, len(ideas)):
            logger.warning("Safety-check тем (%s): вердикты не для всех идей — переспрашиваю. Ответ: %s",
                           engine, json.dumps(results, ensure_ascii=False)[:300])
            continue
        return _to_safety_list(results, ideas, engine)
    if last_error is None:
        # Второй ответ тоже неполный: идеи без вердикта отсеются как небезопасные
        return _to_safety_list(results, ideas, engine)
    raise RuntimeError(f"Safety-check тем не выполнен: {last_error}") from last_error


def _has_all_verdicts(results: list[dict], n: int) -> bool:
    ids = set()
    for item in results:
        try:
            if _as_bool(item.get("safe")) is not None:
                ids.add(int(item.get("id")))
        except (TypeError, ValueError, AttributeError):
            continue
    return ids >= set(range(1, n + 1))


def _check_niche_relevance(ideas: list[dict[str, Any]], niche: str) -> list[bool]:
    """
    Флаги релевантности нише в порядке ideas. Это фильтр качества, а не безопасности,
    поэтому при сбое LLM он не блокирует пайплайн (все идеи считаются релевантными).
    """
    if not niche:
        return [True] * len(ideas)
    try:
        results, engine = _ask(_NICHE_RELEVANCE_PROMPT.format(niche=niche, ideas_json=_ideas_json(ideas)),
                               "topic_filter_niche")
    except Exception as exc:
        logger.warning("Niche-check ошибка: %s — считаем всё релевантным", str(exc)[:200])
        return [True] * len(ideas)

    verdicts: dict[int, bool] = {}
    for item in results:
        try:
            verdicts[int(item.get("id"))] = _as_bool(item.get("relevant")) is not False
        except (TypeError, ValueError):
            continue
    flags = [verdicts.get(n, True) for n in range(1, len(ideas) + 1)]
    for idea, ok in zip(ideas, flags):
        if not ok:
            logger.info("Нерелевантно нише '%s' (%s): '%s' — пропускаем", niche, engine, idea["title"])
    return flags


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
        relevance_flags = _check_niche_relevance(fresh, niche)
        relevant = [i for i, ok in zip(fresh, relevance_flags) if ok]
        if not relevant:
            logger.warning("Niche-check: все идеи нерелевантны нише '%s' — пропускаем фильтр", niche)
            relevant = fresh  # если все отклонены — не блокируем пайплайн
        fresh = relevant

    niche_count = len(fresh)

    # 3. Safety-check (LLM-цепочка), fail-closed
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
