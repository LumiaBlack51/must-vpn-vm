# MUST VPN Router 0.3.0 验证记录（2026-10-05）

## 真实网络和本机 SSH

- 环境：Windows 宿主已有 TUN 网络，独立 Ubuntu Minimal 虚拟机运行学校 VPN。
  用户在独立浏览器中完成学校认证；访问 AISC 的来宾路由为 utun。
- 从来宾配置导入 AISC / AISC-CPU 的指定 SSH 身份和已信任主机密钥，保存在
  Router 私有目录。未在仓库、测试报告或发布包中保存这些凭据。
- Windows 普通终端运行 `ssh AISC`、`ssh AISC-CPU`、`ssh AISCCPU`，实际公钥认证成功。
  三个入口均完成 262144 字节二进制往返、半关闭后读取响应，数据逐字节一致。
- `https://aisc.must.edu.mo/auth/public/auth?callbackUrl=https%3A%2F%2Faisc.must.edu.mo%2Fapi%2Fauth%2Fcallback`
  经虚拟机 VPN 返回 HTTP 200，使用默认 TLS 证书验证。
- `https://www.must.edu.mo/` 返回 HTTP 200，监测到的 Router VPN 调用数为 0。

## 回退与离线行为

- 宿主普通连接对校内 SSH 立即报告 TCP 建连成功，但没有 SSH 服务端识别信息。
  因此新增单独的 SSH 协议探测，避免误判直连可用；真实 SSH 会话使用另一条连接。
- 测试时仅在测试 Router 策略中把两个 AISC IP 归为“学校直连优先”。实际直连探测超时后，
  分别约 4.62 / 4.69 秒完成 VPN 回退、公钥登录和 262144 字节二进制往返；每次 VPN 调用为 1。
  发布默认策略仍将两个地址固定走 VPN。
- 当前网络也能直连 AISC Web，因此真实 Web 直连优先测试没有触发回退。
  另行注入一次直连 TLS 超时，实际 VPN 回退返回 HTTP 200，TLS 验证通过，VPN 调用为 1。
- 强制终止测试服务器，保留就绪文件，进程锁释放后立即判定离线。AISC 和学校回退均为
  0 次代理拨号；1188 / 1189 / 18765 端口全部关闭，没有遗留分流子进程。
- 测试结束后正常关闭测试虚拟机，验证 run.lock 已释放。
- 回退覆盖建连和 SSH/TLS/HTTP 协议可达性，不重放 HTTP 请求体，不自动恢复已建立会话。

## 自动测试与构建

- Windows Python 3.13、WSL Ubuntu Python 3.12 各 54 项测试通过；Go test / vet 通过。
- 覆盖三种模式、域名边界、原版环境变量隔离、子进程状态目录、拒绝复制运行中的 VM、
  SSH 配置备份和重复安装、OpenSSH final 匹配已有学校别名、严格目标主机密钥验证，
  以及 TCP 假连接、TLS 超时、离线禁止代理、SOCKS 二进制传输和旧版测试。
- Node 执行三种模式的 PAC JavaScript，共 18 个域名/IP 样本通过。
- MSI 和 DEB 内冻结程序完成 262144 字节标准输入/输出及半关闭测试。
- Windows 安装后的三种 settings 命令在独立临时目录实际执行并检查保存结果。

## 安装与隔离

- MSI 实际安装，msiexec 返回 0。原版 MUST VPN VM 与 MUST VPN Router 的程序目录和
  桌面快捷方式同时存在；使用不同 MSI UpgradeCode 和组件 GUID。
- 从关闭的原 VM 复制出独立、无 backing file 的磁盘，Router QEMU 路径指向自身安装目录。
  原版测试配置已撤回；安装 Router 后原版磁盘和原版可执行文件的 SHA-256 未改变。
- Router 独立目录为 `.must-vpn-router`，原版为 `.must-vpn-vm`。
  用户 SSH config 原内容先完整备份，再添加一条独立 Include；原有别名与内容保留。
- WSL Ubuntu 实际安装 DEB 0.3.0，补齐包声明的 QEMU 依赖后配置成功。
  Linux 的 help、server-status 和安装后二进制通道检查通过。
- 发布目录扫描未发现私人 VM、导入的 SSH 私钥、会话文件或学校专有 DEB。

## 验证范围

完整真实学校 VPN 验证在 Windows 宿主完成；Linux 验证包含单元测试、DEB 安装及本地数据通道，
未在 Linux 物理宿主重新完成学校 VPN 扫码和端到端校内访问。
学校认证可能在重启后要求再次扫码，程序遵循实际认证状态。
