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
