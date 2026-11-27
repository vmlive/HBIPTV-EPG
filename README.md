# 河北 IPTV EPG 定时抓取

容器每天抓取河北电信、移动和联通的节目单、播放列表和频道 Logo。产物保存在挂载的本地数据目录，可由 VyOS 脚本读取和分发。Python 和 `pycryptodome` 由镜像提供。VyOS 1.5.1 可用原生 `container` 命令部署，也可在 Linux 上用 Docker Compose 运行。

## 配置运营商和出站网卡

用 `python3 import_legacy.py /path/to/old-scripts` 从旧脚本导入鉴权信息，或复制 `config.example.json` 为 `config.local.json` 后填入各运营商的 `auth`。为每家运营商分别设置 `providers.<运营商>.interface`。下面是原部署的网卡示例，新部署请填写自己的接口名。

| 运营商 | 配置路径 | 原部署的接口示例 |
| --- | --- | --- |
| 电信 | `providers.telecom.interface` | `eth0.99` |
| 移动 | `providers.cmcc.interface` | `eth1.3010` |
| 联通 | `providers.unicom.interface` | `wg1` |

`interface` 为 `null` 或省略时，HTTP 连接由系统路由决定出站网卡。设置接口名后，程序通过 Linux 的 `SO_BINDTODEVICE` 将鉴权、EPG、Logo 和重定向后的 HTTP 连接绑定到该网卡。使用下面的部署示例共享宿主机网络并授予 `NET_RAW`。HTTP 目标端口仍由运营商 URL 决定，无需配置容器端口映射。设置 `enabled: false` 可停用某一家。

VLAN 和 WireGuard 由宿主机管理。若使用原部署的联通隧道，目的网段需经 `wg1` 路由，并落在 `Unicom_XFC` peer 的 `AllowedIPs` 下。可用 `ip route get <目标IP> oif <接口>` 检查实际源地址和出站接口，用 `wg show wg1 allowed-ips` 核对 peer 网段。终端播放仍依赖可访问相应的流地址。

### 用策略路由隔离运营商

如果运营商的目标网段重叠，可以给每个运营商配置不同的 `fwmark`，由 VyOS 的 `policy local-route` 选用对应路由表。容器会在 HTTP 和 HTTPS 套接字上设置这个标记。省略 `fwmark` 或填 `null` 时沿用系统原有规则。启用标记需要 Linux 5.17 或更新版本提供的 `NET_RAW` 权限。

例如，在已有的 `providers.telecom` 对象中保留 `"interface": "eth0.99"`，并加入整数 `"fwmark": 101`。VyOS 配置示例如下，默认下一跳取自电信 DHCP 租约。

```text
set protocols static table 101 route 0.0.0.0/0 dhcp-interface 'eth0.99'
set policy local-route rule 101 fwmark '101'
set policy local-route rule 101 set table '101'
```

提交后，在 VyOS 操作模式下用 `ip -4 route show table 101` 查看下一跳，并用 `sudo ip -4 route get 192.168.0.23 mark 101 oif eth0.99` 确认选路。若 DHCP 租约没有提供网关，需要在对应表中配置运营商网络实际可用的下一跳。

### 电信 DHCP 换租后刷新表 101

如果 `dhcp-interface` 换租后仍指向旧网关，可在 VyOS 宿主机运行 [vyos-dhcp-route.py](scripts/vyos-dhcp-route.py)。它使用 DHCP 客户端记录的当前租约，核对接口上的 IPv4 地址，从电信接口 ping 租约网关，未收到 ICMP 响应时用 VyOS 自带的 `arping` 检查网关的二层可达性。任一探测通过后就可以更新表 101 的默认路由。两项探测都失败时，最多每五分钟重启一次电信 DHCP 客户端，并等待新租约后重新探测。等待期间表 101 会拒绝没有其他路由的流量，避免这些请求落到主路由表。

将脚本复制到 VyOS 后，在操作模式下安装并预检。默认处理 `eth0.99` 和表 `101`，预检只读取状态并探测。如果预检失败，输出会说明是接口状态、租约与地址不匹配、租约网关缺失，还是 ICMP 和 ARP 都无响应。通过 ARP 预检后，仍可用一次电信抓取验证网关转发和鉴权。

```bash
sudo install -o root -g root -m 0755 vyos-dhcp-route.py /config/scripts/hbiptv-dhcp-route.py
sudo /config/scripts/hbiptv-dhcp-route.py --check
```

预检通过后，在配置模式下移除表 101 原有的默认路由配置，由脚本单独管理该默认路由。保留 `fwmark 101` 的选表规则和容器中电信的标记配置。

```text
configure
delete protocols static table 101 route 0.0.0.0/0
set system task-scheduler task hbiptv-dhcp-route executable path '/config/scripts/hbiptv-dhcp-route.py'
set system task-scheduler task hbiptv-dhcp-route interval '1m'
commit
save
exit
```

在操作模式下手动运行一次并检查结果。

```bash
sudo /config/scripts/hbiptv-dhcp-route.py
ip -4 route show table 101
sudo ip -4 route get 192.168.0.23 mark 101 oif eth0.99
sudo journalctl -t hbiptv-dhcp-route -n 20
```

正常时表 101 中应有指向当前 DHCP 网关的默认路由。若脚本报告 `unmanaged default route`，请先核对表 101 中是否还有手动添加的默认路由。最后进入容器运行 `python -m iptv_epg once --provider telecom`，检查本地节目单是否更新。

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
