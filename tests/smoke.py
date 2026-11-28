"""Exercise all provider protocols against a loopback HTTP fixture."""
from datetime import datetime
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from iptv_epg.__main__ import PROVIDERS, next_run, parse_run_at
from iptv_epg.protocol import create_session


class IPTVFixture(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_fixture(self, text):
        content = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(200)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if urlsplit(self.path).path == "/login":
            self.send_response(302)
            self.send_header("Location", "/portal")
            self.end_headers()
        elif self.path == "/portal":
            self.send_fixture("login")
        elif self.path == "/logo.png":
            self.send_fixture(b"\x89PNG\r\n\x1a\n")
        else:
            self.send_error(404)

    def do_POST(self):
        route = self.path.rsplit("/", 1)[-1]
        self.server.routes.append(route)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if route.startswith("authLogin"):
            self.send_fixture('EncryptToken = "ABC123"; userToken.value = "DEF456"')
        elif route.startswith("ValidAuthentication"):
            submitted = parse_qs(body)
            if not submitted.get("Authenticator"):
                self.send_error(400)
                return
            self.send_fixture(
                """Authentication.CTCSetConfig('UserToken','VALID'); """
                """document.authform.stbid.value = "SHORT"; UserToken" value="VALID" stbid" value="SHORT" """
            )
        elif route.startswith("getchannellist"):
            method = "CUSetConfig" if route.endswith("HWCU.jsp") else "CTCSetConfig"
            prefix = f"Authentication.{method}('Channel','"
            channels = (
                prefix +
                'ChannelID="1",ChannelName="测试 & 频道",UserChannelID="101",'
                'ChannelURL="igmp://239.1.2.3:1234",TimeShiftURL="rtsp://example.test/1"'
                "');"
            )
            if getattr(self.server, "extra_channels", False):
                channels += (
                    prefix + 'ChannelID="2",ChannelName="直播频道",UserChannelID="102",'
                    'ChannelURL="igmp://239.1.2.4:1234"' "');" +
                    prefix + 'ChannelID="3",ChannelName="备用频道",UserChannelID="103",'
                    'ChannelURL="igmp://239.1.2.5:1234",TimeShiftURL="rtsp://example.test/3?source=iptv"' "');"
                )
            self.send_fixture(channels)
        elif route == "QueryPlaybillList":
            programme = {"startTime": "1760000000000", "name": "早间 & 新闻", "endTime": "1760003600000"}
            self.send_fixture("{}" if self.server.empty_guide else json.dumps(programme, ensure_ascii=False, separators=(",", ":")))
        elif route == "QueryPlaybillContext":
            self.send_fixture(f'{{"channelPics":["http://127.0.0.1:{self.server.server_port}/logo.png"]}}')
        else:
            self.send_error(404)


class SmokeTest(unittest.TestCase):
    def test_provider_batch_and_empty_epg(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), IPTVFixture) as server, TemporaryDirectory() as temp:
            server.empty_guide = False
            server.routes = []
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(worker.join)
            self.addCleanup(server.shutdown)
            root = Path(temp)
            providers = {
                name: {
                    "auth": dict.fromkeys(provider.AUTH_FIELDS, "dummy"),
                }
                for name, (provider, _) in PROVIDERS.items()
            }
            providers["telecom"]["interface"] = "lo"
            providers["telecom"]["fwmark"] = 101
            providers["cmcc"]["interface"] = None
            for entry in providers.values():
                entry["auth"]["IPTV_AUTH_URL"] = f"http://127.0.0.1:{server.server_port}/login"
            config = root / "config.json"
            config.write_text(json.dumps({"providers": providers}), encoding="utf-8")
            data = root / "data"
            previous_logo = data / "telecom/logo/101.png"
            previous_logo.parent.mkdir(parents=True)
            previous_logo.write_bytes(b"existing logo")
            command = [
                sys.executable, "-m", "iptv_epg", "--config", str(config),
                "--data", str(data), "once",
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("telecom 开始抓取，接口 lo，fwmark 101", result.stderr)
            for name in providers:
                output = data / name
                guide = (output / "PL.xml").read_bytes()
                self.assertEqual(gzip.decompress((output / "PL.xml.gz").read_bytes()), guide)
                xml = ET.fromstring(guide)
                self.assertEqual(xml.findtext("channel/display-name"), "测试 & 频道")
                self.assertEqual(len(xml.findall("programme")), 8)
                self.assertEqual(xml.findtext("programme/title"), "早间 & 新闻")
                self.assertTrue(xml.find("programme").get("start").endswith(" +0800"))
                playlist = (output / "rtp2http.m3u").read_text()
                self.assertIn('x-tvg-url="PL.xml.gz"', playlist)
                self.assertIn('tvg-logo="logo/101.png"', playlist)
                self.assertIn("rtp://239.1.2.3:1234", playlist)
            self.assertNotIn("QueryPlaybillContext", server.routes)
            self.assertEqual(previous_logo.read_bytes(), b"existing logo")
            self.assertFalse((data / "unicom/logo/101.png").exists())

            server.routes.clear()
            result = subprocess.run([*command, "--provider", "unicom"], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(server.routes[:5], [
                "authLoginHWCU.jsp", "ValidAuthenticationHWCU.jsp", "getchannellistHWCU.jsp",
                "authLoginHWCU.jsp", "ValidAuthenticationHWCU.jsp",
            ])
            self.assertEqual(server.routes[5:], ["QueryPlaybillList"] * 8)

            unicom_output = data / "unicom"
            original_outputs = {
                name: (unicom_output / name).stat().st_mtime_ns
                for name in ("PL.xml", "PL.xml.gz", "rtp2http.m3u")
            }
            server.routes.clear()
            result = subprocess.run(
                [*command[:-1], "logos", "--provider", "unicom"], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("QueryPlaybillContext", server.routes)
            self.assertNotIn("QueryPlaybillList", server.routes)
            self.assertTrue((unicom_output / "logo/101.png").is_file())
            for name, mtime in original_outputs.items():
                self.assertEqual((unicom_output / name).stat().st_mtime_ns, mtime)

            # Force the first provider to fail and confirm the others still run.
            providers["telecom"]["interface"] = "missing0"
            config.write_text(
                json.dumps({"providers": providers, "playlist_base_url": "https://example.test/epg/"}),
                encoding="utf-8",
            )
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("cmcc 已生成", result.stderr)
            self.assertIn("unicom 已生成", result.stderr)
            self.assertIn("无法使用接口 missing0", result.stderr)
            playlist = (data / "cmcc/rtp2http.m3u").read_text()
            self.assertIn('x-tvg-url="https://example.test/epg/cmcc/PL.xml.gz"', playlist)
            self.assertIn('tvg-logo="https://example.test/epg/cmcc/logo/101.png"', playlist)

            original = (data / "cmcc/PL.xml.gz").read_bytes()
            server.empty_guide = True
            result = subprocess.run(
                [*command, "--provider", "cmcc"], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("保留上一版节目单", result.stderr)
            self.assertEqual((data / "cmcc/PL.xml.gz").read_bytes(), original)

    def test_socket_options_apply_to_http_and_https(self):
        for interface, fwmark in (("lo", 101), (None, 102), ("lo", None), (None, None)):
            with (
                self.subTest(interface=interface, fwmark=fwmark),
                create_session(interface, {}, fwmark=fwmark) as session,
            ):
                for scheme in ("http://", "https://"):
                    options = session.get_adapter(scheme).poolmanager.connection_pool_kw["socket_options"]
                    socket_options = {opt: value for level, opt, value in options if level == socket.SOL_SOCKET}
                    self.assertEqual(socket_options.get(socket.SO_BINDTODEVICE), b"lo\0" if interface else None)
                    self.assertEqual(socket_options.get(socket.SO_MARK), fwmark)

    def test_daily_schedule_uses_next_occurrence(self):
        timezone = ZoneInfo("Asia/Shanghai")
        run_at = parse_run_at("04:00")
        self.assertEqual(next_run(datetime(2026, 11, 26, 3, tzinfo=timezone), run_at).day, 26)
        self.assertEqual(next_run(datetime(2026, 11, 26, 4, tzinfo=timezone), run_at).day, 27)


if __name__ == "__main__":
    unittest.main()
