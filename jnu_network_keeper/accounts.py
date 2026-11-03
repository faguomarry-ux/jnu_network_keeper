"""Ordered account failover, counting only failed submitted login requests."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import write_private_json
from .portal import PortalError


class AccountPool:
    def __init__(self, settings, path: Path | None = None):
        self.settings = settings
        self.path = path
        primary = settings.primary_account - 1
        self.order = [primary, *[i for i in range(len(settings.accounts)) if i != primary]]
        self.ids = [settings.accounts[i].id for i in self.order]
        self.data = self._blank()

    def _blank(self):
        return {"version": 1, "ids": self.ids, "threshold": self.settings.account_failover_threshold,
                "cursor": 0, "failures": [0] * len(self.ids), "pending": False, "exhausted": False}

    def _load(self):
        try:
            if self.path and self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            d = self.data
            if not isinstance(d, dict) or set(d) != {"version", "ids", "threshold", "cursor", "failures", "pending", "exhausted"}:
                raise ValueError
            if (type(d["version"]) is not int or d["version"] != 1 or not isinstance(d["ids"], list) or not 1 <= len(d["ids"]) <= 3
                    or any(not isinstance(i, str) or len(i) != 32 or any(c not in "0123456789abcdef" for c in i) for i in d["ids"])
                    or len(set(d["ids"])) != len(d["ids"])
                    or type(d["threshold"]) is not int or not 1 <= d["threshold"] <= 2147483647
                    or type(d["cursor"]) is not int or not 0 <= d["cursor"] < len(d["ids"])
                    or type(d["pending"]) is not bool or type(d["exhausted"]) is not bool
                    or not isinstance(d["failures"], list) or len(d["failures"]) != len(d["ids"])
                    or any(type(n) is not int or not 0 <= n <= d["threshold"] for n in d["failures"])):
                raise ValueError
            cursor, threshold = d["cursor"], d["threshold"]
            if any(n != threshold for n in d["failures"][:cursor]) or any(d["failures"][cursor + 1:]):
                raise ValueError
            if d["exhausted"]:
                if d["pending"] or cursor != len(d["ids"]) - 1 or any(n != threshold for n in d["failures"]):
                    raise ValueError
            elif d["failures"][cursor] >= threshold:
                raise ValueError
        except (ValueError, TypeError, OSError):
            raise PortalError("Account state invalid/unreadable; refusing authentication. Inspect account_state.json; do not delete it to force retries.") from None

    def _save(self):
        if self.path:
            try:
                write_private_json(self.path, self.data, durable=True)
            except OSError:
                raise PortalError("Cannot persist account state; authentication paused.") from None

    def _gate(self):
        if self.data["ids"] != self.ids or self.data["threshold"] != self.settings.account_failover_threshold:
            return "account_config_changed"
        if self.data["pending"]:
            return "account_outcome_unknown"
        if self.data["exhausted"]:
            return "accounts_exhausted"
        return ""

    def inspect(self):
        self._load()
        return self._gate()

    def selected(self):
        index = self.order[self.data["cursor"]]
        return index + 1, self.settings.accounts[index]

    def begin(self):
        self._load()
        if self._gate():
            raise PortalError("Account state is paused; no login request submitted.")
        # Persist an in-flight marker, not a guessed failure count. A crash in
        # the send/response window requires review instead of inventing results.
        self.data["pending"] = True
        self._save()

    def failed(self, reason="login_request_failed"):
        if not self.data["pending"]:
            raise PortalError("Cannot count an account failure without a submitted login request.")
        slot, _ = self.selected()
        cursor = self.data["cursor"]
        self.data["pending"] = False
        self.data["failures"][cursor] += 1
        count = self.data["failures"][cursor]
        state = "account_login_failed"
        if count >= self.data["threshold"]:
            if cursor + 1 < len(self.ids):
                self.data["cursor"] += 1
                state = "account_switched"
            else:
                self.data["exhausted"] = True
                state = "accounts_exhausted"
        self._save()
        if reason not in ("login_request_failed", "gateway_rejected", "network_error", "portal_response_error"):
            reason = "login_request_failed"
        logging.warning("Account slot %s: %s; failed submitted login count=%s/%s", slot, reason, count, self.data["threshold"])
        if state == "account_switched":
            logging.warning("Next authentication will use account slot %s; global rate limits retained.", self.selected()[0])
        elif state == "accounts_exhausted":
            logging.error("All configured accounts reached the failure threshold; automatic authentication paused. Review configuration, then run reset-accounts.")
        return state

    def succeeded(self):
        if not self.data["pending"]:
            raise PortalError("Cannot confirm account login without a submitted request.")
        self.data = self._blank()
        self._save()

    def reset(self):
        self.data = self._blank()
        self._save()

    def summary(self):
        try:
            self._load()
            state = self._gate()
            if state == "account_config_changed":
                return {"account_status": state}
            counts = [0] * len(self.ids)
            for priority, slot in enumerate(self.order):
                counts[slot] = self.data["failures"][priority]
            return {"account_status": state or "ready", "account_count": len(self.ids),
                    "primary_account": self.settings.primary_account, "next_login_account": self.selected()[0],
                    "account_failure_counts": counts, "account_failover_threshold": self.data["threshold"]}
        except PortalError:
            return {"account_status": "account_state_error"}
