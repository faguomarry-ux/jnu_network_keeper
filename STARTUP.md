# Windows / Linux 配置和开机自启动

两个入口都要配合本项目完整目录使用，不是只复制单个文件。先安装 Python 3.10+，项目不需要 pip 依赖。把项目放在长期保留的位置；系统应能自动连接校园网网卡/Wi-Fi。

## Windows：运行一个 PS1 完成配置和开机启动

脚本：[setup-windows.ps1](setup-windows.ps1)。默认模式为 **Boot**：系统启动后运行，不要求先登录桌面。

用**同一个 Windows 账号**打开“管理员 PowerShell”，进入项目目录：

```powershell
cd D:\swap\jnu_network_keeper
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1
```

安装依次完成：

1. 查找 Python，提示输入校园网学号、校园网密码。已有配置时直接回车保留原值。
2. 以 Windows DPAPI 保存校园网密码。
3. 提示输入**当前 Windows 账户密码**，用于系统在尚未登录桌面时启动任务。它不是校园网密码，也不是 Windows Hello PIN。
4. 注册计划任务 `JNU Network Keeper`，设置开机延迟 30 秒启动，并立即启动一次。通过 `pythonw.exe` 隐藏运行。

Windows 账户密码只在安装时传给任务计划程序，由 Windows 管理；脚本不会把它写入配置、日志或命令行。任务以当前用户普通权限运行，校园网凭据仍按该用户的 DPAPI 解密。不要用“另一个管理员账号”安装原账号的配置。

**配置参考命令**（密码保持交互输入）：

```powershell
# 指定解释器和检测间隔
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1 `
  -Python 'C:\Path\To\Python\python.exe' -Interval 60

# 本机已经保存账号：复用配置，直接安装/更新启动任务
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1 -ReuseConfig

# TUN/虚拟 DNS 干扰时才需要；源地址必须换成本机校园网 IPv4
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1 `
  -GatewayIp '192.168.11.68' -SourceAddress 'YOUR_CAMPUS_IPV4'
```

如果希望编辑 PS1 中的默认配置，可修改顶部参数的默认值，例如 `$Interval = 60`；账号密码使用提示输入。已有配置重新执行时会保留未指定的网关/源 IP。要清除旧绑定，可在 PowerShell 内直接调用 `& .\setup-windows.ps1 -GatewayIp '' -SourceAddress ''` 并回车保留账号密码，或编辑私有 JSON 配置。

如果没有可用的 Windows 账户密码，或者只希望登录桌面后运行，使用 **Logon** 模式：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1 -StartupMode Logon
```

Logon 模式无需 Windows 账户密码，只在当前用户登录后运行，不满足“未登录桌面也保活”的场景。Windows 账户密码变更后，应重新运行 Boot 安装来更新任务凭据。若组织策略禁止批处理登录或密码任务，需要管理员调整对应策略；不能用“禁止存储密码”的 S4U 模式替代现有 DPAPI 凭据方案。[微软任务登录类型说明](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_logon_type)、[任务注册凭据说明](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask)。

**检查及管理：**

```powershell
# 检查触发器：Boot 模式应显示 MSFT_TaskBootTrigger
(Get-ScheduledTask -TaskName 'JNU Network Keeper').Triggers | Format-List *
Get-ScheduledTask -TaskName 'JNU Network Keeper' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'JNU Network Keeper'

# 检查网络；读取日志
.\start-windows.cmd -Action Status
.\start-windows.cmd -Action Logs

# 停止 / 恢复 / 重启 / 卸载自启动
.\start-windows.cmd -Action Stop
.\start-windows.cmd -Action Start
.\start-windows.cmd -Action Restart
.\start-windows.cmd -Action Uninstall
```

配置及日志在 `%USERPROFILE%\.config\jnu_network_keeper\`。重新运行 setup 会更新本项目、同一用户的现有任务，并重新启动；安装前应停止独立前台运行的 keeper。不要移动/删除安装后的项目目录或解释器。如果项目移位，先卸载旧任务再到新目录运行 setup。

## Linux：运行一个 SH 完成配置和开机启动

脚本：[setup-linux.sh](setup-linux.sh)。适用于以 **systemd** 管理系统服务的 Linux。

**登录前 Wi-Fi 自动连接、软件开关恢复及进程保护**：见 [Linux 恢复说明](LINUX-RECOVERY.md)。已有账号配置时可直接升级：

```bash
bash setup-linux.sh --reuse-config \
  --wifi-interface YOUR_WIFI_INTERFACE \
  --wifi-connection YOUR_SAVED_WIFI_PROFILE_UUID
```

通过 `nmcli -f DEVICE,TYPE,STATE device status` 和 `nmcli -f NAME,UUID,TYPE connection show` 查实际值。需要 WPA 密码的 Wi-Fi 应先把密码保存到系统连接，不能依赖登录后解锁的桌面钥匙环。守护只在需要时恢复软件开关/连接，并限制操作次数。

Linux 新版默认在线随机等待 **1～3 分钟**；`--interval 120 --jitter 180` 改为 **2～5 分钟**，`--jitter 0` 关闭随机。已启用无线守护时，加 `--follow-dhcp` 可跟随源 IPv4，避免 DHCP 换地址后旧绑定失效；例如 `bash setup-linux.sh --reuse-config --follow-dhcp`。本地 Wi-Fi/IPv4 变化会提前结束退避，心跳失败后同轮继续核实网络，认证限频不变。

把发布包解压到服务器的长期目录，以普通用户进入项目目录运行：

```bash
cd /path/to/jnu_network_keeper
bash setup-linux.sh
```

脚本先验证 sudo 权限，提示输入校园网学号、校园网密码，然后创建该用户的私有配置，安装并启用 `jnu-network-keeper.service`，立即启动。后续开机会自动运行，关闭 SSH 会话不影响它。不要把整个交互脚本通过 `curl | bash` 输入，保持终端标准输入供密码提示使用。

安装前先退出独立前台运行的 keeper，避免与服务争用实例锁；已有 systemd 服务由安装脚本重启。

**配置参考命令：**

```bash
# 设置 Python 和检测间隔
bash setup-linux.sh --python /usr/bin/python3 --interval 60

# 直接以 root 登录时：指定已有的普通服务用户
sudo bash setup-linux.sh --user YOUR_LINUX_USER --interval 60

# 服务器本机存在 TUN / DNS 干扰时才使用 IP 覆盖
bash setup-linux.sh \
  --gateway-ip 192.168.11.68 \
  --source-address YOUR_LINUX_CAMPUS_IPV4

# 已经配置账号，复用配置安装/更新自启动
bash setup-linux.sh --reuse-config

# 全部参数说明
bash setup-linux.sh --help
```

Linux 的源地址必须是该服务器自己的校园网 IPv4，不能复制 Windows 电脑的地址。通常直接使用校园 DNS 的服务器不需要这两个 IP 选项。脚本没有密码参数；运行时隐藏输入。配置默认在服务用户的 `~/.config/jnu_network_keeper/config.json`，Linux 密码为明文，文件权限限制为 `0600`；Windows 的 DPAPI 配置不能复制过来。

如果需要改默认配置，编辑 SH 顶部 `INTERVAL=60`，或优先使用命令参数。更改网关、源地址、账号等配置时重新运行 setup，不带 `--reuse-config`；账号密码直接回车可保留原值。自定义配置文件用 `--config /absolute/path/config.json`。服务用户必须能读取项目/解释器，并能写入配置所在目录。

**检查及管理：**

```bash
# 一键测试：离线自动测试 + 本机部署/联网诊断
bash test-linux.sh
# 不请求外网/门户，只检查本地（自动测试仍可使用本地回环网络）
bash test-linux.sh --offline

sudo systemctl is-enabled jnu-network-keeper   # 应为 enabled
sudo systemctl is-active jnu-network-keeper    # 应为 active
sudo systemctl status jnu-network-keeper --no-pager
sudo journalctl -u jnu-network-keeper -f

sudo systemctl restart jnu-network-keeper
sudo systemctl stop jnu-network-keeper
sudo systemctl disable --now jnu-network-keeper
```

重启后可通过 `sudo journalctl -u jnu-network-keeper -b` 查看本次开机日志。查看实际外网状态时仍应检查日志/`state.json`；服务进程处于 active 不代表认证一定成功。遇到网卡未连接、学校停网、验证码或账号策略限制，脚本会记录错误并按退避间隔重试。

一键测试不修改服务和网络，也不消耗登录额度。默认自动识别服务用户及本项目安装器生成的配置路径；定制单元/drop-in 可用 `--config` 指定。报告保存到实际测试用户的 `~/.local/state/jnu_network_keeper/diagnostics/` 下，每次一个私有目录。退出码 `0` 为通过，`1` 为存在警告，`2` 为存在失败，`130` 为中断；未启用的可选功能可显示 SKIP。完整参数见 [Linux 测试说明](LINUX-RECOVERY.md#一键测试与脱敏报告)。

## 三账号顺序备用

以实际服务用户停止 keeper 后，运行 `python3 -m jnu_network_keeper init-accounts --failover-threshold 100000`，交互选择通常 3 个账号、主用槽位并输入隐藏密码，再启动服务。首次 Linux 安装可以使用 `bash setup-linux.sh --accounts --failover-threshold 100000`；已有服务先停止，且不要与 `--reuse-config` 混用。Windows setup 对应 `-Accounts -FailoverThreshold 100000`。

只统计真正提交的登录请求失败，普通断网检测不计数；明确拒绝也按全局限频继续累计。主用及两个备用依次达到阈值后记录 ERROR 并暂停，登录 API 成功后清零并让下次认证重新优先主用。`accounts` 命令只显示槽位/计数，`reset-accounts` 显式重新开始但不清空全局限频。完整规则、100000 次可能需要约 3.8 年/账号的时间含义、密码保存、未确认请求处理见 [README 4.4](README.md#44-账号列表与顺序备用)。

## Linux 卸载和信息清理

使用 `sudo bash uninstall-linux.sh` 先预览，带 `--apply` 才停止服务并删除凭据/日志/状态/默认测试报告；之后运行 `sudo bash check-cleanup-linux.sh` 独立复查。源码默认保留，`--remove-project` 可删除已核实的整个项目目录；会在 `/var/lib/jnu-network-keeper-cleanup/check-cleanup.py` 留一个目录外检查器。自定义配置、报告和备份需要显式列出路径。Wi-Fi 默认恢复本项目改过的字段并保留连接，`--keep-wifi-settings` 可跳过回退；`--forget-wifi` 会删除指定连接，可能断开远程访问。

删除范围、源码删除后的复查、清理凭据的最终删除、退出码和系统 journal/浏览器/备份等边界详见 [README 第 7 节](README.md#7-停用和卸载)。

## 当前验证边界

Windows 已用真实任务定义对象验证 Boot / Password 与 Logon / Interactive 配置，测试中拦截了任务注册和启动，不会修改本机计划任务。Linux 安装脚本通过 Bash 语法与参数入口检查。尚未实际重启 Windows 或在 Linux 实机执行开机测试，详见 [VALIDATION.md](VALIDATION.md)。
