"""
Night Finalize — финализирующий скрипт для ночной автономной работы.

Запускается после batch_dry_run.py:
  1. Читает последний batch JSON отчёт
  2. Анализирует pipeline.db за период батча
  3. Шлёт расширенный morning summary в Telegram с превью кадрами

Запуск:
    py night_finalize.py
"""

import glob
import io
import json
import os
import sys
from datetime import datetime, timezone

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def _latest_batch_json() -> dict | None:
    files = sorted(glob.glob("logs/batch_dry_run_*.json"), reverse=True)
    if not files:
        return None
    with open(files[0], "r", encoding="utf-8") as f:
        return json.load(f)


def _format_report(batch: dict) -> tuple[str, list[str]]:
    """Возвращает (текст_отчёта, список_путей_к_видео_для_превью)."""
    ok_results = [r for r in batch["results"] if r["ok"]]
    fail_results = [r for r in batch["results"] if not r["ok"]]
    avg_sec = (
        sum(r["elapsed_sec"] for r in ok_results) / len(ok_results)
        if ok_results else 0
    )

    parts = [
        "🌅 <b>Night batch — отчёт</b>",
        f"<i>{batch['started_at'][:16]} → {batch['ended_at'][:16]}</i>",
        "",
        f"📊 Видео: <b>{batch['ok']}/{batch['total']}</b> успешных",
        f"⏱️ Среднее время рендера: {avg_sec:.0f}с",
        f"🕐 Полное время: {batch['elapsed_total_sec'] / 60:.1f} мин",
        "",
    ]

    if ok_results:
        parts.append("✅ <b>Успешно собрано:</b>")
        for r in ok_results:
            niche = r.get("niche", "?")
            time_str = f"{r['elapsed_sec']:.0f}с"
            parts.append(f"  • {niche} ({time_str})")

    if fail_results:
        parts.append("")
        parts.append("❌ <b>Ошибки:</b>")
        for r in fail_results:
            niche = r.get("niche", "?")
            err = (r.get("error_tail") or "")[-200:].split("\n")[-1]
            err_short = err[:80].replace("<", "&lt;").replace(">", "&gt;")
            parts.append(f"  • {niche}: {err_short}")

    video_paths = [r.get("video_path") for r in ok_results if r.get("video_path")]
    return "\n".join(parts), video_paths


def _send_report(report: str, video_paths: list[str], max_previews: int = 3) -> None:
    try:
        from agents.notify_agent import send as tg_send
        from agents.notify_agent import send_photo, _extract_frame
    except Exception as exc:
        print(f"Не могу импортировать notify_agent: {exc}")
        return

    try:
        tg_send(report, level="info")
        print("✓ Отчёт отправлен в Telegram")
    except Exception as exc:
        print(f"Telegram send ошибка: {exc}")
        return

    sent = 0
    for vp in video_paths:
        if sent >= max_previews:
            break
        if not vp or not os.path.exists(vp):
            continue
        try:
            frame = _extract_frame(vp)
            if frame:
                send_photo(frame, f"📹 {os.path.basename(vp)}")
                sent += 1
        except Exception as exc:
            print(f"send_photo {os.path.basename(vp)}: {exc}")
    print(f"✓ {sent} превью отправлено")


def main() -> None:
    batch = _latest_batch_json()
    if not batch:
        print("Нет batch JSON отчёта в logs/")
        return

    report, video_paths = _format_report(batch)
    print(report)
    print()
    print(f"📹 Видео для превью: {len(video_paths)}")

    if os.getenv("DRY_REPORT") == "1":
        print("DRY_REPORT=1 — не шлём в Telegram")
        return

    _send_report(report, video_paths)


if __name__ == "__main__":
    main()
