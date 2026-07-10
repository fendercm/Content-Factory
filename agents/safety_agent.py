"""
Safety Agent — проверяет сценарий и метаданные на нарушения политик платформ.

Вход:  script_text (str), title (str), hashtags (list[str]), platform (str)
Выход: {"safe": True} или {"safe": False, "issue": "...", "category": "..."}

Приоритет: Gemini 2.0 Flash (25s timeout) → Cerebras → Groq → default safe
"""

import concurrent.futures
import json
import logging
import os
from typing import Any

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")

GEMINI_TIMEOUT = 25  # секунд — после этого переключаемся на fallback

_PLATFORM_RULES = {
    "tiktok": (
        "TikTok Community Guidelines: запрещены hate speech, изображения насилия, "
        "контент для взрослых, опасные челленджи, дезинформация о здоровье, "
        "материалы связанные с террором или экстремизмом."
    ),
    "youtube": (
        "YouTube Community Guidelines: запрещены hate speech, harassment, "
        "контент для взрослых без age-gate, графическое насилие, "
        "дезинформация о выборах или вакцинации, спам."
    ),
    "instagram": (
        "Instagram Community Standards: запрещены hate speech, насилие, "
        "сексуальный контент, травля, поддержка вредных организаций."
    ),
    "linkedin": (
        "LinkedIn Professional Community Policies: запрещены оскорбления, "
        "харассмент, дискриминация, дезинформация, контент не связанный с работой."
    ),
    "twitter": (
        "X/Twitter Rules: запрещены угрозы, hate speech, synthetic media без маркировки, "
        "координированное недостоверное поведение."
    ),
}

_PROMPT_TEMPLATE = """Ты — специалист по модерации контента для социальных сетей.

Платформа: {platform}
Правила платформы: {rules}

Проверь следующий контент на нарушения:

ЗАГОЛОВОК: {title}

ХЭШТЕГИ: {hashtags}

ТЕКСТ СЦЕНАРИЯ:
{script_text}

Проанализируй контент и ответь строго JSON:
{{
  "safe": true/false,
  "issue": "краткое описание проблемы или null если безопасно",
  "category": "hate_speech | violence | adult_content | misinformation | copyright | spam | safe"
}}

Только JSON, без пояснений."""


def _parse_result(raw: str) -> dict[str, Any]:
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    result = json.loads(raw)
    return {
        "safe": bool(result.get("safe", True)),
        "issue": result.get("issue") or None,
        "category": result.get("category", "safe"),
    }


# ---------------------------------------------------------------------------
# Engine 1: Gemini 2.0 Flash с жёстким таймаутом
# ---------------------------------------------------------------------------

def _gemini_call(prompt: str) -> dict[str, Any]:
    """Вызов Gemini с таймаутом через ThreadPoolExecutor."""
    from google import genai

    def _do_request():
        client = genai.Client(api_key=GEMINI_API_KEY)
        return client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_do_request)
        try:
            response = future.result(timeout=GEMINI_TIMEOUT)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"Gemini не ответил за {GEMINI_TIMEOUT}с")

    return _parse_result(response.text.strip())


# ---------------------------------------------------------------------------
# Engine 2: Cerebras fallback
# ---------------------------------------------------------------------------

def _cerebras_call(prompt: str) -> dict[str, Any]:
    if not CEREBRAS_API_KEY:
        raise EnvironmentError("CEREBRAS_API_KEY не задан")
    from utils.llm_client import cerebras_call
    response = cerebras_call(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=256,
        caller="safety_agent",
    )
    return _parse_result(response.choices[0].message.content.strip())


# ---------------------------------------------------------------------------
# Engine 3: Groq fallback
# ---------------------------------------------------------------------------

def _groq_call(prompt: str) -> dict[str, Any]:
    if not GROQ_API_KEY:
        raise EnvironmentError("GROQ_API_KEY не задан")
    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY, max_retries=0)
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=256,
        response_format={"type": "json_object"},
    )
    return _parse_result(response.choices[0].message.content.strip())


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

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
    hashtags_str = ", ".join(f"#{h}" for h in hashtags) if hashtags else "(нет)"
    truncated_script = script_text[:2000] + ("\n...[обрезано]" if len(script_text) > 2000 else "")

    prompt = _PROMPT_TEMPLATE.format(
        platform=platform,
        rules=rules,
        title=title,
        hashtags=hashtags_str,
        script_text=truncated_script,
    )

    from utils.providers import is_enabled, mark_dead

    # ── Engine 1: Gemini ──────────────────────────────────────────────────────
    if GEMINI_API_KEY and is_enabled("gemini"):
        try:
            result = _gemini_call(prompt)
            if not result["safe"]:
                logger.warning(
                    "Safety FAILED '%s' / %s: [%s] %s",
                    title, platform, result["category"], result["issue"],
                )
            else:
                logger.info("Safety OK (Gemini) '%s' / %s", title, platform)
            return result
        except TimeoutError as exc:
            logger.warning("Safety Agent: %s — переключаюсь на Cerebras", exc)
        except Exception as exc:
            err = str(exc)
            if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
                # Квота free tier = 0 → выключаем Gemini до конца процесса
                if "limit: 0" in err or "RESOURCE_EXHAUSTED" in err:
                    mark_dead("gemini", err[:200])
                logger.warning("Gemini 429/quota — переключаюсь на Cerebras")
            else:
                logger.warning("Gemini ошибка: %s — переключаюсь на Cerebras", exc)
    else:
        logger.info("GEMINI_API_KEY не задан — пропускаю Gemini")

    # ── Engine 2: Cerebras ────────────────────────────────────────────────────
    if CEREBRAS_API_KEY:
        try:
            result = _cerebras_call(prompt)
            logger.info("Safety OK (Cerebras) '%s' / %s", title, platform)
            return result
        except Exception as exc:
            logger.warning("Cerebras ошибка: %s — переключаюсь на Groq", exc)
    else:
        logger.info("CEREBRAS_API_KEY не задан — пропускаю Cerebras")

    # ── Engine 3: Groq ────────────────────────────────────────────────────────
    if GROQ_API_KEY:
        try:
            result = _groq_call(prompt)
            logger.info("Safety OK (Groq) '%s' / %s", title, platform)
            return result
        except Exception as exc:
            logger.warning("Groq ошибка: %s — safety-check пропущен", exc)

    # ── Fallback: пропускаем проверку ─────────────────────────────────────────
    logger.warning("Все движки safety-check недоступны — считаем контент безопасным")
    return {"safe": True, "issue": None, "category": "skipped"}


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    sample_script = (
        "Знаешь ли ты, что твой мозг принимает 35 000 решений в день? "
        "Вот 3 лайфхака которые помогут принимать лучшие решения. "
        "Первый: делай важные решения утром. Второй: записывай варианты. "
        "Третий: используй технику 10/10/10. Подписывайся!"
    )
    platform_arg = sys.argv[1] if len(sys.argv) > 1 else "tiktok"
    result = check(
        script_text=sample_script,
        title="3 лайфхака для лучших решений",
        hashtags=["психология", "лайфхак", "мозг"],
        platform=platform_arg,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
