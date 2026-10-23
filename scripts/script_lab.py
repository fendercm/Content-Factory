"""
Лаборатория сценариев: кейсы и сценарии сериала только текстом — прочитать до того, как платить за кадры.

    py scripts/script_lab.py                       # 4 сценария, сценаристы по очереди: Argon, Astra
    py scripts/script_lab.py --n 6 --writers "gemini:gemini-4-argon,codex:gpt-6-astra"
    py scripts/script_lab.py --same                # все пишет первый сценарист, второй — резерв
    py scripts/script_lab.py --idea "..." --n 3    # варианты по идее автора (панель → Студия)
    py scripts/script_lab.py --max-usd 0.5         # траты KIE прогона не больше $0.50

Сценарии подписаны буквами вслепую; какая модель писала — в ключе output/script_lab/<время>_key.json.
Результат: output/script_lab/<время>.md. Кейсы — по брифу сериала (propose_cases), факты не ищутся,
поэтому в сценариях нет цифр и исследований. Состояние сериала (очередь, темы) не меняется.

Траты: у прогона свой run_id lab_<время> в таблице costs; сумма печатается в конце и пишется в
<время>.json под ключом "_run". --max-usd: когда траты дошли до лимита, новые вызовы LLM не стартуют —
недописанные сценарии получают ошибку «лимит», готовые сохраняются.
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
from utils import cost_tracker, kie_client  # noqa: E402

DB_PATH = os.getenv("DB_PATH", "pipeline.db")
_tls = threading.local()
_chain_llm = aa._llm


def _limit_hit(exc: Exception) -> Exception:
    _tls.limited = str(exc)
    return exc


def _llm_by_thread(messages, tier, json_mode, max_tokens, caller):
    """Сценарий пишет модель этого потока (если упала — вторая), остальное — обычная цепочка.
    Лимит --max-usd исчерпан — никаких вызовов: иначе цепочка ушла бы дальше по резервам."""
    try:
        kie_client.check_spend_limit(caller)
    except kie_client.KieLimitError as exc:
        raise _limit_hit(exc)
    if caller != "anthology_episode" or not getattr(_tls, "writers", None):
        return _chain_llm(messages, tier, json_mode, max_tokens, caller)
    last = None
    for spec in _tls.writers:
        try:
            text, _ = kie_client.chat(spec, messages, max_tokens=max_tokens, json_mode=json_mode)
            _tls.answered = spec
            return text
        except kie_client.KieLimitError as exc:
            raise _limit_hit(exc)
        except Exception as exc:
            last = exc
    raise RuntimeError(f"сценаристы не ответили: {last}")


def _limited(result: dict) -> dict:
    """Сценарий упёрся в лимит — ошибка «лимит», даже если дошёл до конца: ошибку критика write_*
    глотают, и сценарий без проверки выглядел бы готовым. Черновик остаётся в json."""
    hit = getattr(_tls, "limited", None)
    if hit and "error" not in result:
        result["error"] = hit[:300]
    elif hit and "лимит" not in result["error"]:
        result["error"] = f"{hit} ({result['error']})"[:300]
    return result


def _write(series: dict, case: dict, writers: list[str]) -> dict:
    _tls.writers, _tls.answered, _tls.limited = writers, writers[0], None
    try:
        ep = aa.write_episode(series, case, [], 1)
        return _limited({"case": case, "episode": ep, "model": _tls.answered})
    except Exception as exc:
        return _limited({"case": case, "error": str(exc)[:300], "model": _tls.answered})


def _write_pitch(series: dict, pitch: dict, writers: list[str]) -> dict:
    _tls.writers, _tls.answered, _tls.limited = writers, writers[0], None
    try:
        ep = aa.write_from_pitch(series, pitch)
        return _limited({"case": ep["case"], "episode": ep, "model": _tls.answered, "pitch_id": pitch["id"]})
    except Exception as exc:
        case = {"title": pitch["title"], **{k: pitch[k] for k in ("client_name", "age", "occupation")}}
        return _limited({"case": case, "error": str(exc)[:300], "model": _tls.answered, "pitch_id": pitch["id"]})


def _run_cost(run_id: str) -> float:
    """Траты прогона: costs по run_id, а если запись в БД не прошла — счётчик kie_client."""
    return max(cost_tracker.total_for_run(run_id, DB_PATH), kie_client.spent_usd())


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
    parser.add_argument("--max-usd", type=float, default=None,
                        help="лимит трат KIE на прогон в $: дальше платные вызовы не стартуют")
    args = parser.parse_args()
    if args.max_usd is not None and args.max_usd < 0:
        parser.error("--max-usd не может быть отрицательным")

    aa._llm = _llm_by_thread
    series = aa.load_series(args.series)
    writers = [w.strip() for w in args.writers.split(",") if w.strip()]
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    run_id = f"lab_{stamp}"
    cost_tracker.set_run_id(run_id)
    kie_client.set_spend_limit(args.max_usd)
    try:
        _run(args, series, writers, stamp, run_id)
    finally:
        print(f"Потрачено на KIE: {_cost_text(run_id, args.max_usd)}, run_id {run_id}", flush=True)


def _cost_text(run_id: str, max_usd: float | None) -> str:
    text = f"${_run_cost(run_id):.2f}"
    if max_usd is not None:
        text += f" из лимита ${max_usd:.2f}" + (" — лимит исчерпан" if kie_client.limit_reached() else "")
    return text


def _run(args, series: dict, writers: list[str], stamp: str, run_id: str) -> None:
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
            if "episode" in r and "error" not in r:
                aa.set_pitch(r["pitch_id"], DB_PATH, used=True)
        _save(series, results, stamp, args.idea, run_id, args.max_usd)
        return
    print(f"Кейсы ({args.n})...", flush=True)
    cases = aa.propose_cases(series, [], aa.recent_episodes(series, DB_PATH), args.n, idea=args.idea or None)

    # Сценаристы по очереди: кейс i пишет writers[i], при сбое — следующий по кругу
    jobs = [(case, writers if args.same else writers[i % len(writers):] + writers[:i % len(writers)])
            for i, case in enumerate(cases)]
    print(f"Сценарии: {len(jobs)} шт. параллельно...", flush=True)
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        results = list(ex.map(lambda job: _write(series, *job), jobs))
    _save(series, results, stamp, args.idea, run_id, args.max_usd)


def _save(series: dict, results: list[dict], stamp: str, idea: str = "", run_id: str = "",
          max_usd: float | None = None) -> None:
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
    md.append(f"\n_Потрачено на KIE: {_cost_text(run_id, max_usd)}_\n")
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
    # Служебный ключ "_run" — не вариант: панель (api_server /api/lab) его пропускает
    saved["_run"] = {"run_id": run_id, "cost_usd": round(_run_cost(run_id), 4), "max_usd": max_usd,
                     "limit_hit": kie_client.limit_reached()}
    (out_dir / f"{stamp}.json").write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
