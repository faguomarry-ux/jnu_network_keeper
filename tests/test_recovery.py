import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from jnu_network_keeper.config import Settings
from jnu_network_keeper.keeper import Keeper, Observation
from jnu_network_keeper.linux_network import GuardError, LinkMonitor, recover, save_json, usable_ipv4
from jnu_network_keeper.portal import Context, Session
from jnu_network_keeper.supervisor import supervised_wait
from jnu_network_keeper.transport import NetworkError, Transport
from test_safety import FakeNM, PROFILE


class RandomScheduleTests(unittest.TestCase):
    def test_normal_checks_cover_minute_range(self):
        settings = Settings(interval=60, interval_jitter=120)
        for fraction, expected in ((0, 60), (0.5, 120), (1, 180)):
            uniform = Mock(side_effect=lambda low, high: low + (high - low) * fraction)
            keeper = Keeper(settings, transport=Mock(), uniform=uniform)
            self.assertEqual(keeper.next_delay(0), expected)
            uniform.assert_called_once_with(0, 120)

    def test_failure_jitter_never_shortens_backoff_or_exceeds_cap(self):
        keeper = Keeper(Settings(interval=60, interval_jitter=120), transport=Mock(), uniform=lambda low, high: high)
        self.assertEqual([keeper.next_delay(n) for n in range(1, 7)], [75, 150, 300, 600, 900, 900])

    def test_server_heartbeat_deadline_is_not_randomized(self):
        keeper = Keeper(Settings(interval_jitter=120), transport=Mock(), uniform=lambda low, high: high)
        keeper.session = Session("fake", 1, 100)
        with patch("jnu_network_keeper.keeper.time.monotonic", return_value=150):
            self.assertEqual(keeper.next_delay(0), 10)
            self.assertEqual(keeper.next_delay(6), 10)

    def test_disabling_jitter_preserves_original_schedule(self):
        uniform = Mock()
        keeper = Keeper(Settings(interval=60, interval_jitter=0), transport=Mock(), uniform=uniform)
        self.assertEqual(keeper.next_delay(0), 60)
        self.assertEqual(keeper.next_delay(3), 240)
        uniform.assert_not_called()

    def test_invalid_randomness_and_dhcp_options_are_rejected(self):
        for value in (-1, 3601, True, float("nan"), float("inf"), "120"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(interval_jitter=value).validate()
        with self.assertRaises(ValueError):
            Settings(follow_dhcp="true").validate()

    def test_state_records_the_chosen_next_check(self):
        stop = threading.Event()
        keeper = Keeper(Settings(interval_jitter=120), transport=Mock(),
                        uniform=lambda low, high: high, wait=lambda stop, delay: stop.set())
        keeper.step = Mock(return_value=Observation("online", True, True))
        with tempfile.TemporaryDirectory() as directory, self.assertLogs(level="INFO"):
            path = Path(directory) / "state.json"
            keeper.run(path, stop)
            data = json.loads(path.read_text())
        self.assertEqual(data["next_check_in"], 180)
        self.assertIn("next_check_at", data)


class LinkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "guard.json"
        save_json(self.config, {"interface": "wlan0", "uuid": PROFILE})
        self.nm = FakeNM(status="100 (connected)")
        self.nm.fields["GENERAL.CON-UUID"] = PROFILE
        platform = patch("jnu_network_keeper.linux_network.sys.platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)

    def test_dhcp_follow_replaces_expired_source_binding_only_in_memory(self):
        settings = Settings(source_address="10.0.0.6", follow_dhcp=True)
        before = self.config.read_text()
        monitor = LinkMonitor(settings, config=self.config, nm=self.nm)
        with self.assertLogs(level="INFO"):
            self.assertTrue(monitor())
            self.assertEqual(settings.source_address, "10.0.0.7")
            generation = monitor.generation
            self.assertTrue(monitor())
            self.assertEqual(monitor.generation, generation)
            self.nm.fields["IP4.ADDRESS"] = "10.0.0.8/24"
            self.assertTrue(monitor())
        self.assertEqual(settings.source_address, "10.0.0.8")
        self.assertEqual(monitor.generation, generation + 1)
        self.assertEqual(self.config.read_text(), before)
        self.assertEqual(self.nm.actions, [])

    def test_dhcp_follow_is_opt_in_and_preserves_a_still_valid_address(self):
        settings = Settings(source_address="10.0.0.99")
        monitor = LinkMonitor(settings, config=self.config, nm=self.nm)
        self.assertTrue(monitor())
        self.assertEqual(settings.source_address, "10.0.0.99")
        settings.follow_dhcp = True
        self.nm.fields["IP4.ADDRESS"] = "10.0.0.7/24\n10.0.0.99/24"
        self.assertTrue(monitor())
        self.assertEqual(settings.source_address, "10.0.0.99")

    def test_wrong_profile_missing_ip_and_bad_network_state_do_not_bind(self):
        settings = Settings(source_address="10.0.0.6", follow_dhcp=True)
        monitor = LinkMonitor(settings, config=self.config, nm=self.nm)
        self.nm.fields["GENERAL.CON-UUID"] = "other-profile"
        self.assertFalse(monitor())
        self.nm.fields["GENERAL.CON-UUID"] = PROFILE
        self.nm.fields["IP4.ADDRESS"] = "169.254.2.3/16\n::1/128\nbad"
        self.assertFalse(monitor())
        self.nm.status = "70 (ip-config)"
        self.assertFalse(monitor())
        self.assertEqual(settings.source_address, "10.0.0.6")
        self.assertEqual(self.nm.actions, [])

    def test_missing_guard_with_follow_enabled_defers_without_crash_loop(self):
        self.config.unlink()
        monitor = LinkMonitor(Settings(follow_dhcp=True), config=self.config, nm=self.nm)
        with self.assertLogs(level="WARNING") as logs:
            self.assertFalse(monitor())
            self.assertFalse(monitor())
        self.assertEqual(len(logs.output), 1)

    def test_read_error_defers_and_subsequent_recovery_changes_generation(self):
        monitor = LinkMonitor(config=self.config, nm=self.nm)
        with patch.object(self.nm, "field", side_effect=GuardError("failed")):
            self.assertFalse(monitor())
        generation = monitor.generation
        self.assertTrue(monitor())
        self.assertEqual(monitor.generation, generation + 1)

    def test_address_change_invalidates_cached_session_before_heartbeat(self):
        monitor = LinkMonitor(config=self.config, nm=self.nm)
        portal = Mock()
        portal.discover.side_effect = [Context("", "", Session("old", 1, 0)), Context("", "", Session("new", 1, 10000))]
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=lambda: True, link_check=monitor)
        self.assertTrue(keeper.step().online)
        self.nm.fields["IP4.ADDRESS"] = "10.0.0.8/24"
        self.assertTrue(keeper.step().online)
        self.assertEqual(keeper.session.user_index, "new")
        portal.heartbeat.assert_not_called()
        portal.login.assert_not_called()

    def test_local_changes_wake_a_long_wait_without_http_or_wifi_mutation(self):
        monitor = LinkMonitor(config=self.config, nm=self.nm)
        monitor()
        portal, probe = Mock(), Mock()
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=probe, link_check=monitor)
        stop = threading.Event()
        def wait(seconds):
            self.nm.fields["IP4.ADDRESS"] = "10.0.0.8/24"
        with patch.object(stop, "wait", side_effect=wait) as wait_mock:
            with patch("jnu_network_keeper.supervisor.notify"):
                keeper.wait(stop, 900)
        wait_mock.assert_called_once_with(30)
        probe.assert_not_called()
        self.assertEqual(portal.mock_calls, [])
        self.assertEqual(self.nm.actions, [])

    def test_connected_without_ipv4_reports_problem_without_disconnecting(self):
        self.nm.fields["IP4.ADDRESS"] = ""
        result = recover(config=self.config, state=Path(self.temp.name) / "state.json", nm=self.nm)
        self.assertEqual(result, "wifi_connected_without_ipv4")
        self.assertEqual(self.nm.actions, [])

    def test_address_parser_filters_unusable_and_duplicate_addresses(self):
        self.assertEqual(usable_ipv4("127.0.0.1/8\n0.0.0.0/8\n224.0.0.1/4\n169.254.2.3/16\n10.2.3.4/24\n10.2.3.4/24"), ("10.2.3.4",))


class HeartbeatRecoveryTests(unittest.TestCase):
    def test_runtime_source_change_reaches_transport_without_restart(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(self.client_address[0].encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            settings = Settings(source_address="127.0.0.1")
            transport = Transport(settings)
            url = f"http://127.0.0.1:{server.server_port}/"
            self.assertEqual(transport.request(url).body, b"127.0.0.1")
            settings.source_address = "127.0.0.2"
            self.assertEqual(transport.request(url).body, b"127.0.0.2")
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_heartbeat_error_does_not_misreport_working_internet_or_login(self):
        portal = Mock()
        portal.heartbeat.side_effect = NetworkError("timeout")
        portal.discover.return_value = Context("", "", Session("fresh"))
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=lambda: True)
        keeper.session = Session("expired", 1, 0)
        with self.assertLogs(level="WARNING"):
            self.assertTrue(keeper.step().online)
        self.assertEqual(keeper.session.user_index, "fresh")
        portal.login.assert_not_called()

    def test_heartbeat_error_can_recover_and_authenticate_in_same_round(self):
        portal = Mock()
        portal.heartbeat.side_effect = NetworkError("expired")
        portal.discover.return_value = Context("", "")
        portal.login.return_value = Session("fresh")
        keeper = Keeper(Settings(), transport=Mock(), portal=portal,
                        probe=Mock(side_effect=[False, True]), sleep=lambda _: None)
        keeper.session = Session("expired", 1, 0)
        with self.assertLogs(level="WARNING"):
            self.assertEqual(keeper.step().state, "reconnected")
        portal.login.assert_called_once()

    def test_link_event_does_not_bypass_authentication_cooldown(self):
        from jnu_network_keeper.auth_guard import AuthGuard
        class Link:
            generation = 1
            def __call__(self):
                return True
        link = Link()
        guard = AuthGuard(clock=lambda: 10000)
        guard.reserve()
        portal = Mock()
        portal.discover.return_value = Context("", "")
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=lambda: False,
                        link_check=link, auth_guard=guard)
        self.assertEqual(keeper.step().state, "auth_cooldown")
        link.generation += 1
        self.assertEqual(keeper.step().state, "auth_cooldown")
        portal.login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
