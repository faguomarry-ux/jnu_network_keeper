from __future__ import annotations

import argparse
import json
import getpass
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sys
import threading

from .config import Account, default_config_path, load_settings, save_settings
from .keeper import InstanceLock, Keeper
from .auth_guard import AuthGuard
from .accounts import AccountPool
from .linux_network import LinkMonitor
from .supervisor import notify
from .portal import PortalError
from .transport import NetworkError


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="JNU ePortal keeper for Linux / Windows (Python 3.10+).")
    cli.add_argument("command", choices=("init", "init-accounts", "accounts", "reset-accounts", "status", "login", "run", "paths", "reset-auth"))
    cli.add_argument("--config", type=Path)
    cli.add_argument("--gateway-ip", help="Optional campus gateway IPv4 pin, preserving TLS hostname validation")
    cli.add_argument("--source-address", help="Optional local campus NIC IPv4, to avoid proxy/TUN routes")
    cli.add_argument("--interval", type=float, help="Probe interval in seconds, minimum 10")
    cli.add_argument("--jitter", dest="interval_jitter", type=float, help="Additional random probe delay, 0..3600 seconds")
    cli.add_argument("--follow-dhcp", action="store_true", default=None, help="Follow the configured Linux Wi-Fi's current source IPv4")
    cli.add_argument("--service", help="Exact ePortal service value; no ECNU account suffix is added")
    cli.add_argument("--log-file", type=Path)
    cli.add_argument("--primary-account", type=int, help="Primary slot for init-accounts (1..3)")
    cli.add_argument("--failover-threshold", dest="account_failover_threshold", type=int,
                     help="Failed submitted logins per account before switching (init-accounts only; default 100000)")
    return cli


def read_integer(prompt, default, minimum, maximum):
    text = input(f"{prompt} [{default}]: ").strip()
    try:
        value = int(text) if text else default
    except ValueError:
        raise ValueError("Enter an integer for account settings.") from None
    if not minimum <= value <= maximum:
        raise ValueError("Account setting is outside the permitted range.")
    return value


def configure_accounts(settings, args):
    old = settings.accounts[:]
    if not old and settings.username and settings.password:
        old = [Account(settings.username, settings.password)]
    print("Account-list mode retries explicit rejections under the shared 5-minute / 3-per-hour limits.")
    count = read_integer("Number of saved accounts (1..3)", len(old) if settings.accounts else 3, 1, 3)
    primary = args.primary_account if args.primary_account is not None else read_integer("Primary account slot", min(settings.primary_account, count), 1, count)
    threshold = args.account_failover_threshold if args.account_failover_threshold is not None else read_integer(
        "Failed submitted logins per account before switching", settings.account_failover_threshold, 1, 2147483647)
    if not 1 <= primary <= count or not 1 <= threshold <= 2147483647:
        raise ValueError("Primary slot or failover threshold is outside the permitted range.")
    accounts = []
    for index in range(count):
        previous = old[index] if index < len(old) else None
        username = input(f"Account {index + 1} ID (blank keeps saved): ").strip() or (previous.username if previous else "")
        password = getpass.getpass(f"Account {index + 1} campus password (blank keeps saved): ") or (previous.password if previous else "")
        service = input(f"Account {index + 1} optional portal service (blank keeps saved/inherits common): ").strip() or (previous.service if previous else "")
        if previous and (username, password, service) == (previous.username, previous.password, previous.service):
            accounts.append(previous)
        else:
            accounts.append(Account(username, password, service))
    settings.accounts = accounts
    settings.primary_account = primary
    settings.account_failover_threshold = threshold
    settings.validate()
    selected = accounts[primary - 1]
    settings.username, settings.password = selected.username, selected.password


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    config = (args.config or default_config_path()).expanduser().resolve()
    if args.command == "paths":
        print(f"Config: {config}\nLog: {config.parent / 'keeper.log'}\nState: {config.parent / 'state.json'}\nAccount state: {config.parent / 'account_state.json'}")
        return 0
    try:
        settings = load_settings(config)
        if args.command != "init-accounts" and (args.primary_account is not None or args.account_failover_threshold is not None):
            raise ValueError("Use init-accounts to change the primary slot or failover threshold.")
        for name in ("gateway_ip", "source_address", "interval", "interval_jitter", "follow_dhcp", "service"):
            value = getattr(args, name)
            if value is not None:
                setattr(settings, name, value)
        settings.validate()
        if args.command == "init-accounts":
            with InstanceLock(config.parent / "keeper.lock"):
                configure_accounts(settings, args)
                save_settings(config, settings)
                AccountPool(settings, config.parent / "account_state.json").reset()
            print("Saved account list; counters reset to the preferred primary. Global authentication limits retained.")
            return 0
        if args.command == "accounts":
            if settings.accounts:
                summary = AccountPool(settings, config.parent / "account_state.json").summary()
                print(json.dumps(summary, ensure_ascii=False, indent=2))
                return 2 if summary["account_status"] == "account_state_error" else 0
            print("Legacy single-account mode; run init-accounts to configure ordered failover.")
            return 0
        if args.command == "reset-accounts":
            if not settings.accounts:
                raise ValueError("No saved account list; run init-accounts first.")
            with InstanceLock(config.parent / "keeper.lock"):
                AccountPool(settings, config.parent / "account_state.json").reset()
            print("Account counters/exhaustion cleared; next authentication prefers the primary. Global auth limits retained.")
            return 0
        if args.command == "init":
            username = input("JNU student/staff ID (blank keeps saved ID): ").strip()
            if username:
                settings.username = username
            password = getpass.getpass("Campus password (blank keeps saved password): ")
            if password:
                settings.password = password
            if not settings.username or not settings.password:
                raise ValueError("Student/staff ID and campus password are required.")
            if settings.accounts:
                with InstanceLock(config.parent / "keeper.lock"):
                    index = settings.primary_account - 1
                    previous = settings.accounts[index]
                    changed = (settings.username, settings.password) != (previous.username, previous.password)
                    if changed:
                        settings.accounts[index] = Account(settings.username, settings.password, previous.service)
                    save_settings(config, settings)
                    if changed:
                        AccountPool(settings, config.parent / "account_state.json").reset()
            else:
                save_settings(config, settings)
            print(f"Saved private config: {config}")
            print("Password: Windows user-bound DPAPI." if os.name == "nt" else "Password: local plaintext, file permissions 600.")
            return 0
        log_path = args.log_file or config.parent / "keeper.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.close(log_fd)
        handlers = [RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")]
        if sys.stderr:
            handlers.append(logging.StreamHandler())
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers, force=True)
        guard = AuthGuard(config.parent / "auth_guard.json")
        pool = AccountPool(settings, config.parent / "account_state.json") if settings.accounts else None
        keeper = Keeper(settings, auth_guard=guard, link_check=LinkMonitor(settings), account_pool=pool)
        if args.command == "status":
            observed = keeper.check()
            logging.info("State: %s; Internet=%s; ePortal=%s", observed.state, observed.online, observed.authenticated)
            return 0 if observed.online else 1
        with InstanceLock(config.parent / "keeper.lock"):
            if args.command == "reset-auth":
                guard.reset_block()
                print("Authentication pause cleared; attempt history and cooldown retained.")
                return 0
            if args.command == "login":
                observed = keeper.step()
                logging.info("State: %s; Internet=%s; ePortal=%s", observed.state, observed.online, observed.authenticated)
                return 0 if observed.online else 1
            if not settings.username or not settings.password:
                raise ValueError("Run init before starting the background keeper.")
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            logging.info("JNU keeper started; probe interval=%ss; additional jitter=0..%ss; follow DHCP=%s",
                         settings.interval, settings.interval_jitter, settings.follow_dhcp)
            notify("READY=1\nWATCHDOG=1")
            keeper.run(config.parent / "state.json", stop)
            logging.info("JNU keeper stopped.")
            return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, PortalError, NetworkError) as exc:
        # All authentication/network errors are deliberately redacted in the owning layer.
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
