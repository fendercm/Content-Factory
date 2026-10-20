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
- Сюжет ясен, подтекст — в чувствах: зритель всегда понимает, кто говорит, с кем и о чём,
  но персонажи не называют свои чувства и не объясняют мораль — их выдают поступки и детали.
- Конкретика вместо общих слов: не «много работы», а «сорок чатов и голосовое от шефа в 23:40».
- Каждая реплика либо двигает конфликт, либо раскрывает характер. Пустых реплик нет.
- Поворот — момент, когда герой сам слышит, что сказал. Психолог не читает лекцию.
- Короткие реплики: до 20 слов, чаще 5-12. Перебивания, недосказанность, «погодите» — можно.
- Без канцелярита, терапевтического жаргона («проработать», «ресурс», «границы»), списков,
  скобок и ремарок внутри реплик. Только кириллица, числа можно цифрами."""

_CASES_PROMPT = """{bible}

Придумай {n} разных кейса для следующих эпизодов. Каждый кейс — законченная история
одного клиента на 60-70 секунд, с неожиданной, но правдоподобной настоящей причиной.

Требования:
- Разные клиенты (возраст, пол, профессия) и разные психологические механизмы.
- Настоящая причина — реальный, известный психологии механизм (без диагнозов и лечения).
- Никакой политики, медицины, денег-советов, 18+, самоповреждения.
- Деталь-привычка клиента, которую можно снять камерой (что он делает руками, с вещами).
- Не повторяй уже вышедшие эпизоды:
{recent}
{topics}
Верни JSON:
{{"cases": [{{
  "title": "название эпизода, до 40 символов, интригующее, без спойлера",
  "client_name": "русское имя", "age": 30, "gender": "female|male", "occupation": "профессия",
  "surface_complaint": "с чем пришёл(ла), одной фразой",
  "real_reason": "настоящая причина, одной-двумя фразами",
  "signature_detail": "деталь, которая выдаёт клиента",
  "psych_concept": "название механизма по-русски",
  "research_query": "2-5 слов для поиска фактов о механизме (научный термин)"
}}]}}"""

_EPISODE_TASK = """ЭПИЗОД {episode_no}. КЛИЕНТ{gender_suffix}: {client_name}, {age}, {occupation}.
На поверхности: {surface_complaint}
На самом деле: {real_reason}
Деталь, которая выдаёт: {signature_detail}

FACTS (можно использовать максимум ОДИН и только если он естественно звучит в устах персонажа;
цифры, годы и имена — только отсюда):
{facts}

Напиши эпизод на 55-65 секунд: {words_min}-{words_max} слов во всех репликах вместе, {lines_min}-{lines_max} реплик.

ЗРИТЕЛЬ ЛИСТАЕТ ЛЕНТУ И СМОТРИТ С СУБТИТРАМИ — ОН ДОЛЖЕН ПОНИМАТЬ ВСЁ С ПЕРВЫХ СЕКУНД:
- Реплика 1 — COLD OPEN: фраза клиента из кульминации, понятная БЕЗ контекста и цепляющая сама по себе.
  Плохо: «Пусть теперь он один надрывается» — непонятно, кто «он» и о чём речь.
  Хорошо: «Я купила тринадцатый курс, лишь бы не называть клиенту цену».
- Реплики 2-3: Вера обращается к клиенту по имени, клиент своими словами говорит, с чем пришёл.
- Каждая реплика понятна сама по себе: никаких «он», «она», «это», «там» без ясного адресата в той же
  или предыдущей реплике. Людей называй по роли: «муж», «начальник», «клиентка».
- Диалог — пинг-понг: реплики чередуются, у одного персонажа не больше двух реплик подряд.
  У Веры не меньше трети реплик.
- Недосказанность — в чувствах, а не в фактах сюжета: никаких загадочных обрывков.

СТРУКТУРА:
1. Cold open — реплика клиента.
2. Завязка: имя и жалоба, ясно и коротко.
3. Две-три перебежки: Вера замечает деталь → бытовой вопрос → клиент защищается.
4. Поворот: неожиданный вопрос Веры → клиент вслух, своими словами произносит настоящую причину.
   Её нельзя не услышать. Здесь снова звучит фраза из cold open — теперь понятная.
5. Вера одной фразой переворачивает ситуацию: простыми словами, без терминов и морали, до 20 слов —
   то, что зритель заберёт с собой.
6. Финал: короткая реплика клиента или одна реплика РАССКАЗЧИКА про линию сезона — крючок на следующую серию.

РОЛИ: ВЕРА, {client_role}, РАССКАЗЧИК (не больше одной реплики и только последней).
Реплики до 25 слов, чаще 6-14.

ЧЕГО НЕ ДЕЛАТЬ:
- Вера не пересказывает факты лекцией и не объясняет, «что происходит с клиентом».
- Никто не говорит «Именно», «Понимаете», «Это нормально», «Давайте разберёмся».
- Не повторяй формулировки брифа — говори голосами героев.
- Никаких советов по лечению, лекарствам, диагнозов.

ПЕРЕД ОТВЕТОМ ПРОВЕРЬ: понятен ли cold open без контекста; звучит ли имя клиента в первых трёх
репликах; нет ли местоимений без адресата; чередуются ли реплики; произнесена ли настоящая причина
вслух; есть ли у Веры фраза-переворот; уложился ли в {words_min}-{words_max} слов.
{feedback}
ФОРМАТ ОТВЕТА (строго, без ремарок и пояснений):
НАЗВАНИЕ: <до 50 символов>
СЦЕНАРИЙ:
<РОЛЬ>: <реплика>
..."""

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
ANIMATE_KEY = ["client_turn", "client_open"]   # Kling 3.0 — лицо клиента в начале и в повороте
ANIMATE_SIMPLE = ["detail", "vera_ask"]        # Wan 2.6 Flash — руки с деталью и вопрос Веры

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
Для client_open, client_turn, vera_ask, detail добавь "motion_en" — одно простое движение на 5 секунд
по-английски (действие + камера). На лицах — едва заметная мимика, моргание, дыхание; рот закрыт.

Выбери номера реплик (или null):
- "turn_line": где клиент вслух произносит настоящую причину;
- "detail_line": где замечают или обсуждают деталь;
- "story_line": реплика клиента, где упоминается предмет из кадра story{story_line_hint}.

Верни JSON: {{"client_visual_en": "...", "turn_line": 0, "detail_line": 0, "story_line": null,
"shots": {{"wide": {{"prompt_en": "..."}}, "client_open": {{"prompt_en": "...", "motion_en": "..."}}, ...}}}}"""


# ── Сериал и БД ──────────────────────────────────────────────────────────────

def load_series(series_id: str) -> dict:
    path = ROOT / "config" / "series" / f"{series_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Нет библии сериала: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


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
    return conn


def next_episode_number(series: dict, db_path: str) -> int:
    conn = _connect(db_path)
    row = conn.execute("SELECT MAX(episode_no) FROM anthology_episodes WHERE series_id=?",
                       (series["id"],)).fetchone()
    conn.close()
    return (row[0] or 0) + 1


def recent_episodes(series: dict, db_path: str, limit: int = 12) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT episode_no, title, client, psych_concept FROM anthology_episodes "
        "WHERE series_id=? ORDER BY episode_no DESC LIMIT ?", (series["id"], limit)).fetchall()
    conn.close()
    return [{"episode_no": r[0], "title": r[1], "client": r[2], "psych_concept": r[3]} for r in rows]


def save_episode(series: dict, episode: dict, run_id: str, post_id: str, db_path: str) -> None:
    conn = _connect(db_path)
    case = episode["case"]
    with conn:
        conn.execute(
            "INSERT INTO anthology_episodes (series_id, episode_no, title, client, psych_concept, summary,"
            " run_id, post_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (series["id"], episode["episode_no"], episode["title"],
             f"{case['client_name']}, {case['age']}, {case['occupation']}", case.get("psych_concept"),
             case.get("real_reason"), run_id, post_id, datetime.now(timezone.utc).isoformat()))
    conn.close()


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
    raw = _llm([{"role": "system", "content": _CRAFT},
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


def parse_screenplay(text: str, client_role: str) -> dict:
    """Разбирает «НАЗВАНИЕ / СЦЕНАРИЙ / КАДРЫ» в структуру. Роли нормализуются."""
    title_m = re.search(r"НАЗВАНИЕ\s*:\s*(.+)", text)
    body = text.split("СЦЕНАРИЙ:", 1)[-1]
    script_part, _, shots_part = body.partition("КАДРЫ:")

    allowed = {"ВЕРА": "ВЕРА", client_role: client_role, "РАССКАЗЧИК": "РАССКАЗЧИК", "НАРРАТОР": "РАССКАЗЧИК"}
    lines = []
    for raw in script_part.splitlines():
        m = _LINE_RE.match(raw)
        if not m:
            continue
        role, said = m.group(1).strip(), m.group(2).strip().strip("«»\"")
        role = allowed.get(role)
        if role and said:
            lines.append({"role": role, "text": said})

    shots = []
    for raw in shots_part.splitlines():
        m = _SHOT_RE.match(raw)
        if m and m.group(2).strip():
            shots.append({"animate": bool(m.group(1)), "description_ru": m.group(2).strip()})

    return {"title": (title_m.group(1).strip() if title_m else "").strip("«»\""),
            "lines": lines, "shots": shots}


def spoken_text(episode: dict) -> str:
    return " ".join(l["text"] for l in episode["lines"])


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
    if "ВЕРА" not in roles or len(roles - {"ВЕРА", "РАССКАЗЧИК"}) != 1:
        problems.append(f"роли {sorted(roles)}: нужны ВЕРА и клиент")
        return problems
    narrator = [i for i, l in enumerate(lines) if l["role"] == "РАССКАЗЧИК"]
    if len(narrator) > 1 or (narrator and narrator[0] != len(lines) - 1):
        problems.append("рассказчик — максимум одна реплика и только последней")

    dialog = [l for l in lines if l["role"] != "РАССКАЗЧИК"]
    run, longest = 1, 1
    for prev, cur in zip(dialog, dialog[1:]):
        run = run + 1 if cur["role"] == prev["role"] else 1
        longest = max(longest, run)
    if longest > 2:
        problems.append(f"у одного персонажа {longest} реплик подряд — диалог должен чередоваться")
    vera_share = sum(l["role"] == "ВЕРА" for l in dialog) / max(len(dialog), 1)
    if vera_share < 0.3:
        problems.append(f"у Веры {vera_share:.0%} реплик, нужно не меньше трети")

    name = (ep.get("case") or {}).get("client_name", "")
    stem = name.lower()[:max(3, min(len(name) - 1, 4))]
    if stem and not any(stem in l["text"].lower() for l in lines[:3]):
        problems.append(f"имя клиента ({name}) не звучит в первых трёх репликах")
    return problems


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
        signature_detail=case["signature_detail"], facts=facts_txt,
        words_min=series["words_min"], words_max=series["words_max"], client_role=client_role,
        lines_min=series.get("lines_min", 10), lines_max=series.get("lines_max", 18), feedback=fb)

    for attempt in range(2):
        raw = _llm([{"role": "system", "content": _CRAFT},
                    {"role": "user", "content": f"{bible_text(series)}\n\n{prompt}"}],
                   tier="premium", json_mode=False, max_tokens=3000, caller="anthology_episode")
        ep = parse_screenplay(raw, client_role)
        # «Кабинет 7. Тринадцатый курс» → «Тринадцатый курс»: название сериала добавляет публикация
        ep["title"] = re.sub(rf"^\W*{re.escape(series['name'])}\W*", "", ep["title"], flags=re.I) or ep["title"]
        ep.update({"episode_no": episode_no, "case": case, "client_role": client_role, "raw": raw})
        problems = validate_episode(ep, series)
        if not problems:
            return ep
        logger.warning("Anthology: эпизод не прошёл проверку (%s) — попытка %d", "; ".join(problems), attempt + 1)
        prompt += f"\n\nПРЕДЫДУЩАЯ ВЕРСИЯ ОТКЛОНЕНА: {'; '.join(problems)}. Исправь это."
    raise ValueError(f"Anthology: эпизод не удался: {'; '.join(problems)}")


# ── Раскадровка: монтаж по говорящему ────────────────────────────────────────

def _line_no(value, n: int) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < n else None


def assign_shots(lines: list[dict], marks: dict) -> list[dict]:
    """
    Слот кадра на каждую реплику: на экране тот, кто говорит. marks — номера реплик от раскадровщика:
    turn_line (клиент произносит причину), detail_line (деталь), story_line (предмет из истории).
    → [{"slot", "clip"}]; clip=True — где использовать анимацию слота (один раз на слот).
    """
    n = len(lines)
    turn, detail, story = (_line_no(marks.get(k), n) for k in ("turn_line", "detail_line", "story_line"))
    narrator = any(l["role"] == "РАССКАЗЧИК" for l in lines)
    key_question = turn - 1 if turn and lines[turn - 1]["role"] == "ВЕРА" else None
    wide_line = next((i for i, l in enumerate(lines)
                      if l["role"] == "ВЕРА" and i not in (0, detail, key_question)), None)

    out, vera_n, client_n = [], 0, 0
    for i, line in enumerate(lines):
        role = line["role"]
        if role == "РАССКАЗЧИК":
            slot = "story"
        elif i == 0:
            slot = "vera_ask" if role == "ВЕРА" else "client_open"
        elif i == detail:
            slot = "detail"
        elif role == "ВЕРА":
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


def build_plan(data: dict, episode: dict, key: int, simple: int) -> dict:
    """Ответ раскадровщика → кадры только используемых слотов + назначение кадра каждой реплике."""
    case = episode["case"]
    specs = data.get("shots") or {}
    if isinstance(specs, list):  # модель вернула список — сопоставляем по полю slot
        specs = {s.get("slot"): s for s in specs if isinstance(s, dict)}
    if sum(1 for slot, _ in SLOTS if (specs.get(slot) or {}).get("prompt_en")) < 5:
        raise ValueError(f"Anthology: раскадровка неполная: {sorted(specs)}")

    assignment = assign_shots(episode["lines"], data)
    used = list(dict.fromkeys(a["slot"] for a in assignment))
    animate_key, animate_simple = set(ANIMATE_KEY[:key]), set(ANIMATE_SIMPLE[:simple])
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
    return build_plan(_json(raw), episode, series["animated_key"], series["animated_simple"])


def speaker_labels(series: dict, episode: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Подписи говорящих над субтитрами: имя, а при первом появлении — имя и кто это."""
    case, heroine, client = episode["case"], series["heroine"], episode["client_role"]
    occupation = case["occupation"].split(",")[0].strip()[:28]
    labels = {"ВЕРА": heroine["role"], client: client}
    intro = {"ВЕРА": heroine.get("intro") or heroine["name"],
             client: f"{case['client_name']}, {case['age']} · {occupation}"}
    return labels, intro
