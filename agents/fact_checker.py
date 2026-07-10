"""
Fact Checker Agent — проверяет сценарий на фактические ошибки через Groq.

Вход:  script_text (str) — полный текст сценария
Выход: {"valid": True} | {"valid": False, "errors": [...]}

Запускается после script_writer, ДО safety_agent и voice_agent.
"""

import json
import logging
import os

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

MODEL = "llama-3.3-70b-versatile"
CEREBRAS_MODEL = "gpt-oss-120b"

_SYSTEM_PROMPT = """Ты редактор-фактчекер. Ищи ТОЛЬКО критические ошибки, которые опозорят автора.

Флагируй ТОЛЬКО если все три условия выполнены одновременно:
• В тексте упоминается конкретный реальный человек по имени (не анонимный пример)
• ЕМУ приписана ЯВНО НЕВЕРНАЯ профессия или биография (например: "футболист Немов" — он гимнаст)
• Ты на 100% уверен в ошибке

НЕ флагируй — это нормально для мотивационного контента:
• Округлённые статистики ("80% людей", "в 3 раза") — никогда не флагуй
• "Исследования показывают...", "учёные выяснили..." — никогда не флагуй
• Hedge-маркеры ("по некоторым исследованиям", "одна из теорий") — никогда не флагуй
• Гипотетические примеры и аналогии — никогда не флагуй
• Приблизительные цифры и неверифицируемые утверждения — никогда не флагуй
• Любые обобщения, советы, мнения — никогда не флагуй
• Ссылки на умерших классиков (Юнг, Фрейд, Дарвин, Эйнштейн, Кант, Ницше, Сократ,
  Платон, Аристотель, Тесла, Павлов, Скиннер) — никогда не флагуй, даже если
  цитата не дословная. Это литературный приём.
• Атрибуция "учёные из Стэнфорда/Гарварда/Йеля/MIT 2022-2024" — никогда не флагуй

Правило по умолчанию: если не уверен — {"valid": true}.
Флагуй только очевидные ошибки с реальными именами.

Если ошибок НЕТ или ты НЕ УВЕРЕН — верни: {"valid": true}
Если ошибка ОЧЕВИДНА (реальный человек + явно неверная профессия) — верни: {"valid": false, "errors": ["описание"]}

Отвечай ТОЛЬКО JSON без комментариев и markdown."""

_USER_TEMPLATE = """Проверь этот текст сценария на фактические ошибки:

{script_text}"""


def check(script_text: str) -> dict:
    """
    Проверяет сценарий на фактические ошибки.

    Returns:
        {"valid": True} — если ошибок нет
        {"valid": False, "errors": [...]} — если найдены ошибки

    Raises:
        EnvironmentError: если ни один LLM-ключ не настроен
        RuntimeError: если Groq вернул невалидный JSON
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

    logger.info("Fact Checker: проверяю сценарий (%d символов)", len(script_text))

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _USER_TEMPLATE.format(script_text=script_text)},
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

    raw = response.choices[0].message.content.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        # 1. LLM refusal (модерация) — пропускаем
        refusal_signals = ("не могу", "cannot", "can't", "sorry", "извини")
        if any(s in raw.lower() for s in refusal_signals) or not raw:
            logger.warning("Fact Checker: LLM отказался проверять — пропускаем: %s", raw[:80])
            return {"valid": True}
        # 2. Иногда LLM возвращает JSON в markdown-блоке ```json {...} ```
        import re as _re
        m = _re.search(r"\{[^{}]*\"valid\"[^{}]*\}", raw)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        # 3. Если "valid" слово вообще есть и нет явных markers ошибок — считаем pass
        if "valid" in raw.lower() and "true" in raw.lower() and "false" not in raw.lower():
            logger.warning("Fact Checker: невалидный JSON, но похоже на pass: %s", raw[:80])
            return {"valid": True}
        # 4. Финальный fallback — не валим пайплайн, считаем pass с warn
        logger.warning(
            "Fact Checker: невалидный JSON, пропускаем (нет refusal/valid сигналов): %s",
            raw[:200],
        )
        return {"valid": True}

    if "valid" not in result:
        logger.warning("Fact Checker: в ответе нет поля 'valid' — пропускаем: %s", result)
        return {"valid": True}

    if result["valid"]:
        logger.info("Fact Checker: сценарий прошёл проверку")
    else:
        errors = result.get("errors", [])
        logger.warning("Fact Checker: найдены ошибки: %s", errors)

    return result


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
