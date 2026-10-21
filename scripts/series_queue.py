"""
Очередь эпизодов сериала (--prepare → --publish-next): посмотреть, снять, вернуть.

    py scripts/series_queue.py                  # список
    py scripts/series_queue.py reject 3         # снять эпизод 3 с публикации
    py scripts/series_queue.py restore 3        # вернуть снятый эпизод в очередь
    py scripts/series_queue.py --series kabinet7 list
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from agents.anthology_agent import episode_queue, load_series, set_episode_status  # noqa: E402

DB_PATH = os.getenv("DB_PATH", "pipeline.db")
STATUS = {"ready": "в очереди", "published": "опубликован", "rejected": "снят"}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Очередь эпизодов сериала")
    parser.add_argument("--series", default="kabinet7")
    parser.add_argument("action", nargs="?", default="list", choices=["list", "reject", "restore"])
    parser.add_argument("episode", nargs="?", type=int)
    args = parser.parse_args()

    series = load_series(args.series)
    rows = episode_queue(series, DB_PATH)

    if args.action == "list":
        if not rows:
            print(f"«{series['name']}»: эпизодов нет. Подготовить: py main.py --format series --niche психология --prepare 5")
            return
        for r in rows:
            print(f"{r['episode_no']:>3}  {STATUS.get(r['status'], r['status']):<12} {r['publish_title'] or r['title']}")
            if r["status"] == "ready":
                print(f"     {r['video_path']}")
        print(f"\nВ очереди: {sum(r['status'] == 'ready' for r in rows)}")
        return

    if args.episode is None:
        parser.error("укажите номер эпизода")
    want = "ready" if args.action == "reject" else "rejected"
    match = [r for r in rows if r["episode_no"] == args.episode and r["status"] == want]
    if not match:
        sys.exit(f"Эпизод {args.episode} со статусом «{STATUS[want]}» не найден")
    if args.action == "restore" and any(r["episode_no"] == args.episode and r["status"] != "rejected" for r in rows):
        sys.exit(f"Номер {args.episode} уже занят новым эпизодом — вернуть снятый нельзя")
    new_status = "rejected" if args.action == "reject" else "ready"
    set_episode_status(match[0]["id"], new_status, DB_PATH)
    print(f"Эпизод {args.episode}: {STATUS[new_status]}")


if __name__ == "__main__":
    main()
