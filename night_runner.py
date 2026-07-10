"""
Night Runner — автономный ночной запуск пайплайна каждые 60 мин.

Ротация ниш: психология → бизнес → здоровье → мотивация → наука → деньги → саморазвитие → продуктивность
Платформа: tiktok (portrait 1080×1920)
Запуск: py night_runner.py
"""

import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

NICHES = [
    "психология",
    "бизнес",
    "здоровье",
    "мотивация",
    "наука",
    "деньги",
    "саморазвитие",
    "продуктивность",
]
PLATFORM = "youtube"
INTERVAL_MIN = 60          # минут между запусками
MAX_PIPELINE_MIN = 55      # max время одного run (после — таймаут)

Path("logs").mkdir(exist_ok=True)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.stream = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1, closefd=False)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        _stream_handler,
        logging.FileHandler("logs/night_run.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("night_runner")


def _notify(msg: str) -> None:
    try:
        from agents.notify_agent import send
        send(msg, level="info")
    except Exception as exc:
        logger.warning("Telegram notify ошибка: %s", exc)


def run_pipeline(niche: str, run_no: int) -> bool:
    logger.info("=== Run #%d | ниша: %s | %s ===", run_no, niche, datetime.now().strftime("%H:%M"))
    _notify(f"🚀 Night Runner #<b>{run_no}</b> запущен\nНиша: <b>{niche}</b> | {datetime.now().strftime('%H:%M')}")

    try:
        result = subprocess.run(
            [sys.executable, "main.py", "--niche", niche, "--platform", PLATFORM],
            timeout=MAX_PIPELINE_MIN * 60,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        ok = result.returncode == 0
        status = "OK" if ok else f"FAIL code={result.returncode}"
        logger.info("Run #%d zavershyon — %s", run_no, status)
        return ok
    except subprocess.TimeoutExpired:
        logger.error("Run #%d таймаут (%d мин)", run_no, MAX_PIPELINE_MIN)
        _notify(f"⏰ Night Runner #<b>{run_no}</b> — таймаут {MAX_PIPELINE_MIN} мин")
        return False
    except Exception as exc:
        logger.error("Run #%d исключение: %s", run_no, exc)
        _notify(f"💥 Night Runner #<b>{run_no}</b> — исключение: {exc}")
        return False


def main() -> None:
    logger.info("Night Runner startует. Niши: %s", " -> ".join(NICHES))
    logger.info("Интервал: %d мин | Платформа: %s", INTERVAL_MIN, PLATFORM)
    _notify(
        f"🌙 <b>Night Runner запущен</b>\n"
        f"Ниши: {', '.join(NICHES)}\n"
        f"Интервал: каждые {INTERVAL_MIN} мин\n"
        f"Платформа: {PLATFORM}"
    )

    niche_idx = 0
    run_no = 0
    ok_count = 0
    fail_count = 0

    while True:
        niche = NICHES[niche_idx % len(NICHES)]
        niche_idx += 1
        run_no += 1

        t_start = time.monotonic()
        ok = run_pipeline(niche, run_no)
        elapsed = time.monotonic() - t_start

        if ok:
            ok_count += 1
        else:
            fail_count += 1

        # Ждём до следующего полного часа от старта этого run
        sleep_secs = max(0, INTERVAL_MIN * 60 - elapsed)
        next_dt = datetime.fromtimestamp(time.time() + sleep_secs).strftime("%H:%M")
        logger.info(
            "OK=%d FAIL=%d | next run at %s (in %.0f min)",
            ok_count, fail_count, next_dt, sleep_secs / 60,
        )

        # Каждые 8 запусков — сводка
        if run_no % 8 == 0:
            _notify(
                f"📊 <b>Night Runner сводка</b> (run #{run_no})\n"
                f"✅ Успешных: {ok_count}\n"
                f"❌ Ошибок: {fail_count}"
            )

        if sleep_secs > 0:
            time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
