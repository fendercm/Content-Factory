"""
Планировщик: расписания сериалов из конфигов, догон пропущенного выпуска, защита от второго экземпляра,
задачи по расписанию. main.py и analytics_agent.py не запускаются — subprocess.run подменён.

`import scheduler` меняет sys.stdout, создаёт logs/ в текущей папке и вешает FileHandler на root-логгер,
поэтому модуль импортируется один раз в изолированном окружении (фикстура `sched`).
"""

import importlib
import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

schedule = pytest.importorskip("schedule")


@pytest.fixture(scope="module")
def sched(tmp_path_factory):
    work = tmp_path_factory.mktemp("sched_import")
    guard = logging.NullHandler()  # у root есть handler → logging.basicConfig в scheduler ничего не делает
    root = logging.getLogger()
    root.addHandler(guard)
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(work)
        mp.setattr(sys, "stdout", io.StringIO())  # без .buffer — scheduler не перематывает stdout
        mp.setattr(sys, "stderr", io.StringIO())
        sys.modules.pop("scheduler", None)
        module = importlib.import_module("scheduler")
    root.removeHandler(guard)
    yield module
    sys.modules.pop("scheduler", None)


@pytest.fixture
def env(sched, monkeypatch, tmp_path):
    """Своя папка конфигов, своя БД и пульс; пустое расписание schedule до и после теста."""
    (tmp_path / "config" / "series").mkdir(parents=True)
    monkeypatch.setattr(sched, "_ROOT", tmp_path)
    monkeypatch.setattr(sched, "HEARTBEAT", tmp_path / "logs" / "scheduler.heartbeat")
    monkeypatch.setattr(sched, "_synced", {})
    monkeypatch.setenv("DB_PATH", str(tmp_path / "pipeline.db"))
    for key in ("SCHEDULER_NICHES", "SCHEDULER_MODE", "SERIES_ID", "SERIES_PLATFORM", "SERIES_PUBLISH_TIME"):
        monkeypatch.delenv(key, raising=False)
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(sched.subprocess, "run", run)
    schedule.clear()
    yield {"root": tmp_path, "run": run, "db": tmp_path / "pipeline.db"}
    schedule.clear()


def _series(root: Path, sid: str, sch=None, **extra):
    cfg = {"id": sid, **extra}
    if sch is not None:
        cfg["schedule"] = sch
    (root / "config" / "series" / f"{sid}.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def _episodes_db(db: Path, rows):
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE anthology_episodes (id INTEGER PRIMARY KEY, series_id TEXT, status TEXT, "
                 "published_at TEXT)")
    conn.executemany("INSERT INTO anthology_episodes (series_id, status, published_at) VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


# ── series_schedules ─────────────────────────────────────────────────────────

def test_series_schedules_only_enabled_with_defaults(env, sched):
    root = env["root"]
    _series(root, "a", {"enabled": True, "time": "08:15", "platform": "tiktok"})
    _series(root, "b", {"enabled": True})                       # время и площадка по умолчанию
    _series(root, "c", {"enabled": False, "time": "10:00"})
    _series(root, "d")                                           # раздела schedule нет
    _series(root, "e", None, schedule=None)
    (root / "config" / "series" / "broken.json").write_text("{not json", encoding="utf-8")
    assert sched.series_schedules() == {"a": ("08:15", "tiktok"), "b": ("19:00", "youtube")}


def test_series_schedules_reads_real_config_shape(sched):
    """Настоящий config/series/kabinet7.json разбирается без ошибок."""
    out = sched.series_schedules()
    for sid, (at, platform) in out.items():
        hh, mm = at.split(":")
        assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60 and platform


@pytest.mark.xfail(reason="конфиг без id роняет series_schedules (KeyError) — docs/tech/testing.md", strict=False)
def test_series_schedules_skips_config_without_id(env, sched):
    (env["root"] / "config" / "series" / "noid.json").write_text(
        json.dumps({"schedule": {"enabled": True}}), encoding="utf-8")
    assert sched.series_schedules() == {}


# ── sync_series_jobs ─────────────────────────────────────────────────────────

def _series_jobs():
    return [j for j in schedule.get_jobs("series")]


def test_sync_registers_daily_jobs_and_is_idempotent(env, sched):
    _series(env["root"], "kabinet7", {"enabled": True, "time": "19:30", "platform": "youtube"})
    sched.sync_series_jobs()
    jobs = _series_jobs()
    assert len(jobs) == 1
    assert jobs[0].at_time == dtime(19, 30) and jobs[0].unit == "days"
    assert jobs[0].job_func.func is sched.job_series_publish
    assert jobs[0].job_func.args == ("kabinet7", "youtube")
    sched.sync_series_jobs()
    assert _series_jobs() == jobs                                # ничего не изменилось — задачи те же


def test_sync_picks_up_panel_changes_and_disable(env, sched):
    root = env["root"]
    _series(root, "kabinet7", {"enabled": True, "time": "19:00"})
    schedule.every().day.at("21:00").do(lambda: None)            # чужая задача (сводка) не трогается
    sched.sync_series_jobs()
    _series(root, "kabinet7", {"enabled": True, "time": "07:45", "platform": "tiktok"})
    sched.sync_series_jobs()
    jobs = _series_jobs()
    assert [(j.at_time, j.job_func.args) for j in jobs] == [(dtime(7, 45), ("kabinet7", "tiktok"))]
    _series(root, "kabinet7", {"enabled": False})
    sched.sync_series_jobs()
    assert _series_jobs() == [] and len(schedule.get_jobs()) == 1


@pytest.mark.xfail(reason="время из панели в неверном формате роняет цикл планировщика — docs/tech/testing.md",
                   strict=False)
def test_sync_survives_bad_time_in_config(env, sched):
    _series(env["root"], "kabinet7", {"enabled": True, "time": "7pm"})
    sched.sync_series_jobs()


# ── already_running ──────────────────────────────────────────────────────────

def test_already_running_by_heartbeat_age(env, sched):
    hb = sched.HEARTBEAT
    assert sched.already_running() is False                      # пульса нет
    hb.parent.mkdir(parents=True)
    hb.touch()
    assert sched.already_running() is True
    old = time.time() - sched.HEARTBEAT_FRESH - 5
    os.utime(hb, (old, old))
    assert sched.already_running() is False


def test_main_exits_when_another_instance_alive(env, sched, monkeypatch):
    sched.HEARTBEAT.parent.mkdir(parents=True)
    sched.HEARTBEAT.touch()
    before = sched.HEARTBEAT.stat().st_mtime
    monkeypatch.setattr(sched, "catch_up_series", MagicMock())
    sched.main()
    assert not sched.catch_up_series.called and schedule.get_jobs() == []
    assert sched.HEARTBEAT.stat().st_mtime == before


# ── published_today / has_ready ──────────────────────────────────────────────

def test_published_today_uses_local_date(env, sched):
    now = datetime(2026, 10, 23, 20, 0)
    local_midday = now.replace(hour=12).astimezone()
    _episodes_db(env["db"], [
        ("kabinet7", "published", local_midday.astimezone(timezone.utc).isoformat()),
        ("other", "published", (local_midday - timedelta(days=1)).isoformat()),
        ("other", "published", "не дата"),
        ("third", "ready", local_midday.isoformat()),
    ])
    assert sched.published_today("kabinet7", now) is True
    assert sched.published_today("other", now) is False          # вчера + мусор в поле даты
    assert sched.published_today("third", now) is False          # готов, но не выпущен


def test_published_today_and_has_ready_without_table(env, sched):
    sqlite3.connect(env["db"]).close()
    assert sched.published_today("kabinet7") is False
    assert sched.has_ready("kabinet7") is False


def test_has_ready_counts_only_ready_of_this_series(env, sched):
    _episodes_db(env["db"], [("kabinet7", "published", None), ("other", "ready", None)])
    assert sched.has_ready("kabinet7") is False
    assert sched.has_ready("other") is True


# ── catch_up_series ──────────────────────────────────────────────────────────

@pytest.fixture
def catch_up(env, sched, monkeypatch):
    _series(env["root"], "kabinet7", {"enabled": True, "time": "19:00", "platform": "youtube"})
    publish = MagicMock()
    monkeypatch.setattr(sched, "job_series_publish", publish)
    return publish


def test_catch_up_publishes_missed_release(env, sched, catch_up):
    _episodes_db(env["db"], [("kabinet7", "ready", None)])
    assert sched.catch_up_series(datetime(2026, 10, 23, 19, 0)) == ["kabinet7"]
    catch_up.assert_called_once_with("kabinet7", "youtube")


def test_catch_up_waits_until_release_time(env, sched, catch_up):
    _episodes_db(env["db"], [("kabinet7", "ready", None)])
    assert sched.catch_up_series(datetime(2026, 10, 23, 18, 59)) == []
    assert not catch_up.called


def test_catch_up_skips_when_already_published_today(env, sched, catch_up):
    now = datetime(2026, 10, 23, 21, 0)
    _episodes_db(env["db"], [("kabinet7", "published", now.replace(hour=19, minute=1).astimezone().isoformat()),
                             ("kabinet7", "ready", None)])
    assert sched.catch_up_series(now) == []


def test_catch_up_skips_when_queue_empty(env, sched, catch_up):
    _episodes_db(env["db"], [("kabinet7", "published", "2026-10-20T19:00:00+00:00")])
    assert sched.catch_up_series(datetime(2026, 10, 23, 21, 0)) == []
    assert not catch_up.called


def test_catch_up_ignores_disabled_series(env, sched, catch_up):
    _series(env["root"], "kabinet7", {"enabled": False, "time": "19:00"})
    _episodes_db(env["db"], [("kabinet7", "ready", None)])
    assert sched.catch_up_series(datetime(2026, 10, 23, 23, 0)) == []


# ── задачи, запускающие подпроцессы ──────────────────────────────────────────

def test_series_publish_command_shape(env, sched, monkeypatch):
    monkeypatch.setenv("SERIES_ID", "s1")
    cmd = sched.series_publish_command(platform="tiktok")
    assert cmd[0] == sys.executable and cmd[1].endswith("main.py")
    assert cmd[2:] == ["--format", "series", "--series", "s1", "--niche", sched._NICHE_DEFAULT,
                       "--platform", "tiktok", "--publish-next"]


@pytest.mark.parametrize("outcome", [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 3),
                                     subprocess.TimeoutExpired("main.py", 3600), OSError("no python")])
def test_job_series_publish_never_raises(env, sched, outcome):
    if isinstance(outcome, Exception):
        env["run"].side_effect = outcome
    else:
        env["run"].return_value = outcome
    sched.job_series_publish("kabinet7", "youtube")
    args, kwargs = env["run"].call_args
    assert args[0][-1] == "--publish-next" and kwargs["timeout"] == 3600
    assert kwargs["cwd"] == str(env["root"])


def test_job_pipeline_one_run_per_niche(env, sched, monkeypatch):
    monkeypatch.setenv("SCHEDULER_NICHES", " психология, бизнес ,,")
    monkeypatch.setenv("SCHEDULER_PLATFORM", "youtube")
    sched.job_pipeline()
    runs = sorted(c.args[0][3] for c in env["run"].call_args_list)
    assert runs == ["бизнес", "психология"]
    assert all(c.args[0][-1] == "youtube" for c in env["run"].call_args_list)


def test_job_pipeline_default_niche(env, sched):
    sched.job_pipeline()
    assert env["run"].call_args.args[0][2:4] == ["--niche", sched._NICHE_DEFAULT]


@pytest.mark.parametrize("outcome", [subprocess.CompletedProcess([], 1), subprocess.TimeoutExpired("x", 1),
                                     RuntimeError("boom")])
def test_run_pipeline_and_analytics_swallow_errors(env, sched, outcome):
    if isinstance(outcome, Exception):
        env["run"].side_effect = outcome
    else:
        env["run"].return_value = outcome
    sched._run_pipeline("психология", "tiktok")
    sched.job_analytics()
    assert env["run"].call_args.args[0][1].endswith("analytics_agent.py")


def test_job_housekeeping_swallows_errors(sched, monkeypatch):
    import utils.housekeeping as hk
    monkeypatch.setattr(hk, "run", MagicMock(side_effect=OSError("disk")))
    sched.job_housekeeping()
    assert hk.run.called


# ── ежедневная сводка ────────────────────────────────────────────────────────

def _digest_db(sched, db: Path, with_series: bool):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=2)).isoformat()
    day_start, _ = sched._local_day_utc_range()
    today = (datetime.fromisoformat(day_start) + timedelta(minutes=1)).isoformat()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE pipeline_state (id INTEGER PRIMARY KEY, run_id TEXT, step TEXT, status TEXT, "
                 "payload TEXT, error TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE error_log (id INTEGER PRIMARY KEY, run_id TEXT, step TEXT, error TEXT, "
                 "attempt INTEGER, created_at TEXT)")
    conn.executemany("INSERT INTO pipeline_state (run_id, step, status, payload, created_at) VALUES (?,?,?,?,?)", [
        ("r1", "pipeline", "completed", None, today),
        ("r2", "pipeline", "completed", None, today),
        ("r0", "pipeline", "completed", None, old),
        ("r1", "publisher_agent", "success", json.dumps({"post_id": "yt<1>", "platform": "youtube"}), today),
        ("r2", "publisher_agent", "success", json.dumps({"status": "skipped"}), today),   # dry-run
    ])
    conn.executemany("INSERT INTO error_log (step, error, created_at) VALUES (?,?,?)",
                     [("voice", "x", today), ("voice", "y", old)])
    if with_series:
        conn.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, total_parts INTEGER)")
        conn.execute("CREATE TABLE series_episodes (id INTEGER PRIMARY KEY, series_id INTEGER)")
        conn.executemany("INSERT INTO series (id, total_parts) VALUES (?,?)", [(1, 3), (2, 1)])
        conn.executemany("INSERT INTO series_episodes (series_id) VALUES (?)", [(1,), (2,)])
    conn.commit()
    conn.close()


@pytest.fixture
def digest(env, monkeypatch):
    import agents.notify_agent as na
    import utils.cost_tracker as ct
    send = MagicMock()
    monkeypatch.setattr(na, "send", send)
    monkeypatch.setattr(ct, "format_summary", lambda db: "Расходы: 0 ₽")
    return send


def test_daily_digest_counts_today_only(env, sched, digest):
    _digest_db(sched, env["db"], with_series=True)
    sched.job_daily_digest()
    msg = digest.call_args.args[0]
    assert "Видео создано: <b>2</b> | Ошибок: <b>1</b>" in msg
    assert "Последняя публикация: yt<1> (youtube)" in msg       # dry-run (skipped) пропущен
    assert "Активных серий: <b>1</b>" in msg and "Расходы: 0 ₽" in msg
    assert digest.call_args.kwargs == {"level": "info"}


def test_daily_digest_without_series_table(env, sched, digest):
    _digest_db(sched, env["db"], with_series=False)
    sched.job_daily_digest()
    assert "Активных серий" not in digest.call_args.args[0]


def test_daily_digest_skipped_without_db(env, sched, digest):
    sched.job_daily_digest()
    assert not digest.called


def test_local_day_range_is_one_day_in_utc(sched):
    start, end = sched._local_day_utc_range()
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    assert s.tzinfo is not None and e - s in (timedelta(hours=23), timedelta(hours=24), timedelta(hours=25))
    assert s <= datetime.now(timezone.utc) < e


# ── main: какие задачи регистрируются ────────────────────────────────────────

class _Stop(Exception):
    pass


def _run_main(sched, monkeypatch):
    monkeypatch.setattr(sched, "catch_up_series", MagicMock(return_value=[]))
    monkeypatch.setattr(sched.time, "sleep", MagicMock(side_effect=_Stop))
    with pytest.raises(_Stop):
        sched.main()
    return {(j.at_time, getattr(j.job_func, "func", None)) for j in schedule.get_jobs()}


def test_main_clips_mode_registers_three_runs_and_service_jobs(env, sched, monkeypatch):
    jobs = _run_main(sched, monkeypatch)
    assert jobs == {(dtime(9), sched.job_pipeline), (dtime(14), sched.job_pipeline), (dtime(20), sched.job_pipeline),
                    (dtime(21), sched.job_daily_digest), (dtime(8, 30), sched.job_analytics),
                    (dtime(4), sched.job_housekeeping)}
    assert sched.HEARTBEAT.exists() and sched.catch_up_series.called


def test_main_series_mode_from_env(env, sched, monkeypatch):
    monkeypatch.setenv("SCHEDULER_MODE", "series")
    monkeypatch.setenv("SERIES_PUBLISH_TIME", "18:10")
    jobs = _run_main(sched, monkeypatch)
    assert (dtime(18, 10), sched.job_series_publish) in jobs
    assert not any(f is sched.job_pipeline for _, f in jobs)


def test_main_series_from_configs_wins_over_clips(env, sched, monkeypatch):
    _series(env["root"], "kabinet7", {"enabled": True, "time": "19:00"})
    jobs = _run_main(sched, monkeypatch)
    assert (dtime(19), sched.job_series_publish) in jobs
    assert not any(f is sched.job_pipeline for _, f in jobs)
