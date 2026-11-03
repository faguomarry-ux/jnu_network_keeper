# Linux 登录前联网与恢复

适用于 **systemd + NetworkManager + Python 3.10+**。无线守护可选，必须指定现有校园 Wi-Fi 连接和网卡；有线网络沿用普通 setup。

## 安装

先查看此服务器的网卡名和已保存连接的 UUID：

```bash
nmcli -f DEVICE,TYPE,STATE device status
nmcli -f NAME,UUID,TYPE connection show
```

登录后才能连接 Wi-Fi，常见原因是连接只允许桌面用户使用，或无线密码在个人钥匙环中。若 Wi-Fi 本身需要 WPA 密码，先通过 `sudo nmtui` 或发行版网络连接编辑器，保存系统连接的密码、启用自动连接及所有用户可用。不要只把密码存在桌面钥匙环，不要将密码写进 shell 命令。Wi-Fi 密码与网页门户的校园账号密码不同。无密码校园 Wi-Fi 无需设置无线密码。

```bash
cd /path/to/jnu_network_keeper
# 首次安装，交互输入校园网学号密码
bash setup-linux.sh \
  --wifi-interface YOUR_WIFI_INTERFACE \
  --wifi-connection YOUR_SAVED_WIFI_PROFILE_UUID

# 已保存校园网凭据，增加无线恢复功能
bash setup-linux.sh --reuse-config \
  --wifi-interface YOUR_WIFI_INTERFACE \
  --wifi-connection YOUR_SAVED_WIFI_PROFILE_UUID
```

把占位值换为实际值，例如网卡名 `wlp2s0`。连接参数必须是 UUID，不是 SSID。原先使用 `--user`、`--config` 时继续沿用。无线 helper 使用系统 `/usr/bin/python3`，需为 3.10+；门户进程仍可使用 `--python`。

安装器将所选连接设为所有用户可用、自动连接，将 NetworkManager 自动激活重试设为 2、底层认证重试设为 1，不主动断开现有连接，不重启 NetworkManager。四个字段的首次原值备份到 `/etc/jnu-network-guard.backup.json`，选择保存到 `/etc/jnu-network-guard.json`，不复制无线密码。WPA-PSK/SAE 密码未保存到系统连接时会拒绝安装；暂不自动处理企业 Wi-Fi、802.1X、WEP、热点/AP。

依据：[NetworkManager 连接属性](https://networkmanager.dev/docs/api/latest/settings-connection.html)、[无线安全属性](https://networkmanager.dev/docs/api/latest/settings-802-11-wireless-security.html)、[nmcli](https://networkmanager.dev/docs/api/latest/nmcli.html)。permissions 为空允许所有用户使用；自动连接不会替换正在使用的连接。

## 随机分钟检测与 DHCP 恢复

Linux setup 默认 `--interval 60 --jitter 120`：正常在线时，每轮完成后随机等待 **60～180 秒（1～3 分钟）** 再检测外网。随机的是额外等待秒数，不是固定整分钟。`--interval 120 --jitter 180` 对应 **2～5 分钟**；`--jitter 0` 关闭随机等待。Windows 和直接 Python CLI 默认仍为零抖动；Linux setup 会在服务启动命令中显式设置参数，复用配置也能修改检测节奏。

```bash
# 已配置无线守护：默认随机 1～3 分钟，并跟随本机无线 IPv4
bash setup-linux.sh --reuse-config --follow-dhcp

# 已配置无线守护：随机 2～5 分钟
bash setup-linux.sh --reuse-config --interval 120 --jitter 180 --follow-dhcp

# 首次启用无线守护时可以合并参数
bash setup-linux.sh --reuse-config --follow-dhcp \
  --wifi-interface YOUR_WIFI_INTERFACE \
  --wifi-connection YOUR_SAVED_WIFI_PROFILE_UUID
```

失败时保留指数退避，仅增加不超过当前退避 25% 的随机等待，不超出 `max_backoff`；不会缩短重试间隔。门户要求更早心跳时仍按其截止时间提前执行，链路变化也会提前唤醒，因此不是每次都等完整随机时长。随机等待用于分散周期请求，不用于规避学校策略，认证仍受 5 分钟/小时 3 次限制。

启用 Wi-Fi 守护后，等待期间每至多 30 秒读取一次本地 NetworkManager 状态（另加本地命令耗时）。不发送额外外网请求，不提交密码，不变更网卡。连接状态、活动连接 UUID 或 IPv4 改变时，提前结束本轮等待，清除缓存会话并重新核实；无需一律等完 15 分钟退避。后台状态读取使用更短的 2 秒 nmcli 等待/3 秒进程超时，避免拖住主循环。

`--follow-dhcp` 是可选功能：只跟随指定网卡上**指定连接 UUID** 的可用 IPv4。旧 `source_address` 不再属于当前地址时，在内存中替换为当前地址；仍有效则保留。不会把账号配置写回磁盘，也不修改 DHCP、路由或 DNS。此功能依赖已配置的 Wi-Fi 守护；所选连接未激活、无有效 IPv4 或配置缺失时暂缓认证，避免把请求绑定到错误网络。要关闭已保存的跟随设置，将私有配置 `follow_dhcp` 改为 `false`，随后不带该选项重装/重启服务。

`state.json` 新增 `next_check_in` 和 `next_check_at`，表示本轮计划等待秒数和下次计划检查时间；链路变化、停止信号或心跳需要可能使实际执行提前。日志会记录启动时的间隔/抖动设置及动态源地址更新，不输出实际账号、密码或会话。

## 无线恢复规则

`jnu-network-guard.timer` 开机基础等待 15 秒、每次结束基础等待 60 秒，每次附加 0～30 秒随机延迟，另有调度和命令耗时。使用 systemd 的 [RandomizedDelaySec](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)。

| 情况 | 行为 |
| --- | --- |
| 已连接、正在激活或获取地址 | 等待，不强制重连 |
| Wi-Fi 软件开关关闭 | 打开软件开关，先让 NetworkManager 自动连接 |
| 开关已开、网卡断开/失败/不可用 | 限频允许时激活指定连接和网卡 |
| 网卡被其他连接占用 | 保留当前连接，不切换 |
| 已连接但没有有效 IPv4 | 记录 `wifi_connected_without_ipv4`；门户暂停认证，不强制断开仍可能可用的 IPv6 连接 |
| 硬件无线开关禁用 | 记录 `wifi_hardware_blocked`，需要现场恢复 |
| 全局 NetworkManager networking 被关闭 | 记录 `networking_disabled_requires_operator`，需管理员处理 |
| 网卡未受管理、设备缺失或命令出错 | 等待/报错，不抢管网卡，不重启网络服务 |

守护的额外无线恢复动作至少相隔 **120 秒**，滚动 **15 分钟最多 3 次**，打开开关也计数。动作前原子写入并同步记录，重启/超时不会清空额度；NetworkManager 自身仍按连接配置自动尝试。单条 nmcli 等待 20 秒，外层超时 25 秒，整个 oneshot 服务超时 90 秒。服务与手工 helper 共用实例锁。

门户每轮先检查指定 Wi-Fi 连接 UUID 和可用 IPv4；未就绪记录 `link_unavailable`，跳过心跳和认证，无线守护继续独立工作。等待中观察到链路恢复会提前复查；仅外网故障而本地链路未变化时仍按退避处理。心跳失败后会在同一轮继续检测外网、重新发现当前会话，在线不重复登录；确需认证时仍先检查持久限频记录。

确实需要保持无线关闭时，先执行 `sudo systemctl disable --now jnu-network-guard.timer` 和 `sudo systemctl stop jnu-network-guard.service`；恢复守护使用 `sudo systemctl enable --now jnu-network-guard.timer`。

## 防重复认证

以下规则同时适用于 Windows/Linux 的 `login` 和 `run`：

- 外网在线时不登录，门户已认证但外网不可达时不重复提交密码；不通过注销或重启网卡刷新。
- 每个配置目录至少 **300 秒**才允许再次尝试，滚动 **1 小时最多 3 次**，包括成功、拒绝、超时。请求前同步写入 `auth_guard.json`，重启后保留。
- 旧版单账号配置遇到明确拒绝后仍暂停自动登录。显式使用账号列表时，按配置阈值累计实际登录请求失败，明确拒绝也限频重试，达到阈值才切换；所有账号达到阈值后记录错误并暂停。完整配置与区别见 [README 账号列表](README.md#44-账号列表与顺序备用)。网络探测继续，人工登录成功仍可被检测到。
- HTTP 错误、非 JSON、网络超时等不明确结果由限频和退避控制。
- 记录损坏、无法读写或系统时间回拨时拒绝提交密码。先排查磁盘、权限和时间同步，不删除记录强制重试。
- 同一配置目录的 `login` / `run` 使用操作系统实例锁。不要为同一个账号启动多份不同目录的配置。

状态：`auth_cooldown` 为限频，`auth_blocked` 为拒绝后暂停，`auth_clock_error` 为时间回拨。`retry_after` 是本轮观测时距允许再次认证的秒数，不是联网恢复承诺。

修正密码、验证码或账号问题后，以**实际服务用户**和原配置路径操作：

```bash
sudo systemctl stop jnu-network-keeper
python3 -m jnu_network_keeper reset-auth --config /absolute/path/config.json
sudo systemctl start jnu-network-keeper
```

reset-auth 只解除暂停，保留次数和最小间隔，不提交登录请求。修改密码可重新执行 setup；原暂停仍需解除。上述上限不是学校公布的封禁阈值，其他设备/浏览器也可能使用相同账号，**无法承诺不会封号**。限频会延长自动恢复时间。

账号列表的阈值默认 100000，只统计已进入登录 API 发送步骤后的失败，不统计断网检测轮次。计数保存在 `account_state.json`，支持三账号及指定主用槽位，所有账号共用原认证限频；用 `init-accounts` 配置、`accounts` 查看、`reset-accounts` 显式重置，均以实际服务用户和同一个配置路径运行。列表计数/耗尽与旧 `reset-auth` 暂停是两套状态，重置列表不会清空认证额度。

## 后台保护与向日葵

门户系统服务开机运行，无需桌面/SSH，按 `network.target` 排序，不等待校园网认证完成。`Restart=always` 在意外退出或被 kill 后等待 30 秒重启，没有短期启动次数限制；主循环超过 600 秒无进展由看门狗终止并重启。正常长退避仍报告存活，主循环卡住不报告，使用 systemd [通知协议](https://www.freedesktop.org/software/systemd/man/latest/sd_notify.html)。自定义很长的请求超时时需评估 600 秒阈值。

`systemctl stop/disable` 属于有意管理操作，始终生效。此功能不隐藏进程、不设置全局 OOM 豁免、不保护服务器上其他任意进程。项目文件、解释器或凭据被删除后无法凭空恢复，需存放在持久目录。特权无线操作由独立 root 脚本完成，门户进程使用指定服务用户。

本项目解决向日葵的联网前提；向日葵自身必须有支持登录界面访问的系统启动服务，本项目不会修改其配置。全盘加密要求人工解锁的启动阶段也需要另行处理。

## 一键测试与脱敏报告

在完整项目目录下运行，无需安装 pip 依赖：

```bash
bash test-linux.sh
```

默认先运行离线 Python 测试，再检查此 Linux 的部署和网络。可以在保活服务运行中执行，不与它争抢登录锁或修改认证次数。检测项包括：

- Python、三个 Bash 脚本语法，以及随机间隔/断网恢复/凭据保护等自动测试。
- 配置可读性、0600 权限和非空保存凭据；只检查保存内容，不让当前终端的账号密码环境变量掩盖缺失配置，不输出实际内容，也不证明密码正确。
- 门户服务是否运行、是否持久开机启用、notify/退出重启/看门狗是否配置，后台状态是否过旧；通过 [systemctl show](https://freedesktop.org/software/systemd/man/latest/systemctl.html) 读取状态，使用 [systemd-analyze verify](https://freedesktop.org/software/systemd/man/latest/systemd-analyze.html) 校验单元，不启动或重启服务。
- 指定 Wi-Fi 的所有用户/自动连接设置、无线开关、连接及有效 IPv4、timer、特权 helper 属主/权限/源码版本；只读查询，不读取 Wi-Fi PSK。
- 认证暂停/限频/时间回拨；使用只读检查，不 reserve 登录次数、不 reset-auth、不写认证记录。
- 最多 45 秒的外网固定结果 GET 探测和门户页面 GET 检查。**不会提交登录、注销、心跳 POST，不会切换 Wi-Fi、重启网络或杀进程。**
- 已识别的向日葵系统服务是否运行/启用；名称识别和服务状态不能证明登录界面远程访问成功。

输出状态为 PASS（通过）、WARN（需注意）、FAIL（失败）、SKIP（未检查/可选功能不适用）。汇总计数是诊断项目数，离线测试的实际数量另列在 offline_tests 中。退出码：`0` 无警告/失败，`1` 有警告且无失败，`2` 有失败/运行错误，`130` 用户中断。单个命令有超时，离线测试上限 180 秒；整个流程耗时取决于机器和网络。

```bash
# 不请求外网和门户；离线测试仍会使用模拟数据和本地回环 HTTP/socket
bash test-linux.sh --offline

# 仅运行离线自动测试，不读取真实部署/账号配置；默认用当前用户
bash test-linux.sh --unit-only

# 日常快速检查部署，跳过自动测试
bash test-linux.sh --skip-unit-tests

# 定制路径/解释器
bash test-linux.sh --python /usr/bin/python3 --config /absolute/path/config.json

# 当前登录用户与服务用户不同，需要由管理员以实际用户执行
sudo bash test-linux.sh --user YOUR_SERVICE_USER --config /absolute/path/config.json

# 把每次报告的私有子目录放入自己可写的位置
bash test-linux.sh --report-dir "$HOME/jnu-test-reports"
bash test-linux.sh --help
```

默认从已安装服务识别用户和本项目标准单元的配置/间隔参数；配置有 drop-in 或不认识的自定义启动命令时，会警告并回退当前测试用户的默认配置，建议显式 `--config`。root 选择其他服务用户时会切换为该用户执行，普通用户不会自动请求 sudo。`--python` 默认是 PATH 中的 python3，若与服务解释器不同会提醒。仅离线测试模式不自动切换到已安装服务的用户。

报告默认位于实际测试用户的 `~/.local/state/jnu_network_keeper/diagnostics/test-时间-随机后缀/`，包含 `report.txt`、`report.json`。目录为 0700，文件为 0600。报告只包含固定状态与允许的摘要，不含账号、密码、会话、IP、MAC、SSID、连接 UUID 或原始命令错误/日志；异常原文也不打包。控制台最后一行显示本机报告路径。报告不自动上传或发送。

自动检查只能核对当前快照和模拟恢复流程，不能证明真实重启、关无线、杀进程或向日葵登录前访问可恢复。需要在现场或备用通道可用的维护窗口另做实机验收。

## 验收及回退

彻底卸载及清除校园网凭据/日志时，使用新增的 `uninstall-linux.sh`（默认预览，`--apply` 执行），再用 `check-cleanup-linux.sh` 复查。删除源码、保留目录外检查器、删除清理范围凭据以及自定义路径清理详见 [README 第 7 节](README.md#7-停用和卸载)。以下手工回退步骤只恢复无线配置，不等于清除全部信息。

```bash
sudo systemctl is-enabled jnu-network-keeper
sudo systemctl is-active jnu-network-keeper
sudo systemctl list-timers jnu-network-guard.timer
sudo journalctl -u jnu-network-keeper -b --no-pager
sudo journalctl -u jnu-network-guard.service -b --no-pager
nmcli -f DEVICE,TYPE,STATE device status
```

现场或备用管理通道可用时，安排真实重启、手动关 Wi-Fi 和杀进程验收。重启后不登录桌面，确认 Wi-Fi、门户恢复后再检查向日葵能否访问登录界面，并查看有无密集认证。oneshot 无线服务在定时等待时显示 inactive 正常，应检查 timer 和最近执行结果。

回退无线配置：

```bash
sudo systemctl disable --now jnu-network-guard.timer
sudo systemctl stop jnu-network-guard.service
sudo /usr/bin/python3 -I /usr/local/libexec/jnu-network-guard.py restore
```

restore 恢复首次备份的四个字段，移除无线守护配置及备份，不断开当前连接，不删除原 Wi-Fi 连接。启用了 DHCP 跟随时，还需将私有配置 `follow_dhcp` 改为 `false`，不带 `--follow-dhcp` 重新运行 setup；否则门户会因缺少指定连接配置而暂缓认证。关闭跟随后门户不再限定 Wi-Fi；无线单元文件保留，timer 已禁用。停用整个工具还需 `sudo systemctl disable --now jnu-network-keeper`。
