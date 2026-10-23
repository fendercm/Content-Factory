"""Очередь сериала: --prepare кладёт эпизоды, --publish-next выпускает по одному, планировщик зовёт его раз в день."""

import json
import os
import subprocess
import sys
from pathlib import Path

from agents import anthology_agent as aa

ROOT = Path(__file__).resolve().parent.parent
SERIES = aa.load_series("kabinet7")


def _episode(no, client="Марина"):
    return {"episode_no": no, "title": f"Эпизод {no}",
            "case": {"client_name": client, "age": 38, "occupation": "главбух", "psych_concept": "вторичная выгода",
                     "real_reason": "причина"}}


def test_queue_numbering_recent_and_statuses(tmp_path):
    db = str(tmp_path / "q.db")
    assert aa.next_episode_number(SERIES, db) == 1
    ids = [aa.queue_episode(SERIES, _episode(n), f"run{n}", db, video_path=f"v{n}.mp4", publish_title=f"T{n}",
                            description="d", source_topic=f"kabinet7:тема{n}") for n in (1, 2, 3)]
    assert aa.next_episode_number(SERIES, db) == 4
    assert [e["episode_no"] for e in aa.recent_episodes(SERIES, db)] == [3, 2, 1]  # очередь тоже учитывается

    aa.set_episode_status(ids[0], "published", db, post_id="yt1")
    aa.set_episode_status(ids[1], "rejected", db)
    nxt = aa.next_ready(SERIES, db)
    assert nxt["episode_no"] == 3 and nxt["publish_title"] == "T3"
    rows = {e["episode_no"]: e for e in aa.episode_queue(SERIES, db)}
    assert rows[1]["status"] == "published" and rows[1]["post_id"] == "yt1" and rows[1]["published_at"]
    assert [e["episode_no"] for e in aa.recent_episodes(SERIES, db)] == [3, 1]     # снятый не мешает кейсам


def test_legacy_table_gets_queue_columns(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE anthology_episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, series_id TEXT NOT NULL, "
                 "episode_no INTEGER NOT NULL, title TEXT, client TEXT, psych_concept TEXT, summary TEXT, "
                 "run_id TEXT, post_id TEXT, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO anthology_episodes (series_id, episode_no, title, created_at) VALUES ('kabinet7', 1, 'x', 'now')")
    conn.commit()
    conn.close()
    assert aa.next_episode_number(SERIES, db) == 2
    assert aa.episode_queue(SERIES, db)[0]["status"] == "published"


_PUBLISH_HARNESS = """
import json, os, sys
from unittest.mock import MagicMock, patch
sys.path.insert(0, %(root)r)
sys.argv = ["main.py"]
os.environ["DB_PATH"] = %(db)r
# Пустые значения не даст перезаписать load_dotenv: доп. площадки из настоящего .env в тест не попадают
for key in ("VK_USER_TOKEN", "ZERNIO_ACCOUNT_ID_TIKTOK", "ZERNIO_ACCOUNT_ID_INSTAGRAM"):
    os.environ[key] = ""
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ZERNIO_API_KEY"):
    os.environ[_k] = ""  # до load_dotenv в main: настоящие ключи из .env не подхватятся (см. conftest.py)
import main
from agents import anthology_agent as aa
series = aa.load_series("kabinet7")
for n in (1, 2):
    video = os.path.join(%(tmp)r, f"ep{n}.mp4")
    open(video, "wb").write(b"0")
    ep = {"episode_no": n, "title": f"E{n}", "case": {"client_name": "Марина", "age": 38, "occupation": "x"}}
    aa.queue_episode(series, ep, f"run{n}", %(db)r, video_path=video, publish_title=f"Кабинет 7 · {n}",
                     description="d", source_topic=f"kabinet7:t{n}")
publisher = MagicMock(side_effect=lambda *a, **k: {"post_id": f"yt{publisher.call_count}", "status": "published"})
out = {}
with patch.object(main, "step_qa_agent", return_value={"status": "approved"}), \\
     patch.object(main, "step_publisher_agent", publisher), \\
     patch("agents.notify_agent.send_manual_upload", return_value={"ok": True}) as manual, \\
     patch.object(main, "step_notify_success"), patch.object(main, "_alert") as alert:
    for _ in range(3):
        main.publish_next_series_episode("kabinet7", "youtube")
    out["titles"] = [c.args[1]["title"] for c in publisher.call_args_list]
    out["alerts"] = alert.call_count
    out["manual_calls"] = manual.call_count
out["statuses"] = [(e["episode_no"], e["status"], e["post_id"]) for e in aa.episode_queue(series, %(db)r)]
import sqlite3
out["topics"] = [r[0] for r in sqlite3.connect(%(db)r).execute("SELECT topic FROM topics_used ORDER BY id")]
open(%(result)r, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False))
"""


def test_publish_next_releases_one_per_call_then_alerts_empty(tmp_path):
    result = tmp_path / "result.json"
    code = _PUBLISH_HARNESS % {"root": str(ROOT), "db": str(tmp_path / "p.db"), "tmp": str(tmp_path),
                               "result": str(result)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, cwd=str(tmp_path), timeout=300)
    assert result.exists(), proc.stderr.decode("utf-8", "replace")[-3000:]
    out = json.loads(result.read_text(encoding="utf-8"))
    assert out["titles"] == ["Кабинет 7 · 1", "Кабинет 7 · 2"]      # третий вызов — очередь пуста
    assert out["alerts"] == 1
    assert out["manual_calls"] == 2                                   # Wibes/VK — по сообщению на эпизод
    assert out["statuses"] == [[1, "published", "yt1"], [2, "published", "yt2"]]
    assert out["topics"] == ["kabinet7:t1", "kabinet7:t2"]


def test_cli_rejects_prepare_with_dry_run(tmp_path):
    proc = subprocess.run([sys.executable, str(ROOT / "main.py"), "--format", "series", "--niche", "психология",
                           "--prepare", "2", "--dry-run"], capture_output=True, cwd=str(tmp_path), timeout=120,
                          env={**os.environ, "DB_PATH": str(tmp_path / "p.db")})
    assert proc.returncode == 2 and "--prepare" in proc.stderr.decode("utf-8", "replace")


def test_scheduler_series_command(tmp_path):
    code = ("import json, sys; sys.path.insert(0, %r); import scheduler; "
            "open(%r, 'w').write(json.dumps(scheduler.series_publish_command()))") % (str(ROOT), str(tmp_path / "c.json"))
    env = {**os.environ, "SERIES_ID": "kabinet7", "SERIES_PLATFORM": "youtube"}
    subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path), timeout=120, env=env, capture_output=True)
    cmd = json.loads((tmp_path / "c.json").read_text())
    assert cmd[1].endswith("main.py")
    assert cmd[2:] == ["--format", "series", "--series", "kabinet7", "--niche", cmd[7], "--platform", "youtube",
                       "--publish-next"]


_RESUME_HARNESS = """
import json, os, sqlite3, sys
sys.path.insert(0, %(root)r)
sys.argv = ["main.py"]
os.environ["DB_PATH"] = %(db)r
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ZERNIO_API_KEY"):
    os.environ[_k] = ""
import main
from agents import anthology_agent as aa
series = aa.load_series("kabinet7")
conn = sqlite3.connect(%(db)r)
main._init_db(conn)
case = {"title": "Кружка", "client_name": "Игорь", "age": 43, "gender": "male", "occupation": "x",
        "psych_concept": "y", "real_reason": "z"}
video = os.path.join(%(tmp)r, "shot_01.mp4"); open(video, "wb").write(b"0")
image = os.path.join(%(tmp)r, "shot_01.jpg"); open(image, "wb").write(b"0")
main._save_step(conn, "run1", "topic_filter", "success", [{"title": "Кружка", "source_topic": "kabinet7:y", "_case": case}])
main._save_step(conn, "run1", "anthology_episode", "success", {"title": "Смотритель", "lines": []})
main._save_step(conn, "run1", "anthology_episode", "success", {"title": "Смотритель", "case": case,
                "lines": [{"role": "ИГОРЬ", "text": "a"}]})
main._save_step(conn, "run1", "anthology_shots", "success", {"shots": [{"slot": "wide"}], "assignment": [{"shot": 0}]})
main._save_step(conn, "run1", "scene_agent", "success", {"shots": [{"slot": "wide", "image_path": image,
                "video_path": video}], "credits": 28})
ep, idea, plan, scenes = main._load_series_run(conn, "run1", series)
out = {"title": ep["title"], "topic": idea["source_topic"], "assign": len(plan["assignment"]), "scenes": bool(scenes)}
os.remove(video)
out["scenes_after_loss"] = bool(main._load_series_run(conn, "run1", series)[3])
open(%(result)r, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False))
"""


def test_resume_loads_saved_episode_and_paid_scenes(tmp_path):
    result = tmp_path / "r.json"
    code = _RESUME_HARNESS % {"root": str(ROOT), "db": str(tmp_path / "p.db"), "tmp": str(tmp_path),
                              "result": str(result)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, cwd=str(tmp_path), timeout=300)
    assert result.exists(), proc.stderr.decode("utf-8", "replace")[-3000:]
    out = json.loads(result.read_text(encoding="utf-8"))
    assert out == {"title": "Смотритель", "topic": "kabinet7:y", "assign": 1, "scenes": True,
                   "scenes_after_loss": False}   # пропал файл — кадры перегенерируются, а не ломают сборку



_CATCH_UP = """
import json, os, sqlite3, sys
from datetime import datetime, timezone
sys.path.insert(0, %(root)r)
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ZERNIO_API_KEY"):
    os.environ[_k] = ""
db = %(db)r
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE anthology_episodes (series_id TEXT, status TEXT, published_at TEXT)")
conn.execute("INSERT INTO anthology_episodes VALUES ('kabinet7', 'ready', NULL)")
conn.commit()
os.environ["DB_PATH"] = db
import scheduler
scheduler.series_schedules = lambda: {"kabinet7": ("19:00", "youtube")}
calls = []
scheduler.job_series_publish = lambda sid, pl: calls.append([sid, pl])
out = {"early": scheduler.catch_up_series(datetime(2026, 10, 23, 18, 0)),
       "missed": scheduler.catch_up_series(datetime(2026, 10, 23, 20, 50))}
now = datetime.now()
conn.execute("INSERT INTO anthology_episodes VALUES ('kabinet7', 'published', ?)",
             (now.astimezone(timezone.utc).isoformat(),))
conn.commit()
out["again"] = scheduler.catch_up_series(now.replace(hour=23, minute=0))
conn.execute("DELETE FROM anthology_episodes")
conn.commit()
out["empty"] = scheduler.catch_up_series(datetime(2026, 10, 24, 20, 0))
out["calls"] = calls
open(%(result)r, "w", encoding="utf-8").write(json.dumps(out))
"""


def test_scheduler_catch_up_publishes_missed_release(tmp_path):
    """Планировщик не работал в 19:00 — при старте выпускает пропущенное, но не дважды за день
    и не при пустой очереди (иначе алерт «очередь пуста» на каждый перезапуск)."""
    import json
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    result = tmp_path / "r.json"
    code = _CATCH_UP % {"root": str(root), "db": str(tmp_path / "p.db"), "result": str(result)}
    proc = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, cwd=str(tmp_path),
                          timeout=120)
    assert result.exists(), proc.stderr.decode("utf-8", "replace")[-3000:]
    out = json.loads(result.read_text(encoding="utf-8"))
    assert out == {"early": [], "missed": ["kabinet7"], "again": [], "empty": [],
                   "calls": [["kabinet7", "youtube"]]}
