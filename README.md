# 河北 IPTV EPG 定时抓取

容器每天抓取河北电信、移动和联通的节目单及播放列表。频道 Logo 可以手动更新。产物保存在挂载的本地数据目录，可由 VyOS 脚本读取和分发。Python 和 `pycryptodome` 由镜像提供。VyOS 1.5.1 可用原生 `container` 命令部署，也可在 Linux 上用 Docker Compose 运行。

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

### 联通经独立 WireGuard 路由表抓取

在 `providers.unicom` 中设置 `"interface": "wg1"` 和 `"fwmark": 103`，鉴权和同一会话中的跳转连接都会使用该标记。若 `wg1` 上的 `Unicom_XFC` 已持有 `0.0.0.0/0`，可把表 103 的默认路由指向 `wg1`，覆盖服务端临时跳转到的 IPv4 地址。先用 `show configuration commands | match 'wireguard.*wg1.*peer.*Unicom_XFC.*allowed-ips'` 核对 peer。

```text
set protocols static table 103 route 0.0.0.0/0 interface 'wg1'
set policy local-route rule 103 fwmark '103'
set policy local-route rule 103 set table '103'
```

提交后用 `sudo ip -4 route get <联通鉴权目标IP> mark 103 oif wg1` 验证应选到 `dev wg1 table 103`。WireGuard 按 `AllowedIPs` 最长前缀匹配选择 peer。若目标地址同时落在其他 peer 的更具体网段里，需先核对该目标地址应由哪个 peer 承载。

### 电信抓取前准备表 101

如果 `dhcp-interface` 换租后仍指向旧网关，可在抓取前运行 VyOS 宿主机脚本 [vyos-dhcp-route.py](scripts/vyos-dhcp-route.py)。脚本核对接口 IPv4 地址和当前租约，用 ping 或 ARP 验证租约网关，再通过 VyOS 原生配置会话提交表 101 的默认路由。网关未就绪时，表 101 会保留拒绝路由。若 DHCP 客户端正在等待地址，脚本让它继续尝试。需要续租时，脚本调用 VyOS 的 DHCP 操作命令，随后最多等待八分钟。自动提交只写入运行配置，每天由抓取前的任务重新确认网关。

接口在续租中暂时消失、地址在探测中变化，或续租命令自身超时时，脚本会继续等待可用租约。白天手动排障时，可用 `--wait-seconds 900` 延长单次等待到 15 分钟。

路由提交沿用 VyOS 启动加载器的权限设置。脚本在创建配置会话前切换主组为 `vyattacfg`，并设置 `umask 0002`，使提交生成的目录可由该组写入。这也适用于由 cron 或开机 `systemd-run` 启动的任务。升级时请替换 `/config/scripts/hbiptv-dhcp-route.py`。如果旧版运行后已出现 `Set failed` 或 `Delete failed`，可用 `sudo stat -c '%A %U:%G %n' /opt/vyatta/config/active/protocols` 检查。确认 `/config/config.boot` 包含要保留的配置后，可在维护窗口重启，由 VyOS 重新加载已保存的配置并建立配置树。重启会丢弃未保存的更改。

将脚本复制到 VyOS 后，在操作模式下安装并预检。默认处理 `eth0.99` 和表 `101`，预检只读取状态并探测。如果预检失败，输出会说明是接口状态、租约与地址不匹配、租约网关缺失，还是 ICMP 和 ARP 都无响应。通过 ARP 预检后，仍可用一次电信抓取验证网关转发和鉴权。

```bash
sudo install -o root -g root -m 0755 vyos-dhcp-route.py /config/scripts/hbiptv-dhcp-route.py
sudo /config/scripts/hbiptv-dhcp-route.py --check
```

预检通过后，在配置模式下移除表 101 原有的默认路由配置，由脚本单独管理该默认路由。保留 `fwmark 101` 的选表规则和容器中电信的标记配置。以下配置按 VyOS 宿主机时区每天 `03:50` 执行。若修改了容器的 `EPG_RUN_AT`，同步调整这个时间，给 DHCP 留出约十分钟。

```text
configure
delete protocols static table 101 route 0.0.0.0/0
delete system task-scheduler task hbiptv-dhcp-route interval
set system task-scheduler task hbiptv-dhcp-route executable path '/config/scripts/hbiptv-dhcp-route.py'
set system task-scheduler task hbiptv-dhcp-route crontab-spec '50 3 * * *'
commit
save
exit
```

前两条 `delete` 用于迁移已有配置，新部署可跳过不存在的项目。如果表 101 仍有旧脚本留下的 `proto 242` 默认路由，可在操作模式下用 `sudo ip -4 route flush table 101 proto 242` 做一次清理。随后手动运行一次并检查结果。

```bash
sudo /config/scripts/hbiptv-dhcp-route.py
ip -4 route show table 101
sudo ip -4 route get 192.168.0.23 mark 101 oif eth0.99
sudo journalctl -t hbiptv-dhcp-route -n 20
```

正常时表 101 中应有指向当前 DHCP 网关的默认路由。若脚本报告 `unmanaged default route`，请先核对 VyOS 运行配置里的表 101 默认路由。最后进入容器运行 `python -m iptv_epg once --provider telecom`，检查本地节目单是否更新。白天需要手动抓取电信时，也可以先手动运行一次这个准备脚本。

### 开机恢复 rtp2httpd 电信回看规则

如果 rtp2httpd 使用宿主机网络，并设置 `upstream-interface-rtsp = eth0.99`，可通过 Linux 的 `oif eth0.99 lookup 101` 规则为这些已绑定接口的本机连接选表。先确认优先级 110 可用，或已经是这条规则，然后运行：

```bash
sudo /config/scripts/hbiptv-dhcp-route.py --ensure-oif-rule 110
```

在现有的 `/config/scripts/vyos-postconfig-bootup.script` 末尾追加以下内容。如果文件已有 `exit`，插在它前面，并保留其他启动命令。第一条命令在开机后恢复规则，第二条安排一次路由准备，给 DHCP 服务留出启动时间。

```bash
if /config/scripts/hbiptv-dhcp-route.py --ensure-oif-rule 110; then
    systemd-run --quiet --collect --unit=hbiptv-telecom-boot --on-active=15s /config/scripts/hbiptv-dhcp-route.py
fi
```

确认启动脚本有执行权限。正常运行时可用 `ip -4 rule show` 核对 110 号规则，并用 `ip -4 route show table 101` 核对当前网关。若白天更换了电信租约，可手动执行一次 `sudo /config/scripts/hbiptv-dhcp-route.py` 刷新表 101。

## 本地产物

每家运营商写入单独的目录。以电信为例，容器内的文件是 `/data/telecom/PL.xml`、`PL.xml.gz`、`rtp2http.m3u` 和 `logo/*.png`。下面的部署示例会把这些文件分别保存到 VyOS 的 `/config/user-data/docker/hbiptv-epg/data/` 或 Compose 项目的 `./data/`。

网页生成 M3U 后会显示容器内的文件路径和修改时间。若 VyOS 上检查的文件时间较早，可用下列命令核对 `/data` 实际挂载的宿主机目录，并查看容器内对应文件。

```bash
sudo podman inspect --format '{{range .Mounts}}{{println .Destination .Source}}{{end}}' hbiptv-epg
sudo podman exec hbiptv-epg stat /data/cmcc/rtp2http.m3u
```

M3U 默认使用相对于同一目录的节目单和 Logo 路径。如果你的脚本会把整个数据目录发布到 HTTP(S) 服务，可将 `playlist_base_url` 设为该目录的网址，例如 `https://example.org/iptv`。生成的电信节目单链接将是 `https://example.org/iptv/telecom/PL.xml.gz`。该选项只改变 M3U 中的链接。

从旧版升级时，已有的 `interface` 设置会继续生效。可移除配置里的 `github_repository` 及容器的令牌挂载。VyOS 原生部署可在配置模式下执行 `delete container name hbiptv-epg volume secrets`，然后提交并保存。

## 用 VyOS 1.5.1 原生命令运行

将已准备的 `config.local.json` 复制到 VyOS，然后在操作模式下安装到持久化的 `/config`。如果文件放在其他目录，请替换下面命令中的源路径。

```bash
sudo install -d -m 0700 /config/user-data/docker/hbiptv-epg /config/user-data/docker/hbiptv-epg/data
sudo install -m 0600 config.local.json /config/user-data/docker/hbiptv-epg/config.json
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
set container name hbiptv-epg volume config source '/config/user-data/docker/hbiptv-epg/config.json'
set container name hbiptv-epg volume config destination '/run/iptv/config.json'
set container name hbiptv-epg volume config mode 'ro'
set container name hbiptv-epg volume data source '/config/user-data/docker/hbiptv-epg/data'
set container name hbiptv-epg volume data destination '/data'
set container name hbiptv-epg volume data mode 'rw'
commit
save
exit
show container
show container log hbiptv-epg
```

在操作模式下运行 `connect container hbiptv-epg` 进入容器。执行 `python -m iptv_epg once` 立即抓取，用 `exit` 返回 VyOS 后检查宿主机 `/config/user-data/docker/hbiptv-epg/data/` 中的产物。定时任务默认每天北京时间 04:00 执行。

### 单独验证联通和更新 Logo

进入容器后运行 `python -m iptv_epg once --provider unicom`。成功后检查宿主机的 `/config/user-data/docker/hbiptv-epg/data/unicom/PL.xml.gz` 和 `rtp2http.m3u`。如果联通仍使用 `wg1`，可在 VyOS 操作模式下用 `sudo wg show wg1 allowed-ips` 核对联通服务地址是否由 `Unicom_XFC` 覆盖。

手动更新联通 Logo 用 `python -m iptv_epg logos --provider unicom`。把运营商换为 `telecom` 或 `cmcc` 可更新另一家，省略 `--provider` 会更新所有已启用运营商。Logo 命令只写各自的 `logo/`，定时任务与 `once` 会保留已有图片。

## 本地 Web 管理

在现有 `config.json` 顶层添加 `web`。容器以默认的 `schedule` 模式启动时会读取这些设置。首次启用或调整监听地址后需要重启容器。

```json
{
  "web": {
    "enabled": true,
    "bind": "127.0.0.1",
    "port": 8090,
    "username": "admin",
    "password": "请在这里填入自己生成的高强度密码"
  }
}
```

请用 12 到 256 个字符的自定义初始密码替换示例值。建议将现有运营商配置和初始密码保存在权限为 `0600` 的 `config.json`，以可写目录挂载到容器。网页保存时会原子替换这个文件。登录后可在网页修改管理密码，首次改密后配置将保存盐值和 scrypt 哈希，并要求重新登录。

### VyOS 挂载与访问

将当前的 `config.json` 复制到新的配置目录，再编辑其中的 `web`。以下以 `/config/user-data/docker/hbiptv-epg` 为例，请按实际情况调整配置文件的源路径。

```bash
sudo install -d -m 0700 /config/user-data/docker/hbiptv-epg/config
sudo install -m 0600 /config/user-data/docker/hbiptv-epg/config.json /config/user-data/docker/hbiptv-epg/config/config.json
```

拉取新版镜像后设置目录挂载。

```text
configure
set container name hbiptv-epg volume config source '/config/user-data/docker/hbiptv-epg/config'
set container name hbiptv-epg volume config destination '/run/iptv'
set container name hbiptv-epg volume config mode 'rw'
commit
save
exit
```

默认监听回环地址。从电脑通过 SSH 转发访问，例如 `ssh -N -L 8090:127.0.0.1:8090 vyos@192.168.15.1`，然后在浏览器打开 `http://127.0.0.1:8090`。如果需要在局域网直连，可以将 `web.bind` 设为 VyOS 的 LAN 地址并将端口访问限制到管理网段。通过 HTTPS 反向代理时可设置 `web.secure_cookie: true`。

更新已有容器的镜像后，在 VyOS 操作模式下执行 `restart container hbiptv-epg`，让运行实例使用拉取后的镜像并重新读取配置。单独编辑宿主机配置文件时，也使用该命令重新读取 Web 设置。

用以下命令检查默认回环监听，按配置替换探测地址和端口。

```bash
sudo podman logs --tail 30 hbiptv-epg
curl --noproxy '*' -sS --max-time 4 -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8090/
```

日志出现 `管理界面监听 127.0.0.1:8090`、本机请求返回 `HTTP 200` 时，可通过 SSH 转发访问。出现配置文件缺失时，检查容器内路径 `/run/iptv/config.json`。出现 `web.enabled` 未启用时，检查该键是否位于 `config.json` 顶层 `web` 对象中，且值为 JSON 布尔值 `true`。如果 `web.bind` 指向局域网地址，请在 VyOS 上用该地址进行探测。

### 在页面中操作

1. 选择电信、移动或联通，设置接口、fwmark 和运营商的鉴权字段。鉴权密码字段留空会保留已存密码。EPG 和 Logo 发布地址应用于三家运营商的 M3U。
2. 点击「刷新频道」获取运营商频道，再勾选播放列表要收录的频道及各自的回看选项。新频道默认收录，有运营商回看地址时默认开启回看。保存后，生成 M3U 时应用选择。
3. 单独执行「生成 EPG」「生成 M3U」或「下载 Logo」，任务结果出现在手动抓取面板。输出保存在 `/data/<运营商>/`，分别对应 `PL.xml` 与 `PL.xml.gz`、`rtp2http.m3u`、`logo/`。每天北京时间 04:00 的默认任务生成 EPG 和 M3U，Logo 手动下载。

网页沿用运营商的接口和 fwmark 策略。白天手动抓取电信时，如果 DHCP 已换租，可先在 VyOS 运行路由准备脚本，再启动任务。

## 用 Docker Compose 运行

从源码构建时运行：

```bash
docker compose build
```

如果镜像已发布到 Docker Hub，在 `.env` 设置 `EPG_IMAGE=<Docker Hub 用户名>/hebei-iptv-epg:latest`，并运行 `docker compose pull epg`。

```bash
docker compose run --rm epg once
docker compose run --rm epg logos --provider unicom
docker compose up -d epg
docker compose logs -f epg
```

第一次执行后可在 `data/` 核对产物。也可加 `--provider telecom`、`--provider cmcc` 或 `--provider unicom` 单独调试。定时服务默认每天北京时间 04:00 运行，可在 `.env` 设置 `EPG_RUN_AT=03:30` 后重新执行 `docker compose up -d epg`。个别运营商失败时，其他运营商仍会运行。空节目单不会覆盖上一版。

如需 Web 管理，将配置安装到 `config/config.json` 并启用其中的 `web`。默认 Compose 容器以 root 运行且仅保留 `NET_RAW`，所以目录和文件都要对容器用户可写。

```bash
sudo install -d -m 0700 config
sudo install -m 0600 config.local.json config/config.json
sudoedit config/config.json
```

将 `compose.yaml` 中指向 `/run/iptv/config.json` 的文件挂载替换为 `./config:/run/iptv:rw`，然后重建服务容器。

如果构建主机使用带自定义 CA 的出口代理，可使用宿主机已信任的 CA 构建：

```bash
docker build --secret id=pip_ca,src=/etc/ssl/certs/ca-certificates.crt -t hebei-iptv-epg .
```

## 发布 Docker Hub 镜像

GitHub Actions 使用仓库变量 `DOCKERHUB_USERNAME` 和仓库 Secret `DOCKERHUB_TOKEN`，令牌需有写入权限。变量和机密已配置时，可直接在 [Publish Docker image](https://github.com/vmlive/HBIPTV-EPG/actions/workflows/dockerhub.yml) 运行工作流。工作流在 `main` 分支相关文件更新时自动构建，也支持手动运行。它将 Linux amd64 和 arm64 镜像发布到 `<Docker Hub 用户名>/hebei-iptv-epg`，标签为 `latest` 及 `sha-<提交 SHA>`。
