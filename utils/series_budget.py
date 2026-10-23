"""
Бюджет эпизода сериала на KIE: оценка до первого платного шага (кадры scene_agent) и сверка с балансом.

Цены (по таблице costs, 2026-10): кадр Nano Banana 2 ≈ $0.04, Wan 2.6 Flash 5 с ≈ $0.10,
Kling 3.0 5 с ≈ $0.35, липсинк Kling AI Avatar ≈ $0.04/с — ключевые реплики ≈ 15 с.
Запас 30% — повторы задач и Kling, упавший в Wan.

API:
    estimate_episode_usd(series, plan, scenes_ready=False) → (сумма $, расшифровка)
    check_balance(series, plan, scenes_ready=False) → {"need_usd", "balance_usd" | None, "detail"}
        денег меньше оценки — BudgetError; баланс не узнать — None и предупреждение в лог
"""

import logging

logger = logging.getLogger(__name__)

IMAGE_USD = 0.04
WAN_USD = 0.10
KLING_USD = 0.35
LIPSYNC_USD_PER_S = 0.04
LIPSYNC_SECONDS = 15
RESERVE = 0.30


class BudgetError(RuntimeError):
    """Денег на KIE меньше оценки эпизода — останавливаемся до трат."""


def estimate_episode_usd(series: dict, plan: dict, scenes_ready: bool = False) -> tuple[float, str]:
    """
    Кадры: слоты раскадровки + портрет клиента; анимация — по конфигу сериала (animated_simple → Wan,
    animated_key → Kling); липсинк, если lipsync_lines = "key". scenes_ready — кадры уже на диске (resume).
    """
    parts: list[tuple[str, float]] = []
    if not scenes_ready:
        images = len(plan.get("shots") or []) + 1
        wan, kling = int(series.get("animated_simple") or 0), int(series.get("animated_key") or 0)
        parts.append((f"кадры {images} × ${IMAGE_USD:.2f}", images * IMAGE_USD))
        if wan:
            parts.append((f"Wan {wan} × ${WAN_USD:.2f}", wan * WAN_USD))
        if kling:
            parts.append((f"Kling {kling} × ${KLING_USD:.2f}", kling * KLING_USD))
    if series.get("lipsync_lines", "key") == "key":
        parts.append((f"липсинк ≈{LIPSYNC_SECONDS} с × ${LIPSYNC_USD_PER_S:.2f}", LIPSYNC_SECONDS * LIPSYNC_USD_PER_S))
    total = round(sum(usd for _, usd in parts) * (1 + RESERVE), 2)
    detail = ", ".join(label for label, _ in parts) + f", запас {RESERVE:.0%}"
    return total, detail


def check_balance(series: dict, plan: dict, scenes_ready: bool = False) -> dict:
    from utils import kie_client

    need, detail = estimate_episode_usd(series, plan, scenes_ready)
    try:
        have = float(kie_client.balance()) / kie_client.CREDITS_PER_USD
    except Exception as exc:  # сеть, KIE выключен, нет ключа — решает дальше сам платный шаг
        logger.warning("Баланс KIE не узнать (%s) — продолжаю без проверки, эпизод ≈ $%.2f (%s)",
                       str(exc)[:160], need, detail)
        return {"need_usd": need, "balance_usd": None, "detail": detail}
    if have < need:
        raise BudgetError(f"на KIE ${have:.2f} ({have * kie_client.CREDITS_PER_USD:.0f} кр.), а эпизод ≈ ${need:.2f} "
                          f"({detail}) — пополните баланс; платные кадры не запускались")
    logger.info("Баланс KIE $%.2f, эпизод ≈ $%.2f (%s)", have, need, detail)
    return {"need_usd": need, "balance_usd": round(have, 2), "detail": detail}
