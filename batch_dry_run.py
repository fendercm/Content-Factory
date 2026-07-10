"""
Batch Dry-Run — генерирует серию видео по разным нишам без публикации.

Запуск:
    py batch_dry_run.py                       # 10 видео по 4 нишам, default
    py batch_dry_run.py --count 5 --niches "психология,бизнес"

Запускает main.py с --dry-run в subprocess, ловит ошибки, измеряет время.
В конце пишет сводку в logs/batch_dry_run_YYYYMMDD.json.
"""

import argparse
import io
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("batch_dry_run")


DEFAULT_NICHES = ["психология", "бизнес", "наука", "здоровье"]
PLATFORMS = ["youtube"]  # youtube как самый стабильный (по 0 ошибок publication)


def _run_one(niche: str, platform: str, idx: int, total: int) -> dict:
    """Запускает один цикл пайплайна в dry-run и собирает метаданные."""
    start = time.time()
    cmd = ["py", "main.py", "--niche", niche, "--platform", platform, "--dry-run"]
    logger.info("[%d/%d] %s / %s — старт", idx, total, niche, platform)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,  # 15 минут на одно видео максимум
        )
        elapsed = time.time() - start
        result = {
            "niche": niche,
            "platform": platform,
            "ok": proc.returncode == 0,
            "elapsed_sec": round(elapsed, 1),
            "returncode": proc.returncode,
        }

        # Парсим финальную строчку лога — там путь к видео
        out = proc.stdout or ""
        for line in out.splitlines()[::-1]:
            if "Видео собрано:" in line or "video_path" in line.lower():
                parts = line.split(":")
                if len(parts) > 1:
                    result["video_path"] = parts[-1].strip()
                    break
        if proc.returncode != 0:
            # Берём последние 30 строк stderr/stdout для диагностики
            tail = (proc.stderr or out).strip().splitlines()[-30:]
            result["error_tail"] = "\n".join(tail)
        logger.info(
            "[%d/%d] %s / %s — %s за %.1fs",
            idx, total, niche, platform,
            "OK ✓" if result["ok"] else f"FAIL (rc={proc.returncode})",
            elapsed,
        )
        return result

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        logger.error("[%d/%d] %s — TIMEOUT после %.0fs", idx, total, niche, elapsed)
        return {
            "niche": niche,
            "platform": platform,
            "ok": False,
            "elapsed_sec": round(elapsed, 1),
            "error_tail": "TIMEOUT 900s",
        }
    except Exception as exc:
        elapsed = time.time() - start
        logger.error("[%d/%d] %s — EXCEPTION: %s", idx, total, niche, exc)
        return {
            "niche": niche,
            "platform": platform,
            "ok": False,
            "elapsed_sec": round(elapsed, 1),
            "error_tail": str(exc),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch dry-run для генерации серии видео")
    parser.add_argument("--count", type=int, default=10, help="Сколько видео сгенерировать (default 10)")
    parser.add_argument(
        "--niches",
        default=",".join(DEFAULT_NICHES),
        help=f"Список ниш через запятую (default: {','.join(DEFAULT_NICHES)})",
    )
    parser.add_argument("--platform", default="youtube", help="Платформа (default youtube)")
    args = parser.parse_args()

    niches = [n.strip() for n in args.niches.split(",") if n.strip()]
    if not niches:
        logger.error("Список ниш пустой")
        return

    # Распределяем count по нишам круговым образом
    jobs: list[tuple[str, str]] = []
    for i in range(args.count):
        niches_idx = i % len(niches)
        jobs.append((niches[niches_idx], args.platform))

    logger.info("Старт: %d видео по нишам %s, платформа %s", len(jobs), niches, args.platform)
    started = datetime.now(timezone.utc)

    results: list[dict] = []
    for i, (niche, platform) in enumerate(jobs, 1):
        r = _run_one(niche, platform, i, len(jobs))
        results.append(r)

    ended = datetime.now(timezone.utc)
    elapsed_total = (ended - started).total_seconds()

    ok_count = sum(1 for r in results if r["ok"])
    summary = {
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "elapsed_total_sec": round(elapsed_total, 1),
        "total": len(results),
        "ok": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }

    os.makedirs("logs", exist_ok=True)
    log_path = f"logs/batch_dry_run_{started.strftime('%Y%m%d_%H%M%S')}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info("ИТОГО: %d/%d успешных за %.0f сек", ok_count, len(results), elapsed_total)
    logger.info("Лог: %s", log_path)


if __name__ == "__main__":
    main()
