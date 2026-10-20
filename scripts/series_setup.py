"""
Разовая подготовка сериала: референс-портреты героини через Nano Banana 2 (KIE).

    py scripts/series_setup.py kabinet7              # 3 варианта → assets/series/<id>/candidates/
    py scripts/series_setup.py kabinet7 --pick 2     # выбрать вариант 2 как референс героини

Референс — лицо сериала: он идёт в image_input каждого кадра, поэтому выбирается один раз.
"""

import argparse
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from utils import kie_client  # noqa: E402


def _portrait_prompt(series: dict) -> str:
    h = series["heroine"]
    return (
        f"{series['style_en']} Character reference portrait for a TV series. {h['visual_en']} "
        "Medium close-up, head and shoulders, facing the camera at a slight three-quarter angle, "
        "neutral attentive expression with a hint of dry irony. Background: softly blurred "
        f"{series['location_en']}."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("series_id")
    parser.add_argument("--pick", type=int, help="номер варианта, который станет референсом")
    parser.add_argument("--n", type=int, default=3)
    args = parser.parse_args()

    series = json.loads((ROOT / "config" / "series" / f"{args.series_id}.json").read_text(encoding="utf-8"))
    out_dir = ROOT / "assets" / "series" / args.series_id
    cand_dir = out_dir / "candidates"
    ref = ROOT / series["heroine"]["reference"]

    if args.pick:
        src = cand_dir / f"vera_{args.pick}.jpg"
        if not src.exists():
            sys.exit(f"нет варианта {src}")
        ref.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, ref)
        print(f"Референс героини: {ref}")
        return

    cand_dir.mkdir(parents=True, exist_ok=True)
    prompt = _portrait_prompt(series)

    def one(i: int) -> str:
        res = kie_client.run_task("nano-banana-2", {"prompt": prompt, "aspect_ratio": "3:4",
                                                    "resolution": "1K", "output_format": "jpg"}, 600)
        dest = cand_dir / f"vera_{i}.jpg"
        kie_client.download(res["urls"][0], dest)
        return f"{dest} ({res['credits']} кр.)"

    with ThreadPoolExecutor(max_workers=args.n) as ex:
        for line in ex.map(one, range(1, args.n + 1)):
            print("готово:", line)
    if not ref.exists():
        shutil.copy2(cand_dir / "vera_1.jpg", ref)
        print(f"По умолчанию референсом выбран вариант 1: {ref} (сменить: --pick N)")


if __name__ == "__main__":
    main()
