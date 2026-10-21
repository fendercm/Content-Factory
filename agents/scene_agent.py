"""
Scene Agent — кадры и анимация эпизода сериала через KIE.

Кадры:    Nano Banana 2 (1K, 9:16) с референсами персонажей в image_input — лицо героини
          держится по её референсу (assets/series/<id>/...), лицо клиента — по портрету,
          который генерируется первым кадром эпизода.
Анимация: priority=key → Kling 3.0 std (5 с, без звука) — лидер слепого теста 2026-10-20;
          priority=simple → Wan 2.6 Flash 720p (5 с) — второе место, в 3.5 раза дешевле.
          Kling упал → тот же план в Wan; Wan упал → кадр остаётся статичным (Ken Burns в сборке).

API:
    render(series, plan, out_dir) → {"client_ref": path, "shots": [... + image_path, video_path, engine]}
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from utils.agent_logger import log_message

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

IMAGE_MODEL = "nano-banana-2"
KEY_VIDEO = "kling"
SIMPLE_VIDEO = "wan"


def _image_input(model_prompt: str, refs: list[str]) -> dict:
    return {"prompt": model_prompt, "image_input": refs, "aspect_ratio": "9:16",
            "resolution": "1K", "output_format": "jpg"}


def _video_request(engine: str, image_url: str, motion: str) -> tuple[str, dict]:
    prompt = (f"{motion} Keep faces, hair and clothing identical to the first frame. "
              "Realistic, natural motion, no text, no new people appearing.")
    if engine == "kling":
        return "kling-3.0/video", {"prompt": prompt, "image_urls": [image_url], "sound": False,
                                   "duration": "5", "aspect_ratio": "9:16", "mode": "std",
                                   "multi_shots": False}
    return "wan/2-6-flash-image-to-video", {"prompt": prompt, "image_urls": [image_url], "duration": "5",
                                            "resolution": "720p", "audio": False, "multi_shots": False,
                                            "nsfw_checker": False}


def _shot_prompt(series: dict, plan: dict, shot: dict) -> str:
    chars = shot["characters"]
    who = []
    for i, c in enumerate(chars, 1):
        desc = series["heroine"]["visual_en"] if c == "VERA" else plan["client_visual_en"]
        who.append(f"{c} (reference image {i}): {desc}")
    cast = ("Characters — keep each face, hairstyle and clothing exactly as in its reference image: "
            + "; ".join(who) + ". ") if who else ""
    return (f"{series['style_en']} Setting: {series['location_en']}. {cast}"
            f"Shot: {shot['prompt_en']}")


def render(series: dict, plan: dict, out_dir: Path) -> dict:
    from utils import kie_client

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Референсы: героиня — с диска (загрузка живёт 24 ч), клиент — портрет этого эпизода
    vera_ref = ROOT / series["heroine"]["reference"]
    if not vera_ref.exists():
        raise FileNotFoundError(f"Нет референса героини {vera_ref} — запустите scripts/series_setup.py")
    vera_url = kie_client.upload_file(vera_ref, f"content-factory/{series['id']}")

    client_prompt = (f"{series['style_en']} Character reference portrait for a TV series: "
                     f"{plan['client_visual_en']}. Medium close-up, facing the camera at a slight angle, "
                     f"tense tired expression. Background: softly blurred {series['location_en']}.")
    client_res = kie_client.run_task(IMAGE_MODEL, _image_input(client_prompt, []), 600)
    client_url = client_res["urls"][0]
    client_ref = kie_client.download(client_url, out_dir / "client_ref.jpg")
    logger.info("Scene: портрет клиента готов (%s кр.)", client_res["credits"])
    log_message("scene_agent", f"Портрет клиента готов, рисую {len(plan['shots'])} кадров", "scene_agent")

    refs_by_char = {"VERA": vera_url, "CLIENT": client_url}
    shots = plan["shots"]

    # 2. Кадры — все задачи сразу, ждём параллельно
    def make_image(i_shot):
        i, shot = i_shot
        refs = [refs_by_char[c] for c in shot["characters"]]
        res = kie_client.run_task(IMAGE_MODEL, _image_input(_shot_prompt(series, plan, shot), refs), 900)
        shot["image_url"] = res["urls"][0]
        shot["image_path"] = str(kie_client.download(shot["image_url"], out_dir / f"shot_{i:02d}.jpg"))
        return res["credits"] or 0

    with ThreadPoolExecutor(max_workers=8) as ex:
        image_credits = sum(ex.map(make_image, enumerate(shots)))
    logger.info("Scene: %d кадров готово (%s кр.)", len(shots), image_credits)
    log_message("scene_agent", f"Кадры готовы ({len(shots)}), оживляю {sum(1 for s in shots if s.get('animate'))} планов",
                "scene_agent")

    # 3. Анимация ключевых (Kling) и простых (Wan) планов
    def animate(i_shot):
        i, shot = i_shot
        if not shot.get("animate"):
            return 0
        engines = [KEY_VIDEO, SIMPLE_VIDEO] if shot.get("priority") == "key" else [SIMPLE_VIDEO]
        for engine in engines:
            model, body = _video_request(engine, shot["image_url"], shot.get("motion_en", ""))
            try:
                res = kie_client.run_task(model, body, 1500)
                shot["video_path"] = str(kie_client.download(res["urls"][0], out_dir / f"shot_{i:02d}.mp4"))
                shot["engine"] = engine
                return res["credits"] or 0
            except Exception as exc:
                logger.warning("Scene: анимация кадра %d через %s не удалась: %s", i, engine, str(exc)[:200])
        shot["engine"] = "still"
        return 0

    with ThreadPoolExecutor(max_workers=4) as ex:
        video_credits = sum(ex.map(animate, enumerate(shots)))
    engines = [s.get("engine", "still") for s in shots if s.get("animate")]
    logger.info("Scene: анимация %s (%s кр.)", engines, video_credits)
    log_message("scene_agent", f"Анимация готова: {', '.join(engines) or 'без анимации'}", "series_assembly")

    return {"client_ref": str(client_ref), "shots": shots,
            "credits": (client_res["credits"] or 0) + image_credits + video_credits}
