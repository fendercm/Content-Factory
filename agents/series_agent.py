"""
Series Agent — управляет многосерийным контентом.

Когда тема слишком объёмная для одного 45-сек видео, разбивает её на N частей
("Часть X из Y") и хранит состояние в SQLite.

API:
    create_series(name, niche, total_parts, db_path) → series_id
    get_next_episode(series_name, db_path) → {part_number, series_id} | None
    mark_episode_published(series_id, part_number, run_id, post_id, title, platform, db_path)
    should_start_series(topic, research_facts, platform) → bool
    make_series_title(base_title, part_number, total_parts) → str
    series_branding_text(part_number, total_parts) → str
"""

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH_DEFAULT = os.environ.get("DB_PATH", "pipeline.db")

# Минимальное количество high-confidence фактов чтобы предложить серию
_SERIES_FACTS_THRESHOLD = 8
# Ключевые слова, которые часто подразумевают объёмный контент
_SERIES_KEYWORDS = (
    "история", "эволюц", "полный разбор", "всё о", "гид", "руководство",
    "глубокий анализ", "полное объяснение", "теория", "феномен",
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def create_series(
    name: str,
    niche: str = "",
    total_parts: int = 3,
    db_path: str = DB_PATH_DEFAULT,
) -> int:
    """
    Создаёт новую серию в БД.

    Returns:
        series_id
    """
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO series (series_name, niche, total_parts, created_at)
               VALUES (?, ?, ?, ?)""",
            (name, niche, total_parts, datetime.now(timezone.utc).isoformat()),
        )
        series_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    logger.info("Series created: id=%d name='%s' parts=%d", series_id, name, total_parts)
    return series_id


def get_next_episode(
    series_name: str,
    db_path: str = DB_PATH_DEFAULT,
) -> dict[str, Any] | None:
    """
    Возвращает следующий незавершённый эпизод серии или None если серия завершена.

    Returns:
        {series_id, part_number, total_parts} | None
    """
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT id, total_parts FROM series WHERE series_name = ? ORDER BY id DESC LIMIT 1",
                (series_name,),
            ).fetchone()
            if not row:
                return None

            series_id, total_parts = row
            published = conn.execute(
                "SELECT part_number FROM series_episodes WHERE series_id = ?",
                (series_id,),
            ).fetchall()
        finally:
            conn.close()

        done_parts = {r[0] for r in published}
        for part in range(1, total_parts + 1):
            if part not in done_parts:
                return {"series_id": series_id, "part_number": part, "total_parts": total_parts}

        return None  # Все части опубликованы
    except Exception as exc:
        logger.warning("get_next_episode error: %s", exc)
        return None


def mark_episode_published(
    series_id: int,
    part_number: int,
    run_id: str = "",
    post_id: str = "",
    title: str = "",
    platform: str = "",
    db_path: str = DB_PATH_DEFAULT,
) -> None:
    """Записывает успешно опубликованный эпизод серии."""
    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                """INSERT INTO series_episodes
                   (series_id, part_number, run_id, post_id, title, platform, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    series_id, part_number, run_id, post_id, title, platform,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info(
            "Episode marked: series_id=%d part=%d/%s post_id=%s",
            series_id, part_number, "?", post_id,
        )
    except Exception as exc:
        logger.warning("mark_episode_published error: %s", exc)


def should_start_series(
    topic: str,
    research_facts: list[dict] | None = None,
    platform: str = "youtube",
) -> bool:
    """
    Эвристика: стоит ли разбить тему на серию из 2-3 видео.
    Критерии:
      - Много high-confidence фактов (глубокая тема)
      - Ключевые слова серийного контента в теме
      - Платформа поддерживает серии (не twitter)
    """
    if platform == "twitter":
        return False

    topic_lower = (topic or "").lower()
    has_series_keyword = any(kw in topic_lower for kw in _SERIES_KEYWORDS)

    facts = research_facts or []
    high_facts = sum(1 for f in facts if f.get("confidence") == "high")

    return has_series_keyword or high_facts >= _SERIES_FACTS_THRESHOLD


def make_series_title(base_title: str, part_number: int, total_parts: int) -> str:
    """
    Добавляет "Часть X из Y" к заголовку для узнаваемости серии.

    Пример: "Психология прокрастинации. Часть 1 из 3"
    """
    base = base_title.rstrip(".!?")
    return f"{base}. Часть {part_number} из {total_parts}"


def series_branding_text(part_number: int, total_parts: int) -> str:
    """
    Возвращает короткую метку для вставки в сценарий (в конец hook или CTA).

    Пример: "[Часть 1/3]" или "[Продолжение ↓]"
    """
    if part_number == 1:
        return f"[Начало серии • {total_parts} части]"
    if part_number == total_parts:
        return f"[Финал серии • часть {part_number}/{total_parts}]"
    return f"[Часть {part_number}/{total_parts}]"


def list_active_series(db_path: str = DB_PATH_DEFAULT) -> list[dict[str, Any]]:
    """Возвращает все незавершённые серии."""
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                """SELECT s.id, s.series_name, s.niche, s.total_parts,
                          COUNT(e.id) as published_parts
                   FROM series s
                   LEFT JOIN series_episodes e ON e.series_id = s.id
                   GROUP BY s.id
                   HAVING published_parts < s.total_parts
                   ORDER BY s.created_at DESC""",
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "series_id": r[0],
                "name": r[1],
                "niche": r[2],
                "total_parts": r[3],
                "published_parts": r[4],
                "remaining": r[3] - r[4],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("list_active_series error: %s", exc)
        return []


if __name__ == "__main__":
    import json, sys
    logging.basicConfig(level=logging.INFO)
    topic = sys.argv[1] if len(sys.argv) > 1 else "полная история Юнга и психоанализа"
    result = should_start_series(topic)
    print(f"Should start series: {result}")
    print(f"Series title: {make_series_title(topic, 1, 3)}")
    print(f"Branding: {series_branding_text(1, 3)}")
