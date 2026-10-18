"""
Entity Visuals Agent — находит упоминания классиков/учёных в сценарии
и тянет их фото из Wikipedia. Используется для overlay-вставки на момент
произношения имени.

Вход:  script_text (str), audio_duration (float) — длина озвучки в секундах
Выход: {"overlays": [{"name", "photo_path", "start_sec", "duration"}], "found": int}

Логика тайминга:
  Без Whisper word-timestamps делаем линейную оценку — позиция имени в тексте
  в символах × audio_duration / общая длина текста. Грубо, но для коротких
  45-сек видео ошибка не превышает 0.5 сек, что незаметно.
"""

import logging
import os
import re
import urllib.parse
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "entity_photos")
TIMEOUT = 10
# Wikipedia требует понятный User-Agent с контактом, иначе upload.wikimedia.org
# тихо возвращает 403/блокирует. См. https://meta.wikimedia.org/wiki/User-Agent_policy
HEADERS = {
    "User-Agent": (
        "ContentFactoryBot/1.0 (educational use; "
        "contact: maxlutcenko@gmail.com) python-requests/2.31"
    )
}

# Список классиков: ключ — словоформа в тексте,
# значение — (ru_title, en_title, lower-third caption для overlay).
_CLASSICS: dict[str, tuple[str, str, str]] = {
    r"\bЮнг[а-я]*\b":       ("Юнг, Карл Густав",        "Carl Jung",         "Карл Юнг · психоаналитик · 1875–1961"),
    r"\bФрейд[а-я]*\b":     ("Фрейд, Зигмунд",          "Sigmund Freud",     "Зигмунд Фрейд · психоаналитик · 1856–1939"),
    r"\bДарвин[а-я]*\b":    ("Дарвин, Чарлз",           "Charles Darwin",    "Чарльз Дарвин · биолог · 1809–1882"),
    r"\bЭйнштейн[а-я]*\b":  ("Эйнштейн, Альберт",       "Albert Einstein",   "Альберт Эйнштейн · физик · 1879–1955"),
    r"\bКант[а-я]*\b":      ("Кант, Иммануил",          "Immanuel Kant",     "Иммануил Кант · философ · 1724–1804"),
    r"\bНицше\b":           ("Ницше, Фридрих",          "Friedrich Nietzsche","Фридрих Ницше · философ · 1844–1900"),
    r"\bСократ[а-я]*\b":    ("Сократ",                   "Socrates",          "Сократ · философ · 470–399 до н.э."),
    r"\bПлатон[а-я]*\b":    ("Платон",                   "Plato",             "Платон · философ · 427–347 до н.э."),
    r"\bАристотел[а-я]*\b": ("Аристотель",               "Aristotle",         "Аристотель · философ · 384–322 до н.э."),
    r"\bТесл[а-я]*\b":      ("Тесла, Никола",            "Nikola Tesla",      "Никола Тесла · изобретатель · 1856–1943"),
    r"\bПавлов[а-я]*\b":    ("Павлов, Иван Петрович",    "Ivan Pavlov",       "Иван Павлов · физиолог · 1849–1936"),
    r"\bСкиннер[а-я]*\b":   ("Скиннер, Беррес Фредерик", "B. F. Skinner",     "Б.Ф. Скиннер · психолог · 1904–1990"),
    r"\bФерми\b":           ("Ферми, Энрико",            "Enrico Fermi",      "Энрико Ферми · физик · 1901–1954"),
    r"\bКюри\b":            ("Кюри, Мария",              "Marie Curie",       "Мария Кюри · физик/химик · 1867–1934"),
}

# Институты — обычно появляются в hedge-конструкциях типа
# "исследование Стэнфорда 2023", "учёные из Гарварда выяснили".
_INSTITUTIONS: dict[str, tuple[str, str, str]] = {
    r"\bСтэнфорд[а-я]*\b":  ("Стэнфордский университет",                  "Stanford University",  "Stanford University · 1891"),
    r"\bГарвард[а-я]*\b":   ("Гарвардский университет",                   "Harvard University",   "Harvard University · 1636"),
    r"\bЙель[а-я]*\b":      ("Йельский университет",                      "Yale University",      "Yale University · 1701"),
    r"\bЙел[а-я]*\b":       ("Йельский университет",                      "Yale University",      "Yale University · 1701"),
    r"\bМТИ\b":             ("Массачусетский технологический институт",    "Massachusetts Institute of Technology", "MIT · 1861"),
    r"\bMIT\b":             ("Массачусетский технологический институт",    "Massachusetts Institute of Technology", "MIT · 1861"),
    r"\bВОЗ\b":             ("Всемирная организация здравоохранения",      "World Health Organization", "WHO · ООН · 1948"),
    r"\bWHO\b":             ("Всемирная организация здравоохранения",      "World Health Organization", "WHO · ООН · 1948"),
    r"\bНАСА\b":            ("НАСА",                                       "NASA",                  "NASA · США · 1958"),
    r"\bNASA\b":            ("НАСА",                                       "NASA",                  "NASA · США · 1958"),
    r"\bОксфорд[а-я]*\b":   ("Оксфордский университет",                   "University of Oxford",  "Oxford · 1096"),
    r"\bКембридж[а-я]*\b":  ("Кембриджский университет",                  "University of Cambridge", "Cambridge · 1209"),
    r"\bПринстон[а-я]*\b":  ("Принстонский университет",                  "Princeton University",  "Princeton · 1746"),
    r"\bКолумбийск[а-я]*\b": ("Колумбийский университет",                 "Columbia University",   "Columbia · 1754"),
}

# Объединённый словарь — _find_mentions ищет по обоим
_ENTITIES: dict[str, tuple[str, str, str]] = {**_CLASSICS, **_INSTITUTIONS}


def _wikipedia_photo_url(title_ru: str, title_en: str) -> str | None:
    """
    Возвращает URL главной картинки статьи (infobox). Сначала ru, потом en.
    Предпочитаем thumbnail (1024px) — Wikipedia сама перекодирует .tif/.svg в JPEG,
    плюс размер в 5-10 раз меньше чем original.
    """
    for lang, title in (("ru", title_ru), ("en", title_en)):
        try:
            resp = requests.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "titles": title,
                    "prop": "pageimages",
                    "format": "json",
                    "piprop": "thumbnail|original",
                    "pithumbsize": 1024,
                },
                timeout=TIMEOUT,
                headers=HEADERS,
            )
            if resp.status_code != 200:
                continue
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                # Берём thumbnail в первую очередь — он всегда JPEG, маленький, готовый
                thumb = page.get("thumbnail")
                if thumb and thumb.get("source"):
                    return thumb["source"]
                orig = page.get("original")
                # Берём original только если он JPEG/PNG (.tif/.svg ffmpeg не прочтёт)
                if orig and orig.get("source"):
                    src = orig["source"].lower()
                    if src.endswith((".jpg", ".jpeg", ".png", ".webp")):
                        return orig["source"]
        except Exception as exc:
            logger.debug("Wikipedia photo %s/%s ошибка: %s", lang, title, exc)
    return None


def _download_photo(url: str, dest: str) -> bool:
    """Скачиваем с 2 попытками и timeout=60 — Wikipedia иногда тормозит на больших JPEG."""
    from utils.download import download_to
    for attempt in range(2):
        try:
            download_to(url, dest, timeout=60, headers=HEADERS, min_bytes=5_001)
            return True
        except Exception as exc:
            logger.warning("Download photo попытка %d ошибка: %s", attempt + 1, exc)
    return False


def _find_mentions(script_text: str) -> list[tuple[str, int]]:
    """Ищет упоминания классиков + институтов. Возвращает list[(pattern_key, char_position)].

    Дедуплицирует по wiki-title (несколько паттернов → одна сущность),
    чтобы Йельский университет не вставлялся дважды если в тексте есть и
    "Йель" и "Йельский".
    """
    mentions: list[tuple[str, int]] = []
    seen_titles: set[str] = set()
    for pattern, (title_ru, _title_en, _caption) in _ENTITIES.items():
        if title_ru in seen_titles:
            continue
        m = re.search(pattern, script_text, flags=re.IGNORECASE)
        if m:
            mentions.append((pattern, m.start()))
            seen_titles.add(title_ru)
    return sorted(mentions, key=lambda x: x[1])


def _find_word_time(
    words: list[dict], pattern: str, fallback_char_pos: int, text_len: int, audio_dur: float
) -> float:
    """
    Ищет время произношения слова, соответствующего entity-паттерну, в Whisper-тaймстампах.
    Если не нашли — возвращаем линейную оценку по позиции в тексте.
    """
    if words:
        for w in words:
            if re.search(pattern, w["text"], flags=re.IGNORECASE):
                return max(0.0, float(w["start"]) - 0.2)
    # Fallback: линейная оценка
    return max(0.0, (fallback_char_pos / max(text_len, 1)) * audio_dur - 0.3)


def run(
    script_text: str,
    audio_duration: float,
    word_timestamps: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Сканирует сценарий, для каждого найденного классика тянет фото из Wikipedia,
    вычисляет тайм-код для overlay.

    Args:
        script_text:      Полный текст сценария.
        audio_duration:   Длительность озвучки в секундах.
        word_timestamps:  Опционально — список {text, start, end} от Whisper.
                          Если задан — используется для точных таймингов overlay'ев
                          (±0.1 сек вместо ±0.5 сек у линейной оценки).

    Returns:
        {
          "overlays": [
            {"name": "Юнг", "photo_path": "...", "start_sec": 22.4, "duration": 1.5},
            ...
          ],
          "found": int,
        }
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    mentions = _find_mentions(script_text)
    if not mentions:
        logger.info("Entity Visuals: классиков не найдено")
        return {"overlays": [], "found": 0}

    text_len = max(len(script_text), 1)
    overlays: list[dict] = []

    for pattern, char_pos in mentions:
        title_ru, title_en, caption = _ENTITIES[pattern]
        safe_name = re.sub(r"[^A-Za-z0-9]+", "_", title_en)
        dest = os.path.abspath(os.path.join(OUTPUT_DIR, f"wiki_{safe_name}.jpg"))

        # Кэш — если уже скачивали, переиспользуем
        if not os.path.exists(dest) or os.path.getsize(dest) < 5_000:
            url = _wikipedia_photo_url(title_ru, title_en)
            if not url:
                logger.warning("Wikipedia: фото не найдено для %s", title_ru)
                continue
            ok = _download_photo(url, dest)
            if not ok:
                logger.warning("Wikipedia: не удалось скачать фото %s", title_ru)
                continue
            logger.info("Wikipedia: скачано фото %s → %s", title_ru, os.path.basename(dest))
        else:
            logger.info("Wikipedia: фото %s уже в кеше", title_ru)

        start_sec = _find_word_time(
            word_timestamps or [], pattern, char_pos, text_len, audio_duration
        )

        overlays.append({
            "name": title_ru.split(",")[0],  # короткое имя для логов
            "photo_path": dest,
            "start_sec": round(start_sec, 2),
            "duration": 1.5,
            "caption": caption,  # lower-third badge для рендера
        })

    timing_mode = "Whisper word-level" if word_timestamps else "линейная"
    logger.info("Entity Visuals: %d overlay-вставок (тайминги: %s)", len(overlays), timing_mode)
    return {"overlays": overlays, "found": len(overlays)}


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    text = sys.argv[1] if len(sys.argv) > 1 else (
        "Я начал применять метод Юнга. Как говорил Фрейд, иногда сигара — просто сигара."
    )
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    result = run(text, dur)
    print(json.dumps(result, ensure_ascii=False, indent=2))
