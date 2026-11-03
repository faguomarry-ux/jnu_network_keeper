# 暨南大学校园网保活工具

本项目用于使用 `https://webauthsa.jnu.edu.cn:8443/eportal/` 网页认证的暨南大学校园网，支持 Windows 电脑和 Linux 服务器。它在后台检查网络，掉线后重新认证，并按网关要求发送会话心跳。

**Windows 运行 `setup-windows.ps1` 安装计划任务；Linux 运行 `setup-linux.sh` 安装 systemd 服务。配置一次后，由操作系统负责自启动，无需一直开着浏览器、终端或 SSH 会话。**

需要 **Python 3.10+**，仅依赖标准库，无需安装 pip 包。部署时必须复制完整项目目录，单独复制一个 PS1/SH 文件不能运行。

## 阅读导航

- [保活原理与两端实现](#1-保活原理与两端实现)
- [Windows 部署](#2-windows-部署)
- [Linux 部署](#3-linux-部署)
- [Linux 多账号添加与修改](#34-添加三组账密并指定主用账号)
- [Linux Wi-Fi 登录前联网](#35-wi-fi-登录前联网软件开关恢复与-dhcp-跟随)
- [Linux 一键检测与报告](#37-一键检测与报告判读)
- [Linux 手动检查与排障](#38-手动检查与分层排障)
- [配置和凭据管理](#4-配置和凭据管理)
- [日常维护](#5-日常维护)
- [常见故障排查](#6-常见故障排查)
- [停用和卸载](#7-停用和卸载)
- [代码结构与验证边界](#8-代码结构与验证边界)

## 1. 保活原理与两端实现

### 1.1 共用的 Python 保活逻辑

Windows 和 Linux 使用同一套 `jnu_network_keeper` Python 程序。PS1/SH 负责配置和安装启动项；真正的检测、认证、心跳和重试由 Python 常驻进程完成。

每轮执行顺序如下：

1. **处理到期心跳。** 已取得当前设备会话且网关要求定时心跳时，向 `InterFace.do?method=keepalive` 提交会话标识。
2. **检测外网。** 按顺序请求下表中的检测点，任意一个返回预期状态码和内容即可判定外网可用。普通登录页返回 HTTP 200 不会被误当成联网。
3. **在线时跳过登录。** 初次启动还会尝试取得本设备已有会话，供后续心跳使用。
4. **掉线后检查门户。** 访问 `redirectortosuccess.jsp`，必要时通过发现地址解析 HTTP、JavaScript 或 meta 跳转，获取本设备当前的认证参数。
5. **门户仍已认证时等待外网恢复。** 记录 `portal_online_internet_unreachable`，不因此反复提交密码。
6. **确实需要认证时自动登录。** 读取实际登录页的加密、公钥和服务选项，向 HTTPS 网关登录；成功后最多复查外网三次，每次复查前等待两秒。
7. **记录状态并等待下一轮。** Windows/直接 CLI 默认等待 60 秒；Linux setup 默认随机等待 60～180 秒。连续失败采用指数退避，Linux 的随机增量最多为当前退避 25%，总等待默认最多 900 秒。门户心跳或本地链路变化可提前唤醒；恢复后回到正常间隔。

| 检测点 | 预期结果 |
| --- | --- |
| `http://www.msftconnecttest.com/connecttest.txt` | HTTP 200，内容为 `Microsoft Connect Test` |
| `http://connect.rom.miui.com/generate_204` | HTTP 204，内容为空 |

检测结果代表这些检测点在脚本所用网络路径上可达，不保证所有网站都可用。“60 秒”是默认轮间等待；网络请求、认证和复查还会耗时，门户心跳也可能缩短等待，因此不是严格的恢复时限。

### 1.2 登录与心跳的处理

- 登录接口是配置网关下的 `InterFace.do?method=login`，通过 HTTPS 提交凭据，保留证书和域名验证。
- 根据 JNU 实际页面规则，登录表单字段进行两次 URL 编码。页面启用 RSA 时，使用页面公钥处理绑定当前 MAC 的密码。
- 每次需要认证都获取本设备当前的 `queryString`，不复用另一台设备的 IP、MAC 或浏览器成功页参数。
- `userIndex` 是临时会话标识，默认仅保存在进程内存中，不需要手工配置，也不能当作长期密码。
- `keepaliveInterval` 的单位是**分钟**。取得有效正数时按其间隔发送门户心跳；为 0 或没有有效正值时，不定时发送该类心跳，仍持续检测网络并在掉线后重连。
- 遇到验证码、无法识别的加密字段、缺失公钥或不明确的服务选择时，本轮报错并退避，不猜测认证参数。

认证尝试至少相隔 300 秒，滚动一小时最多 3 次，请求前记录到配置目录的 `auth_guard.json`，所有账号共用该额度。旧版单账号配置遇到网关明确拒绝后仍立即暂停，使用 `reset-auth` 解除；显式启用账号列表后，明确拒绝也按限频继续重试，实际登录失败达到阈值才切换，详见[账号列表与顺序备用](#44-账号列表与顺序备用)。重启不会清空限频。这些本地限制无法保证学校不会封号，也不覆盖其他设备或其他工具的尝试。

因此，本项目的“保活”包含周期检测产生的网络请求、网关要求的心跳，以及掉线后的自动认证；不代表能够绕过学校停网、账号限制或网卡断开。

### 1.3 Windows：计划任务负责常驻

Windows 安装脚本创建名为 **`JNU Network Keeper`** 的计划任务，用 `pythonw.exe` 隐藏运行：

```text
pythonw.exe -m jnu_network_keeper run --config <配置绝对路径> --interval 60
```

| 模式 | 启动条件 | 是否需要先登录桌面 | 安装所需 Windows 凭据 |
| --- | --- | --- | --- |
| `Boot`，默认 | 系统开机后延迟 30 秒 | 不需要 | 当前 Windows 账户密码 |
| `Logon` | 当前用户登录 Windows 后 | 需要 | 不需要额外输入 Windows 账户密码 |

Boot 使用 `AtStartup` 触发器和 Password 登录类型，以当前用户普通权限运行。Logon 使用 `AtLogOn` 和 Interactive 登录类型。两种模式运行相同的保活程序。

任务允许电池供电时运行，不限制执行时长，并忽略重复启动。进程异常退出时，每分钟尝试重启，最多十次；普通网络故障由 Python 自己退避处理，不需要退出进程。

校园网密码使用当前用户的 DPAPI 保护。Boot 模式的 Windows 账户密码在安装时交给任务计划程序管理，不写入项目配置或日志；它不是校园网密码，也不是 Windows Hello PIN。登录类型区别见[微软说明](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_logon_type)。

### 1.4 Linux：systemd 负责常驻

Linux 安装脚本以指定服务用户配置凭据，生成以下单元：

```text
/etc/systemd/system/jnu-network-keeper.service
```

单元固定项目工作目录、Python 和配置绝对路径，执行：

```text
python3 -m jnu_network_keeper run --config <配置绝对路径> --interval 60 --jitter 120
```

安装时执行 `systemctl enable` 和 `systemctl restart`，通过 `WantedBy=multi-user.target` 启用开机启动。服务不依赖 SSH，断开终端后仍运行。

服务在 `network.target` 后启动，由 Python 检测网络并负责认证，无需桌面登录。意外退出时 systemd 等待 30 秒重启；主循环卡住超过 600 秒时由看门狗终止重启，正常退避等待持续报告存活。单元设置 `NoNewPrivileges`、独立临时目录、禁用核心转储和 `UMask=0077`。主动 `systemctl stop` 仍会正常停止。

可选的 NetworkManager 无线守护能将指定 Wi-Fi 配置为登录前自动连接，并恢复被关闭的软件无线开关。安装参数和完整说明见 [Linux 登录前联网与恢复](LINUX-RECOVERY.md)。

默认服务用户是当前普通用户或调用 sudo 的用户。直接以 root 登录运行且未指定 `--user` 时，默认服务用户就是 root。

## 2. Windows 部署

### 2.1 部署前准备

1. 安装 Python 3.10+，应包含同目录的 `pythonw.exe`。
2. 解压完整项目到长期保留的位置，例如 `D:\swap\jnu_network_keeper`。
3. 确保系统能自动连接校园网网卡/Wi-Fi；脚本不负责配置无线连接、启用网卡或获取 DHCP 地址。
4. 退出独立前台运行的 keeper，避免与后台任务争用实例锁。

脚本依次寻找系统 Python、`py -3` 和 Codex 桌面随附的 Python。长期部署可以用 `-Python` 指定稳定的解释器，避免临时运行环境被清理。Windows Store 的 Python 占位命令不算已安装解释器。

### 2.2 配置并安装开机启动

用**同一个 Windows 账号**打开“管理员 PowerShell”，执行：

```powershell
cd D:\swap\jnu_network_keeper
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1
```

先提示输入校园网学号和密码，再提示输入当前 Windows 账户密码。已有校园网配置时，直接回车可保留原值。安装完成后立即启动任务，后续开机自动运行。

指定解释器和检测间隔：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1 `
  -Python 'C:\Path\To\Python\python.exe' -Interval 60
```

已经保存校园网凭据时，跳过校园网输入；Boot 任务注册仍需 Windows 账户密码：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1 -ReuseConfig
```

只需要登录桌面后启动时：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1 -StartupMode Logon
```

Logon 不满足未登录桌面也运行的场景。不要用另一个管理员账号安装原用户的 DPAPI 配置。若组织策略禁止批处理登录或密码任务，需要由电脑管理员处理。

### 2.3 安装后检查

```powershell
Get-ScheduledTask -TaskName 'JNU Network Keeper' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'JNU Network Keeper'

# Boot 应显示 MSFT_TaskBootTrigger
(Get-ScheduledTask -TaskName 'JNU Network Keeper').Triggers | Format-List *

# 检查实际网络和日志
.\start-windows.cmd -Action Status
.\start-windows.cmd -Action Logs
```

任务显示 `Running` 只说明进程在运行，还应查看网络状态。常驻任务尚在执行时，任务计划程序显示的执行结果不应直接当作校园网认证失败。

前台排障时先停止后台任务：

```powershell
.\start-windows.cmd -Action Stop
.\start-windows.cmd                  # Ctrl+C 结束前台运行
.\start-windows.cmd -Action Start
```

## 3. Linux 部署

本节命令都在**完整项目目录**执行，推荐用以后运行服务的普通 Linux 用户操作；需要系统权限时安装器会调用 sudo。`YOUR_SERVICE_USER`、`YOUR_WIFI_INTERFACE`、`YOUR_WIFI_PROFILE_UUID`、`/absolute/path/...` 是占位值，要替换成此服务器的实际信息。

| 使用场景 | 建议阅读顺序 |
| --- | --- |
| 有线网络、一个校园账号 | 3.1～3.3，再做 3.7 检测 |
| 三组账密、指定一个主用 | 3.1、3.2、3.4，再做 3.7 检测 |
| Wi-Fi 服务器、要求登录桌面前联网 | 在单/多账号部署基础上完成 3.5 |
| 已安装，要增加备用或修改密码 | 3.4 的已有安装流程，先停止 keeper |
| 只检查当前部署 | 3.7 一键检测，再按 3.8 定位原因 |

### 3.1 环境要求与服务用户

| 项目 | 要求 |
| --- | --- |
| 系统 | Linux，systemd 正作为系统服务管理器运行，存在 `/run/systemd/system` |
| Python | 3.10+，只依赖标准库，无需 pip 包或虚拟环境 |
| 系统命令 | Bash、`systemctl`、`systemd-analyze`、`getent`、`realpath`、`runuser`、`install`；普通用户需可用 sudo |
| 可选 Wi-Fi 恢复 | NetworkManager 管理目标网卡，有 `nmcli`；特权 helper 固定使用的 `/usr/bin/python3` 也需为 3.10+ |
| 基础接入 | 有线链路或已保存的校园 Wi-Fi 连接；网页门户认证不能代替网卡接入和获取 IP |
| 文件位置 | 源码和解释器放在持久目录，服务用户能读取源码并写入自己的配置目录 |

先检查，缺少的组件通过所用发行版的软件包管理器安装后再继续：

```bash
python3 --version
ps -p 1 -o comm=
command -v bash systemctl systemd-analyze getent realpath runuser install

# 只有 Wi-Fi 恢复需要
nmcli --version
/usr/bin/python3 --version
```

PID 1 通常应为 `systemd`；只有 Python、没有运行中的 systemd 的容器/精简环境不适用这个安装器。长期运行不要使用临时目录或可能被清理的 Python，也不要把普通用户的源码放在其无法访问的 `/root` 下。

配置属于**服务用户**，例如该用户的 `~/.config/jnu_network_keeper/config.json`。直接以 root 登录执行 setup 而不指定用户，会安装为 root；希望原普通用户运行时应明确指定：

```bash
sudo bash setup-linux.sh --user YOUR_SERVICE_USER
```

后续 Python 配置命令也由同一用户执行。root 维护其他用户时，可在可读的项目目录中用 `sudo -u YOUR_SERVICE_USER -H /usr/bin/python3 -m jnu_network_keeper ... --config /absolute/path/config.json`，不要误查或重建 root 的默认配置。

### 3.2 获取完整项目并验证入口

可访问 GitHub 时，选择长期位置克隆：

```bash
git clone https://github.com/faguomarry-ux/jnu_network_keeper.git "$HOME/jnu_network_keeper"
cd "$HOME/jnu_network_keeper"
python3 -m jnu_network_keeper --help
bash setup-linux.sh --help
```

如果认证前不能访问 GitHub，可在其他电脑下载完整项目/干净发布 ZIP，再通过已有管理通道或移动存储复制到服务器解压。已有完整项目时直接进入其目录，不向同一目录重复 clone。只复制 SH 或 README 无法运行。

服务会继续从这个源码目录启动，不能随意移动/删除。通过 `bash 文件名.sh` 执行无需先 chmod；不要用 `curl | bash` 运行交互安装器，终端标准输入需要留给账密提示。

### 3.3 单账号首次安装

以实际服务用户在项目目录运行：

```bash
bash setup-linux.sh
```

安装依次进行：

1. 验证 systemd、Python、服务用户、间隔参数及 sudo 权限。
2. 提示校园账号和隐藏密码；首次不能为空，已有值时回车保留。
3. 保存该用户的私有配置，Linux 密码为明文、文件权限 0600。
4. 安装 `jnu-network-keeper.service`，启用开机启动，立即启动/重启。

完成后退出 SSH 不影响常驻服务；正常开机不要求先登录桌面。Wi-Fi 登录前能否建立链路还需完成 3.5。Windows 的 DPAPI 配置不能直接复制到 Linux，应在服务器本机输入凭据。安装完成后继续检测，不要只凭安装命令成功就认定已经联网。

### 3.4 添加三组账密并指定主用账号

**首次直接使用三账号列表：**

```bash
bash setup-linux.sh --accounts --failover-threshold 100000
```

这一次交互创建列表并安装服务，不需要分别运行三次单账号安装。下面是提示结构示意，密码在本机隐藏输入，可选服务通常回车留空：

```text
Number of saved accounts (1..3) [3]: 3
Primary account slot [1]: 1
Account 1 ID (blank keeps saved): 主用账号
Account 1 campus password (blank keeps saved): 隐藏输入
Account 1 optional portal service (...): 回车或实际服务值
Account 2 ID (blank keeps saved): 备用账号一
Account 2 campus password (blank keeps saved): 隐藏输入
Account 2 optional portal service (...): 回车或实际服务值
Account 3 ID (blank keeps saved): 备用账号二
Account 3 campus password (blank keeps saved): 隐藏输入
Account 3 optional portal service (...): 回车或实际服务值
```

主用槽位为 1 时顺序 **1 → 2 → 3**，主用为 2 时顺序 **2 → 1 → 3**。账号必须互不相同；每项可配置自己的门户服务值，留空则使用公共配置/门户页面选择，多个服务不明确时不会猜测。

**已有单账号，要增加备用账号或修改已保存列表：**

```bash
sudo systemctl stop jnu-network-keeper
unset JNU_NET_USERNAME JNU_NET_PASSWORD

python3 -m jnu_network_keeper init-accounts \
  --primary-account 1 --failover-threshold 100000

sudo systemctl start jnu-network-keeper
python3 -m jnu_network_keeper accounts
```

数量提示输入 3；已有账号的账号/密码提示回车可保留，新槽位输入真实值。此方法只改配置，不重写原 systemd 间隔和无线参数。自定义位置时，所有 Python 命令加同一个 `--config /absolute/path/config.json`。

先停止后台和独立前台 keeper，否则实例锁会拒绝修改，不要删锁绕过保护。`init-accounts` 是重新配置，会重置本轮账号计数并回到主用，但**保留全局认证限频**；查看用 `accounts`，它不改计数、不显示真实账密。

阈值 100000 指**每个账号实际登录请求失败次数**，不是普通检测轮数。明确拒绝也按限频累计，达到阈值才换下一账号，全部达到阈值会记录 ERROR 并暂停。一次实际登录成功清零，下一次认证重新优先主用；不会主动注销在线的备用会话。三个账号合计至少相隔 5 分钟、每小时最多 3 次，因此 100000 至少约需 **3.8 年/账号**，不是快速切换时限。计数跨重启保存。

完整计数口径、请求中断处理及两个重置命令的区别见 [4.4](#44-账号列表与顺序备用)。`--accounts` 不与 `--reuse-config` 混用；setup 中 `--failover-threshold` 需配合 `--accounts`，修改阈值请重新配置列表。

### 3.5 Wi-Fi 登录前联网、软件开关恢复与 DHCP 跟随

此步骤仅适用于 **NetworkManager 管理的 Wi-Fi**，有线部署不用无线参数。本工具不代替企业 Wi-Fi/802.1X 客户端。

先查此服务器的网卡和已保存连接：

```bash
nmcli -f DEVICE,TYPE,STATE device status
nmcli -f NAME,UUID,TYPE connection show
```

`YOUR_WIFI_INTERFACE` 是设备名，例如 `wlp2s0`；`YOUR_WIFI_PROFILE_UUID` 是已保存连接的 UUID，不是 SSID、校园账号或其他电脑的标识。

有 WPA 密码时，先通过 `sudo nmtui` 或发行版连接编辑器将密码保存为系统连接凭据，启用所有用户可用和自动连接，不能只保存在桌面钥匙环。无线密码与网页校园网密码不同。无无线密码的校园 Wi-Fi 不需添加 WPA 密码；安装器会检查保存状态，企业 Wi-Fi/802.1X/WEP/热点模式会拒绝自动配置。

**已有校园网凭据，只增加无线保护：**

```bash
bash setup-linux.sh --reuse-config \
  --wifi-interface YOUR_WIFI_INTERFACE \
  --wifi-connection YOUR_WIFI_PROFILE_UUID \
  --follow-dhcp --interval 60 --jitter 120
```

**首次“三账号 + 无线恢复”一次配置：**

```bash
bash setup-linux.sh --accounts --failover-threshold 100000 \
  --wifi-interface YOUR_WIFI_INTERFACE \
  --wifi-connection YOUR_WIFI_PROFILE_UUID \
  --follow-dhcp --interval 60 --jitter 120
```

已有服务且要重新配置列表时先停 keeper；首次可直接执行。原先自定义的用户/配置/解释器仍需沿用。`--follow-dhcp` 需要已配置本工具无线守护，或本次同时指定两项无线参数。

| 部分 | 职责 |
| --- | --- |
| `jnu-network-keeper.service` | 服务用户运行：外网探测、门户心跳、受限认证、账号计数和状态 |
| `jnu-network-guard.timer` + `.service` | 独立 root helper：恢复指定无线软件开关/连接，不依赖桌面登录 |

无线检查开机基础等待 15 秒、每次结束基础等待 60 秒，附加 0～30 秒随机延迟。额外恢复动作至少间隔 120 秒、15 分钟最多 3 次。已连接或激活中不强制重连，不抢换其他活动连接；硬件关闭、全局 networking 禁用或设备不受管理时交给管理员。

`--follow-dhcp` 仅让请求源地址跟随指定连接当前 IPv4，改运行时内存，不改配置、路由或 DNS，也不自动更换 `gateway_ip`。本地链路/地址变化可提前唤醒退避，门户失败不会触发整机或 NetworkManager 全局重启。

向日葵自身仍需系统级开机服务和登录界面支持，本项目只提供联网前提，不修改其配置，也不解锁全盘加密启动阶段。完整恢复/回退规则见 [LINUX-RECOVERY.md](LINUX-RECOVERY.md)。

### 3.6 安装参数、随机间隔与复用配置

| 参数 | 默认或含义 | 注意事项 |
| --- | --- | --- |
| `--user USER` | 当前普通用户/sudo 调用用户 | 直接 root 终端建议指定原服务用户，该用户需已存在 |
| `--python PATH` | PATH 中的 python3 | 使用持久解释器；无线 root helper 仍固定系统 Python |
| `--config PATH` | 服务用户默认配置 | 修改、检测、清理都用同一绝对路径 |
| `--interval N` | 60 秒；setup 允许 10～900 | 正常等待基础秒数，服务命令优先于 JSON |
| `--jitter N` | 120 秒；setup 允许 0～900 | 随机附加 0～N 秒，0 关闭随机 |
| `--accounts` | 创建/修改 1～3 组账密 | 先停已有 keeper，不与复用模式混用 |
| `--failover-threshold N` | 默认 100000 | 配合列表配置，按实际请求失败计数 |
| `--reuse-config` | 不提示账密，复用保存值 | 可改间隔/无线；不可同时配置列表或改变网关/源 IP/服务 |
| 两项 `--wifi-*` | 指定网卡和连接 UUID | 必须成对，普通有线部署省略 |
| `--follow-dhcp` | 可选动态源 IPv4 | 需要所选无线守护，不自动选择有线网卡 |
| `--gateway-ip`、`--source-address` | 默认留空 | 仅核实 TUN/DNS/路由问题后指定此设备实际值 |
| `--service NAME` | 公共门户服务值 | 实际选项值，可由账号单独配置覆盖 |

```bash
# 正常等待随机 1～3 分钟
bash setup-linux.sh --reuse-config --interval 60 --jitter 120
# 随机 2～5 分钟
bash setup-linux.sh --reuse-config --interval 120 --jitter 180
# 固定等待 60 秒
bash setup-linux.sh --reuse-config --interval 60 --jitter 0
```

随机的是额外秒数，失败时指数退避并最多增加当前退避 25% 的抖动，默认总退避最多 900 秒。门户心跳截止时间或本地链路变化可提前检查，因此不是固定整分钟定时器。缩短探测间隔不会放开密码提交限频。

重新执行 setup 会重启服务：`--reuse-config` 保留列表及计数；普通 setup/`init` 在列表模式只修改主用并保留备用，主用凭据实际改变才重置本轮计数；`--accounts` 是显式重新配置并重置本轮计数。更新时继续传入原来的用户、配置、解释器和间隔参数。

### 3.7 一键检测与报告判读

安装后在项目目录运行：

```bash
bash test-linux.sh
```

默认先跑离线测试，再检查真实配置/服务/无线/限频，最后执行最多 45 秒的外网/门户 **GET** 检查。可以在 keeper 运行时执行，不另启保活实例，不登录/注销、不发送心跳 POST、不改认证次数、不启停服务或 Wi-Fi。它会创建私有报告；离线用例使用虚构凭据和本地回环 HTTP/socket，不用真实校园密码测试登录。

#### 3.7.1 选择检测模式

| 目的 | 命令 | 范围 |
| --- | --- | --- |
| 安装后完整检查 | `bash test-linux.sh` | 离线测试 + 本机部署 + GET 联网检查 |
| 不请求外网和门户 | `bash test-linux.sh --offline` | 保留本地部署检查；自动测试仍可能用回环网络 |
| 只验证代码/模拟恢复 | `bash test-linux.sh --unit-only` | 不读真实部署/账号配置，不验证实际联网；默认当前用户 |
| 日常快速检查 | `bash test-linux.sh --skip-unit-tests` | 跳过测试套件，检查部署和 GET 联网 |
| 指定配置 | `bash test-linux.sh --config /absolute/path/config.json` | 避免查错配置目录 |
| 指定解释器 | `bash test-linux.sh --python /absolute/path/python3` | 默认 PATH 中的 python3；与服务不一致会提醒 |
| 管理员检查原用户 | `sudo bash test-linux.sh --user YOUR_SERVICE_USER --config /absolute/path/config.json` | 切换为实际服务用户执行 |
| 报告另存 | `bash test-linux.sh --report-dir "$HOME/jnu-test-reports"` | 在可写父目录中创建私有子目录 |

脚本不自动请求 sudo，会尝试识别已安装服务的用户、标准单元配置及间隔。普通用户与服务用户不同会提示；root 选择普通服务用户时会降到该用户执行。自定义单元/drop-in 无法可靠解析会警告，建议显式指定配置并核对有效参数。

单独查看测试失败栈可在本机执行 `python3 -m unittest discover -s tests -v`；数量以当前版本输出为准。默认报告只保留摘要，不直接附带原始异常栈或日志。

#### 3.7.2 每项检测的意义

| 报告项目 | 检查什么 | 仍需另外确认 |
| --- | --- | --- |
| `python`、`offline_tests`、`bash_syntax_*` | 版本、自动测试、Bash 语法 | 不证明密码正确或真实重启恢复成功 |
| `config`、`credentials` | 格式、可读性、0600、非空保存凭据 | 不提交密码；当前终端账号覆盖不会掩盖空配置 |
| `schedule`、`effective_options` | 文件配置及可识别的服务间隔 | 定制单元可能有其他覆盖 |
| `service_running`、`service_boot` | active、持久开机启用 | active 不等于互联网可达 |
| `process_recovery`、`unit_verify` | notify/退出重启/watchdog 配置、单元校验 | 不实际杀进程或模拟卡死 |
| `wifi_boot_profile`、`wifi_secret_flags` | 所有用户/自动连接/系统保存标志 | 不读取无线 PSK，实际登录前可用性需验收 |
| `wifi_switch`、`wifi_link`、`wifi_timer` | 开关、指定连接及 IPv4、timer | 不会替你开无线；oneshot 等待时 inactive 正常 |
| `wifi_helper_permissions`、`wifi_helper_version` | root helper 权限及源码版本 | 不代表硬件、驱动和全部网络路径无故障 |
| `auth_limits` | 暂停、冷却、时钟及记录 | 不消耗/重置额度，不保证学校不会限制账号 |
| `account_pool` | 列表/阈值/耗尽/进行中/配置一致性 | 不逐个尝试真实密码 |
| `runtime_state` | 最近后台快照及更新是否过旧 | 与本次即时检测是不同时间的观测 |
| `internet`、`portal` | 固定结果 GET、当前设备门户会话 | 不保证所有网站/代理路径及以后一直可用 |
| `remote_client` | 向日葵候选系统服务 | 不证明能从另一设备远程访问登录界面 |

#### 3.7.3 报告保存、退出码与分享

末尾会打印实际报告目录。默认属于实际测试用户：

```text
~/.local/state/jnu_network_keeper/diagnostics/test-日期时间-随机后缀/
  report.txt
  report.json
```

每次新建目录；目录权限 0700，文件 0600。文本供阅读，JSON 包含 `format_version`、时间、`checks`、状态数量和 `exit_code`。汇总是**诊断项目数量**，离线用例数量另列在 `offline_tests`。

```bash
bash test-linux.sh
# 紧接着读取刚才的退出码
echo $?
```

| 状态/退出码 | 如何判断 |
| --- | --- |
| PASS | 该项通过，仍按上表理解边界 |
| WARN；退出码 `1` | 有需注意的情况，例如限频/外网不通/未开机启用；不一定是程序错误 |
| FAIL；退出码 `2` | 配置、权限、服务、测试失败或诊断出错，先排查对应项目 |
| SKIP | 未执行或可选功能不适用，有线机器未启用无线可正常跳过 |
| 退出码 `0` | 没有 WARN/FAIL，不等于做过真实密码登录和重启验收 |
| 退出码 `130` | 用户中断，检测未完整完成 |

报告不会自动上传，不含真实账密、会话、IP、MAC、SSID、连接 UUID 和原始命令错误。反馈问题优先分享脱敏报告，不发送 `config.json`、无线配置、完整日志或备份。报告路径本身可能带本机用户名，分享时可以隐藏。

### 3.8 手动检查与分层排障

区分“服务正常”“实际联网”“账号状态”，不反复执行登录来代替检测。以下是只读查询，不主动提交校园网密码：

```bash
sudo systemctl is-enabled jnu-network-keeper
sudo systemctl is-active jnu-network-keeper
sudo systemctl status jnu-network-keeper --no-pager
sudo journalctl -u jnu-network-keeper -b -n 100 --no-pager
sudo journalctl -u jnu-network-keeper -f

# 以实际服务用户，使用同一份配置
python3 -m jnu_network_keeper status --config /absolute/path/config.json
python3 -m jnu_network_keeper accounts --config /absolute/path/config.json
python3 -m jnu_network_keeper paths --config /absolute/path/config.json

# 无线部署另查 timer 和最近结果
sudo systemctl list-timers jnu-network-guard.timer
sudo journalctl -u jnu-network-guard.service -b -n 50 --no-pager
nmcli -f DEVICE,TYPE,STATE device status
```

Ctrl+C 结束 `journalctl -f` 只停止查看，不停 keeper。`status` 做即时检查并写状态日志，不更新常驻 `state.json`；后者由 run 每轮写入。默认位置可查看 `cat "$HOME/.config/jnu_network_keeper/state.json"`，自定义配置则读其旁的同名文件。`observed_at` 是观测时间，`next_check_in/next_check_at` 是计划等待，心跳/链路变化可使下一轮提前。

想核实服务实际用户/配置/间隔，可在本机看 `sudo systemctl cat jnu-network-keeper.service`。标准单元只含配置路径，自定义单元可能含私有环境变量，完整输出不要未经脱敏公开。状态表见 [5.1](#51-日志状态和实例锁)。

| 现象 | 优先处理 |
| --- | --- |
| Python/入口失败 | 检查 3.10+、完整源码目录和读取权限；无线系统 Python 单独核实 |
| `config` / `credentials` 失败 | 确认实际用户、同一配置、属主及 0600，勿用 chmod 777 或公开明文配置排障 |
| 服务未启动或重启频繁 | 查看本次 journal、解释器/源码路径和目录写权限，不删除锁文件绕过前台实例 |
| `wifi_link` / `link_unavailable` | 查网卡、连接 UUID、有效 IPv4、硬件开关、NetworkManager 和 timer；门户认证不能弥补链路断开 |
| `portal_online_internet_unreachable` | 门户仍认证，排查上游/检测点/DNS/TUN/路由，不反复发密码或换账号 |
| `auth_cooldown` | 正常限频，等待到期，不删记录或新建配置目录刷新额度 |
| `auth_blocked` | 旧单账号或迁移遗留暂停，核实根因后停服务执行 reset-auth |
| `account_login_failed` / `account_switched` | 用 accounts 看实际失败数/阈值/下一槽位，不把探测失败当登录失败 |
| `accounts_exhausted` | 全部账号达阈值，处理问题后停服务执行 reset-accounts，不靠重启清零 |
| `account_outcome_unknown` / `account_config_changed` | 请求中断或配置与记录不一致，核实后显式恢复，不猜测结果或直接编辑状态 |
| TLS/DNS/源地址异常 | 核实时间、网关和本机 IP；有依据才覆盖地址，不关闭证书验证，不照抄另一台 IP |
| 状态文件过旧 | 核对进程、配置位置及目录是否移位；仅 active 不足以证明循环正常 |
| 向日葵仍进不去 | internet/portal 通过后，再查客户端自己的系统服务和登录界面支持 |

`reset-auth`、`reset-accounts` 是维护操作，需先停 keeper，不属于只读检测，均不放开全局限频。若明确要执行一轮可能提交密码的认证，可在后台停用后使用 `python3 -m jnu_network_keeper login --config /absolute/path/config.json`；它与检测脚本的 GET 查询不同，仍遵守同一保护。

### 3.9 重启前验收、更新与日常管理

先做一键检测，再在现场或备用通道可用时安排真实重启、关 Wi-Fi、异常退出验收。一键脚本不会主动做这些可能中断管理连接的动作：

| 实机验收 | 观察点 |
| --- | --- |
| 重启后不登录桌面 | 等 Wi-Fi/门户恢复，从另一设备检查向日葵，再回看本次 `journalctl -b` |
| 手工关闭 Wi-Fi 软件开关 | 已启用 guard 时观察 timer 恢复；要长期关闭则先停 timer |
| 进程退出/卡住 | 维护窗口核对 systemd 恢复；有意 `systemctl stop` 应维持停止 |
| 多账号故障切换 | 先依靠模拟网关自动测试，不快速向真实网关提交假密码来凑阈值 |

```bash
sudo systemctl restart jnu-network-keeper
sudo systemctl stop jnu-network-keeper
sudo systemctl start jnu-network-keeper

# 要人工保持无线关闭，先停守护
sudo systemctl disable --now jnu-network-guard.timer
sudo systemctl stop jnu-network-guard.service
# 恢复守护
sudo systemctl enable --now jnu-network-guard.timer
```

更新前停服务并另存自己的源码改动/私有备份，在原目录 `git pull --ff-only`，再运行 `bash setup-linux.sh --reuse-config`，继续传原用户、配置、解释器和间隔/抖动参数；已配置的无线 helper 会随 setup 更新。只重启门户进程不会更新 root 复制的无线 helper。重新检测后再断开管理通道。

源码移位需到新位置重装，彻底停用/清理/复查见 [第 7 节](#7-停用和卸载)。验证边界以 [VALIDATION.md](VALIDATION.md) 为准；离线通过不等于真实 Linux 开机或向日葵验收完成。

## 4. 配置和凭据管理

### 4.1 文件位置及安全方式

| 系统 | 默认配置文件 | 校园网密码保存方式 |
| --- | --- | --- |
| Windows | `%USERPROFILE%\.config\jnu_network_keeper\config.json` | 当前 Windows 用户的 DPAPI |
| Linux | 服务用户的 `~/.config/jnu_network_keeper/config.json` | 明文，文件权限限制为 `0600` |

Linux Python CLI 支持 `XDG_CONFIG_HOME`，两端支持 `JNU_NET_CONFIG`。sudo 切换服务用户时，非默认配置位置应显式传入，避免继承其他账号的环境路径。

自定义位置分别使用 `setup-windows.ps1 -Config 'D:\JnuKeeper\config.json'` 和 `setup-linux.sh --config /absolute/path/config.json`。**后续更新和查看状态/日志时继续使用同一个配置文件。**

默认路径查询：

```powershell
.\start-windows.cmd -Action Paths
```

```bash
python3 -m jnu_network_keeper paths  # 以实际服务用户运行
```

例如，使用自定义配置做即时检查时：

```powershell
.\start-windows.cmd -Action Status -Config 'D:\JnuKeeper\config.json'
```

```bash
python3 -m jnu_network_keeper status --config /absolute/path/config.json
```

配置示例见 [config.example.json](config.example.json)，仅含占位值。推荐用 setup/`init` 创建，不手工填写密码。Windows 换设备、换用户或重装系统后，应重新初始化；DPAPI 不应作为跨设备通用凭据。Linux 配置和备份均应保持私有权限，父目录也应限制访问。

程序没有校园网密码的命令行参数。旧版单账号支持 `JNU_NET_USERNAME`、`JNU_NET_PASSWORD`、`JNU_NET_SERVICE` 环境变量覆盖；账号列表模式拒绝混用账号/密码环境覆盖，以免账号身份和计数混淆，公共服务覆盖仍支持。setup 默认读取本地配置，不会将当前终端的所有环境变量写入计划任务或 systemd。

### 4.2 常用字段和生效规则

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `portal_url` | JNU HTTPS ePortal 基地址 | 必须为 `/eportal/` 基地址，不填成功页或会话 URL |
| `service` | 空字符串 | 使用页面已选/唯一服务；不明确时指定实际服务选项值 |
| `accounts` | 空列表（旧版单账号） | 显式启用后保存 1～3 个账号；推荐 `init-accounts` 交互创建 |
| `primary_account` | `1` | 主用账号槽位，从 1 开始 |
| `account_failover_threshold` | `100000` | 当前账号累计连续失败的实际登录请求达到此值后，切换到下一个槽位 |
| `interval` | 60 秒 | 检测轮间等待，setup 接受 10～900 秒 |
| `interval_jitter` | 0 秒；Linux setup 为 120 秒 | 正常等待中附加 0～该值的随机秒数；Linux setup 参数 `--jitter` |
| `follow_dhcp` | `false` | Linux Wi-Fi 可选动态源 IPv4 跟随，参数 `--follow-dhcp` |
| `timeout` | 6 秒 | 每次网络请求的超时设置 |
| `max_backoff` | 900 秒 | 连续失败退避上限，不得小于 `interval` |
| `gateway_ip` | 空字符串 | 可选真实网关 IPv4，用于绕过错误/虚拟 DNS |
| `source_address` | 空字符串 | 可选本机校园网 IPv4，用于指定连接源地址 |
| `password_encryption` | `auto` | 读取实际页面加密要求；另支持指定 `rsa` / `plain` |
| `ca_file` | 空字符串 | 可选受信任 CA 文件，默认校验证书和域名 |

配置在进程启动时读取，**修改后必须重启**。计划任务和 systemd 启动命令显式指定 `--interval`，Linux 服务还指定 `--jitter`，优先于 JSON；通过重新执行 setup 修改间隔和随机范围。Linux 示例：`bash setup-linux.sh --reuse-config --interval 120 --jitter 180`，对应正常在线随机 2～5 分钟。Wi-Fi/DHCP 恢复说明见 [LINUX-RECOVERY.md](LINUX-RECOVERY.md)。

`-ReuseConfig` / `--reuse-config` 跳过校园网配置初始化，不能同时用于修改网关、源 IP 或服务选项。修改这些字段时不要使用复用参数，账号密码提示可直接回车保留旧值。

### 4.3 代理/TUN 和 IP 绑定

程序绕过系统/环境 HTTP 代理，但不能自动避开所有 TUN 路由或虚拟 DNS。2026-11-03 本机核对时，认证域名被代理解析为 `198.18.*` / `2001:2::*` 虚拟地址，通过校园 DNS 查询到的真实网关为 `192.168.11.68`；指定真实网关和本机校园网源 IPv4 后可连接。

只有确认遇到相同问题且 IP 正确时，才使用以下覆盖。源地址占位符必须替换成**正在配置的设备自己的校园网 IPv4**：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1 `
  -GatewayIp '192.168.11.68' -SourceAddress 'YOUR_WINDOWS_CAMPUS_IPV4'
```

```bash
bash setup-linux.sh \
  --gateway-ip 192.168.11.68 --source-address YOUR_LINUX_CAMPUS_IPV4
```

网关地址是本次环境的核对结果，不保证适用于其他校区或未来迁移。Linux 不得照抄 Windows 的源 IP。正常校园 DNS、无 TUN 干扰时通常留空即可。

指定 IP 仍通过原域名完成 HTTPS SNI 和证书验证；脚本不修改系统路由、DNS 或代理软件。DHCP、网卡或校区变化后，需要更新或清除旧绑定，再重启程序。

Linux 已配置本项目 Wi-Fi 守护时，可通过 `--follow-dhcp` 自动跟随指定连接当前的源 IPv4，仅更新运行时绑定。它不自动改变 `gateway_ip`，校区/网关迁移仍需核实。

### 4.4 账号列表与顺序备用

支持保存 **1～3 个账号**并指定主用槽位，默认切换阈值为 **100000 次实际登录请求失败**。旧版顶层 `username/password` 配置继续兼容，只有显式使用列表配置后才启用此行为。

**已安装 Linux 服务时，以实际服务用户在项目目录配置：**

```bash
sudo systemctl stop jnu-network-keeper
# 如果此前导出过单账号覆盖，先在当前终端清除，勿把密码写进命令行
unset JNU_NET_USERNAME JNU_NET_PASSWORD

python3 -m jnu_network_keeper init-accounts --failover-threshold 100000
# 交互选择账号数量（通常 3）、主用槽位，再逐个输入账号/隐藏密码/可选服务

sudo systemctl start jnu-network-keeper
python3 -m jnu_network_keeper accounts
```

自定义配置时，所有 Python 命令继续传入同一个 `--config /absolute/path/config.json`。首次安装也可以直接 `bash setup-linux.sh --accounts --failover-threshold 100000`，配置完成后安装服务；已有服务需先停止，以便安全更新账号列表。原先的 `--user`、网络、解释器和间隔参数继续沿用，`--accounts` 不与 `--reuse-config` 同时使用。

Windows 使用同一套账号列表逻辑，每个密码分别使用当前用户 DPAPI。先停止后台任务，运行 `python.exe -m jnu_network_keeper init-accounts ...`，再启动任务；也可以在 setup 使用 `-Accounts -FailoverThreshold 100000`。必须在实际任务所属的 Windows 用户下操作。

**实际切换规则：**

1. 主用槽位为 1 时，优先顺序是 **1 → 2 → 3**；主用为 2 时是 **2 → 1 → 3**。普通在线状态不提交登录，也不为切回主账号而主动注销当前会话。
2. 只有代码真正进入登录 API 的发送步骤、且没有得到可接受的登录成功结果时，才累计失败：包含网关明确拒绝、验证码拒绝、HTTP 错误、畸形响应及发送阶段网络超时。单纯外网检测失败、Wi-Fi 关闭、门户发现失败、缺公钥/服务不明确导致未提交，均不计入账号失败次数。
3. 当前账号达到阈值后，记录切换状态，**下一次符合限频的认证**使用下一个账号；不会在同一轮连续提交多个账号。账号切换不创建新额度，三个账号合计仍至少相隔 5 分钟、滚动一小时最多尝试 3 次。
4. 一次实际登录 API 成功会清零这一轮所有账号的失败计数，下一次需要认证时重新优先指定主账号；登录成功但外网仍不通属于上游问题，不把它算成账号登录失败。普通探测轮次既不增加也不清零这些计数。
5. 所有账号分别达到阈值后，记录 **ERROR**，状态变为 `accounts_exhausted`，暂停后续自动认证，不循环回第一个账号；仍可探测网络并识别人工恢复的连接。

阈值可以通过 `init-accounts --failover-threshold N` 修改，范围为 1～2147483647，配置时也可交互输入。**100000 不是快速切换阈值**：按每小时最多 3 次计算，一个账号连续失败到此值至少约需 33333 小时（约 3.8 年），三个账号全部耗尽会更久；随机等待、退避或请求耗时只会继续延长。不要把 100000 次理解为快速循环测试。明确拒绝也会自动重试是账号列表模式的行为，不能承诺学校不会限制账号，也不自动解决或绕过验证码。

**保存、查看与恢复：**

- `config.json` 在列表模式只保存一份 `accounts`，不再同时保留旧顶层账号密码；每项有本地不透明标识、账号、受保护的密码及可选服务。Linux 为 0600 明文配置，Windows 密码分别 DPAPI 保护。推荐交互命令生成，不手写/公开包含真实凭据的示例，也不将真实密码放入 SH/PS1。
- `account_state.json` 只保存不透明标识、当前优先序、失败计数、阈值、请求进行中标记及耗尽状态，不保存用户名或密码；日志及 `accounts` 命令只显示账号槽位和计数，不输出账号、密码或这些标识。`state.json` 同步显示下一次认证槽位及各槽位失败数。
- 账号计数跨进程和服务器重启保留。文件损坏、配置标识/阈值不匹配时暂停认证；不通过删除文件强制重试。
- 登录发送前先持久化“进行中”标记。进程若在请求/写盘窗口崩溃，无法确认结果时状态为 `account_outcome_unknown`，不会凭空增加失败数或立即重发，需要核实当前网络/账号后恢复。
- `accounts` 为只读查看；`init-accounts` 是显式重新配置，会重置本轮账号计数。普通 `init` 在列表模式只修改主用凭据，保留备用账号；主用凭据实际改变时也重置本轮计数，直接回车保留凭据不会重置。

```bash
# 查看槽位、当前计数、阈值，不显示账密
python3 -m jnu_network_keeper accounts --config /absolute/path/config.json

# 处理全部账号失败、未确认请求或修改配置之后，显式开始新一轮
sudo systemctl stop jnu-network-keeper
python3 -m jnu_network_keeper reset-accounts --config /absolute/path/config.json
sudo systemctl start jnu-network-keeper
```

`reset-accounts` **不清空全局认证次数/最小间隔，也不解除旧的 `auth_guard.json` 暂停**；若从旧单账号配置迁移时已有 `auth_blocked`，确认原因已处理后另外按 `reset-auth` 说明解除。修改主用槽位/顺序/阈值请重新运行 `init-accounts`，不是修改状态文件。卸载脚本和清理后检查也覆盖 `account_state.json`，自定义共享配置目录同样支持。

## 5. 日常维护

### 5.1 日志、状态和实例锁

以下文件默认位于配置文件旁：

| 文件 | 用途 |
| --- | --- |
| `keeper.log` | 启动、停止、状态变化及错误；每份约 1 MiB，保留三份备份 |
| `state.json` | 常驻 `run` 最近一轮观测结果，每轮更新 |
| `keeper.lock` | 操作系统实例锁；同一配置目录只能有一个 `login` / `run` |
| `auth_guard.json` | 认证次数和拒绝后的暂停记录，重启保留；不应删除来强制重试 |
| `account_state.json` | 账号列表的失败计数、下次槽位、请求进行中标记和耗尽状态，重启保留 |

Linux 日志还会写入 journal，保留策略由系统管理。锁文件存在不代表进程仍在运行，进程结束后锁会释放；不要通过删除锁文件解决实例冲突。

```powershell
# Windows
.\start-windows.cmd -Action Logs
Get-Content "$env:USERPROFILE\.config\jnu_network_keeper\state.json"
```

```bash
# Linux：HOME 必须属于实际服务用户
cat "$HOME/.config/jnu_network_keeper/state.json"
sudo journalctl -u jnu-network-keeper -n 100 --no-pager
```

自定义配置时替换路径。可通过 Windows 任务的 `Actions` 或 `sudo systemctl cat jnu-network-keeper` 查看实际配置参数。

| 状态/字段 | 含义 |
| --- | --- |
| `online` | 本轮外网检测通过 |
| `reconnected` | 本轮重新认证后外网复查通过 |
| `portal_online_internet_unreachable` | 门户已认证，但外网检测不通过，等待恢复 |
| `connection_failed` | 本轮发现、认证或心跳出错，查看相邻日志原因 |
| `link_unavailable` | 启用无线守护后，指定 Wi-Fi 未连接/没有 IPv4，本轮不认证 |
| `auth_cooldown` | 距上次尝试不足 300 秒或滚动一小时已尝试 3 次 |
| `auth_blocked` | 旧版单账号的明确拒绝暂停或迁移时保留的暂停；处理原因后执行 `reset-auth` |
| `auth_clock_error` | 系统时间早于持久记录，需检查时间同步 |
| `account_login_failed` | 本轮实际登录请求失败，尚未达到当前账号阈值 |
| `account_switched` | 已达到阈值，下次允许认证时使用下一槽位 |
| `accounts_exhausted` | 所有账号达到阈值，已记录错误并暂停自动认证 |
| `account_config_changed` / `account_outcome_unknown` | 配置与计数不一致或请求结果未确认；核实后使用 `reset-accounts` |
| `authenticated: false` | 也可能没有取得当前会话或本轮出错，不能独立证明账号已注销 |
| `observed_at` 很旧 | 检查进程是否退出、停止或长时间处理请求 |
| `next_check_in` / `next_check_at` | 本轮计划等待时长/下次检查时间；链路变化或心跳可使实际检查提前 |

时间是检测到状态的时间，不是精确掉线时刻。`Status` 做即时检查，`state.json` 则由常驻 `run` 更新，二者时间可能不同。服务/任务在运行与实际网络可用必须分开检查。

### 5.2 修改账号、密码、服务和 IP

Windows 在原账号的管理员 PowerShell 中重新运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1
```

输入新校园网凭据，空输入保留旧值。已有本项目任务会被更新并重启。原来选择 Logon 时继续加 `-StartupMode Logon`，否则默认注册 Boot。**Windows 账户密码改变后，用 `-ReuseConfig` 重新注册 Boot 任务，更新 Windows 管理的任务凭据。**

Linux 以原服务用户重新运行：

```bash
bash setup-linux.sh
```

保存新凭据后会重启服务。原先指定其他服务用户/配置文件时，继续传入相同的 `--user` / `--config`。

更改服务选项分别使用 Windows 的 `-Service '实际选项值'` 或 Linux 的 `--service '实际选项值'`。更新 IP 可使用第 4.3 节的参数。不再需要绑定时，将私有 JSON 的 `gateway_ip`、`source_address` 改为空字符串后重启，Windows 的 DPAPI 密码字段保持原样。

### 5.3 修改检测间隔和管理进程

```powershell
# Windows：改为 120 秒并更新任务；原 Logon 模式需再加 -StartupMode Logon
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1 -ReuseConfig -Interval 120

.\start-windows.cmd -Action Restart
.\start-windows.cmd -Action Stop
.\start-windows.cmd -Action Start
```

```bash
# Linux：改为 120 秒并更新服务
bash setup-linux.sh --reuse-config --interval 120

sudo systemctl restart jnu-network-keeper
sudo systemctl stop jnu-network-keeper
sudo systemctl start jnu-network-keeper
```

临时 Stop 不取消后续开机自启动。停止程序不会主动注销校园网，只停止检测/重连；要持续禁用启动项，见第 7 节。

后台已暂停时，Windows 可用 `.\start-windows.cmd -Action Login`、Linux 可用 `python3 -m jnu_network_keeper login` 执行一轮检测及必要的登录。使用自定义配置时也要指定其路径；完成后恢复后台任务/服务。

### 5.4 更新源码、备份和迁移路径

1. 停止后台任务/服务，保留旧版本以便回退。
2. 备份配置到受控位置。Linux 备份继续保持 `0600`；Windows DPAPI 配置不能视作跨设备通用备份，换设备应重新初始化。
3. 将新版完整发布包覆盖解压到原项目目录。默认配置在用户目录下，不包含在发布包里。
4. 以原账号重新运行 setup，使用 `-ReuseConfig` / `--reuse-config`，并继续传入原来的启动模式、检测间隔、自定义解释器和配置路径。
5. 检查进程状态、近期日志和实际网络，确认新版运行。

Windows 项目移位时，先在旧目录卸载任务，再到新目录安装；安装器会拒绝直接覆盖其他项目路径/用户的同名任务。Linux 换目录或解释器后，在新目录重新运行 setup 更新服务单元，单纯重启旧服务不会自动找到新路径。

仅改源码且路径不变时，重启进程即可加载代码。手工修改 Linux service 单元后，要先 `sudo systemctl daemon-reload` 再重启；直接用 setup 会自动完成。

### 5.5 重启后验收

- Windows Boot：登录桌面后检查任务触发器、日志中的启动时间和网络状态，确认开机后无需手动运行。Logon 只验证登录后的自动启动。
- Linux：检查 `is-enabled`、`is-active` 和 `journalctl -b`，确认本次开机已启动并能联网。
- 真实掉线恢复应在可中断网络的维护时间测试，观察重试与 `reconnected`，不要只把进程存活作为重连成功证据。

## 6. 常见故障排查

| 现象 | 检查和处理 |
| --- | --- |
| 找不到 Python / Windows Store 提示 | 安装 3.10+，Windows 用 `-Python`、Linux 用 `--python` 指明路径；Windows 还需要 `pythonw.exe` |
| Windows 开机不运行，登录后才有网 | 核对是否安装为 Logon；需要登录前运行则重新注册 Boot，并确认系统网卡能在登录前连接 |
| Windows 任务启动失败、没有程序日志 | 查看 `Get-ScheduledTaskInfo` 和执行路径；前台运行 `Status` 检查配置/DPAPI 错误；Windows 密码变更后重新注册 Boot |
| 提示实例已在运行 | 停止原后台或前台实例；同一配置目录不能运行两份 keeper，不要删除锁文件绕过 |
| `No current-device ePortal redirect found` | 确认在校园网、网关正确，检查 DNS/TUN 和旧源 IP 绑定；不要填成功页代替本设备登录参数 |
| `portal_online_internet_unreachable` | 门户认证仍在，检查外网、检测点、DNS 和路由；程序会等待恢复，不反复提交密码 |
| 账号/密码或策略拒绝 | 用本设备浏览器核对凭据、账号状态和服务，再通过 setup 更新；日志不回显完整服务端响应 |
| 验证码、缺失加密字段/公钥、多服务不明确 | 浏览器核对实际规则并完成必要认证，按实际值配置服务；页面升级后可能需要适配 |
| Linux 配置权限过宽或无权读写 | 核对服务用户和文件归属，将该用户的配置设为 `chmod 600`；父目录应可访问且可写 |
| Linux 不启动或不断重启 | 查看 journal 并修复路径、权限、解释器和配置；新版 systemd 服务每 30 秒恢复退出的进程，认证次数仍保留 |
| 改 JSON 的间隔仍不生效 | 启动命令的 `--interval` 优先，重新用 setup 传入新间隔 |
| 换网卡/DHCP 后连不上 | 更新或清除 `source_address`，核对 `gateway_ip`，然后重启程序 |
| TLS/证书错误 | 核对时间、域名和真实网关；必要时用 `ca_file` 指定受信任 CA，不关闭证书验证 |

命令退出码：`0` 表示成功/外网检测通过；`1` 表示检查完成但外网仍不可用；`2` 表示配置、网络或认证错误；`130` 表示交互中断。常驻模式内部处理普通网络错误，不会每次掉线都退出进程。

## 7. 停用和卸载

### Windows

删除启动任务并停止后台进程，保留配置和日志：

```powershell
.\start-windows.cmd -Action Uninstall
```

暂时禁用而不删除任务：

```powershell
Stop-ScheduledTask -TaskName 'JNU Network Keeper'
Disable-ScheduledTask -TaskName 'JNU Network Keeper'

# 恢复
Enable-ScheduledTask -TaskName 'JNU Network Keeper'
Start-ScheduledTask -TaskName 'JNU Network Keeper'
```

### Linux

Linux 提供两个独立入口：**`uninstall-linux.sh` 卸载并清理本项目保存的信息，`check-cleanup-linux.sh` 在清理后检查残留**。二者调用标准库实现 `cleanup-linux.py`，无需导入 keeper 包，也无需 pip。请先保留完整项目，完成清理后再选择删除源码；只删除一个 SH 不会卸载后台服务或清除密码。

#### 7.1 只暂时停用

仅停止服务，不删除任何凭据或日志：

```bash
sudo systemctl disable --now jnu-network-keeper
sudo systemctl disable --now jnu-network-guard.timer
sudo systemctl stop jnu-network-guard.service
```

未启用无线守护时后两条可能显示单元不存在。临时停用与下面的卸载清理是不同操作。

#### 7.2 推荐的清理和复查顺序

在当前完整项目目录执行，需 Linux、systemd、Python 3.10+ 和 sudo/root 权限：

```bash
# 第一步：只预览清理范围，不停止服务、不删除文件
sudo bash uninstall-linux.sh

# 第二步：按预览范围实际执行；会删除保存的校园网密码
sudo bash uninstall-linux.sh --apply

# 第三步：独立复查，不登录校园网、不重启服务
sudo bash check-cleanup-linux.sh
```

**没有 `--apply` 时只是预览。** 预览会显示具体目标路径；真正执行时按相同规则重新核对。执行后会自动检查一次，再次运行检查脚本可独立复查。清理中断或失败时不报告成功，可以按输出的清理凭据重试；已删除的数据不能回退。

默认从已安装标准服务识别实际用户和配置路径；额外检查该用户默认配置目录及本项目默认测试报告目录。`sudo` 不意味着清理的是 root 配置。若使用自定义单元/drop-in，无法可靠推断路径时会要求提供 `--config`；同名服务无法确认为本项目时拒绝删除。

#### 7.3 默认清理哪些内容

| 内容 | 默认处理 |
| --- | --- |
| `jnu-network-keeper.service`、`jnu-network-guard.service`、`jnu-network-guard.timer` | 停止、禁用，删除标准单元、同名单元的专用 drop-in 和启动链接，重新加载 systemd |
| 实际用户的 `~/.config/jnu_network_keeper/` | 删除整个专用目录，包括校园网账号/密码、日志及轮转备份、状态、限频记录、锁和目录内自行存放的备份 |
| 已安装服务或 `--config` 指定的自定义配置 | 在共享父目录中只删除该配置及本工具固定运行文件，不递归删除整个共享目录 |
| `/etc/jnu-network-guard.json`、`.backup.json`、root 无线 helper、`/var/lib/jnu-network-guard/` | 默认先恢复备份中的四个 NetworkManager 字段并读回复核，再删除本项目安装的文件/状态 |
| 实际用户的 `~/.local/state/jnu_network_keeper/` | 删除默认的一键测试报告及专用状态目录 |
| 已核实源码目录内的 `.env`、`config.local.json`、`config.json`、`data/`、`logs/` | 删除这些本地私有文件/目录；不要在这些位置放需要保留的其他资料 |
| 源码、SH/Python 脚本、发布 ZIP、参考目录 | 默认保留；加 `--remove-project` 才删除整个已核实的源码目录 |
| 原有 NetworkManager Wi-Fi 连接及其无线密码 | 默认保留连接；只恢复本项目改过的字段，下面说明可选的删除方式 |
| 清理范围凭据和独立检查器 | 暂留在源码目录之外，便于删掉源码后复查；最后可单独删除 |

固定运行文件包括 `keeper.log` 及 `.1/.2/.3`、`state.json`、`auth_guard.json`、`account_state.json`、`keeper.lock`。自定义配置位于共享目录时，该目录中的其他文档、任意命名备份及不明临时文件不会被猜测删除，应通过下面的附加参数明确列出。不要让多个项目把同名 keeper 运行文件放在同一个共享父目录。

删除前会确认服务已停止，并检查前台 keeper/诊断/无线 helper 进程及实例锁；发现进程仍在运行或无法确认时，保留数据并退出。不会使用 `killall python`，也不会通过删锁文件绕过运行中的进程。拒绝删除系统根目录、用户主目录、其他凭据目录、指向外部位置的符号链接或跨挂载点的目录树。

#### 7.4 自定义配置、测试报告和备份

以下参数都应在预览和实际执行时保持一致；路径必须是绝对路径：

```bash
# 自定义服务用户、配置、另存的报告、手工密码备份或自定义日志
sudo bash uninstall-linux.sh \
  --user YOUR_SERVICE_USER \
  --config /absolute/path/config.json \
  --report-dir /absolute/path/custom-reports \
  --extra-file /absolute/path/config-backup.json

# 核对预览后，同一命令加 --apply
sudo bash uninstall-linux.sh --apply \
  --user YOUR_SERVICE_USER \
  --config /absolute/path/config.json \
  --report-dir /absolute/path/custom-reports \
  --extra-file /absolute/path/config-backup.json
```

`--config`、`--report-dir`、`--extra-file` 可重复。`--report-dir` 只清理其中格式匹配、仅含 `report.txt/report.json` 的 `test-*` 报告子目录，保留父目录及其他资料；格式不明或混有其他文档时拒绝递归删除。任意命名的备份、自定义 `--log-file`、旧配置迁移副本等应逐个使用 `--extra-file` 列出。未列出的任意位置不会做全盘搜删。

配置/源码路径有符号链接或挂载点时脚本会停止；需核实真实位置后分别处理，不能用删除整个上级目录代替。默认 Python 为 `/usr/bin/python3`；可用 `--python /absolute/path/python3` 指定目录外的稳定解释器，不能用即将删除的项目虚拟环境解释器完成源码删除。

#### 7.5 Wi-Fi 设置和远程连接

默认恢复首次备份中的 `connection.permissions`、`connection.autoconnect`、`connection.autoconnect-retries`、`connection.auth-retries`，不主动执行断开或重启网络。恢复后会读回核对；备份损坏、NetworkManager 不可用或恢复失败时保留备份和凭据，先停止进一步删除，修复后重试。

远程清理时若要保留当前 NetworkManager 设置，明确使用：

```bash
sudo bash uninstall-linux.sh --apply --keep-wifi-settings
```

这仍删除门户凭据、服务及守护文件，只跳过四个连接字段的回退。它不表示 NetworkManager 的原有无线密码也被清除。

若确实还要删除守护所指定的 Wi-Fi 连接（包括 NetworkManager 在该连接中保存的无线密码），先安排本机/备用管理通道，再使用：

```bash
sudo bash uninstall-linux.sh --apply --forget-wifi
```

**`--forget-wifi` 可能立即断开 SSH/向日葵。** 它只删除 guard 配置/备份中同一个 UUID 对应的连接，并复查不存在，不删除其他网络。`--forget-wifi` 与 `--keep-wifi-settings` 互斥。无论哪种模式，都不发送校园网注销请求，不取消学校账号，不清除校园网网关或向日葵服务器上的记录。

#### 7.6 删除源码后仍能复查

如果连项目目录中的脚本、源码和发布包也不要了：

```bash
# --project-dir 只能是包含项目标记文件的实际源码目录
sudo bash uninstall-linux.sh --project-dir /absolute/path/jnu_network_keeper --remove-project
sudo bash uninstall-linux.sh --apply --project-dir /absolute/path/jnu_network_keeper --remove-project

# 源码目录已删除后，使用留在目录外的独立 Python 检查器
sudo /usr/bin/python3 -I /var/lib/jnu-network-keeper-cleanup/check-cleanup.py check
```

默认项目目录是卸载脚本所在的目录，不猜测并删除磁盘上其他副本。`--remove-project` 会递归删除所选完整目录，其中的 Git 历史、参考项目、ZIP 和其他自行放入的文件也会一起删除；预览时必须核实该目录专用于本项目。

默认清理范围凭据为 `/var/lib/jnu-network-keeper-cleanup/receipt.json`，父目录 0700、凭据 0600，独立检查器为同目录 `check-cleanup.py`。凭据只记录清理范围、阶段和 Wi-Fi 处理结果，不复制账号密码、IP、MAC、SSID、会话或连接 UUID；**路径本身可能包含本机用户名、自定义文件名等元数据**。若用 `--receipt /private/path/receipt.json` 改位置，每次卸载/复查都应带相同 `--receipt`，且位置不能在待删目录内。原先的凭据可用于重复执行，保留之前声明过的自定义清理范围。

确定不再需要按原范围复查时，最后执行一次“检查通过后删除清理凭据”：

```bash
# 源码仍保留时
sudo bash check-cleanup-linux.sh --forget-receipt

# 源码已删除时
sudo /usr/bin/python3 -I /var/lib/jnu-network-keeper-cleanup/check-cleanup.py check --forget-receipt
```

只有该次检查通过才删除凭据和保留的独立检查器；若仍有残留或状态未知则保留它们。删除凭据后，检查器无法再知道你此前列出的自定义路径，应自行保留必要的无凭据范围说明。

#### 7.7 如何理解“检查干净”

检查脚本会重新核对声明文件/目录、systemd 单元及启动引用、相关进程和自定义报告目录，不只相信卸载命令的退出码。输出：

| 输出/退出码 | 含义 |
| --- | --- |
| `CLEAN`；退出码 `0` | 本次声明范围内未发现应删除的内容 |
| `RESIDUE`；退出码 `1` | 有文件、目录、启动引用或进程残留，处理后重查 |
| `UNKNOWN`；退出码 `2` | 权限、systemd、进程检查或凭据异常，无法确认，不能当作已清理 |
| `RETAINED` / `LIMIT` | 明确保留或不覆盖的范围，例如源码、系统共享日志、外部备份；不等于这部分已清除 |
| 退出码 `130` | 用户中断，已做的删除不能恢复，需要重新检查 |

以下内容需要另外核实：

- **系统共享 journal。** 脚本只查询是否还存在相关记录，不全局清空 journal；`journalctl` 的 vacuum 操作针对归档 journal 文件，不能把本项目过滤条件当成只删除本项目日志的保证。不能为清理本工具而删掉服务器其他服务的日志。[journalctl 官方说明](https://www.freedesktop.org/software/systemd/man/latest/journalctl)
- 浏览器保存的校园网密码、Cookie、桌面钥匙环、其他 NetworkManager 连接、向日葵账号/日志，以及本项目之外的 shell 历史、导出环境变量、截图、手工备份、同步盘、快照、硬链接副本、其他 Linux 用户的配置。只清理了一个服务用户不代表清理了所有账号。
- 若曾自行导出密码环境变量，在相应终端执行 `unset JNU_NET_USERNAME JNU_NET_PASSWORD JNU_NET_SERVICE JNU_NET_CONFIG`，并检查自己修改过的 shell 启动文件；子进程不能修改父终端环境。不要把带密码的历史记录贴到公开报告中。
- 这是文件系统层面的删除，不保证 SSD、写时复制文件系统、快照或备份中的内容无法取证恢复。账号已经泄露时，还需要通过学校渠道修改密码。

完整项目之外的副本应在核实后另外删除。脚本不会自动删除 Python、NetworkManager、systemd、向日葵、其他项目或整台机器的日志。

```bash
sudo bash uninstall-linux.sh --help
sudo bash check-cleanup-linux.sh --help
```


## 8. 代码结构与验证边界

| 路径 | 用途 |
| --- | --- |
| `setup-windows.ps1` / `setup-linux.sh` | 交互配置和自启动安装入口 |
| `test-linux.sh` / `jnu_network_keeper/diagnostics.py` | Linux 一键只读诊断、离线测试和私有脱敏报告 |
| `uninstall-linux.sh` / `check-cleanup-linux.sh` / `cleanup-linux.py` | Linux 卸载与隐私清理、源码删除后仍可独立复查 |
| `windows.ps1` / `start-windows.cmd` | Windows 前台运行、状态和任务管理 |
| `deploy/windows/` / `deploy/systemd/` | 计划任务/systemd 的底层安装实现 |
| `jnu_network_keeper/keeper.py` | 常驻循环、退避、状态记录及实例锁 |
| `jnu_network_keeper/portal.py` | 联网检查、当前设备参数发现、认证和心跳 |
| `jnu_network_keeper/transport.py` | HTTP(S)、TLS、代理绕过、IP 连接配置 |
| `jnu_network_keeper/config.py` | 私有配置、DPAPI 和文件权限 |
| `jnu_network_keeper/auth_guard.py` | 持久化认证限频和拒绝后暂停 |
| `jnu_network_keeper/accounts.py` | 账号列表、实际登录失败持久计数及顺序切换/耗尽暂停 |
| `jnu_network_keeper/linux_network.py` | NetworkManager 登录前 Wi-Fi 配置及限频恢复 |
| `jnu_network_keeper/supervisor.py` | systemd 主循环通知和看门狗等待 |
| [LINUX-RECOVERY.md](LINUX-RECOVERY.md) | 无线恢复、进程保护、认证暂停及回退 |
| [STARTUP.md](STARTUP.md) | 自启动脚本配置示例 |
| [VALIDATION.md](VALIDATION.md) | 实测记录、自动检查及未验证边界 |
| `scripts/build_release.py` | 构建干净离线发布包 |

本项目参考开发机 `ecnu_network_keeper/` 的使用与部署思路，独立实现 JNU ePortal 协议。该参考目录不纳入仓库和发布包，也不需要部署；其 SRun 接口、账号后缀和旧 Docker 镜像不适用于本程序。

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build_release.py
```

Windows 任务定义的隔离测试为 `tests/check_windows_setup.ps1`。发布包为 `dist/jnu-network-keeper.zip`，只包含允许的源码、脚本及文档，排除真实凭据、配置、运行日志和参考目录。对外分享时不要额外附带用户配置目录。

Python 自动测试、Windows PowerShell 5.1/7 任务定义隔离测试、Bash 语法/参数检查及历史 Windows 无破坏性在线检查见 [VALIDATION.md](VALIDATION.md)。**真实掉线后重登、真实 Windows 账户密码注册后重启验收、Linux 实机 systemd/Wi-Fi/向日葵登录前验收尚未完成**。

适用范围为本类 ePortal 网页认证，不包含 802.1X 客户端协议、其他校区不同网关、额外运营商密码或自动识别验证码。停电、关机、睡眠、网卡断开和学校停网时，需要等待设备及网络条件恢复。
