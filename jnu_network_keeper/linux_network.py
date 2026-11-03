"""Bounded NetworkManager Wi-Fi recovery; also runs standalone as a root helper.

Never disconnect a device, restart NetworkManager, or guess a network/password.
The installed root copy imports only the Python standard library.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid


CONFIG = Path("/etc/jnu-network-guard.json")
STATE = Path("/var/lib/jnu-network-guard/state.json")


class GuardError(RuntimeError):
    pass


class NetworkManager:
    def __init__(self, *, wait=20, timeout=25):
        self.wait = wait
        self.timeout = timeout

    def run(self, *args):
        try:
            result = subprocess.run(["nmcli", "--colors", "no", "--escape", "no", "--wait", str(self.wait), *args],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=self.timeout, env={**os.environ, "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            raise GuardError("NetworkManager command unavailable or timed out.") from None
        if result.returncode:
            # nmcli error text can include secrets/connection metadata; do not log it.
            raise GuardError("NetworkManager command failed; check profile, device and NetworkManager journal locally.")
        return result.stdout.strip()

    def field(self, name, *target, secret=False):
        args = (["--show-secrets"] if secret else []) + ["-g", name, *target]
        value = self.run(*args)
        return "" if value == "--" else value


def read_config(path=CONFIG):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"interface", "uuid"}:
            raise ValueError
        if (not isinstance(data["interface"], str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", data["interface"])
                or data["interface"].startswith("-") or str(uuid.UUID(data["uuid"])) != data["uuid"]):
            raise ValueError
        return data
    except (ValueError, OSError, TypeError, AttributeError):
        raise GuardError("Invalid Wi-Fi guard configuration; refusing network changes.") from None


def save_json(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".guard-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
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


def prepare(interface, connection, *, path=CONFIG, nm=None):
    nm = nm or NetworkManager()
    # Validate arguments with the same schema before invoking nmcli.
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface) or interface.startswith("-"):
        raise GuardError("Invalid interface name.")
    try:
        connection = str(uuid.UUID(connection))
    except ValueError:
        raise GuardError("Use the saved connection's UUID, not its SSID/name.") from None
    target = ("connection", "show", "uuid", connection)
    if nm.field("connection.type", *target) != "802-11-wireless":
        raise GuardError("The selected connection must be Wi-Fi.")
    if nm.field("GENERAL.TYPE", "device", "show", interface) != "wifi":
        raise GuardError("The selected device must be a NetworkManager Wi-Fi device.")
    bound = nm.field("connection.interface-name", *target)
    if bound and bound != interface:
        raise GuardError("Profile is bound to another interface; refusing to change it.")
    if nm.field("802-11-wireless.mode", *target) not in ("", "infrastructure"):
        raise GuardError("Only Wi-Fi infrastructure/client profiles are supported.")
    key = nm.field("802-11-wireless-security.key-mgmt", *target)
    if key in ("wpa-psk", "sae"):
        flags = nm.field("802-11-wireless-security.psk-flags", *target).split(" ", 1)[0]
        if flags != "0" or not nm.field("802-11-wireless-security.psk", *target, secret=True):
            raise GuardError("Save the Wi-Fi password for all users in NetworkManager first (not a desktop keyring); see LINUX-RECOVERY.md.")
    elif key not in ("", "owe"):
        raise GuardError("802.1X/WEP/enterprise Wi-Fi needs separate review; automatic setup refused.")
    # Save original values once, for an explicit rollback; no secrets are copied.
    backup = path.with_suffix(".backup.json")
    if not backup.exists():
        original = {name: nm.field(name, *target) for name in
                    ("connection.permissions", "connection.autoconnect", "connection.autoconnect-retries", "connection.auth-retries")}
        save_json(backup, {"uuid": connection, "original": original})
    else:
        previous = json.loads(backup.read_text(encoding="utf-8"))
        if previous.get("uuid") != connection:
            raise GuardError("A different Wi-Fi profile is already configured; restore it before selecting another.")
    nm.run("connection", "modify", "uuid", connection, "connection.permissions", "",
           "connection.autoconnect", "yes", "connection.autoconnect-retries", "2", "connection.auth-retries", "1")
    save_json(path, {"interface": interface, "uuid": connection}, 0o644)


def recover(*, config=CONFIG, state=STATE, nm=None, clock=time.time):
    nm = nm or NetworkManager()
    data = read_config(config)
    interface = data["interface"]
    status = nm.field("GENERAL.STATE", "device", "show", interface)
    match = re.match(r"^(\d+)\b", status)
    if not match:
        raise GuardError("Unknown NetworkManager device state; no action taken.")
    code = int(match[1])
    if code == 100:
        if nm.field("GENERAL.CON-UUID", "device", "show", interface) != data["uuid"]:
            return "wifi_other_connection_active"
        if not usable_ipv4(nm.field("IP4.ADDRESS", "device", "show", interface)):
            return "wifi_connected_without_ipv4"
        return "wifi_connected"
    if code not in (20, 30, 120):
        return "wifi_busy_or_unmanaged"
    if nm.run("networking") != "enabled":
        return "networking_disabled_requires_operator"
    if nm.field("WIFI-HW", "general", "status") != "enabled":
        return "wifi_hardware_blocked"
    now = clock()
    history = {"attempts": [], "updated_at": 0}
    if state.exists():
        try:
            history = json.loads(state.read_text(encoding="utf-8"))
            if not isinstance(history, dict) or set(history) != {"attempts", "updated_at"} or not isinstance(history["attempts"], list):
                raise ValueError
            if (not all(type(t) in (int, float) and math.isfinite(t) and t >= 0 for t in [history["updated_at"], *history["attempts"]])
                    or history["attempts"] != sorted(history["attempts"])
                    or not all(t <= history["updated_at"] for t in history["attempts"])):
                raise ValueError
        except (ValueError, TypeError):
            raise GuardError("Wi-Fi attempt history invalid; no action taken.") from None
    if now < history["updated_at"]:
        return "wifi_clock_error"
    attempts = [t for t in history["attempts"] if now - t < 900]
    if (attempts and now - attempts[-1] < 120) or len(attempts) >= 3:
        return "wifi_cooldown"
    # Persist before mutation, even if nmcli times out or the helper is killed.
    save_json(state, {"attempts": [*attempts, now], "updated_at": now})
    if nm.run("radio", "wifi") == "disabled":
        nm.run("radio", "wifi", "on")
        # Let NetworkManager autoconnect, do not race it with a second activation.
        return "wifi_enabled_waiting_for_autoconnect"
    # Re-check after inspection to avoid interrupting an activation already underway.
    current = nm.field("GENERAL.STATE", "device", "show", interface)
    if current.split(" ", 1)[0] not in ("20", "30", "120"):
        return "wifi_busy_or_connected"
    nm.run("connection", "up", "uuid", data["uuid"], "ifname", interface)
    return "wifi_activation_requested"


def usable_ipv4(text):
    addresses = []
    for value in text.splitlines():
        try:
            address = ipaddress.IPv4Interface(value).ip
        except ValueError:
            continue
        if not (address.is_link_local or address.is_loopback or address.is_unspecified or address.is_multicast):
            addresses.append(str(address))
    return tuple(dict.fromkeys(addresses))


class LinkMonitor:
    """Observe local link changes and optionally bind requests to the current DHCP IP."""

    def __init__(self, settings=None, *, config=CONFIG, nm=None):
        self.settings = settings
        self.config = config
        self.nm = nm or NetworkManager(wait=2, timeout=3)
        self.generation = 0
        self.snapshot = None

    def __call__(self):
        if sys.platform != "linux" or not self.config.exists():
            if self.settings and self.settings.follow_dhcp:
                if self.snapshot != ("guard_missing",):
                    self.snapshot = ("guard_missing",)
                    self.generation += 1
                    logging.warning("follow_dhcp requires Linux with a configured Wi-Fi guard; authentication deferred.")
                return False
            if self.snapshot != ("unconfigured",):
                self.snapshot = ("unconfigured",)
                self.generation += 1
            return True
        try:
            data = read_config(self.config)
            interface = data["interface"]
            state = self.nm.field("GENERAL.STATE", "device", "show", interface).split(" ", 1)[0]
            active = self.nm.field("GENERAL.CON-UUID", "device", "show", interface) if state == "100" else ""
            addresses = usable_ipv4(self.nm.field("IP4.ADDRESS", "device", "show", interface)) if state == "100" else ()
            snapshot = (interface, data["uuid"], state, active, tuple(sorted(addresses)))
            ready = state == "100" and active == data["uuid"] and bool(addresses)
        except GuardError:
            snapshot, addresses, ready = ("unavailable",), (), False
        if snapshot != self.snapshot:
            self.snapshot = snapshot
            self.generation += 1
        if ready and self.settings and self.settings.follow_dhcp and self.settings.source_address not in addresses:
            # Only runtime settings change; never rewrite credentials/configuration.
            self.settings.source_address = addresses[0]
            logging.info("Source IPv4 now follows the configured Wi-Fi DHCP address.")
        return ready


def link_ready():
    return LinkMonitor()()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "recover", "restore"))
    parser.add_argument("--interface")
    parser.add_argument("--connection")
    args = parser.parse_args()
    lock = None
    try:
        if os.geteuid() != 0:
            raise GuardError("This helper requires root.")
        import fcntl
        STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The timer and a manual helper invocation must never race each other.
        lock = (STATE.parent / "guard.lock").open("a+b")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Wi-Fi guard already running; no duplicate recovery started.")
            return 0
        if args.command == "prepare":
            if not args.interface or not args.connection:
                parser.error("prepare requires --interface and --connection")
            prepare(args.interface, args.connection)
            print("System-wide Wi-Fi autoconnect configured; current link was not disconnected.")
        elif args.command == "restore":
            data = json.loads(CONFIG.with_suffix(".backup.json").read_text(encoding="utf-8"))
            values = [item for pair in data["original"].items() for item in pair]
            NetworkManager().run("connection", "modify", "uuid", data["uuid"], *values)
            CONFIG.unlink(missing_ok=True)
            CONFIG.with_suffix(".backup.json").unlink()
            print("Original profile settings restored; keep the guard timer disabled.")
        else:
            print(recover())
        return 0
    except (GuardError, OSError, ValueError) as exc:
        print(str(exc) if isinstance(exc, GuardError) else "Wi-Fi guard file operation failed; inspect local files.", file=sys.stderr)
        return 1
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
