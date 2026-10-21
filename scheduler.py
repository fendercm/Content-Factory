"""
Scheduler — запускает main.py по расписанию 3 раза в день.

Использование:
    py scheduler.py

Расписание (локальное время): 09:00, 14:00, 20:00
Ежедневная сводка в Telegram: 21:00
Аналитика Zernio (обратная связь для idea_generator): 08:30
Уборка диска (utils/housekeeping.py): 04:00

Настройка через .env:
    SCHEDULER_NICHE=психология      # ниша по умолчанию
    SCHEDULER_NICHES=психология,бизнес  # несколько ниш (запускаются параллельно)
    SCHEDULER_PLATFORM=tiktok       # платформа по умолчанию

Режим сериала (SCHEDULER_MODE=series): вместо роликов 3 раза в день — один эпизод из очереди
(main.py --format series --prepare N) в SERIES_PUBLISH_TIME:
    SERIES_ID=kabinet7
    SERIES_PLATFORM=youtube         # основная площадка (Zernio); остальные — also_publish в конфиге
    SERIES_PUBLISH_TIME=19:00

Установка: pip install schedule
"""

import io
import json
import logging
import os
import sqlite3
import sys

# UTF-8 stdout/stderr на Windows (избегаем 'charmap' UnicodeEncodeError)
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
import subprocess
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

try:
    import schedule
except ImportError:
    print("Установите schedule: pip install schedule")
    sys.exit(1)

from dotenv import load_dotenv

load_dotenv()

_NICHE_DEFAULT = os.getenv("SCHEDULER_NICHE", "психология")
_PLATFORM = os.getenv("SCHEDULER_PLATFORM", "tiktok")
_RUN_TIMES = ["09:00", "14:00", "20:00"]
_ROOT = Path(__file__).resolve().parent

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/scheduler.log", encoding="utf-8"),
    ],
)
from utils.log_redact import install_log_redaction  # noqa: E402

install_log_redaction()
logger = logging.getLogger("scheduler")


def _run_pipeline(niche: str, platform: str) -> None:
    logger.info(">>> Запуск: niche=%s platform=%s", niche, platform)
    try:
        proc = subprocess.run(
            [sys.executable, str(_ROOT / "main.py"), "--niche", niche, "--platform", platform],
            cwd=str(_ROOT),
            timeout=3600,
        )
        if proc.returncode == 0:
            logger.info("<<< Успех: niche=%s", niche)
        else:
            logger.error("<<< Ошибка (code=%d): niche=%s", proc.returncode, niche)
    except subprocess.TimeoutExpired:
        logger.error("<<< Таймаут (>1ч): niche=%s", niche)
    except Exception as exc:
        logger.error("<<< Исключение при запуске: %s", exc)


def job_pipeline() -> None:
    """Запускает пайплайн(ы) — по одному треду на нишу."""
    niches_env = os.getenv("SCHEDULER_NICHES", "")
    niches = [n.strip() for n in niches_env.split(",") if n.strip()] if niches_env else [_NICHE_DEFAULT]
    platform = os.getenv("SCHEDULER_PLATFORM", _PLATFORM)

    threads = [
        threading.Thread(target=_run_pipeline, args=(niche, platform), daemon=True)
        for niche in niches
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def series_publish_command(series_id: str | None = None, platform: str | None = None) -> list[str]:
    series_id = series_id or os.getenv("SERIES_ID", "kabinet7")
    platform = platform or os.getenv("SERIES_PLATFORM", "youtube")
    return [sys.executable, str(_ROOT / "main.py"), "--format", "series", "--series", series_id,
            "--niche", _NICHE_DEFAULT, "--platform", platform, "--publish-next"]


def job_series_publish(series_id: str | None = None, platform: str | None = None) -> None:
    """Публикует следующий эпизод сериала из очереди (пустая очередь — алерт из main.py)."""
    cmd = series_publish_command(series_id, platform)
    logger.info(">>> Сериал: публикация следующего эпизода (%s)", " ".join(cmd[2:]))
    try:
        proc = subprocess.run(cmd, cwd=str(_ROOT), timeout=3600)
        if proc.returncode == 0:
            logger.info("<<< Сериал: готово")
        else:
            logger.error("<<< Сериал: ошибка (code=%d)", proc.returncode)
    except subprocess.TimeoutExpired:
        logger.error("<<< Сериал: таймаут (>1ч)")
    except Exception as exc:
        logger.error("<<< Сериал: исключение %s", exc)


def job_analytics() -> None:
    """Собирает статистику постов из Zernio в performance — её читает idea_generator."""
    logger.info(">>> Аналитика: сбор статистики за 7 дней")
    try:
        proc = subprocess.run(
            [sys.executable, str(_ROOT / "agents" / "analytics_agent.py"), "--days", "7"],
            cwd=str(_ROOT),
            timeout=900,
        )
        if proc.returncode != 0:
            logger.error("<<< Аналитика: ошибка (code=%d)", proc.returncode)
    except subprocess.TimeoutExpired:
        logger.error("<<< Аналитика: таймаут")
    except Exception as exc:
        logger.error("<<< Аналитика: исключение %s", exc)


def job_housekeeping() -> None:
    try:
        from utils.housekeeping import run as housekeeping_run
        housekeeping_run()
    except Exception as exc:
        logger.warning("Housekeeping ошибка: %s", exc)


def _local_day_utc_range() -> tuple[str, str]:
    """
    Границы сегодняшних локальных суток в UTC ISO: created_at в БД хранится в UTC,
    а «сегодня» для сводки — по местному времени.
    """
    start_local = datetime.combine(datetime.now().date(), dtime.min).astimezone()
    end_local = start_local + timedelta(days=1)
    return (start_local.astimezone(timezone.utc).isoformat(),
            end_local.astimezone(timezone.utc).isoformat())


def job_daily_digest() -> None:
    """Отправляет ежедневную сводку в Telegram в 21:00."""
    try:
        from agents.notify_agent import send

        db_path = os.getenv("DB_PATH", "pipeline.db")
        if not os.path.exists(db_path):
            logger.warning("pipeline.db не найден — сводка пропущена")
            return

        conn = sqlite3.connect(db_path)
        today = datetime.now().date().isoformat()
        day_start, day_end = _local_day_utc_range()

        videos_today = conn.execute(
            "SELECT COUNT(*) FROM pipeline_state "
            "WHERE step='pipeline' AND status='completed' AND created_at >= ? AND created_at < ?",
            (day_start, day_end),
        ).fetchone()[0]

        errors_today = conn.execute(
            "SELECT COUNT(*) FROM error_log WHERE created_at >= ? AND created_at < ?",
            (day_start, day_end),
        ).fetchone()[0]

        # Последняя реальная публикация (dry-run пишет status=skipped)
        top = None
        last_pub = conn.execute(
            "SELECT payload FROM pipeline_state WHERE step='publisher_agent' AND status='success' "
            "AND payload NOT LIKE '%\"skipped\"%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last_pub and last_pub[0]:
            pub = json.loads(last_pub[0])
            top = (pub.get("post_id", "?"), pub.get("platform", "?"))

        active_series = conn.execute(
            "SELECT COUNT(*) FROM series s "
            "LEFT JOIN series_episodes e ON e.series_id = s.id "
            "GROUP BY s.id HAVING COUNT(e.id) < s.total_parts"
        ).fetchall() if _table_exists(conn, "series") else []

        conn.close()

        top_str = f"{top[0]} ({top[1]})" if top else "нет данных"

        # Cost summary
        cost_str = ""
        try:
            from utils.cost_tracker import format_summary
            cost_str = f"\n{format_summary(db_path)}"
        except Exception:
            pass

        series_str = f"\nАктивных серий: <b>{len(active_series)}</b>" if active_series else ""

        msg = (
            f"<b>📊 Ежедневная сводка — {today}</b>\n"
            f"Видео создано: <b>{videos_today}</b> | Ошибок: <b>{errors_today}</b>\n"
            f"Последняя публикация: {top_str}"
            f"{series_str}"
            f"{cost_str}"
        )
        send(msg, level="info")
        logger.info("Ежедневная сводка отправлена")
    except Exception as exc:
        logger.warning("Ошибка ежедневной сводки: %s", exc)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def series_schedules() -> dict[str, tuple[str, str]]:
    """Расписания сериалов из config/series/*.json (раздел schedule, настраивается в панели)."""
    out = {}
    for path in sorted((_ROOT / "config" / "series").glob("*.json")):
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sch = cfg.get("schedule") or {}
        if sch.get("enabled"):
            out[cfg["id"]] = (sch.get("time", "19:00"), sch.get("platform", "youtube"))
    return out


_synced: dict = {}


def sync_series_jobs() -> None:
    """Перечитывает расписания сериалов: изменения из панели подхватываются без перезапуска."""
    global _synced
    current = series_schedules()
    if current == _synced:
        return
    schedule.clear("series")
    for series_id, (at, platform) in current.items():
        schedule.every().day.at(at).do(job_series_publish, series_id, platform).tag("series")
        logger.info("Сериал %s: выпуск ежедневно в %s → %s", series_id, at, platform)
    _synced = current


def main() -> None:
    niches_env = os.getenv("SCHEDULER_NICHES", "")
    niches = [n.strip() for n in niches_env.split(",") if n.strip()] if niches_env else [_NICHE_DEFAULT]

    sync_series_jobs()
    if _synced:
        logger.info("Scheduler запущен | Сериалы по расписанию из конфигов: %s", ", ".join(_synced))
    elif os.getenv("SCHEDULER_MODE", "clips") == "series":
        publish_time = os.getenv("SERIES_PUBLISH_TIME", "19:00")
        logger.info("Scheduler запущен | Сериал %s → %s | Публикация ежедневно в %s",
                    os.getenv("SERIES_ID", "kabinet7"), os.getenv("SERIES_PLATFORM", "youtube"), publish_time)
        schedule.every().day.at(publish_time).do(job_series_publish)
    else:
        logger.info("Scheduler запущен | Ниши: %s | Платформа: %s", niches, _PLATFORM)
        logger.info("Расписание запусков: %s", ", ".join(_RUN_TIMES))
        for t in _RUN_TIMES:
            schedule.every().day.at(t).do(job_pipeline)

    schedule.every().day.at("21:00").do(job_daily_digest)
    schedule.every().day.at("08:30").do(job_analytics)
    schedule.every().day.at("04:00").do(job_housekeeping)

    logger.info("Ожидание по расписанию... (Ctrl+C для остановки)")
    heartbeat = _ROOT / "logs" / "scheduler.heartbeat"
    while True:
        sync_series_jobs()
        schedule.run_pending()
        heartbeat.touch()  # панель видит, что планировщик жив
        time.sleep(30)


if __name__ == "__main__":
    main()
