import argparse
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import re
from threading import Thread
import time
from zoneinfo import ZoneInfo

from .service import PROVIDERS, load_config, run_batch


TZ = "Asia/Shanghai"
logger = logging.getLogger("iptv_epg")


def next_run(now, run_at):
    target = now.replace(hour=run_at.hour, minute=run_at.minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def parse_run_at(value):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise argparse.ArgumentTypeError("时间格式为 HH:MM")
    return datetime.strptime(value, "%H:%M").time()


def start_web(config_path, data_dir):
    try:
        settings = load_config(config_path).get("web", {})
        if not isinstance(settings, dict) or type(settings.get("enabled", False)) is not bool:
            raise ValueError("web.enabled 必须为布尔值")
        if not settings.get("enabled", False):
            return
        from .web import create_web_server

        server = create_web_server(config_path, data_dir, settings)
    except FileNotFoundError:
        return
    except (ValueError, RuntimeError) as exc:
        logger.error("管理界面启动失败。%s", exc)
        return
    except OSError as exc:
        logger.error("管理界面启动失败（%s）", type(exc).__name__)
        return

    Thread(target=server.serve_forever, daemon=True).start()
    logger.info("管理界面监听 %s:%s", *server.server_address[:2])


def main():
    parser = argparse.ArgumentParser(description="抓取 IPTV EPG 并保存到本地")
    parser.add_argument("--config", type=Path, default=Path("/run/iptv/config.json"))
    parser.add_argument("--data", type=Path, default=Path("/data"))
    mode = parser.add_subparsers(dest="mode", required=True)
    modes = {"once": "立即更新 EPG 和播放列表", "schedule": "定时更新 EPG 和播放列表", "logos": "手动下载频道 Logo"}
    for command, help_text in modes.items():
        subparser = mode.add_parser(command, help=help_text)
        # Older deployments may still pass this option.
        subparser.add_argument("--no-upload", action="store_true", help=argparse.SUPPRESS)
        if command in ("once", "logos"):
            subparser.add_argument("--provider", choices=PROVIDERS)
        else:
            subparser.add_argument("--at", type=parse_run_at, default=os.getenv("EPG_RUN_AT", "04:00"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    os.environ["TZ"] = TZ
    time.tzset()
    if args.mode in ("once", "logos"):
        return run_batch(args.config, args.data, args.provider, logos_only=args.mode == "logos")

    start_web(args.config, args.data)
    local_tz = ZoneInfo(TZ)
    while True:
        target = next_run(datetime.now(local_tz), args.at)
        logger.info("下次抓取时间 %s", target.isoformat())
        while (remaining := target.timestamp() - time.time()) > 0:
            time.sleep(min(remaining, 60))
        run_batch(args.config, args.data, None, wait=True)


if __name__ == "__main__":
    raise SystemExit(main())
