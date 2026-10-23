"""Деньги на KIE: лимит трат процесса, траты и лимит лаборатории, проверка баланса до кадров эпизода.
Сети нет: requests, balance и LLM подменены."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents import anthology_agent as aa
from tests.test_audit_fixes import _api_check
from utils import cost_tracker, kie_client, series_budget

ROOT = Path(__file__).resolve().parent.parent
SERIES = aa.load_series("kabinet7")


@pytest.fixture(autouse=True)
def _reset_limit():
    yield
    kie_client.set_spend_limit(None)
    cost_tracker.set_run_id("")


@pytest.fixture
def kie_ready(monkeypatch):
    """Ключ и провайдер «включены», запись в costs — в список, а не в pipeline.db."""
    monkeypatch.setenv("KIE_API_KEY", "test-key")
    monkeypatch.setattr("utils.providers.is_enabled", lambda name: True)
    logged = []
    monkeypatch.setattr(cost_tracker, "log_cost", lambda *a, **k: logged.append((cost_tracker._run_id, a, k)))
    return logged


# ── kie_client: лимит процесса ────────────────────────────────────────────────

def test_spend_limit_blocks_new_calls_before_request(kie_ready):
    resp = MagicMock()
    resp.json.return_value = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                              "credits_consumed": 30}   # $0.15
    kie_client.set_spend_limit(0.1)
    with patch.object(kie_client.requests, "post", return_value=resp) as post:
        assert kie_client.chat("codex:gpt-6-astra", [{"role": "user", "content": "x"}])[0] == "ok"
        assert kie_client.spent_usd() == pytest.approx(0.15) and kie_client.limit_reached()
        with pytest.raises(kie_client.KieLimitError, match="лимит"):
            kie_client.chat("codex:gpt-6-astra", [{"role": "user", "content": "x"}])
        with pytest.raises(kie_client.KieLimitError):
            kie_client.run_task("nano-banana-2", {"prompt": "x"})   # не временный сбой — без повторов
    assert post.call_count == 1                      # заблокированные вызовы в сеть не ушли
    assert len(kie_ready) == 1                       # трата записана в costs


def test_no_limit_by_default_and_reset(kie_ready):
    kie_client.set_spend_limit(None)
    kie_client._track("task", 2000, "kling")         # $10
    assert not kie_client.limit_reached()
    kie_client.check_spend_limit("chat")
    kie_client.set_spend_limit(0)
    assert kie_client.spent_usd() == 0 and kie_client.limit_reached()   # лимит 0 — ни одного платного вызова


# ── Лаборатория: run_id, сумма, --max-usd ─────────────────────────────────────

def _load_lab():
    spec = importlib.util.spec_from_file_location("script_lab_under_test", ROOT / "scripts" / "script_lab.py")
    lab = importlib.util.module_from_spec(spec)
    cwd = os.getcwd()
    try:
        with patch("dotenv.load_dotenv"):           # настоящий .env в процесс тестов не грузим
            spec.loader.exec_module(lab)
    finally:
        os.chdir(cwd)
    return lab


@pytest.fixture
def lab(monkeypatch, tmp_path, kie_ready):
    lab = _load_lab()
    monkeypatch.setattr(aa, "_llm", aa._llm)          # main() подменяет aa._llm — вернуть после теста
    monkeypatch.setattr(lab, "ROOT", tmp_path)
    monkeypatch.setattr(lab, "_chain_llm", lambda *a, **k: "{}")   # бесплатная цепочка (критик)
    monkeypatch.setattr(aa, "recent_episodes", lambda *a, **k: [])
    totals = {}

    def log_cost(agent, provider, operation, units, unit_cost_usd=None, run_id=None, db_path=None):
        rid = run_id or cost_tracker._run_id
        totals[rid] = totals.get(rid, 0.0) + units * unit_cost_usd

    monkeypatch.setattr(cost_tracker, "log_cost", log_cost)
    monkeypatch.setattr(cost_tracker, "total_for_run", lambda rid, db=None: totals.get(rid, 0.0))
    lab.totals = totals
    return lab


def _case(title):
    return {"title": title, "client_name": "Игорь", "age": 43, "occupation": "инженер", "stakes": "—"}


def test_lab_run_id_cost_and_limit(lab, monkeypatch, capsys):
    """Лимит $0.20, сценарий стоит $0.15: первый успевает целиком, второму критик уже не положен —
    ошибка «лимит», черновик в json; сумма прогона в консоли и в json под «_run»."""
    monkeypatch.setattr(aa, "propose_cases", lambda *a, **k: [_case("Первый"), _case("Второй")])

    def chat(spec, messages, max_tokens=4000, json_mode=False):
        kie_client.check_spend_limit("chat")
        kie_client._track("chat", 30, spec)          # $0.15 → costs через log_cost
        return "СЦЕНАРИЙ", 30

    serial = threading.Lock()                        # по очереди — порядок трат детерминирован

    def write_episode(series, case, facts, no):
        with serial:
            aa._llm([{"role": "user", "content": "x"}], "premium", False, 100, "anthology_episode")
            try:
                aa._llm([{"role": "user", "content": "x"}], "critic", True, 100, "anthology_critic")
            except Exception:
                pass                                 # критик не роняет сценарий — как в anthology_agent
            return {"title": case["title"], "lines": [{"role": "ИГОРЬ", "text": "Привет"}]}

    monkeypatch.setattr(kie_client, "chat", chat)
    monkeypatch.setattr(aa, "write_episode", write_episode)
    monkeypatch.setattr(sys, "argv", ["script_lab.py", "--n", "2", "--same", "--max-usd", "0.2"])
    lab.main()

    out = capsys.readouterr().out
    (saved_path,) = (lab.ROOT / "output" / "script_lab").glob("*[0-9].json")
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    run = saved["_run"]
    assert run["run_id"] == f"lab_{saved_path.stem}" and list(lab.totals) == [run["run_id"]]
    assert run["cost_usd"] == pytest.approx(0.30) and run["max_usd"] == 0.2 and run["limit_hit"]
    assert "error" not in saved["A"] and saved["A"]["episode"]["title"] == "Первый"
    assert "лимит" in saved["B"]["error"] and saved["B"]["episode"]["lines"]   # черновик сохранён
    assert "Потрачено на KIE: $0.30 из лимита $0.20 — лимит исчерпан" in out
    md = saved_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "не получилось: лимит" in md and "Потрачено на KIE: $0.30" in md


def test_lab_zero_limit_starts_nothing(lab, monkeypatch, capsys):
    def propose_cases(*a, **k):
        aa._llm([{"role": "user", "content": "x"}], "premium", True, 100, "anthology_cases")

    chain = MagicMock()
    monkeypatch.setattr(lab, "_chain_llm", chain)
    monkeypatch.setattr(aa, "propose_cases", propose_cases)
    monkeypatch.setattr(sys, "argv", ["script_lab.py", "--n", "2", "--max-usd", "0"])
    with pytest.raises(kie_client.KieLimitError):
        lab.main()
    assert not chain.called                          # даже бесплатная цепочка не дёргается: могла бы уйти в KIE
    assert "Потрачено на KIE: $0.00 из лимита $0.00" in capsys.readouterr().out


def test_lab_without_limit_tags_costs_with_run_id(lab, monkeypatch, capsys):
    monkeypatch.setattr(aa, "propose_pitches", lambda *a, **k: (kie_client._track("chat", 4, "argon"), [])[1])
    monkeypatch.setattr(aa, "save_pitches", lambda *a, **k: [])
    monkeypatch.setattr(sys, "argv", ["script_lab.py", "--pitches", "--n", "3"])
    lab.main()
    (rid,) = lab.totals
    assert rid.startswith("lab_") and lab.totals[rid] == pytest.approx(0.02)
    assert f"Потрачено на KIE: $0.02, run_id {rid}" in capsys.readouterr().out


# ── Панель: max_usd в задачи лаборатории, «_run» не вариант ────────────────────

def test_api_passes_max_usd_and_skips_run_key(tmp_path):
    lab_dir = tmp_path / "lab"
    lab_dir.mkdir()
    (lab_dir / "20261023_1200.json").write_text(json.dumps({
        "A": {"case": {"title": "Кейс"}, "model": "m", "episode": {"title": "Серия", "lines": []}},
        "_run": {"run_id": "lab_20261023_1200", "cost_usd": 0.31, "max_usd": 0.5, "limit_hit": False}},
        ensure_ascii=False), encoding="utf-8")
    out = _api_check("""
        api.LAB_DIR = __import__("pathlib").Path(%r)
        series = {"id": "kabinet7"}
        out["lab"] = api._job_command("lab", series, {"n": 2, "max_usd": "0.5"})[3:]
        out["plain"] = api._job_command("lab", series, {"n": 2})[3:]
        out["pitches"] = api._job_command("pitches", series, {"n": 5, "max_usd": 1})[-2:]
        out["from"] = api._job_command("lab", series, {"pitch_ids": [3], "max_usd": 0})[-2:]
        out["bad"] = []
        for bad in ("abc", -1, 99, "nan"):
            try:
                api._job_command("lab", series, {"max_usd": bad})
            except ValueError as exc:
                out["bad"].append(str(exc))
        out["list"] = client.get("/api/lab").get_json()
        out["get"] = client.get("/api/lab/20261023_1200").get_json()
    """ % str(lab_dir), tmp_path)
    assert out["lab"][-2:] == ["--max-usd", "0.5"]
    assert "--max-usd" not in out["plain"]
    assert out["pitches"] == ["--max-usd", "1"] and out["from"] == ["--max-usd", "0"]
    assert len(out["bad"]) == 4 and all("max_usd" in e for e in out["bad"])
    assert out["list"] == [{"stamp": "20261023_1200", "count": 1, "titles": ["Серия"], "cost_usd": 0.31}]
    assert [i["label"] for i in out["get"]["items"]] == ["A"] and out["get"]["run"]["cost_usd"] == 0.31


# ── Эпизод: оценка и баланс до кадров ─────────────────────────────────────────

PLAN = {"shots": [{"slot": f"s{i}"} for i in range(8)]}


def test_episode_estimate_from_config():
    need, detail = series_budget.estimate_episode_usd(SERIES, PLAN)
    # кадры (8 + 1) × 0.04 + Wan 3 × 0.10 + липсинк 15 × 0.04, запас 30%
    assert need == pytest.approx(round((9 * 0.04 + 3 * 0.10 + 15 * 0.04) * 1.3, 2))
    assert "кадры 9" in detail and "Wan 3" in detail and "Kling" not in detail
    kling, _ = series_budget.estimate_episode_usd({**SERIES, "animated_key": 2}, PLAN)
    assert kling == pytest.approx(need + round(2 * 0.35 * 1.3, 2), abs=0.011)
    ready, _ = series_budget.estimate_episode_usd(SERIES, PLAN, scenes_ready=True)   # resume: только липсинк
    assert ready == pytest.approx(round(15 * 0.04 * 1.3, 2))


def test_check_balance_stops_or_warns(caplog):
    with patch.object(kie_client, "balance", return_value=100.0):          # 100 кр. = $0.50
        with pytest.raises(series_budget.BudgetError, match=r"\$0\.50.*пополните"):
            series_budget.check_balance(SERIES, PLAN)
    with patch.object(kie_client, "balance", return_value=1000.0):
        assert series_budget.check_balance(SERIES, PLAN)["balance_usd"] == 5.0
    with patch.object(kie_client, "balance", side_effect=kie_client.KieError("KIE выключен")):
        assert series_budget.check_balance(SERIES, PLAN)["balance_usd"] is None
    assert "Баланс KIE не узнать" in caplog.text


_PIPELINE_HARNESS = """
import json, os, sys
from unittest.mock import MagicMock, patch
sys.path.insert(0, %(root)r)
sys.argv = ["main.py"]
os.environ["DB_PATH"] = %(db)r
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ZERNIO_API_KEY", "KIE_API_KEY"):
    os.environ[_k] = ""  # до load_dotenv в main: настоящие ключи из .env не подхватятся (см. conftest.py)
import main
from agents import dialogue_voice, scene_agent
from utils import kie_client
script = os.path.join(%(root)r, "config", "series", "scripts", "kabinet7_bublik.json")
out = {}
for name, balance in (("poor", {"return_value": 50.0}), ("unknown", {"side_effect": RuntimeError("сеть")})):
    voice = MagicMock(side_effect=RuntimeError("озвучка — дальше не идём"))
    with patch("agents.safety_agent.check", return_value={"safe": True}), \\
         patch("agents.anthology_agent.plan_shots", return_value={"shots": [{"slot": "wide"}] * 8}), \\
         patch.object(kie_client, "balance", **balance), \\
         patch.object(dialogue_voice, "synthesize", voice), \\
         patch.object(scene_agent, "render") as render, \\
         patch.object(main.time, "sleep"), patch.object(main, "_alert") as alert:
        try:
            main.run_series_pipeline("kabinet7", "youtube", False, script_file=script)
        except SystemExit as exc:
            out[name + "_exit"] = exc.code
        out[name + "_voice"] = voice.called
        out[name + "_render"] = render.called
        out[name + "_alerts"] = [c.args[1] for c in alert.call_args_list]
open(%(result)r, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False))
"""


def test_series_pipeline_checks_balance_before_paid_steps(tmp_path):
    result = tmp_path / "r.json"
    code = _PIPELINE_HARNESS % {"root": str(ROOT), "db": str(tmp_path / "p.db"), "tmp": str(tmp_path),
                                "result": str(result)}
    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, cwd=str(tmp_path),
                          timeout=300)
    assert result.exists(), proc.stderr.decode("utf-8", "replace")[-3000:]
    out = json.loads(result.read_text(encoding="utf-8"))
    # Денег мало ($0.25): стоп до озвучки и кадров, алерт с понятной причиной
    assert out["poor_exit"] == 1 and not out["poor_voice"] and not out["poor_render"]
    assert len(out["poor_alerts"]) == 1 and "пополните баланс" in out["poor_alerts"][0]
    assert "$0.25" in out["poor_alerts"][0]
    # Баланс не узнать: предупреждение и дальше — до озвучки (она в тесте падает нарочно)
    assert out["unknown_voice"] and not out["unknown_render"]
    assert "пополните" not in " ".join(out["unknown_alerts"])
