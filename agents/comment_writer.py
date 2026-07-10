"""
Comment Writer Agent — генерирует pinned-комментарий для опубликованного видео.

Цель — drive comments-to-views ratio: алгоритм TikTok/YouTube видит активное
обсуждение → пушит видео в feed.

API:
    write_pinned_comment(script_text, topic) → str  (50-150 символов)

Стиль:
  - От 1-го лица, продолжает мысль видео
  - Обязательно содержит вопрос или провокацию для ответа
  - Без хэштегов, без эмодзи (или с одним максимум)
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

_SYSTEM = """Ты — копирайтер, который пишет pinned-комментарии под TikTok/Shorts.
Твоя задача: написать ОДИН короткий комментарий (50-150 символов), который вызовет
максимум ответов от зрителей.

ПРАВИЛА:
- От 1-го лица («я», «мне», «у меня»).
- Обязательно вопрос ИЛИ острое утверждение, на которое хочется ответить.
- Можно поделиться личным опытом одной фразой.
- Без хэштегов. Максимум 1 эмодзи (или вообще без).
- НЕ повторяй CTA-вопрос из самого видео — добавь НОВЫЙ ракурс.

Хорошие примеры:
- "Я отучился от этого за 3 недели, и продуктивность утроилась. У тебя сколько заняло?"
- "Сижу пишу это в 2 часа ночи — иронично, да? Кто ещё прокрастинирует прямо сейчас?"
- "Самое странное: я заметил это только в 30 лет. А вы?"

Плохие (НЕ так):
- "Лайк если согласны 🔥🔥🔥" — банально
- "Подписывайтесь!" — слишком прямой call
- "Это важная тема" — пустота

Отвечай ТОЛЬКО текст комментария, без кавычек, без меток."""


def write_pinned_comment(script_text: str, topic: str = "") -> str:
    """
    Генерирует pinned-комментарий длиной 50-150 символов.
    При ошибке — возвращает fallback-комментарий из шаблона.
    """
    fallback = "У меня сработало с первого раза. А у тебя?"

    if not script_text:
        return fallback

    user_prompt = (
        f"Тема видео: {topic or 'не указана'}\n\n"
        f"Сценарий (для контекста):\n{script_text[:600]}\n\n"
        "Напиши pinned-комментарий 50-150 символов."
    )

    try:
        from utils.llm_client import groq_with_cerebras_fallback
        resp = groq_with_cerebras_fallback(
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.9,
            max_tokens=200,
            caller="comment_writer",
        )
        text = resp.choices[0].message.content.strip()
        # Удаляем возможные обрамляющие кавычки/markdown
        text = text.strip("\"'«»` ").strip()
        # Берём первую строку (LLM иногда добавляет варианты)
        text = text.split("\n")[0].strip()
        if 20 <= len(text) <= 200:
            logger.info("Comment Writer: %d симв — %s", len(text), text[:60])
            return text
        logger.warning("Comment Writer: ответ %d симв — fallback", len(text))
        return fallback
    except Exception as exc:
        logger.warning("Comment Writer ошибка: %s — fallback", exc)
        return fallback


if __name__ == "__main__":
    import io
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    logging.basicConfig(level=logging.INFO)
    script = (
        sys.argv[1] if len(sys.argv) > 1 else
        "94% людей делают это каждый день — и разрушают свою психику. "
        "Я сам так жил годами. Учёные из Йеля: 20 минут руминации = "
        "стресс как при ДТП. Я начал называть эмоцию вслух — кора успокаивается. "
        "А ты — прокручиваешь старые разговоры или умеешь их отпускать?"
    )
    topic = sys.argv[2] if len(sys.argv) > 2 else "руминация"
    print(write_pinned_comment(script, topic))
