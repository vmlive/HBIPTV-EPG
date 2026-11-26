"""Exercise all provider protocols against a loopback HTTP fixture."""
from datetime import datetime
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from iptv_epg.__main__ import PROVIDERS, next_run, parse_run_at
from iptv_epg.publish import check_playlist, update


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
            self.send_fixture(
                f"Authentication.{method}('Channel','"
                'ChannelID="1",ChannelName="测试 & 频道",UserChannelID="101",'
                'ChannelURL="igmp://239.1.2.3:1234",TimeShiftURL="rtsp://example.test/1"'
                "');"
            )
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
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(worker.join)
            self.addCleanup(server.shutdown)
            root = Path(temp)
            providers = {
                name: {
                    "interface": "lo",
                    "auth": dict.fromkeys(provider.AUTH_FIELDS, "dummy"),
                }
                for name, (provider, _, _) in PROVIDERS.items()
            }
            for entry in providers.values():
                entry["auth"]["IPTV_AUTH_URL"] = f"http://127.0.0.1:{server.server_port}/login"
            config = root / "config.json"
            config.write_text(json.dumps({"providers": providers}), encoding="utf-8")
            data = root / "data"
            command = [
                sys.executable, "-m", "iptv_epg", "--config", str(config),
                "--data", str(data), "once", "--no-upload",
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
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
                self.assertIn(f"https://raw.githubusercontent.com/vmlive/HBIPTV-EPG/epg-data/{name}/PL.xml.gz", playlist)
                self.assertIn("rtp://239.1.2.3:1234", playlist)
                self.assertTrue((output / "logo/101.png").is_file())

            # Force the first provider to fail and confirm the others still run.
            providers["telecom"]["interface"] = "missing0"
            config.write_text(json.dumps({"providers": providers}), encoding="utf-8")
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("cmcc 已生成", result.stderr)
            self.assertIn("unicom 已生成", result.stderr)
            self.assertIn("无法使用接口 missing0", result.stderr)

            original = (data / "cmcc/PL.xml.gz").read_bytes()
            server.empty_guide = True
            result = subprocess.run(
                [*command, "--provider", "cmcc"], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("保留上一版节目单", result.stderr)
            self.assertEqual((data / "cmcc/PL.xml.gz").read_bytes(), original)

    def test_git_release_preserves_other_carriers_and_skips_no_change(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            remote = root / "remote.git"
            stage = root / "stage"
            stage.mkdir()
            for name in ("PL.xml", "PL.xml.gz", "rtp2http.m3u"):
                (stage / name).write_text(name)
            (stage / "logo").mkdir()
            (stage / "logo/1.png").write_bytes(b"image")
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)

            for index, provider in enumerate(("telecom", "cmcc", "cmcc")):
                work = root / str(index)
                work.mkdir()
                update(work, stage, provider, str(remote), None)
            files = subprocess.check_output(
                ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "epg-data"], text=True
            ).splitlines()
            self.assertEqual(len(files), 8)
            self.assertIn("telecom/logo/1.png", files)
            self.assertIn("cmcc/PL.xml.gz", files)
            count = subprocess.check_output(["git", "-C", str(remote), "rev-list", "--count", "epg-data"], text=True)
            self.assertEqual(count.strip(), "2")

        with self.assertRaisesRegex(RuntimeError, "鉴权信息"):
            check_playlist(
                [{"timeshift_url_full": "rtsp://example.test/1?access_token=dummy", "igmp_url": "239.1.1.1"}],
                {},
            )

    def test_daily_schedule_uses_next_occurrence(self):
        timezone = ZoneInfo("Asia/Shanghai")
        run_at = parse_run_at("04:00")
        self.assertEqual(next_run(datetime(2026, 11, 26, 3, tzinfo=timezone), run_at).day, 26)
        self.assertEqual(next_run(datetime(2026, 11, 26, 4, tzinfo=timezone), run_at).day, 27)


if __name__ == "__main__":
    unittest.main()
