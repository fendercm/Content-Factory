"""
Шаг заказа из панели (Студия): сценарий или сборка видео. Статус и ошибка пишутся в order.json.

    py scripts/order_run.py --order 20261021_220000_ab12 --step script
    py scripts/order_run.py --order 20261021_220000_ab12 --step build
"""

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from agents import order_agent as oa  # noqa: E402
from utils.log_redact import redact  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("order_run")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Шаг заказа: сценарий или видео")
    parser.add_argument("--order", required=True)
    parser.add_argument("--step", choices=("script", "build"), required=True)
    args = parser.parse_args()

    order = oa.load(args.order)
    busy, ok = ("scripting", "script_ready") if args.step == "script" else ("building", "done")
    order.update(status=busy, error="")
    oa.save(order)
    try:
        if args.step == "script":
            order["script"] = oa.write_script(order)
            if not order.get("title"):
                order["title"] = order["script"]["title"]
            logger.info("Сценарий: %d реплик, %d слов", len(order["script"]["lines"]), order["script"]["words"])
        else:
            order = oa.load(args.order)  # сценарий мог быть поправлен в панели перед сборкой
            order.update(status=busy, error="")
            order["result"] = oa.build(order)
            logger.info("Видео готово: %s, $%s", order["result"]["video"], order["result"]["cost_usd"])
        order["status"] = ok
    except Exception as exc:
        logger.exception("Заказ %s: шаг %s упал", args.order, args.step)
        order = {**oa.load(args.order), "status": "failed" if args.step == "build" else "draft",
                 "error": redact(f"{type(exc).__name__}: {exc}")[:500]}
        oa.save(order)
        return 1
    oa.save(order)
    return 0


if __name__ == "__main__":
    sys.exit(main())
