# MUST VPN VM — 开发预览版

**新增独立产品 [MUST VPN Router 0.3.1](ROUTER.md)**：支持普通宿主终端 `ssh AISC`、
AISC Web 分流、仅 SSH / AISC SSH 和 Web / 指定域名三种设置。新 MSI/DEB 与原版并存，
安装和虚拟机数据独立。下文仍是原版 `must-vm` 的使用说明。
桌面 **MUST VPN Router 设置** 可打开可视化设置页面，无需启动虚拟机。

把学校 aTrust 放进独立 QEMU 虚拟机，保留 Windows / Ubuntu 宿主机的 Clash。
当前实现为命令行软件，来宾系统为 Ubuntu Minimal 24.04 amd64，无桌面、无浏览器。
学校扫码登录使用宿主机独立的 Chromium / Edge 配置目录。

**状态：用户已完成真实扫码登录，并经虚拟机 VPN 成功登录校内 SSH 服务器。不能承诺所有
Windows 电脑、Clash/WFP 驱动和学校认证策略都兼容。** 详见 [验证记录](docs/VALIDATION.md)。

## 一键进入校内终端

完成一次性 `configure` 后，双击 Windows 桌面的 **MUST VPN Terminal** 图标，或在新终端运行：

```powershell
must-vm terminal
```

这个入口自动启动虚拟机、按需在**虚拟机内**安装和启动学校 VPN，确认已知校内地址
`10.100.16.13` 经虚拟机隧道路由后，
随后在同一个窗口打开虚拟机的交互式终端。已有登录有效时无需扫码；登录过期时会打开
独立浏览器，请按学校流程扫码。出现 `vpn@must-vpn` 提示符后，直接输入
`ssh 用户名@校内主机`，SSH 的用户名、密码和主机密钥交互都由虚拟机处理。
其他校内网络可用 `must-vm terminal --probe 校内IPv4` 指定自己的验证目标。
输入 `exit` 会退出终端，并正常关闭由这个入口启动的虚拟机；若虚拟机原本由 `must-vm run`
启动，退出终端不会关闭它。直接关闭窗口可能无法执行正常关机，请优先使用 `exit`。
桌面图标使用随 MSI 安装的命令脚本，和命令行共用程序的状态目录选择：
启动失败时窗口会保留报错，按任意键关闭；
正常退出时窗口会自行关闭。
启动入口根据操作系统持有的进程锁判断虚拟机是否运行；异常退出后即使残留
`session.json`，下次启动也会重新开机，不会一直等待旧端口。真正连接超时时会显示
最后一次 SSH 错误，便于区分来宾未启动和连接失败。
首次配置仍需要选择物理网卡并提供原始学校 DEB；此流程不在宿主执行学校客户端。

## AISC 定向代理和学校连接回退

新增 `serve` 常驻模式，和独立的本地分流器 `smart-proxy`。分流器不会自动开机、启动 VPN
或触发扫码。当前只监听本机 `127.0.0.1`，不向局域网开放。

| 目标 | 连接方式 |
| --- | --- |
| 配置的 AISC 域名、子域名或 IPv4 网段 | 固定经虚拟机 VPN；服务器关闭时直接报不可用 |
| 其他 `must.edu.mo` / 子域名、配置的学校 IPv4 | 先经宿主正常网络；DNS/TCP 建连失败，且服务器/VPN 就绪时才回退 |
| 其他地址 | 经宿主正常网络，不使用虚拟机回退 |

这里“普通学校流量不代理”指不进入虚拟机、不使用学校 VPN。为了自动判断连接失败，
启用此功能的学校 Web 请求会经过本机分流器，再从宿主直连；其他网站在浏览器 PAC 中
返回 `DIRECT`。宿主已有 TUN 路由仍适用；如果原来的正常渠道依赖应用级 HTTP/SOCKS
代理，分流器的直连不会继承那个代理。只想让普通学校网站完全跳过本机分流器时，
需要放弃对这些网站的自动回退。程序不修改系统代理、路由、Clash 或全局 SSH 配置。

先配置实际 AISC 地址。以下占位内容要换成真实域名/IP；不适用的参数可以省略：

```text
must-vm routing-config --aisc-host <实际AISC域名> --aisc-network <实际AISC的IPv4/32>
```

没有确认 AISC 地址前，AISC 清单默认为空，避免误代理。学校清单默认包含 `must.edu.mo`
及已有验证地址 `10.100.16.13/32`。域名按完整标签匹配，`must.edu.mo.evil.example` 不匹配。
直接用 IP 连接时，需把 IP 加入相应网段清单；程序不会为了分类提前查询宿主 DNS。
参数 `--aisc-host` / `--aisc-network` / `--school-host` / `--school-network` 可以重复。
每次提供某类参数会**替换该类清单**，未提供的类别保留原值。
`--fallback off` 可关闭普通学校连接的回退，`--direct-timeout 4` 设置宿主 TCP 建连超时。
配置保存在状态目录的 `routing.json`；改动后重启服务器及分流器。

需要 VPN 时，在一个终端运行：

```powershell
must-vm serve
```

此入口复用已有 VM，或启动新 VM；按需在来宾安装/启动 VPN，等待登录及校内路由后，
持续提供 `127.0.0.1:1089` 的 VPN SOCKS5 服务。可用 `--port` / `--probe` 更改端口或验证地址。
Ctrl+C 会先撤下服务器就绪状态，再关闭本入口启动的 VM；已有 VM 则继续运行。
服务器每 3 秒更新就绪状态，回退前必须同时满足：进程锁仍持有、心跳不超过 12 秒、
校内验证地址实际走 utun。进程崩溃后，即使留下新鲜状态文件，也不尝试它的代理端口。
检查与建连之间恰逢服务器崩溃时仍可能连接失败，本地分流器随后短暂抑制重试。

在另一个终端运行 Web 分流入口，也可在服务器关闭时运行：

```powershell
must-vm smart-proxy --browser
```

它打开独立的分流浏览器，默认访问学校主页；`--url` 可指定 AISC 页面。
不带 `--browser` 时，只提供本地 SOCKS5 `127.0.0.1:1088` 和 PAC
`http://127.0.0.1:8765/proxy.pac`，可显式配置到其他支持的应用/浏览器。
`--port` / `--pac-port` 可换端口。旧的 `proxy` / `browser` 是全来宾代理，勿与
`smart-proxy` 共用同一监听端口。分流浏览器与扫码浏览器使用不同配置目录。
Windows 停止分流入口时关闭由它打开的独立浏览器；Linux 需自行关闭分流浏览器，
并在改端口/重开分流入口前先关闭旧实例。手动配置 PAC 的其他浏览器需自行撤下该配置。

SSH 使用原生 OpenSSH 的 `ProxyCommand`，保留目标的主机密钥检查和密码交互：

```powershell
must-vm ssh-config
```

将输出的配置片段加入自己的 SSH config，且放在已有通用 `Host *` 代理规则前面。
它包含配置域名和单地址 `/32`；更大 CIDR 的 SSH 主机需自行加到相同 `Host` 段。
随后照常 `ssh 用户名@目标`，SCP/SFTP 也使用同一连接规则。
底层入口是 `must-vm ssh-connect %h %p`，只传输协议字节，不保存密码。
`must-vm server-status` 只读本地就绪状态，不启动 VM、不探测关闭的服务器。

VPN 连接在来宾内解析 IPv4、检查目标的 utun 路由，并用 `SO_BINDTODEVICE` 把 TCP
连接固定到该 utun 网卡。绑定失败或 VPN 路由不存在时拒绝转发，不从来宾 eth0 出去。
辅助脚本经固定主机密钥的 SSH 发送，并仅在来宾用 sudo 执行；已有镜像无需重建。
HTTPS/SSH 内容原样转发，不解密、不重放请求。自动回退仅发生在 DNS/TCP 建连失败时；
HTTP 403/500、证书错误、SSH 认证失败及已经建立的会话断线不会切换线路。
浏览器 PAC/SOCKS 行为依据 [Chromium 代理文档](https://chromium.googlesource.com/chromium/src/+/main/net/docs/proxy.md)，
SSH 集成依据 [OpenSSH ProxyCommand 文档](https://man.openbsd.org/ssh_config)，
来宾网卡绑定依据 [Linux socket 文档](https://man7.org/linux/man-pages/man7/socket.7.html)。

## 网络结构

```text
独立扫码浏览器（自动选择本机 SOCKS5 端口）/ 指定应用（127.0.0.1:1088 SOCKS5）
  → SSH direct-tcpip（独立密钥、固定来宾主机密钥、DNS 在来宾解析）
  → 来宾 aTrust → 来宾 eth0
  → QEMU socket Ethernet → gVisor TCP/UDP 网关
  → Windows IP_UNICAST_IF + 本机物理 IPv4 / Linux SO_BINDTODEVICE
  → 学校 VPN
```

QEMU 没有默认 NAT、TAP、宿主共享目录或默认网卡。网关不使用系统代理和宿主 DNS；
来宾 DNS 也是经物理出口转发的 UDP/TCP。只支持 IPv4，不透明承载 ICMP/ESP；
如果学校隧道必须使用其他 IP 协议，此实现不能连接。

仅允许配置正在工作的物理网卡，地址改变或网卡掉线会停止网关和虚拟机；
没有默认路由回退。不改宿主路由、DNS、Clash、网卡驱动或防火墙规则。
这能避开路由型 TUN，但不能保证越过宿主 WFP 级强制重定向；需要实际环境测试。

## Windows 使用

目标平台：Windows 10/11 x64。MSI 按用户安装，自带 Python、网关、QEMU x86_64
和 Ubuntu 基础镜像。不要求 Docker 或 WSL；默认 auto 优先使用已可用的 WHPX（Linux KVM），
不可用则回退 TCG 软件模拟，后者速度较慢。TCG 翻译缓存限制为 32 MiB，避免默认大缓存
持续抬高宿主内存。可以明确选择 `--accelerator tcg` / `whpx`；
程序不会替你启用系统功能或重启。ARM Windows 尚未验证。

安装 MSI 后会创建桌面图标。首次使用时重新打开终端（或进入 `%LOCALAPPDATA%\MUST VPN VM App`），
完成一次性配置：

```powershell
must-vm adapters
must-vm configure --deb 'E:\software\songfor\MUSTVPN_amd64[https@vpn.must.edu.mo@443].deb' --interface WLAN --source <上一步物理IPv4> --dns <可直连的DNS-IPv4>
must-vm terminal
```

`terminal` 会引导后续安装、登录和终端使用。也可以用原来的手动分步流程：`run` 持续前台运行，
另开终端，等待第一次初始化完成：

```powershell
must-vm status
must-vm install-vpn
must-vm start-vpn
must-vm browser
```

* `install-vpn` 只通过固定 SSH 主机密钥进入本项目 VM，校验 DMI 和来宾标记，再安装
  导入的原始 DEB。需要物理网络可访问 Ubuntu 软件源。绝不在宿主调用 dpkg 或厂商程序。
* `browser` 使用独立浏览器目录、SOCKS5 和 `<-loopback>`，把学校门户对 localhost
  客户端接口的检测送进来宾。扫码、验证码、MFA 由用户按学校正常流程完成。
  `terminal` 自动打开浏览器时使用空闲端口，并在 VPN 就绪后关闭该端口和隔离浏览器；
  若先前的隔离浏览器仍在运行，再次打开前会先关闭旧实例，以更新代理端口。
  单独运行 `must-vm browser` 仍默认使用 1088，可用 `--port` 更改。
  浏览器加 `--disable-external-intent-requests` 阻止网页启动宿主外部应用；若浏览器不支持
  该 Chromium 参数，不要允许“打开 aTrust”提示。如果门户强制自定义协议 `atrust://`
  或其他不可代理的检测，当前浏览器桥接可能不兼容，不以宿主 aTrust 作为回退。
  客户端本地 HTTPS 证书的公钥通过已固定主机密钥的 SSH 读取，仅为这个独立浏览器进程
  配置该公钥的证书例外；不导入宿主根证书、不全局关闭 TLS 检查。原厂证书序列号为 0，
  目前固定使用兼容的 cryptography 48.0.1。
* 校内应用显式配置 `socks5h://127.0.0.1:1088`。普通应用和 Clash 不受本工具配置影响。
  只有 TCP CONNECT；主机侧 SOCKS 不支持 UDP ASSOCIATE（来宾出口支持 UDP）。
* `must-vm proxy` 只启动代理，不打开浏览器。`--port` 可换端口。
* 不支持 SOCKS 的 SSH 客户端可以先运行 `must-vm forward 校内服务器IP:22 --port 2222`，
  再在另一终端运行 `ssh -o HostKeyAlias=校内服务器IP -p 2222 用户名@127.0.0.1`。
  这样密码由系统 SSH 直接询问，不交给本工具保存；HostKeyAlias 复用服务器的已知主机密钥。
* `must-vm exec 'free -m; ip route'` 在来宾执行诊断命令。
* `must-vm shutdown` 正常关机；`run` 窗口 Ctrl+C 是强制停止，仅适合故障处理。
  主进程崩溃时不保证来宾正常关机，重启前确认旧 QEMU 已结束。

虚拟机磁盘上限 6 GiB，qcow2 按需增长；默认 768 MiB RAM、1 vCPU。
`--memory 512` 可试降内存，但不是已证明的最低值。
首次初始化只创建系统和密钥，不安装/启动 VPN；安装器也没有启动自定义动作。

Windows 新配置保存在 `%USERPROFILE%\.must-vpn-vm`，避免打包桌面应用对子进程
AppData 的重定向导致桌面入口看不到数据。没有新目录时，继续使用可访问的旧目录
`%LOCALAPPDATA%\MUST-VPN-VM`；不会自动覆盖或重建旧虚拟机。
Linux 仍使用 `~/.local/share/MUST-VPN-VM`。
状态目录
包括 SSH 密钥、来宾磁盘、登录 Cookie 和日志。Windows 设置仅当前账户/SYSTEM 可访问
的 ACL；Linux 目录 0700。卸载保留数据，避免丢失登录状态。
可以用 `MUST_VM_HOME` 指定另一独立目录。不要上传这个目录或分享已经登录的镜像。
桌面入口也遵守此变量；程序显示实际使用的路径，并向子进程传递同一路径。
若旧数据只在某个打包应用的私有 AppData 缓存中可见，先关闭虚拟机和独立浏览器，
将完整状态目录复制到 `%USERPROFILE%\.must-vpn-vm`，保留原目录作为备份。
迁移后的目录应保持仅当前账户和 SYSTEM 可访问；不要只复制 `config.json`。
换网后在停止 VM 的前提下修改该目录 `config.json` 的 `interface` / `source`，再运行。

## Ubuntu 宿主版本

目标 Ubuntu 24.04 x64。安装构建出的 `must-vpn-vm_0.2.9_amd64.deb` 后：

```sh
must-vm adapters
must-vm configure --base /path/to/base.qcow2 --deb /path/to/MUSTVPN.deb \
  --interface enp3s0 --source 192.168.1.123 --dns 192.168.1.1 --accelerator kvm
must-vm terminal
```

Linux 包通过依赖安装 QEMU；基础镜像按 runtime-lock.json 下载并验证 SHA-256。
KVM 需要当前用户有 `/dev/kvm` 权限；可改 TCG。
Linux `SO_BINDTODEVICE` 在受限系统可能要求 CAP_NET_RAW；绑定失败直接报错，不回退。
不要用 root 启动独立认证浏览器。

## 构建与测试

Go 1.26+，Python 3.11+；依赖锁定在 requirements.txt / gateway/go.mod。

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
cd gateway
go test ./...
go build -o ../must-gateway .
```

Windows：安装 PyInstaller、pefile、WiX 3.14，准备已校验的 QEMU 解包目录及
`runtime/base.qcow2`，运行 `packaging/build-windows.ps1 -QemuSource ... -Wix ...`。
MSI 不包含学校 DEB；用户本地导入。构建得到的第三方运行时需保留许可证，见 THIRD_PARTY.md。
Linux：Ubuntu 24.04 上运行 `sh packaging/build-linux.sh`。

## 包分析和瘦身

检查的学校 DEB：aTrust 2.5.16.30，144,575,768 字节，SHA-256 固定在 runtime-lock.json。
`aTrustTray` 单个 Electron 主程序 130,990,712 字节，Xtunnel 26,026,920 字节。
安装脚本有杀进程、清理 iptables、启动服务等行为，不能在宿主运行。

目前保留原包，没有删改签名或认证/设备合规组件。采用“来宾不运行 Electron，不装桌面，
浏览器在宿主按需运行”的方式节省内存。进一步删减包文件须先通过扫码、重连和资源访问验收，
否则不能证明删掉的组件不影响学校策略。

实现依据：[QEMU 网络](https://www.qemu.org/docs/master/system/devices/net.html)、
[gVisor netstack](https://gvisor.dev/docs/architecture_guide/networking/)、
[Windows IP_UNICAST_IF](https://learn.microsoft.com/en-us/windows/win32/winsock/ipproto-ip-socket-options)。
