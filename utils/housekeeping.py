"""
Housekeeping — уборка диска. Запускается из scheduler.py раз в сутки
или вручную: py utils/housekeeping.py [--dry-run]

По умолчанию трогает только то, что можно восстановить:
  - кэш стоковых клипов Pexels (output/clips/pexels_*.mp4) сверх CLIPS_CACHE_MAX_GB
    (по умолчанию 5 ГБ) — удаляются давно не использованные;
  - промежуточные рендеры output/clips/photo_cache старше 7 дней;
  - недокачанные *.part.

Клипы Kling (kling_*.mp4) не трогаются никогда — они платные.
Логи и готовые видео удаляются только если явно задано в .env:
  LOG_RETENTION_DAYS=30      — logs/*.log старше N дней
  OUTPUT_RETENTION_DAYS=60   — output/{video,audio,subtitles,thumbnails,frames*} старше N дней
"""

import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
CLIPS = OUTPUT / "clips"
PHOTO_CACHE = CLIPS / "photo_cache"
LOGS = ROOT / "logs"

_OUTPUT_SUBDIRS = ("video", "audio", "subtitles", "thumbnails", "frames", "story_images")
_PHOTO_CACHE_DAYS = 7


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _remove(path: Path, dry_run: bool) -> int:
    try:
        size = path.stat().st_size
        if not dry_run:
            path.unlink()
        return size
    except OSError as exc:
        logger.warning("Housekeeping: не удалось удалить %s: %s", path, exc)
        return 0


def _older_than(paths, days: float):
    cutoff = time.time() - days * 86400
    for p in paths:
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                yield p
        except OSError:
            continue


def run(dry_run: bool = False) -> dict:
    freed = 0
    removed = 0

    # 1. Недокачанные файлы
    for p in OUTPUT.rglob("*.part"):
        freed += _remove(p, dry_run)
        removed += 1

    # 2. Кэш Pexels: LRU по mtime (visual_agent обновляет mtime при повторном использовании)
    max_bytes = _env_float("CLIPS_CACHE_MAX_GB", 5.0) * 1024 ** 3
    pexels = sorted(
        (p for p in CLIPS.glob("pexels_*.mp4") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    total = sum(p.stat().st_size for p in pexels)
    for p in pexels:
        if total <= max_bytes:
            break
        size = _remove(p, dry_run)
        total -= size
        freed += size
        removed += 1

    # 3. Промежуточные рендеры (пересоздаются по сигнатуре при следующей сборке)
    if PHOTO_CACHE.is_dir():
        for p in _older_than(PHOTO_CACHE.iterdir(), _PHOTO_CACHE_DAYS):
            freed += _remove(p, dry_run)
            removed += 1

    # 4. Логи — только если задан срок хранения
    log_days = _env_float("LOG_RETENTION_DAYS", 0)
    if log_days > 0 and LOGS.is_dir():
        for p in _older_than(LOGS.glob("*.log"), log_days):
            freed += _remove(p, dry_run)
            removed += 1

    # 5. Готовые видео и артефакты — только если задан срок хранения
    out_days = _env_float("OUTPUT_RETENTION_DAYS", 0)
    if out_days > 0:
        for sub in _OUTPUT_SUBDIRS:
            d = OUTPUT / sub
            if d.is_dir():
                for p in _older_than(d.rglob("*"), out_days):
                    freed += _remove(p, dry_run)
                    removed += 1

    verb = "можно удалить" if dry_run else "удалено"
    logger.info("Housekeeping: %s %d файлов, %.2f ГБ", verb, removed, freed / 1024 ** 3)
    return {"removed": removed, "freed_bytes": freed, "dry_run": dry_run}


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(run(dry_run="--dry-run" in sys.argv))
