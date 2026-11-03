"""Read-only Linux deployment diagnostics with allowlisted, private reports."""
from __future__ import annotations

import argparse
import configparser
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

from .auth_guard import AuthGuard
from .accounts import AccountPool
from .config import default_config_path, load_settings
from .linux_network import CONFIG as WIFI_CONFIG, GuardError, LinkMonitor, NetworkManager, read_config, usable_ipv4
from .portal import PortalClient, PortalError, is_online
from .transport import NetworkError, Transport


ROOT = Path(__file__).resolve().parents[1]
UNIT = "jnu-network-keeper.service"
PROPERTIES = ("LoadState", "ActiveState", "UnitFileState", "User", "Type", "Restart", "RestartUSec",
              "WatchdogUSec", "NRestarts", "FragmentPath", "DropInPaths", "WorkingDirectory", "Result")
STATES = {"online", "reconnected", "internet_online", "offline", "connection_failed", "link_unavailable",
          "portal_online_internet_unreachable", "auth_cooldown", "auth_blocked", "auth_clock_error"}
STATES.update({"account_login_failed", "account_switched", "accounts_exhausted", "account_config_changed", "account_outcome_unknown"})


def command(args, *, timeout=10, cwd=ROOT):
    """Capture all command output; never forward raw local metadata/errors."""
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd,
                              env={**os.environ, "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"})
    except (OSError, subprocess.TimeoutExpired):
        return None


def unit_properties(name=UNIT):
    result = command(["systemctl", "show", "--no-pager", name, *[f"--property={key}" for key in PROPERTIES]])
    if result is None or result.returncode:
        return {}
    return {key: value for line in result.stdout.splitlines() if "=" in line
            for key, value in [line.split("=", 1)] if key in PROPERTIES}


def installed_options(properties):
    """Read this installer's simple unit format; unknown/custom units need --config."""
    if properties.get("DropInPaths") or not properties.get("FragmentPath"):
        return None
    try:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string(Path(properties["FragmentPath"]).read_text(encoding="utf-8"))
        args = shlex.split(parser["Service"]["ExecStart"])
        if args[1:5] != ["-m", "jnu_network_keeper", "run", "--config"]:
            return None
        options = {"python": args[0].replace("%%", "%")}
        index = 4
        while index < len(args):
            if args[index] == "--follow-dhcp":
                options["follow_dhcp"] = True
                index += 1
            elif args[index] in ("--config", "--interval", "--jitter"):
                name = {"--config": "config", "--interval": "interval", "--jitter": "interval_jitter"}[args[index]]
                value = args[index + 1].replace("%%", "%")
                options[name] = value if name == "config" else float(value)
                index += 2
            else:
                return None
        if not Path(options["config"]).is_absolute():
            return None
        return options
    except (OSError, ValueError, KeyError, IndexError, configparser.Error):
        return None


class Report:
    def __init__(self):
        self.checks = []

    def add(self, code, status, detail):
        if status not in ("PASS", "WARN", "FAIL", "SKIP"):
            raise ValueError("Invalid report status")
        self.checks.append({"check": code, "status": status, "detail": detail})

    @property
    def exit_code(self):
        return 2 if any(item["status"] == "FAIL" for item in self.checks) else (1 if any(item["status"] == "WARN" for item in self.checks) else 0)

    def payload(self):
        return {"format_version": 1, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "checks": self.checks, "counts": {status: sum(item["status"] == status for item in self.checks)
                                                   for status in ("PASS", "WARN", "FAIL", "SKIP")},
                "exit_code": self.exit_code}

    def render(self):
        lines = ["JNU Linux 一键测试报告（只读诊断，不执行校园网登录）", ""]
        lines.extend(f'[{item["status"]}] {item["check"]}: {item["detail"]}' for item in self.checks)
        counts = self.payload()["counts"]
        lines.extend(["", " / ".join(f"{key}={value}" for key, value in counts.items()),
                      f"退出码：{self.exit_code}（0=通过，1=有警告，2=有失败）",
                      "实机重启、关闭 Wi-Fi、杀进程和登录前向日葵访问未由此脚本执行。"])
        return "\n".join(lines) + "\n"

    def save(self, parent):
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = Path(tempfile.mkdtemp(prefix="test-" + time.strftime("%Y%m%d-%H%M%S-"), dir=parent))
        for name, data in (("report.txt", self.render()), ("report.json", json.dumps(self.payload(), ensure_ascii=False, indent=2) + "\n")):
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(data)
        return directory


def check_unit_tests(report):
    result = command([sys.executable, "-m", "unittest", "discover", "-s", "tests"], timeout=180)
    if result is None:
        report.add("offline_tests", "FAIL", "自动测试无法启动或超过 180 秒；请检查解释器及本地测试环境。")
        return
    output = result.stdout + result.stderr
    count = re.search(r"Ran (\d+) tests?", output)
    skipped = re.search(r"skipped=(\d+)", output)
    if result.returncode == 0 and count and int(count[1]) > 0:
        report.add("offline_tests", "PASS", f"自动测试共 {int(count[1])} 项，跳过 {int(skipped[1]) if skipped else 0} 项；仅使用模拟数据/本地回环网络。")
    else:
        report.add("offline_tests", "FAIL", "离线测试失败或未找到测试；可单独运行 python3 -m unittest discover -s tests -v 排查。原始输出未写入脱敏报告。")


def check_service(report, properties):
    if properties.get("LoadState") != "loaded":
        report.add("service", "WARN", "未能读取已安装的门户服务；可能尚未安装，或 systemd/权限不可用。")
        return
    report.add("service_running", "PASS" if properties.get("ActiveState") == "active" else "FAIL",
               "门户服务正在运行。" if properties.get("ActiveState") == "active" else "门户服务未处于 active；请检查 systemctl status/journalctl，诊断不会自行启动它。")
    enabled = properties.get("UnitFileState") == "enabled"
    report.add("service_boot", "PASS" if enabled else "WARN", "门户服务已启用启动项。" if enabled else "门户服务未持久启用开机启动；重新运行 setup 检查。")
    protected = properties.get("Type") == "notify" and properties.get("Restart") == "always" and properties.get("WatchdogUSec") not in (None, "", "0", "infinity")
    report.add("process_recovery", "PASS" if protected else "WARN", "已配置 notify、退出重启和看门狗；这里只检查配置，不杀进程验证。" if protected else "服务的 notify/always/watchdog 配置不完整，可能需要更新安装。")
    if properties.get("NRestarts", "").isdigit() and int(properties["NRestarts"]) >= 5:
        report.add("service_restarts", "WARN", "systemd 记录的重启次数不少于 5；不等于当前故障，请查看近期 journal。")
    if properties.get("WorkingDirectory") and Path(properties["WorkingDirectory"]).resolve() != ROOT:
        report.add("service_checkout", "WARN", "已安装服务使用另一个项目目录；本次离线测试不能证明部署中的源码已更新。")
    if properties.get("FragmentPath"):
        result = command(["systemd-analyze", "verify", "--man=no", properties["FragmentPath"]], timeout=20)
        report.add("unit_verify", "SKIP" if result is None else ("PASS" if result.returncode == 0 else "WARN"),
                   "已检查服务单元依赖/语法。" if result and result.returncode == 0 else "单元校验未通过或命令不可用；可在本机运行 systemd-analyze verify 排查，原始输出未打包。")


def check_config(report, config, properties, options):
    if not config.exists():
        report.add("config", "FAIL", "未找到配置；首次请运行 setup，已有配置可通过 --config 指定。")
        return None
    try:
        settings = load_settings(config, use_environment=False)
        same = options and Path(options["config"]).expanduser().resolve() == config
        if same:
            for name in ("interval", "interval_jitter", "follow_dhcp"):
                if name in options:
                    setattr(settings, name, options[name])
        settings.validate()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        report.add("config", "FAIL", "配置不可读、权限过宽或字段无效；请使用实际服务用户检查 0600 权限和 JSON，未输出原文。")
        return None
    report.add("config", "PASS", "配置可读取且格式/权限检查通过；账号和密码内容未输出。")
    present = bool(settings.username and settings.password)
    report.add("credentials", "PASS" if present else "FAIL", "已保存非空校园网凭据；未提交认证，不能证明密码正确。" if present else "缺少校园网账号或密码，请运行 setup 配置。")
    if settings.accounts:
        summary = AccountPool(settings, config.parent / "account_state.json").summary()
        status = summary["account_status"]
        level = "PASS" if status == "ready" else ("WARN" if status == "accounts_exhausted" else "FAIL")
        details = {"ready": f"已配置 {len(settings.accounts)} 个账号，主用槽位 {settings.primary_account}；按实际登录请求失败计数，阈值 {settings.account_failover_threshold}，共用认证限频。",
                   "accounts_exhausted": "所有账号都已达到失败阈值，自动认证暂停；核实原因后使用 reset-accounts。",
                   "account_config_changed": "账号配置与持久计数不一致；核实配置后使用 reset-accounts。",
                   "account_outcome_unknown": "存在未确认完成的登录请求；暂停认证以避免猜测计数，核实后使用 reset-accounts。",
                   "account_state_error": "账号计数文件不可读或损坏，已拒绝自动认证；请检查文件/磁盘。"}
        report.add("account_pool", level, details[status])
    report.add("schedule", "PASS", f"配置的正常轮间等待 {settings.interval:g}～{settings.interval + settings.interval_jitter:g} 秒；最大失败退避 {settings.max_backoff:g} 秒。")
    if not same:
        report.add("effective_options", "WARN" if properties.get("LoadState") == "loaded" else "SKIP", "未确认此配置对应服务的有效启动参数；本次网络检查按文件配置执行，定制单元/drop-in 需自行核对。")
    if options and Path(options["python"]).resolve() != Path(sys.executable).resolve():
        report.add("service_python", "WARN", "测试解释器与服务指定解释器不同；可用 --python 指定服务解释器重新检查。")
    try:
        state, retry = AuthGuard(config.parent / "auth_guard.json").inspect()
        if state == "auth_blocked":
            report.add("auth_limits", "WARN", "自动认证已暂停；先处理账号/验证码/策略问题，再按说明执行 reset-auth。测试未解除暂停。")
        elif state == "auth_clock_error":
            report.add("auth_limits", "FAIL", "系统时间早于认证记录；先检查时间同步，测试未清空记录。")
        elif state:
            report.add("auth_limits", "WARN", f"认证处于限频等待，约 {math.ceil(retry)} 秒后才可能再次尝试；测试未消耗额度。")
        else:
            report.add("auth_limits", "PASS", "认证记录有效且当前未被暂停/限频；测试没有保留或使用登录次数。")
    except PortalError:
        report.add("auth_limits", "FAIL", "认证限频记录不可读或损坏；请排查文件/磁盘，不删除记录强制重试。")
    return settings


def check_recent_state(report, config, settings, active):
    try:
        path = config.parent / "state.json"
        if not path.exists():
            report.add("runtime_state", "WARN", "尚无后台状态记录；安装服务后检查是否已完成首轮。")
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        state = data["state"]
        if state not in STATES:
            raise ValueError
        stamp = datetime.fromisoformat(data["observed_at"])
        if stamp.tzinfo is None:
            raise ValueError
        age = time.time() - stamp.timestamp()
        limit = max(settings.max_backoff, settings.interval + settings.interval_jitter) + 180
        if age < -60:
            report.add("runtime_state", "WARN", "状态时间在未来，需检查系统时间同步。")
        elif age > limit:
            report.add("runtime_state", "FAIL" if active else "WARN", "后台状态超过预期更新窗口；可能进程卡住、退出或配置目录不匹配。")
        else:
            report.add("runtime_state", "PASS" if state in ("online", "reconnected", "internet_online") else "WARN", f"最近后台状态为 {state}，约 {max(0, int(age))} 秒前更新。")
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        report.add("runtime_state", "WARN", "后台状态不可读或格式无效；未输出文件原文。")


def check_wifi(report):
    if not WIFI_CONFIG.exists():
        report.add("wifi_guard", "SKIP", "未启用指定 Wi-Fi 守护；有线部署可忽略，无线登录前恢复需带两项 Wi-Fi 参数安装。")
        return
    try:
        data = read_config(WIFI_CONFIG)
        nm = NetworkManager(wait=2, timeout=3)
        target = ("connection", "show", "uuid", data["uuid"])
        system = not nm.field("connection.permissions", *target) and nm.field("connection.autoconnect", *target) == "yes"
        report.add("wifi_boot_profile", "PASS" if system else "WARN", "指定连接允许所有用户且已设自动连接。" if system else "连接权限或自动连接配置不满足登录前联网条件。")
        key = nm.field("802-11-wireless-security.key-mgmt", *target)
        if key in ("wpa-psk", "sae"):
            flags = nm.field("802-11-wireless-security.psk-flags", *target).split(" ", 1)[0]
            report.add("wifi_secret_flags", "PASS" if flags == "0" else "WARN", "无线凭据标志为系统保存；未读取 PSK，实际登录前可用性仍需验收。" if flags == "0" else "无线凭据可能依赖桌面钥匙环；请保存为系统连接凭据。")
        elif key not in ("", "owe"):
            report.add("wifi_secret_flags", "WARN", "当前无线认证类型不属于自动恢复已支持范围。")
        hardware = nm.field("WIFI-HW", "general", "status") == "enabled"
        software = nm.run("radio", "wifi") == "enabled"
        report.add("wifi_switch", "PASS" if hardware and software else "WARN", "无线硬件和软件开关已开启。" if hardware and software else "无线开关未开启；测试不会打开开关，硬件禁用需要现场处理。")
        status = nm.field("GENERAL.STATE", "device", "show", data["interface"]).split(" ", 1)[0]
        active = nm.field("GENERAL.CON-UUID", "device", "show", data["interface"]) if status == "100" else ""
        ipv4 = usable_ipv4(nm.field("IP4.ADDRESS", "device", "show", data["interface"])) if status == "100" else ()
        ready = status == "100" and active == data["uuid"] and bool(ipv4)
        report.add("wifi_link", "PASS" if ready else "WARN", "指定连接已激活并拥有可用 IPv4；未输出地址、SSID 或 UUID。" if ready else "指定连接未就绪或缺少有效 IPv4；测试不强制重连。")
    except GuardError:
        report.add("wifi_profile", "FAIL", "无线配置无效、NetworkManager 不可用或查询超时；请查看无线守护 journal。")
    timer = unit_properties("jnu-network-guard.timer")
    running = timer.get("ActiveState") == "active" and timer.get("UnitFileState") == "enabled"
    report.add("wifi_timer", "PASS" if running else "WARN", "无线定时器 active 且已持久启用。" if running else "无线定时器未正常启用/运行；oneshot 服务等待时 inactive 本身不代表故障。")
    helper = Path("/usr/local/libexec/jnu-network-guard.py")
    try:
        metadata = helper.stat()
        protected = metadata.st_uid == 0 and not metadata.st_mode & 0o022
        report.add("wifi_helper_permissions", "PASS" if protected else "FAIL", "特权 helper 归 root 且不可被组/其他用户写入。" if protected else "特权 helper 属主或权限不符合要求，请重新安装。")
        current = helper.read_bytes() == (ROOT / "jnu_network_keeper/linux_network.py").read_bytes()
        report.add("wifi_helper_version", "PASS" if current else "WARN", "已安装无线 helper 与本项目源码一致。" if current else "无线 helper 尚未更新或来自其他版本，请重新运行 setup。")
    except OSError:
        report.add("wifi_helper", "FAIL", "特权无线 helper 缺失或不可读取。")


def check_remote_client(report):
    result = command(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"])
    if result is None or result.returncode:
        report.add("remote_client", "SKIP", "未能枚举系统服务，未验证向日葵。")
        return
    names = [line.split()[0] for line in result.stdout.splitlines() if line.split()
             and re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", line.split()[0])
             and re.search(r"sunlogin|sunflower", line.split()[0], re.I)]
    if not names:
        report.add("remote_client", "SKIP", "未识别到向日葵系统服务；不同安装方式名称可能不同，仍需单独验收登录界面访问。")
        return
    healthy = any((p := unit_properties(name)).get("ActiveState") == "active" and p.get("UnitFileState") == "enabled" for name in names[:5])
    report.add("remote_client", "PASS" if healthy else "WARN", "识别到运行且已持久启用的向日葵候选服务；不能据此证明远程解锁成功。" if healthy else "向日葵候选服务未同时满足运行和开机启用，需检查客户端自身配置。")


def network_probe(config, options):
    """GET-only child process, bounded by the parent. Never creates a Keeper."""
    settings = load_settings(config, use_environment=False)
    if options.get("follow_dhcp"):
        settings.follow_dhcp = True
    if not LinkMonitor(settings)():
        return {"internet": False, "portal": "not_checked", "link": False}
    transport = Transport(settings)
    internet = is_online(transport)
    try:
        session = PortalClient(settings, transport).discover().session
        portal = "authenticated" if session else "unauthenticated"
    except (PortalError, NetworkError):
        portal = "unavailable"
    return {"internet": internet, "portal": portal, "link": True}


def check_network(report, config, options):
    args = [sys.executable, "-m", "jnu_network_keeper.diagnostics", "--probe-child", "--config", str(config)]
    if options.get("follow_dhcp"):
        args.append("--follow-dhcp")
    result = command(args, timeout=45)
    if result is None:
        report.add("live_network", "WARN", "只读联网检查超过 45 秒或无法启动；没有登录尝试。可用 --offline 跳过此项。")
        return
    try:
        data = json.loads(result.stdout)
        if (result.returncode or not isinstance(data, dict) or set(data) != {"internet", "portal", "link"}
                or type(data["internet"]) is not bool or type(data["link"]) is not bool
                or data["portal"] not in ("authenticated", "unauthenticated", "unavailable", "not_checked")):
            raise ValueError
    except (ValueError, TypeError):
        report.add("live_network", "WARN", "只读联网检查未返回有效结果；请检查配置、权限、DNS、TLS 和源地址绑定，原始输出未打包。")
        return
    report.add("internet", "PASS" if data["internet"] else "WARN", "固定结果的外网 GET 探测通过。" if data["internet"] else "外网探测未通过或指定 Wi-Fi 链路未就绪；测试未尝试登录。")
    detail = {"authenticated": "GET 页面显示当前设备已有认证会话；未输出会话标识。",
              "unauthenticated": "GET 页面显示需要认证；测试未提交账号密码。",
              "unavailable": "未发现当前设备门户页面；可能存在 DNS/TUN/TLS/网关问题。",
              "not_checked": "指定 Wi-Fi 链路未就绪，已跳过门户请求。"}[data["portal"]]
    report.add("portal", "PASS" if data["portal"] == "authenticated" else "WARN", detail)


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--config", type=Path)
    cli.add_argument("--report-dir", type=Path)
    cli.add_argument("--offline", action="store_true")
    cli.add_argument("--unit-only", action="store_true")
    cli.add_argument("--skip-unit-tests", action="store_true")
    cli.add_argument("--probe-child", action="store_true", help=argparse.SUPPRESS)
    cli.add_argument("--follow-dhcp", action="store_true", help=argparse.SUPPRESS)
    return cli


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.unit_only and args.skip_unit_tests:
        cli.error("--unit-only and --skip-unit-tests cannot be combined")
    if args.probe_child:
        import logging
        logging.disable(logging.CRITICAL)
        try:
            result = network_probe(args.config, {"follow_dhcp": args.follow_dhcp})
            print(json.dumps(result))
            return 0
        except Exception:
            return 2
    report = Report()
    if sys.version_info < (3, 10):
        report.add("python", "FAIL", "需要 Python 3.10+。")
    else:
        report.add("python", "PASS", f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}。")
    try:
        if not args.skip_unit_tests:
            print("正在运行离线自动测试（最多 180 秒）……", flush=True)
            check_unit_tests(report)
        if not args.unit_only:
            if sys.platform != "linux":
                report.add("linux", "FAIL", "部署诊断需要 Linux；其他系统可使用 Python 模块的 --unit-only。")
            else:
                print("正在检查配置、systemd 和本地无线状态……", flush=True)
                properties = unit_properties()
                options = installed_options(properties)
                check_service(report, properties)
                config = (args.config or (Path(options["config"]) if options else default_config_path())).expanduser().resolve()
                if not args.config and not options and properties.get("LoadState") == "loaded":
                    report.add("config_discovery", "WARN", "无法可靠解析定制服务配置，回退到当前用户默认路径；可显式指定 --config。")
                settings = check_config(report, config, properties, options)
                check_wifi(report)
                check_remote_client(report)
                for path in ("setup-linux.sh", "test-linux.sh", "uninstall-linux.sh", "check-cleanup-linux.sh", "deploy/systemd/install.sh"):
                    result = command(["bash", "-n", str(ROOT / path)])
                    report.add("bash_syntax_" + Path(path).stem.replace("-", "_"), "PASS" if result and result.returncode == 0 else "FAIL", "Bash 语法检查通过。" if result and result.returncode == 0 else "Bash 语法检查失败或命令不可用。")
                if settings:
                    check_recent_state(report, config, settings, properties.get("ActiveState") == "active")
                    if args.offline:
                        report.add("live_network", "SKIP", "--offline 已跳过外网和门户 GET 请求；未验证实际联网。")
                    else:
                        print("正在执行只读外网/门户 GET 检查（最多 45 秒）……", flush=True)
                        same = options and Path(options["config"]).expanduser().resolve() == config
                        check_network(report, config, options if same else {})
                else:
                    report.add("live_network", "SKIP", "配置检查失败，已跳过实际网络请求。")
        parent = args.report_dir or Path.home() / ".local/state/jnu_network_keeper/diagnostics"
        print(report.render(), end="")
        directory = report.save(parent.expanduser().resolve())
        print(f"私有报告目录：{directory}")
        return report.exit_code
    except KeyboardInterrupt:
        print("测试已中断；未执行登录或网络变更。", file=sys.stderr)
        return 130
    except Exception:
        # Unknown exceptions may embed raw config values. Never leak a traceback.
        print("诊断或报告写入失败；请检查本地文件权限、磁盘空间和命令环境。未输出原始异常。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
