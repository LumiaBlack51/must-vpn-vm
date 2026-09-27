# MUST VPN VM — 开发预览版

把学校 aTrust 放进独立 QEMU 虚拟机，保留 Windows / Ubuntu 宿主机的 Clash。
当前实现为命令行软件，来宾系统为 Ubuntu Minimal 24.04 amd64，无桌面、无浏览器。
学校扫码登录使用宿主机独立的 Chromium / Edge 配置目录。

**状态：需要学校账号完成端到端认证验收。不能承诺所有 Windows 电脑、所有
Clash/WFP 驱动、所有学校认证策略都兼容。** 详见 [验证记录](docs/VALIDATION.md)。

## 网络结构

```text
独立扫码浏览器 / 指定应用（127.0.0.1:1088 SOCKS5）
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
和 Ubuntu 基础镜像。不要求 Docker、WSL 或 Hyper-V；默认 TCG 软件模拟适配面较广，
速度较慢。已启用 Windows Hypervisor Platform 时可选择 `--accelerator whpx`，
程序不会替你修改系统功能或重启。ARM Windows 尚未验证。

安装 MSI 后重新打开终端（或进入 `%LOCALAPPDATA%\MUST VPN VM App`）：

```powershell
must-vm adapters
must-vm configure --deb 'E:\software\songfor\MUSTVPN_amd64[https@vpn.must.edu.mo@443].deb' --interface WLAN --source <上一步物理IPv4> --dns <可直连的DNS-IPv4>
must-vm run
```

`run` 持续前台运行。另开终端，等待第一次初始化完成：

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
  如果门户强制自定义协议 `atrust://` 或其他不可代理的检测，当前浏览器桥接可能不兼容。
  不把宿主机现有 aTrust 当作回退客户端。
* 校内应用显式配置 `socks5h://127.0.0.1:1088`。普通应用和 Clash 不受本工具配置影响。
  只有 TCP CONNECT；主机侧 SOCKS 不支持 UDP ASSOCIATE（来宾出口支持 UDP）。
* `must-vm proxy` 只启动代理，不打开浏览器。`--port` 可换端口。
* `must-vm exec 'free -m; ip route'` 在来宾执行诊断命令。
* `must-vm shutdown` 正常关机；`run` 窗口 Ctrl+C 是强制停止，仅适合故障处理。
  主进程崩溃时不保证来宾正常关机，重启前确认旧 QEMU 已结束。

虚拟机磁盘上限 6 GiB，qcow2 按需增长；默认 768 MiB RAM、1 vCPU。
`--memory 512` 可试降内存，但不是已证明的最低值。
首次初始化只创建系统和密钥，不安装/启动 VPN；安装器也没有启动自定义动作。

状态保存在 `%LOCALAPPDATA%\MUST-VPN-VM`（Linux `~/.local/share/MUST-VPN-VM`），
包括 SSH 密钥、来宾磁盘、登录 Cookie 和日志。Windows 设置仅当前账户/SYSTEM 可访问
的 ACL；Linux 目录 0700。卸载保留数据，避免丢失登录状态。
可以用 `MUST_VM_HOME` 指定另一独立目录。不要上传这个目录或分享已经登录的镜像。
换网后在停止 VM 的前提下修改该目录 `config.json` 的 `interface` / `source`，再运行。

## Ubuntu 宿主版本

目标 Ubuntu 24.04 x64。安装构建出的 `must-vpn-vm_0.1.0_amd64.deb` 后：

```sh
must-vm adapters
must-vm configure --base /path/to/base.qcow2 --deb /path/to/MUSTVPN.deb \
  --interface enp3s0 --source 192.168.1.123 --dns 192.168.1.1 --accelerator kvm
must-vm run
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
