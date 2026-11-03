import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from jnu_network_keeper.auth_guard import AuthGuard
from jnu_network_keeper.config import Settings
from jnu_network_keeper.keeper import Keeper
from jnu_network_keeper.linux_network import GuardError, NetworkManager, prepare, recover, save_json, link_ready
from jnu_network_keeper.portal import AuthenticationRejected, Context, PortalError, Session
from jnu_network_keeper.supervisor import notify, supervised_wait
from jnu_network_keeper.transport import NetworkError


PROFILE = "12345678-1234-1234-1234-123456789abc"


class AuthSafetyTests(unittest.TestCase):
    def test_budget_survives_restarts_success_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            for now in (10000, 10300, 10600):
                self.assertEqual(AuthGuard(path, clock=lambda: now).reserve(), ("", 0))
            guard = AuthGuard(path, clock=lambda: 10900)
            self.assertEqual(guard.reserve(), ("auth_cooldown", 2700))
            guard.block()
            self.assertEqual(AuthGuard(path, clock=lambda: 15000).reserve()[0], "auth_blocked")
            guard.reset_block()
            self.assertEqual(guard.reserve(), ("auth_cooldown", 2700))
            self.assertEqual(AuthGuard(path, clock=lambda: 13600).reserve(), ("", 0))

    def test_clock_reversal_and_corruption_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            AuthGuard(path, clock=lambda: 10000).reserve()
            self.assertEqual(AuthGuard(path, clock=lambda: 9999).reserve()[0], "auth_clock_error")
            for contents in ('{', '[]', '{"attempts": ["x"], "blocked": false, "updated_at": 0}'):
                path.write_text(contents)
                with self.assertRaises(PortalError):
                    AuthGuard(path).reserve()

    def test_timeout_is_counted_before_request_and_no_immediate_retry(self):
        portal = Mock()
        portal.discover.return_value = Context("https://example.org", "")
        portal.login.side_effect = NetworkError("timeout")
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=lambda: False,
                        auth_guard=AuthGuard(clock=lambda: 10000))
        with self.assertRaises(NetworkError):
            keeper.step()
        self.assertEqual(keeper.step().state, "auth_cooldown")
        portal.login.assert_called_once()

    def test_rejection_blocks_until_operator_reset(self):
        portal = Mock()
        portal.discover.return_value = Context("https://example.org", "")
        portal.login.side_effect = AuthenticationRejected("rejected")
        guard = AuthGuard(clock=lambda: 10000)
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=lambda: False, auth_guard=guard)
        with self.assertRaises(AuthenticationRejected):
            keeper.step()
        self.assertEqual(keeper.step().state, "auth_blocked")
        portal.login.assert_called_once()

    def test_failed_persistence_prevents_password_submission(self):
        portal = Mock()
        portal.discover.return_value = Context("https://example.org", "")
        with tempfile.TemporaryDirectory() as directory:
            keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=lambda: False,
                            auth_guard=AuthGuard(Path(directory) / "guard.json"))
            with patch("jnu_network_keeper.auth_guard.write_private_json", side_effect=OSError), self.assertRaises(PortalError):
                keeper.step()
            portal.login.assert_not_called()

    def test_missing_link_skips_heartbeat_probe_and_auth(self):
        portal, probe = Mock(), Mock()
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=probe, link_check=lambda: False)
        keeper.session = Session("dummy", 1, 0)
        self.assertEqual(keeper.step().state, "link_unavailable")
        self.assertIsNone(keeper.session)
        probe.assert_not_called()
        self.assertEqual(portal.mock_calls, [])


class FakeNM:
    def __init__(self, *, status="30 (disconnected)", radio="enabled", hardware="enabled", networking="enabled"):
        self.status, self.radio, self.hardware, self.networking = status, radio, hardware, networking
        self.actions = []
        self.fields = {"connection.type": "802-11-wireless", "GENERAL.TYPE": "wifi",
                       "connection.interface-name": "", "802-11-wireless.mode": "infrastructure",
                       "802-11-wireless-security.key-mgmt": "", "connection.permissions": "user:demo",
                       "connection.autoconnect": "no", "connection.autoconnect-retries": "-1",
                       "connection.auth-retries": "-1"}
        self.fields["IP4.ADDRESS"] = "10.0.0.7/24"
        self.fields["GENERAL.CON-UUID"] = PROFILE

    def field(self, name, *target, **kwargs):
        if name == "GENERAL.STATE":
            return self.status
        if name == "WIFI-HW":
            return self.hardware
        return self.fields[name]

    def run(self, *args):
        if args == ("networking",):
            return self.networking
        if args == ("radio", "wifi"):
            return self.radio
        self.actions.append(args)
        return ""


class WiFiSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "guard.json"
        self.state = Path(self.temp.name) / "state.json"
        save_json(self.config, {"interface": "wlan0", "uuid": PROFILE})

    def recover(self, nm, now=10000):
        return recover(config=self.config, state=self.state, nm=nm, clock=lambda: now)

    def test_connected_activating_and_unmanaged_devices_are_untouched(self):
        for status in ("100 (connected)", "40 (prepare)", "60 (need-auth)", "70 (ip-config)", "10 (unmanaged)"):
            nm = FakeNM(status=status)
            self.recover(nm)
            self.assertEqual(nm.actions, [])
            self.assertFalse(self.state.exists())

    def test_software_off_only_enables_radio_then_waits_for_nm(self):
        nm = FakeNM(status="20 (unavailable)", radio="disabled")
        self.assertEqual(self.recover(nm), "wifi_enabled_waiting_for_autoconnect")
        self.assertEqual(nm.actions, [("radio", "wifi", "on")])
        self.assertEqual(self.recover(nm, 10060), "wifi_cooldown")
        self.assertEqual(len(nm.actions), 1)

    def test_hardware_and_networking_off_do_not_trigger_changes(self):
        for nm in (FakeNM(hardware="disabled"), FakeNM(networking="disabled")):
            self.recover(nm)
            self.assertEqual(nm.actions, [])

    def test_exact_profile_and_device_with_persistent_attempt_budget(self):
        nm = FakeNM()
        for now in (10000, 10120, 10240):
            self.assertEqual(self.recover(nm, now), "wifi_activation_requested")
        self.assertEqual(nm.actions, [("connection", "up", "uuid", PROFILE, "ifname", "wlan0")] * 3)
        self.assertEqual(self.recover(nm, 10400), "wifi_cooldown")
        self.assertEqual(self.recover(nm, 9999), "wifi_clock_error")
        self.assertEqual(self.recover(nm, 10900), "wifi_activation_requested")

    def test_corrupt_history_and_failed_write_prevent_network_changes(self):
        nm = FakeNM()
        self.state.write_text('{"attempts": [1], "updated_at": "bad"}')
        with self.assertRaises(GuardError):
            self.recover(nm)
        self.assertEqual(nm.actions, [])
        self.state.unlink()
        with patch("jnu_network_keeper.linux_network.save_json", side_effect=OSError), self.assertRaises(OSError):
            self.recover(nm)
        self.assertEqual(nm.actions, [])

    def test_open_profile_prepares_system_connection_without_disconnect(self):
        nm = FakeNM()
        prepare("wlan0", PROFILE, path=self.config, nm=nm)
        self.assertEqual(len(nm.actions), 1)
        action = nm.actions[0]
        self.assertEqual(action[:4], ("connection", "modify", "uuid", PROFILE))
        self.assertIn("connection.permissions", action)
        self.assertIn("", action)
        backup = json.loads(self.config.with_suffix(".backup.json").read_text())
        self.assertEqual(backup["original"]["connection.permissions"], "user:demo")
        prepare("wlan0", PROFILE, path=self.config, nm=nm)
        self.assertEqual(json.loads(self.config.with_suffix(".backup.json").read_text()), backup)

    def test_desktop_keyring_psk_enterprise_ap_and_wrong_device_are_rejected(self):
        cases = ({"802-11-wireless-security.key-mgmt": "wpa-psk", "802-11-wireless-security.psk-flags": "1 (agent-owned)"},
                 {"802-11-wireless-security.key-mgmt": "wpa-psk", "802-11-wireless-security.psk-flags": "0 (none)", "802-11-wireless-security.psk": ""},
                 {"802-11-wireless-security.key-mgmt": "wpa-eap"},
                 {"802-11-wireless.mode": "ap"}, {"connection.interface-name": "wlan1"})
        for fields in cases:
            nm = FakeNM()
            nm.fields.update(fields)
            with self.assertRaises(GuardError):
                prepare("wlan0", PROFILE, path=self.config, nm=nm)
            self.assertEqual(nm.actions, [])

    def test_system_saved_psk_is_supported_without_writing_secret(self):
        nm = FakeNM()
        nm.fields.update({"802-11-wireless-security.key-mgmt": "wpa-psk", "802-11-wireless-security.psk-flags": "0 (none)",
                          "802-11-wireless-security.psk": "fake-psk-for-tests"})
        prepare("wlan0", PROFILE, path=self.config, nm=nm)
        self.assertNotIn("fake-psk-for-tests", self.config.read_text() + self.config.with_suffix(".backup.json").read_text())
        self.assertNotIn("fake-psk-for-tests", str(nm.actions))

    def test_nmcli_timeout_and_error_are_redacted_and_bounded(self):
        import subprocess
        nm = NetworkManager()
        for result in (subprocess.CompletedProcess([], 4, "private", "secret"),):
            with patch("subprocess.run", return_value=result) as run, self.assertRaises(GuardError) as error:
                nm.run("radio", "wifi")
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(run.call_args.kwargs["timeout"], 25)
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("nmcli", 25)), self.assertRaises(GuardError):
            nm.run("radio", "wifi")

    def test_bad_config_causes_no_nm_calls(self):
        nm = FakeNM()
        self.config.write_text('{"interface": "--evil", "uuid": "bad"}')
        with self.assertRaises(GuardError):
            self.recover(nm)
        self.assertEqual(nm.actions, [])


class SupervisorTests(unittest.TestCase):
    def test_long_backoff_feeds_watchdog_and_stop_is_interruptible(self):
        stop = threading.Event()
        with patch("jnu_network_keeper.supervisor.time.monotonic", side_effect=[0, 0, 30, 60, 90]), patch("jnu_network_keeper.supervisor.notify") as send:
            with patch.object(stop, "wait", side_effect=lambda seconds: stop.set() if seconds == 30 and send.call_count == 2 else None) as wait:
                supervised_wait(stop, 900)
            self.assertEqual(wait.call_count, 2)
            self.assertTrue(all(call.args == (30,) for call in wait.call_args_list))
            self.assertEqual(send.call_count, 2)

    @unittest.skipIf(os.name == "nt", "Unix domain socket notification is Linux-only")
    def test_notify_uses_unix_datagram(self):
        import socket
        with tempfile.TemporaryDirectory() as directory:
            address = str(Path(directory) / "notify")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
                server.bind(address)
                server.settimeout(1)
                with patch.dict(os.environ, {"NOTIFY_SOCKET": address}):
                    notify("READY=1\nWATCHDOG=1")
                self.assertEqual(server.recv(100), b"READY=1\nWATCHDOG=1")


if __name__ == "__main__":
    unittest.main()
