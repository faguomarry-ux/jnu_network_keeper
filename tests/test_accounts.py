import contextlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote

from jnu_network_keeper.accounts import AccountPool
from jnu_network_keeper.auth_guard import AuthGuard
from jnu_network_keeper.cli import main
from jnu_network_keeper.config import Account, Settings, load_settings, save_settings, write_private_json
from jnu_network_keeper.keeper import Keeper
from jnu_network_keeper.portal import Context, PortalClient, PortalError, Session
from jnu_network_keeper.transport import NetworkError
from test_keeper import BASE, LOGIN, PAGE, response


SECRETS = ["fake-primary-secret", "fake-backup-one-secret", "fake-backup-two-secret"]


def settings(threshold=100000, primary=1):
    return Settings(accounts=[Account(f"fake-account-{i + 1}", secret) for i, secret in enumerate(SECRETS)],
                    primary_account=primary, account_failover_threshold=threshold)


class AccountConfigTests(unittest.TestCase):
    def test_list_roundtrip_protects_every_password_and_selects_primary(self):
        value = settings(primary=2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_settings(path, value)
            with patch.dict(os.environ, {}, clear=True):
                loaded = load_settings(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("username", payload)
            self.assertNotIn("password", payload)
            self.assertEqual([a.password for a in loaded.accounts], SECRETS)
            self.assertEqual([a.id for a in loaded.accounts], [a.id for a in value.accounts])
            self.assertEqual(loaded.username, "fake-account-2")
            self.assertEqual(loaded.password, SECRETS[1])
            for secret in SECRETS:
                self.assertNotIn(secret, repr(value))
                if os.name == "nt":
                    self.assertNotIn(secret, path.read_text())
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_default_and_invalid_configuration(self):
        self.assertEqual(Settings().account_failover_threshold, 100000)
        for value in (0, -1, True, 1.5, 2147483648):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(account_failover_threshold=value).validate()
        for value in (0, 4, True):
            with self.assertRaises(ValueError):
                settings(primary=value).validate()
        value = settings()
        value.accounts[1].id = value.accounts[0].id
        with self.assertRaises(ValueError):
            value.validate()
        value = settings()
        value.accounts[1].username = value.accounts[0].username
        with self.assertRaises(ValueError):
            value.validate()

    def test_ambiguous_environment_credentials_refused_but_common_service_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_settings(path, settings())
            with patch.dict(os.environ, {"JNU_NET_USERNAME": "other"}, clear=True), self.assertRaises(ValueError):
                load_settings(path)
            with patch.dict(os.environ, {"JNU_NET_SERVICE": "common"}, clear=True):
                self.assertEqual(load_settings(path).service, "common")
            with patch.dict(os.environ, {"JNU_NET_PASSWORD": "ignored-by-diagnostic"}, clear=True):
                self.assertEqual(load_settings(path, use_environment=False).password, SECRETS[0])

    def test_missing_identity_in_handwritten_list_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"accounts": [{"username": "fake", "password": {"scheme": "plain", "value": "fake"}}]}))
            if os.name != "nt":
                path.chmod(0o600)
            with self.assertRaises(ValueError):
                load_settings(path, use_environment=False)


class AccountFailoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "account_state.json"
        self.value = settings(threshold=2)
        self.pool = AccountPool(self.value, self.path)
        self.pool.reset()
        self.now = 10000
        self.guard = AuthGuard(Path(self.temp.name) / "auth_guard.json", clock=lambda: self.now)
        self.wire = Mock()
        self.wire.request.return_value = response(BASE, body=b'{"result":"fail","message":"account policy"}')
        self.portal = PortalClient(self.value, self.wire)
        self.portal.discover = Mock(return_value=Context(LOGIN, PAGE))
        self.keeper = Keeper(self.value, transport=self.wire, portal=self.portal, probe=lambda: False,
                             account_pool=self.pool, auth_guard=self.guard, sleep=lambda _: None)

    def failure(self, at):
        self.now = at
        with self.assertLogs(level="WARNING") as logs:
            result = self.keeper.step()
        for secret in SECRETS:
            self.assertNotIn(secret, str(logs.output))
        for account in self.value.accounts:
            self.assertNotIn(account.username, str(logs.output))
            self.assertNotIn(account.id, str(logs.output))
        return result

    def test_exact_threshold_switches_in_order_and_all_failures_stop(self):
        self.assertEqual(self.failure(10000).state, "account_login_failed")
        self.assertEqual(self.pool.summary()["next_login_account"], 1)
        self.assertEqual(self.failure(10300).state, "account_switched")
        self.assertEqual(self.pool.summary()["next_login_account"], 2)
        self.assertEqual(self.failure(10600).state, "account_login_failed")
        self.assertEqual(self.failure(13601).state, "account_switched")
        self.assertEqual(self.pool.summary()["next_login_account"], 3)
        self.assertEqual(self.failure(13901).state, "account_login_failed")
        with self.assertLogs(level="ERROR"):
            self.now = 14201
            self.assertEqual(self.keeper.step().state, "accounts_exhausted")
        self.assertEqual(self.wire.request.call_count, 6)
        self.now = 20000
        self.assertEqual(self.keeper.step().state, "accounts_exhausted")
        self.assertEqual(self.wire.request.call_count, 6)
        self.assertEqual(self.pool.summary()["account_failure_counts"], [2, 2, 2])
        posted_users = [unquote(parse_qs(call.args[1].decode())["userId"][0]) for call in self.wire.request.call_args_list]
        self.assertEqual(posted_users, ["fake-account-1"] * 2 + ["fake-account-2"] * 2 + ["fake-account-3"] * 2)
        self.assertEqual(AccountPool(self.value, self.path).inspect(), "accounts_exhausted")

    def test_failover_does_not_bypass_shared_minimum_or_hourly_limit(self):
        self.failure(10000)
        self.assertEqual(self.keeper.step().state, "auth_cooldown")
        self.assertEqual(self.wire.request.call_count, 1)
        self.failure(10300)
        # A third account remains subject to the SAME guard, not a fresh budget.
        self.assertEqual(self.keeper.step().state, "auth_cooldown")
        self.assertEqual(self.wire.request.call_count, 2)
        self.failure(10600)
        self.now = 10900
        self.assertEqual(self.keeper.step().state, "auth_cooldown")
        self.assertEqual(self.wire.request.call_count, 3)
        self.assertFalse(self.guard.data["blocked"])

    def test_default_100000_boundary_uses_exactly_one_more_submitted_failure(self):
        self.value.account_failover_threshold = 100000
        self.pool.reset()
        data = json.loads(self.path.read_text())
        data["failures"][0] = 99999
        self.path.write_text(json.dumps(data))
        self.assertEqual(self.failure(10000).state, "account_switched")
        self.assertEqual(self.wire.request.call_count, 1)
        self.assertEqual(self.pool.summary()["account_failure_counts"], [100000, 0, 0])

    def test_preflight_and_discovery_failures_do_not_count(self):
        self.portal.discover.return_value = Context(LOGIN, "not a login page")
        before = self.path.read_bytes()
        with self.assertRaises(PortalError):
            self.keeper.step()
        self.wire.request.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.now = 10300
        self.portal.discover.side_effect = NetworkError("offline discovery")
        with self.assertRaises(NetworkError):
            self.keeper.step()
        self.assertEqual(self.path.read_bytes(), before)

    def test_online_checks_local_link_failures_and_authenticated_outage_do_not_count(self):
        before = self.path.read_bytes()
        self.keeper.probe = lambda: True
        self.keeper.step()
        self.keeper.probe = lambda: False
        self.portal.discover.return_value = Context(BASE, "", Session("already-online"))
        self.keeper.step()
        self.keeper.link_check = lambda: False
        self.keeper.step()
        self.assertEqual(self.path.read_bytes(), before)
        self.wire.request.assert_not_called()

    def test_sent_timeout_and_malformed_response_count_as_failures(self):
        self.wire.request.side_effect = NetworkError("timeout")
        self.failure(10000)
        self.assertEqual(self.pool.summary()["account_failure_counts"], [1, 0, 0])
        self.wire.request.side_effect = None
        self.wire.request.return_value = response(BASE, body=b"not json")
        self.assertEqual(self.failure(10300).state, "account_switched")

    def test_success_on_backup_resets_cycle_and_next_login_prefers_primary(self):
        self.failure(10000)
        self.failure(10300)
        self.now = 10600
        self.wire.request.return_value = response(BASE, body=b'{"result":"success","userIndex":"fake-session"}')
        self.assertEqual(self.keeper.step().state, "portal_online_internet_unreachable")
        self.assertEqual(self.value.username, "fake-account-2")  # Successful request used backup.
        self.assertEqual(self.pool.summary()["next_login_account"], 1)
        self.assertEqual(self.pool.summary()["account_failure_counts"], [0, 0, 0])

    def test_restart_preserves_counts_and_selected_backup(self):
        self.failure(10000)
        new = AccountPool(self.value, self.path)
        self.assertEqual(new.summary()["account_failure_counts"], [1, 0, 0])
        self.failure(10300)
        self.assertEqual(AccountPool(self.value, self.path).summary()["next_login_account"], 2)
        contents = self.path.read_text()
        self.assertTrue(all(secret not in contents for secret in SECRETS))
        self.assertTrue(all(account.username not in contents for account in self.value.accounts))

    def test_unfinished_request_pauses_without_guessing_failure_or_resending(self):
        self.pool.begin()
        self.assertEqual(AccountPool(self.value, self.path).inspect(), "account_outcome_unknown")
        self.assertEqual(self.keeper.step().state, "account_outcome_unknown")
        self.wire.request.assert_not_called()
        self.assertEqual(self.pool.summary()["account_failure_counts"], [0, 0, 0])

    def test_failed_persistence_before_submit_sends_nothing(self):
        with patch("jnu_network_keeper.accounts.write_private_json", side_effect=OSError), self.assertRaises(PortalError):
            self.keeper.step()
        self.wire.request.assert_not_called()

    def test_commit_error_after_success_does_not_invent_a_login_failure(self):
        self.wire.request.return_value = response(BASE, body=b'{"result":"success","userIndex":"fake-session"}')
        calls = 0
        def persist_then_fail(path, data, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError
            write_private_json(path, data, **kwargs)
        with patch("jnu_network_keeper.accounts.write_private_json", side_effect=persist_then_fail), self.assertRaises(PortalError):
            self.keeper.step()
        self.assertEqual(self.wire.request.call_count, 1)
        self.assertEqual(self.pool.data["failures"], [0, 0, 0])
        self.assertEqual(AccountPool(self.value, self.path).inspect(), "account_outcome_unknown")

    def test_corruption_or_changed_configuration_refuses_authentication(self):
        self.path.write_text("invalid")
        with self.assertRaises(PortalError):
            self.keeper.step()
        self.wire.request.assert_not_called()
        self.pool.reset()
        self.value.account_failover_threshold = 4
        self.assertEqual(self.keeper.step().state, "account_config_changed")
        self.wire.request.assert_not_called()

    def test_nonfirst_primary_has_predictable_fallback_order(self):
        value = settings(threshold=1, primary=2)
        pool = AccountPool(value)
        for expected in (2, 1, 3):
            self.assertEqual(pool.selected()[0], expected)
            pool.begin()
            with self.assertLogs(level="WARNING"):
                pool.failed()
        self.assertEqual(pool.inspect(), "accounts_exhausted")


class AccountCliTests(unittest.TestCase):
    def test_normal_init_preserves_backups_and_counts_when_credentials_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "config.json"
            value = settings(threshold=2)
            save_settings(path, value)
            state_path = path.parent / "account_state.json"
            pool = AccountPool(value, state_path)
            pool.begin()
            with self.assertLogs(level="WARNING"):
                pool.failed()
            before = state_path.read_bytes()
            with patch("builtins.input", return_value=""), patch("getpass.getpass", return_value=""):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["init", "--config", str(path)]), 0)
            loaded = load_settings(path)
            self.assertEqual([a.password for a in loaded.accounts], SECRETS)
            self.assertEqual(state_path.read_bytes(), before)

    def test_interactive_three_accounts_and_readonly_listing(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "config.json"
            # Count, primary, then each username and optional service. Threshold is a CLI option.
            inputs = ["3", "1", "fake-account-1", "", "fake-account-2", "", "fake-account-3", ""]
            with patch("builtins.input", side_effect=inputs), patch("getpass.getpass", side_effect=SECRETS):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["init-accounts", "--config", str(path), "--failover-threshold", "100000"]), 0)
            saved = load_settings(path)
            self.assertEqual(len(saved.accounts), 3)
            account_state = path.parent / "account_state.json"
            before = account_state.read_bytes(), account_state.stat().st_mtime_ns
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["accounts", "--config", str(path)]), 0)
            for secret in SECRETS:
                self.assertNotIn(secret, output.getvalue())
            self.assertNotIn("fake-account-1", output.getvalue())
            self.assertEqual((account_state.read_bytes(), account_state.stat().st_mtime_ns), before)

    def test_reset_accounts_keeps_global_attempt_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            value = settings(threshold=1)
            save_settings(path, value)
            pool = AccountPool(value, path.parent / "account_state.json")
            pool.begin()
            with self.assertLogs(level="WARNING"):
                pool.failed()
            guard_path = path.parent / "auth_guard.json"
            AuthGuard(guard_path).reserve()
            before = guard_path.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(main(["reset-accounts", "--config", str(path)]), 0)
            self.assertEqual(guard_path.read_bytes(), before)
            self.assertEqual(AccountPool(value, path.parent / "account_state.json").summary()["next_login_account"], 1)


if __name__ == "__main__":
    unittest.main()
