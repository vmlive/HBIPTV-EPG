from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest

from iptv_epg.__main__ import start_web
from iptv_epg.service import PROVIDERS
from iptv_epg.web import create_web_server
from smoke import IPTVFixture


@contextmanager
def running(server):
    with server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


class WebTest(unittest.TestCase):
    def test_scheduler_reports_missing_config_disabled_web_and_occupied_port(self):
        with TemporaryDirectory() as directory, socket.socket() as occupied:
            data = Path(directory)
            config = data / "config.json"
            with self.assertLogs("iptv_epg", level="WARNING") as logs:
                start_web(config, data)
            self.assertIn(f"未找到配置文件 {config}", logs.output[0])

            config.write_text('{"providers": {}}', encoding="utf-8")
            with self.assertLogs("iptv_epg", level="INFO") as logs:
                start_web(config, data)
            self.assertIn("web.enabled", logs.output[0])

            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            config.write_text(json.dumps({"providers": {}, "web": {
                "enabled": True, "port": occupied.getsockname()[1], "password": "fixture-password",
            }}), encoding="utf-8")
            with self.assertLogs("iptv_epg", level="ERROR") as logs:
                start_web(config, data)
            self.assertIn("管理界面监听失败", logs.output[0])
            self.assertIn("web.bind 和 web.port", logs.output[0])

    def request(self, method, path, data=None, *, csrf=True):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, None if data is None else json.dumps(data), headers)
            response = connection.getresponse()
            body = response.read()
            if response.getheader("Set-Cookie"):
                self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            if response.getheader("Content-Type").startswith("application/json"):
                body = json.loads(body)
            return response.status, body
        finally:
            connection.close()

    def get_job(self, action):
        status, response = self.request("POST", "/api/jobs?name=telecom", {"action": action})
        self.assertEqual(status, 202, response)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            _, jobs = self.request("GET", "/api/jobs")
            job = jobs["telecom"]
            if job["state"] != "running":
                self.assertEqual(job["state"], "done", job)
                return job
            time.sleep(.05)
        self.fail(f"{action} did not finish")

    def test_auth_config_and_independent_output_jobs(self):
        self.csrf, self.cookie = "", ""
        with running(ThreadingHTTPServer(("127.0.0.1", 0), IPTVFixture)) as source, TemporaryDirectory() as directory:
            source.extra_channels = True
            source.routes = []
            source.empty_guide = False
            root = Path(directory)
            config_path, data_dir = root / "config.json", root / "data"
            with socket.socket() as available:
                available.bind(("127.0.0.1", 0))
                self.port = available.getsockname()[1]
            web = {"enabled": True, "username": "admin", "password": "initial-password", "port": self.port}
            auth = dict.fromkeys(PROVIDERS["telecom"][0].AUTH_FIELDS, "dummy")
            auth["IPTV_AUTH_URL"] = f"http://127.0.0.1:{source.server_port}/login"
            config = {"providers": {"telecom": {"auth": auth, "interface": "lo", "fwmark": 101}}, "web": web}
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with running(create_web_server(config_path, data_dir, web)):
                self.assertEqual(self.request("GET", "/")[0], 200)
                self.assertEqual(self.request("GET", "/app.js")[0], 200)
                self.assertEqual(self.request("GET", "/api/provider?name=telecom")[0], 401)
                self.assertEqual(self.request("POST", "/api/jobs?name=telecom", {"action": "epg"})[0], 401)
                creds = {"username": "admin", "password": web["password"]}
                self.assertEqual(self.request("POST", "/api/login", {**creds, "password": "wrong"})[0], 401)
                status, session = self.request("POST", "/api/login", creds)
                self.assertEqual(status, 200)
                self.csrf = session["csrf"]
                self.assertEqual(self.request("POST", "/api/jobs?name=telecom", {"action": "epg"}, csrf=False)[0], 403)

                update = {"enabled": True, "interface": "lo", "fwmark": 101,
                          "playlist_base_url": "https://example.test/epg/", "auth": {"UserID": "updated", "password": ""}}
                self.assertEqual(self.request("POST", "/api/provider?name=telecom", update)[0], 200)
                saved_config = json.loads(config_path.read_text())
                self.assertEqual(saved_config["providers"]["telecom"]["auth"]["password"], auth["password"])
                self.assertEqual(saved_config["providers"]["telecom"]["auth"]["STBID"], auth["STBID"])
                self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
                _, view = self.request("GET", "/api/provider?name=telecom")
                self.assertEqual(view["auth"]["UserID"], "updated")
                self.assertNotIn("password", view["auth"])
                self.assertEqual(view["fwmark"], 101)

                self.assertEqual(self.get_job("channels")["count"], 3)
                self.assertNotIn("QueryPlaybillList", source.routes)
                self.assertFalse((data_dir / "telecom").exists())
                _, view = self.request("GET", "/api/provider?name=telecom")
                self.assertEqual([(ch["id"], ch["has_catchup"]) for ch in view["channels"]],
                                 [("101", True), ("102", False), ("103", True)])

                self.assertEqual(self.get_job("playlist")["count"], 3)
                output = data_dir / "telecom"
                path = output / "rtp2http.m3u"
                original = path.read_text()
                self.assertIn('catchup-source="rtsp://example.test/3?source=iptv&playseek=', original)
                self.assertFalse((output / "PL.xml").exists())

                selection = {"101": {"enabled": True, "catchup": False},
                             "103": {"enabled": False, "catchup": True}}
                self.assertEqual(self.request("POST", "/api/channels?name=telecom", selection)[0], 200)
                self.assertEqual(self.get_job("playlist")["count"], 2)
                playlist = path.read_text()
                self.assertIn('x-tvg-url="https://example.test/epg/telecom/PL.xml.gz"', playlist)
                self.assertIn('tvg-id="101"', playlist)
                self.assertIn('tvg-id="102"', playlist)
                self.assertNotIn('tvg-id="103"', playlist)
                self.assertNotIn("catchup-source=", playlist)
                self.assertNotIn("QueryPlaybillList", source.routes)

                self.assertEqual(self.get_job("epg")["count"], 24)
                self.assertIn('id="103"', (output / "PL.xml").read_text())
                self.assertEqual(path.read_text(), playlist)
                self.assertFalse((output / "logo").exists())
                self.assertEqual(self.get_job("logos")["count"], 3)
                self.assertTrue((output / "logo/103.png").exists())
                self.assertEqual(path.read_text(), playlist)

                self.assertEqual(self.request("POST", "/api/password",
                                              {"current": creds["password"], "password": "another-password"})[0], 200)
                self.assertEqual(self.request("GET", "/api/session")[0], 401)
                saved_web = json.loads(config_path.read_text())["web"]
                self.assertNotIn("password", saved_web)
                self.assertIn("password_hash", saved_web)

            self.csrf = ""
            with running(create_web_server(config_path, data_dir, saved_web)):
                self.assertEqual(self.request("POST", "/api/login", creds)[0], 401)
                self.assertEqual(self.request("POST", "/api/login", {**creds, "password": "another-password"})[0], 200)
                status, view = self.request("GET", "/api/provider?name=telecom")
                self.assertEqual(status, 200)
                self.assertFalse(view["channels"][0]["catchup"])
                self.assertFalse(view["channels"][2]["enabled"])
