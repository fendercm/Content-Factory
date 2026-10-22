"""
Видео на заказ (панель → Студия): бриф клиента → сценарий → голоса, кадры, анимация, липсинк → монтаж.

Заказ живёт в output/orders/<id>/order.json; сериалы, очередь и публикация не затрагиваются.

    new_order(data)     → заказ с параметрами по умолчанию
    write_script(order) → order["script"] = {title, characters, lines} — пишет модель, автор правит в панели
    build(order)        → order["result"] = {video, duration, cost_usd, qa}

Запуск шагов — scripts/order_run.py (фоновая задача панели).
"""

import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
ORDERS_DIR = ROOT / "output" / "orders"

WORDS_PER_SEC = 2.3
DURATIONS = (15, 30, 60, 90)
SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080)}
MODES = {"auto", "scene", "voiceover"}
STYLES = {
    "cinema": "Cinematic photorealistic film still, natural light, shallow depth of field, 35mm film look.",
    "ad": "Premium commercial photography, clean composition, bright soft light, vivid natural colors.",
    "cartoon": "Colorful 3D animated feature film style, expressive characters, soft global illumination.",
    "anime": "Japanese anime style, clean line art, cinematic lighting, detailed backgrounds.",
    "illustration": "Hand-drawn flat illustration, textured paper, warm limited palette.",
}
ANIMATE = {"none": 0, "some": 3, "more": 6}
MOODS = {"auto", "none", "calm", "upbeat", "dramatic"}
VOICES = {"female": ["Leda", "Kore", "Aoede", "Sulafat"], "male": ["Orus", "Charon", "Puck", "Fenrir"]}
NARRATOR_VOICE = {"female": "Kore", "male": "Charon"}
EDGE_VOICE = {"female": "ru-RU-SvetlanaNeural", "male": "ru-RU-DmitryNeural"}
NARRATOR = "ДИКТОР"
LIPSYNC_MAX = 3
SUB_COLORS = ["&H0000E5FF&", "&H00FFFFFF&", "&H00FFC864&", "&H0080FF80&"]
_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{4}$")
_FILE_RE = re.compile(r"^[\w.-]{1,80}\.(jpg|jpeg|png|webp)$", re.I)
_LINE_RE = re.compile(r"^\s*([А-ЯЁA-Z][А-ЯЁA-Z0-9\- ]{0,30})\s*:\s*(?:\[([^\]]{1,60})\]\s*)?(.+)$")

STAGES = [("voice", "Озвучка"), ("cast", "Герои"), ("shots", "Раскадровка"), ("images", "Кадры"),
          ("motion", "Анимация"), ("lipsync", "Липсинк"), ("assembly", "Монтаж"), ("qa", "Проверка")]


# ── Хранение ─────────────────────────────────────────────────────────────────

def order_dir(order_id: str) -> Path:
    if not _ID_RE.match(order_id or ""):
        raise ValueError("неверный id заказа")
    return ORDERS_DIR / order_id


def load(order_id: str) -> dict:
    return json.loads((order_dir(order_id) / "order.json").read_text(encoding="utf-8"))


def save(order: dict) -> None:
    d = order_dir(order["id"])
    d.mkdir(parents=True, exist_ok=True)
    order["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = d / "order.json.part"
    tmp.write_text(json.dumps(order, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / "order.json")


def list_orders() -> list[dict]:
    out = []
    for path in sorted(ORDERS_DIR.glob("*/order.json"), reverse=True) if ORDERS_DIR.exists() else []:
        try:
            o = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({k: o.get(k) for k in ("id", "title", "client", "status", "created_at", "updated_at",
                                          "duration", "aspect", "mode")}
                   | {"cost_usd": (o.get("result") or {}).get("cost_usd")})
    return out


def apply_params(order: dict, data: dict) -> list[str]:
    """Параметры заказа из панели; возвращает список неверных полей."""
    errors = []
    for key in ("title", "client", "brief", "style_custom"):
        if key in data:
            order[key] = str(data[key] or "").strip()[:4000]
    for key, allowed in (("mode", MODES), ("aspect", set(SIZES)), ("style", set(STYLES) | {"custom"}),
                         ("voice", {"auto", "female", "male"}), ("music", MOODS), ("animate", set(ANIMATE))):
        if key in data:
            if data[key] in allowed:
                order[key] = data[key]
            else:
                errors.append(key)
    if "duration" in data:
        try:
            order["duration"] = int(data["duration"])
            if order["duration"] not in DURATIONS:
                errors.append("duration")
        except (TypeError, ValueError):
            errors.append("duration")
    for key in ("text_is_final", "subtitles", "lipsync"):
        if key in data:
            order[key] = bool(data[key])
    return errors


def new_order(data: dict) -> tuple[dict, list[str]]:
    now = datetime.now()
    order = {"id": f"{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}", "title": "", "client": "", "brief": "",
             "text_is_final": False, "mode": "auto", "duration": 30, "aspect": "9:16", "style": "cinema",
             "style_custom": "", "voice": "auto", "subtitles": True, "music": "auto", "animate": "some",
             "lipsync": True, "files": [], "logo": None, "status": "draft", "script": None, "progress": [],
             "result": None, "error": "", "created_at": datetime.now(timezone.utc).isoformat()}
    errors = apply_params(order, data)
    return order, errors


def add_file(order: dict, name: str, data: bytes, kind: str = "asset") -> str:
    """Фото товара/человека/логотип клиента. kind=logo — ставится в угол ролика."""
    stem = re.sub(r"[^\w.-]+", "_", Path(name).stem)[:60] or "file"
    ext = Path(name).suffix.lower()
    fname = f"{stem}{ext}"
    if not _FILE_RE.match(fname):
        raise ValueError("нужна картинка jpg, png или webp")
    files_dir = order_dir(order["id"]) / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while (files_dir / fname).exists():
        n += 1
        fname = f"{stem}_{n}{ext}"
    (files_dir / fname).write_bytes(data)
    if kind == "logo":
        order["logo"] = fname
    else:
        order["files"].append(fname)
    return fname


def file_path(order: dict, name: str) -> Path | None:
    if not _FILE_RE.match(name or "") or (name not in order.get("files", []) and name != order.get("logo")):
        return None
    path = order_dir(order["id"]) / "files" / name
    return path if path.is_file() else None


def _stage(order: dict, key: str, status: str, note: str = "") -> None:
    stages = {s["key"]: s for s in order.setdefault("progress", [])}
    now = datetime.now(timezone.utc).isoformat()
    st = stages.get(key)
    if not st:
        st = {"key": key, "label": dict(STAGES).get(key, key)}
        order["progress"].append(st)
    if status == "running":
        st["started_at"] = now
    st.update(status=status, note=note, at=now)
    save(order)


# ── Сценарий ─────────────────────────────────────────────────────────────────

def _llm(messages: list[dict], json_mode: bool, max_tokens: int, caller: str) -> str:
    from utils.llm_client import chat
    resp = chat(messages, tier="premium", temperature=0.4 if json_mode else 0.9, max_tokens=max_tokens,
                json_mode=json_mode, caller=caller)
    logger.info("Order %s: ответил %s/%s", caller, resp.provider, resp.model)
    return resp.choices[0].message.content


def _json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group() if m else text)


def word_budget(duration: int) -> tuple[int, int]:
    return round(duration * WORDS_PER_SEC * 0.85), round(duration * WORDS_PER_SEC * 1.05)


def style_text(order: dict) -> str:
    return order.get("style_custom") if order.get("style") == "custom" and order.get("style_custom") \
        else STYLES.get(order.get("style"), STYLES["cinema"])


_MODE_RULES = {
    "scene": "СЦЕНКА: 1-3 персонажа говорят друг с другом в кадре, без диктора.",
    "voiceover": f"ГОЛОС ЗА КАДРОМ: один персонаж {NARRATOR} (visual_en пусто), каждая его реплика — одно "
                 "предложение; на экране — то, о чём он говорит.",
    "auto": f"Формат выбери сам под бриф: сценка с героями, голос за кадром ({NARRATOR}) или {NARRATOR} + 1-2 героя.",
}

_SCRIPT_PROMPT = """Ты сценарист коротких роликов на заказ. Напиши сценарий ролика по брифу клиента.

БРИФ КЛИЕНТА:
{brief}

ФОРМАТ: {mode}
ДЛИТЕЛЬНОСТЬ: {duration} с — всего {words_min}-{words_max} слов во всех репликах.
КАРТИНКА: {style}
{files}
ПРАВИЛА:
- Первая фраза цепляет сразу: вопрос, признание, неожиданный факт или конфликт.
- Реплики короткие и разговорные, как говорят живые люди; одна мысль на реплику.
- Если бриф про товар или услугу — покажи пользу через ситуацию, а не перечислением; финал — ясный
  призыв к действию из брифа (если он там есть).
- Никакой политики, медицинских советов, 18+, насилия; не выдумывай цены, адреса и обещания,
  которых нет в брифе.
- Эмоция реплики — 1-3 слова, как её сказать (озвучка сыграет, вслух не читается).
{final_rule}
Верни JSON:
{{"title": "название ролика, до 50 символов",
  "characters": [{{"role": "ИМЯ КАПСОМ (для диктора — {narrator})", "gender": "female|male",
                   "visual_en": "внешность по-английски: возраст, лицо, волосы, одежда (для диктора пусто)",
                   "asset": "имя файла клиента с фото этого человека или null"}}],
  "lines": [{{"role": "ИМЯ", "emotion": "как сказать", "text": "реплика"}}]}}"""


def _files_text(order: dict) -> str:
    if not order.get("files"):
        return ""
    return ("ФАЙЛЫ КЛИЕНТА (фото товара, людей, места — используй, если подходят): "
            + ", ".join(order["files"]) + "\n")


def write_script(order: dict) -> dict:
    words_min, words_max = word_budget(order["duration"])
    final_rule = ("- ТЕКСТ КЛИЕНТА ГОТОВЫЙ: используй его дословно, ничего не переписывай и не добавляй — "
                  "только раздели на реплики (для голоса за кадром — по предложениям) и подбери роли.\n"
                  if order.get("text_is_final") else "")
    raw = _llm([{"role": "user", "content": _SCRIPT_PROMPT.format(
        brief=order["brief"], mode=_MODE_RULES[order.get("mode", "auto")], duration=order["duration"],
        words_min=words_min, words_max=words_max, style=style_text(order), files=_files_text(order),
        final_rule=final_rule, narrator=NARRATOR)}], json_mode=True, max_tokens=4000, caller="order_script")
    data = _json(raw)
    script = normalize_script({"title": data.get("title") or order.get("title") or "Ролик",
                               "characters": data.get("characters") or [], "lines": data.get("lines") or []},
                              order)
    if not script["lines"]:
        raise ValueError(f"сценарист не вернул реплик: {raw[:200]}")
    return script


def parse_lines(text: str) -> list[dict]:
    """«РОЛЬ: [эмоция] реплика» по строке → [{role, emotion?, text}]."""
    lines = []
    for raw in str(text or "").splitlines():
        m = _LINE_RE.match(raw)
        if m and m.group(3).strip():
            lines.append({"role": m.group(1).strip().upper(), "text": m.group(3).strip(),
                          **({"emotion": m.group(2).strip()} if m.group(2) else {})})
    return lines


def normalize_script(script: dict, order: dict) -> dict:
    """Роли реплик ↔ персонажи: у каждой роли есть персонаж с полом и голосом; лишние персонажи убираются."""
    chars = {}
    for c in script.get("characters") or []:
        role = str(c.get("role", "")).strip().upper()
        if role:
            chars[role] = {"role": role, "gender": "male" if c.get("gender") == "male" else "female",
                           "visual_en": str(c.get("visual_en") or "").strip(),
                           "asset": c.get("asset") if c.get("asset") in order.get("files", []) else None,
                           **({"voice": c["voice"]} if c.get("voice") in VOICES["female"] + VOICES["male"] else {})}
    lines = []
    for line in script.get("lines") or []:
        role, text = str(line.get("role", "")).strip().upper(), str(line.get("text", "")).strip()
        if not role or not text:
            continue
        lines.append({"role": role, "text": text, **({"emotion": line["emotion"]} if line.get("emotion") else {})})
        if role not in chars:
            gender = order.get("voice") if order.get("voice") in VOICES else "female"
            chars[role] = {"role": role, "gender": gender, "visual_en": "", "asset": None}
    used = {l["role"] for l in lines}
    characters = [c for r, c in chars.items() if r in used]
    taken: dict[str, int] = {}
    for c in characters:  # голос: диктору — выбранный в заказе, героям — разные по полу
        if c.get("voice"):
            continue
        if c["role"] == NARRATOR:
            gender = order.get("voice") if order.get("voice") in VOICES else c["gender"]
            c["gender"], c["voice"] = gender, NARRATOR_VOICE[gender]
        else:
            pool = [v for v in VOICES[c["gender"]] if v != NARRATOR_VOICE[c["gender"]]] or VOICES[c["gender"]]
            c["voice"] = pool[taken.get(c["gender"], 0) % len(pool)]
            taken[c["gender"]] = taken.get(c["gender"], 0) + 1
    words = sum(len(l["text"].split()) for l in lines)
    words_min, words_max = word_budget(order["duration"])
    warnings = []
    if words > words_max * 1.15:
        warnings.append(f"{words} слов — ролик выйдет длиннее {order['duration']} с")
    elif words < words_min * 0.7:
        warnings.append(f"{words} слов — ролик выйдет короче {order['duration']} с")
    return {"title": str(script.get("title") or "Ролик")[:80], "characters": characters, "lines": lines,
            "words": words, "warnings": warnings}


# ── Раскадровка ──────────────────────────────────────────────────────────────

_SHOTS_PROMPT = """Раскадровка рекламного/сюжетного ролика, кадр {aspect}.
Стиль картинки: {style}
Бриф: {brief}
Персонажи в кадре:
{cast}
{files}
Реплики (номер. РОЛЬ: текст):
{lines}

Сделай {n} кадров по порядку. Каждый кадр покрывает подряд идущие реплики (first_line..last_line);
все реплики покрыты, без пропусков и пересечений, первый кадр начинается с реплики 0.
- Сценка: в кадре тот, кто говорит (средний или крупный план), иногда общий план или выразительная деталь.
- Голос за кадром ({narrator}): кадр показывает то, о чём говорится; диктора в кадре нет.
- Если в кадре нужен товар, место или человек с фото клиента — укажи "asset": имя файла.
- В кадре нет текста, надписей, логотипов (кроме товара клиента).
JSON: {{"shots": [{{"first_line": 0, "last_line": 0, "characters": ["РОЛЬ"], "asset": null,
  "prompt_en": "what we see: shot size, who/what, action, setting, light",
  "motion_en": "subtle natural motion for a 5-second clip",
  "face_speaks": "РОЛЬ, если её лицо крупно и она говорит в этом кадре, иначе null"}}]}}"""


def _shot_count(order: dict, n_lines: int) -> int:
    return max(1, min(n_lines, max(3, round(order["duration"] / 3.5))))


def plan_shots(order: dict, script: dict) -> list[dict]:
    lines = script["lines"]
    visible = [c for c in script["characters"] if c["role"] != NARRATOR]
    cast = "\n".join(f"- {c['role']}: {c['visual_en'] or 'see client photo'}" for c in visible) or "- (никого)"
    n = _shot_count(order, len(lines))
    raw = _llm([{"role": "user", "content": _SHOTS_PROMPT.format(
        aspect=order["aspect"], style=style_text(order), brief=order["brief"][:1500], cast=cast,
        files=_files_text(order), n=n, narrator=NARRATOR,
        lines="\n".join(f"{i}. {l['role']}: {l['text']}" for i, l in enumerate(lines)))}],
        json_mode=True, max_tokens=4000, caller="order_shots")
    return fix_shots(_json(raw).get("shots") or [], script, order)


def fix_shots(shots: list[dict], script: dict, order: dict) -> list[dict]:
    """Кадры покрывают все реплики по порядку; персонажи и файлы — только существующие."""
    n_lines = len(script["lines"])
    roles = {c["role"] for c in script["characters"] if c["role"] != NARRATOR}
    clean, covered = [], 0
    for s in shots:
        try:
            first, last = int(s.get("first_line", covered)), int(s.get("last_line", covered))
        except (TypeError, ValueError):
            continue
        first, last = covered, max(min(last, n_lines - 1), covered)
        if first >= n_lines:
            break
        chars = [r for r in (str(x).upper() for x in s.get("characters") or []) if r in roles]
        face = str(s.get("face_speaks") or "").upper()
        clean.append({"first_line": first, "last_line": last, "characters": chars[:3],
                      "asset": s.get("asset") if s.get("asset") in order.get("files", []) else None,
                      "prompt_en": str(s.get("prompt_en") or "").strip() or script["lines"][first]["text"],
                      "motion_en": str(s.get("motion_en") or "slow subtle camera push-in").strip(),
                      "face_speaks": face if face in chars else None})
        covered = last + 1
    if not clean:
        clean = [{"first_line": 0, "last_line": n_lines - 1, "characters": sorted(roles)[:2], "asset": None,
                  "prompt_en": order["brief"][:300], "motion_en": "slow push-in", "face_speaks": None}]
    clean[-1]["last_line"] = n_lines - 1
    return clean


def assignment_for(shots: list[dict], n_lines: int) -> list[dict]:
    out = []
    for i in range(n_lines):
        idx = next((k for k, s in enumerate(shots) if s["first_line"] <= i <= s["last_line"]), len(shots) - 1)
        out.append({"shot": idx, "clip": True})
    return out


def lipsync_lines(shots: list[dict], lines: list[dict], dialogue_lines: list[dict]) -> list[int]:
    """Реплики, где говорящий крупно в своём кадре: первая, самая длинная и последняя из таких."""
    cands = [i for i, line in enumerate(lines)
             for s in shots if s["first_line"] <= i <= s["last_line"] and s.get("face_speaks") == line["role"]]
    if not cands:
        return []
    longest = max(cands, key=lambda i: dialogue_lines[i]["end"] - dialogue_lines[i]["start"])
    return sorted(dict.fromkeys([cands[0], longest, cands[-1]]))[:LIPSYNC_MAX]


# ── Сборка ───────────────────────────────────────────────────────────────────

def _voices(script: dict) -> dict:
    return {c["role"]: {"gemini": c["voice"], "edge": EDGE_VOICE[c["gender"]]} for c in script["characters"]}


def _voice(order: dict, script: dict, out_dir: Path) -> dict:
    from agents import dialogue_voice as dv

    lines, voices = script["lines"], _voices(script)
    direction = ("Озвучь ролик живо и естественно, как актёры в хорошей рекламе или кино: эмоции настоящие, "
                 f"темп разговорный, паузы короткие, весь ролик — около {order['duration']} секунд.")
    roles = {l["role"] for l in lines}
    try:
        if len(roles) <= 2:
            audio, timed = dv._render_dialogue(lines, voices, out_dir, direction)
            engine = "gemini-dialogue"
        else:
            audio, timed = dv._render_lines(lines, voices, "gemini", out_dir)
            engine = "gemini"
    except Exception as exc:
        logger.warning("Order: Gemini TTS не справился (%s) — edge-tts", str(exc)[:200])
        audio, timed = dv._render_lines(lines, voices, "edge", out_dir)
        engine = "edge"
    path = out_dir / "voice.wav"
    dv._write_wav(audio, path)
    return {"audio_path": str(path), "lines": timed, "engine": engine,
            "duration": round(len(audio) / dv.SAMPLE_RATE, 3)}


def _image_input(prompt: str, refs: list[str], aspect: str) -> dict:
    return {"prompt": prompt, "image_input": refs, "aspect_ratio": aspect, "resolution": "1K", "output_format": "jpg"}


def build(order: dict) -> dict:
    from agents import lipsync_agent
    from agents.scene_agent import IMAGE_MODEL, _video_request
    from agents.series_assembly import assemble
    from agents.subtitle_agent import write_dialogue_ass
    from utils import kie_client

    script = order["script"]
    if not script or not script.get("lines"):
        raise ValueError("сначала сценарий")
    out_dir = order_dir(order["id"]) / "build"
    out_dir.mkdir(parents=True, exist_ok=True)
    files_dir = order_dir(order["id"]) / "files"
    size, aspect, style = SIZES[order["aspect"]], order["aspect"], style_text(order)
    upload_dir = f"content-factory/orders/{order['id']}"
    credits = 0.0
    order["progress"] = []

    # 1. Голос — первым: бесплатный TTS упирается в квоту, и тогда до платных кадров дело не доходит
    _stage(order, "voice", "running")
    dialogue = _voice(order, script, out_dir)
    _stage(order, "voice", "done", f"{dialogue['duration']:.0f} с, {dialogue['engine']}")

    # 2. Герои: фото клиента или портрет по описанию — референс для всех кадров
    _stage(order, "cast", "running")
    shots = plan_shots(order, script)
    in_frame = {r for s in shots for r in s["characters"]}
    cast = [c for c in script["characters"] if c["role"] in in_frame]

    def portrait(k_c) -> tuple[str, str, float]:
        k, c = k_c
        if c.get("asset"):
            return c["role"], kie_client.upload_file(files_dir / c["asset"], upload_dir), 0.0
        prompt = (f"{style} Character reference portrait: {c['visual_en'] or c['role']}. Medium close-up, "
                  "looking slightly off camera, neutral background, no text.")
        res = kie_client.run_task(IMAGE_MODEL, _image_input(prompt, [], aspect), 600)
        kie_client.download(res["urls"][0], out_dir / f"cast_{k}.jpg")
        return c["role"], res["urls"][0], res["credits"] or 0

    with ThreadPoolExecutor(max_workers=4) as ex:
        refs = list(ex.map(portrait, enumerate(cast)))
    ref_url = {role: url for role, url, _ in refs}
    credits += sum(c for _, _, c in refs)
    asset_url = {f: kie_client.upload_file(files_dir / f, upload_dir) for f in {s["asset"] for s in shots if s["asset"]}}
    _stage(order, "cast", "done", f"героев: {len(cast)}")
    _stage(order, "shots", "done", f"кадров: {len(shots)}")

    # 3. Кадры
    _stage(order, "images", "running")
    vis = {c["role"]: c for c in script["characters"]}

    def image(i_shot) -> float:
        i, shot = i_shot
        refs_, who = [], []
        for role in shot["characters"]:
            refs_.append(ref_url[role])
            who.append(f"{role.title()} (reference image {len(refs_)}): {vis[role]['visual_en'] or 'as in the photo'}")
        prompt = style + " "
        if who:
            prompt += "Characters — keep each face, hairstyle and clothing exactly as in its reference image: " + \
                "; ".join(who) + ". "
        if shot["asset"]:
            refs_.append(asset_url[shot["asset"]])
            prompt += f"The product/place from reference image {len(refs_)} must look exactly as in it. "
        prompt += f"Shot: {shot['prompt_en']}. No captions, no subtitles, no watermarks."
        res = kie_client.run_task(IMAGE_MODEL, _image_input(prompt, refs_, aspect), 900)
        shot["image_url"] = res["urls"][0]
        shot["image_path"] = str(kie_client.download(shot["image_url"], out_dir / f"shot_{i:02d}.jpg"))
        return res["credits"] or 0

    with ThreadPoolExecutor(max_workers=8) as ex:
        credits += sum(ex.map(image, enumerate(shots)))
    _stage(order, "images", "done", f"{len(shots)} кадров")

    # 4. Анимация (Wan) — самые длинные кадры без говорящего лица; лица оживит липсинк
    assignment = assignment_for(shots, len(script["lines"]))
    span = {k: sum(dialogue["lines"][i]["end"] - dialogue["lines"][i]["start"]
                   for i, a in enumerate(assignment) if a["shot"] == k) for k in range(len(shots))}
    to_animate = sorted((k for k, s in enumerate(shots) if not (order.get("lipsync") and s.get("face_speaks"))),
                        key=lambda k: -span[k])[:ANIMATE.get(order.get("animate"), 0)]
    _stage(order, "motion", "running" if to_animate else "skipped")

    def animate(k: int) -> float:
        model, body = _video_request("wan", shots[k]["image_url"], shots[k]["motion_en"])
        try:
            res = kie_client.run_task(model, body, 1500, retries=2)
            shots[k]["video_path"] = str(kie_client.download(res["urls"][0], out_dir / f"shot_{k:02d}.mp4"))
            return res["credits"] or 0
        except Exception as exc:
            logger.warning("Order: анимация кадра %d не удалась: %s", k, str(exc)[:200])
            return 0

    if to_animate:
        with ThreadPoolExecutor(max_workers=4) as ex:
            credits += sum(ex.map(animate, to_animate))
        _stage(order, "motion", "done", f"оживлено {sum(1 for k in to_animate if shots[k].get('video_path'))}")

    # 5. Липсинк ключевых реплик
    targets = lipsync_lines(shots, script["lines"], dialogue["lines"]) if order.get("lipsync") else []
    _stage(order, "lipsync", "running" if targets else "skipped")

    def lip(i: int) -> tuple[int, str | None, float]:
        line = dialogue["lines"][i]
        try:
            audio = lipsync_agent._cut_audio(dialogue["audio_path"], line["start"], line["end"], out_dir / f"lip_{i:02d}.mp3")
            emo = f" Mood: {script['lines'][i]['emotion']}." if script["lines"][i].get("emotion") else ""
            res = kie_client.run_task(lipsync_agent.MODEL, {
                "image_url": shots[assignment[i]["shot"]]["image_url"],
                "audio_url": kie_client.upload_file(audio, upload_dir),
                "prompt": f"The person speaks naturally, subtle head movement and blinking, static camera, "
                          f"keep the face and clothing exactly as in the image.{emo}"}, 1500, retries=2)
            return i, str(kie_client.download(res["urls"][0], out_dir / f"lip_{i:02d}.mp4")), res["credits"] or 0
        except Exception as exc:
            logger.warning("Order: липсинк реплики %d не удался: %s", i, str(exc)[:200])
            return i, None, 0

    if targets:
        with ThreadPoolExecutor(max_workers=len(targets)) as ex:
            res_lip = list(ex.map(lip, targets))
        credits += sum(c for _, _, c in res_lip)
        clips = {i: p for i, p, _ in res_lip if p}
        assignment = lipsync_agent.apply(assignment, shots, clips)
        _stage(order, "lipsync", "done", f"{len(clips)} из {len(targets)}")

    # 6. Субтитры, музыка, монтаж
    _stage(order, "assembly", "running")
    sub_path = None
    if order.get("subtitles", True):
        sub_path = str(out_dir / "subtitles.ass")
        vertical = size[1] > size[0]
        colors = {c["role"]: SUB_COLORS[k % len(SUB_COLORS)] for k, c in enumerate(script["characters"])}
        write_dialogue_ass(dialogue["lines"], sub_path, colors, play_res=size,
                           fontsize=92 if vertical else 64, margin_v=620 if vertical else 80,
                           max_chars=18 if vertical else 30)
    music_path = None
    if order.get("music", "auto") != "none":
        try:
            from agents.music_agent import get_music
            mood = None if order.get("music", "auto") == "auto" else order["music"]
            music_path = get_music(" ".join(l["text"] for l in script["lines"]), mood).get("music_path")
        except Exception as exc:
            logger.warning("Order: музыка не подобрана: %s", exc)
    logo = str(files_dir / order["logo"]) if order.get("logo") and (files_dir / order["logo"]).is_file() else None
    video_path = order_dir(order["id"]) / f"{order['id']}.mp4"
    assembled = assemble(shots, dialogue, sub_path, str(video_path), assignment, music_path, size=size, logo_path=logo)
    _stage(order, "assembly", "done", f"{assembled['duration']:.0f} с")

    # 7. Проверка — не блокирует заказ, показывает замечания автору
    _stage(order, "qa", "running")
    qa = {"passed": None, "problems": []}
    try:
        from agents.series_qa import check
        qa = check(str(video_path), dialogue, sub_path or "", assembled.get("timeline"), shots)
    except Exception as exc:
        logger.warning("Order: проверка не выполнена: %s", exc)
    _stage(order, "qa", "done" if qa.get("passed") is not False else "failed",
           "; ".join(p["text"] for p in qa.get("problems", []))[:300])

    return {"video": video_path.name, "duration": assembled["duration"], "resolution": assembled["resolution"],
            "cost_usd": round(credits / kie_client.CREDITS_PER_USD, 2), "credits": credits,
            "qa": {"passed": qa.get("passed"), "problems": qa.get("problems", [])},
            "voice_engine": dialogue["engine"], "shots": len(shots)}


def estimate_usd(order: dict) -> float:
    """Грубая оценка до сборки: кадры, портреты, анимация, липсинк."""
    n_shots = max(3, round(order["duration"] / 3.5))
    images = (n_shots + 2) * 8
    motion = ANIMATE.get(order.get("animate"), 0) * 20
    lip = (LIPSYNC_MAX * 4 * 8) if order.get("lipsync") and order.get("mode") != "voiceover" else 0
    return round((images + motion + lip) / 200, 2)


def file_size_ok(n_bytes: int) -> bool:
    return 0 < n_bytes <= 15 * 1024 * 1024

