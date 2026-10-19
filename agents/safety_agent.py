"""
Safety Agent — проверяет сценарий и метаданные на нарушения политик платформ
и стоп-категорий проекта перед производством видео.

Вход:  script_text (str), title (str), hashtags (list[str]), platform (str)
Выход: {"safe": True, ...} или {"safe": False, "issue": "...", "category": "..."}

Движок: общая LLM-цепочка utils.llm_client.chat (Gemini Flash-Lite → Groq → … → Gemma).
Fail-closed: если ни один провайдер не дал разборчивый ответ —
{"safe": False, "category": "unchecked"}; Orchestrator не пускает такой сценарий дальше.
"""

import json
import logging
import os
from typing import Any

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

_PLATFORM_RULES = {
    "tiktok": (
        "TikTok Community Guidelines: запрещены hate speech, насилие, контент для взрослых, "
        "опасные челленджи, дезинформация о здоровье, терроризм и экстремизм. "
        "Реалистичный AI-контент должен быть помечен."
    ),
    "youtube": (
        "YouTube Community Guidelines: запрещены hate speech, harassment, контент для взрослых, "
        "графическое насилие, медицинская дезинформация, спам. Монетизация закрыта для "
        "«inauthentic content»: шаблонного массового контента, контента, рассчитанного "
        "на шок и эмоциональную манипуляцию, и AI-персонажей, рассуждающих о здоровье, "
        "финансах и праве."
    ),
    "instagram": (
        "Instagram Community Standards: запрещены hate speech, насилие, сексуальный контент, "
        "травля, поддержка опасных организаций."
    ),
    "linkedin": (
        "LinkedIn Professional Community Policies: запрещены оскорбления, харассмент, "
        "дискриминация, дезинформация."
    ),
    "twitter": (
        "X Rules: запрещены угрозы, hate speech, вводящие в заблуждение синтетические медиа, "
        "координированное недостоверное поведение."
    ),
}

_CATEGORIES = (
    "politics | medical_advice | financial_advice | adult_content | self_harm | drugs | "
    "dangerous_acts | hate_speech | harassment | violence | misinformation | "
    "manipulative_shock | copyright | spam | safe"
)

_PROMPT_TEMPLATE = """Ты — модератор коротких видео перед публикацией. Решение бинарное: публиковать или нет.

Платформа: {platform}
Правила платформы: {rules}

СТОП-КАТЕГОРИИ ПРОЕКТА — safe=false при любом из пунктов:
1. Политика: политики, выборы, войны и военные действия, санкции, геополитика, протесты.
2. Медицинские советы: диагнозы, лечение конкретных болезней, лекарства и дозировки,
   «замени врача», обещания вылечить. Общие факты о мозге, сне, привычках — допустимы.
3. Финансовые советы: «купи/продай» конкретные активы, обещания дохода, схемы заработка.
   Общие рассуждения о деньгах, карьере и поведении — допустимы.
4. 18+: секс, эротика, наркотики, суицид и самоповреждение, жёсткое насилие.
5. Опасные действия, которые зритель может повторить и пострадать.
6. Вражда и унижение групп людей по признаку пола, национальности, религии, ориентации.
7. Явная дезинформация: утверждения, которые ты точно знаешь как ложные.
8. Шок ради шока: запугивание и эмоциональная манипуляция без пользы для зрителя.

НЕ считается нарушением: дерзкий тон, провокационный вопрос, обращение на «ты»,
популярная психология, критика вредных привычек, личная история рассказчика.

ЗАГОЛОВОК: {title}
ХЭШТЕГИ: {hashtags}
ТЕКСТ СЦЕНАРИЯ:
{script_text}

Ответь строго JSON:
{{"safe": true/false, "issue": "какая фраза и какое правило нарушает, или null", "category": "{categories}"}}"""


def _as_bool(value: Any) -> bool | None:
    """LLM иногда присылает "false" строкой — а bool("false") == True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "1"):
            return True
        if v in ("false", "no", "0"):
            return False
    return None


def _parse_result(raw: str) -> dict[str, Any]:
    """Разбирает ответ движка. Неоднозначный ответ → ValueError."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError(f"ответ safety-движка не объект: {raw[:120]}")
    safe = _as_bool(result.get("safe"))
    if safe is None:
        raise ValueError(f"в ответе safety-движка нет однозначного 'safe': {raw[:120]}")
    return {
        "safe": safe,
        "issue": result.get("issue") or None,
        "category": result.get("category") or ("safe" if safe else "unspecified"),
    }


def _llm_check(prompt: str) -> tuple[dict[str, Any], str]:
    """Один вызов цепочки + один повтор, если ответ не разобрался."""
    from utils.llm_client import chat
    last_error: Exception | None = None
    for _ in range(2):
        resp = chat(
            [{"role": "user", "content": prompt}],
            tier="fast", temperature=0.0, max_tokens=300, json_mode=True, caller="safety_agent",
        )
        try:
            return _parse_result(resp.choices[0].message.content), f"{resp.provider}/{resp.model}"
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning("Safety: неразборчивый ответ %s/%s: %s", resp.provider, resp.model, exc)
    raise ValueError(f"неразборчивый ответ: {last_error}")


def check(
    script_text: str,
    title: str,
    hashtags: list[str],
    platform: str = "tiktok",
) -> dict[str, Any]:
    """
    Проверяет контент на безопасность для публикации.

    Returns:
        {"safe": bool, "issue": str|None, "category": str}
    """
    rules = _PLATFORM_RULES.get(platform, "Общие правила социальных сетей.")
    hashtags_str = ", ".join(f"#{h.lstrip('#')}" for h in hashtags) if hashtags else "(нет)"
    truncated_script = script_text[:3000] + ("\n...[обрезано]" if len(script_text) > 3000 else "")

    prompt = _PROMPT_TEMPLATE.format(
        platform=platform,
        rules=rules,
        title=title,
        hashtags=hashtags_str,
        script_text=truncated_script,
        categories=_CATEGORIES,
    )

    try:
        result, engine = _llm_check(prompt)
    except Exception as exc:
        # Fail-closed: непроверенный контент не публикуем
        logger.error("Safety-check не выполнен (%s) — контент считается непроверенным", exc)
        return {
            "safe": False,
            "issue": f"safety-check не выполнен: {str(exc)[:200]}",
            "category": "unchecked",
        }

    if result["safe"]:
        logger.info("Safety OK (%s) '%s' / %s", engine, title, platform)
    else:
        logger.warning("Safety FAILED (%s) '%s' / %s: [%s] %s",
                       engine, title, platform, result["category"], result["issue"])
    return result


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    sample_script = (
        "Знаешь ли ты, что твой мозг принимает тысячи решений в день? "
        "Вот 3 приёма, которые помогут принимать лучшие решения. "
        "Первый: делай важные решения утром. Второй: записывай варианты. "
        "Третий: используй технику 10/10/10. Подписывайся!"
    )
    platform_arg = sys.argv[1] if len(sys.argv) > 1 else "tiktok"
    result = check(
        script_text=sample_script,
        title="3 приёма для лучших решений",
        hashtags=["психология", "лайфхак", "мозг"],
        platform=platform_arg,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
