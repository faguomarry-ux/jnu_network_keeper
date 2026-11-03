from __future__ import annotations

import base64
import ctypes
from dataclasses import asdict, dataclass, field
import json
import math
import os
import secrets
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


DEFAULT_PORTAL = "https://webauthsa.jnu.edu.cn:8443/eportal/"


def default_config_path() -> Path:
    if os.environ.get("JNU_NET_CONFIG"):
        return Path(os.environ["JNU_NET_CONFIG"]).expanduser()
    if os.name == "nt":
        # AppData may be virtualized for packaged desktop processes, so a task
        # launched outside that package could see a different file. User profile
        # storage gives the CLI, launcher and Task Scheduler one stable location.
        return Path.home() / ".config" / "jnu_network_keeper" / "config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "jnu_network_keeper" / "config.json"


@dataclass
class Account:
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    service: str = ""
    # Opaque local identity: counters never contain usernames or password hashes.
    id: str = field(default_factory=lambda: secrets.token_hex(16), repr=False)


@dataclass
class Settings:
    portal_url: str = DEFAULT_PORTAL
    username: str = ""
    password: str = field(default="", repr=False)
    service: str = ""
    interval: float = 60
    interval_jitter: float = 0
    follow_dhcp: bool = False
    timeout: float = 6
    max_backoff: float = 900
    gateway_ip: str = ""
    source_address: str = ""
    ca_file: str = ""
    # Auto reads the actual login page's passwordEncrypt/public-key inputs.
    password_encryption: str = "auto"
    accounts: list[Account] = field(default_factory=list, repr=False)
    primary_account: int = 1
    account_failover_threshold: int = 100000

    def validate(self) -> None:
        parsed = urlsplit(self.portal_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("portal_url must be an HTTPS URL without embedded credentials.")
        if parsed.query or parsed.fragment or not parsed.path.rstrip("/").endswith("/eportal"):
            raise ValueError("Use the ePortal base URL ending in /eportal/, not a success/session URL.")
        self.portal_url = self.portal_url.rstrip("/") + "/"
        for name in ("interval", "timeout", "max_backoff"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number.")
        if self.interval < 10 or self.max_backoff < self.interval:
            raise ValueError("interval must be >= 10 seconds; max_backoff must be >= interval.")
        if (isinstance(self.interval_jitter, bool) or not isinstance(self.interval_jitter, (int, float))
                or not math.isfinite(self.interval_jitter) or not 0 <= self.interval_jitter <= 3600):
            raise ValueError("interval_jitter must be a finite number from 0 to 3600 seconds.")
        if not isinstance(self.follow_dhcp, bool):
            raise ValueError("follow_dhcp must be a boolean.")
        if type(self.account_failover_threshold) is not int or not 1 <= self.account_failover_threshold <= 2147483647:
            raise ValueError("account_failover_threshold must be an integer from 1 to 2147483647.")
        if not isinstance(self.accounts, list) or len(self.accounts) > 3:
            raise ValueError("accounts must be a list with at most three entries.")
        if type(self.primary_account) is not int or not 1 <= self.primary_account <= max(1, len(self.accounts)):
            raise ValueError("primary_account must identify a configured account (1-based).")
        for account in self.accounts:
            if (not isinstance(account, Account) or not all(isinstance(v, str) for v in
                    (account.username, account.password, account.service, account.id))
                    or not account.username.strip() or not account.password
                    or len(account.id) != 32 or any(c not in "0123456789abcdef" for c in account.id)):
                raise ValueError("Invalid account entry; use init-accounts to save credentials.")
        if (len({a.id for a in self.accounts}) != len(self.accounts)
                or len({a.username.strip() for a in self.accounts}) != len(self.accounts)):
            raise ValueError("Account identities and usernames must be distinct.")
        if self.password_encryption not in ("auto", "rsa", "plain"):
            raise ValueError("password_encryption must be auto, rsa, or plain.")
        for name in ("username", "password", "service", "gateway_ip", "source_address", "ca_file"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string.")
        import ipaddress
        for name in ("gateway_ip", "source_address"):
            value = getattr(self, name)
            if value:
                ipaddress.IPv4Address(value)


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """Windows user-bound DPAPI; never a home-grown encryption algorithm."""
    if os.name != "nt":
        raise ValueError("This password is protected by Windows DPAPI. Run init on this device again.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt32.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        fn = crypt32.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), "JNU Network Keeper", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ValueError("Windows DPAPI failed; use the same Windows account that created the config.")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def write_private_json(path: Path, payload: dict, *, durable=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".keeper-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            if durable:
                stream.flush()
                os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        if durable and os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_settings(path: Path, settings: Settings) -> None:
    settings.validate()
    payload = asdict(settings)
    if settings.accounts:
        payload.pop("username")
        payload.pop("password")
        for account in payload["accounts"]:
            account["password"] = encode_password(account["password"])
    else:
        payload["password"] = encode_password(payload["password"])
    write_private_json(path, payload)


def encode_password(password):
    if os.name == "nt":
        return {"scheme": "windows-dpapi", "value": base64.b64encode(_dpapi(password.encode())).decode()}
    return {"scheme": "plain", "value": password}


def decode_password(secret):
    if not isinstance(secret, dict):
        raise ValueError("password must contain scheme and value; run init/init-accounts.")
    if secret.get("scheme") == "windows-dpapi":
        return _dpapi(base64.b64decode(secret["value"], validate=True), decrypt=True).decode()
    if secret.get("scheme") == "plain":
        return secret["value"]
    if not secret:
        return ""
    raise ValueError("Unsupported password storage scheme.")


def load_settings(path: Path, *, use_environment=True) -> Settings:
    payload = {}
    if path.exists():
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ValueError("Config permissions are too broad. Run chmod 600 on the config file.")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("Config must be a JSON object.")
        if payload.get("accounts"):
            if not isinstance(payload["accounts"], list):
                raise ValueError("Invalid accounts list; use init-accounts.")
            accounts = []
            for entry in payload["accounts"]:
                if not isinstance(entry, dict) or set(entry) - {"id", "username", "password", "service"} or "id" not in entry:
                    raise ValueError("Invalid account entry; use init-accounts.")
                accounts.append(Account(**{**entry, "password": decode_password(entry.get("password", {}))}))
            payload["accounts"] = accounts
            # List mode has one canonical credential store, never stale duplicates.
            if "username" in payload or "password" in payload:
                raise ValueError("Do not combine legacy credentials with accounts; use init-accounts.")
        else:
            payload["password"] = decode_password(payload.get("password", {}))
    # Environment overrides are useful on headless servers; no password CLI flag.
    if use_environment:
        if payload.get("accounts") and any(os.environ.get("JNU_NET_" + key) is not None for key in ("USERNAME", "PASSWORD")):
            raise ValueError("Account-list mode cannot combine JNU_NET_USERNAME/PASSWORD overrides; update the saved list instead.")
        for name in ("username", "password", "service"):
            value = os.environ.get("JNU_NET_" + name.upper())
            if value is not None:
                payload[name] = value
    known = Settings.__dataclass_fields__
    if set(payload) - set(known):
        raise ValueError("Unknown configuration keys: " + ", ".join(sorted(set(payload) - set(known))))
    settings = Settings(**payload)
    settings.validate()
    if settings.accounts:
        primary = settings.accounts[settings.primary_account - 1]
        settings.username, settings.password = primary.username, primary.password
    return settings
