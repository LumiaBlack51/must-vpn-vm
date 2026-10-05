# Router 0.3.2 统一主界面验证（2026-10-05）

本版把 VPN 启停、状态和代理设置合并到一个本机窗口。Windows MSI 桌面和 Linux 应用菜单只保留 MUST VPN Router 入口。打开窗口不自动开机；关闭窗口时已经启动的 VPN 保持后台运行，再次打开复用同一个控制服务。

## 自动检查

- Windows：67 项 Python 检查通过，Go gateway 测试通过。
- Ubuntu/WSL：67 项 Python 检查完成（1 项 Windows 专用检查跳过）。
- GitHub Actions Windows / Ubuntu 均通过：[运行记录](https://github.com/LumiaBlack51/must-vpn-vm/actions/runs/37261256950)。
- 新增覆盖：重复启动只创建一个工作线程、重启读取新策略、启动取消后正常关机、不关闭单独启动的 VM、启动错误反馈，以及控制 API 的来源和访问凭证验证。
- Windows 冻结 CLI、冻结无控制台 GUI、Linux 冻结 CLI 和安装后的 DEB：验证主页面、状态 API、仅 SSH 保存、无配置启动的错误提示、单实例重开、退出清理及无自动开机。

## Windows 本机验证

- MSI 升级成功，返回 0；已安装命令报告 0.3.2。
- 桌面 MUST VPN Router 指向安装目录 must-router-ui.exe；此前独立设置快捷方式已备份并移除。
- 已通过独立 Edge 应用窗口打开本机主界面。VPN 控制与三种代理模式显示在同一页面。
- 源码主界面实测启动独立 VM、进入学校认证等待，点击停止后 VM 正常关机，状态回到 stopped；没有剩余有效 server endpoint。
- 安装后的 GUI 再次实测启动期间取消，等待可关机后 VM 正常退出，状态回到 stopped。
- 测试主机配置保留为 traffic_mode=ssh。AISC SSH 经 VPN；AISC Web 和普通学校 Web 的策略均为 direct。
- 设置保存使用安装目录的主程序生成 SSH ProxyCommand，无需虚拟机终端。
- 原版 MUST VPN VM 的可执行文件和磁盘 SHA-256 与原隔离基线一致，原目录仍没有新增 routing.json。

## 验证范围

本次学校 VPN 再次要求扫码认证，因此验证了主界面启动、取消和关机，没有重新完成本版的 AISC SSH 认证或 Web 转发实测。既有真实 SSH/Web/回退记录见 [0.3.0 验证记录](ROUTER-VALIDATION-2026-10-05.md)。本次 Linux 验证覆盖构建、DEB 安装和本机控制 API，不包含 Linux 物理网卡上的 VPN 实测。
