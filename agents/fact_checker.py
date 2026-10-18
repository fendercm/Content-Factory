"""
Fact Checker Agent — проверяет сценарий на фактические ошибки через LLM.

Вход:  script_text (str) — полный текст сценария
       facts (list[dict] | None) — факты от research_agent, на которые опирался сценарий
Выход: {"valid": True} | {"valid": False, "errors": [...]}

Запускается после script_writer, ДО safety_agent и voice_agent.

Fail-closed: если ответ LLM не удалось разобрать, сценарий считается
непроверенным ({"valid": False}) — Orchestrator пробует следующую идею.
"""

import json
import logging
import os
import re

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

MODEL = "llama-3.3-70b-versatile"
CEREBRAS_MODEL = "gpt-oss-120b"

_SYSTEM_PROMPT = """Ты редактор-фактчекер коротких видео. Тон и стиль не оцениваешь — только факты.

ФЛАГУЙ (valid=false), если есть хотя бы одно:
1. Реальному человеку по имени приписана явно неверная профессия или биография
   (например: "футболист Немов" — он гимнаст).
2. Конкретная атрибуция исследования — университет, организация, год, автор
   ("учёные Стэнфорда показали", "исследование 2024 года", "по данным ВОЗ") —
   которой НЕТ в списке FACTS ниже. Выдуманная атрибуция = ошибка.
3. Точная статистика (проценты, "в N раз", "N минут") подана как установленный факт,
   но её НЕТ в списке FACTS и она НЕ смягчена hedge-маркером
   ("по некоторым данным", "по некоторым исследованиям", "примерно").
4. Утверждение, которое ты точно знаешь как ложное.

НЕ флагуй:
• Цифры и атрибуции, которые есть в FACTS (допустимо округление и пересказ)
• Hedge-формулировки без конкретного вуза/года ("исследования показывают...")
• Личные истории от 1-го лица, гипотетические примеры, аналогии, мнения, советы
• Ссылки на умерших классиков (Юнг, Фрейд, Дарвин, Эйнштейн, Кант, Ницше, Сократ,
  Платон, Аристотель, Тесла, Павлов, Скиннер, Ферми, Кюри) в их реальной области

В errors пиши кратко: какая фраза и почему (нет в FACTS / неверная биография / ложь).

Если ошибок нет — верни: {"valid": true}
Если есть — верни: {"valid": false, "errors": ["описание", ...]}

Отвечай ТОЛЬКО JSON без комментариев и markdown."""

_USER_TEMPLATE = """FACTS (единственная разрешённая база для цифр и атрибуций):
{facts_block}

Проверь этот текст сценария:

{script_text}"""

_MAX_FACTS = 12


def _format_facts(facts: list[dict] | None) -> str:
    if not facts:
        return "(пусто — значит, в сценарии не должно быть точной статистики и атрибуций исследований)"
    lines = []
    for i, f in enumerate(facts[:_MAX_FACTS], 1):
        text = str(f.get("text", "")).strip()
        if not text:
            continue
        source = f.get("source", "")
        lines.append(f"{i}. {text}" + (f" — {source}" if source else ""))
    return "\n".join(lines) or "(пусто)"


def _as_bool(value) -> bool | None:
    """LLM иногда присылает "false" строкой — bool("false") == True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "1"):
            return True
        if v in ("false", "no", "0"):
            return False
    return None


def _unverified(reason: str) -> dict:
    logger.warning("Fact Checker: %s — сценарий считается непроверенным", reason)
    return {"valid": False, "errors": [f"fact-check не выполнен: {reason}"], "unverified": True}


def check(script_text: str, facts: list[dict] | None = None) -> dict:
    """
    Проверяет сценарий на фактические ошибки.

    Returns:
        {"valid": True} — если ошибок нет
        {"valid": False, "errors": [...]} — если найдены ошибки или проверка не удалась

    Raises:
        EnvironmentError: если ни один LLM-ключ не настроен
    """
    has_llm = any([
        os.getenv("YANDEX_API_KEY"),
        os.getenv("GROQ_API_KEY"),
        os.getenv("CEREBRAS_API_KEY"),
    ])
    if not has_llm:
        raise EnvironmentError(
            "Ни один LLM не настроен: задайте YANDEX_API_KEY, GROQ_API_KEY или CEREBRAS_API_KEY"
        )

    logger.info("Fact Checker: проверяю сценарий (%d символов, %d фактов)",
                len(script_text), len(facts or []))

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _USER_TEMPLATE.format(
            facts_block=_format_facts(facts), script_text=script_text,
        )},
    ]

    from utils.llm_client import groq_with_cerebras_fallback
    response = groq_with_cerebras_fallback(
        messages=messages,
        groq_model=MODEL,
        cb_model=CEREBRAS_MODEL,
        temperature=0.1,
        max_tokens=512,
        response_format={"type": "json_object"},
        caller="fact_checker",
    )

    raw = (response.choices[0].message.content or "").strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Иногда LLM оборачивает JSON в markdown-блок ```json {...} ```
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            result = json.loads(m.group()) if m else None
        except json.JSONDecodeError:
            result = None
        if result is None:
            return _unverified(f"невалидный JSON от LLM: {raw[:120]!r}")

    if not isinstance(result, dict):
        return _unverified(f"ответ не объект: {str(result)[:120]}")

    valid = _as_bool(result.get("valid"))
    if valid is None:
        return _unverified(f"нет корректного поля 'valid': {str(result)[:120]}")

    if valid:
        logger.info("Fact Checker: сценарий прошёл проверку")
        return {"valid": True}

    errors = result.get("errors") or ["ошибка без описания"]
    if not isinstance(errors, list):
        errors = [str(errors)]
    logger.warning("Fact Checker: найдены ошибки: %s", errors)
    return {"valid": False, "errors": errors}


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    sample = sys.argv[1] if len(sys.argv) > 1 else (
        "Алексей Немов — легендарный российский футболист, "
        "завоевавший 6 медалей на Олимпийских играх. "
        "По статистике FIFA, 95% спортсменов бросают карьеру до 25 лет."
    )

    result = check(sample)
    print(json.dumps(result, ensure_ascii=False, indent=2))
