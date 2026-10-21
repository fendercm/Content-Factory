"""
Лаборатория сценариев: кейсы и сценарии сериала только текстом — прочитать до того, как платить за кадры.

    py scripts/script_lab.py                       # 4 сценария, сценаристы по очереди: Argon, Astra
    py scripts/script_lab.py --n 6 --writers "gemini:gemini-4-argon,codex:gpt-6-astra"
    py scripts/script_lab.py --same                # все пишет первый сценарист, второй — резерв
    py scripts/script_lab.py --idea "..." --n 3    # варианты по идее автора (панель → Студия)

Сценарии подписаны буквами вслепую; какая модель писала — в ключе output/script_lab/<время>_key.json.
Результат: output/script_lab/<время>.md. Кейсы — по брифу сериала (propose_cases), факты не ищутся,
поэтому в сценариях нет цифр и исследований. Состояние сериала (очередь, темы) не меняется.
"""

import argparse
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from agents import anthology_agent as aa  # noqa: E402
from utils import kie_client  # noqa: E402

DB_PATH = os.getenv("DB_PATH", "pipeline.db")
_tls = threading.local()
_chain_llm = aa._llm


def _llm_by_thread(messages, tier, json_mode, max_tokens, caller):
    """Сценарий пишет модель этого потока (если упала — вторая), остальное — обычная цепочка."""
    if caller != "anthology_episode" or not getattr(_tls, "writers", None):
        return _chain_llm(messages, tier, json_mode, max_tokens, caller)
    last = None
    for spec in _tls.writers:
        try:
            text, _ = kie_client.chat(spec, messages, max_tokens=max_tokens, json_mode=json_mode)
            _tls.answered = spec
            return text
        except Exception as exc:
            last = exc
    raise RuntimeError(f"сценаристы не ответили: {last}")


def _write(series: dict, case: dict, writers: list[str]) -> dict:
    _tls.writers, _tls.answered = writers, writers[0]
    try:
        ep = aa.write_episode(series, case, [], 1)
        return {"case": case, "episode": ep, "model": _tls.answered}
    except Exception as exc:
        return {"case": case, "error": str(exc)[:300], "model": _tls.answered}


def _write_pitch(series: dict, pitch: dict, writers: list[str]) -> dict:
    _tls.writers, _tls.answered = writers, writers[0]
    try:
        ep = aa.write_from_pitch(series, pitch)
        return {"case": ep["case"], "episode": ep, "model": _tls.answered, "pitch_id": pitch["id"]}
    except Exception as exc:
        case = {"title": pitch["title"], **{k: pitch[k] for k in ("client_name", "age", "occupation")}}
        return {"case": case, "error": str(exc)[:300], "model": _tls.answered, "pitch_id": pitch["id"]}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Сценарии сериала текстом, вслепую")
    parser.add_argument("--series", default="kabinet7")
    parser.add_argument("--n", type=int, default=4)
    parser.add_argument("--writers", default="gemini:gemini-4-argon,codex:gpt-6-astra")
    parser.add_argument("--same", action="store_true",
                        help="все сценарии пишет первый из --writers, остальные — резерв (без чередования)")
    parser.add_argument("--idea", default="", help="замысел автора: кейсы строятся на нём")
    parser.add_argument("--pitches", action="store_true",
                        help="только завязки (--n штук) — автор отмечает 👍/👎 в панели")
    parser.add_argument("--from-pitches", default="",
                        help="id завязок через запятую: история → диалог по тактам → критик")
    args = parser.parse_args()

    aa._llm = _llm_by_thread
    series = aa.load_series(args.series)
    writers = [w.strip() for w in args.writers.split(",") if w.strip()]
    stamp = datetime.now().strftime("%Y%m%d_%H%M")

    if args.pitches:
        print(f"Завязки ({args.n})...", flush=True)
        pitches = aa.propose_pitches(series, aa.recent_episodes(series, DB_PATH), args.n, DB_PATH,
                                     idea=args.idea or None)
        aa.save_pitches(series, pitches, stamp, DB_PATH)
        for p in pitches:
            print(f"- {p['title']}: {p['logline']}")
        return

    results = None
    if args.from_pitches:
        ids = [int(x) for x in args.from_pitches.split(",") if x.strip().isdigit()]
        pitches = aa.list_pitches(series["id"], DB_PATH, limit=len(ids), ids=ids)
        if not pitches:
            sys.exit("завязки не найдены")
        print(f"Сценарии по завязкам: {len(pitches)} шт. параллельно...", flush=True)
        with ThreadPoolExecutor(max_workers=len(pitches)) as ex:
            results = list(ex.map(lambda p: _write_pitch(series, p, writers), pitches))
        for r in results:
            if "episode" in r:
                aa.set_pitch(r["pitch_id"], DB_PATH, used=True)
        _save(series, results, stamp, args.idea)
        return
    print(f"Кейсы ({args.n})...", flush=True)
    cases = aa.propose_cases(series, [], aa.recent_episodes(series, DB_PATH), args.n, idea=args.idea or None)

    # Сценаристы по очереди: кейс i пишет writers[i], при сбое — следующий по кругу
    jobs = [(case, writers if args.same else writers[i % len(writers):] + writers[:i % len(writers)])
            for i, case in enumerate(cases)]
    print(f"Сценарии: {len(jobs)} шт. параллельно...", flush=True)
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        results = list(ex.map(lambda job: _write(series, *job), jobs))
    _save(series, results, stamp, args.idea)


def _save(series: dict, results: list[dict], stamp: str, idea: str = "") -> None:
    out_dir = ROOT / "output" / "script_lab"
    out_dir.mkdir(parents=True, exist_ok=True)
    md, key = [f"# Лаборатория сценариев «{series['name']}» — {stamp}\n"], {}
    if idea:
        md.append(f"Идея: {idea}\n")
    for i, r in enumerate(results):
        label = chr(ord("A") + i)
        key[label] = r["model"]
        case = r["case"]
        md.append(f"\n## {label}. {r.get('episode', {}).get('title') or case['title']}\n")
        md.append(f"*Клиент: {case['client_name']}, {case['age']}, {case['occupation']}. "
                  f"Ставки: {case.get('stakes', '—')}*\n")
        if "error" in r:
            md.append(f"\n(не получилось: {r['error']})\n")
            continue
        ep = r["episode"]
        md.append("")
        md += [f"**{line['role']}:** {line['text']}  " for line in ep["lines"]]
        md.append(f"\n_{len(ep['lines'])} реплик, {len(aa.spoken_text(ep).split())} слов_\n")
    path = out_dir / f"{stamp}.md"
    path.write_text("\n".join(md), encoding="utf-8")
    (out_dir / f"{stamp}_key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    # Кейсы и сценарии целиком — чтобы удачный доработать или собрать в видео
    saved = {chr(ord("A") + i): {"case": r["case"], "model": r["model"],
                                 "episode": {k: r["episode"][k] for k in ("title", "lines")} if "episode" in r else None,
                                 **({k: r["episode"][k] for k in ("story", "critic") if k in r["episode"]}
                                    if "episode" in r else {}),
                                 **({"pitch_id": r["pitch_id"]} if "pitch_id" in r else {}),
                                 **({"error": r["error"]} if "error" in r else {})}
             for i, r in enumerate(results)}
    (out_dir / f"{stamp}.json").write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
