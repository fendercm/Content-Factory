"""
Idea Generator Agent — генерирует идеи для видео через Groq API.

Вход:  topics (list[dict]) — [{topic, virality, source}] от Trend Scout
Выход: list[dict] — топ-3 идеи:
       [{title, hook, angle, hashtags, source_topic, virality}]
"""

import json
import logging
import os
from typing import Any

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Ты — опытный контент-стратег для коротких вертикальных видео (TikTok, Reels, Shorts).
Твоя задача — превращать трендовые темы в конкретные идеи видео, которые наберут максимум просмотров.
Всегда отвечай строго в формате JSON без лишних комментариев.

ХОРОШАЯ ИДЕЯ:
• Отвечает на один конкретный вопрос зрителя («почему я откладываю важное?»),
  а не описывает область («психология прокрастинации»).
• Обещает инсайт, который зритель не ожидает: разрушает миф, называет скрытую причину,
  показывает ошибку, которую делают почти все.
• Раскрывается за 45-60 секунд одной мыслью, без списков из 10 пунктов.
• Не требует медицинских, финансовых или юридических советов и не касается политики —
  такие идеи отсеются фильтром, не трать на них место.

КРИТИЧЕСКИ ВАЖНО — правила по упоминанию людей:
• НЕ упоминай конкретных людей по имени, если не уверен в их профессии и биографии на 100%.
• Если тема связана с известной личностью и ты не можешь точно подтвердить факты — переформулируй идею в универсальную (без имён).
• Приоритет: универсальные темы (привычки, психология, советы, методики) — они безопаснее и охватывают более широкую аудиторию, чем биографии конкретных людей.
• Примеры ПЛОХИХ заголовков: "Как Немов стал чемпионом", "Секрет успеха Маска" — риск фактических ошибок.
• Примеры ХОРОШИХ заголовков: "5 привычек олимпийских чемпионов", "Психология успешных предпринимателей"."""

_USER_TEMPLATE = """НИША: {niche}

ВАЖНЕЙШЕЕ ТРЕБОВАНИЕ: все три идеи ОБЯЗАНЫ быть строго про нишу «{niche}».
Если входящие тренды НЕ связаны с нишей «{niche}» — ИГНОРИРУЙ их полностью и генерируй идеи самостоятельно в рамках ниши «{niche}».
Запрещено предлагать идеи на другие темы (технологии, iPhone, политика и т.п.) если ниша — «{niche}».
{viral_signal_block}
Вот список трендовых тем (используй только те, что реально относятся к нише «{niche}»):
{topics_json}

Три идеи должны РАЗЛИЧАТЬСЯ форматом подачи — выбери три разных из:
миф-разрушение · скрытая причина · типичная ошибка · неожиданный эксперимент/факт · сравнение «большинство vs немногие».
Одинаковые по структуре ролики подряд платформы считают шаблонным контентом и режут охват.

Верни JSON-объект с тремя идеями строго про «{niche}»:
{{"ideas": [
  {{
    "title": "заголовок видео до 60 символов: конкретный и интригующий, без кликбейт-лжи",
    "hook": "первая фраза ролика, 6-10 слов, на «ты»: шок-утверждение или провокация, НЕ вопрос",
    "angle": "формат подачи + главный инсайт, который зритель унесёт (1-2 предложения)",
    "hashtags": ["хэштег1", "хэштег2", "хэштег3", "хэштег4"],
    "source_topic": "исходная тема из входных данных или сама ниша если тренды нерелевантны",
    "research_query": "2-5 слов для поиска фактов: научный термин или явление (например: «прокрастинация эмоциональная регуляция»), НЕ заголовок",
    "virality": 0.0
  }}
]}}

Хэштеги — 3-5 штук, без решётки, строчными: 1-2 широких по нише + 2-3 узких по теме ролика, в основном по-русски.
virality скопируй из входных данных для этой темы (или 0.5, если идея придумана самостоятельно).
Если тема подразумевает конкретного человека — переформулируй идею в универсальную (без имён и биографий)."""


def _load_viral_patterns(db_path: str, days: int = 30, limit: int = 5) -> list[dict]:
    """
    Загружает топ-N успешных видео из performance таблицы за последние N дней.
    Возвращает list[{title, views, platform}] для feedback в idea_generator.
    Если таблицы или данных нет — пустой список.
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        conn = sqlite3.connect(db_path)
        # Проверяем что таблица существует и имеет колонки views/title
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='performance'"
        )
        if not cur.fetchone():
            conn.close()
            return []
        rows = conn.execute(
            """SELECT title, platform, COALESCE(views, 0) as v
               FROM performance
               WHERE title IS NOT NULL AND title != ''
                 AND COALESCE(views, 0) > 0
                 AND (published_at IS NULL OR published_at >= ?)
               ORDER BY v DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        conn.close()
        return [{"title": r[0], "platform": r[1], "views": r[2]} for r in rows]
    except Exception as exc:
        # warning, а не debug: раньше обратная связь от аналитики молча не работала
        logger.warning("Viral patterns load error: %s", exc)
        return []


def _format_viral_block(patterns: list[dict]) -> str:
    """Формирует блок ПРОШЛЫЕ ХИТЫ для промпта."""
    if not patterns:
        return ""
    lines = [
        "",
        "ПРОШЛЫЕ ХИТЫ КАНАЛА (вдохновляйся тоном/формулой, но не копируй темы):",
    ]
    for p in patterns:
        v = p["views"]
        v_str = f"{v // 1000}k" if v >= 1000 else str(v)
        lines.append(f"  • «{p['title']}» — {v_str} views")
    lines.append("")
    return "\n".join(lines)


def run(topics: list[dict[str, Any]], niche: str = "") -> list[dict[str, Any]]:
    """Генерирует топ-3 идеи видео из переданных тем."""
    if not topics:
        raise ValueError("Список тем пуст")

    # Analytics feedback — подсасываем топ-5 виралов канала за 30 дней
    db_path = os.getenv("DB_PATH", "pipeline.db")
    viral_patterns = _load_viral_patterns(db_path, days=30, limit=5)
    viral_block = _format_viral_block(viral_patterns)
    if viral_patterns:
        logger.info("Idea Generator: подключено %d виральных паттернов из performance", len(viral_patterns))

    topics_json = json.dumps(topics, ensure_ascii=False, indent=2)
    user_prompt = _USER_TEMPLATE.format(
        topics_json=topics_json,
        niche=niche or "общая",
        viral_signal_block=viral_block,
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    logger.info("Отправляю %d тем в LLM для генерации идей", len(topics))

    from utils.llm_client import chat
    response = chat(messages, tier="fast", temperature=0.8, max_tokens=1200,
                    json_mode=True, caller="idea_generator")

    raw = response.choices[0].message.content.strip()

    # Убираем возможные markdown-блоки
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        ideas = json.loads(raw)
        if isinstance(ideas, dict):
            ideas = next((v for v in ideas.values() if isinstance(v, list)), [])
    except json.JSONDecodeError:
        # Ищем JSON-массив в тексте (LLM иногда добавляет преамбулу)
        import re as _re
        m = _re.search(r"\[.*\]", raw, _re.DOTALL)
        if m:
            ideas = json.loads(m.group())
        else:
            raise ValueError(f"Idea Generator: невалидный JSON от LLM: {raw[:200]}")

    # Гарантируем нужные поля и лимит топ-3
    required = {"title", "hook", "angle", "hashtags", "source_topic", "virality"}
    validated = []
    for idea in ideas[:3]:
        missing = required - idea.keys()
        if missing:
            logger.warning("Идея пропущена — отсутствуют поля: %s", missing)
            continue
        idea["hashtags"] = [str(h).lstrip("#").strip().lower() for h in idea.get("hashtags") or [] if str(h).strip()][:5]
        try:
            idea["virality"] = float(idea.get("virality") or 0.5)
        except (TypeError, ValueError):
            idea["virality"] = 0.5
        validated.append(idea)

    logger.info("Idea Generator вернул %d идей", len(validated))
    return validated


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    sample_topics = [
        {"topic": "психология манипуляций", "virality": 0.85, "source": "google_trends"},
        {"topic": "как перестать прокрастинировать", "virality": 0.72, "source": "reddit"},
        {"topic": "эффект Даннинга-Крюгера", "virality": 0.60, "source": "google_trends"},
    ]
    results = run(sample_topics)
    print(json.dumps(results, ensure_ascii=False, indent=2))
