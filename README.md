# 河北 IPTV EPG 定时抓取

Docker 容器每天抓取河北电信、移动和联通的节目单、播放列表和频道 Logo，并发布到 GitHub 的 `epg-data` 分支。源码存放在 `main` 分支。Python、`pycryptodome` 和 Git 都由镜像提供，VyOS 只需提供 Docker Engine、Compose v2 及已有网络配置。

## 宿主机网络

| 运营商 | 配置键 | Linux 接口 |
| --- | --- | --- |
| 电信 | `telecom` | `eth0.99` |
| 移动 | `cmcc` | `eth1.3010` |
| 联通 | `unicom` | `wg1` |

Compose 使用 `network_mode: host`，容器因此能使用 VyOS 上的接口。程序给每家运营商创建独立的 HTTP 会话，通过 Linux 的 `SO_BINDTODEVICE` 把鉴权、EPG、Logo 请求及重定向后的 HTTP 连接绑定到相应出站接口。目标 IP 和 TCP 端口由运营商 URL 决定。Compose 不需要做端口映射。容器使用 `NET_RAW` 完成套接字绑定。

VLAN、IP 和路由由 VyOS 管理。联通目的网段须经 `wg1` 路由，并落在 `Unicom_XFC` peer 的 `AllowedIPs` 下。可用 `ip route get <目标IP> oif <接口>` 核对实际源地址和出站接口，用 `wg show wg1 allowed-ips` 核对 peer 的目标网段。DNS 与 GitHub 上传走宿主机常规解析和路由。容器抓取成功也不改变终端播放所需的运营商网络条件。

## 配置 GitHub 发布

在部署机器上准备鉴权配置和令牌文件。

```bash
python3 import_legacy.py /path/to/old-scripts
mkdir -m 700 -p secrets
```

也可将 `config.example.json` 复制为 `config.local.json` 并填入各运营商的 `auth` 字段。默认目标是 `vmlive/Hebei-IPTV-EPG-and-PLAYLIST`，可用 `github_repository` 指向另一个数据仓库。使用旧版配置时只需核对该字段，并用新的 Compose 文件。关闭某一家可设置 `enabled: false`。

创建范围限于数据仓库、具有 **Contents: Read and write** 权限的 GitHub 细粒度访问令牌，将它保存在 `secrets/github-token`，权限设为 `0600`。Git 通过只读挂载读取令牌。配置和令牌应单独安全迁移。数据分支会在首次成功发布时创建。

发布文件位于 `epg-data` 分支的 `telecom/`、`cmcc/` 和 `unicom/`。每个目录包含 `PL.xml`、`PL.xml.gz`、`rtp2http.m3u` 和可用的 `logo/*.png`。普通播放器直接订阅 Raw URL 时，数据仓库需允许匿名读取。发布到公开仓库前，请确认有权共享运营商内容。程序遇到常见带鉴权参数的播放地址时会跳过该运营商的发布，并保留本地结果供检查。

例如，电信 EPG 的地址为：

```text
https://raw.githubusercontent.com/vmlive/Hebei-IPTV-EPG-and-PLAYLIST/epg-data/telecom/PL.xml.gz
```

## 在 VyOS 运行

从源码构建时运行：

```bash
docker compose build
```

如果镜像已发布到 Docker Hub，在 `.env` 设置 `EPG_IMAGE=<Docker Hub 用户名>/hebei-iptv-epg:latest`，并运行 `docker compose pull epg`。

```bash
docker compose run --rm epg once --no-upload
docker compose run --rm epg once
docker compose up -d epg
docker compose logs -f epg
```

第一次执行后可在 `data/` 核对产物。也可加 `--provider telecom`、`--provider cmcc` 或 `--provider unicom` 单独调试。第二次执行时检查 GitHub 的 `epg-data` 分支。定时服务默认每天北京时间 04:00 运行，可在 `.env` 设置 `EPG_RUN_AT=03:30` 后重新执行 `docker compose up -d epg`。个别运营商失败时，其他运营商仍会运行。空节目单不会覆盖上一版。

如果构建主机使用带自定义 CA 的出口代理，可使用宿主机已信任的 CA 构建：

```bash
docker build --secret id=pip_ca,src=/etc/ssl/certs/ca-certificates.crt -t hebei-iptv-epg .
```

## 发布 Docker Hub 镜像

在源码仓库的 GitHub Actions 设置中添加仓库变量 `DOCKERHUB_USERNAME` 和仓库 Secret `DOCKERHUB_TOKEN`，令牌需有写入权限。工作流在 `main` 分支相关文件更新时自动构建，也支持手动运行。它将 Linux amd64 和 arm64 镜像发布到 `<Docker Hub 用户名>/hebei-iptv-epg`，标签为 `latest` 及 `sha-<提交 SHA>`。
