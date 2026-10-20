"""
Слепое сравнение видеомоделей image-to-video через KIE.

1. Nano Banana 2 (KIE) генерирует опорный кадр сериала «Кабинет 7».
2. Один и тот же кадр и промпт движения уходят в несколько видеомоделей.
3. Ролики скачиваются, звук вырезается (иначе Veo выдаёт себя звуком), имена
   заменяются буквами A, B, C…; какая модель за буквой — в key.json.
4. Склейка grid.mp4: все варианты рядом, слева направо A → …

Запуск:
    py scripts/animation_bakeoff.py
    py scripts/animation_bakeoff.py --keyframe-url https://...   # переиспользовать готовый кадр

Нужен KIE_API_KEY и FFmpeg в PATH.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API = "https://api.kie.ai/api/v1/jobs"
CREDITS_PER_USD = 200

KEYFRAME_PROMPT = (
    "Photorealistic cinematic still, vertical 9:16, 35mm lens, shallow depth of field. "
    "A 31-year-old woman with light tan and dark hair in a loose bun, wearing an open beige autumn "
    "trench coat, sits on the edge of a vintage armchair in a cozy psychologist's office in an old "
    "Saint Petersburg apartment building. She holds a blue ballpoint pen in both hands, thumb on the cap. "
    "Her phone lies face up on her knee. Tense, tired expression, looking slightly off-camera. "
    "Warm desk lamp light from the left, rain streaks on a tall window behind her, bookshelves, "
    "parquet floor. Natural skin texture, realistic hands with five fingers. No text, no watermark."
)

MOTION_PROMPT = (
    "The woman nervously clicks the pen cap closed twice, glances down at the phone on her knee, "
    "then slowly lifts her eyes and looks to the left as if someone asked her an unexpected question. "
    "Subtle breathing, natural hand movement, rain running down the window. Slow gentle push-in. "
    "Realistic, no camera shake, no text, no new people appearing."
)


def _models(keyframe_url: str) -> dict[str, dict]:
    """Кандидаты: имя → тело createTask. Цены KIE на 2026-10-19 (кредиты, 200 = $1)."""
    veo = lambda mode: {  # noqa: E731
        "model": "veo-3-1",
        "input": {"prompt": MOTION_PROMPT, "model": mode, "image_urls": [keyframe_url],
                  "generation_type": "FIRST_AND_LAST_FRAMES_2_VIDEO", "aspect_ratio": "9:16",
                  "resolution": "720p", "duration": 6},
    }
    return {
        "veo-3.1-lite (30 кр.)": veo("veo3_lite"),
        "veo-3.1-fast (60 кр.)": veo("veo3_fast"),
        "kling-3.0 std (70 кр.)": {
            "model": "kling-3.0/video",
            "input": {"prompt": MOTION_PROMPT, "image_urls": [keyframe_url], "sound": False,
                      "duration": "5", "aspect_ratio": "9:16", "mode": "std", "multi_shots": False},
        },
        "seedance-2-fast 720p (≈124 кр.)": {
            "model": "bytedance/seedance-2-fast",
            "input": {"prompt": MOTION_PROMPT, "first_frame_url": keyframe_url, "duration": 5,
                      "resolution": "720p", "aspect_ratio": "9:16", "generate_audio": False},
        },
        "wan-2.6-flash 720p (20 кр.)": {
            "model": "wan/2-6-flash-image-to-video",
            "input": {"prompt": MOTION_PROMPT, "image_urls": [keyframe_url], "duration": "5",
                      "resolution": "720p", "audio": False, "multi_shots": False, "nsfw_checker": False},
        },
    }


def _headers() -> dict:
    key = os.getenv("KIE_API_KEY", "")
    if not key:
        sys.exit("KIE_API_KEY не задан в .env")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def create_task(body: dict) -> str:
    r = requests.post(f"{API}/createTask", headers=_headers(), json=body, timeout=60).json()
    if r.get("code") != 200:
        raise RuntimeError(f"createTask: {r.get('code')} {r.get('msg')}")
    return r["data"]["taskId"]


def wait_task(task_id: str, timeout_s: int = 1200) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(f"{API}/recordInfo", headers=_headers(), params={"taskId": task_id}, timeout=30).json()
        except (requests.RequestException, ValueError):
            # Сетевой сбой при опросе — не повод терять уже оплаченную задачу
            time.sleep(10)
            continue
        data = r.get("data") or {}
        state = data.get("state")
        if state == "success":
            result = json.loads(data.get("resultJson") or "{}")
            urls = result.get("resultUrls") or result.get("data", {}).get("result_urls") or []
            return {"urls": urls, "credits": data.get("creditsConsumed"), "seconds": data.get("costTime")}
        if state == "fail":
            raise RuntimeError(f"{data.get('failCode')}: {data.get('failMsg')}")
        time.sleep(10)
    raise TimeoutError(f"задача {task_id} не завершилась за {timeout_s} с")


def download(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, timeout=300)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyframe-url", help="готовый опорный кадр (пропустить генерацию)")
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        sys.exit("FFmpeg не найден в PATH")

    out = ROOT / "output" / "_bakeoff" / f"anim_{datetime.now():%Y-%m-%d_%H%M}"
    out.mkdir(parents=True, exist_ok=True)

    # 1. Опорный кадр
    if args.keyframe_url:
        keyframe_url, kf_credits = args.keyframe_url, 0
    else:
        print("Опорный кадр: Nano Banana 2…")
        res = wait_task(create_task({"model": "nano-banana-2", "input": {
            "prompt": KEYFRAME_PROMPT, "aspect_ratio": "9:16", "resolution": "1K", "output_format": "jpg"}}), 600)
        keyframe_url, kf_credits = res["urls"][0], res["credits"]
    download(keyframe_url, out / "keyframe.jpg")
    print(f"  кадр готов ({kf_credits} кр.): {out / 'keyframe.jpg'}")

    # 2. Все видеозадачи — параллельно
    tasks = {}
    for name, body in _models(keyframe_url).items():
        try:
            tasks[name] = (create_task(body), time.time())
            print(f"  отправлено: {name}")
        except Exception as exc:
            print(f"  FAIL {name}: {exc}")

    results = []
    for name, (task_id, t0) in tasks.items():
        try:
            res = wait_task(task_id)
            raw = out / f"_raw_{len(results)}.mp4"
            download(res["urls"][0], raw)
            results.append({"model": name, "raw": raw, "credits": res["credits"],
                            "wall_seconds": round(time.time() - t0)})
            print(f"OK   {name}: {res['credits']} кр., {round(time.time() - t0)} с")
        except Exception as exc:
            print(f"FAIL {name}: {str(exc)[:200]}")

    if not results:
        sys.exit("Ни одна модель не вернула видео")

    # 3. Анонимизация: буквы, без звука
    random.shuffle(results)
    key, panels = {}, []
    for letter, r in zip("ABCDEFGH", results):
        dst = out / f"{letter}.mp4"
        ffmpeg("-i", str(r["raw"]), "-an", "-c:v", "copy", str(dst))
        r["raw"].unlink()
        key[letter] = {"model": r["model"], "credits": r["credits"],
                       "usd": round((r["credits"] or 0) / CREDITS_PER_USD, 3),
                       "wall_seconds": r["wall_seconds"]}
        panels.append(dst)
    (out / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4. Склейка всех вариантов рядом (по 5 секунд, 360x640 каждая)
    inputs, filters = [], []
    for i, p in enumerate(panels):
        inputs += ["-i", str(p)]
        filters.append(f"[{i}:v]trim=0:5,setpts=PTS-STARTPTS,fps=24,"
                       f"scale=360:640:force_original_aspect_ratio=decrease,pad=360:640:(ow-iw)/2:(oh-ih)/2[v{i}]")
    stack = "".join(f"[v{i}]" for i in range(len(panels)))
    filters.append(f"{stack}hstack=inputs={len(panels)}[out]" if len(panels) > 1 else "[v0]copy[out]")
    ffmpeg(*inputs, "-filter_complex", ";".join(filters), "-map", "[out]",
           "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(out / "grid.mp4"))

    total = sum((r["credits"] or 0) for r in results) + (kf_credits or 0)
    print(f"\nГотово: {out}\n  grid.mp4 — все варианты слева направо {''.join(key)}\n"
          f"  потрачено ≈ {total} кр. (${total / CREDITS_PER_USD:.2f})\n  key.json — не открывать до выбора")


if __name__ == "__main__":
    main()
