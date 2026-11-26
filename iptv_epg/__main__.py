import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import gzip
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from . import cmcc, telecom, unicom
from .protocol import create_session, download_logos, get_all_epg


PROVIDERS = {
    "telecom": (telecom, "电信"),
    "cmcc": (cmcc, "移动"),
    "unicom": (unicom, "联通"),
}
TZ = "Asia/Shanghai"
logger = logging.getLogger("iptv_epg")


def load_config(path):
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict) or not isinstance(config.get("providers"), dict):
        raise ValueError("配置必须包含 providers 对象")
    unknown = config["providers"].keys() - PROVIDERS.keys()
    if unknown:
        raise ValueError("providers 包含未知运营商")
    base_url = config.get("playlist_base_url")
    if base_url is not None:
        if not isinstance(base_url, str) or re.search(r'[\s"]', base_url):
            raise RuntimeError("playlist_base_url 必须是 HTTP(S) 目录地址")
        url = urlsplit(base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username is not None or url.query or url.fragment:
            raise RuntimeError("playlist_base_url 必须是 HTTP(S) 目录地址")
        config["playlist_base_url"] = base_url.rstrip("/")
    return config


def provider_config(config, name):
    module, _ = PROVIDERS[name]
    entry = config["providers"][name]
    auth = entry.get("auth", {})
    invalid = [key for key in module.AUTH_FIELDS if not isinstance(auth.get(key), str)]
    invalid += [key for key in ("IPTV_AUTH_URL", "UserID", "STBID", "password") if auth.get(key) == ""]
    if invalid:
        raise ValueError(f"缺少有效鉴权设置 {'、'.join(invalid)}")
    url = urlsplit(auth["IPTV_AUTH_URL"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username:
        raise ValueError("IPTV_AUTH_URL 必须是运营商的 HTTP(S) 鉴权入口")
    interface = entry.get("interface")
    if interface is not None and (
        not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface)
    ):
        raise ValueError("无效的 Linux 接口名称")

    fwmark = entry.get("fwmark")
    if fwmark is not None and (type(fwmark) is not int or not 1 <= fwmark <= 2147483647):
        raise ValueError("fwmark 必须是 1 到 2147483647 的整数")

    return interface, fwmark, auth


@contextmanager
def provider_lock(data_dir, name):
    with (data_dir / f".{name}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有任务在抓取该运营商") from None
        yield


def write_playlist(path, channels, label, path_prefix):
    with path.open("w", encoding="utf-8") as stream:
        stream.write(f'#EXTM3U name="{label}IPTV" x-tvg-url="{path_prefix}PL.xml.gz"\n')
        for channel in channels:
            id_ = channel["userchannel_id"]
            name = channel["channel_name"]
            group = channel["group_title"]
            replay = channel["timeshift_url_full"]
            sep = "&" if "?" in replay else "?"
            catchup = f"{replay}{sep}playseek=${{(b)yyyyMMddHHmmss}}-${{(e)yyyyMMddHHmmss}}"
            live = f'rtp://{channel["igmp_url"]}'
            if "fcc_ip" in channel and "fcc_port" in channel:
                live += f'?fcc={channel["fcc_ip"]}:{channel["fcc_port"]}'
                if channel.get("fec_port"):
                    live += f'&fec={channel["fec_port"]}'
            stream.write(
                f'#EXTINF:-1 tvg-id="{id_}" tvg-name="{name}" '
                f'tvg-logo="{path_prefix}logo/{id_}.png" group-title="{group}" '
                f'catchup="default" catchup-source="{catchup}", {name}\n{live}\n'
            )


def write_guide(stage, data, label):
    root = ET.Element("tv", {"info-name": f"{label}EPG", "info-url": ""})
    for _, channel_id, channel_name, _ in data:
        channel = ET.SubElement(root, "channel", id=channel_id)
        ET.SubElement(channel, "display-name", lang="zh").text = channel_name
    count = 0
    for _, channel_id, _, days in data:
        for day in days:
            for start, title, stop in day:
                programme = ET.SubElement(root, "programme", channel=channel_id, start=start, stop=stop)
                ET.SubElement(programme, "title", lang="zh").text = title
                count += 1
    if not count:
        raise RuntimeError("未提取到节目，保留上一版节目单")
    ET.indent(root)
    xml = stage / "PL.xml"
    ET.ElementTree(root).write(xml, encoding="utf-8", xml_declaration=True)
    ET.parse(xml)
    with xml.open("rb") as source, (stage / "PL.xml.gz").open("wb") as target:
        with gzip.GzipFile(filename="PL.xml", fileobj=target, mode="wb", mtime=0) as compressed:
            shutil.copyfileobj(source, compressed)
    return count


def save_local(stage, output):
    (output / "logo").mkdir(parents=True, exist_ok=True)
    for path in stage.rglob("*"):
        if path.is_file():
            # Use a temporary file on the destination filesystem for an atomic replacement.
            destination = output / path.relative_to(stage)
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
                temp_path = Path(temp.name)
            try:
                shutil.copy2(path, temp_path)
                temp_path.replace(destination)
            finally:
                temp_path.unlink(missing_ok=True)


def collect(name, config, data_dir):
    module, label = PROVIDERS[name]
    interface, fwmark, auth = provider_config(config, name)
    with provider_lock(data_dir, name):
        with tempfile.TemporaryDirectory(prefix=f".{name}-", dir=data_dir) as temp:
            stage = Path(temp)
            logger.info("%s 开始抓取，接口 %s，fwmark %s", name, interface or "自动", fwmark or "未设置")
            try:
                session = create_session(interface, auth, fwmark=fwmark)
            except OSError:
                raise RuntimeError(f"无法使用接口 {interface}，请检查宿主机网络") from None
            with session:
                if not module.authenticate(session):
                    raise RuntimeError("运营商鉴权失败")
                channels = module.get_channel_list(session)
                if not channels:
                    raise RuntimeError("运营商未返回有效频道")
                data = get_all_epg(session, channels, module.authenticate, reauth_on_503=name != "unicom")
                count = write_guide(stage, data, label)
                download_logos(session, channels, stage / "logo", module.authenticate)
            base_url = config.get("playlist_base_url")
            prefix = f"{base_url}/{name}/" if base_url else ""
            write_playlist(stage / "rtp2http.m3u", channels, label, prefix)
            save_local(stage, data_dir / name)
            logos = sum(1 for _ in (stage / "logo").glob("*.png"))
            logger.info("%s 已生成 %s 个频道、%s 条节目和 %s 张 Logo", name, len(channels), count, logos)


def run_batch(config_path, data_dir, provider):
    try:
        config = load_config(config_path)
        selected = [
            name for name in PROVIDERS
            if name in config["providers"] and config["providers"][name].get("enabled", True)
        ]
        if provider:
            if provider not in selected:
                raise RuntimeError("所选运营商未配置或已停用")
            selected = [provider]
        if not selected:
            raise RuntimeError("没有启用的运营商")
        data_dir.mkdir(parents=True, exist_ok=True)
    except RuntimeError as exc:
        logger.error("无法启动抓取。%s", exc)
        return 1
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        logger.error("无法启动抓取，请检查配置文件和数据目录（%s）", type(exc).__name__)
        return 1

    failures = 0
    for name in selected:
        try:
            collect(name, config, data_dir)
        except (ValueError, RuntimeError) as exc:
            logger.error("%s 抓取失败。%s", name, exc)
            failures += 1
        except (OSError, ET.ParseError, TypeError, AttributeError) as exc:
            logger.error("%s 抓取或写入失败（%s）", name, type(exc).__name__)
            failures += 1
    return int(bool(failures))


def next_run(now, run_at):
    target = now.replace(hour=run_at.hour, minute=run_at.minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def parse_run_at(value):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise argparse.ArgumentTypeError("时间格式为 HH:MM")
    return datetime.strptime(value, "%H:%M").time()


def main():
    parser = argparse.ArgumentParser(description="抓取 IPTV EPG 并保存到本地")
    parser.add_argument("--config", type=Path, default=Path("/run/iptv/config.json"))
    parser.add_argument("--data", type=Path, default=Path("/data"))
    mode = parser.add_subparsers(dest="mode", required=True)
    for command in ("once", "schedule"):
        subparser = mode.add_parser(command)
        # Older deployments may still pass this option.
        subparser.add_argument("--no-upload", action="store_true", help=argparse.SUPPRESS)
        if command == "once":
            subparser.add_argument("--provider", choices=PROVIDERS)
        else:
            subparser.add_argument("--at", type=parse_run_at, default=os.getenv("EPG_RUN_AT", "04:00"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    os.environ["TZ"] = TZ
    time.tzset()
    if args.mode == "once":
        return run_batch(args.config, args.data, args.provider)

    local_tz = ZoneInfo(TZ)
    while True:
        target = next_run(datetime.now(local_tz), args.at)
        logger.info("下次抓取时间 %s", target.isoformat())
        while (remaining := target.timestamp() - time.time()) > 0:
            time.sleep(min(remaining, 60))
        run_batch(args.config, args.data, None)


if __name__ == "__main__":
    raise SystemExit(main())
