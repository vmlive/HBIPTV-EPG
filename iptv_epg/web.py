"""Local administration for the scheduler and its configuration."""
import hashlib
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
import secrets
from threading import Lock, Thread
import time
from urllib.parse import parse_qs, urlsplit

from .service import PROVIDERS, catalog_path, collect, load_config, normalize_base_url, provider_config, save_json


ASSETS = Path(__file__).with_name("web_assets")
ACTIONS = {"channels": (), "epg": ("epg",), "playlist": ("playlist",), "logos": ("logos",)}
SESSION_TTL = 8 * 60 * 60
COOKIE_NAME = "hbiptv_session"
logger = logging.getLogger("iptv_epg.web")


class HTTPError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def derive_password(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)


class Console:
    def __init__(self, config_path, data_dir, settings):
        self.config_path = config_path.resolve()
        self.data_dir = data_dir
        self.username = settings.get("username", "admin")
        self.secure_cookie = settings.get("secure_cookie", False)
        if not isinstance(self.username, str) or not 1 <= len(self.username) <= 128:
            raise ValueError("web.username 无效")
        if type(self.secure_cookie) is not bool:
            raise ValueError("web.secure_cookie 必须为布尔值")
        stored = settings.get("password_hash")
        if stored is not None:
            if not isinstance(stored, str) or not re.fullmatch(r"[a-f0-9]{32}:[a-f0-9]{64}", stored):
                raise ValueError("web.password_hash 无效")
            self.salt, self.password = map(bytes.fromhex, stored.split(":"))
        else:
            password = settings.get("password")
            if not isinstance(password, str) or not 12 <= len(password) <= 256:
                raise ValueError("请在 web.password 中配置 12 到 256 个字符的管理密码")
            self.salt = secrets.token_bytes(16)
            self.password = derive_password(password, self.salt)
        self.sessions = {}
        self.failures = {}
        self.jobs = {}
        self.lock = Lock()

    def login(self, peer, payload):
        user = payload.get("username")
        password = payload.get("password")
        if not isinstance(user, str) or not isinstance(password, str) or len(user) > 128 or len(password) > 256:
            raise HTTPError(400, "用户名或密码格式有误")
        with self.lock:
            now = time.monotonic()
            self.failures = {ip: entry for ip, entry in self.failures.items() if entry[1] > now}
            attempts, expiry = self.failures.get(peer, (0, now + 60))
            if attempts >= 5 or len(self.failures) >= 256:
                raise HTTPError(429, "尝试次数过多，请稍后重试")
            valid_password = secrets.compare_digest(derive_password(password, self.salt), self.password)
            if user != self.username or not valid_password:
                self.failures[peer] = (attempts + 1, expiry)
                raise HTTPError(401, "用户名或密码错误")
            self.failures.pop(peer, None)
            self.sessions = {key: entry for key, entry in self.sessions.items() if entry[1] > now}
            if len(self.sessions) >= 32:
                self.sessions.pop(next(iter(self.sessions)))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[token] = (csrf, now + SESSION_TTL)
            return token, csrf

    def session(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if session and session[1] > time.monotonic():
                return session[0]
            self.sessions.pop(token, None)
            return None

    def change_password(self, token, payload):
        old, new = payload.get("current"), payload.get("password")
        if not isinstance(old, str) or len(old) > 256 or not isinstance(new, str) or not 12 <= len(new) <= 256:
            raise ValueError("新密码需为 12 到 256 个字符")
        with self.lock:
            if token not in self.sessions:
                raise HTTPError(401, "请重新登录")
            if not secrets.compare_digest(derive_password(old, self.salt), self.password):
                raise HTTPError(403, "当前密码错误")
            salt = secrets.token_bytes(16)
            digest = derive_password(new, salt)
            config = load_config(self.config_path)
            config["web"].pop("password", None)
            config["web"]["password_hash"] = f"{salt.hex()}:{digest.hex()}"
            self.write_config(config)
            self.salt, self.password = salt, digest
            self.sessions.clear()

    def write_config(self, config):
        try:
            save_json(self.config_path, config)
        except OSError as exc:
            logger.error("无法保存配置（%s）", type(exc).__name__)
            raise HTTPError(409, "配置无法写入，请确认 /run/iptv 是可写目录挂载") from None

    def provider_view(self, name):
        config = load_config(self.config_path)
        module, _ = PROVIDERS[name]
        entry = config["providers"].get(name, {})
        auth = entry.get("auth", {})
        preferences = entry.get("channels", {})
        try:
            channels = json.loads(catalog_path(self.data_dir, name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            channels = []
        return {
            "enabled": entry.get("enabled", bool(entry)),
            "interface": entry.get("interface"),
            "fwmark": entry.get("fwmark"),
            "auth": {key: auth.get(key, "") for key in module.AUTH_FIELDS if key != "password"},
            "has_password": bool(auth.get("password")),
            "playlist_base_url": config.get("playlist_base_url"),
            "channels": [
                {**channel,
                 "enabled": preferences.get(channel["id"], {}).get("enabled", True),
                 "catchup": preferences.get(channel["id"], {}).get("catchup", True)}
                for channel in channels
            ],
        }

    def save_provider(self, name, data):
        with self.lock:
            config = load_config(self.config_path)
            entry = config["providers"].setdefault(name, {})
            if type(data.get("enabled")) is not bool or not isinstance(data.get("auth"), dict):
                raise ValueError("运营商参数格式有误")
            entry["enabled"] = data["enabled"]
            entry["interface"] = data.get("interface")
            entry["fwmark"] = data.get("fwmark")
            config["playlist_base_url"] = normalize_base_url(data.get("playlist_base_url"))
            module, _ = PROVIDERS[name]
            auth = entry.setdefault("auth", {})
            for key, value in data["auth"].items():
                if key not in module.AUTH_FIELDS or not isinstance(value, str) or len(value) > 4096:
                    raise ValueError("鉴权参数格式有误")
                if key != "password" or value:
                    auth[key] = value
            if entry["enabled"]:
                provider_config(config, name)
            self.write_config(config)

    def save_channels(self, name, data):
        if not data or len(data) > 8192:
            raise ValueError("请先获取频道")
        with self.lock:
            config = load_config(self.config_path)
            entry = config["providers"].get(name)
            if entry is None:
                raise ValueError("请先设置运营商参数")
            preferences = entry.setdefault("channels", {})
            for channel_id, pair in data.items():
                if not re.fullmatch(r"[0-9]{1,32}", channel_id) or not isinstance(pair, dict) or any(
                    type(pair.get(key)) is not bool for key in ("enabled", "catchup")
                ):
                    raise ValueError("频道选择格式有误")
                if pair["enabled"] and pair["catchup"]:
                    preferences.pop(channel_id, None)
                else:
                    preferences[channel_id] = {"enabled": pair["enabled"], "catchup": pair["catchup"]}
            self.write_config(config)

    def start_job(self, name, action):
        config = load_config(self.config_path)
        if not config["providers"].get(name, {}).get("enabled", True) or name not in config["providers"]:
            raise HTTPError(409, "请先配置并启用该运营商")
        provider_config(config, name)
        with self.lock:
            if self.jobs.get(name, {}).get("state") == "running":
                raise HTTPError(409, "该运营商已有任务在运行")
            self.jobs[name] = {"action": action, "state": "running"}
        Thread(target=self.run_job, args=(name, action, config), daemon=True).start()

    def run_job(self, name, action, config):
        job = {"action": action}
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            result = collect(name, config, self.data_dir, outputs=ACTIONS[action])
            job.update(state="done", count=result[action])
            if action == "playlist":
                job["file"] = result["playlist_file"]
        except RuntimeError as exc:
            job.update(state="error", message=str(exc))
            logger.warning("%s %s 手动任务失败。%s", name, action, exc)
        except Exception as exc:
            job.update(state="error", message="抓取或写入失败，请查看容器日志")
            logger.error("%s %s 手动任务失败（%s）", name, action, type(exc).__name__)
        with self.lock:
            self.jobs[name] = job


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, format, *args):
        pass

    def respond(self, status, body, content_type="application/json; charset=utf-8", cookie=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ""

    def set_cookie(self, token, max_age):
        cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        return cookie + ("; Secure" if self.server.console.secure_cookie else "")

    def read_json(self):
        if self.headers.get("Sec-Fetch-Site") == "cross-site" or self.headers.get_content_type() != "application/json":
            raise HTTPError(415, "请从管理页面提交 JSON 请求")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 512 * 1024:
            raise HTTPError(413, "请求大小超出限制")
        payload = json.loads(self.rfile.read(size))
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        return payload

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def handle_request(self, method):
        try:
            self.dispatch(method)
        except HTTPError as exc:
            self.respond(exc.status, {"error": exc.message})
        except (json.JSONDecodeError, UnicodeError):
            self.respond(400, {"error": "JSON 或配置格式有误"})
        except (ValueError, RuntimeError) as exc:
            self.respond(400, {"error": str(exc)})
        except OSError as exc:
            if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
                return
            logger.error("管理请求无法读取文件（%s）", type(exc).__name__)
            self.respond(500, {"error": "无法读取配置或频道数据"})

    def dispatch(self, method):
        url = urlsplit(self.path)
        path = url.path
        console = self.server.console
        if method == "GET" and path in ("/", "/app.js", "/style.css"):
            filename, mime = {
                "/": ("index.html", "text/html"),
                "/app.js": ("app.js", "text/javascript"),
                "/style.css": ("style.css", "text/css"),
            }[path]
            self.respond(200, (ASSETS / filename).read_bytes(), mime + "; charset=utf-8")
            return
        if method == "POST" and path == "/api/login":
            token, csrf = console.login(self.client_address[0], self.read_json())
            self.respond(200, {"csrf": csrf}, cookie=self.set_cookie(token, SESSION_TTL))
            return

        token = self.session_token()
        csrf = console.session(token)
        if not csrf:
            raise HTTPError(401, "请先登录")
        if method == "POST" and not secrets.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), csrf.encode()):
            raise HTTPError(403, "会话校验失败，请刷新页面")
        if method == "GET" and path == "/api/session":
            self.respond(200, {"csrf": csrf, "providers": {name: label for name, (_, label) in PROVIDERS.items()}})
            return
        if method == "POST" and path == "/api/logout":
            with console.lock:
                console.sessions.pop(token, None)
            self.respond(200, {}, cookie=self.set_cookie("", 0))
            return
        if method == "POST" and path == "/api/password":
            console.change_password(token, self.read_json())
            self.respond(200, {}, cookie=self.set_cookie("", 0))
            return
        if method == "GET" and path == "/api/jobs":
            with console.lock:
                jobs = console.jobs.copy()
            self.respond(200, jobs)
            return

        name = parse_qs(url.query).get("name", [""])[0]
        if name not in PROVIDERS:
            raise HTTPError(404, "找不到运营商或请求地址")
        if path == "/api/provider" and method == "GET":
            self.respond(200, console.provider_view(name))
        elif path == "/api/provider" and method == "POST":
            console.save_provider(name, self.read_json())
            self.respond(200, {})
        elif path == "/api/channels" and method == "POST":
            console.save_channels(name, self.read_json())
            self.respond(200, {})
        elif path == "/api/jobs" and method == "POST":
            action = self.read_json().get("action")
            if not isinstance(action, str) or action not in ACTIONS:
                raise HTTPError(400, "未知任务类型")
            console.start_job(name, action)
            self.respond(202, {})
        else:
            raise HTTPError(404, "找不到请求地址")


def create_web_server(config_path, data_dir, settings):
    port = settings.get("port", 8090)
    bind = settings.get("bind", "127.0.0.1")
    if type(port) is not int or not 1 <= port <= 65535 or not isinstance(bind, str) or not bind.strip():
        raise ValueError("请设置有效的 web.bind 和 web.port")
    console = Console(config_path, data_dir, settings)
    server = ThreadingHTTPServer((bind, port), Handler)
    server.console = console
    return server
