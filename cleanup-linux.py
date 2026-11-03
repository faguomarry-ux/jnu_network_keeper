"""JNU cleanup: standalone standard-library uninstaller and scoped verifier.

Never import the project or execute the installed privileged helper.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import configparser
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


JNU_CLEANUP_FORMAT = 1
UNITS = ("jnu-network-guard.timer", "jnu-network-guard.service", "jnu-network-keeper.service")
WIFI_FIELDS = ("connection.permissions", "connection.autoconnect", "connection.autoconnect-retries", "connection.auth-retries")
RECEIPT = Path("/var/lib/jnu-network-keeper-cleanup/receipt.json")
SOURCE = Path(__file__).absolute()


class CleanupError(RuntimeError):
    pass


def absolute(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise CleanupError("所有清理路径必须是无 .. 的绝对路径。")
    return path


def within(path, parent):
    return path == parent or parent in path.parents


def no_symlink(path, *, leaf_link=False):
    nodes = path.parents if leaf_link else (path, *path.parents)
    if any(node.is_symlink() for node in nodes):
        raise CleanupError("路径含符号链接，已拒绝沿链接删除；请检查并指定真实路径。")


def private_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    no_symlink(path)
    if os.name != "nt" and (path.parent.stat().st_uid != os.geteuid() or path.parent.stat().st_mode & 0o077):
        raise CleanupError("清理凭据目录必须归 root 且权限为 0700；请换一个私有 --receipt 路径。")
    fd, temporary = tempfile.mkstemp(prefix=".cleanup-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class System:
    def __init__(self, root=Path("/")):
        self.root = root

    def path(self, relative):
        return self.root / relative.lstrip("/")

    def run(self, args, timeout=20):
        executable = shutil.which(args[0], path="/usr/sbin:/usr/bin:/sbin:/bin")
        if not executable:
            return None
        try:
            return subprocess.run([executable, *args[1:]], stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                  cwd=self.root, env={**os.environ, "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            return None

    def props(self, name):
        result = self.run(["systemctl", "show", "--no-pager", name, "-p", "LoadState", "-p", "ActiveState",
                           "-p", "User", "-p", "FragmentPath", "-p", "DropInPaths", "-p", "WorkingDirectory"])
        if result is None or result.returncode:
            raise CleanupError("无法可靠读取 systemd 状态；已拒绝把未知状态当作清理成功。")
        data = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if not data.get("LoadState") or not data.get("ActiveState"):
            raise CleanupError("systemd 返回不完整状态。")
        return data

    def processes(self):
        count, unknown = 0, False
        proc = self.path("/proc")
        if not proc.is_dir():
            return 0, True
        for item in proc.iterdir():
            if not item.name.isdigit() or int(item.name) == os.getpid():
                continue
            try:
                args = (item / "cmdline").read_bytes().split(b"\0")
                match = any(args[i] == b"-m" and (args[i + 1] == b"jnu_network_keeper" or args[i + 1].startswith(b"jnu_network_keeper.")) for i in range(len(args) - 1))
                match = match or any(arg.endswith((b"/jnu-network-guard.py", b"/jnu_network_keeper/linux_network.py")) for arg in args)
                count += bool(match)
            except FileNotFoundError:
                pass
            except OSError:
                unknown = True
        return count, unknown


def fixed_targets(system):
    entries = []
    for unit in UNITS:
        entries.extend([{"path": str(system.path("/etc/systemd/system/" + unit)), "kind": "file", "label": "systemd_unit"},
                        {"path": str(system.path("/etc/systemd/system/" + unit + ".d")), "kind": "tree", "label": "unit_dropins"}])
    for target, unit in (("multi-user.target", UNITS[2]), ("timers.target", UNITS[0])):
        entries.append({"path": str(system.path(f"/etc/systemd/system/{target}.wants/{unit}")), "kind": "link", "label": "enable_link"})
    for name, kind, label in (("/usr/local/libexec/jnu-network-guard.py", "file", "wifi_helper"),
                              ("/etc/jnu-network-guard.json", "file", "wifi_config"),
                              ("/etc/jnu-network-guard.backup.json", "file", "wifi_backup"),
                              ("/var/lib/jnu-network-guard", "tree", "wifi_state")):
        entries.append({"path": str(system.path(name)), "kind": kind, "label": label})
    return entries


def is_project(path):
    return all((path / name).is_file() for name in ("setup-linux.sh", "README.md", "jnu_network_keeper/keeper.py", "jnu_network_keeper/config.py"))


def validate_target(entry, home, project, system):
    path = absolute(entry["path"])
    kind = entry["kind"]
    if kind not in ("file", "tree", "link", "project"):
        raise CleanupError("不认识的清理目标类型。")
    no_symlink(path, leaf_link=kind == "link")
    protected = {system.root, home}
    protected.update(system.path(name) for name in ("/home", "/root", "/etc", "/usr", "/usr/local", "/usr/local/libexec", "/var", "/var/lib", "/opt", "/srv", "/tmp", "/var/tmp", "/mnt", "/media"))
    if path in protected or within(home, path):
        raise CleanupError("清理目标是受保护目录或用户主目录的祖先，已拒绝删除。")
    allowed = {(item["path"], item["kind"]) for item in fixed_targets(system)}
    if (str(path), kind) in allowed:
        return
    etc = system.path("/etc/systemd/system")
    if kind == "link" and path.name in UNITS and path.parent.name.endswith((".wants", ".requires")) and within(path, etc):
        return
    if any(within(path, system.path(name)) for name in ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/proc", "/sys", "/dev", "/run")):
        raise CleanupError("额外目标位于系统目录，且不是本项目固定安装文件。")
    if any(within(path, home / name) for name in (".ssh", ".gnupg", ".aws", ".kube")):
        raise CleanupError("额外目标属于其他凭据目录，已拒绝删除。")
    if kind == "project":
        if path != project or (path.exists() and not is_project(path)):
            raise CleanupError("删除源码需要一个已核实的完整项目目录。")
    elif kind == "tree":
        dedicated = path.name == "jnu_network_keeper" and within(path, home)
        own_data = path in (project / "data", project / "logs") and (is_project(project) or not path.exists())
        report = entry.get("label") == "custom_report" and path.name.startswith("test-")
        if not (dedicated or own_data or report):
            raise CleanupError("不允许递归删除非专用目录；自定义备份请逐个使用 --extra-file。")
    elif kind == "file" and path.exists() and not path.is_file():
        raise CleanupError("文件清理目标实际是目录，已拒绝删除。")


def own_units(system):
    props = {}
    for unit in UNITS:
        data = system.props(unit)
        path = system.path("/etc/systemd/system/" + unit)
        fragment = data.get("FragmentPath")
        if fragment and Path(fragment) != path:
            raise CleanupError("同名单元来自非本项目标准安装位置；请先人工核实。")
        no_symlink(path)
        if path.exists():
            text = path.read_text(encoding="utf-8")
            expected = ("-m jnu_network_keeper run" if unit == UNITS[2] else
                        ("jnu-network-guard.py recover" if unit == UNITS[1] else "Unit=jnu-network-guard.service"))
            if expected not in text:
                raise CleanupError("检测到不属于本项目的同名单元，已拒绝覆盖或删除。")
        props[unit] = data
    helper = system.path("/usr/local/libexec/jnu-network-guard.py")
    no_symlink(helper)
    if helper.exists() and "Bounded NetworkManager Wi-Fi recovery" not in helper.read_text(encoding="utf-8"):
        raise CleanupError("同名无线 helper 无法确认为本项目文件。")
    return props


def service_config(props):
    path = props[UNITS[2]].get("FragmentPath")
    if not path or props[UNITS[2]].get("DropInPaths"):
        return None
    try:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string(Path(path).read_text(encoding="utf-8"))
        args = shlex.split(parser["Service"]["ExecStart"])
        if args[1:4] != ["-m", "jnu_network_keeper", "run"]:
            return None
        return absolute(args[args.index("--config") + 1].replace("%%", "%"))
    except (ValueError, OSError, KeyError, IndexError, configparser.Error):
        return None


def account(name):
    import pwd
    try:
        return Path(pwd.getpwnam(name).pw_dir).resolve()
    except KeyError:
        raise CleanupError("找不到指定 Linux 服务用户。") from None


def add_entry(entries, path, kind, label):
    value = {"path": str(absolute(path)), "kind": kind, "label": label}
    if not any(item["path"] == value["path"] for item in entries):
        entries.append(value)


def make_plan(args, system):
    props = own_units(system)
    installed_user = props[UNITS[2]].get("User")
    name = args.user or installed_user or os.environ.get("SUDO_USER") or __import__("getpass").getuser()
    if args.user and installed_user and args.user != installed_user:
        raise CleanupError("--user 与已安装服务的用户不同；请使用实际服务用户。")
    home = account(name)
    project = absolute(args.project_dir or SOURCE.parent)
    entries = fixed_targets(system)
    configs = [home / ".config/jnu_network_keeper/config.json"]
    detected = service_config(props)
    if props[UNITS[2]].get("LoadState") == "loaded" and detected is None and not args.config:
        raise CleanupError("无法解析定制服务的配置路径；请显式提供 --config，避免漏掉凭据。")
    if detected:
        configs.append(detected)
    configs.extend(absolute(path) for path in args.config)
    for config in dict.fromkeys(configs):
        no_symlink(config)
        if config.parent.name == "jnu_network_keeper" and within(config.parent, home):
            add_entry(entries, config.parent, "tree", "private_config_directory")
        else:
            for path in (config, config.parent / "state.json", config.parent / "auth_guard.json", config.parent / "account_state.json", config.parent / "keeper.lock",
                         *[config.parent / ("keeper.log" + suffix) for suffix in ("", ".1", ".2", ".3")]):
                add_entry(entries, path, "file", "custom_config_or_runtime")
    add_entry(entries, home / ".local/state/jnu_network_keeper", "tree", "diagnostic_reports")
    for parent in args.report_dir:
        parent = absolute(parent)
        no_symlink(parent)
        if parent.exists():
            for child in parent.glob("test-*"):
                no_symlink(child)
                report = child / "report.json"
                if not child.is_dir() or not report.is_file():
                    raise CleanupError("自定义报告目录中存在无法识别的 test-* 项，已保留；请人工核实。")
                data = json.loads(report.read_text(encoding="utf-8"))
                if data.get("format_version") != 1 or not isinstance(data.get("checks"), list) or set(p.name for p in child.iterdir()) - {"report.json", "report.txt"}:
                    raise CleanupError("自定义报告子目录含其他文件或格式不匹配，已拒绝递归删除。")
                add_entry(entries, child, "tree", "custom_report")
    for path in args.extra_file:
        add_entry(entries, absolute(path), "file", "explicit_backup_or_log")
    if is_project(project):
        for name in (".env", "config.local.json", "config.json"):
            add_entry(entries, project / name, "file", "project_private_file")
        for name in ("data", "logs"):
            add_entry(entries, project / name, "tree", "project_private_directory")
    if args.remove_project:
        add_entry(entries, project, "project", "source_project")
    etc = system.path("/etc/systemd/system")
    if etc.exists():
        for directory, _, files in os.walk(etc, followlinks=False):
            for name in set(files) & set(UNITS):
                path = Path(directory) / name
                if path.is_symlink() and path.parent.name.endswith((".wants", ".requires")):
                    add_entry(entries, path, "link", "enable_link")
    for entry in entries:
        validate_target(entry, home, project, system)
        if entry["kind"] in ("tree", "project") and within(Path(sys.executable).absolute(), Path(entry["path"])):
            raise CleanupError("当前 Python 位于待删除目录内；请用目录外的系统 Python 执行清理。")
    receipt = absolute(args.receipt)
    no_symlink(receipt)
    if any(within(receipt, Path(item["path"])) or within(receipt.parent / "check-cleanup.py", Path(item["path"])) for item in entries):
        raise CleanupError("清理凭据/独立检查器必须放在待删除范围之外。")
    return {"format_version": JNU_CLEANUP_FORMAT, "phase": "planned", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "home": str(home), "service_user": name, "project": str(project), "targets": entries, "configs": [str(c) for c in dict.fromkeys(configs)],
            "report_parents": [str(absolute(p)) for p in args.report_dir],
            "wifi_mode": "forget" if args.forget_wifi else ("keep" if args.keep_wifi_settings else "restore"),
            "remove_project": args.remove_project}


def load_receipt(path):
    path = absolute(path)
    no_symlink(path)
    if os.name != "nt" and (path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077 or path.parent.stat().st_uid != os.geteuid() or path.parent.stat().st_mode & 0o077):
        raise CleanupError("清理凭据及父目录必须归 root，且不能被其他用户访问。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format_version") != JNU_CLEANUP_FORMAT or not isinstance(data.get("targets"), list):
        raise CleanupError("清理凭据格式无效。")
    for item in data["targets"]:
        absolute(item["path"])
        if item["kind"] not in ("file", "tree", "link", "project"):
            raise CleanupError("清理凭据含未知目标。")
    # Older receipts predate account lists. Include the new sibling state file
    # on every read so a previous cleanup scope cannot silently miss it.
    for config in data.get("configs", []):
        add_entry(data["targets"], absolute(config).parent / "account_state.json", "file", "account_failover_state")
    return data


def stop_services(system):
    for unit in UNITS:
        props = system.props(unit)
        if props["ActiveState"] not in ("inactive", "failed"):
            result = system.run(["systemctl", "stop", unit], timeout=45)
            if result is None or result.returncode:
                raise CleanupError("服务未成功停止，已保留凭据/文件；请检查 systemd。")
        if props["LoadState"] == "loaded":
            result = system.run(["systemctl", "disable", unit])
            if result is None or result.returncode:
                raise CleanupError("服务未成功禁用，已暂停删除；请检查权限或自定义单元。")
        if system.props(unit)["ActiveState"] not in ("inactive", "failed"):
            raise CleanupError("后台服务仍在运行，拒绝删除它可能重新生成的数据。")
    count, unknown = system.processes()
    if count or unknown:
        raise CleanupError("发现前台 keeper/诊断/无线守护进程，或无法完整检查进程；请先关闭它们再重试。")


def hold_lock(stack, path):
    if not path.exists():
        return
    no_symlink(path)
    import fcntl
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    stream = stack.enter_context(os.fdopen(descriptor, "r+b"))
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise CleanupError("仍有进程持有实例锁；没有删除锁文件来绕过它。") from None


def handle_wifi(plan, system):
    config = system.path("/etc/jnu-network-guard.json")
    backup = system.path("/etc/jnu-network-guard.backup.json")
    if not config.exists() and not backup.exists():
        return "not_configured"
    if plan["wifi_mode"] == "keep":
        return "kept_by_request"
    data = json.loads(backup.read_text(encoding="utf-8")) if backup.exists() else {}
    selected = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    profile = data.get("uuid") or selected.get("uuid")
    try:
        if str(uuid.UUID(profile)) != profile or (selected.get("uuid") and selected["uuid"] != profile):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise CleanupError("Wi-Fi 配置/备份不一致，已保留备份。") from None
    query = system.run(["nmcli", "--colors", "no", "--escape", "no", "--wait", "10", "-g", "connection.uuid", "connection", "show", "uuid", profile])
    if query and query.returncode == 10:
        return "profile_already_absent"
    if query is None or query.returncode:
        raise CleanupError("NetworkManager 不可用或查询失败，已保留 Wi-Fi 备份；可修复后重试或明确选择 --keep-wifi-settings。")
    if plan["wifi_mode"] == "forget":
        result = system.run(["nmcli", "--wait", "10", "connection", "delete", "uuid", profile])
        if result is None or result.returncode:
            raise CleanupError("未能删除指定 Wi-Fi 连接，已保留备份。")
        check = system.run(["nmcli", "-g", "connection.uuid", "connection", "show", "uuid", profile])
        if check is None or check.returncode != 10:
            raise CleanupError("无法确认指定 Wi-Fi 连接已删除，已保留备份。")
        return "forgotten"
    original = data.get("original")
    if not isinstance(original, dict) or set(original) != set(WIFI_FIELDS) or not all(isinstance(v, str) for v in original.values()):
        raise CleanupError("缺少有效 Wi-Fi 原值备份；请人工核实后重试，或明确选择 --keep-wifi-settings。")
    values = [part for key in WIFI_FIELDS for part in (key, original[key])]
    result = system.run(["nmcli", "connection", "modify", "uuid", profile, *values])
    if result is None or result.returncode:
        raise CleanupError("恢复 Wi-Fi 原设置失败，已保留备份。")
    for key in WIFI_FIELDS:
        check = system.run(["nmcli", "--escape", "no", "-g", key, "connection", "show", "uuid", profile])
        value = check.stdout.strip() if check is not None else None
        if value == "--":
            value = ""
        if check is None or check.returncode or value != original[key]:
            raise CleanupError("Wi-Fi 恢复后的原值复查未通过，已保留备份。")
    return "restored"


def remove_target(entry):
    path = absolute(entry["path"])
    no_symlink(path, leaf_link=entry["kind"] == "link")
    if entry["kind"] == "link":
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            raise CleanupError("启动链接位置实际是普通文件，已拒绝删除。")
    elif path.exists():
        if entry["kind"] in ("tree", "project"):
            if not path.is_dir():
                raise CleanupError("目录清理目标类型已变化，已停止。")
            if os.path.ismount(path):
                raise CleanupError("递归目标是挂载点，已拒绝删除。")
            for directory, names, _ in os.walk(path, followlinks=False):
                if any(os.path.ismount(Path(directory) / name) for name in names):
                    raise CleanupError("递归目标内部存在挂载点，已拒绝跨文件系统删除。")
            shutil.rmtree(path)
        else:
            path.unlink()


def retain_checker(receipt):
    destination = receipt.parent / "check-cleanup.py"
    if destination.exists() and "JNU_CLEANUP_FORMAT = 1" not in destination.read_text(encoding="utf-8"):
        raise CleanupError("检查器目标位置有其他文件，已拒绝覆盖。")
    no_symlink(destination)
    if SOURCE != destination:
        content = SOURCE.read_bytes()
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o700)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.chmod(destination, 0o700)


def apply_plan(plan, receipt, system):
    # Revalidate immediately before each mutation, not just at preview time.
    home, project = Path(plan["home"]), Path(plan["project"])
    for entry in plan["targets"]:
        validate_target(entry, home, project, system)
    own_units(system)
    private_json(receipt, plan)
    retain_checker(receipt)
    stop_services(system)
    with ExitStack() as stack:
        locks = {Path(config).parent / "keeper.lock" for config in plan["configs"]}
        locks.add(system.path("/var/lib/jnu-network-guard/guard.lock"))
        for path in sorted(locks):
            hold_lock(stack, path)
        plan["phase"] = "services_stopped"
        private_json(receipt, plan)
        plan["wifi_result"] = handle_wifi(plan, system)
        plan["phase"] = "wifi_handled"
        private_json(receipt, plan)
        entries = sorted(plan["targets"], key=lambda item: item["kind"] in ("tree", "project"))
        for entry in entries:
            # An enclosing project may already have disappeared only on retries.
            if not (entry["kind"] == "project" and not Path(entry["path"]).exists()):
                validate_target(entry, home, project, system)
            remove_target(entry)
    result = system.run(["systemctl", "daemon-reload"])
    if result is None or result.returncode:
        raise CleanupError("文件已处理，但 systemd daemon-reload 未成功；请重试并检查清理结果。")
    for unit in UNITS:
        system.run(["systemctl", "reset-failed", unit])
    plan["phase"] = "purged"
    private_json(receipt, plan)


def verify(plan, system):
    rows = []
    for entry in plan["targets"]:
        path = Path(entry["path"])
        try:
            no_symlink(path, leaf_link=entry["kind"] == "link")
            try:
                path.lstat()
                present = True
            except FileNotFoundError:
                present = False
            rows.append(("RESIDUE" if present else "CLEAN", entry["label"]))
        except (OSError, CleanupError):
            rows.append(("UNKNOWN", entry["label"]))
    for unit in UNITS:
        try:
            data = system.props(unit)
            clear = data["LoadState"] == "not-found" and data["ActiveState"] in ("inactive", "failed")
            rows.append(("CLEAN" if clear else "RESIDUE", unit))
        except CleanupError:
            rows.append(("UNKNOWN", unit))
    count, unknown = system.processes()
    rows.append(("UNKNOWN" if unknown else ("RESIDUE" if count else "CLEAN"), "keeper_processes"))
    for parent in plan.get("report_parents", []):
        try:
            remaining = any(Path(parent).glob("test-*/report.json"))
            rows.append(("RESIDUE" if remaining else "CLEAN", "custom_report_scan"))
        except OSError:
            rows.append(("UNKNOWN", "custom_report_scan"))
    def walk_error(error):
        raise error
    try:
        references = False
        for directory, _, files in os.walk(system.path("/etc/systemd/system"), followlinks=False, onerror=walk_error):
            if set(files) & set(UNITS):
                references = True
        rows.append(("RESIDUE" if references else "CLEAN", "unit_file_and_enable_link_scan"))
    except OSError:
        rows.append(("UNKNOWN", "unit_file_and_enable_link_scan"))
    journal = system.run(["journalctl", "--no-pager", "-n", "1", "-o", "json", *[part for unit in UNITS for part in ("-u", unit)]], timeout=10)
    rows.append(("RETAINED" if journal and journal.returncode == 0 and journal.stdout.strip() else "LIMIT", "shared_journal_not_erased"))
    rows.append(("LIMIT", "browser_backups_snapshots_and_other_accounts_not_scanned"))
    if not plan.get("remove_project") and Path(plan["project"]).exists():
        rows.append(("RETAINED", "source_project"))
    rows.append(("LIMIT", "cleanup_receipt_and_standalone_checker_retained_until_forget_receipt"))
    code = 2 if any(status == "UNKNOWN" for status, _ in rows) else (1 if any(status == "RESIDUE" for status, _ in rows) else 0)
    return code, rows


def print_result(code, rows):
    for status, label in rows:
        print(f"[{status}] {label}")
    print("指定范围检查通过。" if code == 0 else "仍有残留或无法确认的项目，请处理后重新检查。")
    print("结果只覆盖本次声明范围；共享 journal、浏览器、其他账号及手工备份/快照不代表已清除。")


def forget_receipt(receipt):
    checker = receipt.parent / "check-cleanup.py"
    no_symlink(receipt)
    no_symlink(checker)
    if checker.exists() and "JNU_CLEANUP_FORMAT = 1" not in checker.read_text(encoding="utf-8"):
        raise CleanupError("独立检查器内容不匹配，未删除清理凭据。")
    checker.unlink(missing_ok=True)
    receipt.unlink()
    try:
        receipt.parent.rmdir()
    except OSError:
        pass


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("--apply", action="store_true")
    uninstall.add_argument("--user")
    uninstall.add_argument("--config", type=Path, action="append", default=[])
    uninstall.add_argument("--report-dir", type=Path, action="append", default=[])
    uninstall.add_argument("--extra-file", type=Path, action="append", default=[])
    uninstall.add_argument("--project-dir", type=Path)
    uninstall.add_argument("--remove-project", action="store_true")
    wifi = uninstall.add_mutually_exclusive_group()
    wifi.add_argument("--keep-wifi-settings", action="store_true")
    wifi.add_argument("--forget-wifi", action="store_true")
    uninstall.add_argument("--receipt", type=Path, default=RECEIPT)
    check = sub.add_parser("check")
    check.add_argument("--receipt", type=Path, default=RECEIPT)
    check.add_argument("--forget-receipt", action="store_true")
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    if sys.platform != "linux" or sys.version_info < (3, 10):
        print("需要 Linux 和 Python 3.10+。", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("请使用 sudo 运行，以便检查系统服务、实际用户数据和清理凭据。", file=sys.stderr)
        return 2
    system = System()
    try:
        receipt = absolute(args.receipt)
        if receipt.name == "check-cleanup.py":
            raise CleanupError("清理凭据不能与保留检查器使用同一文件名。")
        if args.command == "check":
            plan = load_receipt(receipt)
            code, rows = verify(plan, system)
            print_result(code, rows)
            if code == 0 and args.forget_receipt:
                forget_receipt(receipt)
                print("清理凭据及保留检查器已删除；以后无法按该凭据复查自定义路径。")
            return code
        # A prior receipt preserves custom scopes even after the unit is removed.
        previous = load_receipt(receipt) if receipt.exists() else None
        reuse = previous is not None and not any((args.user, args.config, args.report_dir, args.extra_file, args.project_dir, args.remove_project, args.keep_wifi_settings, args.forget_wifi))
        if reuse:
            plan = previous
        else:
            if previous:
                args.user = args.user or previous.get("service_user")
                args.project_dir = args.project_dir or Path(previous["project"])
            plan = make_plan(args, system)
            if previous:
                if plan["home"] != previous["home"] or plan["project"] != previous["project"]:
                    raise CleanupError("现有清理凭据属于另一个范围；请为新范围使用单独的 --receipt。")
                for item in previous["targets"]:
                    add_entry(plan["targets"], Path(item["path"]), item["kind"], item["label"])
                plan["configs"] = list(dict.fromkeys(plan["configs"] + previous["configs"]))
                plan["report_parents"] = list(dict.fromkeys(plan["report_parents"] + previous.get("report_parents", [])))
                plan["remove_project"] = plan["remove_project"] or previous.get("remove_project", False)
                if not args.keep_wifi_settings and not args.forget_wifi:
                    plan["wifi_mode"] = previous["wifi_mode"]
        print("清理计划（路径只用于预览/范围记录，不读取或输出凭据内容）：")
        for item in plan["targets"]:
            print(f'  {item["kind"]}: {item["path"]}')
        print("Wi-Fi 处理：" + plan["wifi_mode"])
        if plan["wifi_mode"] == "forget":
            print("删除指定 Wi-Fi 连接可能立即断开 SSH/向日葵。")
        if not args.apply:
            print("仅预览，未停止服务或删除文件。确认范围后使用相同参数加 --apply。")
            return 0
        apply_plan(plan, receipt, system)
        code, rows = verify(plan, system)
        if code == 0:
            plan["phase"] = "verified"
            private_json(receipt, plan)
        print_result(code, rows)
        print(f"清理范围凭据：{receipt}")
        print(f"源码删除后仍可检查：sudo {shlex.quote(sys.executable)} -I {shlex.quote(str(receipt.parent / 'check-cleanup.py'))} check --receipt {shlex.quote(str(receipt))}")
        return code
    except KeyboardInterrupt:
        print("已中断；已完成的删除不能回退，请按清理凭据重新检查。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(str(exc) if isinstance(exc, CleanupError) else "清理/检查失败：文件、权限或数据格式有异常；原始内容未输出。", file=sys.stderr)
        print("未报告清理成功。若已有清理凭据，可运行检查脚本定位残留。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
