"""
Analytics Agent — забирает статистику постов через Zernio API и сохраняет в SQLite.

Запуск (раз в сутки, независимо от основного пайплайна):
    py agents/analytics_agent.py
    py agents/analytics_agent.py --days 14

Вход:  days (int) — окно в днях (по умолчанию 7)
Выход: топ-3 темы по просмотрам:
       [{"post_id", "title", "platform", "views", "likes", "comments"}]

Флоу:
  1. GET /posts — список постов из Zernio
  2. GET /posts/{id}/analytics — статистика по каждому
  3. Upsert в таблицу performance в pipeline.db
  4. Вернуть топ-3 по views за указанный период
"""

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

BASE_URL = "https://zernio.com/api/v1"
DB_PATH = os.getenv("DB_PATH", "pipeline.db")


# ---------------------------------------------------------------------------
# Zernio API
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    api_key = os.getenv("ZERNIO_API_KEY", "")
    if not api_key:
        raise EnvironmentError("ZERNIO_API_KEY не задан в .env")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _fetch_posts(account_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Возвращает список постов аккаунта."""
    resp = requests.get(
        f"{BASE_URL}/posts",
        headers=_headers(),
        params={"accountId": account_id, "limit": limit},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    # Zernio может вернуть {posts: [...]} или сразу список
    if isinstance(data, list):
        return data
    return data.get("posts", data.get("data", []))


def _fetch_post_analytics(post_id: str) -> dict[str, Any]:
    """Возвращает метрики одного поста."""
    resp = requests.get(
        f"{BASE_URL}/posts/{post_id}/analytics",
        headers=_headers(),
        timeout=30,
    )
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS performance (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id      TEXT NOT NULL,
            platform     TEXT,
            title        TEXT,
            views        INTEGER DEFAULT 0,
            likes        INTEGER DEFAULT 0,
            comments     INTEGER DEFAULT 0,
            published_at TEXT,
            fetched_at   TEXT,
            niche        TEXT,
            hook         TEXT
        )
    """)
    # Индекс для быстрого upsert по post_id
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uix_performance_post
        ON performance (post_id)
    """)
    # Добавляем колонки если таблица уже существовала без них (migration).
    # Старые БД создавали performance вообще без метрик — без views/likes/comments
    # upsert ниже падал.
    for col, typedef in [
        ("views", "INTEGER DEFAULT 0"),
        ("likes", "INTEGER DEFAULT 0"),
        ("comments", "INTEGER DEFAULT 0"),
        ("fetched_at", "TEXT"),
        ("niche", "TEXT"),
        ("hook", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE performance ADD COLUMN {col} {typedef}")
        except Exception:
            pass  # колонка уже есть
    conn.commit()


def _upsert_performance(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute("""
        INSERT INTO performance
            (post_id, platform, title, views, likes, comments, published_at, fetched_at)
        VALUES
            (:post_id, :platform, :title, :views, :likes, :comments, :published_at, :fetched_at)
        ON CONFLICT(post_id) DO UPDATE SET
            views        = excluded.views,
            likes        = excluded.likes,
            comments     = excluded.comments,
            fetched_at   = excluded.fetched_at
    """, row)
    conn.commit()


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------

def _parse_post(post: dict[str, Any], analytics: dict[str, Any]) -> dict[str, Any]:
    """Нормализует поля из разных форматов ответа Zernio."""
    # ID
    post_id = post.get("_id") or post.get("id", "")

    # Платформа (может быть список или строка)
    platforms = post.get("platforms", [])
    if isinstance(platforms, list) and platforms:
        platform = platforms[0].get("platform", "") if isinstance(platforms[0], dict) else platforms[0]
    else:
        platform = post.get("platform", "")

    title = post.get("title") or post.get("content", "")[:80]
    published_at = post.get("publishedAt") or post.get("createdAt", "")

    # Метрики — Zernio возвращает их в analytics или прямо в посте
    metrics = analytics.get("metrics", analytics) if analytics else {}
    views = int(metrics.get("views", metrics.get("impressions", post.get("views", 0))) or 0)
    likes = int(metrics.get("likes", metrics.get("reactions", post.get("likes", 0))) or 0)
    comments = int(metrics.get("comments", post.get("comments", 0)) or 0)

    return {
        "post_id": post_id,
        "platform": platform,
        "title": title,
        "views": views,
        "likes": likes,
        "comments": comments,
        "published_at": published_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def run(days: int = 7, db_path: str | None = None) -> list[dict[str, Any]]:
    """
    Собирает аналитику за последние N дней, сохраняет в SQLite.

    Returns:
        Топ-3 поста по просмотрам за период.
    """
    account_id = os.getenv("ZERNIO_ACCOUNT_ID", "")
    if not account_id:
        raise EnvironmentError("ZERNIO_ACCOUNT_ID не задан в .env")

    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    _ensure_table(conn)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    logger.info("Загружаю посты из Zernio (аккаунт %s)...", account_id)
    try:
        posts = _fetch_posts(account_id)
    except requests.HTTPError as exc:
        logger.error("Не удалось загрузить посты: %s", exc)
        conn.close()
        raise

    logger.info("Найдено %d постов, запрашиваю аналитику...", len(posts))

    updated = 0
    for post in posts:
        published_at = post.get("publishedAt") or post.get("createdAt", "")
        # Фильтруем по дате
        if published_at and published_at < cutoff_iso:
            continue

        post_id = post.get("_id") or post.get("id", "")
        if not post_id:
            continue

        try:
            analytics = _fetch_post_analytics(post_id)
        except Exception as exc:
            logger.warning("Аналитика для %s недоступна: %s", post_id, exc)
            analytics = {}

        row = _parse_post(post, analytics)
        _upsert_performance(conn, row)
        updated += 1

    logger.info("Обновлено %d записей в performance", updated)

    # Топ-3 по просмотрам за период
    rows = conn.execute(
        """SELECT post_id, platform, title, views, likes, comments, published_at
           FROM performance
           WHERE published_at >= ?
           ORDER BY views DESC
           LIMIT 3""",
        (cutoff_iso,),
    ).fetchall()

    conn.close()

    top3 = [
        {
            "post_id": r[0],
            "platform": r[1],
            "title": r[2],
            "views": r[3],
            "likes": r[4],
            "comments": r[5],
            "published_at": r[6],
        }
        for r in rows
    ]

    logger.info("Топ-%d тем по просмотрам за %d дней:", len(top3), days)
    for i, t in enumerate(top3, 1):
        logger.info("  %d. '%s' — %d просмотров", i, t["title"], t["views"])

    return top3


# ---------------------------------------------------------------------------
# CLI (запускается раз в сутки по cron / n8n)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Analytics Agent — сбор статистики постов")
    parser.add_argument("--days", type=int, default=7, help="Окно в днях (по умолчанию 7)")
    args = parser.parse_args()

    top = run(days=args.days)
    print(json.dumps(top, ensure_ascii=False, indent=2))
