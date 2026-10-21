"""
Series Assembly — монтаж эпизода сериала (1080x1920, H.264).

Таймлайн — монтаж по говорящему: у каждой реплики свой кадр (assignment из
anthology_agent.assign_shots, тайминги — из dialogue_voice); соседние одинаковые кадры
склеиваются, последний длится до конца речи + хвост. Статичные кадры — плавный наезд
(Ken Burns); анимированный клип (5 с) идёт один раз — на реплике с clip=True: длиннее
отрезка → обрезка, короче → лёгкое замедление (до 1.2x) и удержание последнего кадра.

Звук: диалог (loudnorm −16 LUFS) + музыка, приглушаемая под голос (sidechain).
Субтитры: ASS из subtitle_agent.write_dialogue_ass (стиль файла, без force_style).

API:
    build_timeline(assignment, lines, speech_end, tail) → [{"index", "start", "duration", "clip"}]
    assemble(shots, dialogue, subtitle_path, out_path, assignment, music_path?) → {"video_path", "duration", ...}
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

W, H, FPS = 1080, 1920, 30
MIN_SHOT = 1.0      # секунд — короче кадр не смотрится, сливаем с соседним
TAIL = 0.8          # хвост после последней реплики
MAX_SLOWDOWN = 1.2  # насколько можно замедлить 5-секундный клип
GRADE = "eq=saturation=1.05:contrast=1.03"


def _ff(args: list[str], what: str, timeout: int = 600) -> None:
    from utils.ffmpeg import run_ffmpeg
    run_ffmpeg(["ffmpeg", "-y", "-v", "error", *args], timeout=timeout, what=what)


def _duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                         capture_output=True, text=True, timeout=60)
    return float(json.loads(out.stdout)["format"]["duration"])


def build_timeline(assignment: list[dict], lines: list[dict], speech_end: float,
                   tail: float = TAIL) -> list[dict]:
    """Кадр на каждую реплику (assignment[i] — для lines[i]): одинаковые соседние кадры склеиваются,
    слишком короткие отдают время предыдущему (первый — следующему)."""
    total = speech_end + tail
    segs: list[dict] = []
    for i, a in enumerate(assignment[:len(lines)]):
        if segs and segs[-1]["index"] == a["shot"]:
            segs[-1]["clip"] = segs[-1]["clip"] or bool(a.get("clip"))
            continue
        segs.append({"index": a["shot"], "start": 0.0 if i == 0 else float(lines[i]["start"]),
                     "clip": bool(a.get("clip"))})
    for i, seg in enumerate(segs):
        end = segs[i + 1]["start"] if i + 1 < len(segs) else total
        seg["duration"] = max(end - seg["start"], 0.0)

    merged: list[dict] = []
    for seg in segs:
        if merged and (seg["duration"] < MIN_SHOT or merged[-1]["index"] == seg["index"]):
            merged[-1]["duration"] += seg["duration"]
            if merged[-1]["index"] == seg["index"]:
                merged[-1]["clip"] = merged[-1]["clip"] or seg["clip"]
        else:
            merged.append(dict(seg))
    if len(merged) > 1 and merged[0]["duration"] < MIN_SHOT:
        merged[1]["start"] = 0.0
        merged[1]["duration"] += merged[0]["duration"]
        merged.pop(0)
    return merged


def _render_still(image: str, duration: float, out: str, zoom_in: bool) -> None:
    frames = max(int(round(duration * FPS)), 1)
    step = 0.08 / frames
    z = f"min(1+{step:.6f}*on,1.08)" if zoom_in else f"max(1.08-{step:.6f}*on,1.0)"
    vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
          f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
          f"{GRADE},format=yuv420p")
    _ff(["-loop", "1", "-i", image, "-vf", vf, "-frames:v", str(frames), "-r", str(FPS),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", out], "series still")


def _render_clip(video: str, duration: float, out: str, slowdown: bool = True) -> None:
    """Клип в отрезок: длиннее — обрезка; короче — замедление до MAX_SLOWDOWN и стоп-кадр.
    Липсинк не замедляется (slowdown=False): губы должны совпадать со звуком."""
    clip = _duration(video)
    factor = 1.0
    if slowdown and duration > clip:
        factor = min(duration / clip, MAX_SLOWDOWN)
    hold = max(duration - clip * factor, 0)
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},"
          f"setpts={factor:.4f}*PTS,tpad=stop_mode=clone:stop_duration={hold + 0.1:.3f},{GRADE},format=yuv420p")
    _ff(["-i", video, "-vf", vf, "-t", f"{duration:.3f}", "-r", str(FPS),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", out], "series clip")


def assemble(shots: list[dict], dialogue: dict, subtitle_path: str, out_path: str, assignment: list[dict],
             music_path: str | None = None, music_volume: float = 0.14) -> dict:
    from agents.assembly_agent import _esc

    out_path = Path(out_path)
    work = out_path.parent / f"{out_path.stem}_segments"
    work.mkdir(parents=True, exist_ok=True)

    timeline = build_timeline(assignment, dialogue["lines"], dialogue["duration"])
    seg_files = []
    for n, seg in enumerate(timeline):
        shot = shots[seg["index"]]
        seg_out = str(work / f"seg_{n:02d}.mp4")
        if seg["clip"] and shot.get("video_path"):
            _render_clip(shot["video_path"], seg["duration"], seg_out, slowdown=not shot.get("lipsync"))
        else:
            _render_still(shot["image_path"], seg["duration"], seg_out, zoom_in=n % 2 == 0)
        seg_files.append(seg_out)

    concat_list = work / "concat.txt"
    concat_list.write_text("".join(f"file '{Path(p).as_posix()}'\n" for p in seg_files), encoding="utf-8")
    video_only = str(work / "video_only.mp4")
    _ff(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", video_only], "series concat")

    total = sum(seg["duration"] for seg in timeline)
    # Звук сводится отдельным проходом: в одном графе с субтитрами и libx264 ffmpeg 9 молча терял
    # ~3 с голоса (окно loudnorm) — реплики уезжали от субтитров
    mix = str(work / "mix.wav")
    inputs = ["-i", dialogue["audio_path"]]
    filters = ["[0:a]aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11,apad[voice]"]
    if music_path:
        inputs += ["-stream_loop", "-1", "-i", music_path]
        filters += [
            "[voice]asplit=2[vmain][vkey]",
            f"[1:a]aresample=48000,volume={music_volume},afade=t=out:st={max(total - 1.5, 0):.2f}:d=1.5[mus]",
            "[mus][vkey]sidechaincompress=threshold=0.04:ratio=8:attack=20:release=350[duck]",
            "[vmain][duck]amix=inputs=2:duration=first:normalize=0[a]",
        ]
    else:
        filters.append("[voice]anull[a]")
    _ff([*inputs, "-filter_complex", ";".join(filters), "-map", "[a]", "-t", f"{total:.3f}",
         "-ar", "48000", "-c:a", "pcm_s16le", mix], "series audio mix")

    _ff(["-i", video_only, "-i", mix, "-filter_complex", f"[0:v]subtitles='{_esc(subtitle_path)}'[v]",
         "-map", "[v]", "-map", "1:a", "-t", f"{total:.3f}", "-c:v", "libx264", "-profile:v", "high",
         "-level", "4.1", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", "-b:a", "192k", "-movflags", "+faststart", str(out_path)],
        "series final", timeout=1800)

    logger.info("Series assembly: %d сегментов, %.1f с → %s", len(timeline), total, out_path)
    placed, t = [], 0.0  # где какой кадр стоит в ролике — для проверки липсинка в Series QA
    for seg in timeline:
        placed.append({"shot": seg["index"], "start": round(t, 3), "duration": round(seg["duration"], 3)})
        t += seg["duration"]
    return {"video_path": str(out_path), "duration": round(total, 2), "resolution": f"{W}x{H}",
            "segments": len(timeline), "timeline": placed}
