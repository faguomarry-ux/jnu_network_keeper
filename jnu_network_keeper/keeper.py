from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import random
from pathlib import Path
import time

from .config import Settings, write_private_json
from .auth_guard import AuthGuard
from .accounts import AccountPool
from .portal import AuthenticationRejected, PortalClient, PortalError, Session, is_online
from .supervisor import notify, supervised_wait
from .transport import NetworkError, Transport


class InstanceLock:
    """An OS-owned lock, automatically released after crashes or termination."""

    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            # Reading an already locked byte itself raises PermissionError on
            # Windows. Check the size without touching the locked byte instead.
            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise ValueError("A keeper/login instance using this config is already running.") from None
        return self

    def __exit__(self, *args):
        if os.name == "nt":
            import msvcrt
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        self.stream.close()


@dataclass
class Observation:
    state: str
    online: bool
    authenticated: bool
    retry_after: float = 0


class Keeper:
    def __init__(self, settings: Settings, *, transport=None, portal=None, probe=None, sleep=time.sleep,
                 auth_guard=None, link_check=None, wait=None, uniform=None, account_pool=None):
        self.settings = settings
        self.transport = transport or Transport(settings)
        self.portal = portal or PortalClient(settings, self.transport)
        self.probe = probe or (lambda: is_online(self.transport))
        self.sleep = sleep
        self.session: Session | None = None
        self.auth_guard = auth_guard or AuthGuard()
        self.link_check = link_check
        self.wait = wait or (lambda stop, delay: supervised_wait(stop, delay, wake=self._link_changed))
        self.uniform = uniform or random.SystemRandom().uniform
        self._link_generation = None
        self.account_pool = account_pool or (AccountPool(settings) if settings.accounts else None)
        self.base_service = settings.service

    def _link_changed(self):
        # Read only local NetworkManager state during long sleeps. No Internet
        # probes, login attempts or privileged recovery actions happen here.
        if self.link_check is None or not hasattr(self.link_check, "generation"):
            return False
        before = self.link_check.generation
        self.link_check()
        return self.link_check.generation != before

    def next_delay(self, failures):
        base = min(self.settings.max_backoff, self.settings.interval * 2 ** min(max(failures - 1, 0), 10))
        # Jitter only adds time; it never makes authentication retries faster.
        extra = self.settings.interval_jitter
        if failures:
            extra = min(extra, base * 0.25, self.settings.max_backoff - base)
        delay = base + (self.uniform(0, extra) if extra > 0 else 0)
        if self.session and self.session.keepalive_minutes > 0:
            due = self.session.last_heartbeat + self.session.keepalive_minutes * 60 - time.monotonic()
            delay = min(delay, max(1, due))
        return delay

    def adopt(self, session: Session | None) -> None:
        if self.session and session and self.session.user_index == session.user_index:
            session.last_heartbeat = self.session.last_heartbeat
            session.keepalive_minutes = session.keepalive_minutes or self.session.keepalive_minutes
        self.session = session

    def check(self) -> Observation:
        if self.link_check and not self.link_check():
            return Observation("link_unavailable", False, False)
        internet = self.probe()
        try:
            context = self.portal.discover()
        except (PortalError, NetworkError):
            if internet:
                return Observation("internet_online", True, False)
            raise
        self.adopt(context.session)
        return Observation("online" if internet else ("portal_online_internet_unreachable" if context.session else "offline"),
                           internet, bool(context.session))

    def step(self) -> Observation:
        if self.link_check:
            ready = self.link_check()
            generation = getattr(self.link_check, "generation", None)
            if generation != self._link_generation:
                # A DHCP/address/link change invalidates the cached device session.
                self.session = None
                self._link_generation = generation
            if not ready:
                self.session = None
                return Observation("link_unavailable", False, False)
        if self.session and self.session.keepalive_minutes > 0:
            due = self.session.last_heartbeat + self.session.keepalive_minutes * 60
            if time.monotonic() >= due:
                try:
                    self.portal.heartbeat(self.session)
                except (PortalError, NetworkError) as exc:
                    self.session = None
                    logging.warning("Heartbeat failed; checking current connectivity: %s", exc)
        internet = self.probe()
        if internet:
            # On startup, adopt a gateway-provided current-device session if available.
            if self.session is None:
                try:
                    self.session = self.portal.discover().session
                except (PortalError, NetworkError):
                    pass
            return Observation("online", True, bool(self.session))
        context = self.portal.discover()
        if context.session:
            self.adopt(context.session)
            # An authenticated gateway + failed Internet probes indicates an upstream
            # problem. Repeated password submissions do not fix that.
            return Observation("portal_online_internet_unreachable", False, True)
        self.session = None
        if self.account_pool:
            state = self.account_pool.inspect()
            if state:
                return Observation(state, False, False)
            _, account = self.account_pool.selected()
            self.settings.username, self.settings.password = account.username, account.password
            self.settings.service = account.service or self.base_service
        state, retry_after = self.auth_guard.reserve()
        if state:
            return Observation(state, False, False, retry_after)
        submitted = False
        def before_request():
            nonlocal submitted
            self.account_pool.begin()
            submitted = True
        try:
            if self.account_pool:
                self.session = self.portal.login(context, before_request=before_request)
            else:
                self.session = self.portal.login(context)
        except (PortalError, NetworkError) as exc:
            if self.account_pool:
                if submitted:
                    reason = ("gateway_rejected" if isinstance(exc, AuthenticationRejected) else
                              "network_error" if isinstance(exc, NetworkError) else "portal_response_error")
                    return Observation(self.account_pool.failed(reason), False, False)
                raise
            if isinstance(exc, AuthenticationRejected):
                self.auth_guard.block()
                raise AuthenticationRejected(str(exc) + " Automatic login paused; fix the issue, then run reset-auth.") from None
            raise
        if self.account_pool and submitted:
            # A persistence error after an accepted login is not a failed login.
            self.account_pool.succeeded()
        for attempt in range(3):
            self.sleep(2)
            if self.probe():
                return Observation("reconnected", True, True)
        return Observation("portal_online_internet_unreachable", False, True)

    def run(self, state_path: Path, stop) -> None:
        previous = None
        failures = 0
        while not stop.is_set():
            notify("WATCHDOG=1")
            try:
                observed = self.step()
                failures = 0 if observed.online else failures + 1
                state = observed.state
                if state != previous:
                    logging.info("Network state: %s", state)
            except (PortalError, NetworkError) as exc:
                failures += 1
                state = "connection_failed"
                logging.warning("%s", exc)
                observed = Observation(state, False, False)
            previous = state
            delay = self.next_delay(failures)
            payload = {"state": state, "online": observed.online,
                       "authenticated": observed.authenticated,
                       "retry_after": observed.retry_after,
                       "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "next_check_in": round(delay, 2),
                       "next_check_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + delay))}
            if self.account_pool:
                payload.update(self.account_pool.summary())
            write_private_json(state_path, payload)
            self.wait(stop, delay)
