"""Студия: видео на заказ — параметры, сценарий, раскадровка, API заказов (без сети и KIE)."""

import base64
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from agents import order_agent as oa

ROOT = Path(__file__).resolve().parent.parent


def test_new_order_defaults_and_param_validation():
    order, errors = oa.new_order({"brief": "Реклама кофейни", "duration": 60, "aspect": "16:9"})
    assert not errors and order["duration"] == 60 and order["aspect"] == "16:9" and order["status"] == "draft"
    assert oa._ID_RE.match(order["id"])
    assert oa.apply_params(order, {"duration": 45, "aspect": "4:3", "style": "anime"}) == ["aspect", "duration"]
    assert order["style"] == "anime"


def test_parse_lines_and_normalize_script_assigns_voices():
    order, _ = oa.new_order({"voice": "male", "duration": 30})
    lines = oa.parse_lines("ДИКТОР: [бодро] Утро начинается с кофе.\nАННА: Мне двойной!\nмусор без роли\nОЛЕГ: [смеясь] А мне — второй в подарок.")
    assert [l["role"] for l in lines] == ["ДИКТОР", "АННА", "ОЛЕГ"] and lines[0]["emotion"] == "бодро"
    script = oa.normalize_script({"title": "Кофе", "lines": lines, "characters": [
        {"role": "АННА", "gender": "female", "visual_en": "a young woman", "voice": "Hacker"},
        {"role": "ЛИШНИЙ", "gender": "male"}]}, order)
    chars = {c["role"]: c for c in script["characters"]}
    assert set(chars) == {"ДИКТОР", "АННА", "ОЛЕГ"}                      # лишний убран, недостающие добавлены
    assert chars["ДИКТОР"]["voice"] == oa.NARRATOR_VOICE["male"]          # голос диктора — из заказа
    assert chars["АННА"]["voice"] in oa.VOICES["female"]                  # неизвестный голос заменён
    assert script["words"] == 12 and script["warnings"]                   # 12 слов на 30 с — коротко


def test_fix_shots_covers_all_lines_and_assignment():
    order, _ = oa.new_order({"files": []})
    order["files"] = ["cup.jpg"]
    script = {"characters": [{"role": "АННА"}, {"role": "ДИКТОР"}],
              "lines": [{"role": "ДИКТОР", "text": "a"}, {"role": "АННА", "text": "b"}, {"role": "АННА", "text": "c"},
                        {"role": "ДИКТОР", "text": "d"}]}
    shots = oa.fix_shots([{"first_line": 0, "last_line": 0, "characters": ["КТО-ТО"], "asset": "cup.jpg"},
                          {"first_line": 5, "last_line": 1, "characters": ["анна"], "face_speaks": "АННА"},
                          {"first_line": 2, "last_line": 2, "asset": "nope.jpg"}], script, order)
    assert [(s["first_line"], s["last_line"]) for s in shots] == [(0, 0), (1, 1), (2, 3)]   # без дыр до конца
    assert shots[0]["characters"] == [] and shots[0]["asset"] == "cup.jpg" and shots[2]["asset"] is None
    assert shots[1]["characters"] == ["АННА"] and shots[1]["face_speaks"] == "АННА"
    assert [a["shot"] for a in oa.assignment_for(shots, 4)] == [0, 1, 2, 2]
    timed = [{"start": 0, "end": 1}, {"start": 1, "end": 4}, {"start": 4, "end": 5}, {"start": 5, "end": 6}]
    assert oa.lipsync_lines(shots, script["lines"], timed) == [1]


def test_estimate_grows_with_animation_and_lipsync():
    cheap, _ = oa.new_order({"duration": 30, "animate": "none", "lipsync": False})
    rich, _ = oa.new_order({"duration": 30, "animate": "more", "lipsync": True, "mode": "scene"})
    assert 0 < oa.estimate_usd(cheap) < oa.estimate_usd(rich) < 3


_API = """
import base64, json, os, sys
from pathlib import Path
sys.path.insert(0, %(root)r)
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ZERNIO_API_KEY", "KIE_API_KEY"):
    os.environ[_k] = ""
from agents import order_agent
order_agent.ORDERS_DIR = Path(%(tmp)r) / "orders"
import utils.api_server as api
c = api.app.test_client(); h = {"Host": "localhost"}
out = {}
r = c.post("/api/orders", json={"brief": "Реклама кофейни", "duration": 30}, headers=h)
oid = r.get_json()["id"]; out["create"] = r.status_code
out["build_without_script"] = c.post("/api/jobs", json={"type": "order_build", "order": oid}, headers=h).status_code
r = c.put(f"/api/orders/{oid}", json={"script_text": "ДИКТОР: [тепло] Кофе с утра.\\nАННА: Второй в подарок!"}, headers=h)
o = r.get_json(); out["status"] = o["status"]; out["roles"] = [x["role"] for x in o["script"]["characters"]]
out["bad_text"] = c.put(f"/api/orders/{oid}", json={"script_text": "просто текст"}, headers=h).status_code
png = base64.b64encode(b"\\x89PNG\\r\\n\\x1a\\n" + b"0" * 64).decode()
out["upload"] = c.post(f"/api/orders/{oid}/files", json={"name": "../evil cup.png", "data": png}, headers=h).get_json()
out["upload_exe"] = c.post(f"/api/orders/{oid}/files", json={"name": "x.exe", "data": png}, headers=h).status_code
name = out["upload"]["name"]
out["file"] = c.get(f"/api/orders/{oid}/file/{name}", headers=h).status_code
out["file_other"] = c.get(f"/api/orders/{oid}/file/order.json", headers=h).status_code
out["video"] = c.get(f"/api/orders/{oid}/video", headers=h).status_code
cmd = api._job_command("order_build", None, {"order": oid})
out["cmd"] = cmd[-4:]
out["list"] = [x["id"] == oid for x in c.get("/api/orders", headers=h).get_json()]
out["bad_id"] = c.get("/api/orders/..%%2F..", headers=h).status_code
out["delete"] = c.post(f"/api/orders/{oid}/delete", json={}, headers=h).status_code
out["after"] = c.get(f"/api/orders/{oid}", headers=h).status_code
open(%(result)r, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False))
"""


def test_orders_api_flow(tmp_path):
    result = tmp_path / "r.json"
    code = _API % {"root": str(ROOT), "tmp": str(tmp_path), "result": str(result)}
    proc = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, cwd=str(tmp_path),
                          timeout=120)
    assert result.exists(), proc.stderr.decode("utf-8", "replace")[-3000:]
    out = json.loads(result.read_text(encoding="utf-8"))
    assert out["create"] == 201 and out["build_without_script"] == 400
    assert out["status"] == "script_ready" and out["roles"] == ["ДИКТОР", "АННА"]
    assert out["bad_text"] == 400
    assert out["upload"]["name"] == "evil_cup.png" and out["upload_exe"] == 400   # имя очищено, не картинка — отказ
    assert out["file"] == 200 and out["file_other"] == 404 and out["video"] == 404
    assert out["cmd"][-3:] == [out["cmd"][-3], "--step", "build"] and out["cmd"][0] == "--order"
    assert out["list"] == [True] and out["bad_id"] == 404
    assert out["delete"] == 200 and out["after"] == 404
