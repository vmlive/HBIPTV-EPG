import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qsl, unquote, urlsplit


BRANCH = "epg-data"
TOKEN_FILE = Path("/run/iptv-secrets/github-token")


def repository(config):
    name = config.get("github_repository", "vmlive/HBIPTV-EPG")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", name):
        raise ValueError("github_repository 必须是 GitHub 的 owner/repo")
    return name


def check_playlist(channels, auth):
    # Carrier URLs are outside our control. Keep credential-bearing playlists local.
    sensitive = {auth.get(name) for name in ("UserID", "STBID", "mac", "password", "tempKey")}
    query_keys = {"username", "userid", "passwd", "pwd", "stbid", "mac", "apikey", "secret", "authorization"}
    for channel in channels:
        for value in (channel["timeshift_url_full"], "rtp://" + channel["igmp_url"]):
            parsed = urlsplit(value)
            if parsed.username is not None or any(
                key.lower().replace("_", "").replace("-", "") in query_keys
                or "token" in key.lower() or "password" in key.lower() or "authenticator" in key.lower()
                or field in sensitive
                for key, field in parse_qsl(parsed.query)
            ) or any(secret and len(secret) >= 6 and secret in unquote(value) for secret in sensitive):
                raise RuntimeError("播放地址可能含鉴权信息，已保留本地文件并跳过该运营商的 GitHub 发布")


def publish(stage, name, repo, token_file=TOKEN_FILE):
    if not token_file.is_file() or not token_file.read_text(encoding="utf-8").strip():
        raise RuntimeError("缺少 GitHub 发布令牌，可先使用 --no-upload")
    with tempfile.TemporaryDirectory(prefix="iptv-publish-") as temp:
        root = Path(temp)
        work = root / "git"
        work.mkdir()
        askpass = root / "askpass"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in\n*Username*) echo x-access-token ;;\n'
            '*) cat "$EPG_GIT_TOKEN_FILE" ;;\nesac\n', encoding="utf-8"
        )
        askpass.chmod(0o700)
        env = {
            **os.environ, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
            "EPG_GIT_TOKEN_FILE": str(token_file.resolve()), "LC_ALL": "C",
        }
        update(work, stage, name, f"https://github.com/{repo}.git", env)


def update(work, stage, name, remote, env):
    ref = f"refs/heads/{BRANCH}"

    def git(*args, permitted=(0,)):
        result = subprocess.run(
            ["git", "-c", "credential.helper=", *args], cwd=work, env=env,
            capture_output=True, timeout=180,
        )
        if result.returncode not in permitted:
            raise RuntimeError(f"GitHub 发布的 git {args[0]} 失败（退出码 {result.returncode}）")
        return result

    git("init", f"--initial-branch={BRANCH}")
    if git("ls-remote", "--exit-code", remote, ref, permitted=(0, 2)).returncode == 0:
        git("fetch", "--depth=1", remote, ref)
        git("reset", "--hard", "FETCH_HEAD")
    for file in ("PL.xml", "PL.xml.gz", "rtp2http.m3u"):
        output = work / name / file
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(stage / file, output)
    for logo in (stage / "logo").glob("*.png"):
        output = work / name / "logo" / logo.name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(logo, output)
    git("add", "--", name)
    if git("diff", "--cached", "--quiet", permitted=(0, 1)).returncode == 0:
        return
    git("-c", "user.name=IPTV EPG", "-c", "user.email=iptv-epg@users.noreply.github.com",
        "commit", "-m", f"Update {name} EPG")
    git("push", remote, f"HEAD:{ref}")
