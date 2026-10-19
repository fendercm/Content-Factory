"""
Script Writer Agent — генерирует сценарий видео через общую LLM-цепочку
(utils.llm_client, tier="quality": Gemini Flash → Groq → Gemini Flash-Lite → …).

Вход:  topic, platform, research_context (факты), бриф идеи (hook/angle),
       avoid_hooks — недавние hook'и канала, чтобы ролики не были шаблонными
Выход: JSON с полями hook, body, cta, full_script, min_words, max_words

Стратегия двух шагов:
  1. Генерируем full_script как сплошной текст в диапазоне слов пресета (без JSON)
  2. Парсим текст на hook/body/cta через второй LLM-вызов
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Literal

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

Platform = Literal["tiktok", "youtube", "instagram", "linkedin", "twitter"]

PLATFORM_GUIDELINES = {
    "tiktok":    {"tone": "дерзкий, вызывающий, конфронтационный",    "max_words": 115},
    "youtube":   {"tone": "дерзкий, экспертный, шокирующий",            "max_words": 115},
    "instagram": {"tone": "дерзкий, личный, конфронтационный",          "max_words": 115},
    "linkedin":  {"tone": "профессиональный, острый, провокационный",   "max_words": 120},
    "twitter":   {"tone": "прямой, острый, провокационный",             "max_words": 110},
}

# Multi-shot: длительность видео по глубине темы.
# Жёсткий минимум: короче 120 слов видео выходит <40с с рваным темпом
MIN_WORDS_FLOOR = 120

# Карта target_sec → (min_words, max_words). Темп речи ~2.5 слова/сек у Yandex alena.
DURATION_PRESETS = {
    45: (120, 135),   # короткий — простые темы; <120 слов даёт видео <40с и рваный темп
    60: (140, 160),   # средний — глубже, требует развития
    75: (175, 200),   # длинный — наука, философия, исторические разборы
    90: (210, 240),   # макси — для YouTube Shorts (60-90с лимит)
}


def recommend_duration(topic: str, research_facts: list[dict] | None = None, platform: str = "youtube") -> int:
    """
    Возвращает рекомендованную длительность (сек) на основе глубины темы.
    Эвристика:
      - Кол-во HIGH-confidence фактов
      - Ключевые слова темы (наука/философия → длиннее)
      - Платформа (Twitter короче, YouTube Shorts длиннее)

    Default 45с — если ничего не подходит.
    """
    facts = research_facts or []
    high_facts = sum(1 for f in facts if f.get("confidence") == "high")
    total_facts = len(facts)

    topic_lower = (topic or "").lower()
    deep_keywords = (
        "наук", "филосо", "истор", "квант", "космо", "эволюц", "психоана",
        "нейро", "теор", "парадокс", "сознан", "релятив",
    )
    is_deep = any(k in topic_lower for k in deep_keywords)

    # Twitter — всегда короткое
    if platform == "twitter":
        return 45

    # Расчёт
    if is_deep and high_facts >= 5:
        return 75
    if is_deep or high_facts >= 6 or total_facts >= 10:
        return 60
    return 45

# Разрешённые имена — только умершие классики, без юр-риска
_ALLOWED_NAMES = (
    "Юнг, Фрейд, Дарвин, Эйнштейн, Кант, Ницше, Сократ, Платон, "
    "Аристотель, Тесла, Павлов, Скиннер, Ферми, Кюри"
)

# Стоп-категории — пайплайн не должен генерировать видео на эти темы
_STOP_TOPICS = (
    "политика и политики, выборы, война, военные действия, "
    "медицинские советы по лечению болезней, диагнозы, лекарства, дозировки, "
    "секс и эротика, наркотики, суицид, насилие"
)

# ---------------------------------------------------------------------------
# Промпт шаг 1: генерация сплошного текста
# ---------------------------------------------------------------------------

_SCRIPT_SYSTEM = f"""Ты сценарист вирусных TikTok/Shorts с 10M+ просмотров. Сейчас {datetime.now().year} год.
Стиль: ДЕРЗКИЙ, ВЫЗЫВАЮЩИЙ, конфронтационный — как будто бьёшь зрителя по лицу правдой.
Пишешь только на русском (кириллица, без иероглифов и латиницы, кроме имён), от 1-го лица ("я"),
с прямым обращением к зрителю. Текст будет озвучен синтезатором речи — пиши так, как говорят:
числа можно цифрами, без скобок, списков, эмодзи и сокращений вроде «т.е.», «и т.д.».
Ты рассказчик, а не врач и не финансовый консультант: объясняешь, как устроены мышление
и поведение, но не назначаешь лечение и не говоришь, куда вкладывать деньги.
Если в FACTS-блоке есть [NOW]-факты — используй их как свежий повод, но опирайся на [HIGH].
ТОЛЬКО текст без меток, заголовков, JSON и markdown."""

_FEW_SHOT = """
════════════ ЭТАЛОННЫЕ СЦЕНАРИИ (копируй темп и тон; длина — по заданию ниже) ════════════

ВАЖНО: цифры и атрибуции в примерах взяты из их FACTS-блоков. Ты бери свои
ТОЛЬКО из своего FACTS-блока ниже — никогда не переноси цифры из примеров.

ПРИМЕР 1 · психология (hook=провокация, твист + 2 контраста):
Ты прокручиваешь старые ссоры в голове — и сам продлеваешь себе стресс.
Я сам так жил годами и не понимал, почему напряжение не уходит.
Большинство гоняет один и тот же разговор часами. Ищет, что надо было ответить. Это не анализ — это петля. Звучит знакомо?
Но вот что меняет всё: я начал называть эмоцию вслух. Просто "я злюсь" или "мне страшно". Исследование UCLA показало: когда называешь чувство словами, активность миндалины падает. Это не эзотерика — это нейробиология.
А ты — прокручиваешь старые разговоры или умеешь их отпускать?

ПРИМЕР 2 · продуктивность (hook=парадокс, 2 контраста):
Чем больше задач ты делаешь параллельно — тем меньше успеваешь.
Я раньше гордился, что закрываю 10 чатов одновременно.
А потом наткнулся на исследование Калифорнийского университета в Ирвайне: после отвлечения человек возвращается к задаче в среднем через 23 минуты. Открыл почту — минус 23. Ответил в чат — минус 23. Вдумайся.
Я перешёл на блоки по 90 минут. Одна задача. Телефон лицом вниз. За 4 часа закрываю больше, чем раньше за день. И это не сила воли — это архитектура.
Скажи честно — сколько раз ты сегодня переключился между задачами?

ПРИМЕР 3 · отношения (hook=провокация-императив, твист):
Перестань говорить партнёру "ты" в ссоре. Прямо сегодня.
Я сам понял это слишком поздно — после развода.
Несчастные пары атакуют: "ты всегда опаздываешь", "ты никогда не слушаешь". Слово "ты" — это удар по личности. Мозг партнёра включает защиту, и диалога нет. Семейные психологи давно называют критику личности одним из главных предвестников развода.
А счастливые говорят то же самое, но через "я": "мне больно, когда жду". Тот же смысл — но партнёр не атакован. Он слышит.
Покажи это своему партнёру — и напиши, что он ответил.
════════════════════════════════════════════════════════════════
"""

_SCRIPT_USER = _FEW_SHOT + """
Напиши сценарий для {platform} на тему: "{topic}"
Тон: {tone}. ОТ 1-ГО ЛИЦА ("я"). Длина видео — 45 секунд.
{brief_block}{avoid_block}
СТОП-ТЕМЫ (если попадает в одну — откажись одной строкой "STOP: <причина>"):
{stop_topics}

РАЗРЕШЁННЫЕ ИМЕНА (ссылаться ТОЛЬКО в контексте их реальных открытий):
{allowed_names}
Как использовать: "Павлов показал, что мозг формирует рефлексы" — ДА.
НЕ: "как говорил Павлов, концентрация ведёт к успеху" — он физиолог, а не коуч.
Принцип: упоминание = их реальная область (рефлексы, архетипы, эволюция, физика).
ЗАПРЕЩЕНО приписывать классикам цитаты об успехе/продуктивности/деньгах.
ЗАПРЕЩЕНО упоминать политиков, селебрити и блогеров. Учёных из FACTS можно называть
только вместе с их исследованием из FACTS, других живых людей — нельзя.

{research_block}

ОБЯЗАТЕЛЬНАЯ СТРУКТУРА (5 блоков, строго по порядку, всё от 1-го лица):

① HOOK [0-3 сек · 6-10 слов, одно предложение]:
   Зритель решает листать дальше за 1.5-2.5 секунды — hook должен быть коротким
   и понятным без контекста. Он же выводится крупным текстом на первом кадре.
   Выбери ОДНУ формулу под тему:
   A. СТАТИСТИКА-ШОК: "X% людей делают Y — и разрушают свою Z" (только если X есть в FACTS)
   B. ПРОВОКАЦИЯ-ИМПЕРАТИВ: "Перестань делать X прямо сейчас"
   C. ПАРАДОКС: "Чем больше ты X — тем меньше получаешь Y"
   D. МИКРО-ИСТОРИЯ: "Я делал X 10 лет — пока не узнал что"
   ЗАПРЕЩЕНО: вопрос, "Привет", "Сегодня", "В этом видео".

② SETUP — личная связь [3-8 сек · 12-18 слов]:
   КОНКРЕТНАЯ бытовая история от 1-го лица: не "я был поражён" — а ЧТО именно ты делал/терял.
   Примеры: "Я так жил 5 лет." / "Я потерял на этом 2 года." / "Я делал это каждый день."
   Обязательно "я" + обращение к зрителю на "ты".
   Без диагнозов, лекарств, сумм заработка и «я заработал X» — это звучит как совет и
   выглядит недостоверно.

③ КОНТРАСТ 1 — плохой/обычный путь [8-22 сек · 25-32 слова]:
   Опиши что делает большинство и почему это РАЗРУШИТЕЛЬНО — дерзко, без мягкости.
   Если в FACTS-блоке есть факт с цифрой — используй его. Нет фактов — обойдись без цифр.
   Атрибуцию (университет, организация, год, автор) называй ТОЛЬКО если она есть
   в тексте факта или в его источнике. Нет атрибуции в факте — пиши
   "исследования показывают", НЕ придумывай Стэнфорд/Гарвард/год.
   При confidence=med — один раз hedge: "по некоторым данным...".
   ЗАПРЕЩЕНО называть источником "Google", "Wikipedia"/"Википедия", новостные сайты и поисковики —
   это не авторитетно. Источник — только исследователи/организации, названные в тексте факта.
   Замени: "по данным Google News" → "по свежим данным" / "по последним исследованиям".
   ОБЯЗАТЕЛЬНО после цифры/факта — ударная фраза из 2-4 слов, которая усиливает эмоцию,
   но не добавляет нового утверждения: "Вдумайся." / "Перечитай это." / "Узнаёшь себя?" / "Вот где подвох."
   НЕ используй абсолюты-утверждения ("Каждый раз.", "Без исключений.", "Это эпидемия.") —
   fact_checker засчитывает их как непроверенную статистику.
   ЗАПРЕЩЕНО: "иногда", "в некоторых случаях", "не всегда", "возможно", "ты не одинок", "особенность", "это нормально".

④ КОНТРАСТ 2 — правильный путь / ТВИСТ [22-38 сек · 28-35 слов]:
   Начни с "А я..." / "Но вот что меняет всё:" / "А успешные делают наоборот:"
   ПРАВИЛО: ты (большинство) = делает плохо. Я = нашёл решение и изменил жизнь.
   НЕ ОБЪЯСНЯЙ почему оба подхода правильны — выбери СТОРОНУ.
   Добавь конкретику: что именно ты делаешь иначе, с каким результатом.
   Пример: "Я перешёл на X. Результат за 3 недели — Y. Это не мотивация — это биология."
   Можно сослаться на классика из разрешённого списка ("как говорил Юнг...").

⑤ CTA-ВОПРОС [38-45 сек · СТРОГО 8-12 слов, не больше]:
   ЗАВЕРШИ ОДНИМ коротким вопросом к аудитории. Не "подпишись". Не "лайк".
   Формулы (8-12 слов!): "А ты — делаешь так же?" / "Скажи честно — у тебя так?" /
   "Напиши в комментарии: ты знал об этом?"
   ЗАПРЕЩЕНО: вопросы длиннее 12 слов. Одно предложение. Точка или вопросительный знак.

СТИЛЬ:
- Короткие рваные предложения (5-9 слов). Темп как рэп.
- Прямое обращение: "я", "ты", "твой".
- Конкретные цифры — только из FACTS-блока. Своих цифр, процентов и вузов не придумывай:
  fact_checker сверяет каждую цифру и атрибуцию с FACTS и отклоняет выдуманные.
- ЗАПРЕЩЕНО: "важно", "следует", "таким образом", "подводя итог", "необходимо", "данный".
- Дерзость — в мысли, а не в лексике: без мата, грубостей и слов про насилие и секс даже
  в переносном смысле («насиловать мозг», «убивает») — автомодерация платформ режет охват.
- Не придумывай даты, «учёные доказали» и «по свежим данным» без факта из FACTS-блока.

ДЛИНА: ровно 100-115 слов. Посчитай перед ответом.
Если меньше 100 — разверни блоки ③ и ④.
Если больше 115 — сократи setup и убери лишние пояснения.

Напиши ТОЛЬКО текст монолога. Без заголовков. Без меток. Без JSON."""

# ---------------------------------------------------------------------------
# Промпт шаг 2: парсинг текста в JSON
# ---------------------------------------------------------------------------

_PARSE_SYSTEM = "Ты парсер текста. Отвечаешь только JSON без markdown."

_PARSE_USER = """Раздели этот сценарий на три части и верни JSON.

Сценарий:
{script_text}

Правила:
- hook: первые 1-2 предложения (крючок, до 20 слов)
- cta: последнее предложение (призыв к действию)
- body: всё между hook и cta (основная часть)

Верни ТОЛЬКО:
{{"hook": "...", "body": "...", "cta": "..."}}"""

# ---------------------------------------------------------------------------
# Блоки с фактами
# ---------------------------------------------------------------------------

_RESEARCH_BLOCK = """
┌──────────────────────────────────────────────────────────────────────┐
│  ФАКТЫ ДЛЯ СЦЕНАРИЯ — актуально на {year} год                        │
│  [HIGH] — можно цитировать конкретно, атрибуция — из самого факта    │
│  [MED]  — нужен hedge ("по некоторым исследованиям...")              │
│  [NOW]  — свежие новости {year}: повод «на этой неделе писали...»,   │
│           цифры из заголовков — только с hedge «по свежим данным»    │
│  [LOW]  — только как фоновая мысль, без конкретики                   │
└──────────────────────────────────────────────────────────────────────┘
{facts_list}

Распределение:
• Блок ③ КОНТРАСТ 1: самый сильный факт с цифрой (HIGH > NOW > MED)
• Блок ④ КОНТРАСТ 2: альтернатива/решение/твист (HIGH/MED)
[NOW] — хороший повод показать актуальность, но не главный аргумент.
ЗАПРЕЩЕНО придумывать свои цифры. Бери ТОЛЬКО из списка."""

_BRIEF_BLOCK = """
БРИФ ИДЕИ (держись этого угла, но формулировки — свои):
  Крючок из брифа: {hook}
  Угол подачи: {angle}
"""

_AVOID_BLOCK = """
НЕДАВНИЕ HOOK'И КАНАЛА — не повторяй их формулу, первые слова и конструкцию
(шаблонные ролики подряд режут охват и монетизацию):
{hooks}
"""

_NO_RESEARCH_BLOCK = """Фактической базы нет. ЗАПРЕЩЕНО использовать конкретные цифры, проценты,
названия университетов, организаций и годы исследований — их нечем подтвердить,
и fact_checker отклонит сценарий. Строй контрасты на наблюдениях и личном опыте.
Формулу hook'а A (статистика-шок) не используй."""


def _sanitize_script(text: str) -> str:
    """Удаляет CJK-символы и прочий мусор LLM (找到, 的 и т.п.)."""
    # Убираем CJK: китайский, японский, корейский
    cleaned = re.sub(r'[⺀-鿿豈-﫿︰-﹏]+', '', text)
    # Убираем двойные пробелы
    return re.sub(r' {2,}', ' ', cleaned).strip()


def _format_research_block(facts: list[dict]) -> str:
    if not facts:
        return ""
    # Сортируем: high → fresh → medium → low. Свежие новости — это заголовки,
    # их цифры менее надёжны, чем Wikipedia / Semantic Scholar
    conf_order = {"high": 0, "fresh": 1, "medium": 2, "low": 3}
    sorted_facts = sorted(
        facts, key=lambda f: conf_order.get(f.get("confidence", "low"), 4)
    )
    lines = []
    for i, f in enumerate(sorted_facts[:8], 1):
        raw_conf = f.get("confidence", "low")
        if raw_conf == "fresh":
            conf_label = "NOW"   # Google News — свежие новости
        elif raw_conf == "high":
            conf_label = "HIGH"
        elif raw_conf == "medium":
            conf_label = "MED"
        else:
            conf_label = "LOW"
        # Названия агрегаторов (Wikipedia, Google News, DuckDuckGo) не показываем:
        # модель вставляет их в текст («Википедия напоминает…»), а это не авторитетно.
        # Атрибуция исследования, если она есть, уже внутри текста факта.
        source = str(f.get("source", ""))
        hide = re.search(r"wiki|news|duckduckgo|tavily|brave|serper|google|web", source, re.I)
        src_str = f" — {source}" if source and not hide else ""
        lines.append(f"{i}. [{conf_label}] {f['text']}{src_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

_HOOK_AB_SYSTEM = """Ты — эксперт по виральным TikTok-hook'ам.
Анализируешь варианты первого предложения видео и выбираешь самый виральный.
Отвечаешь ТОЛЬКО JSON, без markdown."""

_HOOK_AB_USER = """Тема видео: "{topic}"

Текущий сценарий (для контекста):
{script_excerpt}

Сгенерируй 2 АЛЬТЕРНАТИВНЫХ hook'а к этому видео — каждый по РАЗНОЙ формуле,
ОТЛИЧНОЙ от текущего:
  A. СТАТИСТИКА-ШОК ("X% людей делают Y и разрушают свою Z")
  B. ПРОВОКАЦИЯ-ИМПЕРАТИВ ("Перестань делать X прямо сейчас.")
  C. ПАРАДОКС ("Чем больше ты X — тем меньше Y.")
  D. МИКРО-ИСТОРИЯ ("Я делал X 10 лет, пока не узнал что...")

Текущий hook:
"{current_hook}"

ЖЁСТКИЕ ОГРАНИЧЕНИЯ (нарушение = дисквалификация hook'а):
• ЗАПРЕЩЕНО начинать с вопроса или заканчивать знаком ?
• ЗАПРЕЩЕНО: "Привет", "Сегодня", "В этом видео", "Вы знали"
• ОБЯЗАТЕЛЬНО: конкретная цифра или глагол-действие в первых 5 словах
• Цифры — ТОЛЬКО те, что уже есть в сценарии. Новые цифры, проценты и вузы не придумывай
• Длина строго 6-10 слов, одно предложение: его читают крупным текстом за 1-2 секунды
• Hook обещает ровно то, что раскрывает сценарий — без обмана ожиданий
{avoid_block}

Затем сравни все 3 варианта и выбери самый ВИРАЛЬНЫЙ —
тот, который ЗАСТАВИТ зрителя остановить скролл. Критерии: шок, конкретика,
неожиданность, ясность. Вопросы не вирусны — они слабее утверждений.

Верни JSON:
{{
  "alternatives": ["новый hook A", "новый hook B"],
  "all_hooks": ["текущий hook (повтор)", "новый A", "новый B"],
  "chosen_index": 0,
  "reasoning": "почему выбран"
}}"""


_FIX_SYSTEM = """Ты редактор сценариев коротких видео. Исправляешь только указанные
фактические ошибки и сохраняешь всё остальное: стиль, структуру, длину, первое предложение
и финальный вопрос. Пишешь только на русском. Отвечаешь только исправленным текстом сценария."""

_FIX_USER = """Сценарий:
{script}

Замечания фактчекера:
{errors}

FACTS (единственная разрешённая база для цифр и атрибуций):
{facts}

Исправь каждую проблемную фразу одним из способов:
- замени на подходящий факт из FACTS (с его атрибуцией);
- или убери конкретику: цифру, год, вуз, «учёные доказали» → «исследования показывают» / мысль без цифры;
- или перепиши фразу как личное наблюдение рассказчика.
Длина — {min_words}-{max_words} слов. Верни только текст сценария, без пояснений."""


def _split_fields(full_script: str) -> dict:
    """hook / body / cta из сплошного текста без LLM (первое и последнее предложения)."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", full_script.strip()) if s.strip()]
    if len(sentences) < 3:
        return {"hook": full_script[:100], "body": full_script, "cta": ""}
    return {"hook": sentences[0], "body": " ".join(sentences[1:-1]), "cta": sentences[-1]}


def fix_script(script: dict, errors: list[str], facts: list[dict] | None = None) -> dict | None:
    """
    Исправляет сценарий по замечаниям fact_checker'а (один LLM-вызов).
    Возвращает новый dict сценария или None, если правка не удалась.
    """
    full = script.get("full_script", "")
    min_w = int(script.get("min_words") or MIN_WORDS_FLOOR)
    max_w = int(script.get("max_words") or min_w + 30)
    facts_block = _format_research_block(facts or []) or "(пусто — значит, без цифр и атрибуций)"
    try:
        from utils.llm_client import chat
        resp = chat(
            [{"role": "system", "content": _FIX_SYSTEM},
             {"role": "user", "content": _FIX_USER.format(
                 script=full, errors="\n".join(f"- {e}" for e in errors),
                 facts=facts_block, min_words=min_w, max_words=max_w)}],
            tier="quality", temperature=0.3, max_tokens=900, caller="script_writer_fix",
        )
        fixed = _sanitize_script(resp.choices[0].message.content.strip().strip('"«»'))
    except Exception as exc:
        logger.warning("Script fix недоступен: %s", str(exc)[:150])
        return None
    words = len(fixed.split())
    if words < int(min_w * 0.9):
        logger.warning("Script fix: %d слов < минимума %d — отклоняю правку", words, min_w)
        return None
    return {**script, **_split_fields(fixed), "full_script": fixed}


def improve_hook(full_script: str, topic: str, avoid_hooks: list[str] | None = None) -> dict:
    """
    A/B: генерирует 2 альтернативных hook'а к существующему сценарию + LLM-judge
    выбирает лучший из 3. Возвращает {script, hook, alternatives, chosen_index}.

    При ошибке возвращает исходный сценарий без изменений.
    """
    # Извлекаем текущий hook — первое предложение
    sentences = [s.strip() for s in re.split(r"[.!?]+", full_script) if len(s.strip()) > 5]
    if not sentences:
        return {"script": full_script, "hook": full_script[:80], "chosen_index": 0, "alternatives": []}
    current_hook = sentences[0]

    user_prompt = _HOOK_AB_USER.format(
        topic=topic,
        script_excerpt=full_script[:700],
        current_hook=current_hook,
        avoid_block=_avoid_block(avoid_hooks),
    )

    try:
        from utils.llm_client import groq_with_cerebras_fallback
        resp = groq_with_cerebras_fallback(
            messages=[
                {"role": "system", "content": _HOOK_AB_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.8,
            max_tokens=500,
            response_format={"type": "json_object"},
            caller="script_writer_hook_ab",
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)
    except Exception as exc:
        logger.warning("Hook A/B ошибка: %s — оставляем исходный hook", exc)
        return {"script": full_script, "hook": current_hook, "chosen_index": 0, "alternatives": []}

    alternatives = data.get("alternatives") or []
    all_hooks = data.get("all_hooks") or ([current_hook] + alternatives)
    chosen_idx = int(data.get("chosen_index", 0))
    if chosen_idx < 0 or chosen_idx >= len(all_hooks):
        chosen_idx = 0

    chosen_hook = all_hooks[chosen_idx]
    # Цифр, которых нет в сценарии, в hook быть не должно: модель любит «94% людей…»
    new_numbers = set(re.findall(r"\d+", chosen_hook)) - set(re.findall(r"\d+", full_script))
    if chosen_idx != 0 and new_numbers:
        logger.info("Hook A/B: вариант %d с новыми цифрами %s отклонён — оставляю исходный",
                    chosen_idx, sorted(new_numbers))
        chosen_idx, chosen_hook = 0, all_hooks[0] if all_hooks else current_hook
    logger.info(
        "Hook A/B: выбран вариант %d из %d — %s",
        chosen_idx, len(all_hooks), chosen_hook[:60],
    )

    # Заменяем первое предложение в full_script на выбранный hook
    if chosen_idx == 0 or chosen_hook.strip() == current_hook.strip():
        # Оставляем как было
        new_script = full_script
    else:
        # Меняем первое предложение
        # Ищем первое разделение по . ! ?
        m = re.search(r"[.!?]", full_script)
        if m:
            tail = full_script[m.end():].strip()
            chosen_clean = chosen_hook.strip().rstrip(".!?")
            new_script = f"{chosen_clean}.\n\n{tail}"
        else:
            new_script = chosen_hook + " " + full_script

    return {
        "script": new_script,
        "hook": chosen_hook,
        "chosen_index": chosen_idx,
        "alternatives": all_hooks,
    }


def _brief_block(idea_hook: str | None, idea_angle: str | None) -> str:
    if not (idea_hook or idea_angle):
        return ""
    return _BRIEF_BLOCK.format(hook=(idea_hook or "—").strip(), angle=(idea_angle or "—").strip())


def _avoid_block(avoid_hooks: list[str] | None) -> str:
    hooks = [h.strip() for h in (avoid_hooks or []) if h and h.strip()][:8]
    if not hooks:
        return ""
    return _AVOID_BLOCK.format(hooks="\n".join(f"  - {h}" for h in hooks))


def write_script(
    topic: str,
    platform: Platform,
    research_context: list[dict] | None = None,
    target_duration_sec: int | None = None,
    idea_hook: str | None = None,
    idea_angle: str | None = None,
    avoid_hooks: list[str] | None = None,
) -> dict:
    """
    Генерирует сценарий видео по вирусной формуле 2026.

    Стратегия: 2 LLM-вызова
      1. Сплошной текст в диапазоне слов пресета (без JSON-ограничений)
      2. Парсинг в структуру hook/body/cta

    Args:
        topic:            Тема видео
        platform:         Целевая платформа
        research_context: Список фактов от research_agent (опционально)

    Returns:
        dict с полями: hook, body, cta, full_script, min_words, max_words

    Raises:
        ValueError:   если platform не поддерживается
    """
    if platform not in PLATFORM_GUIDELINES:
        raise ValueError(
            f"Неподдерживаемая платформа: {platform}. "
            f"Допустимые: {list(PLATFORM_GUIDELINES.keys())}"
        )

    guidelines = PLATFORM_GUIDELINES[platform]

    # Multi-shot: длина зависит от target_duration_sec (если задан)
    target_dur = target_duration_sec or 45
    # Snap к ближайшему пресету
    closest_preset = min(DURATION_PRESETS.keys(), key=lambda k: abs(k - target_dur))
    min_words, max_words = DURATION_PRESETS[closest_preset]
    logger.info(
        "Script Writer: target=%dс → диапазон %d-%d слов",
        closest_preset, min_words, max_words,
    )

    current_year = datetime.now().year
    research_block = (
        _RESEARCH_BLOCK.format(
            facts_list=_format_research_block(research_context),
            year=current_year,
        )
        if research_context
        else _NO_RESEARCH_BLOCK
    )

    user_prompt = _SCRIPT_USER.format(
        topic=topic,
        platform=platform.upper(),
        tone=guidelines["tone"],
        research_block=research_block,
        stop_topics=_STOP_TOPICS,
        allowed_names=_ALLOWED_NAMES,
        brief_block=_brief_block(idea_hook, idea_angle),
        avoid_block=_avoid_block(avoid_hooks),
    )
    # Прокидываем целевую длину в промпт — заменяем фиксированные 100-115 на динамику
    user_prompt = user_prompt.replace(
        "ДЛИНА: ровно 100-115 слов",
        f"ДЛИНА: ровно {min_words}-{max_words} слов",
    ).replace(
        "Длина видео — 45 секунд",
        f"Длина видео — {closest_preset} секунд",
    ).replace(
        "Если меньше 100 — разверни",
        f"Если меньше {min_words} — разверни",
    ).replace(
        "Если больше 115 — сократи",
        f"Если больше {max_words} — сократи",
    )

    from utils.llm_client import groq_with_cerebras_fallback

    def _chat(messages: list[dict], temperature: float, max_tokens: int,
              response_format: dict | None = None) -> object:
        return groq_with_cerebras_fallback(
            messages=messages,
            yandex_model="deepseek-v3",
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            caller="script_writer",
        )

    # ── Шаг 1: генерация сплошного текста ───────────────────────────────────
    messages: list[dict] = [
        {"role": "system", "content": _SCRIPT_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    full_script = ""
    for attempt in range(3):
        resp = _chat(messages, temperature=0.85, max_tokens=900)
        raw_text = resp.choices[0].message.content.strip()

        # Стоп-тема — LLM отказался по нашему промпту
        if raw_text.upper().startswith("STOP:"):
            raise ValueError(f"Сценарист отказался от темы: {raw_text}")

        word_count = len(raw_text.split())

        # Целевой диапазон зависит от длительности; принимаем ±15% от max как ОК
        accept_min = max(int(min_words * 0.9), MIN_WORDS_FLOOR)
        accept_max = int(max_words * 1.1)
        if accept_min <= word_count <= accept_max:
            full_script = raw_text
            break

        # Перегенерируем — указываем направление
        messages.append({"role": "assistant", "content": raw_text})
        if word_count < accept_min:
            adjust = (
                f"Сценарий получился {word_count} слов — нужно {min_words}-{max_words}. "
                "Разверни блоки ③ КОНТРАСТ 1 и ④ КОНТРАСТ 2 — добавь конкретики и цифр. "
            )
        else:
            adjust = (
                f"Сценарий получился {word_count} слов — нужно {min_words}-{max_words}. "
                "Сократи setup до 1 предложения, убери лишние пояснения и повторы. "
            )
        messages.append({
            "role": "user",
            "content": adjust + "Напиши ТОЛЬКО текст монолога, без меток.",
        })

    if not full_script:
        # Берём последний ответ LLM как есть
        full_script = raw_text

    full_script = _sanitize_script(full_script)

    # ── Шаг 2: парсинг текста в JSON ────────────────────────────────────────
    parse_resp = _chat(
        messages=[
            {"role": "system", "content": _PARSE_SYSTEM},
            {"role": "user", "content": _PARSE_USER.format(script_text=full_script)},
        ],
        temperature=0.1,
        max_tokens=900,
        response_format={"type": "json_object"},
    )
    raw_json = parse_resp.choices[0].message.content.strip()

    try:
        result = json.loads(raw_json)
    except json.JSONDecodeError:
        # Не удалось распарсить — синтезируем структуру из full_script напрямую
        result = {}

    # Гарантируем наличие full_script
    if not isinstance(result, dict):
        result = {}
    result["full_script"] = full_script

    # Синтетический fallback для отсутствующих полей из full_script
    try:
        sentences = [s.strip() for s in re.split(r'[.!?]+', full_script) if len(s.strip()) > 10]
        if not result.get("hook"):
            result["hook"] = sentences[0] if sentences else full_script[:100]
        if not result.get("cta"):
            result["cta"] = sentences[-1] if sentences else ""
        if not result.get("body"):
            hook_end = full_script.find(result["hook"]) + len(result["hook"])
            cta_start = full_script.rfind(result["cta"])
            result["body"] = full_script[hook_end:cta_start].strip() if cta_start > hook_end else full_script
    except Exception as exc:
        logger.warning("Script field synthesis error: %s — using full_script as body", exc)
        result.setdefault("hook", full_script[:100])
        result.setdefault("cta", "")
        result.setdefault("body", full_script)

    # Orchestrator сверяет длину с этим диапазоном, а не с собственной константой
    result["min_words"] = max(int(min_words * 0.9), MIN_WORDS_FLOOR)
    result["max_words"] = int(max_words * 1.1)
    return result


if __name__ == "__main__":
    import sys
    import io
    import logging

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO)

    topic = sys.argv[1] if len(sys.argv) > 1 else "психология прокрастинации"
    platform = sys.argv[2] if len(sys.argv) > 2 else "tiktok"

    print(f"Генерирую: '{topic}' для {platform}...\n")
    script = write_script(topic, platform)
    words = len(script.get("full_script", "").split())
    print(f"Слов: {words}\n")
    print(f"HOOK: {script['hook']}\n")
    print(f"FULL:\n{script['full_script']}")
