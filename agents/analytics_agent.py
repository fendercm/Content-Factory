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
  2. YouTube: просмотры, лайки и комментарии из YouTube Data API (videos.list, YOUTUBE_API_KEY) —
     живые публичные счётчики, 1 единица квоты на 50 роликов
  3. Остальные (и YouTube без ключа): GET /analytics?postId={id} в Zernio. Zernio синхронизирует
     статистику с площадкой с задержкой; пока синхронизации нет, отвечает 202 и нулями —
     такие нули не метрики
  4. Upsert в таблицу performance в pipeline.db (неизвестные метрики не затирают прежние)
  5. Вернуть топ-3 по views за указанный период

Удержание и время просмотра Data API не отдаёт: это YouTube Analytics API (OAuth владельца
канала) или Zernio /analytics/youtube/daily-views.
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
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
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
    """
    Возвращает ответ Zernio GET /analytics?postId=… для одного поста.

    202 — синхронизация с площадкой ещё идёт: тело есть, но метрики в нём нулевые
    (syncStatus="pending"); _zernio_metrics такие ответы отбрасывает.
    """
    resp = requests.get(
        f"{BASE_URL}/analytics",
        headers=_headers(),
        params={"postId": post_id},
        timeout=30,
    )
    if resp.status_code == 404:
        logger.warning("Zernio: аналитики для поста %s нет (404)", post_id)
        return {}
    resp.raise_for_status()
    return resp.json()


def _fetch_youtube_stats(video_ids: list[str]) -> dict[str, dict[str, int]]:
    """
    Живые публичные счётчики YouTube через Data API: {video_id: {views, likes, comments}}.
    Без YOUTUBE_API_KEY или при ошибке — пустой dict (тогда метрики берутся из Zernio).
    """
    if not video_ids:
        return {}
    api_key = os.getenv("YOUTUBE_API_KEY", "")
    if not api_key:
        logger.warning("YOUTUBE_API_KEY не задан — статистика YouTube только из Zernio")
        return {}
    stats: dict[str, dict[str, int]] = {}
    for i in range(0, len(video_ids), 50):
        try:
            resp = requests.get(
                YOUTUBE_VIDEOS_URL,
                headers={"X-Goog-Api-Key": api_key},
                params={"part": "statistics", "id": ",".join(video_ids[i:i + 50])},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("YouTube Data API недоступен: %s", exc)
            continue
        for item in resp.json().get("items", []):
            st = item.get("statistics", {})
            stats[item["id"]] = {
                "views": int(st.get("viewCount", 0) or 0),
                "likes": int(st.get("likeCount", 0) or 0),
                "comments": int(st.get("commentCount", 0) or 0),
            }
    return stats


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
            (:post_id, :platform, :title, COALESCE(:views, 0), COALESCE(:likes, 0),
             COALESCE(:comments, 0), :published_at, :fetched_at)
        ON CONFLICT(post_id) DO UPDATE SET
            views        = COALESCE(:views, performance.views),
            likes        = COALESCE(:likes, performance.likes),
            comments     = COALESCE(:comments, performance.comments),
            fetched_at   = COALESCE(:fetched_at, performance.fetched_at)
    """, row)
    conn.commit()


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------

def _first_platform(post: dict[str, Any]) -> dict[str, Any]:
    platforms = post.get("platforms", [])
    if isinstance(platforms, list) and platforms and isinstance(platforms[0], dict):
        return platforms[0]
    return {}


def _platform_post_id(post: dict[str, Any]) -> str:
    """ID ролика на площадке (для YouTube — videoId)."""
    return _first_platform(post).get("platformPostId") or ""


def _zernio_metrics(analytics: dict[str, Any]) -> dict[str, int] | None:
    """
    Метрики из ответа GET /analytics?postId=… или None, если Zernio их ещё не синхронизировал.

    Пока syncStatus="pending", Zernio отдаёт analytics с нулями и lastUpdated=null —
    это «нет данных», а не «ноль просмотров».
    """
    if not analytics:
        return None
    metrics = analytics.get("analytics") or {}
    synced = analytics.get("syncStatus") == "synced" or bool(metrics.get("lastUpdated"))
    if not synced:
        # Сводка поста пустая — возможно, синхронизирована хотя бы одна площадка
        for pa in analytics.get("platformAnalytics") or []:
            if pa.get("syncStatus") == "synced" and pa.get("analytics"):
                metrics, synced = pa["analytics"], True
                break
    if not synced:
        return None
    return {
        "views": int(metrics.get("views") or 0),
        "likes": int(metrics.get("likes") or 0),
        "comments": int(metrics.get("comments") or 0),
    }


def _parse_post(
    post: dict[str, Any],
    analytics: dict[str, Any],
    youtube_stats: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """
    Нормализует пост Zernio в строку performance.

    Источник метрик: YouTube Data API (для YouTube) → синхронизированная аналитика Zernio →
    нет данных. При «нет данных» views/likes/comments = None: upsert не затрёт прежние цифры.
    metrics_source ("youtube" | "zernio" | None) — только для лога, в БД не пишется.
    """
    post_id = post.get("_id") or post.get("id", "")

    # Платформа (может быть список или строка)
    platforms = post.get("platforms", [])
    if isinstance(platforms, list) and platforms:
        platform = platforms[0].get("platform", "") if isinstance(platforms[0], dict) else platforms[0]
    else:
        platform = post.get("platform", "")

    title = post.get("title") or post.get("content", "")[:80]
    published_at = post.get("publishedAt") or post.get("createdAt", "")

    video_id = _platform_post_id(post)
    if platform == "youtube" and youtube_stats and video_id in youtube_stats:
        metrics, source = youtube_stats[video_id], "youtube"
    else:
        metrics = _zernio_metrics(analytics)
        source = "zernio" if metrics is not None else None

    return {
        "post_id": post_id,
        "platform": platform,
        "title": title,
        "views": metrics["views"] if metrics else None,
        "likes": metrics["likes"] if metrics else None,
        "comments": metrics["comments"] if metrics else None,
        "published_at": published_at,
        "fetched_at": datetime.now(timezone.utc).isoformat() if metrics else None,
        "metrics_source": source,
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

    recent = []
    for post in posts:
        published_at = post.get("publishedAt") or post.get("createdAt", "")
        # Фильтруем по дате
        if published_at and published_at < cutoff_iso:
            continue
        if post.get("_id") or post.get("id"):
            recent.append(post)

    youtube_ids = [
        vid for p in recent
        if _first_platform(p).get("platform") == "youtube" and (vid := _platform_post_id(p))
    ]
    youtube_stats = _fetch_youtube_stats(youtube_ids)

    updated = no_data = 0
    for post in recent:
        post_id = post.get("_id") or post.get("id", "")
        analytics: dict[str, Any] = {}
        if _platform_post_id(post) not in youtube_stats:
            try:
                analytics = _fetch_post_analytics(post_id)
            except Exception as exc:
                logger.warning("Аналитика для %s недоступна: %s", post_id, exc)

        row = _parse_post(post, analytics, youtube_stats)
        _upsert_performance(conn, row)
        if row["metrics_source"] is None:
            no_data += 1
            logger.warning("Нет метрик для %s (%s): Zernio ещё не синхронизировал, прежние цифры сохранены",
                           post_id, row["title"])
        else:
            updated += 1
            logger.info("  %s [%s]: %d просмотров, %d лайков, %d комментариев",
                        row["title"], row["metrics_source"], row["views"], row["likes"], row["comments"])

    logger.info("Обновлено %d записей в performance, без данных %d", updated, no_data)

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
