"""
Morning Summary — отправляет в Telegram отчёт о ночных рендерах.

Запуск:
    py morning_summary.py             # последние 24ч
    py morning_summary.py --hours 8   # за 8 часов

Что в отчёте:
  - Сколько видео отрендерено (успешно/упало)
  - Темы и нишы покрытия
  - Top-3 кадра из лучших видео (превью)
  - Топ ошибок если были
  - Время следующего запуска
"""

import argparse
import io
import json
import logging
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("morning_summary")

DB_PATH = os.getenv("DB_PATH", "pipeline.db")


def _runs_since(conn: sqlite3.Connection, since_iso: str) -> dict:
    """Собирает статистику пайплайн-запусков с момента since_iso."""
    rows = conn.execute(
        """SELECT run_id, step, status, payload, error, created_at
           FROM pipeline_state
           WHERE created_at > ?
           ORDER BY id DESC""",
        (since_iso,),
    ).fetchall()

    by_run: dict[str, dict] = {}
    for run_id, step, status, payload, error, ts in rows:
        r = by_run.setdefault(run_id, {"steps": [], "completed": False, "failed": False, "ts": ts})
        r["steps"].append({"step": step, "status": status, "ts": ts, "error": error, "payload": payload})
        if step == "pipeline" and status == "completed":
            r["completed"] = True
            try:
                r["title"] = json.loads(payload or "{}").get("idea", "?")
                r["niche"] = json.loads(payload or "{}").get("niche", "?")
                r["platform"] = json.loads(payload or "{}").get("platform", "?")
            except Exception:
                pass
        if step == "pipeline" and status == "failed":
            r["failed"] = True
            r["error"] = error
        if step == "assembly_agent" and status == "success" and payload:
            try:
                r["video_path"] = json.loads(payload).get("video_path")
            except Exception:
                pass

    completed = [r for r in by_run.values() if r.get("completed")]
    failed    = [r for r in by_run.values() if r.get("failed")]
    return {
        "total": len(by_run),
        "completed": completed,
        "failed": failed,
    }


def _top_errors(conn: sqlite3.Connection, since_iso: str, n: int = 5) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT step, error FROM error_log WHERE created_at > ?",
        (since_iso,),
    ).fetchall()
    # Группируем по step + первые 60 символов ошибки
    groups: Counter[tuple[str, str]] = Counter()
    for step, err in rows:
        key = (step, (err or "")[:60])
        groups[key] += 1
    return [(f"{step}: {err}", cnt) for (step, err), cnt in groups.most_common(n)]


def _format_report(stats: dict, since: datetime, hours: int) -> str:
    completed = stats["completed"]
    failed = stats["failed"]
    total = stats["total"]

    niches = Counter(r.get("niche", "?") for r in completed)
    titles = [r.get("title", "?") for r in completed][:5]

    icon = "🌅" if completed else "😴"
    parts = [
        f"{icon} <b>Morning Summary</b> — за последние {hours}ч",
        f"<i>{since.strftime('%Y-%m-%d %H:%M')} → сейчас</i>",
        "",
        f"📊 Запусков: <b>{total}</b> "
        f"(✅ {len(completed)}  ❌ {len(failed)})",
    ]

    if niches:
        niches_str = ", ".join(f"{k} ×{v}" for k, v in niches.most_common())
        parts.append(f"📂 Ниши: {niches_str}")

    if titles:
        parts.append("")
        parts.append("🎬 <b>Темы видео:</b>")
        for t in titles:
            parts.append(f"  • {t[:80]}")

    return "\n".join(parts)


def _send_summary(report: str, video_paths: list[str]) -> None:
    """Отправляет отчёт + до 3 превью-кадров в Telegram."""
    try:
        from agents.notify_agent import send as tg_send
        from agents.notify_agent import send_photo, _extract_frame
    except Exception as exc:
        logger.error("Не могу импортировать notify_agent: %s", exc)
        return

    # 1. Текстовый отчёт
    try:
        tg_send(report, level="info")
    except Exception as exc:
        logger.error("Telegram send ошибка: %s", exc)
        return

    # 2. До 3 превью кадров из видео
    for vp in video_paths[:3]:
        if not vp or not os.path.exists(vp):
            continue
        frame = _extract_frame(vp)
        if frame:
            caption = f"📹 {os.path.basename(vp)}"
            try:
                send_photo(frame, caption)
            except Exception as exc:
                logger.warning("send_photo ошибка для %s: %s", vp, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Morning summary Telegram report")
    parser.add_argument("--hours", type=int, default=12, help="Период отчёта в часах (default 12)")
    parser.add_argument("--dry", action="store_true", help="Только напечатать, не слать в Telegram")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    since_iso = since.isoformat()

    conn = sqlite3.connect(DB_PATH)
    stats = _runs_since(conn, since_iso)
    errors = _top_errors(conn, since_iso)
    conn.close()

    report = _format_report(stats, since, args.hours)
    if errors:
        report += "\n\n⚠️ <b>Топ ошибок:</b>\n"
        for err, cnt in errors:
            err_short = err[:120].replace("<", "&lt;").replace(">", "&gt;")
            report += f"  • {cnt}× {err_short}\n"

    print(report)
    print()
    print(f"📹 Видео: {[r.get('video_path') for r in stats['completed'] if r.get('video_path')]}")

    if args.dry:
        return

    video_paths = [r.get("video_path") for r in stats["completed"] if r.get("video_path")][:3]
    _send_summary(report, video_paths)
    print("\n✓ Отправлено в Telegram")


if __name__ == "__main__":
    main()
