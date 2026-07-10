"""
Story Agent — серийные истории с полноценным лором.

Библия мира хранится в SQLite:
  lore_worlds     — сеттинг, тон, стилевой суффикс для image-промптов
  lore_characters — герои: внешность (EN visual_prompt) + фиксированный seed
  lore_episodes   — что уже произошло (summary + cliffhanger каждого эпизода)

Каждый эпизод пишется с контекстом мира и последних эпизодов, заканчивается
клиффхэнгером и раскладывается на 8-10 сцен с EN-промптами для image_agent.

API:
    get_or_create_world(niche, db_path) → world dict
    write_episode(world, target_words) → {title, full_script, hook, scenes, summary, cliffhanger, episode_num}
    save_episode(world_id, episode, run_id, post_id, db_path)
"""

import json
import logging
import os
import random
import re
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_PATH_DEFAULT = os.environ.get("DB_PATH", "pipeline.db")

_WORLD_SYSTEM = """Ты — шоураннер вирусных сериальных историй для TikTok/Shorts (вертикальные видео по 45-60 секунд, русская озвучка).
Придумай оригинальный мир для бесконечного сериала в заданной тематике.
Мир должен: цеплять с первого эпизода, иметь загадку/конфликт на десятки эпизодов, одного яркого главного героя.
Отвечай строго валидным JSON без markdown."""

_WORLD_USER = """Тематика сериала: «{niche}».

Верни JSON:
{{
  "name": "название мира/сериала (2-4 слова, по-русски)",
  "setting": "сеттинг и центральный конфликт, 3-4 предложения по-русски",
  "tone": "тон повествования (например: мрачный, ироничный, напряжённый)",
  "style_suffix": "визуальный стиль для image-промптов, ПО-АНГЛИЙСКИ, например: 'dark fantasy digital painting, muted teal and amber palette, dramatic rim lighting, cinematic composition, highly detailed'",
  "main_character": {{
    "name": "имя героя",
    "role": "кто он в этом мире",
    "personality": "характер в 1-2 предложениях",
    "visual_prompt": "внешность героя ПО-АНГЛИЙСКИ, детально и воспроизводимо: пол, возраст, лицо, волосы, одежда, отличительная деталь. Например: 'a young woman in her 20s with short silver hair, pale sharp face, long black coat with copper clasps, glowing blue amulet on her neck'"
  }}
}}"""

_EPISODE_SYSTEM = """Ты — сценарист вирусного сериала для TikTok/Shorts. Пишешь по-русски, от третьего лица, настоящим временем, короткими фразами. Каждый эпизод:
- HOOK (первая фраза — шок/интрига, без вступлений)
- развитие с конкретными деталями и хотя бы одним поворотом
- обрыв на КЛИФФХЭНГЕРЕ (последняя фраза оставляет вопрос)
Запрещено: политика, реальные трагедии, 18+, медицинские советы.
Отвечай строго валидным JSON без markdown."""

_EPISODE_USER = """МИР: {world_name}
СЕТТИНГ: {setting}
ТОН: {tone}
ГЕРОЙ: {char_name} — {char_role}. {char_personality}

{history_block}

Напиши эпизод {episode_num} длиной {target_words}-{max_words} слов.
{continuity_hint}

Верни JSON:
{{
  "title": "название эпизода (до 60 символов, начинается с '{world_name}. Эпизод {episode_num}:')",
  "full_script": "полный текст озвучки {target_words}-{max_words} слов",
  "hook": "первая фраза сценария дословно",
  "cliffhanger": "последняя фраза сценария дословно",
  "summary": "что произошло в эпизоде, 2 предложения (для памяти сериала)",
  "scenes": [
    {{"text": "фрагмент сценария, который иллюстрирует эта сцена", "image_prompt": "описание кадра ПО-АНГЛИЙСКИ"}},
    ... ровно {n_scenes} сцен, в порядке повествования ...
  ]
}}

Правила для image_prompt:
- ПО-АНГЛИЙСКИ, конкретная композиция кадра (кто, где, что делает, свет)
- если в кадре герой — вставь дословно его описание: "{char_visual}"
- без текста и надписей в кадре
- вертикальный кадр 9:16, разнообразь планы: крупный / средний / общий"""


# ---------------------------------------------------------------------------
# База
# ---------------------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lore_worlds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            niche TEXT NOT NULL,
            name TEXT NOT NULL,
            setting TEXT NOT NULL,
            tone TEXT,
            style_suffix TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lore_characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            world_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            role TEXT,
            personality TEXT,
            visual_prompt TEXT NOT NULL,
            seed INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lore_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            world_id INTEGER NOT NULL,
            episode_num INTEGER NOT NULL,
            title TEXT,
            summary TEXT,
            cliffhanger TEXT,
            run_id TEXT,
            post_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_lore_worlds_niche ON lore_worlds(niche);
        CREATE INDEX IF NOT EXISTS idx_lore_episodes_world ON lore_episodes(world_id);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _llm_json(system: str, user: str, max_tokens: int, temperature: float = 0.8) -> dict:
    """LLM-вызов с парсингом JSON (json mode + regex fallback)."""
    from utils.llm_client import groq_with_cerebras_fallback

    resp = groq_with_cerebras_fallback(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        caller="story_agent",
    )
    raw = (resp.choices[0].message.content or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise ValueError(f"Story Agent: LLM вернул невалидный JSON: {raw[:200]}")
        return json.loads(m.group())


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def get_or_create_world(niche: str, db_path: str = DB_PATH_DEFAULT) -> dict:
    """Возвращает мир для ниши; если нет — генерирует новый через LLM."""
    conn = _connect(db_path)
    try:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT id, name, setting, tone, style_suffix FROM lore_worlds "
            "WHERE niche = ? ORDER BY id DESC LIMIT 1",
            (niche.strip().lower(),),
        ).fetchone()

        if row:
            world_id, name, setting, tone, style_suffix = row
        else:
            logger.info("Story Agent: создаю новый мир для ниши «%s»...", niche)
            data = _llm_json(_WORLD_SYSTEM, _WORLD_USER.format(niche=niche), max_tokens=900)
            char = data.get("main_character") or {}
            if not data.get("name") or not char.get("visual_prompt"):
                raise ValueError(f"Story Agent: неполная библия мира: {data}")

            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                "INSERT INTO lore_worlds (niche, name, setting, tone, style_suffix, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (niche.strip().lower(), data["name"], data.get("setting", ""),
                 data.get("tone", ""), data.get("style_suffix", ""), now),
            )
            world_id = cur.lastrowid
            conn.execute(
                "INSERT INTO lore_characters "
                "(world_id, name, role, personality, visual_prompt, seed, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (world_id, char.get("name", "Герой"), char.get("role", ""),
                 char.get("personality", ""), char["visual_prompt"],
                 random.randint(1, 99_999), now),
            )
            conn.commit()
            name, setting = data["name"], data.get("setting", "")
            tone, style_suffix = data.get("tone", ""), data.get("style_suffix", "")
            logger.info("Story Agent: мир «%s» создан (id=%d)", name, world_id)

        chars = [
            {"name": r[0], "role": r[1], "personality": r[2],
             "visual_prompt": r[3], "seed": r[4]}
            for r in conn.execute(
                "SELECT name, role, personality, visual_prompt, seed "
                "FROM lore_characters WHERE world_id = ? ORDER BY id",
                (world_id,),
            )
        ]
        episodes = conn.execute(
            "SELECT episode_num, summary, cliffhanger FROM lore_episodes "
            "WHERE world_id = ? ORDER BY episode_num DESC LIMIT 3",
            (world_id,),
        ).fetchall()

        return {
            "id": world_id,
            "name": name,
            "setting": setting,
            "tone": tone,
            "style_suffix": style_suffix,
            "characters": chars,
            "last_episodes": [
                {"episode_num": e[0], "summary": e[1], "cliffhanger": e[2]}
                for e in episodes
            ],
            "next_episode_num": (episodes[0][0] + 1) if episodes else 1,
        }
    finally:
        conn.close()


def write_episode(
    world: dict,
    target_words: int = 110,
    n_scenes: int = 9,
) -> dict:
    """Пишет следующий эпизод сериала с учётом лора и прошлых эпизодов."""
    char = world["characters"][0] if world.get("characters") else {
        "name": "Герой", "role": "", "personality": "", "visual_prompt": "a mysterious protagonist",
    }
    ep_num = world.get("next_episode_num", 1)

    history = world.get("last_episodes") or []
    if history:
        lines = [
            f"Эпизод {e['episode_num']}: {e['summary']}" for e in reversed(history)
        ]
        history_block = "ЧТО УЖЕ ПРОИЗОШЛО:\n" + "\n".join(lines)
        continuity_hint = (
            f"Начни с разрешения клиффхэнгера прошлого эпизода: «{history[0]['cliffhanger']}». "
            "Не противоречь прошлым событиям."
        )
    else:
        history_block = "Это ПЕРВЫЙ эпизод — представь героя и мир через действие, не через описание."
        continuity_hint = "Заложи центральную загадку сериала."

    data = _llm_json(
        _EPISODE_SYSTEM,
        _EPISODE_USER.format(
            world_name=world["name"],
            setting=world["setting"],
            tone=world.get("tone", ""),
            char_name=char["name"],
            char_role=char.get("role", ""),
            char_personality=char.get("personality", ""),
            char_visual=char["visual_prompt"],
            history_block=history_block,
            continuity_hint=continuity_hint,
            episode_num=ep_num,
            target_words=target_words,
            max_words=target_words + 25,
            n_scenes=n_scenes,
        ),
        max_tokens=2400,
    )

    full_script = (data.get("full_script") or "").strip()
    scenes = [
        s for s in (data.get("scenes") or [])
        if isinstance(s, dict) and s.get("image_prompt")
    ]
    if not full_script or len(scenes) < 4:
        raise ValueError(
            f"Story Agent: неполный эпизод (script={len(full_script.split())} слов, "
            f"scenes={len(scenes)})"
        )

    words = len(full_script.split())
    logger.info(
        "Story Agent: эпизод %d «%s» — %d слов, %d сцен",
        ep_num, data.get("title", "?"), words, len(scenes),
    )
    return {
        "episode_num": ep_num,
        "title": (data.get("title") or f"{world['name']}. Эпизод {ep_num}")[:100],
        "full_script": full_script,
        "hook": (data.get("hook") or full_script.split(".")[0]).strip(),
        "cliffhanger": (data.get("cliffhanger") or "").strip(),
        "summary": (data.get("summary") or "").strip(),
        "scenes": scenes,
        "character_seed": char.get("seed", 42),
        "style_suffix": world.get("style_suffix", ""),
    }


def save_episode(
    world_id: int,
    episode: dict,
    run_id: str = "",
    post_id: str = "",
    db_path: str = DB_PATH_DEFAULT,
) -> None:
    """Фиксирует эпизод в лор-памяти (вызывать после успешной публикации/сборки)."""
    conn = _connect(db_path)
    try:
        _ensure_tables(conn)
        conn.execute(
            "INSERT INTO lore_episodes "
            "(world_id, episode_num, title, summary, cliffhanger, run_id, post_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (world_id, episode["episode_num"], episode["title"],
             episode.get("summary", ""), episode.get("cliffhanger", ""),
             run_id, post_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        logger.info("Story Agent: эпизод %d сохранён в лор", episode["episode_num"])
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO)

    niche = sys.argv[1] if len(sys.argv) > 1 else "тёмное фэнтези"
    world = get_or_create_world(niche)
    print(json.dumps({k: v for k, v in world.items() if k != "characters"},
                     ensure_ascii=False, indent=2))
    ep = write_episode(world)
    print(json.dumps(ep, ensure_ascii=False, indent=2))
