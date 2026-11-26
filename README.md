# 河北 IPTV EPG 定时抓取

容器每天抓取河北电信、移动和联通的节目单、播放列表和频道 Logo。产物保存在挂载的本地数据目录，可由 VyOS 脚本读取和分发。Python 和 `pycryptodome` 由镜像提供。VyOS 1.5.1 可用原生 `container` 命令部署，也可在 Linux 上用 Docker Compose 运行。

## 配置运营商和出站网卡

用 `python3 import_legacy.py /path/to/old-scripts` 从旧脚本导入鉴权信息，或复制 `config.example.json` 为 `config.local.json` 后填入各运营商的 `auth`。为每家运营商分别设置 `providers.<运营商>.interface`。下面是原部署的网卡示例，新部署请填写自己的接口名。

| 运营商 | 配置路径 | 原部署的接口示例 |
| --- | --- | --- |
| 电信 | `providers.telecom.interface` | `eth0.99` |
| 移动 | `providers.cmcc.interface` | `eth1.3010` |
| 联通 | `providers.unicom.interface` | `wg1` |

`interface` 为 `null` 或省略时，HTTP 连接使用系统路由。设置接口名后，程序通过 Linux 的 `SO_BINDTODEVICE` 将鉴权、EPG、Logo 和重定向后的 HTTP 连接绑定到该网卡。使用下面的部署示例共享宿主机网络并授予 `NET_RAW`。HTTP 目标端口仍由运营商 URL 决定，无需配置容器端口映射。设置 `enabled: false` 可停用某一家。

VLAN 和 WireGuard 由宿主机管理。若使用原部署的联通隧道，目的网段需经 `wg1` 路由，并落在 `Unicom_XFC` peer 的 `AllowedIPs` 下。可用 `ip route get <目标IP> oif <接口>` 检查实际源地址和出站接口，用 `wg show wg1 allowed-ips` 核对 peer 网段。终端播放仍依赖可访问相应的流地址。

## 本地产物

每家运营商写入单独的目录。以电信为例，容器内的文件是 `/data/telecom/PL.xml`、`PL.xml.gz`、`rtp2http.m3u` 和 `logo/*.png`。下面的部署示例会把这些文件分别保存到 VyOS 的 `/config/hbiptv-epg/data/` 或 Compose 项目的 `./data/`。

M3U 默认使用相对于同一目录的节目单和 Logo 路径。如果你的脚本会把整个数据目录发布到 HTTP(S) 服务，可将 `playlist_base_url` 设为该目录的网址，例如 `https://example.org/iptv`。生成的电信节目单链接将是 `https://example.org/iptv/telecom/PL.xml.gz`。该选项只改变 M3U 中的链接。

从旧版升级时，已有的 `interface` 设置会继续生效。可移除配置里的 `github_repository` 及容器的令牌挂载。VyOS 原生部署可在配置模式下执行 `delete container name hbiptv-epg volume secrets`，然后提交并保存。

## 用 VyOS 1.5.1 原生命令运行

将已准备的 `config.local.json` 复制到 VyOS，然后在操作模式下安装到持久化的 `/config`。如果文件放在其他目录，请替换下面命令中的源路径。

```bash
sudo install -d -m 0700 /config/hbiptv-epg /config/hbiptv-epg/data
sudo install -m 0600 config.local.json /config/hbiptv-epg/config.json
```

把下面的 `YOUR_DOCKERHUB_USER` 替换为实际发布镜像的 Docker Hub 用户名。先拉取镜像，再进入配置模式。镜像默认启动定时任务。

```text
add container image docker.io/YOUR_DOCKERHUB_USER/hebei-iptv-epg:latest
configure
set container name hbiptv-epg image 'docker.io/YOUR_DOCKERHUB_USER/hebei-iptv-epg:latest'
set container name hbiptv-epg allow-host-networks
set container name hbiptv-epg capability 'net-raw'
set container name hbiptv-epg restart 'always'
set container name hbiptv-epg environment EPG_RUN_AT value '04:00'
set container name hbiptv-epg volume config source '/config/hbiptv-epg/config.json'
set container name hbiptv-epg volume config destination '/run/iptv/config.json'
set container name hbiptv-epg volume config mode 'ro'
set container name hbiptv-epg volume data source '/config/hbiptv-epg/data'
set container name hbiptv-epg volume data destination '/data'
set container name hbiptv-epg volume data mode 'rw'
commit
save
exit
show container
show container log hbiptv-epg
```

在操作模式下运行 `connect container hbiptv-epg` 进入容器。执行 `python -m iptv_epg once` 立即抓取，用 `exit` 返回 VyOS 后检查宿主机 `/config/hbiptv-epg/data/` 中的产物。定时任务默认每天北京时间 04:00 执行。

## 用 Docker Compose 运行

从源码构建时运行：

```bash
docker compose build
```

如果镜像已发布到 Docker Hub，在 `.env` 设置 `EPG_IMAGE=<Docker Hub 用户名>/hebei-iptv-epg:latest`，并运行 `docker compose pull epg`。

```bash
docker compose run --rm epg once
docker compose up -d epg
docker compose logs -f epg
```

第一次执行后可在 `data/` 核对产物。也可加 `--provider telecom`、`--provider cmcc` 或 `--provider unicom` 单独调试。定时服务默认每天北京时间 04:00 运行，可在 `.env` 设置 `EPG_RUN_AT=03:30` 后重新执行 `docker compose up -d epg`。个别运营商失败时，其他运营商仍会运行。空节目单不会覆盖上一版。

如果构建主机使用带自定义 CA 的出口代理，可使用宿主机已信任的 CA 构建：

```bash
docker build --secret id=pip_ca,src=/etc/ssl/certs/ca-certificates.crt -t hebei-iptv-epg .
```

## 发布 Docker Hub 镜像

GitHub Actions 使用仓库变量 `DOCKERHUB_USERNAME` 和仓库 Secret `DOCKERHUB_TOKEN`，令牌需有写入权限。变量和机密已配置时，可直接在 [Publish Docker image](https://github.com/vmlive/HBIPTV-EPG/actions/workflows/dockerhub.yml) 运行工作流。工作流在 `main` 分支相关文件更新时自动构建，也支持手动运行。它将 Linux amd64 和 arm64 镜像发布到 `<Docker Hub 用户名>/hebei-iptv-epg`，标签为 `latest` 及 `sha-<提交 SHA>`。
