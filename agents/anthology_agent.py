"""
Anthology Agent — эпизоды сериала-антологии (по умолчанию «Кабинет 7», config/series/<id>.json).

Сценарист — tier="premium" общей LLM-цепочки (Gemini 4 Argon → GPT-6 Astra через KIE),
выбран слепым тестом 2026-10-19 (scripts/model_bakeoff.py, тур 2). Бриф эпизода — ремесло тура 2
(cold open из кульминации, максимум один факт, без лекций) плюс правила понятности после пилота:
имя и жалоба в первых репликах, реплики чередуются, без местоимений без адресата, настоящая причина
звучит вслух, у Веры — одна фраза-переворот.

Монтаж по говорящему: раскадровка — фиксированный набор кадров (SLOTS), каждой реплике
assign_shots() ставит кадр того, кто её говорит (деталь/предмет — на отмеченных репликах).

API:
    load_series(series_id) → dict
    next_episode_number(series, db_path) → int
    recent_episodes(series, db_path, limit) → list[dict]
    propose_cases(series, topics, recent, n) → list[case]
    write_episode(series, case, facts, episode_no, feedback?) → episode
    plan_shots(series, episode) → {"client_visual_en", "shots": [...], "assignment": [{shot, slot, clip}]}
    save_episode(series, episode, run_id, post_id, db_path)   — только после реальной публикации
    queue_episode(...), next_ready(series, db_path), set_episode_status(id, status, db_path)
                                                              — очередь --prepare / --publish-next
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

# ── Промпты ──────────────────────────────────────────────────────────────────

_CRAFT = """Ты — сценарист сериалов, который пишет вертикальные мини-эпизоды для TikTok, Reels и Shorts.
Пишешь по-русски так, как люди говорят вслух. Реплики озвучат синтезаторы речи разными голосами.

ПРИНЦИПЫ РЕМЕСЛА:
- Эпизод — исповедь: человек рассказывает о том, что болит, и зритель это чувствует, а не разгадывает.
- Сюжет ясен с первого прослушивания; чувства персонажи не называют и мораль не объясняют — их выдают
  поступки, оговорки и то, о чём человек молчит.
- Конкретика вместо общих слов: точное время, вещь, имя, место — но не больше, чем нужно истории.
- Каждая реплика добавляет новое — событие, чувство или признание. Повторов нет.
- {hero_name} не читает лекцию и не угадывает по предметам: простой человеческий вопрос и одна фраза,
  после которой всё видно иначе.
- Без канцелярита, терапевтического жаргона («проработать», «ресурс», «границы»), списков,
  скобок и ремарок внутри реплик. Только кириллица, числа можно цифрами."""

_CASES_PROMPT = """{bible}

Придумай {n} разных кейса для следующих эпизодов. Сериал смотрят в ленте: кейс должен цеплять
с первой фразы и держать до конца. Это история, которую человек может рассказать за минуту:
одна эмоция, одна тайна, поворот понятен в одной фразе.

КАЖДЫЙ КЕЙС:
- Ставки: что клиент вот-вот потеряет — брак, ребёнка, родителей, друга, деньги семьи, дело жизни,
  уважение к себе. Конкретно и скоро: «свадьба в субботу», «завтра подписывает развод».
- Тайна: клиент скрывает важный факт — и от собеседника, и от себя. Когда он всплывает, первая фраза эпизода
  звучит совсем иначе.
- Поворот, которого зритель не ждёт: настоящая причина — не то, что подсказывает жалоба, но в ней
  есть логика, и зритель узнаёт в ней себя или близких. Поворот — в чувствах и поступках людей,
  а не в предметах, документах и уликах.
- Темы, о которых спорят в комментариях: ревность и доверие, родители и взрослые дети, деньги в паре,
  зависть к друзьям, наследство, свадьба, развод, второй брак, старые обиды, выбор между близкими.
- Вещь или привычка клиента — её можно снять камерой; в сюжете она не главная.
- Настоящий механизм психологии (без диагнозов и лечения). Никакой политики, медицины, советов
  про деньги, 18+, насилия, самоповреждения.
- Скучное не берём: офис и коллеги, гаджеты, бытовые неудобства, учёба и курсы без высоких ставок.
- Разные клиенты (возраст, пол, жизнь) и разные механизмы. Не повторяй уже вышедшие эпизоды:
{recent}
{topics}
Верни JSON:
{{"cases": [{{
  "title": "название эпизода, до 40 символов, интригующее, без спойлера",
  "client_name": "русское имя", "age": 30, "gender": "female|male", "occupation": "кто по жизни",
  "surface_complaint": "с чем пришёл(ла), одной фразой",
  "stakes": "что клиент теряет и когда",
  "secret": "что клиент скрывает",
  "real_reason": "настоящая причина, одной-двумя фразами",
  "hook": "первая фраза клиента: признание или вопрос, после которого нельзя пролистать",
  "signature_detail": "вещь или привычка, связанная с тайной",
  "psych_concept": "название механизма по-русски",
  "research_query": "2-5 слов для поиска фактов о механизме (научный термин)"
}}]}}"""

_EPISODE_TASK = """ЭПИЗОД {episode_no}. КЛИЕНТ{gender_suffix}: {client_name}, {age}, {occupation}.
С чем пришёл(ла): {surface_complaint}
Ставки: {stakes}
Тайна: {secret}
Настоящая причина: {real_reason}
Вещь или привычка: {signature_detail}
Черновик первой фразы: {hook}

FACTS (можно использовать максимум ОДИН и только если он естественно звучит в устах персонажа;
цифры, годы и имена — только отсюда):
{facts}

Напиши эпизод на 55-65 секунд: {words_min}-{words_max} слов во всех репликах вместе, {lines_min}-{lines_max} реплик.

ЭТО ИСПОВЕДЬ — {setting}. Клиент рассказывает свою историю так, как рассказывают близкому
человеку на кухне. Зритель должен почувствовать, а не разгадывать.
- Говорит в основном клиент — около трёх четвертей всех слов. Его реплики могут быть длинными, до 35 слов.
- Одна история, одна главная эмоция, одна тайна. События по порядку: что случилось → что я сделал(а) →
  о чём молчу. Никаких улик, документов и загадок на внимательность.
- Реплика 1 — признание клиента, после которого нельзя пролистать: одна мысль простыми словами,
  понятно без контекста. Черновик выше можно сделать сильнее.
- {hero_name} говорит мало: 3-4 короткие реплики. Приглашение рассказать, один простой человеческий вопрос
  о людях и чувствах (не о вещах в руках) — после него клиент проговаривается, и в конце одна фраза,
  которая всё переворачивает. Она не угадывает по предметам и не разоблачает.
- В рассказе есть один личный, стыдный момент, который клиент никому не показывал (что делал тайком,
  ночью, в одиночку) — через него зритель чувствует боль.
- Тайна всплывает по шагам, а не одной репликой: клиент отрицает → проговаривается → признаётся
  короткими фразами в двух-трёх репликах. Это эмоциональный момент, а не разгадка.
- Финал — короткое решение клиента или одна реплика РАССКАЗЧИКА про линию сезона.

ЖИВАЯ РЕЧЬ:
- Пиши так, как говорят вслух: короткие фразы, оговорки, «ну», «знаете», «короче» — в меру.
- Одна реплика — одна мысль; в реплике не больше одного нового факта.
- Никаких фамилий, протокольных формулировок и пояснений для зрителя. Близких называют как в жизни:
  «муж», «Лёша», «мама».
- Ни одна фраза и мысль не повторяется.

ПОНЯТНОСТЬ: смотрят вполуха — в первых трёх репликах ясно, кто перед нами и что у него случилось.
{hero_name} обращается к клиенту по имени в одной из первых реплик.

РОЛИ: {hero}, {client_role}, РАССКАЗЧИК (не больше одной реплики и только последней).

ЧЕГО НЕ ДЕЛАТЬ:
- {hero_name} не пересказывает факты лекцией и не объясняет, «что происходит с клиентом».
- Никто не говорит «Именно», «Понимаете», «Это нормально», «Давайте разберёмся».
- Не повторяй формулировки брифа — говори голосами героев.
- Никаких советов по лечению, лекарствам, диагнозов.

ПЕРЕД ОТВЕТОМ ПРОВЕРЬ: держит ли первая фраза; живая ли речь; чувствуется ли эмоция; понятна ли
история с первого прослушивания; нет ли повторов; уложился ли в {words_min}-{words_max} слов.
{feedback}
ФОРМАТ ОТВЕТА (строго, без ремарок и пояснений):
НАЗВАНИЕ: <до 50 символов>
СЦЕНАРИЙ:
<РОЛЬ>: [как сказать: 2-4 слова — эмоция и подача] <реплика>
..."""

_POLISH_PROMPT = """Ты — редактор диалогов. Перепиши реплики так, чтобы они звучали как живая русская речь вслух.

Сохрани: роли и порядок реплик, их число, сюжет, тайну, поворот и финал; общий объём ±10%.
Убери: протокольные и книжные обороты, пересказ нескольких фактов в одной реплике, фамилии,
пояснения для зрителя. Добавь живые интонации и оговорки («ну», «знаете», «погодите») — в меру.
Не добавляй новых фактов и не меняй смысл.

СЦЕНАРИЙ:
{lines}

Пометки в квадратных скобках (как сказать реплику) сохрани — можно уточнить.
Верни только реплики, по одной на строку, в том же формате: РОЛЬ: [пометка] текст"""

_ROLES_PROMPT = """Сценарий: {profession} {hero} и клиент {client_role} ({client_desc}). {hero_name} задаёт
вопросы и не рассказывает о своей жизни; клиент рассказывает о своей семье и своих поступках.
Проверь, нет ли реплик, приписанных не тому персонажу (например, {hero} рассказывает историю клиента
от первого лица, а клиент задаёт вопросы вместо {hero}).

{lines}

Верни JSON: {{"ok": true}} или {{"ok": false, "lines": [номера реплик]}}"""

# Фиксированный набор кадров эпизода. Монтаж по говорящему: на экране лицо того, чья реплика звучит.
SLOTS: list[tuple[str, list[str]]] = [
    ("wide", ["VERA", "CLIENT"]),      # общий план — знакомит зрителя с обоими
    ("client_open", ["CLIENT"]),       # крупный план клиента: cold open
    ("client_talk", ["CLIENT"]),       # средний план клиента: рассказывает, защищается
    ("client_turn", ["CLIENT"]),       # крупный план клиента: настоящая причина
    ("vera_listen", ["VERA"]),         # крупный план Веры: слушает
    ("vera_ask", ["VERA"]),            # средний план Веры: задаёт вопрос
    ("detail", ["CLIENT"]),            # макро: деталь-привычка клиента
    ("story", []),                     # предмет из истории или кадр к реплике рассказчика
]
# Какие кадры оживлять, по убыванию важности. Первые animated_key — Kling 3.0 (ключевые),
# следующие animated_simple — Wan 2.6 Flash. Только кадры без говорящих лиц: оживлённое лицо
# двигает губами не в такт речи, и зритель видит, что голос и картинка не совпадают (2026-10-21).
# Лица остаются кадрами с плавным наездом, пока нет липсинка.
ANIMATE_ORDER = ["detail", "wide", "story"]

_SLOT_DEFAULTS = {
    "wide": "Wide shot: VERA and CLIENT sit in two vintage armchairs facing each other, the whole office visible.",
    "client_open": "Close-up of CLIENT, tense expression, looking slightly past the camera.",
    "client_talk": "Medium shot of CLIENT talking, defensive posture, hands in her lap.",
    "client_turn": "Close-up of CLIENT at the moment of realization, eyes glistening, frozen.",
    "vera_listen": "Close-up of VERA listening attentively, calm, head slightly tilted.",
    "vera_ask": "Medium shot of VERA leaning slightly forward, asking a question, red notebook in hand.",
    "detail": "Macro shot of CLIENT's hands with the telling detail.",
    "story": "Atmospheric still life of an object from the story, no people.",
}

_SHOTS_PROMPT = """Ты — оператор-постановщик вертикального сериала. Монтаж строится по говорящему:
пока звучит реплика, на экране лицо того, кто её говорит. Опиши кадры фиксированного набора.

ПЕРСОНАЖИ:
- VERA: {vera_visual}
- CLIENT ({client_desc}): придумай внешность и одежду, подходящие персонажу, одной фразой
  по-английски — она пойдёт в client_visual_en и в каждый кадр с клиентом.
ЛОКАЦИЯ: {location}

СЦЕНАРИЙ (номера реплик):
{lines}

КАДРЫ — для каждого "prompt_en" по-английски: композиция, ракурс, эмоция, свет. Внешность не описывай,
только имена VERA/CLIENT. VERA и CLIENT сидят в креслах друг напротив друга.
- wide: общий план — VERA и CLIENT в креслах, виден кабинет.
- client_open: крупный план CLIENT с эмоцией первой реплики.
- client_talk: средний план CLIENT — рассказывает, защищается.
- client_turn: крупный план CLIENT в момент, когда звучит настоящая причина.
- vera_listen: крупный план VERA — внимательно слушает.
- vera_ask: средний план VERA — задаёт вопрос, чуть подалась вперёд, красный блокнот в руке.
- detail: макро рук CLIENT: {detail}.
- story: без людей — {story_target}.
Для detail, wide и story добавь "motion_en" — одно простое движение на 5 секунд по-английски:
руки и предметы, дождь за окном, медленное движение камеры. Лиц крупно не показывай и рты не двигай —
говорящие лица не оживляются, губы не совпали бы с речью.

Выбери номера реплик (или null):
- "turn_line": где клиент вслух произносит настоящую причину;
- "detail_line": где замечают или обсуждают деталь;
- "story_line": реплика клиента, где упоминается предмет из кадра story{story_line_hint}.

Верни JSON: {{"client_visual_en": "...", "turn_line": 0, "detail_line": 0, "story_line": null,
"shots": {{"wide": {{"prompt_en": "..."}}, "client_open": {{"prompt_en": "...", "motion_en": "..."}}, ...}}}}"""


def _ngram_list(text: str, n: int) -> list[tuple[str, ...]]:
    """N слов подряд в порядке текста (без регистра, «ё» = «е»)."""
    words = re.findall(r"\w+", text.lower().replace("ё", "е"))
    return [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]


def _ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    return set(_ngram_list(text, n))


# ── Сериал и БД ──────────────────────────────────────────────────────────────

def load_series(series_id: str) -> dict:
    path = ROOT / "config" / "series" / f"{series_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Нет библии сериала: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# Очередь (--prepare): эпизод собран заранее, status='ready', и ждёт ежедневного --publish-next.
# Статусы: ready → published | rejected (снят после просмотра). Старые строки — published.
_QUEUE_COLUMNS = {"status": "TEXT NOT NULL DEFAULT 'published'", "video_path": "TEXT",
                  "publish_title": "TEXT", "description": "TEXT", "source_topic": "TEXT",
                  "published_at": "TEXT"}


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.execute("""CREATE TABLE IF NOT EXISTS anthology_episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id TEXT NOT NULL,
        episode_no INTEGER NOT NULL,
        title TEXT,
        client TEXT,
        psych_concept TEXT,
        summary TEXT,
        run_id TEXT,
        post_id TEXT,
        created_at TEXT NOT NULL
    )""")
    have = {r[1] for r in conn.execute("PRAGMA table_info(anthology_episodes)")}
    for column, decl in _QUEUE_COLUMNS.items():
        if column not in have:
            conn.execute(f"ALTER TABLE anthology_episodes ADD COLUMN {column} {decl}")
    return conn


def next_episode_number(series: dict, db_path: str) -> int:
    conn = _connect(db_path)
    row = conn.execute("SELECT MAX(episode_no) FROM anthology_episodes WHERE series_id=? AND status!='rejected'",
                       (series["id"],)).fetchone()
    conn.close()
    return (row[0] or 0) + 1


def recent_episodes(series: dict, db_path: str, limit: int = 12) -> list[dict]:
    """Вышедшие и стоящие в очереди эпизоды — чтобы новые кейсы их не повторяли."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT episode_no, title, client, psych_concept FROM anthology_episodes "
        "WHERE series_id=? AND status!='rejected' ORDER BY episode_no DESC LIMIT ?",
        (series["id"], limit)).fetchall()
    conn.close()
    return [{"episode_no": r[0], "title": r[1], "client": r[2], "psych_concept": r[3]} for r in rows]


def _insert_episode(series: dict, episode: dict, run_id: str, db_path: str, **fields) -> int:
    case = episode["case"]
    row = {"series_id": series["id"], "episode_no": episode["episode_no"], "title": episode["title"],
           "client": f"{case['client_name']}, {case['age']}, {case['occupation']}",
           "psych_concept": case.get("psych_concept"), "summary": case.get("real_reason"), "run_id": run_id,
           "created_at": datetime.now(timezone.utc).isoformat(), **fields}
    conn = _connect(db_path)
    with conn:
        cur = conn.execute(f"INSERT INTO anthology_episodes ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                           tuple(row.values()))
    conn.close()
    return cur.lastrowid


def save_episode(series: dict, episode: dict, run_id: str, post_id: str, db_path: str) -> None:
    """Эпизод опубликован сразу, без очереди."""
    _insert_episode(series, episode, run_id, db_path, post_id=post_id, status="published",
                    published_at=datetime.now(timezone.utc).isoformat())


def queue_episode(series: dict, episode: dict, run_id: str, db_path: str, *, video_path: str,
                  publish_title: str, description: str, source_topic: str) -> int:
    """Собранный заранее эпизод (--prepare) встаёт в очередь на публикацию. → id строки"""
    return _insert_episode(series, episode, run_id, db_path, post_id="", status="ready", video_path=video_path,
                           publish_title=publish_title, description=description, source_topic=source_topic)


def episode_queue(series: dict, db_path: str) -> list[dict]:
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM anthology_episodes WHERE series_id=? ORDER BY episode_no, id",
                        (series["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def next_ready(series: dict, db_path: str) -> dict | None:
    """Самый ранний эпизод очереди, ещё не опубликованный и не снятый."""
    return next((e for e in episode_queue(series, db_path) if e["status"] == "ready"), None)


def set_episode_status(row_id: int, status: str, db_path: str, post_id: str | None = None) -> None:
    conn = _connect(db_path)
    published_at = datetime.now(timezone.utc).isoformat() if status == "published" else None
    with conn:
        conn.execute("UPDATE anthology_episodes SET status=?, post_id=COALESCE(?, post_id), "
                     "published_at=COALESCE(?, published_at) WHERE id=?", (status, post_id, published_at, row_id))
    conn.close()


def hero_fields(series: dict) -> dict:
    """Подстановки героини в промпты: роль в сценарии, имя, профессия, где идёт действие."""
    h = series["heroine"]
    return {"hero": h["role"], "hero_name": h["name"].split()[0],
            "profession": h.get("profession", "психолог"), "setting": h.get("setting", "в кабинете психолога")}


def bible_text(series: dict) -> str:
    h = series["heroine"]
    return (f"СЕРИАЛ: «{series['name']}» — {series['format']}.\n"
            f"ГЕРОИНЯ: {h['name']}, {h['bio']}\nГолос героини: {h['voice_ru']}\n"
            f"ЛИНИЯ СЕЗОНА: {series['season_arc']}")


# ── LLM ──────────────────────────────────────────────────────────────────────

def _llm(messages: list[dict], tier: str, json_mode: bool, max_tokens: int, caller: str) -> str:
    from utils.llm_client import chat
    resp = chat(messages, tier=tier, temperature=0.9 if not json_mode else 0.4,
                max_tokens=max_tokens, json_mode=json_mode, caller=caller)
    logger.info("Anthology %s: ответил %s/%s", caller, resp.provider, resp.model)
    return resp.choices[0].message.content


def _json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group() if m else text)


def propose_cases(series: dict, topics: list[str] | None, recent: list[dict], n: int = 3) -> list[dict]:
    recent_txt = "\n".join(f"  - №{e['episode_no']} «{e['title']}» ({e['psych_concept']})" for e in recent) or "  (пока нет)"
    topics_txt = ("\nО чём сейчас говорят (используй тему, только если она естественно ложится в кейс):\n"
                  + "\n".join(f"  - {t}" for t in topics)) if topics else ""
    raw = _llm([{"role": "system", "content": _CRAFT.format(**hero_fields(series))},
                {"role": "user", "content": _CASES_PROMPT.format(bible=bible_text(series), n=n,
                                                                 recent=recent_txt, topics=topics_txt)}],
               tier="premium", json_mode=True, max_tokens=2500, caller="anthology_cases")
    cases = _json(raw).get("cases") or []
    required = {"title", "client_name", "age", "gender", "occupation", "surface_complaint", "real_reason",
                "signature_detail", "psych_concept", "research_query"}
    valid = [c for c in cases if required <= set(c)]
    if not valid:
        raise ValueError(f"Anthology: LLM не вернул ни одного полного кейса: {raw[:200]}")
    return valid[:n]


# ── Сценарий ─────────────────────────────────────────────────────────────────

_LINE_RE = re.compile(r"^\s*([А-ЯЁA-Z][А-ЯЁA-Z\- ]{1,30})\s*:\s*(.+)$")
_SHOT_RE = re.compile(r"^\s*\d+[.)]\s*(\[ОЖИВИТЬ\])?\s*(.+)$")


def parse_screenplay(text: str, client_role: str, hero_role: str = "ВЕРА") -> dict:
    """Разбирает «НАЗВАНИЕ / СЦЕНАРИЙ / КАДРЫ» в структуру. Роли нормализуются."""
    title_m = re.search(r"НАЗВАНИЕ\s*:\s*(.+)", text)
    body = text.split("СЦЕНАРИЙ:", 1)[-1]
    script_part, _, shots_part = body.partition("КАДРЫ:")

    allowed = {hero_role: hero_role, client_role: client_role, "РАССКАЗЧИК": "РАССКАЗЧИК", "НАРРАТОР": "РАССКАЗЧИК"}
    lines = []
    for raw in script_part.splitlines():
        m = _LINE_RE.match(raw)
        if not m:
            continue
        role, said = m.group(1).strip(), m.group(2).strip()
        # «[устало, тихо] текст» — как сыграть реплику; в субтитры и проверки идёт только текст
        emo = re.match(r"\[([^\]]{1,60})\]\s*(.+)", said)
        emotion, said = (emo.group(1).strip(), emo.group(2).strip()) if emo else ("", said)
        said = said.strip("«»\"")
        role = allowed.get(role)
        if role and said:
            lines.append({"role": role, "text": said, **({"emotion": emotion} if emotion else {})})

    shots = []
    for raw in shots_part.splitlines():
        m = _SHOT_RE.match(raw)
        if m and m.group(2).strip():
            shots.append({"animate": bool(m.group(1)), "description_ru": m.group(2).strip()})

    return {"title": (title_m.group(1).strip() if title_m else "").strip("«»\""),
            "lines": lines, "shots": shots}


def spoken_text(episode: dict) -> str:
    return " ".join(l["text"] for l in episode["lines"])


def repeated_phrase(lines: list[dict], n: int = 4) -> str | None:
    """Четыре слова подряд, которые звучат в двух разных репликах: эпизод топчется на месте."""
    seen: dict[tuple[str, ...], int] = {}
    for i, line in enumerate(lines):
        for gram in _ngram_list(line["text"], n):
            if gram in seen and seen[gram] != i:
                return " ".join(gram)
            seen.setdefault(gram, i)
    return None


def validate_episode(ep: dict, series: dict) -> list[str]:
    problems = []
    lines = ep["lines"]
    words = len(spoken_text(ep).split())
    if not (series["words_min"] - 10 <= words <= series["words_max"] + 10):
        problems.append(f"длина {words} слов, нужно {series['words_min']}-{series['words_max']}")
    lo, hi = series.get("lines_min", 10), series.get("lines_max", 18)
    if not (lo - 2 <= len(lines) <= hi + 2):
        problems.append(f"реплик {len(lines)}, нужно {lo}-{hi}")
    roles = {l["role"] for l in lines}
    hero = series["heroine"]["role"]
    if hero not in roles or len(roles - {hero, "РАССКАЗЧИК"}) != 1:
        problems.append(f"роли {sorted(roles)}: нужны {hero} и клиент")
        return problems
    narrator = [i for i, l in enumerate(lines) if l["role"] == "РАССКАЗЧИК"]
    if len(narrator) > 1 or (narrator and narrator[0] != len(lines) - 1):
        problems.append("рассказчик — максимум одна реплика и только последней")

    dialog = [l for l in lines if l["role"] != "РАССКАЗЧИК"]
    run, longest = 1, 1
    for prev, cur in zip(dialog, dialog[1:]):
        run = run + 1 if cur["role"] == prev["role"] else 1
        longest = max(longest, run)
    if longest > 4:
        problems.append(f"у одного персонажа {longest} реплик подряд — нужен диалог, а не монолог")
    vera_share = sum(l["role"] == hero for l in dialog) / max(len(dialog), 1)
    if vera_share < 0.25:
        problems.append(f"у {hero} {vera_share:.0%} реплик, нужно не меньше четверти")
    repeated = repeated_phrase(lines)
    if repeated:
        problems.append(f"фраза «{repeated}» повторяется — каждая реплика должна добавлять новое")

    name = (ep.get("case") or {}).get("client_name", "")
    stem = name.lower()[:max(3, min(len(name) - 1, 4))]
    if stem and not any(stem in l["text"].lower() for l in lines[:4]):
        problems.append(f"имя клиента ({name}) не звучит в первых четырёх репликах")
    return problems


_BRIEF_NGRAMS = _ngrams(_CRAFT + "\n" + _EPISODE_TASK, 4)


def copied_from_brief(episode: dict) -> str | None:
    """
    Реплика, повторяющая инструкцию сценаристу (4 слова подряд). Модели копируют примеры из брифа:
    так пример cold open про «тринадцатый курс» попал в историю про стикер на камере (2026-10-21).
    """
    for line in episode["lines"]:
        if _ngrams(line["text"], 4) & _BRIEF_NGRAMS:
            return line["text"]
    return None


def roles_mixed(series: dict, episode: dict) -> list[int]:
    """Номера реплик, приписанных не тому персонажу (по оценке LLM). Ошибка проверки — пустой список."""
    case = episode["case"]
    lines_txt = "\n".join(f"{i}. {l['role']}: {l['text']}" for i, l in enumerate(episode["lines"]))
    try:
        raw = _llm([{"role": "user", "content": _ROLES_PROMPT.format(
            **hero_fields(series), client_role=episode["client_role"], client_desc=f"{case['client_name']}, {case.get('occupation', '')}",
            lines=lines_txt)}], tier="fast", json_mode=True, max_tokens=300, caller="anthology_roles")
        data = _json(raw)
    except Exception as exc:
        logger.warning("Anthology: проверка ролей не прошла (%s) — пропускаю", str(exc)[:120])
        return []
    if data.get("ok", True):
        return []
    return [int(i) for i in data.get("lines") or [] if str(i).isdigit()]


def polish_episode(series: dict, episode: dict) -> dict:
    """
    Редакторский проход: те же реплики живой разговорной речью. Если редактор поменял число или
    порядок ролей, нарушил проверки или скопировал бриф — остаётся исходная версия.
    """
    lines_txt = "\n".join(f"{l['role']}: " + (f"[{l['emotion']}] " if l.get("emotion") else "") + l["text"]
                          for l in episode["lines"])
    try:
        raw = _llm([{"role": "system", "content": _CRAFT.format(**hero_fields(series))},
                    {"role": "user", "content": _POLISH_PROMPT.format(lines=lines_txt)}],
                   tier="premium", json_mode=False, max_tokens=2500, caller="anthology_polish")
    except Exception as exc:
        logger.warning("Anthology: редактор не ответил (%s) — оставляю как есть", str(exc)[:120])
        return episode
    polished = parse_screenplay(f"СЦЕНАРИЙ:\n{raw}", episode["client_role"], series["heroine"]["role"])["lines"]
    if [l["role"] for l in polished] != [l["role"] for l in episode["lines"]]:
        logger.warning("Anthology: редактор изменил роли или число реплик — оставляю исходный текст")
        return episode
    candidate = {**episode, "lines": polished}
    problems = validate_episode(candidate, series)
    if problems or copied_from_brief(candidate):
        logger.warning("Anthology: правка редактора не прошла проверку (%s) — оставляю исходный текст",
                       "; ".join(problems) or "копия брифа")
        return episode
    return candidate


def write_episode(series: dict, case: dict, facts: list[dict], episode_no: int,
                  feedback: str | None = None) -> dict:
    client_role = case["client_name"].upper()
    facts_txt = "\n".join(f"{i}. {f['text']}" for i, f in enumerate(facts[:4], 1)) or \
        "(пусто — без цифр, годов и названий исследований)"
    fb = f"\nИСПРАВЬ В ЭТОЙ ВЕРСИИ: {feedback}\n" if feedback else ""
    prompt = _EPISODE_TASK.format(
        episode_no=episode_no, gender_suffix="КА" if case.get("gender") == "female" else "",
        client_name=case["client_name"], age=case["age"], occupation=case["occupation"],
        surface_complaint=case["surface_complaint"], real_reason=case["real_reason"],
        stakes=case.get("stakes") or "—", secret=case.get("secret") or "—",
        hook=case.get("hook") or "—", signature_detail=case["signature_detail"], facts=facts_txt,
        words_min=series["words_min"], words_max=series["words_max"], client_role=client_role,
        lines_min=series.get("lines_min", 10), lines_max=series.get("lines_max", 18), feedback=fb,
        **hero_fields(series))

    for attempt in range(2):
        raw = _llm([{"role": "system", "content": _CRAFT.format(**hero_fields(series))},
                    {"role": "user", "content": f"{bible_text(series)}\n\n{prompt}"}],
                   tier="premium", json_mode=False, max_tokens=3000, caller="anthology_episode")
        ep = parse_screenplay(raw, client_role, series["heroine"]["role"])
        # «Кабинет 7. Тринадцатый курс» → «Тринадцатый курс»: название сериала добавляет публикация
        ep["title"] = re.sub(rf"^\W*{re.escape(series['name'])}\W*", "", ep["title"], flags=re.I) or ep["title"]
        ep.update({"episode_no": episode_no, "case": case, "client_role": client_role, "raw": raw})
        problems = validate_episode(ep, series)
        copied = copied_from_brief(ep)
        if copied:
            problems.append(f"реплика «{copied}» скопирована из инструкции — придумай свою из этой истории")
        if not problems:
            ep = polish_episode(series, ep)
            mixed = roles_mixed(series, ep)
            if not mixed:
                return ep
            problems.append(f"реплики {mixed} приписаны не тому персонажу")
        logger.warning("Anthology: эпизод не прошёл проверку (%s) — попытка %d", "; ".join(problems), attempt + 1)
        prompt += f"\n\nПРЕДЫДУЩАЯ ВЕРСИЯ ОТКЛОНЕНА: {'; '.join(problems)}. Исправь это."
    raise ValueError(f"Anthology: эпизод не удался: {'; '.join(problems)}")


# ── Раскадровка: монтаж по говорящему ────────────────────────────────────────

def _line_no(value, n: int) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < n else None


def assign_shots(lines: list[dict], marks: dict, hero_role: str = "ВЕРА") -> list[dict]:
    """
    Слот кадра на каждую реплику: на экране тот, кто говорит. marks — номера реплик от раскадровщика:
    turn_line (клиент произносит причину), detail_line (деталь), story_line (предмет из истории).
    → [{"slot", "clip"}]; clip=True — где использовать анимацию слота (один раз на слот).
    """
    n = len(lines)
    turn, detail, story = (_line_no(marks.get(k), n) for k in ("turn_line", "detail_line", "story_line"))
    narrator = any(l["role"] == "РАССКАЗЧИК" for l in lines)
    key_question = turn - 1 if turn and lines[turn - 1]["role"] == hero_role else None
    wide_line = next((i for i, l in enumerate(lines)
                      if l["role"] == hero_role and i not in (0, detail, key_question)), None)

    out, vera_n, client_n = [], 0, 0
    for i, line in enumerate(lines):
        role = line["role"]
        if role == "РАССКАЗЧИК":
            slot = "story"
        elif i == 0:
            slot = "vera_ask" if role == hero_role else "client_open"
        elif i == detail:
            slot = "detail"
        elif role == hero_role:
            if i == wide_line:
                slot = "wide"
            elif i == key_question:
                slot = "vera_ask"
            else:
                slot = ("vera_listen", "vera_ask")[vera_n % 2]
                vera_n += 1
        elif turn is not None and i >= turn:
            slot = "client_turn"
        elif i == story and not narrator:
            slot = "story"
        else:
            slot = ("client_talk", "client_open")[client_n % 2]
            client_n += 1
        out.append({"slot": slot, "clip": False})

    clip_at = {"client_open": 0, "client_turn": turn, "detail": detail, "vera_ask": key_question}
    for slot in dict.fromkeys(a["slot"] for a in out):
        where = [i for i, a in enumerate(out) if a["slot"] == slot]
        out[clip_at.get(slot) if clip_at.get(slot) in where else where[0]]["clip"] = True
    return out


def build_plan(data: dict, episode: dict, key: int, simple: int, hero_role: str = "ВЕРА") -> dict:
    """Ответ раскадровщика → кадры только используемых слотов + назначение кадра каждой реплике."""
    case = episode["case"]
    specs = data.get("shots") or {}
    if isinstance(specs, list):  # модель вернула список — сопоставляем по полю slot
        specs = {s.get("slot"): s for s in specs if isinstance(s, dict)}
    if sum(1 for slot, _ in SLOTS if (specs.get(slot) or {}).get("prompt_en")) < 5:
        raise ValueError(f"Anthology: раскадровка неполная: {sorted(specs)}")

    assignment = assign_shots(episode["lines"], data, hero_role)
    used = list(dict.fromkeys(a["slot"] for a in assignment))
    animate_key, animate_simple = set(ANIMATE_ORDER[:key]), set(ANIMATE_ORDER[key:key + simple])
    shots = []
    for slot, chars in SLOTS:
        if slot not in used:
            continue
        spec = specs.get(slot) or {}
        shot = {"slot": slot, "characters": chars, "prompt_en": spec.get("prompt_en") or _SLOT_DEFAULTS[slot],
                "animate": slot in animate_key | animate_simple}
        if shot["animate"]:
            shot["priority"] = "key" if slot in animate_key else "simple"
            shot["motion_en"] = spec.get("motion_en") or "Subtle natural movement: blinking, breathing, slow push-in."
        shots.append(shot)
    index = {s["slot"]: i for i, s in enumerate(shots)}
    for a in assignment:
        a["shot"] = index[a["slot"]]

    return {"client_visual_en": data.get("client_visual_en")
            or f"a {case['age']}-year-old Russian {case['gender']} {case['occupation']}",
            "shots": shots, "assignment": assignment,
            **{k: _line_no(data.get(k), len(episode["lines"])) for k in ("turn_line", "detail_line", "story_line")}}


def plan_shots(series: dict, episode: dict) -> dict:
    case = episode["case"]
    lines = episode["lines"]
    lines_txt = "\n".join(f"{i}. {l['role']}: {l['text']}" for i, l in enumerate(lines))
    client_desc = f"{case['client_name']}, {case['age']}, {case['gender']}, {case['occupation']}"
    if lines and lines[-1]["role"] == "РАССКАЗЧИК":
        story_target = f"кадр к последней реплике РАССКАЗЧИКА: «{lines[-1]['text']}»"
        story_line_hint = " (здесь есть рассказчик — верни null)"
    else:
        story_target = "предмет или место из истории клиента, о котором говорится в сценарии"
        story_line_hint = ""
    raw = _llm([{"role": "user", "content": _SHOTS_PROMPT.format(
        vera_visual=series["heroine"]["visual_en"], client_desc=client_desc, location=series["location_en"],
        lines=lines_txt, detail=case["signature_detail"], story_target=story_target,
        story_line_hint=story_line_hint)}],
        tier="quality", json_mode=True, max_tokens=3500, caller="anthology_shots")
    return build_plan(_json(raw), episode, series["animated_key"], series["animated_simple"],
                      series["heroine"]["role"])


def speaker_labels(series: dict, episode: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Подписи говорящих над субтитрами: имя, а при первом появлении — имя и кто это."""
    case, heroine, client = episode["case"], series["heroine"], episode["client_role"]
    occupation = case["occupation"].split(",")[0].strip()[:28]
    labels = {heroine["role"]: heroine["role"], client: client}
    intro = {heroine["role"]: heroine.get("intro") or heroine["name"],
             client: f"{case['client_name']}, {case['age']} · {occupation}"}
    return labels, intro
