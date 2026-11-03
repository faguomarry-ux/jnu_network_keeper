import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from jnu_network_keeper.auth_guard import AuthGuard
from jnu_network_keeper.config import Account, Settings, save_settings
from jnu_network_keeper.accounts import AccountPool
from jnu_network_keeper import diagnostics as d
from jnu_network_keeper.portal import Context, Session


SECRET = "dummy-sensitive-password-never-printed"
PROFILE = "12345678-1234-1234-1234-123456789abc"


def completed(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config = self.folder / "config.json"
        save_settings(self.config, Settings(username="dummy-student-id", password=SECRET))

    def test_auth_inspection_never_consumes_or_changes_attempts(self):
        path = self.folder / "auth_guard.json"
        guard = AuthGuard(path, clock=lambda: 10000)
        self.assertEqual(guard.inspect(), ("", 0))
        self.assertFalse(path.exists())
        guard.reserve()
        before = path.read_bytes(), path.stat().st_mtime_ns
        for _ in range(3):
            self.assertEqual(guard.inspect(), ("auth_cooldown", 300))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_parse_installed_unit_with_spaces_and_literal_percent(self):
        path = self.folder / "unit.service"
        config = str(self.folder / "folder with spaces" / "100%config.json")
        args = ["/usr/bin/python3", "-m", "jnu_network_keeper", "run", "--config", config,
                "--interval", "120", "--jitter", "180", "--follow-dhcp"]
        path.write_text("[Service]\nExecStart=" + " ".join(json.dumps(v).replace("%", "%%") for v in args) + "\n")
        options = d.installed_options({"FragmentPath": str(path)})
        self.assertEqual(options["config"], config)
        self.assertEqual(options["interval"], 120)
        self.assertEqual(options["interval_jitter"], 180)
        self.assertTrue(options["follow_dhcp"])
        self.assertIsNone(d.installed_options({"FragmentPath": str(path), "DropInPaths": "custom.conf"}))
        path.write_text("[Service]\nExecStart=/other/program --secret do-not-print\n")
        self.assertIsNone(d.installed_options({"FragmentPath": str(path)}))

    def test_config_uses_effective_schedule_without_writing_or_printing_secrets(self):
        path = self.folder / "auth_guard.json"
        AuthGuard(path).reserve()
        before = {file: file.read_bytes() for file in (self.config, path)}
        options = {"config": str(self.config), "python": __import__("sys").executable,
                   "interval": 120, "interval_jitter": 180, "follow_dhcp": True}
        report = d.Report()
        settings = d.check_config(report, self.config, {"LoadState": "loaded"}, options)
        self.assertEqual((settings.interval, settings.interval_jitter, settings.follow_dhcp), (120, 180, True))
        for file, data in before.items():
            self.assertEqual(file.read_bytes(), data)
        self.assertNotIn(SECRET, report.render())
        self.assertNotIn("dummy-student-id", json.dumps(report.payload()))
        self.assertEqual(next(row for row in report.checks if row["check"] == "auth_limits")["status"], "WARN")

    def test_interactive_environment_cannot_mask_missing_saved_credentials(self):
        save_settings(self.config, Settings())
        report = d.Report()
        with patch.dict(os.environ, {"JNU_NET_USERNAME": "environment-user", "JNU_NET_PASSWORD": SECRET}):
            settings = d.check_config(report, self.config, {}, None)
        self.assertEqual(settings.username, "")
        self.assertEqual(settings.password, "")
        self.assertEqual(next(row for row in report.checks if row["check"] == "credentials")["status"], "FAIL")
        self.assertNotIn(SECRET, report.render())

    def test_account_diagnostics_are_read_only_and_exhaustion_is_visible(self):
        settings = Settings(accounts=[Account("private-fake-user", SECRET)], account_failover_threshold=1)
        save_settings(self.config, settings)
        state_path = self.folder / "account_state.json"
        pool = AccountPool(settings, state_path)
        pool.begin()
        with self.assertLogs(level="WARNING"):
            pool.failed()
        before = state_path.read_bytes()
        report = d.Report()
        d.check_config(report, self.config, {}, None)
        self.assertEqual(next(row for row in report.checks if row["check"] == "account_pool")["status"], "WARN")
        self.assertEqual(state_path.read_bytes(), before)
        self.assertNotIn(SECRET, report.render())
        self.assertNotIn("private-fake-user", report.render())

    def test_corrupt_config_and_state_cannot_leak_raw_contents(self):
        self.config.write_text("bad-json-" + SECRET)
        report = d.Report()
        self.assertIsNone(d.check_config(report, self.config, {}, None))
        (self.folder / "state.json").write_text(json.dumps({"state": SECRET, "observed_at": datetime.now().astimezone().isoformat()}))
        d.check_recent_state(report, self.config, Settings(), True)
        self.assertNotIn(SECRET, report.render())
        self.assertEqual(report.exit_code, 2)

    def test_old_state_is_detected_with_custom_long_schedule(self):
        settings = Settings(interval=60, interval_jitter=1800)
        path = self.folder / "state.json"
        path.write_text(json.dumps({"state": "online", "observed_at": datetime.fromtimestamp(time.time() - 1200).astimezone().isoformat()}))
        report = d.Report()
        d.check_recent_state(report, self.config, settings, True)
        self.assertEqual(report.checks[0]["status"], "PASS")
        path.write_text(json.dumps({"state": "online", "observed_at": datetime.fromtimestamp(time.time() - 5000).astimezone().isoformat()}))
        report = d.Report()
        d.check_recent_state(report, self.config, settings, True)
        self.assertEqual(report.checks[0]["status"], "FAIL")

    def test_runtime_only_enable_is_not_reported_as_persistent_boot(self):
        report = d.Report()
        d.check_service(report, {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled-runtime",
                                 "Type": "notify", "Restart": "always", "WatchdogUSec": "10min"})
        self.assertEqual(next(row for row in report.checks if row["check"] == "service_boot")["status"], "WARN")

    def test_report_exit_codes_and_private_files(self):
        for status, code in (("PASS", 0), ("SKIP", 0), ("WARN", 1), ("FAIL", 2)):
            report = d.Report()
            report.add("example", status, "safe detail")
            self.assertEqual(report.exit_code, code)
        directory = report.save(self.folder)
        self.assertEqual(json.loads((directory / "report.json").read_text(encoding="utf-8"))["exit_code"], 2)
        self.assertIn("[FAIL]", (directory / "report.txt").read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / "report.json").stat().st_mode & 0o777, 0o600)

    def test_commands_are_captured_bounded_and_have_no_stdin(self):
        with patch("subprocess.run", return_value=completed(SECRET)) as run:
            self.assertEqual(d.command(["systemctl", "show"]).stdout, SECRET)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertTrue(run.call_args.kwargs["capture_output"])
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("test", 10)):
            self.assertIsNone(d.command(["systemctl", "show"]))

    def test_unit_failure_raw_output_is_not_in_report(self):
        report = d.Report()
        with patch.object(d, "command", return_value=completed(SECRET, 1, SECRET)):
            d.check_unit_tests(report)
        self.assertEqual(report.exit_code, 2)
        self.assertNotIn(SECRET, report.render())
        self.assertNotIn(SECRET, json.dumps(report.payload()))

    def test_get_only_probe_never_calls_login_heartbeat_or_guard_reserve(self):
        portal = Mock()
        portal.discover.return_value = Context("", "", Session(SECRET))
        with patch.object(d, "LinkMonitor", return_value=lambda: True), patch.object(d, "Transport"), patch.object(d, "is_online", return_value=True):
            with patch.object(d, "PortalClient", return_value=portal), patch.object(AuthGuard, "reserve", side_effect=AssertionError("must not reserve")):
                result = d.network_probe(self.config, {})
        self.assertEqual(result, {"internet": True, "portal": "authenticated", "link": True})
        portal.login.assert_not_called()
        portal.heartbeat.assert_not_called()
        self.assertNotIn(SECRET, json.dumps(result))

    def test_missing_link_skips_all_http(self):
        with patch.object(d, "LinkMonitor", return_value=lambda: False), patch.object(d, "Transport") as transport:
            result = d.network_probe(self.config, {})
        self.assertFalse(result["link"])
        transport.assert_not_called()

    def test_network_child_output_is_allowlisted_and_timeout_does_not_login(self):
        for output in (SECRET, json.dumps({"internet": SECRET}), json.dumps({"internet": True, "portal": SECRET, "link": True})):
            report = d.Report()
            with patch.object(d, "command", return_value=completed(output)):
                d.check_network(report, self.config, {})
            self.assertNotIn(SECRET, report.render())
            self.assertEqual(report.exit_code, 1)
        report = d.Report()
        with patch.object(d, "command", return_value=None) as command:
            d.check_network(report, self.config, {"follow_dhcp": True})
        self.assertEqual(command.call_args.kwargs["timeout"], 45)
        self.assertNotIn("login", command.call_args.args[0])
        self.assertIn("--follow-dhcp", command.call_args.args[0])

    def test_healthy_network_results_do_not_include_session_information(self):
        report = d.Report()
        with patch.object(d, "command", return_value=completed(json.dumps({"internet": True, "portal": "authenticated", "link": True}))):
            d.check_network(report, self.config, {})
        self.assertEqual(report.exit_code, 0)

    def test_wifi_queries_are_read_only_and_never_request_psk(self):
        from test_safety import FakeNM
        nm = FakeNM(status="100 (connected)")
        nm.fields.update({"connection.permissions": "", "connection.autoconnect": "yes",
                          "802-11-wireless-security.key-mgmt": "wpa-psk", "802-11-wireless-security.psk-flags": "0 (none)"})
        fake_config = self.folder / "wifi.json"
        fake_config.write_text(json.dumps({"interface": "wlan0", "uuid": PROFILE}))
        report = d.Report()
        with patch.object(d, "WIFI_CONFIG", fake_config), patch.object(d, "NetworkManager", return_value=nm):
            with patch.object(d, "unit_properties", return_value={"ActiveState": "active", "UnitFileState": "enabled"}):
                d.check_wifi(report)
        self.assertEqual(nm.actions, [])
        output = report.render()
        self.assertNotIn(PROFILE, output)
        self.assertNotIn("10.0.0.7", output)
        self.assertNotIn("dummy", output)

    def test_offline_main_never_runs_external_probe_or_mutating_checks(self):
        report_folder = self.folder / "reports"
        output = io.StringIO()
        options = {"config": str(self.config), "python": __import__("sys").executable}
        with patch.object(d.sys, "platform", "linux"), patch.object(d, "unit_properties", return_value={}), patch.object(d, "installed_options", return_value=options):
            with patch.object(d, "check_service"), patch.object(d, "check_wifi"), patch.object(d, "check_remote_client"):
                with patch.object(d, "command", return_value=completed()), patch.object(d, "check_network") as network:
                    with contextlib.redirect_stdout(output):
                        code = d.main(["--offline", "--skip-unit-tests", "--config", str(self.config), "--report-dir", str(report_folder)])
        network.assert_not_called()
        self.assertEqual(code, 1)  # No running daemon/state in this fixture.
        self.assertNotIn(SECRET, output.getvalue())
        self.assertFalse((self.folder / "auth_guard.json").exists())
        files = list(report_folder.glob("*/report.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn(SECRET, files[0].read_text(encoding="utf-8"))

    def test_unexpected_exception_does_not_print_traceback_or_credentials(self):
        with patch.object(d, "check_unit_tests", side_effect=RuntimeError(SECRET)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                code = d.main(["--unit-only", "--report-dir", str(self.folder)])
        self.assertEqual(code, 2)
        self.assertNotIn(SECRET, err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
