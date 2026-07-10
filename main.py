"""
Master Orchestrator — запускает полный пайплайн генерации и публикации видео.

Использование:
    python main.py --niche "психология"
    python main.py --niche "психология" --platform tiktok
    python main.py --niche "психология" --dry-run       # без реальной публикации
    python main.py --niches "психология,бизнес,здоровье"  # несколько ниш параллельно

Пайплайн:
    trend_scout → idea_generator → topic_filter → script_writer → fact_checker →
    safety_agent → voice_agent → music_agent → subtitle_agent → visual_agent →
    assembly_agent → thumbnail_agent → qa_agent → publisher_agent → notify_agent
"""

import argparse
import io
import json
import logging
import os
import sqlite3
import sys
import threading

# UTF-8 stdout для Windows (избегаем cp1251 UnicodeEncodeError в консоли)
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from utils.agent_logger import log_message, set_run_id

# Настройка логов
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            f"logs/pipeline_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("orchestrator")

DB_PATH = os.getenv("DB_PATH", "pipeline.db")
MAX_RETRIES = 3
RETRY_DELAY = 5  # секунды между попытками

# conn шарится между потоками ThreadPoolExecutor (voice/visual/music) —
# execute+commit должны быть атомарны, иначе «cannot commit - no transaction is active»
_db_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

def _init_db(conn: sqlite3.Connection) -> None:
    # WAL mode — безопасно при параллельных потоках (--niches), снижает SQLITE_BUSY
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS topics_used (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            published_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pipeline_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            step TEXT NOT NULL,
            status TEXT NOT NULL,
            payload TEXT,
            error TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT,
            platform TEXT,
            title TEXT,
            published_at TEXT,
            views INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            niche TEXT,
            hook TEXT,
            fetched_at TEXT
        );

        CREATE TABLE IF NOT EXISTS error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            step TEXT,
            error TEXT,
            attempt INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            sender TEXT NOT NULL,
            receiver TEXT,
            message TEXT NOT NULL,
            run_id TEXT
        );

        CREATE TABLE IF NOT EXISTS costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            agent TEXT NOT NULL,
            provider TEXT NOT NULL,
            operation TEXT NOT NULL,
            units REAL DEFAULT 0,
            unit_cost_usd REAL DEFAULT 0,
            total_usd REAL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_name TEXT NOT NULL,
            niche TEXT,
            total_parts INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS series_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL,
            part_number INTEGER NOT NULL,
            run_id TEXT,
            post_id TEXT,
            title TEXT,
            platform TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_topics_used_topic ON topics_used(topic);
        CREATE INDEX IF NOT EXISTS idx_pipeline_state_run_id ON pipeline_state(run_id);
        CREATE INDEX IF NOT EXISTS idx_pipeline_state_created ON pipeline_state(created_at);
        CREATE INDEX IF NOT EXISTS idx_costs_run_id ON costs(run_id);
        CREATE INDEX IF NOT EXISTS idx_performance_platform ON performance(platform);
        CREATE INDEX IF NOT EXISTS idx_agent_messages_run_id ON agent_messages(run_id);
    """)
    conn.commit()


def _save_step(
    conn: sqlite3.Connection,
    run_id: str,
    step: str,
    status: str,
    payload: Any = None,
    error: str | None = None,
) -> None:
    with _db_write_lock:
        conn.execute(
            """INSERT INTO pipeline_state (run_id, step, status, payload, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                step,
                status,
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                error,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def _log_error(conn: sqlite3.Connection, run_id: str, step: str, error: str, attempt: int) -> None:
    with _db_write_lock:
        conn.execute(
            "INSERT INTO error_log (run_id, step, error, attempt, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, step, error, attempt, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Retry-обёртка
# ---------------------------------------------------------------------------

def _run_with_retry(
    step_name: str,
    fn,
    conn: sqlite3.Connection,
    run_id: str,
    notify_fn,
    *args,
    **kwargs,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("[%s] Попытка %d/%d", step_name, attempt, MAX_RETRIES)
            _save_step(conn, run_id, step_name, "running")
            result = fn(*args, **kwargs)
            _save_step(conn, run_id, step_name, "success", result)
            return result
        except Exception as exc:
            last_error = exc
            err_text = str(exc)
            logger.error("[%s] Ошибка (попытка %d): %s", step_name, attempt, err_text)
            _log_error(conn, run_id, step_name, err_text, attempt)
            _save_step(conn, run_id, step_name, f"failed_attempt_{attempt}", error=err_text)

            if attempt == MAX_RETRIES:
                # Финальный алерт после 3 неудач
                try:
                    from agents.notify_agent import alert_error
                    alert_error(step_name, err_text, attempt)
                except Exception as notify_exc:
                    logger.warning("Не удалось отправить Telegram алерт: %s", notify_exc)
            else:
                time.sleep(RETRY_DELAY)

    raise RuntimeError(f"Шаг '{step_name}' провалился после {MAX_RETRIES} попыток") from last_error


# ---------------------------------------------------------------------------
# Шаги пайплайна
# ---------------------------------------------------------------------------

def step_trend_scout(niche: str) -> list[dict]:
    from agents.trend_scout import run
    topics = run(niche)
    if not topics:
        raise ValueError("Trend Scout не нашёл ни одной темы")
    return topics


def step_idea_generator(topics: list[dict], niche: str = "") -> list[dict]:
    from agents.idea_generator import run
    ideas = run(topics, niche=niche)
    if not ideas:
        raise ValueError("Idea Generator не сгенерировал идей")
    return ideas


def step_topic_filter(ideas: list[dict], platform: str, niche: str = "") -> list[dict]:
    from agents.topic_filter import run
    filtered = run(ideas, platform=platform, db_path=DB_PATH, niche=niche)
    if not filtered:
        raise ValueError("Все темы отфильтрованы (дубли или unsafe) — нечего публиковать")
    return filtered


def step_research_agent(topic: str) -> dict:
    """Исследует тему. При ошибке — возвращает пустой контекст, не блокирует пайплайн."""
    try:
        from agents.research_agent import research
        return research(topic)
    except Exception as exc:
        logger.warning("Research Agent ошибка: %s — продолжаем без контекста", exc)
        return {"facts": [], "summary": "research failed", "sources_used": []}


def step_script_writer(idea: dict, platform: str, research: dict | None = None) -> dict:
    from agents.script_writer import write_script, recommend_duration
    research_context = research.get("facts") if research else None
    # Multi-shot: выбираем длину видео под глубину темы
    target_sec = recommend_duration(
        topic=idea["title"],
        research_facts=research_context,
        platform=platform,
    )
    script = write_script(
        topic=idea["title"],
        platform=platform,
        research_context=research_context or None,
        target_duration_sec=target_sec,
    )
    script["target_duration_sec"] = target_sec
    return script


def step_fact_checker(script: dict) -> dict:
    from agents.fact_checker import check
    return check(script_text=script.get("full_script", ""))


def step_safety_agent(script: dict, idea: dict, platform: str) -> dict:
    from agents.safety_agent import check
    result = check(
        script_text=script.get("full_script", ""),
        title=idea["title"],
        hashtags=idea.get("hashtags", []),
        platform=platform,
    )
    if not result["safe"]:
        raise ValueError(
            f"Safety check провалился [{result.get('category')}]: {result.get('issue')}"
        )
    return result


def step_voice_agent(script: dict, platform: str, niche: str = "") -> dict:
    from agents.voice_agent import generate_voice, voice_for_niche
    # Persona по нише — даёт разный «характер» голоса (alena для psy, filipp для biz и т.п.)
    niche_voice = voice_for_niche(niche)
    voice = generate_voice(
        full_script=script["full_script"],
        platform=platform,
        voice=niche_voice,
    )
    if niche_voice:
        voice["niche_persona"] = niche_voice
    return voice


def step_visual_agent(idea: dict, platform: str, script_text: str | None = None, niche: str = "") -> dict:
    from agents.visual_agent import get_visuals
    visuals = get_visuals(topic=idea["title"], platform=platform, num_clips=25, script_text=script_text, niche=niche)
    return visuals


def step_music_agent(script: dict, niche: str = "") -> dict | None:
    """Подбирает фоновую музыку. При ошибке — продолжаем без музыки."""
    try:
        from agents.music_agent import get_music
        result = get_music(script_text=script.get("full_script", ""), niche=niche)
        logger.info("Музыка: source=%s mood=%s", result.get("source"), result.get("mood"))
        return result
    except Exception as exc:
        logger.warning("Music Agent ошибка: %s — продолжаем без музыки", exc)
        return None


def step_subtitle_agent(voice: dict, platform: str) -> dict | None:
    """Генерирует субтитры. При ошибке — пайплайн продолжается без субтитров."""
    try:
        from agents.subtitle_agent import generate_subtitles
        result = generate_subtitles(audio_path=voice["audio_path"], platform=platform)
        logger.info("Субтитры: %d сегментов", result.get("segment_count", 0))
        return result
    except EnvironmentError as exc:
        logger.warning("Subtitle Agent: %s — продолжаем без субтитров", exc)
        return None
    except Exception as exc:
        logger.warning("Subtitle Agent ошибка: %s — продолжаем без субтитров", exc)
        return None


def step_entity_visuals(
    script_text: str,
    audio_duration: float,
    word_timestamps: list[dict] | None = None,
) -> dict:
    """
    Сканирует сценарий на упоминания классиков (Юнг, Фрейд...) + институтов
    (Стэнфорд, MIT, WHO...) и тянет их фото из Wikipedia.
    При наличии Whisper-таймстампов делает точные оверлеи (±0.1с).
    """
    try:
        from agents.entity_visuals import run as entity_run
        return entity_run(
            script_text=script_text,
            audio_duration=audio_duration,
            word_timestamps=word_timestamps,
        )
    except Exception as exc:
        logger.warning("Entity Visuals ошибка: %s — продолжаем без фото-overlay", exc)
        return {"overlays": [], "found": 0}


def step_get_photos(topic: str, script_text: str, platform: str, n: int = 17) -> list[str]:
    """
    Скачивает ~17 фото по сценарию: 5 для burst + 12 для 50/50 weave.
    При ошибке возвращает [] — пайплайн собирается без фото.
    """
    try:
        from agents.visual_agent import get_photos
        result = get_photos(topic=topic, platform=platform, n=n, script_text=script_text)
        photos = result.get("photos") or []
        logger.info("Photos pool: %d шт (source=%s)", len(photos), result.get("source", "?"))
        return photos
    except Exception as exc:
        logger.warning("Photos pool ошибка: %s — продолжаем без фото", exc)
        return []


def step_counter_overlays(script_text: str, audio_duration: float) -> list[dict]:
    """Ищет числовую статистику в сценарии и готовит анимированные счётчики."""
    try:
        from agents.counter_overlay import find_counters
        counters = find_counters(script_text, audio_duration)
        if counters:
            logger.info("Counter overlays: %d найдено", len(counters))
        return counters
    except Exception as exc:
        logger.warning("Counter overlay ошибка: %s — продолжаем без счётчиков", exc)
        return []


def step_assembly_agent(
    voice: dict,
    visuals: dict,
    platform: str,
    subtitle_path: str | None = None,
    music_path: str | None = None,
    entity_overlays: list[dict] | None = None,
    photo_burst: dict | None = None,
    photo_pool: list[str] | None = None,
    hook_text: str | None = None,
    counter_overlays: list[dict] | None = None,
) -> dict:
    from agents.assembly_agent import assemble_video
    clips = visuals.get("clips", [])
    video = assemble_video(
        audio_path=voice["audio_path"],
        platform=platform,
        clips=clips if clips else None,
        subtitle_path=subtitle_path,
        music_path=music_path,
        entity_overlays=entity_overlays,
        photo_burst=photo_burst,
        photo_pool=photo_pool,
        hook_text=hook_text,
        counter_overlays=counter_overlays,
    )
    return video


def step_thumbnail_agent(idea: dict, platform: str, video: dict | None = None) -> dict | None:
    """Генерирует thumbnail. При ошибке — продолжаем без thumbnail."""
    try:
        from agents.thumbnail_agent import generate_thumbnail
        result = generate_thumbnail(
            title=idea["title"],
            platform=platform,
            video_path=video.get("video_path") if video else None,
        )
        logger.info("Thumbnail: %s (source=%s)", result.get("thumbnail_path"), result.get("source"))
        return result
    except Exception as exc:
        logger.warning("Thumbnail Agent ошибка: %s — продолжаем без thumbnail", exc)
        return None


def step_qa_agent(video: dict, platform: str) -> dict:
    from agents.qa_agent import check
    result = check(
        video_path=video["video_path"],
        platform=platform,
        expected_duration=video.get("duration"),
    )
    if result["status"] == "rejected":
        raise ValueError(f"QA отклонил видео: {result.get('reason')}")
    return result


def step_publisher_agent(
    video: dict,
    idea: dict,
    script: dict,
    platform: str,
    dry_run: bool,
    thumbnail: dict | None = None,
    account_id_override: str | None = None,
) -> dict:
    if dry_run:
        logger.info("[publisher] DRY RUN — публикация пропущена")
        return {"post_id": "dry-run-000", "status": "skipped", "platform": platform}

    from agents.publisher_agent import publish_video
    account_id = account_id_override or os.getenv("ZERNIO_ACCOUNT_ID", "")
    hashtags = idea.get("hashtags", [])
    description = script.get("body", idea.get("angle", ""))[:500]
    thumbnail_path = thumbnail.get("thumbnail_path") if thumbnail else None

    result = publish_video(
        video_path=video["video_path"],
        title=idea["title"],
        description=description,
        hashtags=hashtags,
        platform=platform,
        account_id=account_id,
        thumbnail_path=thumbnail_path,
    )
    return result


def step_notify_success(
    idea: dict, pub_result: dict, platform: str, video: dict | None = None
) -> None:
    from agents.notify_agent import alert_success
    alert_success(
        title=idea["title"],
        platforms=[platform],
        post_id=pub_result.get("post_id", "unknown"),
        duration=video.get("duration") if video else None,
        video_path=video.get("video_path") if video else None,
    )


def step_mark_published(idea: dict) -> None:
    from agents.topic_filter import mark_published
    mark_published(idea.get("source_topic", idea["title"]), db_path=DB_PATH)


def step_series_check(
    topic: str,
    research_facts: list[dict],
    platform: str,
    niche: str,
) -> dict | None:
    """
    Проверяет, стоит ли разбить тему на серию.
    Если тема уже в БД — возвращает следующий незаписанный эпизод.
    Если нет — создаёт новую серию из 3 частей.
    Возвращает {series_id, part_number, total_parts} или None.
    """
    try:
        from agents.series_agent import (
            should_start_series, get_next_episode, create_series,
        )
        if not should_start_series(topic, research_facts, platform):
            return None

        episode = get_next_episode(topic, db_path=DB_PATH)
        if episode:
            logger.info(
                "Серия продолжается: '%s' часть %d/%d",
                topic, episode["part_number"], episode["total_parts"],
            )
            return episode

        # Начинаем новую серию из 3 частей
        series_id = create_series(name=topic, niche=niche, total_parts=3, db_path=DB_PATH)
        logger.info("Новая серия: '%s' (id=%d, 3 части)", topic, series_id)
        return {"series_id": series_id, "part_number": 1, "total_parts": 3}
    except Exception as exc:
        logger.warning("Series check ошибка: %s — продолжаем без серии", exc)
        return None


# ---------------------------------------------------------------------------
# Главный пайплайн
# ---------------------------------------------------------------------------

def run_pipeline(
    niche: str,
    platform: str,
    dry_run: bool,
    account_id_override: str | None = None,
    custom_topic: str | None = None,
) -> None:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{niche[:20]}"
    set_run_id(run_id)
    logger.info("=== Запуск пайплайна run_id=%s ===", run_id)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _init_db(conn)
    log_message("orchestrator", f"Запуск пайплайна: ниша={niche}, платформа={platform}")

    def retry(name, fn, *args, **kwargs):
        return _run_with_retry(name, fn, conn, run_id, None, *args, **kwargs)

    try:
        if custom_topic:
            # Пропускаем trend_scout / idea_generator / topic_filter
            logger.info("Custom topic: «%s» — пропускаю тренды и фильтрацию", custom_topic)
            log_message("orchestrator", f"Custom topic: «{custom_topic}» — пропускаю тренды и фильтрацию", "research_agent")
            sorted_ideas = [{
                "title": custom_topic,
                "hook": "",
                "angle": "",
                "hashtags": [],
                "source_topic": custom_topic,
                "virality": 0,
            }]
        else:
            # 1. Тренды
            topics = retry("trend_scout", step_trend_scout, niche)
            logger.info("Тренды: %d тем", len(topics))
            top = topics[0]["topic"] if topics else "—"
            log_message("trend_scout", f"Нашёл {len(topics)} трендов, топ: «{top}»", "idea_generator")

            # 2. Идеи
            ideas = retry("idea_generator", step_idea_generator, topics, niche)
            logger.info("Идеи: %d вариантов", len(ideas))
            log_message("idea_generator", f"Сгенерировал {len(ideas)} идей", "topic_filter")

            # 3. Фильтрация
            filtered = retry("topic_filter", step_topic_filter, ideas, platform, niche)
            logger.info("После фильтрации: %d идей", len(filtered))
            log_message("topic_filter", f"После фильтрации: {len(filtered)} идей — передаю в сценарист", "script_writer")
            sorted_ideas = sorted(filtered, key=lambda x: x.get("virality", 0), reverse=True)

        # 4. Сценарий + fact-check: итерируемся по идеям пока одна не пройдёт
        idea: dict | None = None
        script: dict | None = None
        series_info: dict | None = None
        fallback_idea: dict | None = None   # лучшая идея даже если провалила fact-check
        fallback_script: dict | None = None

        for candidate in sorted_ideas:
            logger.info("Генерирую сценарий для идеи: '%s'", candidate["title"])

            # 4a. Research — собираем факты до написания сценария
            research_data = step_research_agent(candidate["title"])
            log_message("research_agent",
                f"{research_data['summary']}",
                "script_writer")

            log_message("script_writer", f"Генерирую сценарий: «{candidate['title']}»")
            candidate_script = retry("script_writer", step_script_writer, candidate, platform, research_data)
            words = len(candidate_script.get("full_script", "").split())
            # If script is too short, retry up to 2 more times
            for _sw_retry in range(2):
                if words >= 120:
                    break
                logger.warning("Script too short (%d words) — retrying script_writer (%d/2)", words, _sw_retry + 1)
                candidate_script = retry("script_writer", step_script_writer, candidate, platform, research_data)
                words = len(candidate_script.get("full_script", "").split())
            log_message("script_writer", f"Сценарий готов: {words} слов", "fact_checker")

            # Сохраняем как fallback на случай если все идеи провалят fact-check
            if fallback_idea is None:
                fallback_idea = candidate
                fallback_script = candidate_script

            # 4.5. Проверка фактов — если провалена, пробуем следующую идею
            fact_result = retry("fact_checker", step_fact_checker, candidate_script)
            if not fact_result["valid"]:
                errors = fact_result.get("errors", [])
                err_msg = f"Фактические ошибки в «{candidate['title']}»: {errors}"
                logger.warning("Fact-check не прошёл: %s", err_msg)
                _log_error(conn, run_id, "fact_checker", err_msg, 1)
                log_message("fact_checker", f"Ошибки: {'; '.join(str(e) for e in errors)} — пробую другую тему", "orchestrator")
                try:
                    from agents.notify_agent import alert_error
                    alert_error("fact_checker", err_msg, 1)
                except Exception as notify_exc:
                    logger.warning("Telegram алерт не отправлен: %s", notify_exc)
                continue  # следующая идея

            log_message("fact_checker", f"Факты проверены ✓ — «{candidate['title']}»", "safety_agent")
            idea = candidate
            script = candidate_script
            logger.info("Идея прошла fact-check: '%s'", idea["title"])

            # 4.7. Hook A/B — генерируем 2 альтернативы и LLM-судья выбирает лучший
            try:
                from agents.script_writer import improve_hook
                hook_ab = improve_hook(script.get("full_script", ""), idea["title"])
                if hook_ab.get("chosen_index", 0) != 0 and hook_ab.get("script"):
                    old_hook = script.get("hook", "")[:50]
                    script["full_script"] = hook_ab["script"]
                    script["hook"] = hook_ab["hook"]
                    log_message("script_writer",
                        f"Hook A/B: заменён hook — было «{old_hook}», стало «{hook_ab['hook'][:50]}»",
                        "safety_agent")
            except Exception as exc:
                logger.warning("Hook A/B пропущен: %s", exc)

            # 4.8. Series mode — если тема глубокая, разбиваем на N частей
            series_info = step_series_check(
                idea["title"], research_data.get("facts", []), platform, niche,
            )
            break

        if idea is None or script is None:
            # Fallback: все идеи провалили fact-check, берём первую и продолжаем
            if fallback_idea is not None:
                idea = fallback_idea
                script = fallback_script
                logger.warning("Все идеи провалили fact-check — продолжаю с «%s» (нет других вариантов)", idea["title"])
                log_message("orchestrator", f"Fact-check: нет идеального варианта — продолжаю с «{idea['title']}»", "safety_agent")
            else:
                raise ValueError("Не удалось сгенерировать ни одного сценария")

        # 4.9. Применяем серийный брендинг к заголовку и сценарию
        if series_info is not None:
            from agents.series_agent import make_series_title, series_branding_text
            part_num = series_info["part_number"]
            total = series_info["total_parts"]
            idea = dict(idea)
            idea["title"] = make_series_title(idea["title"], part_num, total)
            branding = series_branding_text(part_num, total)
            script = dict(script)
            full = script.get("full_script", "").rstrip()
            script["full_script"] = f"{full}\n{branding}"
            log_message(
                "series_agent",
                f"Серия: часть {part_num}/{total} — «{idea['title']}»",
                "voice_agent",
            )

        # 5. Safety-check сценария перед производством
        retry("safety_agent", step_safety_agent, script, idea, platform)
        log_message("safety_agent", "Контент безопасен ✓", "voice_agent")

        # 6. Голос + визуалы + музыка — независимы, запускаем параллельно
        from concurrent.futures import ThreadPoolExecutor as _TPool
        import traceback as _tb

        _script_full = script.get("full_script", "")

        def _run_voice():
            return retry("voice_agent", step_voice_agent, script, platform, niche)

        def _run_visuals():
            return retry("visual_agent", step_visual_agent, idea, platform, _script_full, niche)

        def _run_music():
            return step_music_agent(script, niche)

        with _TPool(max_workers=3, thread_name_prefix="prod") as _pex:
            _fv = _pex.submit(_run_voice)
            _fvis = _pex.submit(_run_visuals)
            _fm = _pex.submit(_run_music)
            voice = _fv.result()
            visuals = _fvis.result()
            music = _fm.result()

        log_message("voice_agent", "Голос синтезирован", "assembly_agent")
        log_message("visual_agent", f"Скачал {len(visuals.get('clips', []))} клипов с Pexels", "assembly_agent")
        music_path = music["music_path"] if music else None
        if music:
            log_message("music_agent", f"Трек: {music.get('mood','?')} ({music.get('source','?')})", "assembly_agent")

        # 6.6. Субтитры + скачивание фото — параллельно (оба независимы после voice)
        from agents.assembly_agent import _get_duration
        from concurrent.futures import ThreadPoolExecutor
        audio_dur = _get_duration(voice["audio_path"])

        _full_script_text = script.get("full_script", "")
        _idea_title = idea["title"]

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="media") as _ex:
            _fut_sub = _ex.submit(step_subtitle_agent, voice, platform)
            _fut_photos = _ex.submit(step_get_photos, _idea_title, _full_script_text, platform, 17)
            subtitles = _fut_sub.result()
            all_photos = _fut_photos.result()

        subtitle_path = subtitles["subtitle_path"] if subtitles else None
        if all_photos:
            log_message("visual_agent",
                f"Скачал {len(all_photos)} фото для weave + burst",
                "assembly_agent")

        # 6.7. Entity overlays (фото классиков + институтов) — после субтитров (нужны word_ts)
        word_ts = subtitles.get("words") if subtitles else None
        entity = step_entity_visuals(_full_script_text, audio_dur, word_ts)
        entity_overlays = entity.get("overlays") or []
        if entity_overlays:
            log_message("entity_visuals",
                f"Найдено сущностей: {entity['found']} — добавляю фото-overlay'и",
                "assembly_agent")

        # Первые 5 фото уходят в burst, остальные — в pool для 50/50 weave
        burst = None
        photo_pool: list[str] = []
        if len(all_photos) >= 3:
            burst = {
                "photo_paths": all_photos[:5],
                "start_sec": audio_dur * 0.5,
            }
            photo_pool = all_photos[5:]

        # 6.8. Animated counter overlays — числа из статистики
        counters = step_counter_overlays(_full_script_text, audio_dur)
        if counters:
            log_message("counter_overlay",
                f"Счётчики: {[str(c['value'])+c.get('suffix','') for c in counters]}",
                "assembly_agent")

        # 7. Монтаж — передаём hook для big-text overlay в первые 1.5 сек
        hook_text = script.get("hook") or script.get("full_script", "").split(".")[0]
        video = retry(
            "assembly_agent", step_assembly_agent,
            voice, visuals, platform, subtitle_path, music_path,
            entity_overlays, burst, photo_pool, hook_text, counters,
        )
        logger.info("Видео собрано: %s", video.get("video_path"))
        log_message("assembly_agent", f"Видео собрано: {video.get('duration','?')} сек, {video.get('resolution','?')}", "qa_agent")

        # 7.5. Thumbnail — опционально, генерируем после видео
        thumbnail = step_thumbnail_agent(idea, platform, video)

        # 7.6. Quality Scorer — AI оценка финального видео (Gemini Vision)
        try:
            from agents.quality_scorer import score_video
            score_result = score_video(
                video_path=video["video_path"],
                hook_text=script.get("hook", ""),
                threshold=6,
            )
            log_message("quality_scorer",
                f"Score: {score_result['score']}/10 — {score_result['verdict']}",
                "qa_agent")
            _save_step(conn, run_id, "quality_scorer", "success", score_result)
            if score_result["verdict"] == "rejected" and not dry_run:
                # В production: rejected = warn в Telegram, но не блокируем (на первом этапе)
                logger.warning(
                    "Quality Scorer: видео отбраковано (score=%d): %s — публикуем с предупреждением",
                    score_result["score"], score_result.get("reasoning", ""),
                )
        except Exception as exc:
            logger.warning("Quality Scorer пропущен: %s", exc)

        # 8. QA — проверяем готовое видео перед публикацией
        retry("qa_agent", step_qa_agent, video, platform)
        log_message("qa_agent", "Видео прошло QA ✓", "publisher_agent")

        # 9. Публикация
        pub_result = retry(
            "publisher_agent", step_publisher_agent,
            video, idea, script, platform, dry_run, thumbnail, account_id_override,
        )
        logger.info("Опубликовано: post_id=%s", pub_result.get("post_id"))
        log_message("publisher_agent", f"Опубликовано: post_id={pub_result.get('post_id','?')}", "notify_agent")

        # 10. Отмечаем тему как использованную
        step_mark_published(idea)

        # 10.1. Серийный эпизод — фиксируем после успешной публикации
        if series_info is not None:
            try:
                from agents.series_agent import mark_episode_published
                mark_episode_published(
                    series_id=series_info["series_id"],
                    part_number=series_info["part_number"],
                    run_id=run_id,
                    post_id=pub_result.get("post_id", ""),
                    title=idea["title"],
                    platform=platform,
                    db_path=DB_PATH,
                )
                logger.info(
                    "Серия: эпизод %d/%d опубликован и отмечен",
                    series_info["part_number"], series_info["total_parts"],
                )
            except Exception as exc:
                logger.warning("mark_episode_published ошибка: %s", exc)

        # 10.5. Pinned-комментарий для engagement boost
        try:
            from agents.comment_writer import write_pinned_comment
            pinned = write_pinned_comment(script.get("full_script", ""), idea["title"])
            _save_step(conn, run_id, "comment_writer", "success", {"comment": pinned})
            log_message("comment_writer", f"Pinned: «{pinned}»", "notify_agent")
        except Exception as exc:
            logger.warning("Comment Writer пропущен: %s", exc)
            pinned = None

        # 11. Уведомление об успехе (с превью кадром из видео)
        try:
            step_notify_success(idea, pub_result, platform, video)
            log_message("notify_agent", "Telegram-карточка отправлена ✓")
        except Exception as exc:
            logger.warning("Уведомление не отправлено: %s", exc)

        _save_step(conn, run_id, "pipeline", "completed", {
            "idea": idea["title"],
            "post_id": pub_result.get("post_id"),
            "platform": platform,
            "niche": niche,
        })
        logger.info("=== Пайплайн завершён успешно ===")

    except Exception as exc:
        tb = traceback.format_exc()
        logger.critical("Пайплайн упал: %s\n%s", exc, tb)
        _save_step(conn, run_id, "pipeline", "failed", error=str(exc))
        sys.exit(1)

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Story-пайплайн (--format story): серийные истории с лором
# ---------------------------------------------------------------------------

def run_story_pipeline(
    niche: str,
    platform: str,
    dry_run: bool,
    account_id_override: str | None = None,
) -> None:
    """
    Эпизод сериала: библия мира (SQLite) → сценарий эпизода → сцены через
    Pollinations FLUX → голос → субтитры → ordered-сборка → QA → публикация.
    Полностью бесплатный стек.
    """
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_story_{niche[:14]}"
    set_run_id(run_id)
    logger.info("=== Story-пайплайн run_id=%s ===", run_id)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _init_db(conn)
    log_message("orchestrator", f"Story-эпизод: мир ниши «{niche}», платформа {platform}")

    def retry(name, fn, *args, **kwargs):
        return _run_with_retry(name, fn, conn, run_id, None, *args, **kwargs)

    try:
        from agents.story_agent import get_or_create_world, write_episode, save_episode

        # 1. Мир + эпизод
        world = retry("story_world", get_or_create_world, niche, DB_PATH)
        log_message("story_agent",
                    f"Мир «{world['name']}», эпизод {world['next_episode_num']}",
                    "script_writer")
        episode = retry("story_episode", write_episode, world)
        log_message("story_agent",
                    f"Эпизод: «{episode['title']}» — {len(episode['scenes'])} сцен",
                    "safety_agent")

        script = {"full_script": episode["full_script"], "hook": episode["hook"],
                  "body": episode["summary"], "cta": episode["cliffhanger"]}
        idea = {"title": episode["title"],
                "hashtags": ["#история", "#сериал", f"#{niche.replace(' ', '')}", "#shorts"]}

        # 2. Safety
        retry("safety_agent", step_safety_agent, script, idea, platform)
        log_message("safety_agent", "Контент безопасен ✓", "voice_agent")

        # 3. Голос (размеренный рассказчик) + сцены + музыка — параллельно
        from concurrent.futures import ThreadPoolExecutor
        from agents.image_agent import generate_scenes

        scene_prompts = [s["image_prompt"] for s in episode["scenes"]]

        def _run_voice():
            from agents.voice_agent import generate_voice
            return retry("voice_agent", generate_voice,
                         episode["full_script"], platform, "ermil")

        def _run_scenes():
            return generate_scenes(
                scene_prompts,
                style_suffix=episode.get("style_suffix", ""),
                seed=episode.get("character_seed", 42),
            )

        def _run_music():
            return step_music_agent(script, niche)

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="story") as pex:
            f_voice = pex.submit(_run_voice)
            f_scenes = pex.submit(_run_scenes)
            f_music = pex.submit(_run_music)
            voice = f_voice.result()
            scene_images = [p for p in f_scenes.result() if p]
            music = f_music.result()

        if len(scene_images) < 4:
            raise ValueError(
                f"Story: только {len(scene_images)} сцен из {len(scene_prompts)} — мало для эпизода"
            )
        log_message("image_agent",
                    f"Сцены готовы: {len(scene_images)}/{len(scene_prompts)}",
                    "assembly_agent")
        music_path = music["music_path"] if music else None

        # 4. Субтитры
        subtitles = step_subtitle_agent(voice, platform)
        subtitle_path = subtitles["subtitle_path"] if subtitles else None

        # 5. Сборка: сцены в нарративном порядке, каждая — photo-клип с Ken Burns
        from agents.assembly_agent import assemble_video

        def _assemble():
            return assemble_video(
                audio_path=voice["audio_path"],
                platform=platform,
                clips=scene_images,
                subtitle_path=subtitle_path,
                music_path=music_path,
                hook_text=episode["hook"],
                ordered=True,
            )

        video = retry("assembly_agent", _assemble)
        log_message("assembly_agent",
                    f"Эпизод собран: {video.get('duration', '?')} сек", "qa_agent")

        # 6. Quality score (Gemini → Groq vision)
        try:
            from agents.quality_scorer import score_video
            score_result = score_video(video["video_path"], hook_text=episode["hook"])
            _save_step(conn, run_id, "quality_scorer", "success", score_result)
            log_message("quality_scorer",
                        f"Score: {score_result['score']}/10 — {score_result['verdict']}",
                        "qa_agent")
        except Exception as exc:
            logger.warning("Quality Scorer пропущен: %s", exc)

        # 7. QA + публикация
        retry("qa_agent", step_qa_agent, video, platform)
        pub_result = retry(
            "publisher_agent", step_publisher_agent,
            video, idea, script, platform, dry_run, None, account_id_override,
        )
        log_message("publisher_agent",
                    f"Опубликовано: post_id={pub_result.get('post_id', '?')}", "notify_agent")

        # 8. Эпизод — в лор-память (только после успешной публикации/сборки)
        save_episode(world["id"], episode, run_id=run_id,
                     post_id=pub_result.get("post_id", ""), db_path=DB_PATH)

        try:
            step_notify_success(idea, pub_result, platform, video)
        except Exception as exc:
            logger.warning("Уведомление не отправлено: %s", exc)

        _save_step(conn, run_id, "pipeline", "completed", {
            "idea": episode["title"],
            "post_id": pub_result.get("post_id"),
            "platform": platform,
            "niche": niche,
            "format": "story",
            "episode": episode["episode_num"],
        })
        logger.info("=== Story-эпизод завершён успешно ===")

    except Exception as exc:
        tb = traceback.format_exc()
        logger.critical("Story-пайплайн упал: %s\n%s", exc, tb)
        _save_step(conn, run_id, "pipeline", "failed", error=str(exc))
        sys.exit(1)

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Content Factory — Master Orchestrator")
    niche_group = parser.add_mutually_exclusive_group(required=True)
    niche_group.add_argument("--niche", help="Одна тематика, например: психология")
    niche_group.add_argument(
        "--niches",
        help="Несколько ниш через запятую: 'психология,бизнес,здоровье' — параллельный запуск",
    )
    parser.add_argument(
        "--platform",
        default="tiktok",
        choices=["tiktok", "instagram", "youtube", "linkedin", "twitter"],
        help="Целевая платформа (по умолчанию: tiktok)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Запустить пайплайн без реальной публикации",
    )
    parser.add_argument(
        "--custom-topic",
        default=None,
        help="Задать тему напрямую, пропуская тренды и фильтрацию",
    )
    parser.add_argument(
        "--format",
        dest="video_format",
        default="clips",
        choices=["clips", "story"],
        help="Формат видео: clips — стоковый монтаж, story — серийная история с лором",
    )
    args = parser.parse_args()

    if args.video_format == "story":
        if args.niches:
            parser.error("--format story поддерживает только одну нишу (--niche)")
        run_story_pipeline(
            niche=args.niche,
            platform=args.platform,
            dry_run=args.dry_run,
        )
        return

    if args.niches:
        niches = [n.strip() for n in args.niches.split(",") if n.strip()]
        logger.info("Multi-niche режим: %s", niches)

        def _run_niche(niche: str) -> None:
            # Нишево-специфичный account_id из .env
            env_key = f"ZERNIO_ACCOUNT_ID_{niche.upper().replace(' ', '_')}"
            account_id = os.getenv(env_key) or os.getenv("ZERNIO_ACCOUNT_ID", "")
            run_pipeline(
                niche=niche,
                platform=args.platform,
                dry_run=args.dry_run,
                account_id_override=account_id,
                custom_topic=getattr(args, "custom_topic", None),
            )

        threads = [threading.Thread(target=_run_niche, args=(n,), daemon=False) for n in niches]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        run_pipeline(
            niche=args.niche,
            platform=args.platform,
            dry_run=args.dry_run,
            custom_topic=getattr(args, "custom_topic", None),
        )


if __name__ == "__main__":
    main()
