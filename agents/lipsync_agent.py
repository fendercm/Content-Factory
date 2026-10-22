"""
Lipsync Agent — говорящие лица на ключевых репликах эпизода сериала.

Kling AI Avatar (KIE, `kling/ai-avatar-standard`, 720p) получает кадр говорящего и кусок готовой
озвучки этой реплики — губы и голова двигаются в такт речи. ≈8 кредитов ($0.04) за секунду, поэтому
оживляются только ключевые реплики (series["lipsync_lines"] = "key"): первая, поворот (turn_line
раскадровщика) и последняя реплика героев. Остальные лица — кадры с плавным наездом.
InfiniteTalk на KIE 2026-10-21 отвечал 500 на любые входы — не используем.

Сбой липсинка не роняет эпизод: реплика остаётся статичным кадром.

API:
    key_lines(lines, plan) → [индексы реплик]
    render(series, plan, shots, dialogue, out_dir) → {индекс реплики: путь к mp4}
    apply(assignment, shots, clips) → assignment, где у оживлённых реплик свой кадр-клип
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

MODEL = "kling/ai-avatar-standard"
FACE_SLOTS = ("client_open", "client_talk", "client_turn", "vera_listen", "vera_ask")
TAIL = 0.15  # секунд звука после реплики — чтобы губы не обрезало на последнем слове.
# Начало — ровно со старта реплики: клип встаёт в монтаж с этого же момента, иначе губы опередят звук


def key_lines(lines: list[dict], plan: dict) -> list[int]:
    """Первая реплика, поворот и последняя реплика героев (без рассказчика), только кадры с одним лицом."""
    spoken = [i for i, l in enumerate(lines) if l["role"] != "РАССКАЗЧИК"]
    if not spoken:
        return []
    picks = [spoken[0], plan.get("turn_line"), spoken[-1]]
    assignment = plan.get("assignment") or []
    out = []
    for i in picks:
        if isinstance(i, int) and 0 <= i < len(lines) and i not in out and lines[i]["role"] != "РАССКАЗЧИК":
            if i < len(assignment) and assignment[i].get("slot") in FACE_SLOTS:
                out.append(i)
    return out


def _cut_audio(src: str, start: float, end: float, dest: Path) -> Path:
    from utils.ffmpeg import run_ffmpeg
    run_ffmpeg(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-to", f"{end + TAIL:.3f}",
                "-i", src, "-ac", "1", "-ar", "44100", "-b:a", "128k", str(dest)], timeout=60, what="lipsync audio")
    return dest


def _prompt(series: dict, plan: dict, line: dict) -> str:
    who = (series["heroine"]["visual_en"] if line["role"] == series["heroine"]["role"]
           else plan.get("client_visual_en", "the client"))
    mood = f" Mood: {line['emotion']}." if line.get("emotion") else ""
    return (f"{who} speaks in a quiet psychologist's office, natural subtle head movement and blinking, "
            f"static camera, keep the face and clothing exactly as in the image.{mood}")


def render(series: dict, plan: dict, shots: list[dict], dialogue: dict, out_dir: Path) -> dict[int, str]:
    if series.get("lipsync_lines", "key") != "key":
        return {}
    from utils import kie_client

    out_dir = Path(out_dir)
    lines = dialogue["lines"]
    targets = key_lines(lines, plan)
    if not targets:
        return {}

    def one(i: int) -> tuple[int, str | None, float]:
        line = lines[i]
        shot = shots[plan["assignment"][i]["shot"]]
        try:
            audio = _cut_audio(dialogue["audio_path"], line["start"], line["end"], out_dir / f"lip_{i:02d}.mp3")
            image_url = shot.get("image_url") or kie_client.upload_file(shot["image_path"], f"content-factory/{series['id']}")
            audio_url = kie_client.upload_file(audio, f"content-factory/{series['id']}")
            res = kie_client.run_task(MODEL, {"image_url": image_url, "audio_url": audio_url,
                                              "prompt": _prompt(series, plan, line)}, 1500, retries=2)
            path = kie_client.download(res["urls"][0], out_dir / f"lip_{i:02d}.mp4")
            return i, str(path), res["credits"] or 0
        except Exception as exc:
            logger.warning("Lipsync: реплика %d не оживлена (%s) — статичный кадр", i, str(exc)[:200])
            return i, None, 0

    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        results = list(ex.map(one, targets))
    clips = {i: p for i, p, _ in results if p}
    logger.info("Lipsync: оживлено %d из %d ключевых реплик (%s кр.)", len(clips), len(targets),
                sum(c for _, _, c in results))
    return clips


def apply(assignment: list[dict], shots: list[dict], clips: dict[int, str]) -> list[dict]:
    """Оживлённой реплике — свой кадр с клипом липсинка (тот же снимок, другое видео)."""
    out = [dict(a) for a in assignment]
    for i, path in sorted(clips.items()):
        base = shots[out[i]["shot"]]
        shots.append({**{k: v for k, v in base.items() if k not in ("video_path", "engine")},
                      "slot": f"lipsync_{i}", "video_path": path, "engine": "lipsync", "lipsync": True})
        out[i] = {**out[i], "shot": len(shots) - 1, "slot": f"lipsync_{i}", "clip": True}
    return out

