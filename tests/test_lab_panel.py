"""Страница лаборатории в админке: новый формат результатов (критик по кругам, реплика ухода, открытия),
старые прогоны без этих полей и «В производство» через POST /api/scripts."""

import json
from pathlib import Path

from tests.test_audit_fixes import _api_check

ROOT = Path(__file__).resolve().parent.parent
BUBLIK = json.loads((ROOT / "config" / "series" / "scripts" / "kabinet7_bublik.json").read_text(encoding="utf-8"))
CASE = {**BUBLIK["case"], "title": "Ошейник"}   # у завязки своё название, у эпизода — своё

# Как пишет scripts/script_lab.py после write_from_pitch: лучший круг — третий
NEW_RUN = {
    "A": {"case": CASE, "model": "gemini:gemini-4-argon", "pitch_id": 7, "story": "Артём вертит ошейник.",
          "episode": {"title": "Бублик", "lines": BUBLIK["lines"]},
          "reveals": ["собаки дома нет", "мама прилетела", "мама знала"],
          "critic": {"score": 8, "failed": ["reveals", "logic"], "fixes": ["реплика 4: сжать"], "drop_line": 4,
                     "rounds": [{"score": 6, "failed": ["turn"], "fixes": [], "drop_line": 2, "words": 137},
                                {"score": 7, "failed": [], "fixes": [], "drop_line": 3, "words": 120},
                                {"score": 8, "failed": ["reveals", "logic"], "fixes": [], "drop_line": 4, "words": 115},
                                {"score": 8, "failed": [], "fixes": [], "drop_line": None, "words": 110}]}},
    "B": {"case": CASE, "model": "codex:gpt-6-astra", "pitch_id": 8, "episode": None, "error": "сценарист молчит"},
}
# Прогон 2026-10-21: критик без drop_line, круги без оценок, открытий нет
OLD_RUN = {"A": {"case": {"title": "Старый", "client_name": "Анна", "age": 30, "occupation": "x"}, "model": "m",
                 "episode": {"title": "Старый", "lines": BUBLIK["lines"][:3]}, "story": "s",
                 "critic": {"score": None, "failed": [], "fixes": [], "rounds": [{"failed": []}]}},
           "B": {"case": {"title": "Совсем старый"}, "episode": {"title": "Без критика", "lines": BUBLIK["lines"][:2]}}}


def _write_runs(tmp_path: Path) -> Path:
    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "20261023_2250.json").write_text(json.dumps(NEW_RUN, ensure_ascii=False), encoding="utf-8")
    (lab / "20261023_2250_key.json").write_text(json.dumps({"A": "gemini:gemini-4-argon"}), encoding="utf-8")
    (lab / "20261021_2325.json").write_text(json.dumps(OLD_RUN, ensure_ascii=False), encoding="utf-8")
    (lab / "20261020_1200.json").write_text("[1, 2]", encoding="utf-8")      # чужой формат — пропускается
    (lab / "20261020_1100.json").write_text("{битый", encoding="utf-8")
    return lab


def test_lab_new_format_old_runs_and_to_production(tmp_path):
    lab = _write_runs(tmp_path)
    out = _api_check("""
        from pathlib import Path
        api.LAB_DIR = Path(%r)
        api.SCRIPTS_DIR = Path(%r)
        out["list"] = client.get("/api/lab").get_json()
        new = client.get("/api/lab/20261023_2250").get_json()
        out["new"] = new
        out["old"] = client.get("/api/lab/20261021_2325").get_json()
        out["foreign"] = client.get("/api/lab/20261020_1200").status_code
        out["broken"] = client.get("/api/lab/20261020_1100").status_code
        it = new["items"][0]
        # ровно то, что отправляет кнопка «В производство»
        body = {"series": "kabinet7", "title": it["title"], "case": it["case"], "lines": it["episode"]["lines"],
                "slug": "lab_20261023_2250_A".lower()}
        r = client.post("/api/scripts", json=body)
        out["save"] = [r.status_code, r.get_json()]
        out["again"] = client.post("/api/scripts", json=body).get_json()
        out["scripts"] = client.get("/api/scripts?series=kabinet7").get_json()
        out["junk_lines"] = client.post("/api/scripts", json={**body, "lines": [{"role": "ВЕРА"}, "x"]}).status_code
        out["bad_age"] = client.post("/api/scripts", json={**body, "case": {**it["case"], "age": "много"}}).status_code
        out["stranger"] = client.post("/api/scripts", json={**body, "lines": [{"role": "ПЁТР", "text": "Я"}]}).status_code
    """ % (str(lab), str(tmp_path / "scripts")), tmp_path)

    assert [l["stamp"] for l in out["list"]] == ["20261023_2250", "20261021_2325"]
    assert out["list"][0]["titles"] == ["Бублик", "Ошейник"] and out["list"][0]["scores"] == [8, None]
    assert out["list"][1]["titles"] == ["Старый", "Без критика"]
    assert out["foreign"] == 422 and out["broken"] == 422

    a, b = out["new"]["items"]
    assert a["label"] == "A" and a["model"] == "gemini:gemini-4-argon" and a["pitch_id"] == 7
    assert a["words"] == sum(len(l["text"].split()) for l in BUBLIK["lines"])
    assert a["reveals"] == ["собаки дома нет", "мама прилетела", "мама знала"]
    c = a["critic"]
    assert c["score"] == 8 and c["drop_line"] == 4 and c["fixes"] == ["реплика 4: сжать"]
    assert c["failed"] == ["reveals", "logic"]
    assert c["failed_text"]["reveals"].startswith("Каждые 2-3 реплики")      # текст пункта из _CRITIC_CHECKS
    assert [(r["score"], r["words"], r["best"]) for r in c["rounds"]] == \
        [(6, 137, False), (7, 120, False), (8, 115, True), (8, 110, False)]   # в работу шёл первый лучший
    assert b["episode"] is None and b["critic"] is None and b["error"] == "сценарист молчит"
    assert b["title"] == "Ошейник" and b["words"] == 0 and b["reveals"] == []

    old_a, old_b = out["old"]["items"]
    assert old_a["critic"]["drop_line"] is None and old_a["critic"]["rounds"] == \
        [{"score": None, "failed": [], "words": None, "best": True}]
    assert old_a["reveals"] == [] and old_a["model"] == "m"
    assert old_b["critic"] is None and old_b["case"] == {"title": "Совсем старый"} and old_b["model"] is None

    assert out["save"] == [201, {"ok": True, "file": "kabinet7_lab_20261023_2250_a.json"}]
    assert out["again"]["file"] == "kabinet7_lab_20261023_2250_a.json"   # повторная отправка не плодит копии
    assert [s["file"] for s in out["scripts"]] == ["kabinet7_lab_20261023_2250_a.json"]
    saved = json.loads((tmp_path / "scripts" / "kabinet7_lab_20261023_2250_a.json").read_text(encoding="utf-8"))
    assert saved["title"] == "Бублик" and saved["case"]["title"] == "Бублик" and saved["case"]["age"] == 32
    assert saved["case"]["signature_detail"] == CASE["signature_detail"]
    assert saved["lines"] == BUBLIK["lines"]                                   # эмоции сохранены
    assert out["junk_lines"] == 400 and out["bad_age"] == 400 and out["stranger"] == 400


def test_lab_page_renders_critic_drop_line_and_reveals():
    """Разметка страницы: номера реплик, подсветка ухода, путь по кругам, открытия, slug варианта."""
    html = (ROOT / "admin_panel.html").read_text(encoding="utf-8")
    assert "function criticPanel(c, words)" in html and "reveals: 'открытия'" in html
    assert "renderLines(it.episode.lines, {numbers: true, drop: c && c.drop_line})" in html
    assert "← здесь уйдут" in html and "it.reveals.length" in html
    assert "slug: `lab_${window._lab.stamp}_${it.label}`.toLowerCase()" in html
