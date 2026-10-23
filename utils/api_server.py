"""
Панель управления сериалами — Flask-бэкенд (admin_panel.html).

Запуск:  py utils/api_server.py   (или start.bat) → http://localhost:5000
Хост:    127.0.0.1 (API_HOST). Для доступа из сети — API_HOST=0.0.0.0 и обязательно ADMIN_TOKEN.

Защита: панель запускает генерацию (платный KIE) и публикацию, поэтому
  - запросы принимаются только с Host = localhost/127.0.0.1 (+ API_ALLOWED_HOSTS): защита от DNS rebinding;
  - POST/PUT требуют Content-Type: application/json и Origin того же хоста: защита от CSRF;
  - если задан ADMIN_TOKEN — нужен заголовок X-Admin-Token или ?token=.

Эндпоинты:
  GET  /api/overview                    сводка: сериалы, очередь, расходы, баланс KIE, планировщик, задачи
  GET  /api/series                      список сериалов (config/series/*.json)
  POST /api/series                      создать сериал по шаблону {id, name, logline, heroine{...}, ...}
  GET  /api/series/<id>                 конфиг сериала + портреты героини + сохранённые сценарии
  PUT  /api/series/<id>                 изменить настройки (разрешённые поля)
  GET  /api/series/<id>/episodes        серии: в очереди, вышедшие, снятые
  POST /api/series/<id>/episodes/<row>  {action: reject|restore}
  GET  /api/series/<id>/builds          текущая сборка по этапам + последние сборки
  GET  /api/series/<id>/heroine/<file>  портрет героини (референс или кандидат)
  POST /api/series/<id>/heroine/pick    {n} — выбрать кандидата референсом
  GET  /api/video/<row>                 ролик эпизода (только из output/)
  GET  /api/jobs | POST /api/jobs       фоновые задачи: prepare, script, resume, publish_next, lab, analytics,
                                        heroine, scheduler; GET /api/jobs/<id>/log; POST /api/jobs/<id>/stop
  GET  /api/analytics?series=&days=     просмотры по сериям (таблица performance) + расходы на эпизод
  GET  /api/lab?series= | GET /api/lab/<stamp>   результаты лаборатории сценариев
  GET  /api/scripts?series= | POST /api/scripts  сценарии для производства (config/series/scripts/)
  GET  /api/logs?lines=                 хвост логов (секреты маскируются)
"""

import hmac
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

# UTF-8 stdout/stderr на Windows (избегаем 'charmap' UnicodeEncodeError)
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402
from flask import Flask, jsonify, request, send_file  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=ROOT_DIR / ".env")
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
from utils.log_redact import redact  # noqa: E402

app = Flask(__name__)

API_HOST = os.getenv("API_HOST", "127.0.0.1")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_ALLOWED_HOSTS = _LOOPBACK_HOSTS | {
    h.strip().lower() for h in os.getenv("API_ALLOWED_HOSTS", "").split(",") if h.strip()
}
DB_PATH = os.getenv("DB_PATH", "pipeline.db")
LOGS_DIR = ROOT_DIR / "logs"
SERIES_DIR = ROOT_DIR / "config" / "series"
SCRIPTS_DIR = SERIES_DIR / "scripts"
LAB_DIR = ROOT_DIR / "output" / "script_lab"
JOBS_DIR = LOGS_DIR / "jobs"
SCHEDULER_HEARTBEAT = LOGS_DIR / "scheduler.heartbeat"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
_PLATFORMS = {"tiktok", "instagram", "youtube"}


def _hostname(netloc: str) -> str:
    """'127.0.0.1:5000' → '127.0.0.1', '[::1]:5000' → '::1'."""
    return (urlsplit(f"//{netloc}").hostname or "").lower()


@app.before_request
def _guard():
    if _hostname(request.host) not in _ALLOWED_HOSTS:
        return jsonify({"error": "host not allowed"}), 403
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and _hostname(urlsplit(origin).netloc) != _hostname(request.host):
            return jsonify({"error": "cross-origin request rejected"}), 403
        if not request.is_json:
            return jsonify({"error": "Content-Type must be application/json"}), 415
    if ADMIN_TOKEN and request.path.startswith("/api/"):
        supplied = request.headers.get("X-Admin-Token") or request.args.get("token", "")
        if not hmac.compare_digest(supplied, ADMIN_TOKEN):
            return jsonify({"error": "unauthorized"}), 401
    return None


def _int_arg(name: str, default: int, lo: int = 1, hi: int = 1000) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.Error:
        return []  # таблицы ещё нет — этот раздел пока пуст


def _ts(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except (TypeError, ValueError):
        return None


# ── Сериалы ───────────────────────────────────────────────────────────────────

def _series_path(series_id: str) -> Path:
    if not _ID_RE.match(series_id or ""):
        raise ValueError("id сериала: латиница, цифры, - и _, 2-41 символ")
    return SERIES_DIR / f"{series_id}.json"


def _load_series(series_id: str) -> dict | None:
    path = _series_path(series_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_series(series: dict) -> None:
    _series_path(series["id"]).write_text(json.dumps(series, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _all_series() -> list[dict]:
    out = []
    for path in sorted(SERIES_DIR.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _schedule(series: dict) -> dict:
    return {"enabled": False, "time": "19:00", "platform": "youtube", **(series.get("schedule") or {})}


def _episode_counts(conn: sqlite3.Connection, series_id: str) -> dict:
    counts = {"ready": 0, "published": 0, "rejected": 0}
    for r in _rows(conn, "SELECT status, COUNT(*) AS n FROM anthology_episodes WHERE series_id=? GROUP BY status",
                   (series_id,)):
        counts[r["status"]] = r["n"]
    return counts


@app.get("/")
@app.get("/admin_panel.html")
def serve_admin():
    return send_file(ROOT_DIR / "admin_panel.html")


@app.get("/api/series")
def api_series_list():
    conn = _db()
    try:
        out = [{"id": s["id"], "name": s.get("name", s["id"]), "logline": s.get("logline", ""),
                "heroine": s.get("heroine", {}).get("name", ""), "schedule": _schedule(s),
                "has_reference": (ROOT_DIR / s.get("heroine", {}).get("reference", "-")).exists(),
                "episodes": _episode_counts(conn, s["id"])} for s in _all_series()]
    finally:
        conn.close()
    return jsonify(out)


# Поля, которые панель может менять; остальное — через файл конфига
_EDITABLE = {
    "name": str, "logline": str, "format": str, "season_arc": str, "location_en": str, "style_en": str,
    "disclaimer": str, "tts_direction": str, "trend_niche": str,
    "words_min": int, "words_max": int, "lines_min": int, "lines_max": int,
    "animated_key": int, "animated_simple": int, "lipsync_lines": str,
    "hashtags": list, "also_publish": list, "manual_publish": list,
}
_EDITABLE_HEROINE = {"name": str, "role": str, "profession": str, "setting": str, "bio": str, "voice_ru": str,
                     "visual_en": str, "intro": str, "gemini_voice": str}


def _apply_fields(series: dict, data: dict) -> list[str]:
    errors = []
    for key, typ in _EDITABLE.items():
        if key in data:
            value = data[key]
            if typ is list and isinstance(value, str):
                value = [v.strip().lstrip("#") for v in re.split(r"[,\s]+", value) if v.strip()]
            try:
                series[key] = typ(value) if typ is not list else list(value)
            except (TypeError, ValueError):
                errors.append(key)
    heroine = data.get("heroine") or {}
    for key, typ in _EDITABLE_HEROINE.items():
        if key in heroine:
            series.setdefault("heroine", {})[key] = str(heroine[key]).strip()
    if "role" in heroine:
        series["heroine"]["role"] = series["heroine"]["role"].upper()
    if "schedule" in data:
        sch = data["schedule"] or {}
        time_ok = re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(sch.get("time", "19:00")))
        if not time_ok or sch.get("platform", "youtube") not in _PLATFORMS:
            errors.append("schedule")
        else:
            series["schedule"] = {"enabled": bool(sch.get("enabled")), "time": sch.get("time", "19:00"),
                                  "platform": sch.get("platform", "youtube")}
    if series.get("words_min", 0) > series.get("words_max", 0):
        errors.append("words_min > words_max")
    return errors


@app.post("/api/series")
def api_series_create():
    """Новый сериал по шаблону: технические настройки — от первого сериала, история и героиня — свои."""
    data = _body()
    try:
        path = _series_path(str(data.get("id", "")).strip().lower())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if path.exists():
        return jsonify({"error": "сериал с таким id уже есть"}), 409
    heroine = data.get("heroine") or {}
    missing = [k for k in ("name", "role", "profession", "setting", "visual_en") if not str(heroine.get(k, "")).strip()]
    if not str(data.get("name", "")).strip() or missing:
        return jsonify({"error": f"заполните: {', '.join(['name'] * (not data.get('name')) + missing)}"}), 400

    template = _load_series("kabinet7") or {}
    series = {k: v for k, v in template.items() if k not in ("id", "name", "logline", "heroine", "season_arc",
                                                            "hashtags", "schedule")}
    series_id = path.stem
    series.update({"id": series_id, "name": "", "logline": "", "season_arc": "", "hashtags": [],
                   "heroine": {**{k: v for k, v in (template.get("heroine") or {}).items()
                                  if k in ("gemini_voice", "edge_voice")},
                               "reference": f"assets/series/{series_id}/heroine_ref.jpg"},
                   "schedule": {"enabled": False, "time": "19:00", "platform": "youtube"}})
    errors = _apply_fields(series, data)
    if errors:
        return jsonify({"error": f"неверные поля: {', '.join(errors)}"}), 400
    _save_series(series)
    return jsonify({"ok": True, "id": series_id}), 201


def _heroine_images(series: dict) -> dict:
    ref = ROOT_DIR / series.get("heroine", {}).get("reference", "-")
    cand_dir = ROOT_DIR / "assets" / "series" / series["id"] / "candidates"
    return {"reference": ref.name if ref.exists() else None,
            "candidates": sorted(p.name for p in cand_dir.glob("*.jpg")) if cand_dir.exists() else []}


@app.get("/api/series/<series_id>")
def api_series_get(series_id: str):
    try:
        series = _load_series(series_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not series:
        return jsonify({"error": "сериал не найден"}), 404
    return jsonify({"config": series, "schedule": _schedule(series), "images": _heroine_images(series),
                    "scripts": _scripts(series_id)})


@app.put("/api/series/<series_id>")
def api_series_update(series_id: str):
    try:
        series = _load_series(series_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not series:
        return jsonify({"error": "сериал не найден"}), 404
    errors = _apply_fields(series, _body())
    if errors:
        return jsonify({"error": f"неверные поля: {', '.join(errors)}"}), 400
    _save_series(series)
    return jsonify({"ok": True, "config": series, "schedule": _schedule(series)})


@app.get("/api/series/<series_id>/heroine/<name>")
def api_heroine_image(series_id: str, name: str):
    series = _load_series(series_id) if _ID_RE.match(series_id) else None
    if not series or not re.match(r"^[\w.-]+\.jpg$", name):
        return jsonify({"error": "not found"}), 404
    ref = ROOT_DIR / series.get("heroine", {}).get("reference", "-")
    path = ref if ref.name == name else ROOT_DIR / "assets" / "series" / series_id / "candidates" / name
    return send_file(path, mimetype="image/jpeg") if path.is_file() else (jsonify({"error": "not found"}), 404)


@app.post("/api/series/<series_id>/heroine/pick")
def api_heroine_pick(series_id: str):
    series = _load_series(series_id) if _ID_RE.match(series_id) else None
    name = str(_body().get("file", ""))
    src = ROOT_DIR / "assets" / "series" / series_id / "candidates" / name
    if not series or not re.match(r"^[\w.-]+\.jpg$", name) or not src.is_file():
        return jsonify({"error": "кандидат не найден"}), 404
    dest = ROOT_DIR / series["heroine"]["reference"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return jsonify({"ok": True, "reference": dest.name})


# ── Эпизоды и сборки ──────────────────────────────────────────────────────────

def _video_path(row_id: int, conn: sqlite3.Connection) -> Path | None:
    """Путь к ролику эпизода — только внутри output/ проекта и только если файл существует."""
    row = _rows(conn, "SELECT video_path FROM anthology_episodes WHERE id=?", (row_id,))
    if not row or not row[0]["video_path"]:
        return None
    path = Path(row[0]["video_path"])
    path = (path if path.is_absolute() else ROOT_DIR / path).resolve()
    output_dir = (ROOT_DIR / "output").resolve()
    if output_dir not in path.parents or not path.is_file():
        return None
    return path


@app.get("/api/series/<series_id>/episodes")
def api_episodes(series_id: str):
    conn = _db()
    try:
        rows = _rows(conn, "SELECT id, episode_no, title, publish_title, status, created_at, published_at, post_id, "
                           "run_id FROM anthology_episodes WHERE series_id=? ORDER BY episode_no DESC, id DESC",
                     (series_id,))
        for row in rows:
            row["has_video"] = _video_path(row["id"], conn) is not None
    finally:
        conn.close()
    return jsonify(rows)


@app.post("/api/series/<series_id>/episodes/<int:row_id>")
def api_episode_action(series_id: str, row_id: int):
    """Снять эпизод с публикации (reject) или вернуть снятый в очередь (restore)."""
    action = _body().get("action")
    if action not in ("reject", "restore"):
        return jsonify({"error": "action: reject или restore"}), 400
    conn = _db()
    try:
        row = _rows(conn, "SELECT series_id, episode_no, status FROM anthology_episodes WHERE id=?", (row_id,))
        if not row or row[0]["series_id"] != series_id:
            return jsonify({"error": "эпизод не найден"}), 404
        row = row[0]
        want = "ready" if action == "reject" else "rejected"
        if row["status"] != want:
            return jsonify({"error": f"эпизод в статусе {row['status']}"}), 409
        if action == "restore" and _rows(
                conn, "SELECT 1 FROM anthology_episodes WHERE series_id=? AND episode_no=? AND status!='rejected' "
                      "AND id!=?", (series_id, row["episode_no"], row_id)):
            return jsonify({"error": f"номер {row['episode_no']} уже занят другим эпизодом"}), 409
    finally:
        conn.close()
    from agents.anthology_agent import set_episode_status
    set_episode_status(row_id, "rejected" if action == "reject" else "ready", DB_PATH)
    return jsonify({"ok": True})


@app.get("/api/video/<int:row_id>")
def api_video(row_id: int):
    conn = _db()
    try:
        path = _video_path(row_id, conn)
    finally:
        conn.close()
    if path is None:
        return jsonify({"error": "video not found"}), 404
    return send_file(path, mimetype="video/mp4", conditional=True)


SERIES_STAGES = [
    ("anthology_cases", "Кейсы"), ("topic_filter", "Фильтр тем"), ("anthology_episode", "Сценарий"),
    ("anthology_shots", "Раскадровка"), ("dialogue_voice", "Озвучка"), ("scene_agent", "Кадры"),
    ("series_assembly", "Монтаж"), ("series_qa", "Проверка"), ("quality_scorer", "Оценка"), ("qa_agent", "QA"),
    ("publisher_agent", "Публикация"),
]
_STALL_SECONDS = 20 * 60  # без новых записей дольше — процесс, скорее всего, умер


def _run_view(conn: sqlite3.Connection, run_id: str) -> dict:
    """Этапы запуска сериала по pipeline_state: статус, длительность, ошибка."""
    rows = _rows(conn, "SELECT step, status, payload, error, created_at FROM pipeline_state WHERE run_id=? "
                       "ORDER BY id", (run_id,))
    last = rows[-1] if rows else None
    finished = bool(last and last["step"] == "pipeline")
    final = {}
    if finished and last.get("payload"):
        try:
            final = json.loads(last["payload"])
        except ValueError:
            pass
    last_activity = _ts(last["created_at"]) if last else None
    stalled = not finished and last_activity is not None and time.time() - last_activity > _STALL_SECONDS
    queued = finished and final.get("status") == "queued"
    # Попытка = строки после предыдущего итога «pipeline»: у доделанного запуска время считается заново
    ends = [i for i, r in enumerate(rows) if r["step"] == "pipeline"]
    if finished:
        ends = ends[:-1]
    start_at = ends[-1] + 1 if ends else 0

    stages = []
    for key, label in SERIES_STAGES:
        now_rows = [r for r in rows[start_at:] if r["step"] == key]
        before = [r for r in rows[:start_at] if r["step"] == key]
        if not now_rows:
            state = ("done" if before and before[-1]["status"] == "success"
                     else "skipped" if key == "publisher_agent" and queued else "pending")
            stages.append({"key": key, "label": label, "status": state, "reused": state == "done"})
            continue
        end = now_rows[-1]
        start = next((r for r in reversed(now_rows) if r["status"] == "running"), now_rows[0])
        status = end["status"]
        if status == "success":
            state = "done"
        elif status == "failed" or (finished or stalled) and (status == "running" or status.startswith("failed")):
            state = "failed"
        else:
            state = "running"
        t0 = _ts(start["created_at"])
        t1 = time.time() if state == "running" else _ts(end["created_at"])
        stages.append({"key": key, "label": label, "status": state,
                       "seconds": round(t1 - t0) if t0 and t1 else None,
                       "retries": sum(1 for r in now_rows if r["status"].startswith("failed_attempt")),
                       "error": redact(end.get("error") or "")[:300] if state == "failed" else ""})

    title = ""
    for r in reversed(rows):
        if r["step"] == "anthology_episode" and r["status"] == "success" and r.get("payload"):
            try:
                title = json.loads(r["payload"]).get("title", "")
            except (ValueError, AttributeError):
                pass
            if title:
                break
    note = _rows(conn, "SELECT message FROM agent_messages WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,))
    status = (("queued" if queued else "completed" if last["status"] == "completed" else "failed") if finished
              else "stalled" if stalled else "running")
    first = rows[start_at] if start_at < len(rows) else (rows[0] if rows else None)
    started = _ts(first["created_at"]) if first else None
    return {"run_id": run_id, "title": title, "status": status, "episode": final.get("episode"),
            "cost_usd": final.get("cost_usd"), "resumed": start_at > 0,
            "started_at": first["created_at"] if first else None,
            "seconds": round((last_activity if finished else time.time()) - started) if started else None,
            "error": redact(last.get("error") or "")[:400] if finished and status == "failed" else "",
            "note": note[0]["message"][:160] if note and status == "running" else "", "stages": stages}


@app.get("/api/series/<series_id>/builds")
def api_builds(series_id: str):
    conn = _db()
    try:
        run_ids = [r["run_id"] for r in _rows(
            conn, "SELECT run_id FROM pipeline_state WHERE run_id LIKE ? AND run_id NOT LIKE ? "
                  "GROUP BY run_id ORDER BY MAX(id) DESC LIMIT 10",
            (f"%_series_{series_id}", "%_publish"))]
        runs = [_run_view(conn, rid) for rid in run_ids]
    finally:
        conn.close()
    return jsonify({"current": runs[0] if runs else None,
                    "runs": [{k: r[k] for k in ("run_id", "title", "status", "episode", "started_at", "seconds",
                                                "cost_usd")} for r in runs]})


# ── Фоновые задачи ────────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}
_procs: dict[str, subprocess.Popen] = {}
_jobs_lock = threading.Lock()
_JOB_TITLES = {"prepare": "Собрать эпизоды", "script": "Собрать по сценарию", "resume": "Доделать сборку",
               "publish_next": "Опубликовать следующий", "lab": "Лаборатория сценариев",
               "analytics": "Обновить аналитику", "heroine": "Портреты героини", "scheduler": "Планировщик",
               "order_script": "Заказ: сценарий", "order_build": "Заказ: видео",
               "pitches": "Завязки серий"}


def _job_command(kind: str, series: dict | None, data: dict) -> list[str]:
    py = [sys.executable, "-X", "utf8"]
    if kind == "analytics":
        return py + [str(ROOT_DIR / "agents" / "analytics_agent.py"), "--days", str(int(data.get("days", 7)))]
    if kind == "scheduler":
        return py + [str(ROOT_DIR / "scheduler.py")]
    if kind in ("order_script", "order_build"):
        from agents import order_agent
        order = order_agent.load(str(data.get("order", "")))  # ValueError/OSError — нет такого заказа
        if kind == "order_build" and not (order.get("script") or {}).get("lines"):
            raise ValueError("сначала сценарий")
        return py + [str(ROOT_DIR / "scripts" / "order_run.py"), "--order", order["id"],
                     "--step", "script" if kind == "order_script" else "build"]
    if series is None:
        raise ValueError("нужен сериал")
    sid = series["id"]
    if kind == "heroine":
        return py + [str(ROOT_DIR / "scripts" / "series_setup.py"), sid]
    if kind == "pitches":
        n = max(3, min(int(data.get("n", 12)), 20))
        idea = str(data.get("idea") or "").strip()[:2000]
        return py + [str(ROOT_DIR / "scripts" / "script_lab.py"), "--series", sid, "--pitches", "--n", str(n)] + \
            (["--idea", idea] if idea else [])
    if kind == "lab" and data.get("pitch_ids"):
        ids = [int(i) for i in data["pitch_ids"] if str(i).isdigit()][:6]
        if not ids:
            raise ValueError("отметьте завязки")
        return py + [str(ROOT_DIR / "scripts" / "script_lab.py"), "--series", sid, "--same",
                     "--from-pitches", ",".join(map(str, ids))]
    if kind == "lab":
        n = max(1, min(int(data.get("n", 4)), 8))
        idea = str(data.get("idea") or "").strip()
        if len(idea) > 2000:
            raise ValueError("идея длиннее 2000 символов")
        return py + [str(ROOT_DIR / "scripts" / "script_lab.py"), "--series", sid, "--n", str(n)] + \
            (["--same"] if data.get("same", True) else []) + (["--idea", idea] if idea else [])
    platform = data.get("platform") or _schedule(series)["platform"]
    if platform not in _PLATFORMS:
        raise ValueError("неизвестная площадка")
    base = py + [str(ROOT_DIR / "main.py"), "--format", "series", "--series", sid,
                 "--niche", series.get("trend_niche", "психология"), "--platform", platform]
    if kind == "prepare":
        return base + ["--prepare", str(max(1, min(int(data.get("n", 1)), 10)))]
    if kind == "publish_next":
        if data.get("confirm") is not True:
            raise ValueError("публикация требует подтверждения")
        return base + ["--publish-next"]
    if kind in ("script", "resume"):
        args = []
        if kind == "resume":
            if not re.match(r"^\d{8}_\d{6}_series_[\w-]+$", str(data.get("run_id", ""))):
                raise ValueError("неверный run_id")
            args += ["--resume", data["run_id"]]
        if data.get("file") or kind == "script":
            file = (SCRIPTS_DIR / Path(str(data.get("file", ""))).name)
            if not file.is_file():
                raise ValueError("сценарий не найден")
            args += ["--script", str(file)]
        return base + args
    raise ValueError(f"неизвестная задача: {kind}")


_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _spawn(kind: str, cmd: list[str], log) -> subprocess.Popen:
    """Процесс задачи. Планировщик отвязывается от панели: закрыли панель (или окно, в котором она
    запущена) — выпуск по расписанию продолжается (2026-10-22 планировщик погиб вместе с панелью)."""
    kwargs = dict(cwd=str(ROOT_DIR), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if os.name != "nt":
        return subprocess.Popen(cmd, start_new_session=(kind == "scheduler"), **kwargs)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    if kind != "scheduler":
        return subprocess.Popen(cmd, creationflags=flags, **kwargs)
    try:
        return subprocess.Popen(cmd, creationflags=flags | subprocess.DETACHED_PROCESS | _CREATE_BREAKAWAY_FROM_JOB,
                                **kwargs)
    except OSError:  # job-объект родителя не разрешает отвязку — хотя бы без консоли родителя
        return subprocess.Popen(cmd, creationflags=flags | subprocess.DETACHED_PROCESS, **kwargs)


def _watch(job_id: str) -> None:
    proc = _procs[job_id]
    code = proc.wait()
    with _jobs_lock:
        job = _jobs[job_id]
        job.update(status="done" if code == 0 else ("stopped" if job["status"] == "stopping" else "failed"),
                   returncode=code, ended_at=datetime.now(timezone.utc).isoformat())
        _save_jobs()


def _save_jobs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    recent = sorted(_jobs.values(), key=lambda j: j["started_at"])[-100:]
    (JOBS_DIR / "jobs.json").write_text(json.dumps(recent, ensure_ascii=False, indent=1), encoding="utf-8")


def _load_jobs() -> None:
    try:
        for job in json.loads((JOBS_DIR / "jobs.json").read_text(encoding="utf-8")):
            if job.get("status") in ("running", "stopping"):
                job["status"] = "lost"  # сервер перезапускался — процесс мог завершиться без нас
            _jobs[job["id"]] = job
    except (OSError, ValueError):
        pass


@app.get("/api/jobs")
def api_jobs():
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["started_at"], reverse=True)[:50]
    return jsonify(jobs)


@app.post("/api/jobs")
def api_job_start():
    data = _body()
    kind = data.get("type")
    if kind not in _JOB_TITLES:
        return jsonify({"error": "неизвестная задача"}), 400
    series = None
    if data.get("series"):
        try:
            series = _load_series(str(data["series"]))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not series:
            return jsonify({"error": "сериал не найден"}), 404
    try:
        cmd = _job_command(kind, series, data)
    except (ValueError, TypeError, OSError) as exc:
        return jsonify({"error": str(exc) if not isinstance(exc, OSError) else "заказ не найден"}), 400
    with _jobs_lock:
        busy = [j for j in _jobs.values() if j["status"] == "running" and j["type"] == kind
                and j.get("series") == data.get("series") and j["params"].get("order") == data.get("order")]
        if busy and kind != "lab":
            return jsonify({"error": "такая задача уже идёт"}), 409
        job_id = uuid.uuid4().hex[:10]
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = JOBS_DIR / f"{job_id}.log"
        log = open(log_path, "w", encoding="utf-8")
        proc = _spawn(kind, cmd, log)
        _procs[job_id] = proc
        _jobs[job_id] = {"id": job_id, "type": kind, "title": _JOB_TITLES[kind], "series": data.get("series"),
                         "params": {k: v for k, v in data.items() if k not in ("type", "series", "confirm")},
                         "status": "running", "pid": proc.pid, "log": log_path.name,
                         "started_at": datetime.now(timezone.utc).isoformat(), "ended_at": None}
        _save_jobs()
    threading.Thread(target=_watch, args=(job_id,), daemon=True).start()
    return jsonify(_jobs[job_id]), 201


@app.get("/api/jobs/<job_id>/log")
def api_job_log(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "задача не найдена"}), 404
    try:
        lines = (JOBS_DIR / job["log"]).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    n = _int_arg("lines", 200, hi=2000)
    return jsonify({"job": job, "lines": [redact(ln) for ln in lines[-n:]]})


@app.post("/api/jobs/<job_id>/stop")
def api_job_stop(job_id: str):
    proc = _procs.get(job_id)
    job = _jobs.get(job_id)
    if not proc or not job or proc.poll() is not None:
        return jsonify({"error": "задача не выполняется"}), 409
    with _jobs_lock:
        job["status"] = "stopping"
    proc.terminate()
    return jsonify({"ok": True})


# ── Аналитика ─────────────────────────────────────────────────────────────────

_kie_cache: dict = {"at": 0.0, "value": None}
_kie_lock = threading.Lock()


def _kie_balance() -> float | None:
    """Баланс KIE: запрос в сеть в фоне раз в минуту, панель сразу получает последнее известное значение."""
    if time.time() - _kie_cache["at"] > 60 and _kie_lock.acquire(blocking=False):
        _kie_cache["at"] = time.time()

        def refresh():
            try:
                from utils.kie_client import balance
                _kie_cache["value"] = round(float(balance()), 1)
            except Exception:
                pass
            finally:
                _kie_lock.release()

        threading.Thread(target=refresh, daemon=True).start()
    return _kie_cache["value"]


def _scheduler_alive() -> bool:
    try:
        return time.time() - SCHEDULER_HEARTBEAT.stat().st_mtime < 120
    except OSError:
        return False


@app.get("/api/analytics")
def api_analytics():
    series_id = request.args.get("series", "")
    days = _int_arg("days", 30, hi=365)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _db()
    try:
        episodes = _rows(conn, "SELECT id, episode_no, publish_title, title, post_id, run_id, published_at, created_at, "
                               "status FROM anthology_episodes WHERE series_id=? AND status!='rejected' "
                               "ORDER BY episode_no", (series_id,))
        perf = {r["post_id"]: r for r in _rows(conn, "SELECT post_id, platform, views, likes, comments, fetched_at "
                                                     "FROM performance")}
        costs = {r["run_id"]: r["usd"] for r in _rows(conn, "SELECT run_id, SUM(total_usd) AS usd FROM costs "
                                                             "GROUP BY run_id")}
        spend = _rows(conn, "SELECT SUM(total_usd) AS usd FROM costs WHERE run_id LIKE ? AND created_at >= ?",
                      (f"%_series_{series_id}%", since))
    finally:
        conn.close()
    rows, views_total, likes_total, comments_total = [], 0, 0, 0
    for e in episodes:
        p = perf.get(e["post_id"] or "") or {}
        views, likes, comments = p.get("views") or 0, p.get("likes") or 0, p.get("comments") or 0
        if e["status"] == "published":
            views_total, likes_total, comments_total = views_total + views, likes_total + likes, comments_total + comments
        rows.append({"episode_no": e["episode_no"], "title": e["publish_title"] or e["title"], "status": e["status"],
                     "published_at": e["published_at"], "views": views, "likes": likes, "comments": comments,
                     "platform": p.get("platform"), "fetched_at": p.get("fetched_at"),
                     "cost_usd": round(costs.get(e["run_id"], 0) or 0, 2)})
    published = [r for r in rows if r["status"] == "published"]
    return jsonify({
        "episodes": rows,
        "totals": {"published": len(published), "views": views_total, "likes": likes_total, "comments": comments_total,
                   "avg_views": round(views_total / len(published)) if published else 0,
                   "spend_usd": round((spend[0]["usd"] if spend else 0) or 0, 2), "days": days,
                   "cost_per_episode": round(sum(r["cost_usd"] for r in rows) / len(rows), 2) if rows else 0},
        "kie_balance": _kie_balance()})


@app.get("/api/overview")
def api_overview():
    conn = _db()
    try:
        series = [{"id": s["id"], "name": s.get("name", s["id"]), "schedule": _schedule(s),
                   "episodes": _episode_counts(conn, s["id"])} for s in _all_series()]
        week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        spend = _rows(conn, "SELECT SUM(total_usd) AS usd FROM costs WHERE run_id LIKE '%_series_%' AND created_at >= ?",
                      (week,))
        errors = _rows(conn, "SELECT step, error, created_at FROM error_log ORDER BY id DESC LIMIT 5")
    finally:
        conn.close()
    with _jobs_lock:
        running = [j for j in _jobs.values() if j["status"] == "running"]
    return jsonify({"series": series, "spend_week_usd": round((spend[0]["usd"] if spend else 0) or 0, 2),
                    "kie_balance": _kie_balance(), "scheduler_alive": _scheduler_alive(),
                    "running_jobs": running,
                    "errors": [{**e, "error": redact(e["error"] or "")[:200]} for e in errors]})


# ── Лаборатория и сценарии ────────────────────────────────────────────────────

@app.get("/api/pitches")
def api_pitches():
    from agents.anthology_agent import list_pitches
    series_id = request.args.get("series", "")
    if not _ID_RE.match(series_id):
        return jsonify({"error": "нужен сериал"}), 400
    return jsonify(list_pitches(series_id, DB_PATH, limit=_int_arg("limit", 60, hi=200)))


@app.post("/api/pitches/<int:pitch_id>")
def api_pitch_vote(pitch_id: int):
    """Отметка автора: 1 — нравится, -1 — нет, 0 — снять. Отметки учатся в следующих завязках."""
    from agents.anthology_agent import set_pitch
    vote = _body().get("vote")
    if vote not in (1, 0, -1):
        return jsonify({"error": "vote: 1, 0 или -1"}), 400
    if not set_pitch(pitch_id, DB_PATH, vote=vote):
        return jsonify({"error": "завязка не найдена"}), 404
    return jsonify({"ok": True})


@app.get("/api/lab")
def api_lab_list():
    out = []
    for path in sorted(LAB_DIR.glob("*.json"), reverse=True) if LAB_DIR.exists() else []:
        if path.stem.endswith("_key") or not re.match(r"^\d{8}_\d{4}$", path.stem):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cases = list(data.values())
        out.append({"stamp": path.stem, "count": len(cases),
                    "titles": [(c.get("episode") or {}).get("title") or c["case"]["title"] for c in cases]})
    return jsonify(out[:30])


@app.get("/api/lab/<stamp>")
def api_lab_get(stamp: str):
    if not re.match(r"^\d{8}_\d{4}$", stamp):
        return jsonify({"error": "not found"}), 404
    path = LAB_DIR / f"{stamp}.json"
    if not path.is_file():
        return jsonify({"error": "not found"}), 404
    data = json.loads(path.read_text(encoding="utf-8"))
    key_path = LAB_DIR / f"{stamp}_key.json"
    key = json.loads(key_path.read_text(encoding="utf-8")) if key_path.is_file() else {}
    return jsonify({"stamp": stamp, "items": [{"label": label, **item, "model": key.get(label, item.get("model"))}
                                              for label, item in data.items()]})


def _scripts(series_id: str) -> list[dict]:
    out = []
    for path in sorted(SCRIPTS_DIR.glob(f"{series_id}_*.json")) if SCRIPTS_DIR.exists() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({"file": path.name, "title": data.get("title", path.stem), "lines": len(data.get("lines", [])),
                        "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
        except (OSError, ValueError):
            continue
    return out


@app.get("/api/scripts")
def api_scripts_list():
    return jsonify(_scripts(request.args.get("series", "")))


@app.get("/api/scripts/<name>")
def api_script_get(name: str):
    path = SCRIPTS_DIR / Path(name).name
    if not path.is_file() or path.suffix != ".json":
        return jsonify({"error": "not found"}), 404
    return jsonify(json.loads(path.read_text(encoding="utf-8")))


_LINE_RE = re.compile(r"^\s*([А-ЯЁA-Z][А-ЯЁA-Z\- ]{1,30})\s*:\s*(?:\[([^\]]{1,60})\]\s*)?(.+)$")


@app.post("/api/scripts")
def api_script_save():
    """Сохранить сценарий для производства: {series, title, case{...}, lines | text: "РОЛЬ: [эмоция] реплика"}."""
    data = _body()
    series = _load_series(str(data.get("series", ""))) if _ID_RE.match(str(data.get("series", ""))) else None
    if not series:
        return jsonify({"error": "сериал не найден"}), 404
    title = str(data.get("title", "")).strip()
    case = data.get("case") or {}
    lines = data.get("lines")
    if not lines and data.get("text"):
        lines = []
        for raw in str(data["text"]).splitlines():
            m = _LINE_RE.match(raw)
            if m:
                lines.append({"role": m.group(1).strip().upper(), "text": m.group(3).strip(),
                              **({"emotion": m.group(2).strip()} if m.group(2) else {})})
    required = ("client_name", "age", "gender", "occupation", "signature_detail")
    missing = [k for k in required if not str(case.get(k, "")).strip()]
    if not title or not lines or missing:
        return jsonify({"error": "нужны название, реплики и клиент: " + ", ".join(missing or ["—"])}), 400
    client_role = str(case["client_name"]).upper()
    hero_role = series["heroine"]["role"]
    bad = sorted({l["role"] for l in lines} - {hero_role, client_role, "РАССКАЗЧИК"})
    if bad:
        return jsonify({"error": f"роли {bad}: в сценарии только {hero_role}, {client_role} и РАССКАЗЧИК"}), 400
    case = {"title": title, "surface_complaint": case.get("surface_complaint", ""), "real_reason": "",
            "psych_concept": case.get("psych_concept", ""), "research_query": case.get("psych_concept", ""), **case,
            "age": int(case["age"])}
    slug = re.sub(r"[^a-z0-9]+", "_", data.get("slug") or "").strip("_") or datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SCRIPTS_DIR / f"{series['id']}_{slug}.json"
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"title": title, "case": case, "lines": lines}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return jsonify({"ok": True, "file": path.name}), 201


# ── Заказы (Студия) ───────────────────────────────────────────────────────────

def _order_or_404(order_id: str):
    from agents import order_agent
    try:
        return order_agent.load(order_id), None
    except (ValueError, OSError):
        return None, (jsonify({"error": "заказ не найден"}), 404)


def _order_jobs(order_id: str) -> list[dict]:
    with _jobs_lock:
        return [j for j in _jobs.values() if j["type"].startswith("order_") and j["params"].get("order") == order_id]


@app.get("/api/orders")
def api_orders():
    from agents import order_agent
    return jsonify(order_agent.list_orders())


@app.post("/api/orders")
def api_order_create():
    from agents import order_agent
    order, errors = order_agent.new_order(_body())
    if errors:
        return jsonify({"error": f"неверные поля: {', '.join(errors)}"}), 400
    order_agent.save(order)
    return jsonify({"ok": True, "id": order["id"]}), 201


@app.get("/api/orders/<order_id>")
def api_order_get(order_id: str):
    from agents import order_agent
    order, err = _order_or_404(order_id)
    if err:
        return err
    jobs = sorted(_order_jobs(order_id), key=lambda j: j["started_at"])
    return jsonify({**order, "estimate_usd": order_agent.estimate_usd(order),
                    "job": jobs[-1] if jobs else None})


@app.put("/api/orders/<order_id>")
def api_order_update(order_id: str):
    """Параметры заказа и правка сценария: script_text «РОЛЬ: [эмоция] реплика» + characters."""
    from agents import order_agent
    order, err = _order_or_404(order_id)
    if err:
        return err
    if any(j["status"] == "running" for j in _order_jobs(order_id)):
        return jsonify({"error": "заказ сейчас в работе — дождитесь окончания"}), 409
    data = _body()
    errors = order_agent.apply_params(order, data)
    if errors:
        return jsonify({"error": f"неверные поля: {', '.join(errors)}"}), 400
    if "script_text" in data or "characters" in data:
        old = order.get("script") or {"title": order.get("title") or "Ролик", "characters": [], "lines": []}
        lines = order_agent.parse_lines(data["script_text"]) if "script_text" in data else old["lines"]
        if "script_text" in data and data["script_text"].strip() and not lines:
            return jsonify({"error": "не разобрал реплики: каждая строка — «РОЛЬ: [эмоция] текст»"}), 400
        chars = data.get("characters", old["characters"])
        order["script"] = order_agent.normalize_script(
            {"title": data.get("script_title") or old.get("title"), "characters": chars, "lines": lines}, order)
        if order["script"]["lines"] and order["status"] in ("draft", "failed", "done"):
            order["status"] = "script_ready"
    order_agent.save(order)
    return jsonify({**order, "estimate_usd": order_agent.estimate_usd(order)})


@app.post("/api/orders/<order_id>/files")
def api_order_file_add(order_id: str):
    """Файл клиента: {name, data (base64), kind: asset|logo} — JSON, как все запросы панели."""
    import base64
    from agents import order_agent
    order, err = _order_or_404(order_id)
    if err:
        return err
    body = _body()
    try:
        raw = base64.b64decode(str(body.get("data", "")).split(",")[-1], validate=True)
    except ValueError:
        return jsonify({"error": "файл не прочитан"}), 400
    if not order_agent.file_size_ok(len(raw)):
        return jsonify({"error": "файл пустой или больше 15 МБ"}), 400
    try:
        name = order_agent.add_file(order, str(body.get("name", "")), raw,
                                    "logo" if body.get("kind") == "logo" else "asset")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    order_agent.save(order)
    return jsonify({"ok": True, "name": name}), 201


@app.post("/api/orders/<order_id>/files/remove")
def api_order_file_remove(order_id: str):
    from agents import order_agent
    order, err = _order_or_404(order_id)
    if err:
        return err
    name = str(_body().get("name", ""))
    path = order_agent.file_path(order, name)
    if not path:
        return jsonify({"error": "файл не найден"}), 404
    order["files"] = [f for f in order["files"] if f != name]
    if order.get("logo") == name:
        order["logo"] = None
    path.unlink(missing_ok=True)
    order_agent.save(order)
    return jsonify({"ok": True})


@app.get("/api/orders/<order_id>/file/<name>")
def api_order_file(order_id: str, name: str):
    from agents import order_agent
    order, err = _order_or_404(order_id)
    if err:
        return err
    path = order_agent.file_path(order, name)
    return send_file(path) if path else (jsonify({"error": "not found"}), 404)


@app.get("/api/orders/<order_id>/video")
def api_order_video(order_id: str):
    from agents import order_agent
    order, err = _order_or_404(order_id)
    if err:
        return err
    name = (order.get("result") or {}).get("video")
    path = order_agent.order_dir(order_id) / name if name else None
    if not path or not path.is_file():
        return jsonify({"error": "видео ещё нет"}), 404
    title = re.sub(r"[^\w\- ]+", "", order.get("title") or order_id).strip() or order_id
    return send_file(path, mimetype="video/mp4", conditional=True,
                     as_attachment=request.args.get("dl") == "1", download_name=f"{title}.mp4")


@app.post("/api/orders/<order_id>/delete")
def api_order_delete(order_id: str):
    from agents import order_agent
    order, err = _order_or_404(order_id)
    if err:
        return err
    if any(j["status"] == "running" for j in _order_jobs(order_id)):
        return jsonify({"error": "заказ сейчас в работе"}), 409
    shutil.rmtree(order_agent.order_dir(order_id))
    return jsonify({"ok": True})


# ── Логи ──────────────────────────────────────────────────────────────────────

_LOG_LEVELS = [(re.compile(r"CRITICAL|ERROR|Traceback"), "err"), (re.compile(r"WARNING"), "warn")]


@app.get("/api/logs")
def api_logs():
    n = _int_arg("lines", 200, hi=2000)
    files = sorted(LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime) if LOGS_DIR.exists() else []
    raw: list[str] = []
    for lf in reversed(files):
        try:
            raw = lf.read_text(encoding="utf-8", errors="replace").splitlines() + raw
        except OSError:
            continue
        if len(raw) >= n * 2:
            break
    out = []
    for ln in raw[-n:]:
        if ln.strip():
            level = next((lvl for pat, lvl in _LOG_LEVELS if pat.search(ln)), "")
            out.append({"text": redact(ln), "level": level})
    return jsonify(out)


# ── Запуск ────────────────────────────────────────────────────────────────────

_load_jobs()


def _autostart_scheduler() -> bool:
    """Расписание включено, а планировщик не работает — запустить его вместе с панелью."""
    if _scheduler_alive() or not any(_schedule(s)["enabled"] for s in _all_series()):
        return False
    headers = {"Host": "localhost", **({"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {})}
    resp = app.test_client().post("/api/jobs", json={"type": "scheduler"}, headers=headers)
    print("Планировщик запущен — выпуск по расписанию" if resp.status_code == 201
          else f"Планировщик не запустился: {resp.get_json()}")
    return resp.status_code == 201

if __name__ == "__main__":
    port = int(os.getenv("API_PORT", 5000))
    if API_HOST not in _LOOPBACK_HOSTS and not ADMIN_TOKEN:
        sys.exit(f"API_HOST={API_HOST} открывает панель в сеть — задайте ADMIN_TOKEN в .env "
                 "(панель запускает генерацию и публикацию).")
    if API_HOST not in _LOOPBACK_HOSTS:
        print("Внимание: для доступа по IP/имени хоста добавьте его в API_ALLOWED_HOSTS")
    print(f"Панель управления → http://localhost:{port}")
    _autostart_scheduler()
    app.run(host=API_HOST, port=port, debug=False, threaded=True)
