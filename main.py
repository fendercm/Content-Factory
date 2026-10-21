"""
Master Orchestrator — запускает полный пайплайн генерации и публикации видео.

Использование:
    python main.py --niche "психология"
    python main.py --niche "психология" --platform tiktok
    python main.py --niche "психология" --dry-run       # без реальной публикации
    python main.py --niches "психология,бизнес,здоровье"  # несколько ниш параллельно (отдельные процессы)

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
import subprocess
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
from utils.log_redact import install_log_redaction, redact

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
install_log_redaction()
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
    _migrate_performance(conn)
    conn.commit()


def _migrate_performance(conn: sqlite3.Connection) -> None:
    """
    Старые БД содержат performance без метрик (post_id/platform/title/published_at).
    CREATE TABLE IF NOT EXISTS их не обновляет — без views idea_generator
    молча не видит прошлые хиты, а analytics_agent не может сделать upsert.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(performance)")}
    for col, typedef in [
        ("views", "INTEGER DEFAULT 0"),
        ("likes", "INTEGER DEFAULT 0"),
        ("comments", "INTEGER DEFAULT 0"),
        ("niche", "TEXT"),
        ("hook", "TEXT"),
        ("fetched_at", "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE performance ADD COLUMN {col} {typedef}")


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

class StepFailedError(RuntimeError):
    """Шаг исчерпал попытки — Telegram-алерт уже отправлен из _run_with_retry."""


def _alert(step: str, error: str, attempt: int | None = None) -> None:
    try:
        from agents.notify_agent import alert_error
        alert_error(step, error, attempt)
    except Exception as notify_exc:
        logger.warning("Не удалось отправить Telegram алерт: %s", notify_exc)


def _run_with_retry(
    step_name: str,
    fn,
    conn: sqlite3.Connection,
    run_id: str,
    *args,
    max_retries: int = MAX_RETRIES,
    **kwargs,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.info("[%s] Попытка %d/%d", step_name, attempt, max_retries)
            _save_step(conn, run_id, step_name, "running")
            result = fn(*args, **kwargs)
            _save_step(conn, run_id, step_name, "success", result)
            return result
        except Exception as exc:
            last_error = exc
            err_text = redact(exc)
            logger.error("[%s] Ошибка (попытка %d): %s", step_name, attempt, err_text)
            _log_error(conn, run_id, step_name, err_text, attempt)
            _save_step(conn, run_id, step_name, f"failed_attempt_{attempt}", error=err_text)

            if attempt == max_retries:
                _alert(step_name, err_text, attempt)
            else:
                time.sleep(RETRY_DELAY)

    raise StepFailedError(
        f"Шаг '{step_name}' провалился после {max_retries} попыток: {redact(last_error)}"
    ) from last_error


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


def step_research_agent(topic: str, query: str | None = None) -> dict:
    """Исследует тему. При ошибке — возвращает пустой контекст, не блокирует пайплайн."""
    try:
        from agents.research_agent import research
        return research(topic, query=query)
    except Exception as exc:
        logger.warning("Research Agent ошибка: %s — продолжаем без контекста", exc)
        return {"facts": [], "summary": "research failed", "sources_used": []}


def _recent_hooks(limit: int = 8) -> list[str]:
    """Hook'и последних сценариев канала — Script Writer не должен их повторять."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        rows = conn.execute(
            "SELECT payload FROM pipeline_state WHERE step='script_writer' AND status='success' "
            "ORDER BY id DESC LIMIT ?", (limit * 2,),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    hooks: list[str] = []
    for (payload,) in rows:
        try:
            hook = (json.loads(payload) or {}).get("hook", "").strip()
        except (TypeError, ValueError):
            continue
        if hook and hook not in hooks:
            hooks.append(hook)
    return hooks[:limit]


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
        idea_hook=idea.get("hook"),
        idea_angle=idea.get("angle"),
        avoid_hooks=_recent_hooks(),
    )
    script["target_duration_sec"] = target_sec
    return script


def step_fact_checker(script: dict, research_facts: list[dict] | None = None) -> dict:
    from agents.fact_checker import check
    return check(script_text=script.get("full_script", ""), facts=research_facts)


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


def step_subtitle_agent(voice: dict, platform: str, script_text: str | None = None) -> dict | None:
    """Генерирует субтитры (текст — из сценария). При ошибке — пайплайн продолжается без субтитров."""
    try:
        from agents.subtitle_agent import generate_subtitles
        result = generate_subtitles(audio_path=voice["audio_path"], platform=platform,
                                    script_text=script_text)
        logger.info("Субтитры: %d сегментов (по сценарию: %s)",
                    result.get("segment_count", 0), result.get("aligned"))
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
    ai_generated: bool = False,
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
        ai_generated=ai_generated,
    )
    return result


def step_notify_success(
    idea: dict, pub_result: dict, platform: str, video: dict | None = None,
    dry_run: bool = False,
) -> None:
    from agents.notify_agent import alert_success
    alert_success(
        title=idea["title"],
        platforms=[platform],
        post_id=pub_result.get("post_id", "unknown"),
        duration=video.get("duration") if video else None,
        video_path=video.get("video_path") if video else None,
        dry_run=dry_run,
        status=pub_result.get("status"),
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


def _quality_gate(
    conn: sqlite3.Connection, run_id: str, video: dict, hook_text: str, dry_run: bool,
) -> None:
    """
    AI-оценка готового видео. Видео с verdict=rejected в реальном режиме
    не публикуется. Если оценщик недоступен (verdict=skipped) — не блокируем:
    технические проверки делает qa_agent.
    """
    try:
        from agents.quality_scorer import score_video
        score_result = score_video(video_path=video["video_path"], hook_text=hook_text, threshold=6)
    except Exception as exc:
        logger.warning("Quality Scorer пропущен: %s", exc)
        return

    _save_step(conn, run_id, "quality_scorer", "success", score_result)
    score = score_result.get("score")
    verdict = score_result.get("verdict")
    log_message("quality_scorer",
                f"Score: {score if score is not None else '—'}/10 — {verdict}", "qa_agent")
    if verdict == "rejected":
        reason = score_result.get("reasoning", "")
        if dry_run:
            logger.warning("Quality Scorer: видео отбраковано (score=%s): %s — dry-run, продолжаю",
                           score, reason)
        else:
            raise ValueError(f"Quality Scorer отбраковал видео (score={score}/10): {reason[:200]}")


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
    from utils.cost_tracker import set_run_id as set_cost_run_id
    set_cost_run_id(run_id)
    logger.info("=== Запуск пайплайна run_id=%s ===", run_id)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _init_db(conn)
    log_message("orchestrator", f"Запуск пайплайна: ниша={niche}, платформа={platform}")

    def retry(name, fn, *args, **kwargs):
        return _run_with_retry(name, fn, conn, run_id, *args, **kwargs)

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
            research_data = step_research_agent(
                candidate["title"],
                candidate.get("research_query") or candidate.get("source_topic"),
            )
            log_message("research_agent",
                f"{research_data['summary']}",
                "script_writer")

            log_message("script_writer", f"Генерирую сценарий: «{candidate['title']}»")
            candidate_script = retry("script_writer", step_script_writer, candidate, platform, research_data)
            words = len(candidate_script.get("full_script", "").split())
            # Script Writer сам добивается диапазона пресета; повторяем только если
            # текст короче его же минимума (раньше здесь была константа 120,
            # не совпадавшая с пресетами, — лишние генерации жгли лимит Groq)
            min_words = int(candidate_script.get("min_words") or 0)
            for _sw_retry in range(2):
                if words >= min_words:
                    break
                logger.warning("Script too short (%d < %d words) — retrying script_writer (%d/2)",
                               words, min_words, _sw_retry + 1)
                candidate_script = retry("script_writer", step_script_writer, candidate, platform, research_data)
                words = len(candidate_script.get("full_script", "").split())
            log_message("script_writer", f"Сценарий готов: {words} слов", "fact_checker")

            # Сохраняем как fallback на случай если все идеи провалят fact-check
            if fallback_idea is None:
                fallback_idea = candidate
                fallback_script = candidate_script

            # 4.5. Проверка фактов — если провалена, пробуем следующую идею
            facts = research_data.get("facts") or []
            fact_result = retry("fact_checker", step_fact_checker, candidate_script, facts)
            if not fact_result["valid"] and not fact_result.get("unverified"):
                # Одна попытка исправить конкретные фразы, прежде чем выбрасывать идею
                # вместе с собранными фактами
                from agents.script_writer import fix_script
                fixed = fix_script(candidate_script, fact_result.get("errors", []), facts)
                if fixed is not None:
                    recheck = retry("fact_checker", step_fact_checker, fixed, facts)
                    if recheck["valid"]:
                        logger.info("Fact-check: сценарий исправлен по замечаниям")
                        log_message("fact_checker", "Сценарий исправлен по замечаниям и прошёл повторную проверку",
                                    "script_writer")
                        candidate_script, fact_result = fixed, recheck
            if not fact_result["valid"]:
                errors = fact_result.get("errors", [])
                err_msg = f"Фактические ошибки в «{candidate['title']}»: {errors}"
                logger.warning("Fact-check не прошёл: %s", err_msg)
                _log_error(conn, run_id, "fact_checker", err_msg, 1)
                log_message("fact_checker", f"Ошибки: {'; '.join(str(e) for e in errors)} — пробую другую тему", "orchestrator")
                continue  # следующая идея (итоговый алерт — если не пройдёт ни одна)

            log_message("fact_checker", f"Факты проверены ✓ — «{candidate['title']}»", "safety_agent")
            idea = candidate
            script = candidate_script
            logger.info("Идея прошла fact-check: '%s'", idea["title"])

            # 4.7. Hook A/B — генерируем 2 альтернативы и LLM-судья выбирает лучший.
            # Новый hook тоже проходит fact-check: иначе непроверенная «статистика-шок»
            # попадает в публикацию в обход проверки.
            try:
                from agents.script_writer import improve_hook
                hook_ab = improve_hook(script.get("full_script", ""), idea["title"],
                                       avoid_hooks=_recent_hooks())
                if hook_ab.get("chosen_index", 0) != 0 and hook_ab.get("script"):
                    new_script = {**script, "full_script": hook_ab["script"], "hook": hook_ab["hook"]}
                    recheck = step_fact_checker(new_script, facts)
                    if recheck.get("valid"):
                        old_hook = script.get("hook", "")[:50]
                        script = new_script
                        log_message("script_writer",
                            f"Hook A/B: заменён hook — было «{old_hook}», стало «{hook_ab['hook'][:50]}»",
                            "safety_agent")
                    else:
                        logger.warning("Hook A/B: новый hook не прошёл fact-check (%s) — оставляю исходный",
                                       recheck.get("errors"))
            except Exception as exc:
                logger.warning("Hook A/B пропущен: %s", exc)

            # 4.8. Series mode — если тема глубокая, разбиваем на N частей
            series_info = step_series_check(
                idea["title"], research_data.get("facts", []), platform, niche,
            )
            break

        if idea is None or script is None:
            if fallback_idea is None:
                raise ValueError("Не удалось сгенерировать ни одного сценария")
            if not dry_run:
                # Публикация непроверенного сценария — риск страйков. Fail-closed.
                raise ValueError(
                    f"Ни одна из {len(sorted_ideas)} идей не прошла fact-check — публикация отменена"
                )
            # Dry-run: собираем видео для просмотра, но явно помечаем
            idea = fallback_idea
            script = fallback_script
            logger.warning("DRY-RUN: все идеи провалили fact-check — собираю «%s» для просмотра", idea["title"])
            log_message("orchestrator", f"Dry-run: fact-check не пройден — собираю «{idea['title']}» только для просмотра", "safety_agent")

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
            _fut_sub = _ex.submit(step_subtitle_agent, voice, platform, _full_script_text)
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

        # 7.6. Quality Scorer — AI оценка финального видео (Gemini → Groq Vision)
        _quality_gate(conn, run_id, video, script.get("hook", ""), dry_run)

        # 8. QA — проверяем готовое видео перед публикацией
        retry("qa_agent", step_qa_agent, video, platform)
        log_message("qa_agent", "Видео прошло QA ✓", "publisher_agent")

        # 9. Публикация — одна попытка: POST /posts не идемпотентен, повтор после
        # обрыва соединения создаёт дубль поста (presign/upload ретраятся внутри агента)
        pub_result = retry(
            "publisher_agent", step_publisher_agent,
            video, idea, script, platform, dry_run, thumbnail, account_id_override,
            max_retries=1,
        )
        logger.info("Опубликовано: post_id=%s", pub_result.get("post_id"))
        log_message("publisher_agent", f"Опубликовано: post_id={pub_result.get('post_id','?')}", "notify_agent")

        # 10. Отмечаем тему как использованную — только после реальной публикации,
        # иначе dry-run «съедает» темы на 30 дней
        if not dry_run:
            step_mark_published(idea)

        # 10.1. Серийный эпизод — фиксируем после успешной публикации
        if series_info is not None and not dry_run:
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
            step_notify_success(idea, pub_result, platform, video, dry_run=dry_run)
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
        _save_step(conn, run_id, "pipeline", "failed", error=redact(exc))
        if not isinstance(exc, StepFailedError):
            _alert("pipeline", f"[{niche}] {exc}")
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
    from utils.cost_tracker import set_run_id as set_cost_run_id
    set_cost_run_id(run_id)
    logger.info("=== Story-пайплайн run_id=%s ===", run_id)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _init_db(conn)
    log_message("orchestrator", f"Story-эпизод: мир ниши «{niche}», платформа {platform}")

    def retry(name, fn, *args, **kwargs):
        return _run_with_retry(name, fn, conn, run_id, *args, **kwargs)

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
        subtitles = step_subtitle_agent(voice, platform, episode["full_script"])
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
        _quality_gate(conn, run_id, video, episode["hook"], dry_run)

        # 7. QA + публикация (одна попытка — см. run_pipeline)
        retry("qa_agent", step_qa_agent, video, platform)
        pub_result = retry(
            "publisher_agent", step_publisher_agent,
            video, idea, script, platform, dry_run, None, account_id_override,
            max_retries=1,
        )
        log_message("publisher_agent",
                    f"Опубликовано: post_id={pub_result.get('post_id', '?')}", "notify_agent")

        # 8. Эпизод — в лор-память только после реальной публикации:
        # иначе dry-run двигает сюжет, и настоящий следующий эпизод «перескакивает»
        if not dry_run:
            save_episode(world["id"], episode, run_id=run_id,
                         post_id=pub_result.get("post_id", ""), db_path=DB_PATH)

        try:
            step_notify_success(idea, pub_result, platform, video, dry_run=dry_run)
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
        _save_step(conn, run_id, "pipeline", "failed", error=redact(exc))
        if not isinstance(exc, StepFailedError):
            _alert("story_pipeline", f"[{niche}] {exc}")
        sys.exit(1)

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Series-пайплайн (--format series): сериал-антология «Кабинет 7» через KIE
# ---------------------------------------------------------------------------

def _case_as_idea(case: dict, series: dict) -> dict:
    """Кейс в формате идеи — для topic_filter (дедуп 30 дней по механизму + safety)."""
    return {"title": case["title"], "hook": case["surface_complaint"], "angle": case["real_reason"],
            "source_topic": f"{series['id']}:{case['psych_concept']}".lower(),
            "hashtags": series["hashtags"], "virality": 0.5, "_case": case}


def _series_episode_for_case(series: dict, case: dict, episode_no: int, platform: str,
                             conn, run_id: str) -> tuple[dict | None, list[dict], str]:
    """Факты → сценарий → fact-check (+1 правка) → safety. → (episode | None, facts, причина отказа)"""
    from agents.anthology_agent import spoken_text, write_episode

    research = step_research_agent(case["title"], case.get("research_query"))
    facts = research.get("facts") or []
    log_message("research_agent", research.get("summary", ""), "anthology_agent")

    episode = _run_with_retry("anthology_episode", write_episode, conn, run_id,
                              series, case, facts, episode_no, max_retries=2)
    fact = step_fact_checker({"full_script": spoken_text(episode)}, facts)
    if not fact["valid"] and not fact.get("unverified"):
        errors = "; ".join(str(e) for e in fact.get("errors", []))
        logger.warning("Series fact-check: %s — переписываю эпизод", errors)
        episode = write_episode(series, case, facts, episode_no, feedback=f"фактические ошибки: {errors}")
        fact = step_fact_checker({"full_script": spoken_text(episode)}, facts)
    if not fact["valid"]:
        return None, facts, f"fact-check: {fact.get('errors')}"

    from agents.safety_agent import check as safety_check
    safety = safety_check(spoken_text(episode), episode["title"] or case["title"], series["hashtags"], platform)
    if not safety["safe"]:
        return None, facts, f"safety [{safety.get('category')}]: {safety.get('issue')}"
    return episode, facts, ""


def _load_series_run(conn: sqlite3.Connection, run_id: str, series: dict) -> tuple[dict, dict, dict, dict | None]:
    """
    Артефакты упавшего запуска сериала из pipeline_state: сценарий с кейсом, идея (для topics_used),
    раскадровка и — если шаг кадров успел пройти и файлы на месте — оплаченные кадры с анимацией.
    → (episode, idea, plan, scenes | None)
    """
    def last(step: str, ok=lambda data: True):
        rows = conn.execute("SELECT payload FROM pipeline_state WHERE run_id=? AND step=? AND status='success' "
                            "ORDER BY id DESC", (run_id, step)).fetchall()
        for (payload,) in rows:
            data = json.loads(payload) if payload else None
            if data is not None and ok(data):
                return data
        return None

    saved = last("anthology_episode", lambda d: isinstance(d, dict) and "case" in d and "lines" in d)
    plan = last("anthology_shots", lambda d: isinstance(d, dict) and "assignment" in d)
    if not saved or not plan:
        raise ValueError(f"В запуске {run_id} нет сценария или раскадровки — возобновлять нечего")
    episode = {"title": saved["title"], "case": saved["case"], "lines": saved["lines"]}
    ideas = last("topic_filter", lambda d: isinstance(d, list)) or []
    idea = next((i for i in ideas if (i.get("_case") or {}).get("title") == episode["case"]["title"]), None) \
        or _case_as_idea(episode["case"], series)

    scenes = last("scene_agent", lambda d: isinstance(d, dict) and d.get("shots"))
    if scenes:
        missing = [p for s in scenes["shots"] for p in (s.get("image_path"), s.get("video_path"))
                   if p and not os.path.exists(p)]
        if missing:
            logger.warning("Возобновление: нет %d файлов кадров (%s…) — кадры будут сгенерированы заново",
                           len(missing), missing[0])
            scenes = None
    return episode, idea, plan, scenes


def _publish_series_extra(series: dict, video: dict, title: str, description: str, dry_run: bool) -> dict:
    """
    Дополнительные площадки сериала. "also_publish": VK — своим публикатором (нужен ключ с правом video),
    tiktok/instagram/youtube — через Zernio с аккаунтом ZERNIO_ACCOUNT_ID_<ПЛОЩАДКА>; площадка без ключа
    или аккаунта пропускается. "manual_publish": площадки, куда автор загружает сам (Wibes, VK) —
    одно сообщение в Telegram с роликом и подписью на все сразу. Основная публикация уже прошла,
    поэтому сбой здесь не роняет эпизод: алерт в Telegram. Одна попытка — повтор дал бы дубль.
    → {площадка | "manual": {"status": published|manual|failed|not_configured|skipped|unsupported, ...}}
    """
    results: dict[str, dict] = {}
    manual = series.get("manual_publish", [])
    if manual:
        if dry_run:
            results["manual"] = {"status": "skipped"}
        else:
            from agents.notify_agent import send_manual_upload
            tags = " ".join(f"#{h.lstrip('#')}" for h in series["hashtags"])
            try:
                sent = send_manual_upload(", ".join(manual), video["video_path"], f"{description}\n\n{tags}")
                if not sent.get("ok"):
                    raise RuntimeError(f"Telegram: {sent.get('error')}")
                results["manual"] = {"status": "manual", "platforms": manual}
            except Exception as exc:
                logger.error("Ручная публикация: ролик в Telegram не отправлен: %s", exc)
                results["manual"] = {"status": "failed", "error": redact(str(exc))}
                _alert("publish_manual", f"«{title}»: ролик для {', '.join(manual)} не отправлен: {exc}")

    for name in series.get("also_publish", []):
        if dry_run:
            results[name] = {"status": "skipped"}
            continue
        if name == "vk":
            from agents import vk_publisher
            if not vk_publisher.is_configured():
                logger.warning("VK: нет VK_USER_TOKEN / VK_GROUP_ID — публикация в VK пропущена")
                results[name] = {"status": "not_configured"}
                continue

            def publish():
                return vk_publisher.publish_video(video["video_path"], title, description, series["hashtags"])
        elif name in ("tiktok", "instagram", "youtube"):
            account = os.getenv(f"ZERNIO_ACCOUNT_ID_{name.upper()}")
            if not account:
                logger.warning("%s: нет ZERNIO_ACCOUNT_ID_%s — публикация пропущена", name, name.upper())
                results[name] = {"status": "not_configured"}
                continue

            def publish(name=name, account=account):
                return step_publisher_agent(video, {"title": title, "hashtags": series["hashtags"]},
                                            {"body": description}, name, False, None, account,
                                            ai_generated=True)
        else:
            logger.warning("also_publish: площадка %s не поддерживается — пропуск", name)
            results[name] = {"status": "unsupported"}
            continue
        try:
            results[name] = publish()
            log_message("publisher_agent", f"{name}: {results[name].get('post_id')}", "notify_agent")
        except Exception as exc:
            logger.error("%s: публикация не удалась: %s", name, exc)
            results[name] = {"status": "failed", "error": redact(str(exc))}
            _alert(f"publish_{name}", f"«{title}»: {exc}")
    return results


def run_series_pipeline(
    series_id: str,
    platform: str,
    dry_run: bool,
    trend_niche: str = "психология",
    account_id_override: str | None = None,
    prepare_only: bool = False,
    resume_run: str | None = None,
    script_file: str | None = None,
) -> None:
    """
    Эпизод сериала: кейс (premium LLM) → факты → сценарий с репликами по ролям → fact-check/safety →
    раскадровка → озвучка по ролям → (кадры Nano Banana 2 + анимация Kling/Wan ∥ музыка) →
    диалоговые субтитры → монтаж → оценка качества → QA → публикация.

    prepare_only: всё до QA, затем эпизод встаёт в очередь (status=ready) вместо публикации —
    его выпустит publish_next_series_episode() по расписанию.
    resume_run: доделать упавший запуск — сценарий, раскадровка и оплаченные кадры берутся из
    pipeline_state и с диска, заново делается только недостающее. Всегда в очередь (prepare_only).
    """
    from agents.anthology_agent import (load_series, next_episode_number, plan_shots, propose_cases,
                                        queue_episode, recent_episodes, save_episode, spoken_text)

    if prepare_only and dry_run:
        raise ValueError("--prepare пишет очередь в БД и несовместим с --dry-run")

    series = load_series(series_id)
    if resume_run or script_file:
        prepare_only = True  # готовый/возобновлённый эпизод не публикуется сразу — сначала просмотр в очереди
    run_id = resume_run or f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_series_{series_id}"
    set_run_id(run_id)
    from utils.cost_tracker import set_run_id as set_cost_run_id, total_for_run
    set_cost_run_id(run_id)
    logger.info("=== Series-пайплайн run_id=%s%s ===", run_id, " (возобновление)" if resume_run else "")

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _init_db(conn)
    out_dir = os.path.join("output", "series", run_id)
    os.makedirs(out_dir, exist_ok=True)

    def retry(name, fn, *args, **kwargs):
        return _run_with_retry(name, fn, conn, run_id, *args, **kwargs)

    try:
        episode_no = next_episode_number(series, DB_PATH)
        log_message("orchestrator", f"«{series['name']}», эпизод {episode_no}", "anthology_agent")
        scenes = None

        if resume_run:
            episode, idea, plan, scenes = _load_series_run(conn, resume_run, series)
            if script_file:  # новая редакция реплик (например, с пометками эмоций) поверх готовых кадров
                episode["lines"] = json.loads(open(script_file, encoding="utf-8").read())["lines"]
            episode.update({"episode_no": episode_no, "client_role": episode["case"]["client_name"].upper()})
            logger.info("Возобновляю «%s»: %s", episode["title"],
                        "кадры и анимация с диска" if scenes else "кадры заново")
        elif script_file:
            # Сценарий, написанный или одобренный автором: {"title", "case", "lines"} — без сценариста
            data = json.loads(open(script_file, encoding="utf-8").read())
            case = data["case"]
            episode = {"title": data["title"], "case": case, "lines": data["lines"], "episode_no": episode_no,
                       "client_role": case["client_name"].upper()}
            idea = _case_as_idea(case, series)
            _save_step(conn, run_id, "anthology_episode", "success",
                       {"title": episode["title"], "case": case, "lines": episode["lines"]})
            from agents.safety_agent import check as safety_check
            safety = safety_check(spoken_text(episode), episode["title"], series["hashtags"], platform)
            if not safety["safe"]:
                raise ValueError(f"safety [{safety.get('category')}]: {safety.get('issue')}")
            plan = retry("anthology_shots", plan_shots, series, episode, max_retries=2)
        else:
            # 1. Кейсы: тренды ниши — как необязательное вдохновение
            try:
                topics = [t["topic"] for t in step_trend_scout(trend_niche)][:5]
            except Exception as exc:
                logger.warning("Trend Scout для сериала: %s — без трендов", exc)
                topics = []
            cases = retry("anthology_cases", propose_cases, series, topics,
                          recent_episodes(series, DB_PATH), 3, max_retries=2)
            ideas = retry("topic_filter", step_topic_filter, [_case_as_idea(c, series) for c in cases],
                          platform, trend_niche)
            log_message("anthology_agent", f"Кейсов после фильтра: {len(ideas)}", "script_writer")

            # 2. Сценарий: первый кейс, прошедший факт-чек и safety
            episode, facts, idea = None, [], None
            for candidate in ideas:
                case = candidate["_case"]
                episode, facts, reason = _series_episode_for_case(series, case, episode_no, platform, conn, run_id)
                if episode:
                    idea = candidate
                    break
                logger.warning("Кейс «%s» отклонён: %s", case["title"], reason)
                log_message("orchestrator", f"Кейс «{case['title']}» отклонён: {reason}", "anthology_agent")
            if episode is None:
                raise ValueError("Ни один кейс не прошёл fact-check/safety — эпизод не собран")
            _save_step(conn, run_id, "anthology_episode", "success",
                       {"title": episode["title"], "case": episode["case"], "lines": episode["lines"]})
            log_message("anthology_agent", f"Эпизод {episode_no}: «{episode['title']}», "
                        f"{len(episode['lines'])} реплик, {len(spoken_text(episode).split())} слов", "scene_agent")

            # 3. Раскадровка
            plan = retry("anthology_shots", plan_shots, series, episode, max_retries=2)

        # 4. Озвучка — до платных кадров: бесплатный TTS упирается в дневную квоту, и если голоса нет,
        #    эпизод останавливается до трат на KIE. Затем кадры+анимация ∥ музыка
        from concurrent.futures import ThreadPoolExecutor
        from agents import dialogue_voice, scene_agent

        dialogue = retry("dialogue_voice", dialogue_voice.synthesize, series, episode, out_dir)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="series") as ex:
            f_scenes = None if scenes else ex.submit(retry, "scene_agent", scene_agent.render, series, plan,
                                                     out_dir, max_retries=1)
            f_music = ex.submit(step_music_agent, {"full_script": spoken_text(episode)}, trend_niche)
            scenes, music = scenes or f_scenes.result(), f_music.result()
        # Кадры прошлых запусков могли оживлять лица — такие клипы не берём: губы не в такт речи
        from agents.anthology_agent import ANIMATE_ORDER
        allowed = set(ANIMATE_ORDER[:series["animated_key"] + series["animated_simple"]])
        for shot in scenes["shots"]:
            if shot.get("video_path") and shot.get("slot") not in allowed:
                logger.info("Монтаж: клип кадра %s не используется (лицо) — статичный кадр", shot.get("slot"))
                shot.pop("video_path")
        log_message("scene_agent", f"Кадры и анимация: {[s.get('engine', 'still') for s in scenes['shots']]}, "
                    f"{scenes['credits']} кр. KIE", "series_assembly")

        # 5. Субтитры по ролям + монтаж
        from agents.anthology_agent import speaker_labels
        from agents.series_assembly import assemble
        from agents.subtitle_agent import write_dialogue_ass

        colors = series["subtitle_colors"]
        hero = series["heroine"]["role"]
        role_colors = {hero: colors.get("hero") or colors.get(hero) or "&H00FFFFFF&", episode["client_role"]: colors["client"],
                       "РАССКАЗЧИК": colors["РАССКАЗЧИК"]}
        labels, intro_labels = speaker_labels(series, episode)
        subtitle_path = os.path.abspath(os.path.join(out_dir, "subtitles.ass"))
        write_dialogue_ass(dialogue["lines"], subtitle_path, role_colors,
                           brand_text=f"{series['name']} · эпизод {episode_no}",
                           labels=labels, intro_labels=intro_labels)
        video_path = os.path.abspath(os.path.join(
            "output", "video", f"{run_id}_ep{episode_no:03d}.mp4"))
        os.makedirs(os.path.dirname(video_path), exist_ok=True)
        # Говорящие лица на ключевых репликах (липсинк по готовой озвучке); сбой — статичный кадр
        from agents import lipsync_agent
        lips = lipsync_agent.render(series, plan, scenes["shots"], dialogue, out_dir)
        assignment = lipsync_agent.apply(plan["assignment"], scenes["shots"], lips)
        if lips:
            log_message("lipsync_agent", f"Липсинк: реплики {sorted(lips)}", "series_assembly")
        video = retry("series_assembly", assemble, scenes["shots"], dialogue, subtitle_path, video_path,
                      assignment, music["music_path"] if music else None, max_retries=2)
        log_message("series_assembly", f"Эпизод собран: {video['duration']} с", "qa_agent")

        # 6. Качество и QA
        _quality_gate(conn, run_id, video, dialogue["lines"][0]["text"], dry_run)
        retry("qa_agent", step_qa_agent, video, platform)

        # 7. Публикация (или очередь для --prepare)
        title = f"{series['name']} · {episode_no}. {episode['title'] or episode['case']['title']}"[:95]
        description = "\n\n".join(p for p in (title, dialogue["lines"][-1]["text"], series["logline"],
                                                 series.get("disclaimer", "")) if p)
        pub_idea = {"title": title, "hashtags": series["hashtags"]}
        if prepare_only:
            row_id = queue_episode(series, episode, run_id, DB_PATH, video_path=video["video_path"],
                                   publish_title=title, description=description,
                                   source_topic=idea.get("source_topic", title))
            try:
                from agents.notify_agent import alert_queued
                alert_queued(title, episode_no, video["video_path"], video.get("duration"))
            except Exception as exc:
                logger.warning("Уведомление не отправлено: %s", exc)
            cost = total_for_run(run_id)
            _save_step(conn, run_id, "pipeline", "completed", {
                "idea": title, "status": "queued", "queue_id": row_id, "platform": platform,
                "niche": trend_niche, "format": "series", "series": series_id, "episode": episode_no,
                "cost_usd": round(cost, 3), "voice_engine": dialogue["engine"],
            })
            logger.info("=== Эпизод %d «%s» в очереди: %s (≈ $%.2f) ===", episode_no, title, video["video_path"], cost)
            return

        pub_result = retry("publisher_agent", step_publisher_agent, video, pub_idea, {"body": description},
                           platform, dry_run, None, account_id_override, ai_generated=True, max_retries=1)
        extra = _publish_series_extra(series, video, title, description, dry_run)

        if not dry_run:
            save_episode(series, episode, run_id, pub_result.get("post_id", ""), DB_PATH)
            step_mark_published(idea)
        published_on = ", ".join([platform] + [p for p, r in extra.items() if r.get("status") == "published"])
        try:
            step_notify_success(pub_idea, pub_result, published_on, video, dry_run=dry_run)
        except Exception as exc:
            logger.warning("Уведомление не отправлено: %s", exc)

        cost = total_for_run(run_id)
        _save_step(conn, run_id, "pipeline", "completed", {
            "idea": title, "post_id": pub_result.get("post_id"), "platform": platform,
            "niche": trend_niche, "format": "series", "series": series_id, "episode": episode_no,
            "cost_usd": round(cost, 3), "voice_engine": dialogue["engine"], "also_published": extra,
        })
        logger.info("=== Эпизод %d «%s» готов: %s (≈ $%.2f) ===", episode_no, title, video["video_path"], cost)

    except Exception as exc:
        tb = traceback.format_exc()
        logger.critical("Series-пайплайн упал: %s\n%s", exc, tb)
        _save_step(conn, run_id, "pipeline", "failed", error=redact(exc))
        if not isinstance(exc, StepFailedError):
            _alert("series_pipeline", f"[{series_id}] {exc}")
        sys.exit(1)

    finally:
        conn.close()


def publish_next_series_episode(series_id: str, platform: str, account_id_override: str | None = None) -> None:
    """
    Выпускает самый ранний эпизод из очереди (--prepare): основная площадка через Zernio, затем
    also_publish. Публикация — одна попытка (повтор дал бы дубль); статус published ставится сразу
    после основной площадки, чтобы сбой дальше не привёл к повторной публикации завтра.
    Пустая очередь — алерт и выход без ошибки.
    """
    from agents.anthology_agent import load_series, next_ready, set_episode_status

    series = load_series(series_id)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_series_{series_id}_publish"
    set_run_id(run_id)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _init_db(conn)
    try:
        row = next_ready(series, DB_PATH)
        if row is None:
            logger.warning("Очередь «%s» пуста — публиковать нечего", series["name"])
            _alert("series_queue", f"Очередь «{series['name']}» пуста — подготовьте эпизоды: --prepare N")
            return
        if not row["video_path"] or not os.path.exists(row["video_path"]):
            set_episode_status(row["id"], "rejected", DB_PATH)
            raise FileNotFoundError(f"Эпизод {row['episode_no']}: нет файла {row['video_path']} — снят с очереди")

        video = {"video_path": row["video_path"]}
        _run_with_retry("qa_agent", step_qa_agent, conn, run_id, video, platform)
        title, description = row["publish_title"], row["description"]
        pub_idea = {"title": title, "hashtags": series["hashtags"]}
        pub_result = _run_with_retry("publisher_agent", step_publisher_agent, conn, run_id, video, pub_idea,
                                     {"body": description}, platform, False, None, account_id_override,
                                     ai_generated=True, max_retries=1)
        set_episode_status(row["id"], "published", DB_PATH, pub_result.get("post_id", ""))
        step_mark_published({"title": title, "source_topic": row["source_topic"] or title})

        extra = _publish_series_extra(series, video, title, description, dry_run=False)
        published_on = ", ".join([platform] + [p for p, r in extra.items() if r.get("status") == "published"])
        try:
            step_notify_success(pub_idea, pub_result, published_on, video)
        except Exception as exc:
            logger.warning("Уведомление не отправлено: %s", exc)
        _save_step(conn, run_id, "pipeline", "completed", {
            "idea": title, "post_id": pub_result.get("post_id"), "platform": platform, "format": "series",
            "series": series_id, "episode": row["episode_no"], "also_published": extra,
        })
        logger.info("=== Эпизод %d «%s» опубликован: %s ===", row["episode_no"], title, published_on)

    except Exception as exc:
        logger.critical("Публикация эпизода из очереди упала: %s\n%s", exc, traceback.format_exc())
        _save_step(conn, run_id, "pipeline", "failed", error=redact(exc))
        if not isinstance(exc, StepFailedError):
            _alert("series_publish", f"[{series_id}] {exc}")
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
        choices=["clips", "story", "series"],
        help="Формат видео: clips — стоковый монтаж, story — серийная история с лором, "
             "series — сериал-антология через KIE (config/series/<id>.json)",
    )
    parser.add_argument("--series", default="kabinet7", help="ID сериала для --format series")
    parser.add_argument("--prepare", type=int, default=0, metavar="N",
                        help="--format series: собрать N эпизодов в очередь без публикации")
    parser.add_argument("--publish-next", action="store_true",
                        help="--format series: опубликовать следующий эпизод из очереди")
    parser.add_argument("--script", metavar="FILE",
                        help="--format series: собрать эпизод по готовому сценарию (JSON: title, case, lines) в очередь")
    parser.add_argument("--resume", metavar="RUN_ID",
                        help="--format series: доделать упавший запуск (готовые кадры берутся с диска) в очередь")
    args = parser.parse_args()

    if args.video_format == "series":
        if args.niches:
            parser.error("--format series поддерживает только одну нишу (--niche — тренды для кейсов)")
        if args.prepare and (args.dry_run or args.publish_next):
            parser.error("--prepare несовместим с --dry-run и --publish-next")
        account = os.getenv(f"ZERNIO_ACCOUNT_ID_{args.series.upper()}") or None
        if args.publish_next:
            publish_next_series_episode(args.series, args.platform, account)
            return
        if args.script and not args.resume:
            run_series_pipeline(series_id=args.series, platform=args.platform, dry_run=False,
                                trend_niche=args.niche, account_id_override=account, script_file=args.script)
            return
        if args.resume:
            run_series_pipeline(series_id=args.series, platform=args.platform, dry_run=False,
                                trend_niche=args.niche, account_id_override=account, resume_run=args.resume,
                                script_file=args.script)
            return
        if args.prepare:
            failed = 0
            for i in range(args.prepare):
                logger.info("=== Подготовка эпизода %d из %d ===", i + 1, args.prepare)
                try:
                    run_series_pipeline(series_id=args.series, platform=args.platform, dry_run=False,
                                        trend_niche=args.niche, account_id_override=account, prepare_only=True)
                except SystemExit as exc:
                    failed += 1 if exc.code else 0
            logger.info("Подготовлено %d из %d эпизодов", args.prepare - failed, args.prepare)
            sys.exit(1 if failed == args.prepare else 0)
        run_series_pipeline(series_id=args.series, platform=args.platform, dry_run=args.dry_run,
                            trend_niche=args.niche, account_id_override=account)
        return

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
        if args.custom_topic:
            parser.error("--custom-topic задаёт одну тему — используйте его с --niche")
        sys.exit(_run_niches_parallel(niches, args.platform, args.dry_run))
    else:
        env_key = f"ZERNIO_ACCOUNT_ID_{args.niche.upper().replace(' ', '_')}"
        run_pipeline(
            niche=args.niche,
            platform=args.platform,
            dry_run=args.dry_run,
            account_id_override=os.getenv(env_key) or None,
            custom_topic=args.custom_topic,
        )


def _run_niches_parallel(niches: list[str], platform: str, dry_run: bool) -> int:
    """
    Каждая ниша — отдельный процесс main.py --niche: у агентов есть
    процессно-глобальное состояние (run_id логгера, circuit breaker провайдеров),
    которое потоки перемешивали. Возвращает 1, если упала хотя бы одна ниша.
    """
    procs: dict[str, subprocess.Popen] = {}
    for niche in niches:
        cmd = [sys.executable, os.path.abspath(__file__), "--niche", niche, "--platform", platform]
        if dry_run:
            cmd.append("--dry-run")
        procs[niche] = subprocess.Popen(cmd)

    failed = []
    for niche, proc in procs.items():
        if proc.wait() != 0:
            failed.append(niche)
    if failed:
        logger.error("Multi-niche: упали ниши %s", failed)
        return 1
    logger.info("Multi-niche: все %d ниш завершены успешно", len(niches))
    return 0


if __name__ == "__main__":
    main()
