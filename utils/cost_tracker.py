"""
Cost Tracker — учёт стоимости API-вызовов в SQLite.

Таблица costs (создаётся в main.py):
  (id, run_id, agent, provider, operation, units, unit_cost_usd, total_usd, created_at)

API:
    log_cost(run_id, agent, provider, operation, units, unit_cost_usd) → None
    total_for_run(run_id, db_path) → float
    monthly_total(db_path) → float
    daily_breakdown(db_path, days=7) → list[dict]

Тарификация (примерная, на 2026-05):
  Yandex SpeechKit: $0.006 / 1000 символов (alena/filipp)
  Yandex AI Studio (YandexGPT): $0.0012 / 1000 токенов
  Groq (Llama 70B): бесплатно (TPD ограничен) → $0
  Cerebras: бесплатно (1M токенов/день) → $0
  Pexels: бесплатно → $0
  Gemini Flash: бесплатно (15 RPM) → $0
  ElevenLabs: $0.30 / 1000 символов
  YandexART: $0.018 / изображение
  Kling: $0.14 / клип (5 сек, std)
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH_DEFAULT = os.environ.get("DB_PATH", "pipeline.db")

# Известные unit_cost (USD за единицу)
UNIT_COSTS: dict[str, float] = {
    # provider:operation → USD per unit
    "yandex_speechkit:chars":  0.006 / 1000,   # $0.006 per 1k chars
    "yandex_ai:tokens":        0.0012 / 1000,  # $0.0012 per 1k tokens
    "elevenlabs:chars":        0.30 / 1000,    # $0.30 per 1k chars
    "yandexart:image":         0.018,           # $0.018 per image
    "kling:clip":              0.14,            # $0.14 per 5s clip
    "groq:tokens":             0.0,            # free tier
    "cerebras:tokens":         0.0,            # free tier
    "pexels:request":          0.0,            # free
    "gemini:request":          0.0,            # free tier
}


_run_id: str = ""


def set_run_id(rid: str) -> None:
    global _run_id
    _run_id = rid


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def log_cost(
    agent: str,
    provider: str,
    operation: str,
    units: float,
    unit_cost_usd: float | None = None,
    run_id: str | None = None,
    db_path: str = DB_PATH_DEFAULT,
) -> None:
    """
    Записывает стоимость одного API-вызова в БД.

    Args:
        agent:        имя агента ("voice_agent", "script_writer", …)
        provider:     провайдер ("yandex_speechkit", "groq", "pexels", …)
        operation:    тип операции ("chars", "tokens", "image", "clip", "request")
        units:        количество единиц (символы, токены, штуки)
        unit_cost_usd: стоимость одной единицы; если None — берётся из UNIT_COSTS
        run_id:       ID запуска; если None — берётся из глобального _run_id
        db_path:      путь к SQLite БД
    """
    if unit_cost_usd is None:
        key = f"{provider}:{operation}"
        unit_cost_usd = UNIT_COSTS.get(key, 0.0)

    total_usd = units * unit_cost_usd
    rid = run_id or _run_id

    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                """INSERT INTO costs
                   (run_id, agent, provider, operation, units, unit_cost_usd, total_usd, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rid, agent, provider, operation,
                    units, unit_cost_usd, total_usd,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        if total_usd > 0:
            logger.debug(
                "Cost [%s/%s/%s]: %.0f units × $%.6f = $%.4f",
                agent, provider, operation, units, unit_cost_usd, total_usd,
            )
    except Exception as exc:
        logger.warning("Cost tracker write error: %s", exc)


def total_for_run(run_id: str, db_path: str = DB_PATH_DEFAULT) -> float:
    """Суммарная стоимость одного пайплайн-запуска в USD."""
    try:
        conn = _connect(db_path)
        row = conn.execute(
            "SELECT COALESCE(SUM(total_usd), 0) FROM costs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        logger.warning("Cost tracker read error: %s", exc)
        return 0.0


def monthly_total(db_path: str = DB_PATH_DEFAULT) -> float:
    """Суммарная стоимость за последние 30 дней."""
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn = _connect(db_path)
        row = conn.execute(
            "SELECT COALESCE(SUM(total_usd), 0) FROM costs WHERE created_at >= ?",
            (since,),
        ).fetchone()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        logger.warning("Cost tracker monthly error: %s", exc)
        return 0.0


def daily_breakdown(db_path: str = DB_PATH_DEFAULT, days: int = 7) -> list[dict[str, Any]]:
    """Разбивка расходов по агентам за последние N дней."""
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conn = _connect(db_path)
        rows = conn.execute(
            """SELECT agent, provider, SUM(units) as total_units, SUM(total_usd) as total_usd
               FROM costs
               WHERE created_at >= ?
               GROUP BY agent, provider
               ORDER BY total_usd DESC""",
            (since,),
        ).fetchall()
        conn.close()
        return [
            {"agent": r[0], "provider": r[1], "total_units": r[2], "total_usd": round(r[3], 6)}
            for r in rows
        ]
    except Exception as exc:
        logger.warning("Cost tracker breakdown error: %s", exc)
        return []


def format_summary(db_path: str = DB_PATH_DEFAULT) -> str:
    """Форматирует суточный отчёт стоимостей для Telegram-уведомления."""
    monthly = monthly_total(db_path)
    breakdown = daily_breakdown(db_path, days=1)

    if not breakdown and monthly == 0.0:
        return "💰 Costs today: $0.00 (all free APIs)"

    lines = [f"💰 Cost today: ${sum(b['total_usd'] for b in breakdown):.4f}"]
    for b in breakdown[:5]:
        if b["total_usd"] > 0:
            lines.append(f"  • {b['agent']}/{b['provider']}: ${b['total_usd']:.4f}")
    lines.append(f"  Monthly total: ${monthly:.4f}")
    return "\n".join(lines)
