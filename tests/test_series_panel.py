"""Страница «Сериал» в админке: этапы сборки (в том числе возобновлённой), очередь, отдача видео."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = """
import json, os, sqlite3, sys
sys.path.insert(0, %(root)r)
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ZERNIO_API_KEY", "KIE_API_KEY"):
    os.environ[_k] = ""
import utils.api_server as api
api.DB_PATH = %(db)r
from agents import anthology_agent as aa
conn = sqlite3.connect(%(db)r)
conn.execute("CREATE TABLE pipeline_state (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, step TEXT, "
             "status TEXT, payload TEXT, error TEXT, created_at TEXT)")
def add(run, step, status, payload=None, t="2026-10-21T10:00:00+00:00", error=None):
    conn.execute("INSERT INTO pipeline_state (run_id, step, status, payload, error, created_at) VALUES (?,?,?,?,?,?)",
                 (run, step, status, json.dumps(payload) if payload is not None else None, error, t))
# Готовый эпизод в очереди
q = "20261021_090000_series_kabinet7"
for step in ("anthology_cases", "anthology_episode", "series_assembly", "qa_agent"):
    add(q, step, "running"); add(q, step, "success", {"title": "Стикер"} if step == "anthology_episode" else {})
add(q, "pipeline", "completed", {"status": "queued", "episode": 1})
# Упавший и возобновлённый запуск: озвучка упала трижды, при доделке прошла, идёт монтаж
r = "20261021_100000_series_kabinet7"
add(r, "anthology_cases", "running"); add(r, "anthology_cases", "success")
add(r, "anthology_episode", "success", {"title": "Смотритель кружек"})
add(r, "scene_agent", "running"); add(r, "scene_agent", "success")
add(r, "dialogue_voice", "running")
for i in (1, 2, 3):
    add(r, "dialogue_voice", f"failed_attempt_{i}", error="429")
add(r, "pipeline", "failed", error="voice")
add(r, "dialogue_voice", "running", t="2099-01-01T00:00:00+00:00")
add(r, "dialogue_voice", "success", t="2099-01-01T00:00:50+00:00")
add(r, "series_assembly", "running", t="2099-01-01T00:00:51+00:00")
conn.commit()
series = aa.load_series("kabinet7")
inside = os.path.join(%(root)r, "output", "video", "_panel_test.mp4")
ep = {"episode_no": 1, "title": "Стикер", "case": {"client_name": "Анна", "age": 30, "occupation": "x"}}
ok_id = aa.queue_episode(series, ep, q, %(db)r, video_path=inside, publish_title="Кабинет 7 · 1", description="d",
                         source_topic="t")
bad_id = aa.queue_episode(series, {**ep, "episode_no": 2}, r, %(db)r, video_path=%(outside)r,
                          publish_title="Кабинет 7 · 2", description="d", source_topic="t2")
os.makedirs(os.path.dirname(inside), exist_ok=True)
open(inside, "wb").write(b"0" * 100)
client = api.app.test_client()
h = {"Host": "localhost"}
try:
    d = client.get("/api/series/kabinet7/builds", headers=h).get_json()
    cur = d["current"]
    out = {"current": cur["title"], "status": cur["status"], "resumed": cur["resumed"],
           "stages": {s["key"]: [s["status"], s.get("reused", False), s.get("retries")] for s in cur["stages"]},
           "runs": [[x["title"], x["status"]] for x in d["runs"]],
           "queued_publish": [s["status"] for s in api._run_view(api._db(), q)["stages"] if s["key"] == "publisher_agent"],
           "videos": {str(e["id"]): e["has_video"] for e in client.get("/api/series/kabinet7/episodes", headers=h).get_json()},
           "get_ok": client.get(f"/api/video/{ok_id}", headers=h).status_code,
           "get_outside": client.get(f"/api/video/{bad_id}", headers=h).status_code,
           "reject": client.post(f"/api/series/kabinet7/episodes/{ok_id}", json={"action": "reject"}, headers=h).status_code,
           "after": [e["status"] for e in aa.episode_queue(series, %(db)r) if e["id"] == ok_id]}
finally:
    os.remove(inside)
open(%(result)r, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False))
"""


def test_series_panel_stages_queue_and_video(tmp_path):
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"0" * 100)
    result = tmp_path / "r.json"
    code = _SCRIPT % {"root": str(ROOT), "db": str(tmp_path / "p.db"), "outside": str(outside), "result": str(result)}
    proc = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, cwd=str(tmp_path),
                          timeout=120)
    assert result.exists(), proc.stderr.decode("utf-8", "replace")[-3000:]
    out = json.loads(result.read_text(encoding="utf-8"))
    assert out["current"] == "Смотритель кружек" and out["status"] == "running" and out["resumed"] is True
    stages = out["stages"]
    assert stages["anthology_cases"] == ["done", True, None]        # из прошлой попытки
    assert stages["scene_agent"] == ["done", True, None]            # оплаченные кадры не пересобирались
    assert stages["dialogue_voice"] == ["done", False, 0]           # повторы прошлой попытки не считаются
    assert stages["series_assembly"][0] == "running"
    assert out["runs"] == [["Смотритель кружек", "running"], ["Стикер", "queued"]]
    assert out["queued_publish"] == ["skipped"]                     # в очереди — публикация ждёт выхода
    assert list(out["videos"].values()) == [False, True] or list(out["videos"].values()) == [True, False]
    assert out["get_ok"] == 200 and out["get_outside"] == 404       # файлы вне output/ не отдаются
    assert out["reject"] == 200 and out["after"] == ["rejected"]
