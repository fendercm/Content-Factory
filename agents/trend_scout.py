"""
Trend Scout Agent — собирает трендовые темы без LLM.

Источники:
  1. Google Trends (pytrends) — related queries за 7 дней
  2. YouTube Data API v3 — trending videos, фильтрация по нише

Вход:  niche (str) — тематика, например "психология"
Выход: JSON [{topic, virality, source}] топ-5 по убыванию виральности
"""

import logging
import os
import time
import urllib.parse
from typing import Any

import requests
from pytrends.request import TrendReq
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

# Вес источника при нормализации виральности
_VIRALITY_WEIGHTS = {
    "google_trends": 1.0,
    "youtube":       0.9,
    "google_news":   0.85,
    "habr":          0.75,
    "vc_ru":         0.70,
}

# Коды регионов для YouTube Trending (пробуем по очереди до первого успеха)
_YT_REGIONS = ["RU", "US", ""]

# Категории YouTube — нишевой маппинг (categoryId)
# 0 = все, 22 = People & Blogs, 26 = How-to, 27 = Education, 24 = Entertainment
_YT_CATEGORY_MAP: dict[str, str] = {
    "психология":     "26",
    "наука":          "27",
    "технологии":     "28",
    "бизнес":         "22",
    "финансы":        "22",
    "образование":    "27",
    "здоровье":       "26",
    "спорт":          "17",
    "музыка":         "10",
    "кулинария":      "26",
    "путешествия":    "19",
    "мода":           "26",
    "игры":           "20",
}


# ---------------------------------------------------------------------------
# Google Trends
# ---------------------------------------------------------------------------

def _fetch_google_trends(niche: str) -> list[dict[str, Any]]:
    # Пробуем сначала RU (меньше шансов получить 429), потом глобально
    for geo in ("RU", ""):
        try:
            # retries=1 чтобы не усугублять rate-limit ошибку повторными запросами
            pytrends = TrendReq(hl="ru-RU", tz=180, timeout=(10, 25), retries=1, backoff_factor=1.0)
            pytrends.build_payload([niche], cat=0, timeframe="now 7-d", geo=geo)
            related = pytrends.related_queries()

            results: list[dict[str, Any]] = []
            queries = related.get(niche, {})
            top_df = queries.get("top")
            if top_df is not None and not top_df.empty:
                for _, row in top_df.head(5).iterrows():
                    results.append({
                        "topic": str(row["query"]),
                        "virality": float(row["value"]) / 100.0,
                        "source": "google_trends",
                    })

            if results:
                logger.info("Google Trends (geo=%s): найдено %d запросов по '%s'", geo or "global", len(results), niche)
                return results

        except Exception as exc:
            err = str(exc)
            if "429" in err or "Too Many Requests" in err:
                logger.warning("Google Trends: rate limit (429) — Google временно блокирует запросы с этого IP")
            else:
                logger.warning("Google Trends (geo=%s): %s", geo or "global", exc)

    return []


# ---------------------------------------------------------------------------
# YouTube Data API v3
# ---------------------------------------------------------------------------

def _yt_get(endpoint: str, params: dict) -> dict:
    """GET запрос к YouTube Data API v3 с двумя попытками при сетевых сбоях."""
    base = "https://www.googleapis.com/youtube/v3"
    # Передаём params как dict — requests сам кодирует, сохраняя запятые в part
    payload = {**params, "key": YOUTUBE_API_KEY}
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = requests.get(f"{base}/{endpoint}", params=payload, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError:
            raise  # HTTP ошибки (4xx/5xx) не ретраим
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(2)
    raise last_exc


def _fetch_youtube_trending(niche: str) -> list[dict[str, Any]]:
    if not YOUTUBE_API_KEY:
        logger.warning("YOUTUBE_API_KEY не задан — пропускаем YouTube Trending")
        return []

    # Определяем категорию по нише (нет совпадения → используем 0 = все)
    niche_lower = niche.lower()
    category_id = next(
        (v for k, v in _YT_CATEGORY_MAP.items() if k in niche_lower),
        "0",
    )

    results: list[dict[str, Any]] = []

    for region in _YT_REGIONS:
        try:
            params: dict[str, Any] = {
                "part": "snippet,statistics",
                "chart": "mostPopular",
                "maxResults": 50,
                "videoCategoryId": category_id,
            }
            if region:
                params["regionCode"] = region

            data = _yt_get("videos", params)
            items = data.get("items", [])

            for item in items:
                title = item.get("snippet", {}).get("title", "")
                description = item.get("snippet", {}).get("description", "")[:200]
                stats = item.get("statistics", {})

                # Проверяем релевантность нише
                text_lower = (title + " " + description).lower()
                if niche_lower not in text_lower:
                    continue

                view_count = int(stats.get("viewCount", 0) or 0)
                like_count = int(stats.get("likeCount", 0) or 0)

                # Нормализуем: 1M просмотров = виральность 1.0
                virality = min(view_count / 1_000_000, 1.0)
                # Бонус за высокий like ratio (до +20%)
                if view_count > 0:
                    like_ratio = like_count / view_count
                    virality = min(virality * (1 + like_ratio * 0.2), 1.0)

                results.append({
                    "topic": title[:120],
                    "virality": round(virality, 4),
                    "source": "youtube",
                })

            if results:
                logger.info(
                    "YouTube Trending (region=%s, cat=%s): найдено %d релевантных видео",
                    region or "global", category_id, len(results),
                )
                break  # Нашли данные — не идём дальше по регионам

        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 403:
                logger.error("YouTube API: ошибка 403 — проверьте YOUTUBE_API_KEY и квоту")
                break
            logger.warning("YouTube Trending (region=%s) HTTP ошибка: %s", region, exc)
        except Exception as exc:
            logger.warning("YouTube Trending (region=%s) ошибка: %s", region, exc)

    # Если по нише ничего не нашли — не берём нерелевантный контент
    if not results:
        logger.info("Ниша '%s' не найдена в YouTube Trending — пропускаем", niche)

    return results


# ---------------------------------------------------------------------------
# Google News RSS
# ---------------------------------------------------------------------------

def _fetch_google_news_rss(niche: str) -> list[dict[str, Any]]:
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser не установлен: pip install feedparser")
        return []

    query = urllib.parse.quote(niche)
    url = f"https://news.google.com/rss/search?q={query}&hl=ru&gl=RU&ceid=RU:ru"
    try:
        feed = feedparser.parse(url)
        results: list[dict[str, Any]] = []
        for i, entry in enumerate(feed.entries[:10]):
            title = entry.get("title", "").strip()
            if not title:
                continue
            # Убираем суффикс источника " - Издание" который Google добавляет
            if " - " in title:
                title = title.rsplit(" - ", 1)[0].strip()
            virality = max(0.3, 1.0 - i * 0.07)
            results.append({"topic": title[:120], "virality": round(virality, 4), "source": "google_news"})
        logger.info("Google News RSS: найдено %d статей по '%s'", len(results), niche)
        return results
    except Exception as exc:
        logger.warning("Google News RSS ошибка: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Habr RSS
# ---------------------------------------------------------------------------

def _fetch_habr(niche: str) -> list[dict[str, Any]]:
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser не установлен: pip install feedparser")
        return []

    query = urllib.parse.quote(niche)
    url = f"https://habr.com/ru/rss/search/articles/?q={query}"
    try:
        feed = feedparser.parse(url)
        results: list[dict[str, Any]] = []
        for i, entry in enumerate(feed.entries[:5]):
            title = entry.get("title", "").strip()
            if not title:
                continue
            virality = max(0.25, 0.9 - i * 0.12)
            results.append({"topic": title[:120], "virality": round(virality, 4), "source": "habr"})
        logger.info("Habr RSS: найдено %d статей по '%s'", len(results), niche)
        return results
    except Exception as exc:
        logger.warning("Habr RSS ошибка: %s", exc)
        return []


# ---------------------------------------------------------------------------
# vc.ru RSS + фильтр по нише
# ---------------------------------------------------------------------------

def _fetch_vc_ru(niche: str) -> list[dict[str, Any]]:
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser не установлен: pip install feedparser")
        return []

    try:
        feed = feedparser.parse("https://vc.ru/rss")
        niche_lower = niche.lower()
        results: list[dict[str, Any]] = []
        for entry in feed.entries[:50]:
            title = entry.get("title", "").strip()
            summary = entry.get("summary", "").lower()
            if niche_lower not in title.lower() and niche_lower not in summary:
                continue
            virality = max(0.2, 0.85 - len(results) * 0.1)
            results.append({"topic": title[:120], "virality": round(virality, 4), "source": "vc_ru"})
            if len(results) >= 5:
                break
        logger.info("vc.ru RSS: найдено %d статей по '%s'", len(results), niche)
        return results
    except Exception as exc:
        logger.warning("vc.ru RSS ошибка: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

def run(niche: str) -> list[dict[str, Any]]:
    """Возвращает топ-5 трендовых тем по нише."""
    all_results: list[dict[str, Any]] = []
    all_results.extend(_fetch_google_trends(niche))
    all_results.extend(_fetch_youtube_trending(niche))
    all_results.extend(_fetch_google_news_rss(niche))
    all_results.extend(_fetch_habr(niche))
    all_results.extend(_fetch_vc_ru(niche))

    if not all_results:
        logger.warning("Нет данных от источников, возвращаем заглушку")
        return [{"topic": niche, "virality": 0.5, "source": "fallback"}]

    # Применяем вес источника
    for item in all_results:
        weight = _VIRALITY_WEIGHTS.get(item["source"], 0.7)
        item["virality"] = round(item["virality"] * weight, 4)

    # Дедупликация по первым 30 символам заголовка
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in all_results:
        key = item["topic"][:30].lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    top5 = sorted(unique, key=lambda x: x["virality"], reverse=True)[:5]
    logger.info("Trend Scout: итого %d тем для ниши '%s'", len(top5), niche)
    return top5


if __name__ == "__main__":
    import io
    import json
    import sys

    # Принудительно UTF-8 для Windows-терминала (избегаем cp1251 UnicodeEncodeError)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    logging.basicConfig(level=logging.INFO)
    niche_arg = sys.argv[1] if len(sys.argv) > 1 else "психология"
    print(json.dumps(run(niche_arg), ensure_ascii=False, indent=2))
