# 验证记录

2026-09-27，Windows 11 x64 开发机实测。用户已亲自在隔离浏览器中完成扫码并报告登录成功。
随后用户纠正了 SSH 用户名，并在严格校验既有主机密钥的原生 SSH 窗口中自行输入密码，
确认校内 SSH 登录成功。密码没有写入代码、配置或 GitHub。

## 已通过

- 原始 DEB 移动后 SHA-256 与 runtime-lock.json 一致；只静态解包，没有在 Windows
  或 WSL 安装/启动厂商 VPN。WSL 仅用于编译 Linux 版本。
- 9 个 Python 测试、4 个 Go 测试；此前 GitHub Actions 的 Windows / Ubuntu 测试通过。
- TCG 启动 Ubuntu Minimal；云初始化读取 virtio 只读 seed；固定 SSH 主机密钥认证通过。
- 来宾 DNS 查询成功、HTTPS example.com 返回 200。
- 宿主 curl → SOCKS5 → SSH → 来宾 → 用户态物理出口，HTTPS 返回 200。
- Meta Tunnel 为 Up 且有默认路由的同时，网关 TCP 连接的源 IPv4 是 WLAN 的物理地址。
  这是本机样本的验证，不代表所有 WFP 驱动组合。
- 来宾安装 aTrust 2.5.16.30 成功（dpkg --audit 无输出）。桌面相关安装脚本产生非致命
  警告；首次缺少 libproxy / libharfbuzz，已补进来宾安装依赖。
- 显式启动守护服务和独立认证核心后，54630 / 54631 等认证接口监听；无 Electron、
  Xorg、Xvfb。经 SSH 读取到了本地 API 的证书公钥。
- HTTP `/v1/service/status` 未认证探测返回 503；仅证明代理链到达接口，**不代表认证成功**。
- 更新 KillMode 后停止客户端，`ps` 未发现残留 aTrust 进程；正常来宾关机成功。
- 登录后，来宾出现 utun7 和学校下发的资源路由；宿主 WLAN / Meta 均仍为 Up，
  经隔离代理的普通 HTTPS 仍返回 200。没有启动 Windows 本机的 aTrust。
- 校内 SSH 目标的 `ip route get` 指向 utun7；TCP/SSH 握手、既有主机密钥验证通过。
  本地 2222 TCP 转发经已固定密钥的来宾 SSH 进入 VPN；用户确认服务器账号登录成功。
- Windows 初版 MSI 真实安装返回 0，安装后启动器和精简 QEMU 可以运行。
- 0.1.1 MSI 管理解包返回 0；解包后的启动器（含 forward）和 QEMU 可运行，随后真实升级安装成功。
- 0.2.1 MSI 真实升级安装返回 0；桌面图标存在且指向安装版 `must-vm.exe terminal`。
- 用户反馈桌面双击时控制台一闪而过；0.2.2 MSI 已升级安装，图标现在指向
  `terminal.cmd`，非零退出会保留错误窗口。直接从管理员 PowerShell 启动原快捷方式可开机；
  普通桌面双击的原始退出原因仍待用户回报。
- 新终端入口实测启动虚拟机、VPN 服务和隔离浏览器。初版只检查 utun 路由时曾过早放行：
  当时 `ip route get 10.100.16.13` 仍走 eth0。修正后等待该校内地址实际走 utun7，
  用户完成扫码后才打开虚拟机交互式终端。终端内 `/usr/bin/ssh` 可用，校内 SSH 22 端口
  TCP 连接成功；`exit` 后自动正常关机。
- Ubuntu 原生打包成功，dpkg-deb 元数据检查通过，冻结后的 Linux 启动器 `--help` 可运行。
- 512 MiB 来宾配置完成重启和真实扫码登录；最终仍保留 768 MiB 默认配置以留出认证峰值余量。
- QMP 预启动探测确认本机 WHPX 可初始化；`tcg,tb-size=32,thread=single` 参数可正常初始化。
- 另用无网卡、无 VPN 的基础镜像验证 auto 硬件加速启动，到达 Linux 登录提示符。

## 资源样本

768 MiB 配置、1 vCPU、TCG：无 VPN 的来宾 `free -m` used 约 216 MiB；
守护进程和认证核心运行、尚未登录时约 268–287 MiB。
同一无 VPN 样本中，Windows QEMU 工作集约 767 MiB，网关约 53 MiB，另外还有
启动器及按需浏览器。来宾 used 不能当作整套软件的宿主内存。
后续 512 MiB 样本登录后 guest used 约 190 MiB；但未限制的 TCG 翻译缓存让 QEMU
宿主工作集增长到约 1.43 GiB。因此最终代码显式限制 TCG 缓存 32 MiB，并优先使用可用
的硬件加速。最终组合的长期工作集还需要持续测量，不能宣传成 190 MiB 总内存。

Windows 初版 MSI 约 291 MiB（带 QEMU 和基础镜像），Linux 包约 15 MiB（不带它们）。
6 GiB 是来宾磁盘容量上限，不是下载大小或即时实际占用。

## 尚未完成的验收

- 真实断网后 fail-closed（仅做了无效网卡拒绝测试，未为测试断开宿主 WLAN）。
- VPN 更新、不同 Windows 版本、Linux KVM。
- Windows ARM、受管理的浏览器策略、认证高峰内存以及纯校内网络表现。

源码测试不能替代真实 VPN 验收，也不能证明任意 WFP 拦截场景可用。
