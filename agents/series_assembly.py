"""
Series Assembly — монтаж эпизода сериала (1080x1920, H.264).

Таймлайн — монтаж по говорящему: у каждой реплики свой кадр (assignment из
anthology_agent.assign_shots, тайминги — из dialogue_voice); соседние одинаковые кадры
склеиваются, последний длится до конца речи + хвост. Статичные кадры — плавный наезд
(Ken Burns); анимированный клип (5 с) идёт один раз — на реплике с clip=True: длиннее
отрезка → обрезка, короче → лёгкое замедление (до 1.2x) и удержание последнего кадра.
Смена крупности (max_still): статичный кадр длиннее max_still режется на равные куски, и каждый
следующий показ кадра чередует полный кадр и наезд на лицо (PUNCH_ZOOM) — картинка меняется
каждые 2–3 с без новых генераций.

Звук: диалог (loudnorm −16 LUFS) + музыка, приглушаемая под голос (sidechain).
Субтитры: ASS из subtitle_agent.write_dialogue_ass (стиль файла, без force_style).

API:
    build_timeline(assignment, lines, speech_end, tail) → [{"index", "start", "duration", "clip"}]
    plan_pieces(timeline, shots, max_still?) → [{"index", "duration", "clip", "punch"}]
    assemble(shots, dialogue, subtitle_path, out_path, assignment, music_path?, max_still?) → {"video_path", ...}
"""

import json
import logging
import math
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

W, H, FPS = 1080, 1920, 30
MIN_SHOT = 1.0      # секунд — короче кадр не смотрится, сливаем с соседним
TAIL = 0.8          # хвост после последней реплики
MAX_SLOWDOWN = 1.2  # насколько можно замедлить 5-секундный клип
GRADE = "eq=saturation=1.05:contrast=1.03"
PUNCH_ZOOM = 1.3    # наезд при смене крупности
FACE_Y = 0.38       # где по высоте обычно лицо в вертикальном кадре — центр наезда


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


def plan_pieces(timeline: list[dict], shots: list[dict], max_still: float | None = None) -> list[dict]:
    """
    Отрезки таймлайна → куски рендера. Клип (анимация, липсинк) — один кусок. Статичный кадр длиннее
    max_still делится на равные куски; крупность чередуется по числу показов кадра: первый показ —
    полный кадр, следующий — наезд (punch), и так далее, в том числе при возврате к кадру позже.
    """
    pieces, shown = [], {}
    for seg in timeline:
        if seg["clip"] and shots[seg["index"]].get("video_path"):
            pieces.append({"index": seg["index"], "duration": seg["duration"], "clip": True, "punch": False})
            continue
        n = max(1, math.ceil(seg["duration"] / max_still - 1e-6)) if max_still else 1
        for _ in range(n):
            k = shown.get(seg["index"], 0)
            shown[seg["index"]] = k + 1
            pieces.append({"index": seg["index"], "duration": seg["duration"] / n, "clip": False,
                           "punch": bool(max_still) and k % 2 == 1})
    return pieces


def _render_still(image: str, duration: float, out: str, zoom_in: bool, size: tuple[int, int] = (W, H),
                  punch: bool = False) -> None:
    w, h = size
    frames = max(int(round(duration * FPS)), 1)
    step = 0.08 / frames
    base = PUNCH_ZOOM if punch else 1.0
    z = f"min({base}+{step:.6f}*on,{base + 0.08})" if zoom_in else f"max({base + 0.08}-{step:.6f}*on,{base})"
    y = f"max(0,min(ih*{FACE_Y}-ih/zoom/2,ih-ih/zoom))" if punch else "ih/2-(ih/zoom/2)"
    vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
          f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='{y}':d={frames}:s={w}x{h}:fps={FPS},"
          f"{GRADE},format=yuv420p")
    _ff(["-loop", "1", "-i", image, "-vf", vf, "-frames:v", str(frames), "-r", str(FPS),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", out], "series still")


def _render_clip(video: str, duration: float, out: str, slowdown: bool = True,
                 size: tuple[int, int] = (W, H)) -> None:
    """Клип в отрезок: длиннее — обрезка; короче — замедление до MAX_SLOWDOWN и стоп-кадр.
    Липсинк не замедляется (slowdown=False): губы должны совпадать со звуком."""
    clip = _duration(video)
    factor = 1.0
    if slowdown and duration > clip:
        factor = min(duration / clip, MAX_SLOWDOWN)
    hold = max(duration - clip * factor, 0)
    w, h = size
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={FPS},"
          f"setpts={factor:.4f}*PTS,tpad=stop_mode=clone:stop_duration={hold + 0.1:.3f},{GRADE},format=yuv420p")
    _ff(["-i", video, "-vf", vf, "-t", f"{duration:.3f}", "-r", str(FPS),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", out], "series clip")


def assemble(shots: list[dict], dialogue: dict, subtitle_path: str | None, out_path: str, assignment: list[dict],
             music_path: str | None = None, music_volume: float = 0.14, size: tuple[int, int] = (W, H),
             logo_path: str | None = None, max_still: float | None = None) -> dict:
    """size — кадр (заказы бывают 16:9); subtitle_path=None — без субтитров; logo_path — логотип в углу;
    max_still — смена крупности (None — кадр держится всю реплику, как в заказах)."""
    from agents.assembly_agent import _esc
    from utils.ffmpeg import concat_line

    out_path = Path(out_path)
    work = out_path.parent / f"{out_path.stem}_segments"
    work.mkdir(parents=True, exist_ok=True)

    timeline = plan_pieces(build_timeline(assignment, dialogue["lines"], dialogue["duration"]), shots, max_still)
    seg_files = []
    for n, seg in enumerate(timeline):
        shot = shots[seg["index"]]
        seg_out = str(work / f"seg_{n:02d}.mp4")
        if seg["clip"]:
            _render_clip(shot["video_path"], seg["duration"], seg_out, slowdown=not shot.get("lipsync"), size=size)
        else:
            _render_still(shot["image_path"], seg["duration"], seg_out, zoom_in=n % 2 == 0, size=size,
                          punch=seg["punch"])
        seg_files.append(seg_out)

    concat_list = work / "concat.txt"
    concat_list.write_text("".join(concat_line(p) for p in seg_files), encoding="utf-8")
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

    vfilters = [f"[0:v]subtitles='{_esc(subtitle_path)}'[vs]" if subtitle_path else "[0:v]null[vs]"]
    inputs = ["-i", video_only, "-i", mix]
    if logo_path:
        inputs += ["-i", logo_path]
        vfilters += [f"[2:v]scale={int(min(size) * 0.18)}:-1[lg]", "[vs][lg]overlay=W-w-40:40[v]"]
    else:
        vfilters.append("[vs]null[v]")
    _ff([*inputs, "-filter_complex", ";".join(vfilters),
         "-map", "[v]", "-map", "1:a", "-t", f"{total:.3f}", "-c:v", "libx264", "-profile:v", "high",
         "-level", "4.1", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", "-b:a", "192k", "-movflags", "+faststart", str(out_path)],
        "series final", timeout=1800)

    logger.info("Series assembly: %d сегментов, %.1f с → %s", len(timeline), total, out_path)
    placed, t = [], 0.0  # где какой кадр стоит в ролике — для проверки липсинка в Series QA
    for seg in timeline:
        placed.append({"shot": seg["index"], "start": round(t, 3), "duration": round(seg["duration"], 3)})
        t += seg["duration"]
    return {"video_path": str(out_path), "duration": round(total, 2), "resolution": f"{size[0]}x{size[1]}",
            "segments": len(timeline), "timeline": placed}
