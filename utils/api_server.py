"""
API Server v2 — расширенный Flask бэкенд для Admin Panel.

Запуск:  py utils/api_server.py
Порт:    5000 (переопределяется через API_PORT)

Эндпоинты:
  GET  /api/stats          — KPI: видео сегодня/всего, ошибок, аккаунтов
  GET  /api/runs           — история запусков из pipeline_state
  GET  /api/agents         — статистика по каждому агенту
  GET  /api/pipeline       — статус шагов последнего запуска
  GET  /api/logs           — последние N строк из logs/
  GET  /api/messages       — сообщения агентов (?since=ISO&limit=200)
  GET  /api/stream         — SSE: новые сообщения + pipeline каждую секунду
  POST /api/run            — запустить пайплайн {niche, platform, dry_run, custom_topic}
  POST /api/run/stop       — остановить текущий процесс
  GET  /api/run/status     — статус текущего процесса
  GET  /api/ideas          — все идеи из pipeline_state (?status=&limit=200)
  POST /api/test/<agent>   — быстрый тест агента с мок-данными
"""

import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

# UTF-8 stdout/stderr на Windows (избегаем 'charmap' UnicodeEncodeError на →, ✓ и т.п.)
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from flask import Flask, Response, jsonify, request, send_file
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

app = Flask(__name__)
CORS(app, origins="*")

DB_PATH = os.getenv("DB_PATH", "pipeline.db")
LOGS_DIR = Path(__file__).parent.parent / "logs"
ROOT_DIR = Path(__file__).parent.parent

STEP_DISPLAY = {
    "trend_scout":    "Trend Scout",
    "idea_generator": "Idea Generator",
    "topic_filter":   "Topic Filter",
    "script_writer":  "Script Writer",
    "fact_checker":   "Fact Checker",
    "safety_agent":   "Safety Agent",
    "voice_agent":    "Voice Agent",
    "music_agent":    "Music Agent",
    "subtitle_agent": "Subtitle Agent",
    "visual_agent":   "Visual Agent",
    "assembly_agent": "Assembly Agent",
    "thumbnail_agent":"Thumbnail Agent",
    "qa_agent":       "QA Agent",
    "publisher_agent":"Publisher Agent",
    "notify_agent":   "Notify Agent",
}

STEP_META = {
    "trend_scout":    {"model": "pytrends + PRAW",       "layer": "Идеи"},
    "idea_generator": {"model": "Groq Llama 3.3 70B",    "layer": "Идеи"},
    "topic_filter":   {"model": "Gemini Flash + SQLite",  "layer": "Идеи"},
    "script_writer":  {"model": "Groq Llama 3.3 70B",    "layer": "Сценарий"},
    "fact_checker":   {"model": "Groq Llama 3.3 70B",    "layer": "Сценарий"},
    "safety_agent":   {"model": "Gemini 2.0 Flash",       "layer": "Контроль"},
    "voice_agent":    {"model": "Edge TTS",               "layer": "Медиа"},
    "music_agent":    {"model": "Groq + FMA/local",       "layer": "Медиа"},
    "subtitle_agent": {"model": "Whisper",                "layer": "Медиа"},
    "visual_agent":   {"model": "Pexels API",             "layer": "Медиа"},
    "assembly_agent": {"model": "FFmpeg",                 "layer": "Медиа"},
    "thumbnail_agent":{"model": "Pixazo / Pillow",        "layer": "Медиа"},
    "qa_agent":       {"model": "ffprobe",                "layer": "Контроль"},
    "publisher_agent":{"model": "Zernio API",             "layer": "Публикация"},
    "notify_agent":   {"model": "Telegram Bot API",       "layer": "Публикация"},
}

# ── Процесс-менеджер ──────────────────────────────────────────────────────────

_active_process: subprocess.Popen | None = None
_active_meta: dict | None = None
_process_lock = threading.Lock()


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _db_exists() -> bool:
    return os.path.exists(DB_PATH)


def _ensure_tables() -> None:
    """Создаёт нужные таблицы если БД уже существует (для запуска до main.py)."""
    if not _db_exists():
        return
    conn = _db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                sender TEXT NOT NULL,
                receiver TEXT,
                message TEXT NOT NULL,
                run_id TEXT
            );
            CREATE TABLE IF NOT EXISTS pipeline_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                step TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS error_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                step TEXT,
                error TEXT,
                attempt INTEGER,
                created_at TEXT NOT NULL
            );
        """)
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _map_status(raw: str) -> str:
    if raw in ("success", "completed"):
        return "done"
    if raw == "running":
        return "running"
    if raw and raw.startswith("failed"):
        return "error"
    return "idle"


# ── Serve admin panel ─────────────────────────────────────────────────────────

@app.get("/")
@app.get("/admin_panel.html")
def serve_admin():
    return send_file(str(ROOT_DIR / "admin_panel.html"))


# ── /api/stats ────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def api_stats():
    if not _db_exists():
        return jsonify({"videos_today": 0, "videos_total": 0,
                        "errors_today": 0, "accounts": 0, "last_run": None})
    today = _today_iso()
    conn = _db()
    videos_today = conn.execute(
        "SELECT COUNT(*) FROM pipeline_state"
        " WHERE step='pipeline' AND status='completed' AND created_at LIKE ?",
        (f"{today}%",),
    ).fetchone()[0]
    videos_total = conn.execute(
        "SELECT COUNT(*) FROM pipeline_state WHERE step='pipeline' AND status='completed'"
    ).fetchone()[0]
    errors_today = conn.execute(
        "SELECT COUNT(*) FROM error_log WHERE created_at LIKE ?", (f"{today}%",)
    ).fetchone()[0]
    last_row = conn.execute(
        "SELECT created_at FROM pipeline_state WHERE step='pipeline'"
        " ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return jsonify({
        "videos_today": videos_today,
        "videos_total": videos_total,
        "errors_today": errors_today,
        "accounts": 1 if os.getenv("ZERNIO_ACCOUNT_ID", "") else 0,
        "last_run": last_row[0] if last_row else None,
    })


# ── /api/runs ─────────────────────────────────────────────────────────────────

@app.get("/api/runs")
def api_runs():
    limit = int(request.args.get("limit", 50))
    if not _db_exists():
        return jsonify([])
    conn = _db()
    rows = conn.execute(
        "SELECT run_id, status, payload, error, created_at FROM pipeline_state"
        " WHERE step='pipeline' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    result = []
    for i, r in enumerate(rows):
        payload = {}
        if r["payload"]:
            try:
                payload = json.loads(r["payload"])
            except Exception:
                pass
        parts = r["run_id"].split("_", 2)
        niche = parts[2][:20] if len(parts) >= 3 else ""
        result.append({
            "id": len(rows) - i,
            "run_id": r["run_id"],
            "topic": payload.get("idea", "—"),
            "niche": niche,
            "platform": payload.get("platform", "—"),
            "post_id": payload.get("post_id", ""),
            "status": "success" if r["status"] == "completed" else "error",
            "error": r["error"],
            "date": r["created_at"],
        })
    return jsonify(result)


# ── /api/agents ───────────────────────────────────────────────────────────────

@app.get("/api/agents")
def api_agents():
    if not _db_exists():
        return jsonify([_agent_stub(k) for k in STEP_DISPLAY])
    conn = _db()
    result = []
    for key, name in STEP_DISPLAY.items():
        runs = conn.execute(
            "SELECT COUNT(*) FROM pipeline_state WHERE step=? AND status='success'", (key,)
        ).fetchone()[0]
        errors = conn.execute(
            "SELECT COUNT(*) FROM error_log WHERE step=?", (key,)
        ).fetchone()[0]
        last = conn.execute(
            "SELECT created_at, status FROM pipeline_state"
            " WHERE step=? ORDER BY created_at DESC LIMIT 1", (key,)
        ).fetchone()
        meta = STEP_META.get(key, {"model": "—", "layer": "—"})
        rate = round(runs / (runs + errors) * 100) if (runs + errors) > 0 else 100
        result.append({
            "key": key, "name": name,
            "model": meta["model"], "layer": meta["layer"],
            "runs": runs, "errors": errors, "success_rate": rate,
            "last_run": last["created_at"] if last else None,
            "status": _map_status(last["status"] if last else "idle"),
        })
    conn.close()
    return jsonify(result)


def _agent_stub(key: str) -> dict:
    meta = STEP_META.get(key, {"model": "—", "layer": "—"})
    return {"key": key, "name": STEP_DISPLAY[key], "model": meta["model"],
            "layer": meta["layer"], "runs": 0, "errors": 0, "success_rate": 100,
            "last_run": None, "status": "idle"}


# ── /api/pipeline ─────────────────────────────────────────────────────────────

def _get_pipeline_data() -> dict:
    if not _db_exists():
        return {"run_id": None, "steps": _idle_steps()}
    conn = _db()
    last = conn.execute(
        "SELECT run_id FROM pipeline_state ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not last:
        conn.close()
        return {"run_id": None, "steps": _idle_steps()}
    run_id = last["run_id"]
    rows = conn.execute(
        "SELECT step, status, error, created_at FROM pipeline_state"
        " WHERE run_id=? ORDER BY created_at ASC", (run_id,)
    ).fetchall()
    conn.close()
    step_map: dict = {}
    for r in rows:
        step_map[r["step"]] = {
            "status": _map_status(r["status"]),
            "error": r["error"],
            "updated_at": r["created_at"],
        }
    steps = []
    for k in STEP_DISPLAY:
        info = step_map.get(k, {"status": "idle", "error": None, "updated_at": None})
        steps.append({"key": k, "label": STEP_DISPLAY[k], **info})
    return {"run_id": run_id, "steps": steps}


@app.get("/api/pipeline")
def api_pipeline():
    return jsonify(_get_pipeline_data())


def _idle_steps() -> list:
    return [{"key": k, "label": v, "status": "idle", "error": None, "updated_at": None}
            for k, v in STEP_DISPLAY.items()]


# ── /api/logs ─────────────────────────────────────────────────────────────────

_LOG_PATTERNS = [
    (re.compile(r"\[ERROR\]|\[CRITICAL\]|ошибка|Error|ERROR|Traceback", re.I), "error"),
    (re.compile(r"\[WARNING\]|Warning|WARNING",                             re.I), "warn"),
    (re.compile(r"успешно|approved|completed|published|OK",                 re.I), "success"),
    (re.compile(r"\[INFO\]",                                                re.I), "info"),
]

def _classify_log(line: str) -> str:
    for pat, lvl in _LOG_PATTERNS:
        if pat.search(line):
            return lvl
    return "dim"

@app.get("/api/logs")
def api_logs():
    n = int(request.args.get("lines", 100))
    if not LOGS_DIR.exists():
        return jsonify([])
    log_files = sorted(LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)
    raw: list[str] = []
    for lf in reversed(log_files):
        try:
            with open(lf, encoding="utf-8", errors="replace") as f:
                raw = f.readlines() + raw
            if len(raw) >= n * 2:
                break
        except OSError:
            continue
    return jsonify([
        {"text": ln.rstrip(), "level": _classify_log(ln)}
        for ln in raw[-n:] if ln.strip()
    ])


# ── /api/messages ─────────────────────────────────────────────────────────────

@app.get("/api/messages")
def api_messages():
    """Возвращает сообщения агентов. ?since=ISO&limit=200&run_id="""
    since = request.args.get("since", "")
    limit = int(request.args.get("limit", 200))
    run_id_filter = request.args.get("run_id", "")
    _ensure_tables()
    if not _db_exists():
        return jsonify([])
    conn = _db()
    if since:
        rows = conn.execute(
            "SELECT id, timestamp, sender, receiver, message, run_id"
            " FROM agent_messages WHERE timestamp > ? ORDER BY id ASC LIMIT ?",
            (since, limit),
        ).fetchall()
    elif run_id_filter:
        rows = conn.execute(
            "SELECT id, timestamp, sender, receiver, message, run_id"
            " FROM agent_messages WHERE run_id=? ORDER BY id ASC LIMIT ?",
            (run_id_filter, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, timestamp, sender, receiver, message, run_id"
            " FROM agent_messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        rows = list(reversed(rows))
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── /api/stream (SSE) ─────────────────────────────────────────────────────────

@app.get("/api/stream")
def api_stream():
    """Server-Sent Events: сообщения агентов + статус пайплайна каждую секунду."""

    def generate():
        last_msg_id = 0
        tick = 0
        _ensure_tables()

        # Отправляем последние 50 сообщений сразу при подключении
        if _db_exists():
            try:
                conn = _db()
                rows = conn.execute(
                    "SELECT id, timestamp, sender, receiver, message, run_id"
                    " FROM agent_messages ORDER BY id DESC LIMIT 50"
                ).fetchall()
                conn.close()
                for r in reversed(rows):
                    last_msg_id = max(last_msg_id, r["id"])
                    payload = json.dumps({"type": "message", **dict(r)}, ensure_ascii=False)
                    yield f"data: {payload}\n\n"
            except Exception:
                pass

        while True:
            try:
                if _db_exists():
                    conn = _db()
                    rows = conn.execute(
                        "SELECT id, timestamp, sender, receiver, message, run_id"
                        " FROM agent_messages WHERE id > ? ORDER BY id ASC LIMIT 20",
                        (last_msg_id,),
                    ).fetchall()
                    conn.close()
                    for r in rows:
                        last_msg_id = r["id"]
                        payload = json.dumps(
                            {"type": "message", **dict(r)}, ensure_ascii=False
                        )
                        yield f"data: {payload}\n\n"

                # Статус пайплайна каждые 2 секунды
                if tick % 2 == 0:
                    pl = _get_pipeline_data()
                    yield f"data: {json.dumps({'type': 'pipeline', 'data': pl}, ensure_ascii=False)}\n\n"

                # Статус процесса каждую секунду
                with _process_lock:
                    running = _active_process is not None and _active_process.poll() is None
                    meta = _active_meta
                yield f"data: {json.dumps({'type': 'run_status', 'running': running, 'meta': meta}, ensure_ascii=False)}\n\n"

            except Exception:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

            tick += 1
            time.sleep(1)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
            "Connection": "keep-alive",
        },
    )


# ── /api/run ──────────────────────────────────────────────────────────────────

_stdout_lines: list[str] = []
_stdout_lock = threading.Lock()


def _drain_stdout(proc: subprocess.Popen) -> None:
    """Читает stdout subprocess в фоне чтобы пайп-буфер не переполнился."""
    try:
        for line in iter(proc.stdout.readline, ""):
            with _stdout_lock:
                _stdout_lines.append(line.rstrip())
                if len(_stdout_lines) > 500:
                    _stdout_lines.pop(0)
    except Exception:
        pass


@app.post("/api/run")
def api_run():
    """Запускает py main.py в subprocess. Body: {niche, platform, dry_run, custom_topic}."""
    global _active_process, _active_meta
    data = request.get_json(force=True, silent=True) or {}
    niche = str(data.get("niche", "психология")).strip()
    platform = str(data.get("platform", "tiktok")).strip()
    dry_run = bool(data.get("dry_run", False))
    custom_topic = str(data.get("custom_topic", "")).strip()

    with _process_lock:
        if _active_process and _active_process.poll() is None:
            return jsonify({"error": "Пайплайн уже запущен", "pid": _active_process.pid}), 409

        topic_arg = custom_topic if custom_topic else niche
        cmd = [sys.executable, "main.py", "--niche", topic_arg, "--platform", platform]
        if dry_run:
            cmd.append("--dry-run")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT_DIR),
            )
            _active_process = proc
            _active_meta = {
                "niche": topic_arg,
                "platform": platform,
                "dry_run": dry_run,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "pid": proc.pid,
            }
            # Дренируем stdout в фоне — иначе пайп переполняется и процесс зависает
            t = threading.Thread(target=_drain_stdout, args=(proc,), daemon=True)
            t.start()
            return jsonify({"status": "started", "pid": proc.pid})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.get("/api/run/output")
def api_run_output():
    """Последние строки stdout текущего/последнего запуска."""
    with _stdout_lock:
        lines = list(_stdout_lines[-100:])
    return jsonify(lines)


@app.post("/api/run/stop")
def api_run_stop():
    """Завершает активный subprocess."""
    global _active_process
    with _process_lock:
        if not _active_process or _active_process.poll() is not None:
            return jsonify({"error": "Нет активного процесса"}), 404
        _active_process.terminate()
        try:
            _active_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _active_process.kill()
        return jsonify({"status": "stopped"})


@app.get("/api/run/status")
def api_run_status():
    """Статус текущего процесса."""
    with _process_lock:
        if not _active_process:
            return jsonify({"running": False, "meta": None, "exit_code": None})
        poll = _active_process.poll()
        return jsonify({"running": poll is None, "exit_code": poll, "meta": _active_meta})


# ── /api/ideas ────────────────────────────────────────────────────────────────

@app.get("/api/ideas")
def api_ideas():
    """Все идеи из pipeline_state. ?status=pending|published|failed&limit=200"""
    status_filter = request.args.get("status", "")
    limit = int(request.args.get("limit", 200))
    if not _db_exists():
        return jsonify([])

    conn = _db()
    rows = conn.execute(
        "SELECT run_id, payload, created_at FROM pipeline_state"
        " WHERE step='idea_generator' AND status='success'"
        " ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()

    result = []
    for row in rows:
        if not row["payload"]:
            continue
        try:
            ideas = json.loads(row["payload"])
            if not isinstance(ideas, list):
                ideas = [ideas]
        except Exception:
            continue

        script_row = conn.execute(
            "SELECT payload FROM pipeline_state"
            " WHERE run_id=? AND step='script_writer' AND status='success' LIMIT 1",
            (row["run_id"],),
        ).fetchone()

        pipeline_row = conn.execute(
            "SELECT status FROM pipeline_state WHERE run_id=? AND step='pipeline' LIMIT 1",
            (row["run_id"],),
        ).fetchone()

        if pipeline_row:
            idea_status = "published" if pipeline_row["status"] == "completed" else "failed"
        else:
            idea_status = "pending"

        if status_filter and idea_status != status_filter:
            continue

        script = None
        if script_row and script_row["payload"]:
            try:
                script = json.loads(script_row["payload"])
            except Exception:
                pass

        parts = row["run_id"].split("_", 2)
        niche = parts[2][:20] if len(parts) >= 3 else ""

        for idea in ideas:
            result.append({
                "run_id": row["run_id"],
                "date": row["created_at"],
                "niche": niche,
                "title": idea.get("title", ""),
                "hook": idea.get("hook", ""),
                "angle": idea.get("angle", ""),
                "hashtags": idea.get("hashtags", []),
                "virality": idea.get("virality", 0),
                "source_topic": idea.get("source_topic", ""),
                "status": idea_status,
                "script": script,
            })

    conn.close()
    return jsonify(result)


# ── /api/test/<agent> ─────────────────────────────────────────────────────────

@app.post("/api/test/<agent_name>")
def api_test_agent(agent_name: str):
    """Запускает быстрый тест агента с мок-данными."""
    start = time.time()
    root = str(ROOT_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from tests.test_agents import run_agent_test
        result = run_agent_test(agent_name)
        ms = round((time.time() - start) * 1000)
        return jsonify({"success": result.get("success", False),
                        "message": result.get("message", ""), "duration_ms": ms})
    except Exception as e:
        ms = round((time.time() - start) * 1000)
        return jsonify({"success": False, "message": str(e), "duration_ms": ms})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = str(ROOT_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    _ensure_tables()
    port = int(os.getenv("API_PORT", 5000))
    print(f"Admin API v2 → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
