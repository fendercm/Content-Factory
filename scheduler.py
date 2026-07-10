"""
Scheduler — запускает main.py по расписанию 3 раза в день.

Использование:
    py scheduler.py

Расписание (локальное время): 09:00, 14:00, 20:00
Ежедневная сводка в Telegram: 21:00

Настройка через .env:
    SCHEDULER_NICHE=психология      # ниша по умолчанию
    SCHEDULER_NICHES=психология,бизнес  # несколько ниш (запускаются параллельно)
    SCHEDULER_PLATFORM=tiktok       # платформа по умолчанию

Установка: pip install schedule
"""

import io
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
from datetime import date, datetime

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

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/scheduler.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("scheduler")


def _run_pipeline(niche: str, platform: str) -> None:
    logger.info(">>> Запуск: niche=%s platform=%s", niche, platform)
    try:
        proc = subprocess.run(
            [sys.executable, "main.py", "--niche", niche, "--platform", platform],
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


def job_daily_digest() -> None:
    """Отправляет ежедневную сводку в Telegram в 21:00."""
    try:
        from agents.notify_agent import send

        db_path = os.getenv("DB_PATH", "pipeline.db")
        if not os.path.exists(db_path):
            logger.warning("pipeline.db не найден — сводка пропущена")
            return

        conn = sqlite3.connect(db_path)
        today = date.today().isoformat()

        videos_today = conn.execute(
            "SELECT COUNT(*) FROM pipeline_state "
            "WHERE step='pipeline' AND status='completed' AND created_at LIKE ?",
            (f"{today}%",),
        ).fetchone()[0]

        errors_today = conn.execute(
            "SELECT COUNT(*) FROM error_log WHERE created_at LIKE ?",
            (f"{today}%",),
        ).fetchone()[0]

        top = conn.execute(
            "SELECT title, platform FROM performance ORDER BY id DESC LIMIT 1"
        ).fetchone()

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


def main() -> None:
    niches_env = os.getenv("SCHEDULER_NICHES", "")
    niches = [n.strip() for n in niches_env.split(",") if n.strip()] if niches_env else [_NICHE_DEFAULT]

    logger.info("Scheduler запущен | Ниши: %s | Платформа: %s", niches, _PLATFORM)
    logger.info("Расписание запусков: %s", ", ".join(_RUN_TIMES))

    for t in _RUN_TIMES:
        schedule.every().day.at(t).do(job_pipeline)

    schedule.every().day.at("21:00").do(job_daily_digest)

    logger.info("Ожидание по расписанию... (Ctrl+C для остановки)")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
