"""
Слепое сравнение LLM-сценаристов на реальной задаче проекта.

Каждая модель получает одинаковый бриф эпизода сериала-антологии «психолог Вера».
Ответы перемешиваются и подписываются A, B, C…; какая модель скрывается за буквой,
пишется в отдельный key.json — его открываем только после выбора.

Все модели вызываются через KIE (один баланс с видео/картинками; цены LLM на KIE ~60% ниже
официальных). Формат ID: <адаптер>:<модель>
    kie-claude:<model>   → POST /claude/v1/messages              (Anthropic Messages)
    kie-gemini:<model>   → POST /<model>-openai/v1/chat/completions (OpenAI Chat)
    kie-codex:<model>    → POST /codex/v1/responses              (OpenAI Responses — GPT)
    kie-openai:<model>   → POST /openai/v1/responses             (OpenAI Responses — DeepSeek)
    gemini-direct:<model> — напрямую в Gemini API (проверка без KIE)

Запуск:
    py scripts/model_bakeoff.py                  # все модели из MODELS
    py scripts/model_bakeoff.py --only kie-codex:gpt-6-astra

Результат: output/_bakeoff/<дата>/scripts_blind.md + key.json
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Кандидаты по ценовым уровням (официальные цены за 1M токенов вход/выход; на KIE ~60% ниже)
MODELS = [
    "kie-claude:claude-fable-5",       # $10 / $50  — топ EQ-Bench Creative Writing
    "kie-codex:gpt-6-astra",           # $10 / $50  — топ EQ-Bench Creative Writing
    "kie-gemini:gemini-4-argon",       # $2 / $10   — лидер Arena creative (голоса людей)
    "kie-codex:gpt-6-1-sol",           # $2 / $10
    "kie-openai:deepseek-v4-1-flash",  # дешёвый уровень
    "kie-gemini:gemini-3-8-flash",     # контроль: класс текущего сценариста
]

KIE_BASE = "https://api.kie.ai"
CREDITS_PER_USD = 200

SYSTEM = """Ты — сценарист вертикального мини-сериала-антологии для TikTok, Reels и Shorts.
Пишешь по-русски живым разговорным языком. Текст озвучивают синтезаторы речи разными голосами,
поэтому реплики должны звучать естественно вслух: без канцелярита, списков, скобок и ремарок внутри реплик."""

BIBLE = """СЕРИАЛ: «Кабинет 7» — антология, каждый эпизод — законченная история.
ГЕРОИНЯ: Вера Соколова, 34 года, практикующий психолог в Петербурге. Тёплая, ироничная,
наблюдательная. Её фирменный приём — один неожиданный вопрос, после которого человек
сам видит то, чего не замечал. Не ставит диагнозов и не назначает лечения.
ЛИНИЯ СЕЗОНА (фоном, одна фраза на эпизод максимум): кто-то оставляет Вере анонимные записки
с вопросами, которые она обычно задаёт клиентам."""

CASE = """ЭПИЗОД 4. КЛИЕНТ: Максим, 29 лет, менеджер в логистической компании.
Жалоба: «Каждую ночь сижу в телефоне до трёх, утром разбитый, а вечером снова то же самое».
Развязка: дело не в телефоне — ночь единственное время, которое принадлежит ему, а не
начальнику и не чатам. Это «месть сном».

FACTS (единственная разрешённая база для цифр, дат и атрибуций):
1. Термин «прокрастинация отхода ко сну» предложили исследователи Утрехтского университета
   в 2014 году: человек ложится позже, чем собирался, хотя никаких внешних причин нет.
2. В Китае это явление называют «местью сном»: люди отнимают часы у сна, чтобы вернуть себе
   время, которое днём им не принадлежало. Выражение стало популярным в 2020 году.
3. Исследования связывают откладывание сна с тем, что к вечеру самоконтроль истощается:
   чем больше человек сдерживал себя днём, тем труднее ему вовремя лечь."""

TASK = """Напиши эпизод на 60-70 секунд: 150-170 произносимых слов во всех репликах вместе.

СТРУКТУРА:
1. COLD OPEN (0-3 с): самая острая реплика эпизода — зритель должен остаться.
2. Завязка: кто пришёл и с чем.
3. Две-три короткие сцены-обмена репликами, напряжение растёт.
4. Поворот: неожиданный вопрос Веры → Максим сам понимает настоящую причину.
   Инсайт опирается на FACTS (атрибуция — только из FACTS).
5. Финал: вопрос зрителю или крючок к линии сезона (записка).

ПРАВИЛА: роли — ВЕРА, МАКСИМ, РАССКАЗЧИК (рассказчик — не больше 2 реплик).
Никаких медицинских советов, диагнозов, названий препаратов. Цифры и исследования — только из FACTS.

ФОРМАТ ОТВЕТА (строго):
НАЗВАНИЕ: <до 50 символов>
СЦЕНАРИЙ:
<РОЛЬ>: <реплика>
...
КАДРЫ:
1. [ОЖИВИТЬ] <что в кадре, ракурс, свет — по-русски, одна строка>
... ровно 8 кадров, ровно 4 из них помечены [ОЖИВИТЬ]"""

# ── Тур 2: бриф по правилам сценарного ремесла ───────────────────────────────
# Выводы тура 1: требование «опереться на FACTS» превращало эпизод в лекцию,
# жалоба из брифа повторялась дословно, cold open часто был просто жалобой.

SYSTEM_2 = """Ты — сценарист сериалов, который пишет вертикальные мини-эпизоды для TikTok, Reels и Shorts.
Пишешь по-русски так, как люди говорят вслух. Реплики озвучат синтезаторы речи разными голосами.

ПРИНЦИПЫ РЕМЕСЛА:
- Подтекст важнее текста: персонажи не называют свои чувства и не объясняют мораль. Зритель догадывается сам.
- Конкретика вместо общих слов: не «много работы», а «сорок чатов и голосовое от шефа в 23:40».
- Каждая реплика либо двигает конфликт, либо раскрывает характер. Пустых реплик нет.
- Поворот — момент, когда герой сам слышит, что сказал. Психолог не читает лекцию.
- Короткие реплики: до 20 слов, чаще 5-12. Перебивания, недосказанность, «погодите» — можно.
- Без канцелярита, терапевтического жаргона («проработать», «ресурс», «границы»), списков,
  скобок и ремарок внутри реплик."""

BIBLE_2 = """СЕРИАЛ: «Кабинет 7» — антология: каждый эпизод — законченная история одного клиента.
ГЕРОИНЯ: Вера Соколова, 34, частный психолог, Петербург, кабинет №7 в старом доме на Петроградке.
Голос Веры: говорит мало и коротко, на «вы»; сухая ирония; замечает детали (как человек держит
телефон, что повторяет дважды); не даёт советов «сделайте так» — задаёт вопрос, после которого
человек сам слышит ответ. Её фирменный ход — один неожиданный, почти бытовой вопрос.
Не ставит диагнозов, не говорит о лекарствах и лечении.
ЛИНИЯ СЕЗОНА: кто-то оставляет Вере анонимные записки с её же вопросами. Максимум одна фраза
за эпизод, и только если она естественно ложится в финал."""

CASE_2 = """ЭПИЗОД 5. КЛИЕНТКА: Алина, 31 год, ведущий бухгалтер. Вернулась из отпуска на три дня раньше.
На поверхности: не умеет отдыхать — лежит у моря, а в голове отчёты, рабочий чат проверяет по сорок раз в день.
На самом деле: перед отпуском она бросила квартальный отчёт недоделанным. Незакрытая задача держит
голову сильнее сделанных — отдых не включается, пока петля не закрыта.
Деталь, которая её выдаёт: всё время что-то «закрывает» — колпачок ручки, вкладки, молнию на сумке.

FACTS (можно использовать максимум ОДИН и только если он естественно звучит в устах персонажа;
цифры, годы и имена — только отсюда):
1. В 1927 году советский психолог Блюма Зейгарник описала эффект: незавершённые действия
   запоминаются лучше завершённых — мозг продолжает держать их «открытыми».
2. Исследование Масикампо и Баумайстера 2011 года: если составить конкретный план, когда и как
   доделать незавершённое дело, навязчивые мысли о нём ослабевают — даже если само дело ещё не сделано."""

TASK_2 = """Напиши эпизод на 60-70 секунд: 150-170 слов во всех репликах вместе.

РИТМ:
- 0-3 с — COLD OPEN: самая острая реплика из кульминации, вырванная из контекста. Позже она
  прозвучит снова — и только тогда станет понятна.
- Завязка: кто пришла и с чем — через действие или деталь, а не через представление.
- Две-три короткие сцены: Вера замечает деталь → вопрос → Алина защищается → напряжение растёт.
- Поворот: неожиданный бытовой вопрос Веры → Алина сама произносит настоящую причину.
- Финал: последняя реплика — вопрос, который зритель примерит на себя. Без морали и выводов.

РОЛИ: ВЕРА, АЛИНА, РАССКАЗЧИК (не больше одной реплики и только если без неё непонятно).
Реплики не длиннее 25 слов.

ЧЕГО НЕ ДЕЛАТЬ:
- Вера не пересказывает факты лекцией и не объясняет, «что происходит с Алиной».
- Никто не говорит «Именно», «Понимаете», «Это нормально», «Давайте разберёмся».
- Не повторяй формулировки брифа — говори голосами героев.

ПЕРЕД ОТВЕТОМ ПРОВЕРЬ: есть ли подтекст; есть ли деталь, которую можно снять камерой;
цепляет ли cold open без контекста; нет ли лекции.

ФОРМАТ ОТВЕТА (строго):
НАЗВАНИЕ: <до 50 символов>
СЦЕНАРИЙ:
<РОЛЬ>: <реплика>
...
КАДРЫ:
1. [ОЖИВИТЬ] <что в кадре, ракурс, свет — по-русски, одна строка>
... ровно 8 кадров, ровно 4 из них помечены [ОЖИВИТЬ]"""

ROUNDS = {
    1: {"system": SYSTEM, "user": f"{BIBLE}\n\n{CASE}\n\n{TASK}",
        "title": "эпизод «Кабинет 7», №4 (Максим, месть сном)"},
    2: {"system": SYSTEM_2, "user": f"{BIBLE_2}\n\n{CASE_2}\n\n{TASK_2}",
        "title": "эпизод «Кабинет 7», №5 (Алина, незакрытая задача)"},
}


def _kie_post(path: str, body: dict) -> dict:
    key = os.getenv("KIE_API_KEY", "")
    if not key:
        raise RuntimeError("KIE_API_KEY не задан в .env")
    resp = requests.post(f"{KIE_BASE}{path}", json=body, timeout=600,
                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    # KIE отдаёт ошибки и с HTTP 200 — смотрим на тело
    if data.get("type") == "error" or (data.get("code") not in (None, 200) and "msg" in data):
        raise RuntimeError(f"HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:300]}")
    return data


def _kie_stream_chat(path: str, body: dict) -> tuple[str, dict]:
    """OpenAI Chat Completions со стримом (SSE): склеиваем delta.content."""
    key = os.getenv("KIE_API_KEY", "")
    parts: list[str] = []
    credits = None
    with requests.post(f"{KIE_BASE}{path}", json=body, stream=True, timeout=(30, 900),
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}) as resp:
        for raw in resp.iter_lines():
            line = raw.decode("utf-8", "replace").strip()
            if line.lower().startswith("<!doctype html"):
                raise RuntimeError(f"HTTP {resp.status_code}: HTML-страница ошибки (таймаут Cloudflare/KIE)")
            if not line.startswith("data:"):
                if line.startswith("{"):  # ошибка без SSE
                    raise RuntimeError(f"HTTP {resp.status_code}: {line[:300]}")
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if "choices" not in chunk and (chunk.get("error") or chunk.get("msg")):
                raise RuntimeError(f"ошибка в потоке: {json.dumps(chunk, ensure_ascii=False)[:300]}")
            credits = chunk.get("credits_consumed", credits)
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    parts.append(delta["content"])
    text = "".join(parts)
    if not text:
        raise RuntimeError("пустой потоковый ответ")
    return text, {"credits": credits, "cost_usd": round(credits / CREDITS_PER_USD, 4) if credits else None}


def _usage(data: dict) -> dict:
    credits = data.get("credits_consumed")
    return {"credits": credits, "cost_usd": round(credits / CREDITS_PER_USD, 4) if credits else None}


def _responses_text(data: dict) -> str:
    for item in data.get("output", []):
        if item.get("type") == "message":
            return "".join(c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text")
    raise RuntimeError(f"нет текста в ответе: {json.dumps(data, ensure_ascii=False)[:200]}")


def _call_kie(adapter: str, model: str, messages: list[dict]) -> tuple[str, dict]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
    if adapter == "kie-claude":
        # system в схеме KIE не описан — передаём его в начале пользовательского сообщения
        last = None
        for attempt in range(4):  # Claude на KIE периодически отвечает 429 «Internal error»
            try:
                data = _kie_post("/claude/v1/messages", {
                    "model": model, "stream": False, "max_tokens": 4000,
                    "messages": [{"role": "user", "content": f"{system}\n\n{user}"}],
                })
                text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                return text, _usage(data)
            except RuntimeError as exc:
                last = exc
                time.sleep(20 * (attempt + 1))
        raise last
    if adapter == "kie-gemini":
        # Потоковый ответ: на reasoning_effort=high Gemini думает дольше 100 с,
        # и без стрима Cloudflare перед KIE рвёт соединение (HTTP 524)
        # high на Gemini 4 Argon думает 3-5+ минут и упирается в таймаут Cloudflare (524) —
        # для пайплайна это неприемлемо, поэтому low
        return _kie_stream_chat(f"/{model}-openai/v1/chat/completions", {
            "messages": messages, "stream": True, "reasoning_effort": "low",
        })
    if adapter in ("kie-codex", "kie-openai"):
        path = "/codex/v1/responses" if adapter == "kie-codex" else "/openai/v1/responses"
        body = {"model": model, "stream": False, "instructions": system, "input": user}
        if adapter == "kie-codex":
            body["reasoning"] = {"effort": "medium"}
        data = _kie_post(path, body)
        return _responses_text(data), _usage(data)
    raise ValueError(f"неизвестный адаптер: {adapter}")


def _call_gemini_direct(model: str, messages: list[dict]) -> tuple[str, dict]:
    sys.path.insert(0, str(ROOT))
    from utils.llm_client import gemini_call
    resp = gemini_call(messages, model, temperature=0.9, max_tokens=2000)
    return resp.choices[0].message.content, {"cost_usd": 0.0}


def _spoken_words(text: str) -> int:
    body = text.split("СЦЕНАРИЙ:", 1)[-1].split("КАДРЫ:", 1)[0]
    words = 0
    for line in body.splitlines():
        if ":" in line:
            words += len(line.split(":", 1)[1].split())
    return words


def _load_run(run_dir: Path) -> list[dict]:
    """Восстанавливает результаты прогона из scripts_blind.md + key.json."""
    key = json.loads((run_dir / "key.json").read_text(encoding="utf-8"))
    md = (run_dir / "scripts_blind.md").read_text(encoding="utf-8")
    results = []
    for block in md.split("\n## Вариант ")[1:]:
        letter = block[0]
        text = block.split("\n", 1)[1].strip()
        text = text.rsplit("\n---", 1)[0].strip() if text.endswith("---") else text
        results.append({**key[letter], "text": text.removesuffix("---").strip()})
    return results


def _write_blind(results: list[dict], title: str) -> Path:
    random.shuffle(results)
    out_dir = ROOT / "output" / "_bakeoff" / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# Слепое сравнение сценаристов — {title}", "",
             "Модели скрыты. Прочитайте варианты и выберите лучший (можно 1-2 запасных).", "",
             "Критерии: держит ли cold open, живые ли диалоги, неожиданный ли поворот, "
             "звучит ли текст естественно вслух.", ""]
    key = {}
    for letter, r in zip("ABCDEFGHIJ", results):
        key[letter] = {k: v for k, v in r.items() if k != "text"}
        lines += ["---", "", f"## Вариант {letter}  ·  {r['spoken_words']} слов в репликах", "", r["text"], ""]
    (out_dir / "scripts_blind.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--merge", nargs="+", metavar="RUN_DIR",
                        help="слить несколько прогонов одного тура в один слепой файл")
    parser.add_argument("--only", help="одна модель (<адаптер>:<модель>)")
    parser.add_argument("--round", type=int, default=max(ROUNDS), choices=sorted(ROUNDS))
    args = parser.parse_args()
    models = [args.only] if args.only else MODELS
    brief = ROUNDS[args.round]

    if args.merge:
        merged = [r for d in args.merge for r in _load_run(Path(d))]
        out_dir = _write_blind(merged, f"тур {args.round}: {brief['title']}")
        print(f"Слито {len(merged)} вариантов → {out_dir / 'scripts_blind.md'}")
        return

    messages = [
        {"role": "system", "content": brief["system"]},
        {"role": "user", "content": brief["user"]},
    ]
    results = []
    for model in models:
        t0 = time.time()
        try:
            adapter, name = model.split(":", 1)
            if adapter == "gemini-direct":
                text, usage = _call_gemini_direct(name, messages)
            else:
                text, usage = _call_kie(adapter, name, messages)
            text = text.strip()
            results.append({"model": model, "text": text, "seconds": round(time.time() - t0, 1),
                            "spoken_words": _spoken_words(text), **usage})
            print(f"OK   {model}: {results[-1]['spoken_words']} слов, {results[-1]['seconds']} с, "
                  f"${usage.get('cost_usd')}")
        except Exception as exc:
            print(f"FAIL {model}: {str(exc)[:200]}")

    if not results:
        sys.exit("Ни одна модель не ответила")

    random.shuffle(results)
    out_dir = ROOT / "output" / "_bakeoff" / datetime.now().strftime("%Y-%m-%d_%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    letters = "ABCDEFGHIJ"

    lines = [f"# Слепое сравнение сценаристов, тур {args.round} — {brief['title']}", "",
             "Модели скрыты. Прочитайте варианты и выберите лучший (можно 1-2 запасных).", "",
             "Критерии: держит ли cold open, живые ли диалоги, неожиданный ли поворот, "
             "звучит ли текст естественно вслух.", ""]
    key = {}
    for letter, r in zip(letters, results):
        key[letter] = {k: v for k, v in r.items() if k != "text"}
        lines += ["---", "", f"## Вариант {letter}  ·  {r['spoken_words']} слов в репликах", "", r["text"], ""]
    (out_dir / "scripts_blind.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nГотово: {out_dir / 'scripts_blind.md'}\nКлюч (не открывать до выбора): {out_dir / 'key.json'}")


if __name__ == "__main__":
    main()
