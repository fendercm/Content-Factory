"""
Research Agent — собирает факты по теме из Wikipedia, Semantic Scholar, Google News
и веб-поисковиков (Tavily/Brave/Serper/DDG — с автопереключением).

Вход:  topic (str) — тема видео
Выход: {"facts": list[dict], "summary": str, "sources_used": list[str]}

Источники (по порядку):
  1. Wikipedia REST API (ru) + EN — определения, статистика, даты [confidence=high]
  2. Semantic Scholar API — научные работы с цитированием [confidence=high]
  3. Google News RSS — свежие заголовки [confidence=low, hedge-маркер]
  4. Web Search (Tavily → Brave → Serper → DuckDuckGo) — широкий поиск
     по сниппетам, LLM выжимает факты [confidence=medium]

Web-провайдеры выбираются по наличию ключей в .env:
  TAVILY_API_KEY (best, 1000/мес) → BRAVE_API_KEY (2000/мес) →
  SERPER_API_KEY (2500/мес) → DuckDuckGo (без ключа, rate-limited).
"""

import json
import logging
import os
import random
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

WIKIPEDIA_SUMMARY    = "https://ru.wikipedia.org/api/rest_v1/page/summary/{}"
WIKIPEDIA_SEARCH     = "https://ru.wikipedia.org/w/api.php"
WIKIPEDIA_EN_SEARCH  = "https://en.wikipedia.org/w/api.php"
SCHOLAR_API          = "https://api.semanticscholar.org/graph/v1/paper/search"
GOOGLE_NEWS_RSS      = "https://news.google.com/rss/search?q={}&hl=ru&gl=RU&ceid=RU:ru"
NUMBERS_API          = "http://numbersapi.com/{}/trivia"
TIMEOUT              = 10
HEADERS              = {"User-Agent": "ContentFactory/1.0 (research agent)"}


# ---------------------------------------------------------------------------
# Источник 1: Wikipedia (двухшаговый: поиск → summary)
# ---------------------------------------------------------------------------

def _wiki_search(topic: str) -> list[str]:
    """Ищет названия статей Wikipedia через OpenSearch API (возвращает до 5 заголовков)."""
    try:
        resp = requests.get(
            WIKIPEDIA_SEARCH,
            params={"action": "opensearch", "search": topic, "limit": 5, "format": "json"},
            timeout=TIMEOUT,
            headers=HEADERS,
        )
        return resp.json()[1] if resp.status_code == 200 else []
    except Exception:
        return []


def _wiki_extract(title: str) -> tuple[str, str]:
    """
    Возвращает (extract, page_url) для точного заголовка.
    Сначала пробует MediaWiki extracts API (до 3000 символов),
    fallback — REST summary.
    """
    page_url = f"https://ru.wikipedia.org/wiki/{urllib.parse.quote(title)}"
    try:
        # MediaWiki extracts API — возвращает больше текста чем REST summary
        resp = requests.get(
            WIKIPEDIA_SEARCH,
            params={
                "action": "query",
                "prop": "extracts",
                "titles": title,
                "format": "json",
                "explaintext": True,
                "exsectionformat": "plain",
                "exchars": 3000,
            },
            timeout=TIMEOUT,
            headers=HEADERS,
        )
        if resp.status_code == 200:
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                if page.get("pageid", -1) == -1:
                    break  # статья не найдена
                extract = page.get("extract", "")
                if extract:
                    return extract, page_url
    except Exception:
        pass

    # Fallback: REST summary
    try:
        resp = requests.get(
            WIKIPEDIA_SUMMARY.format(urllib.parse.quote(title)),
            timeout=TIMEOUT,
            headers=HEADERS,
        )
        if resp.status_code == 200:
            data = resp.json()
            extract  = data.get("extract", "")
            page_url = data.get("content_urls", {}).get("desktop", {}).get("page", page_url)
            return extract, page_url
    except Exception:
        pass

    return "", ""


def _parse_facts(extract: str, source_label: str, url: str = "") -> list[dict]:
    """Выделяет факты из текста статьи: сначала предложения с цифрами, потом содержательные."""
    facts: list[dict] = []

    for s in re.split(r"[.!?]+", extract):
        s = s.strip()
        if len(s) > 30 and re.search(r"\d", s):
            facts.append({"text": s, "source": source_label, "url": url})

    # Если совсем нет цифровых фактов — берём содержательные предложения
    if not facts:
        for s in re.split(r"[.!?]+", extract):
            s = s.strip()
            if len(s) > 60:
                facts.append({"text": s, "source": source_label, "url": url})
                if len(facts) >= 2:
                    break

    return facts


def _wiki_en_facts(topic: str) -> list[dict]:
    """
    Ищет факты в английской Wikipedia — полезно для тем с латинскими словами
    (iPhone, ChatGPT, Tesla и т.п.) где русская Wikipedia бедна.
    Активируется только если в теме есть латинские символы.
    """
    if not re.search(r'[A-Za-z]{3,}', topic):
        return []  # нет латиницы — пропускаем
    try:
        resp = requests.get(
            WIKIPEDIA_EN_SEARCH,
            params={"action": "opensearch", "search": topic, "limit": 3, "format": "json"},
            timeout=TIMEOUT,
            headers=HEADERS,
        )
        titles: list[str] = resp.json()[1] if resp.status_code == 200 else []
    except Exception:
        return []

    facts: list[dict] = []
    for title in titles[:2]:
        if len(facts) >= 4:
            break
        try:
            resp = requests.get(
                WIKIPEDIA_EN_SEARCH,
                params={
                    "action": "query", "prop": "extracts", "titles": title,
                    "format": "json", "explaintext": True, "exchars": 2000,
                },
                timeout=TIMEOUT, headers=HEADERS,
            )
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                extract = page.get("extract", "")
                if not extract:
                    continue
                page_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title)}"
                # Берём предложения с цифрами из английского текста
                for s in re.split(r'[.!?]+', extract):
                    s = s.strip()
                    if len(s) > 40 and re.search(r'\d', s):
                        facts.append({"text": s, "source": f"Wikipedia EN ({title})", "url": page_url})
                        if len(facts) >= 4:
                            break
        except Exception:
            continue

    if facts:
        logger.info("Wikipedia EN: %d фактов для «%s»", len(facts), topic)
    return facts


def _llm_extract_numeric_facts(extract: str, topic: str, source_label: str, url: str = "") -> list[dict]:
    """
    Использует LLM (Groq → Cerebras fallback) чтобы вытащить из Wikipedia-текста
    ТОЛЬКО факты с конкретными цифрами, процентами или датами.
    Возвращает пустой список при любой ошибке — не блокирует пайплайн.
    """
    if not extract:
        return []

    prompt = (
        f"Тема видео: «{topic}»\n\n"
        f"Текст:\n{extract[:3000]}\n\n"
        "Извлеки из текста факты с конкретными цифрами, процентами или датами, относящиеся к теме.\n"
        "Правила:\n"
        "1. Только то, что прямо написано в тексте. Ничего не додумывай и не округляй по-своему.\n"
        "2. Если в тексте названо, КТО это установил (университет, организация, автор, год "
        "исследования), сохрани это в предложении. Если не названо — не добавляй источник.\n"
        "3. Каждый факт — одно законченное предложение на русском, понятное без контекста.\n"
        "4. Не больше 5 фактов, самые неожиданные — первыми.\n"
        'Верни JSON-объект: {"facts": ["Исследователи Калифорнийского университета в Ирвайне '
        'установили, что после отвлечения к задаче возвращаются в среднем через 23 минуты."]}\n'
        'Если фактов с цифрами нет — {"facts": []}.'
    )
    messages = [{"role": "user", "content": prompt}]

    raw = ""
    try:
        from utils.llm_client import chat
        resp = chat(messages, tier="fast", temperature=0.1, max_tokens=700,
                    json_mode=True, caller="research_agent")
        raw = resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.warning("LLM fact extraction ошибка: %s", exc)
        return []

    try:
        data = json.loads(raw)
        items = data.get("facts", []) if isinstance(data, dict) else data
        return [
            {"text": s.strip(), "source": source_label, "url": url}
            for s in items
            if isinstance(s, str) and len(s.strip()) > 20
        ]
    except json.JSONDecodeError:
        return []


def _numbers_facts(topic: str) -> list[dict]:
    """
    Получает интересные факты о числах через Numbers API.
    Сначала пробует числа из самой темы, потом случайные (7, 10, 21, 42, 100).
    """
    numbers = re.findall(r"\d+", topic)
    fallback = [str(n) for n in [7, 10, 21, 42, 100] if str(n) not in numbers]
    random.shuffle(fallback)
    candidates = numbers[:3] + fallback[:2]

    facts: list[dict] = []
    for num in candidates:
        if len(facts) >= 2:
            break
        try:
            resp = requests.get(
                NUMBERS_API.format(num),
                timeout=5,
                headers={**HEADERS, "Accept": "text/plain"},
            )
            text = resp.text.strip() if resp.status_code == 200 else ""
            if text and "boring number" not in text.lower():
                facts.append({"text": text, "source": "Numbers API"})
                logger.debug("Numbers API [%s]: %s", num, text[:80])
            else:
                logger.debug("Numbers API [%s]: скучный/пустой ответ — пропускаем", num)
        except Exception as exc:
            logger.debug("Numbers API [%s] ошибка: %s", num, exc)

    logger.info("Numbers API: %d фактов для «%s»", len(facts), topic)
    return facts


def _topic_keywords(topic: str) -> list[str]:
    """Разбивает тему на ключевые слова длиннее 4 символов (стоп-слова исключены)."""
    stop = {"это", "того", "тому", "этой", "этом", "свои", "свою", "наши", "ваши", "что",
            "для", "или", "как", "так", "при", "без", "над", "под", "про", "его", "она"}
    words = re.findall(r"[а-яёА-ЯЁa-zA-Z]{4,}", topic)
    return [w for w in words if w.lower() not in stop]


def _wiki_facts(topic: str) -> list[dict]:
    """
    Собирает факты из Wikipedia (три прохода):
      1. Прямой запрос по теме.
      2. OpenSearch-поиск → топ-3 найденные статьи.
      3. Ключевые слова темы → отдельные статьи (если фактов ещё мало).
    """
    all_facts: list[dict] = []
    tried: set[str] = set()

    def _fetch_and_add(title: str) -> None:
        if title.lower() in tried or len(all_facts) >= 8:
            return
        tried.add(title.lower())
        extract, page_url = _wiki_extract(title)
        if extract:
            label = "Wikipedia" if title.lower() == topic.lower() else f"Wikipedia ({title})"
            # Сначала пробуем LLM-фильтрацию (только конкретные цифры/даты)
            llm_facts = _llm_extract_numeric_facts(extract, topic, label, page_url)
            if llm_facts:
                all_facts.extend(llm_facts)
            else:
                # Fallback на regex-парсинг если LLM недоступен
                all_facts.extend(_parse_facts(extract, label, page_url))

    # Шаг 1: прямой запрос
    _fetch_and_add(topic)

    # Шаг 2: OpenSearch → ближайшие статьи
    if len(all_facts) < 4:
        for title in _wiki_search(topic)[:3]:
            _fetch_and_add(title)

    # Шаг 3: ключевые слова как отдельные статьи
    if len(all_facts) < 4:
        for kw in _topic_keywords(topic)[:3]:
            for title in _wiki_search(kw)[:1]:
                _fetch_and_add(title)

    logger.info("Wikipedia: %d фактов для «%s»", len(all_facts), topic)
    return all_facts[:8]


# ---------------------------------------------------------------------------
# Источник 2: Semantic Scholar
# ---------------------------------------------------------------------------

def _scholar_facts(topic: str) -> list[dict]:
    """Извлекает факты из научных статей через Semantic Scholar API.
    Бывает медленный — даём 30 сек, иначе пропускаем (fallback на Web Search)."""
    try:
        resp = requests.get(
            SCHOLAR_API,
            params={
                "query": topic,
                "fields": "title,abstract,year,citationCount",
                "limit": 5,
            },
            timeout=30,
            headers=HEADERS,
        )
        if resp.status_code == 429:
            logger.warning("Semantic Scholar: rate limit (429) — пропускаем")
            return []
        if resp.status_code != 200:
            return []

        papers = resp.json().get("data", [])
        facts: list[dict] = []

        for paper in papers:
            if len(facts) >= 3:
                break
            title   = (paper.get("title") or "").strip()
            abstract = (paper.get("abstract") or "").strip()
            year    = paper.get("year") or ""
            cites   = paper.get("citationCount") or 0

            if not title:
                continue

            # Приоритет: предложение с числом из аннотации
            found = False
            for s in re.split(r"[.!?]+", abstract)[:4]:
                s = s.strip()
                if len(s) > 30 and re.search(r"\d", s):
                    facts.append({"text": s, "source": f"Semantic Scholar ({year})", "citations": cites})
                    found = True
                    break

            if not found and year:
                facts.append({
                    "text": f"Исследование «{title}» ({year}), {cites} цитирований",
                    "source": "Semantic Scholar",
                    "citations": cites,
                })

        logger.info("Semantic Scholar: %d фактов для «%s»", len(facts), topic)
        return facts
    except Exception as exc:
        logger.warning("Semantic Scholar ошибка: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Источник 3: Google News RSS
# ---------------------------------------------------------------------------

def _news_facts(topic: str) -> list[dict]:
    """Извлекает заголовки свежих новостей из Google News RSS."""
    url = GOOGLE_NEWS_RSS.format(urllib.parse.quote(topic))
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
        if resp.status_code != 200:
            return []

        root = ET.fromstring(resp.content)
        facts: list[dict] = []

        for item in root.findall(".//item")[:8]:
            title = item.findtext("title", "").strip()
            pub   = (item.findtext("pubDate") or "")[:16] or "сегодня"

            # Убираем название источника после тире в конце заголовка
            title = re.sub(r"\s*[-–—]\s*[^-–—]+$", "", title).strip()

            if len(title) > 20:
                facts.append({"text": title, "source": f"Google News ({pub})"})
                if len(facts) >= 3:
                    break

        logger.info("Google News: %d новостей для «%s»", len(facts), topic)
        return facts
    except Exception as exc:
        logger.warning("Google News ошибка: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Источник 4: Web Search (Tavily → Brave → Serper → DuckDuckGo)
# ---------------------------------------------------------------------------

def _tavily_search(query: str, max_results: int = 6) -> list[dict]:
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        return []
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key, "query": query, "max_results": max_results,
                "include_answer": False, "search_depth": "basic",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [
            {"title": r.get("title", ""), "snippet": r.get("content", ""),
             "url": r.get("url", "")}
            for r in data.get("results", [])
        ]
    except Exception as exc:
        logger.warning("Tavily error: %s", exc)
        return []


def _brave_search(query: str, max_results: int = 6) -> list[dict]:
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={"Accept": "application/json", "X-Subscription-Token": key},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        web = resp.json().get("web", {}).get("results", [])
        return [
            {"title": r.get("title", ""), "snippet": r.get("description", ""),
             "url": r.get("url", "")}
            for r in web[:max_results]
        ]
    except Exception as exc:
        logger.warning("Brave error: %s", exc)
        return []


def _serper_search(query: str, max_results: int = 6) -> list[dict]:
    key = os.environ.get("SERPER_API_KEY", "")
    if not key:
        return []
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": query, "num": max_results, "hl": "ru"},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        organic = resp.json().get("organic", [])
        return [
            {"title": r.get("title", ""), "snippet": r.get("snippet", ""),
             "url": r.get("link", "")}
            for r in organic[:max_results]
        ]
    except Exception as exc:
        logger.warning("Serper error: %s", exc)
        return []


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        logger.warning("DuckDuckGo: ddgs не установлен (pip install ddgs)")
        return []
    try:
        results = list(DDGS().text(query, max_results=max_results, region="ru-ru"))
    except Exception as exc:
        logger.warning("DuckDuckGo search failed: %s", exc)
        return []
    return [
        {"title": r.get("title", ""), "snippet": r.get("body", ""),
         "url": r.get("href", "")}
        for r in results
    ]


# Порядок: лучшие/платные первыми, бесплатный DDG в конце
_WEB_PROVIDERS = [
    ("Tavily",     _tavily_search),
    ("Brave",      _brave_search),
    ("Serper",     _serper_search),
    ("DuckDuckGo", _ddg_search),
]


def _web_search(query: str, max_results: int = 6) -> tuple[list[dict], str]:
    """Перебирает провайдеров по приоритету — первый непустой выигрывает.

    Returns:
        (results, provider_name) — пустой список + 'none' если все упали.
    """
    for name, fn in _WEB_PROVIDERS:
        results = fn(query, max_results)
        if results:
            logger.info("Web search via %s: %d результатов для «%s»", name, len(results), query)
            return results, name
    return [], "none"


def _web_facts(topic: str) -> tuple[list[dict], str]:
    """
    Делает 3 веб-запроса по теме (общий + свежий с текущим годом + поиск новостей), отдаёт сниппеты в LLM
    для выжимки фактов с цифрами. Возвращает (facts, provider_used).
    """
    current_year = datetime.now().year
    queries = [
        f"{topic} исследование статистика данные {current_year}",
        f"{topic} {current_year} новости факты результаты",
        f"{topic} последние данные исследование",
    ]
    all_facts: list[dict] = []
    provider_used = "none"

    for q in queries:
        if len(all_facts) >= 5:
            break
        results, provider = _web_search(q, max_results=6)
        if not results:
            continue
        provider_used = provider
        # Объединяем сниппеты в один текстовый блок для LLM
        text_blob = "\n\n".join(
            f"[{r.get('title','')}] {r.get('snippet','')}"
            for r in results if r.get("snippet")
        )
        if not text_blob:
            continue
        # Берём топ-2 URL для атрибуции (LLM сам выберет какой факт откуда)
        urls = [r.get("url", "") for r in results if r.get("url")][:3]
        url_str = " | ".join(urls)
        llm_facts = _llm_extract_numeric_facts(
            text_blob, topic, f"Web ({provider})", url_str
        )
        for fact in llm_facts:
            fact["confidence"] = "medium"
            all_facts.append(fact)

    logger.info("Web search total: %d фактов для «%s» через %s",
                len(all_facts), topic, provider_used)
    return all_facts, provider_used


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

_RELEVANCE_PROMPT = """Тема ролика: «{topic}».
Ниже факты, собранные поиском. Поиск ошибается: попадаются однофамильцы, фильмы
и статьи с похожим названием, но про другое.

Оставь только факты, которые сценарист может использовать в ролике на эту тему
(они про тот же феномен, механизм, поведение или исследование).

Факты:
{facts}

Верни JSON-объект: {{"relevant_ids": [1, 4]}}"""


def _filter_relevant(topic: str, facts: list[dict]) -> list[dict]:
    """Отсеивает нерелевантные факты через LLM. При сбое LLM возвращает все факты."""
    if len(facts) < 2:
        return facts
    listing = "\n".join(f"{i}. {f.get('text', '')[:300]}" for i, f in enumerate(facts, 1))
    try:
        from utils.llm_client import chat
        resp = chat([{"role": "user", "content": _RELEVANCE_PROMPT.format(topic=topic, facts=listing)}],
                    tier="fast", temperature=0.0, max_tokens=300, json_mode=True,
                    caller="research_relevance")
        data = json.loads(resp.choices[0].message.content)
        ids = {int(i) for i in (data.get("relevant_ids") or [])}
    except Exception as exc:
        logger.warning("Research Agent: фильтр релевантности недоступен (%s) — оставляю все факты",
                       str(exc)[:150])
        return facts
    kept = [f for i, f in enumerate(facts, 1) if i in ids]
    logger.info("Research Agent: релевантных фактов %d из %d", len(kept), len(facts))
    return kept


def research(topic: str, query: str | None = None) -> dict[str, Any]:
    """
    Собирает факты по теме из нескольких источников.

    Args:
        topic: Тема/заголовок видео (на русском) — для оценки релевантности
        query: Поисковый запрос (термин, 2-5 слов). Заголовки вроде «Почему вы не
               ленитесь» дают в Wikipedia фильмы «Почему?» — поэтому ищем по query.

    Returns:
        dict с полями:
          facts        — list[{"text", "source", ...}], минимум 5 элементов
          summary      — строка-резюме для логов
          sources_used — список задействованных источников
    """
    q = (query or topic).strip()
    logger.info("Research Agent: исследую «%s» (поиск: «%s»)", topic, q)

    all_facts: list[dict] = []
    sources_used: list[str] = []

    wiki = _wiki_facts(q)
    if wiki:
        for f in wiki:
            f.setdefault("confidence", "high")
        all_facts.extend(wiki)
        sources_used.append("Wikipedia")

    # English Wikipedia — для тем с латинскими словами (iPhone, ChatGPT и т.п.)
    wiki_en = _wiki_en_facts(q)
    if wiki_en:
        for f in wiki_en:
            f.setdefault("confidence", "high")
        all_facts.extend(wiki_en)
        sources_used.append("Wikipedia EN")

    scholar = _scholar_facts(q)
    if scholar:
        for f in scholar:
            f.setdefault("confidence", "high")
        all_facts.extend(scholar)
        sources_used.append("Semantic Scholar")

    # Web search — всегда запускаем: Wikipedia+Scholar дают устоявшиеся факты,
    # веб-поиск с текущим годом добавляет свежие данные и актуальную статистику.
    web, web_provider = _web_facts(q)
    if web:
        all_facts.extend(web)
        sources_used.append(f"Web ({web_provider})")

    news = _news_facts(q)
    if news:
        for f in news:
            f["confidence"] = "fresh"  # свежие новости — особый тег для script_writer
        all_facts.extend(news)
        sources_used.append("Google News")

    # Numbers API намеренно убран: возвращает факты на английском языке,
    # которые загрязняют русскоязычный контекст сценаристу.

    all_facts = _filter_relevant(f"{topic} ({q})" if q != topic else topic, all_facts)

    if len(all_facts) < 5:
        logger.warning(
            "Research Agent: собрано только %d фактов (минимум 5) из: %s",
            len(all_facts),
            ", ".join(sources_used) or "нет источников",
        )

    summary = (
        f"Собрано {len(all_facts)} фактов по теме «{topic}» "
        f"из {len(sources_used)} источников: {', '.join(sources_used) or 'нет'}"
    )
    logger.info("Research Agent: %s", summary)

    return {
        "facts": all_facts,
        "summary": summary,
        "sources_used": sources_used,
    }


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    topic_arg = sys.argv[1] if len(sys.argv) > 1 else "психология"
    result = research(topic_arg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
