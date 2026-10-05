# MUST VPN Router 0.3.1（独立预览版）

在 Windows 或 Linux 的普通终端使用本机 OpenSSH，经独立虚拟机的学校 VPN 访问 AISC。
这是独立产品：安装 Router 不会升级、卸载或覆盖 MUST VPN VM。

| 项目 | Router | 原版 |
| --- | --- | --- |
| 命令 | `must-router` | `must-vm` |
| Windows 安装目录（LocalAppData 下） | `MUST VPN Router App` | `MUST VPN VM App` |
| Windows 数据目录（用户目录下） | `.must-vpn-router` | `.must-vpn-vm` |
| Linux 程序目录 | `/opt/must-router` | `/opt/must-vm` |
| Linux 数据目录 | `~/.local/share/MUST-VPN-Router` | `~/.local/share/MUST-VPN-VM` |
| VPN / 分流 SOCKS / PAC 端口 | 1189 / 1188 / 18765 | 1089 / 1088 / 8765 |

Router 使用单独的 MSI UpgradeCode、组件身份、DEB 包名和浏览器配置目录。
`MUST_ROUTER_HOME` 或 `--home PATH` 可选择 Router 数据目录；不继承 `MUST_VM_HOME`。
所有入口只监听本机回环地址，不修改系统代理、路由或 Clash。

## 一次性准备

已配置过原版的 Windows 用户，先在原版执行 `must-vm shutdown`，等虚拟机完全关闭，再运行：

```powershell
must-router init --from-state "$env:USERPROFILE\.must-vpn-vm"
must-router serve
```

`init` 完整复制虚拟磁盘、引导文件和连接身份，原数据保留；正在运行的磁盘会被拒绝。
需要约 3 GB 额外空间，复制的是已有虚拟机，学校 VPN 和来宾里的 SSH 配置也会保留。
Linux 的 `--from-state` 使用原版的 Linux 数据目录。
从零配置也可用 `must-router configure --help`；Linux 需另行准备文档中的基础系统镜像。

`serve` 打开 VPN 服务及分流入口，登录过期时打开独立的扫码浏览器。
首次在另一个普通终端运行：

```powershell
must-router import-ssh AISC AISC-CPU
must-router ssh-setup --install
ssh AISC
ssh AISC-CPU
ssh AISCCPU
```

`import-ssh` 通过已固定主机密钥的来宾连接，导入这些别名的用户名、私钥和已信任的目标主机密钥。
它们仅保存在 Router 私有数据目录的 `ssh` 子目录，Windows 使用当前账户与 SYSTEM 的 ACL，
Linux 使用 0700/0600；不会进入源码或安装包。目标主机密钥保持严格校验。
`ssh-setup --install` 在用户 SSH config 顶部添加一条 Include，先保留原文件的完整备份。
Include 定义导入的别名，并在 OpenSSH 的 final 阶段匹配配置的学校域名、单地址网段，
让 `ssh 用户@学校域名` 及已有 HostName 指向学校的别名也能参与回退。
已有别名显式设置的 ProxyCommand 优先保留。若原来已有同名 AISC 别名，请先查看新配置以确认选择。
不安装 Include 时可直接 `must-router ssh AISC`，仍使用本机 OpenSSH。
SCP/SFTP 使用相同别名，例如 `sftp AISC`。

后续双击桌面 **MUST VPN Router** 或运行 `must-router serve`，等就绪后在普通终端连接。
Ctrl+C 正常停止服务器，并关闭由它启动的虚拟机；单独运行的 VM 不会因此被关闭。
服务器未启动或 VPN 未就绪时，AISC 连接立即失败，不会自动开机或反复探测代理端口。

## 设置

Windows 双击桌面 **MUST VPN Router 设置**，Linux 从应用菜单打开 **MUST VPN Router Settings**，
即可在本机浏览器中打开可视化设置。也可以运行 `must-router settings`。
页面提供模式选择、指定域名、学校回退开关、直连等待时间和 SSH 端口；点“保存设置”保存。
打开设置不需要启动虚拟机。保存后重启 Router，并重新打开分流浏览器。
点击“关闭设置”退出；直接关闭页面后，设置服务会在两分钟内自动退出。
设置页只监听随机的本机端口，需要本次启动的访问凭证，不对局域网开放。

命令行设置仍然可用，`--show` 只打印当前配置：

```powershell
must-router settings --show
must-router settings --mode ssh
must-router settings --mode all
must-router settings --mode domains --domain aisc.must.edu.mo --domain another.school.example
must-router settings --fallback off
must-router settings --fallback on --direct-timeout 4
```

修改后重启 `serve`，并重新打开分流浏览器。

| 模式 | 行为 |
| --- | --- |
| `ssh` | 仅 SSH 端口参与 AISC 代理和学校回退；Web 直连 |
| `all`（默认） | AISC 的 SSH 和 Web 经 VPN；其他学校地址直连优先；其他网站直连 |
| `domains` | 只有指定域名及其子域名经 VPN；其他地址直连，不参与学校回退 |

“都代理”限定为 AISC，未扩大到整个互联网。域名输入裸域名，不带 URL、路径或 `*.`。
重复 `--domain` 添加多个域名；每次设置替换整份域名清单。域名模式不会匹配直接输入的 IP。
SSH 默认识别 22 端口；`--ssh-port 22 --ssh-port 2222` 可替换 SSH 端口清单。
可用 `--aisc-host`、`--aisc-network`、`--school-host`、`--school-network` 调整范围。
默认 AISC 包含 `aisc.must.edu.mo`、`10.100.16.13/32`、`10.100.16.8/32` 和 Web 地址
`172.16.130.167/32`；学校域名为 `must.edu.mo` 及子域名。

## Web 与回退范围

```powershell
must-router web
```

此命令打开独立的 Edge/Chrome/Chromium 配置目录并访问 AISC 登录页。
其他浏览器若支持 PAC，可手动使用 `http://127.0.0.1:18765/proxy.pac`。
Web 分流不会自动作用于未配置 PAC 的普通浏览器，也不会接管已有浏览器会话。

正常学校流量从宿主网络发出。为了判断失败并回退，学校请求会先进入本机分流器；
不进入虚拟机。域名和 TCP 错误会触发回退；SSH 还检查服务端识别信息，HTTPS 检查证书有效的
TLS 握手，HTTP 80 端口用单独的 HEAD 请求检查协议响应。探测连接和真实连接分开，
不会重放用户的请求体或登录数据。已建立连接中的应用错误、HTTP 403/500 或后续断线不自动重放。
宿主 TUN 路由仍适用，分流器不会继承应用级代理环境变量。

回退前同时检查进程锁、12 秒内心跳及来宾 VPN 路由；服务器关闭、崩溃或未就绪时不尝试回退。
如果只需要学校普通网站完全绕过本地分流器，可选择 `domains` 模式。
独立分流器 `must-router smart-proxy --browser` 也能在服务器未开启时运行，但不能与
`serve` 占用同一个 1188 / 18765 端口。

## 移除本机 SSH 集成

从用户 `.ssh/config` 删除指向 `.must-vpn-router/ssh/config` 的 Include 即可。
安装器不删除私有数据；需要清理时先关闭 Router 和虚拟机，再按需保留/删除独立数据目录。
保留原版不需要恢复虚拟磁盘或卸载 Router。

构建入口为 `packaging/build-router-windows.ps1` 与 `packaging/build-router-linux.sh`。
发布包仅包含程序和公开运行时，不包含私人 VM、SSH 私钥、学校登录状态或学校专有 DEB。
