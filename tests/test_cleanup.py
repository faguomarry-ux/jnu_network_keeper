from contextlib import ExitStack
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("cleanup_linux", Path(__file__).resolve().parents[1] / "cleanup-linux.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
PROFILE = "12345678-1234-1234-1234-123456789abc"
SECRET = "dummy-campus-secret-do-not-copy"


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class FakeSystem(c.System):
    def __init__(self, root):
        super().__init__(root)
        self.calls = []
        self.active = {unit: "active" for unit in c.UNITS}
        self.profile_exists = True
        self.original = dict(zip(c.WIFI_FIELDS, ("user:tester", "no", "-1", "-1")))
        self.current = dict(zip(c.WIFI_FIELDS, ("", "yes", "2", "1")))
        self.fail_modify = False
        self.foreground = False
        self.unknown_processes = False
        self.unknown_manager = False

    def props(self, name):
        if self.unknown_manager:
            raise c.CleanupError("unavailable")
        path = self.path("/etc/systemd/system/" + name)
        return {"LoadState": "loaded" if path.exists() else "not-found",
                "ActiveState": self.active.get(name, "inactive"),
                "User": "tester" if name == c.UNITS[2] and path.exists() else "",
                "FragmentPath": str(path) if path.exists() else "", "DropInPaths": ""}

    def processes(self):
        return int(self.foreground), self.unknown_processes

    def run(self, args, timeout=20):
        self.calls.append(args)
        code, stdout = 0, ""
        if args[:2] == ["systemctl", "stop"]:
            self.active[args[2]] = "inactive"
        elif args[0] == "nmcli":
            if "modify" in args:
                if self.fail_modify:
                    code = 1
                else:
                    values = args[args.index("uuid") + 2:]
                    self.current.update(zip(values[::2], values[1::2]))
            elif "delete" in args:
                self.profile_exists = False
            elif not self.profile_exists:
                code = 10
            else:
                field = args[args.index("-g") + 1]
                stdout = PROFILE if field == "connection.uuid" else self.current[field]
        elif args[0] == "journalctl":
            stdout = '{"MESSAGE":"' + SECRET + '"}'
        return subprocess.CompletedProcess(args, code, stdout, "")


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home/tester"
        self.home.mkdir(parents=True)
        self.project = self.root / "project"
        for name in ("README.md", "setup-linux.sh", "jnu_network_keeper/keeper.py", "jnu_network_keeper/config.py"):
            write(self.project / name, "test project marker")
        self.system = FakeSystem(self.root / "system")
        self.config = self.home / ".config/jnu_network_keeper/config.json"
        write(self.config, json.dumps({"username": "dummy-student", "password": {"scheme": "plain", "value": SECRET}}))
        write(self.config.parent / "keeper.log", SECRET)
        write(self.config.parent / "keeper.lock", "0")
        for unit in c.UNITS:
            body = ("-m jnu_network_keeper run --config " + json.dumps(str(self.config)) if unit == c.UNITS[2]
                    else ("jnu-network-guard.py recover" if unit == c.UNITS[1] else "Unit=jnu-network-guard.service"))
            write(self.system.path("/etc/systemd/system/" + unit), "[Service]\nExecStart=/usr/bin/python3 " + body)
        write(self.system.path("/usr/local/libexec/jnu-network-guard.py"), '"""Bounded NetworkManager Wi-Fi recovery"""')
        write(self.system.path("/etc/jnu-network-guard.json"), json.dumps({"interface": "wlan0", "uuid": PROFILE}))
        write(self.system.path("/etc/jnu-network-guard.backup.json"), json.dumps({"uuid": PROFILE, "original": self.system.original}))
        write(self.system.path("/var/lib/jnu-network-guard/state.json"), '{"attempts":[]}')
        write(self.home / ".local/state/jnu_network_keeper/diagnostics/test-old/report.txt", "safe report")
        self.receipt = self.root / "audit/receipt.json"

    def plan(self, *flags):
        args = c.parser().parse_args(["uninstall", "--project-dir", str(self.project), "--receipt", str(self.receipt), *flags])
        with patch.object(c, "account", return_value=self.home):
            return c.make_plan(args, self.system)

    def apply(self, plan):
        if os.name == "nt":
            with patch.object(c, "hold_lock"):
                c.apply_plan(plan, self.receipt, self.system)
        else:
            c.apply_plan(plan, self.receipt, self.system)

    def test_preview_only_reads_and_keeps_all_credentials(self):
        plan = self.plan()
        self.assertTrue(plan["targets"])
        self.assertEqual(self.system.calls, [])
        self.assertIn(SECRET, self.config.read_text())
        self.assertFalse(self.receipt.exists())

    def test_purge_restores_wifi_removes_runtime_keeps_source_and_standalone_checker(self):
        plan = self.plan()
        self.apply(plan)
        self.assertFalse(self.config.parent.exists())
        self.assertFalse((self.home / ".local/state/jnu_network_keeper").exists())
        self.assertTrue(self.project.exists())
        self.assertTrue(self.system.profile_exists)
        self.assertEqual(self.system.current, self.system.original)
        self.assertTrue((self.receipt.parent / "check-cleanup.py").is_file())
        self.assertNotIn(SECRET, self.receipt.read_text())
        self.assertNotIn(PROFILE, self.receipt.read_text())
        self.assertEqual(c.verify(plan, self.system)[0], 0)
        stop_calls = [args[2] for args in self.system.calls if args[:2] == ["systemctl", "stop"]]
        self.assertEqual(stop_calls, list(c.UNITS))
        self.assertFalse(any("restart" in args or "login" in args for args in self.system.calls))

    def test_source_deletion_preserves_external_checker_and_can_be_retried(self):
        plan = self.plan("--remove-project")
        self.apply(plan)
        self.assertFalse(self.project.exists())
        checker = self.receipt.parent / "check-cleanup.py"
        result = subprocess.run([sys.executable, "-I", str(checker), "--help"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.apply(plan)
        self.assertEqual(c.verify(c.load_receipt(self.receipt), self.system)[0], 0)

    def test_failed_wifi_rollback_keeps_backup_and_credentials_for_retry(self):
        plan = self.plan()
        self.system.fail_modify = True
        with self.assertRaises(c.CleanupError):
            self.apply(plan)
        self.assertTrue(self.config.exists())
        self.assertTrue(self.system.path("/etc/jnu-network-guard.backup.json").exists())
        self.assertNotEqual(c.verify(plan, self.system)[0], 0)
        self.system.fail_modify = False
        self.apply(plan)
        self.assertEqual(c.verify(plan, self.system)[0], 0)

    def test_foreground_or_unknown_process_prevents_data_deletion(self):
        for attribute in ("foreground", "unknown_processes"):
            setattr(self.system, attribute, True)
            with self.assertRaises(c.CleanupError):
                self.apply(self.plan())
            self.assertTrue(self.config.exists())
            setattr(self.system, attribute, False)

    def test_keep_wifi_settings_never_calls_nmcli(self):
        self.apply(self.plan("--keep-wifi-settings"))
        self.assertFalse(any(args[0] == "nmcli" for args in self.system.calls))
        self.assertTrue(self.system.profile_exists)
        self.assertEqual(self.system.current["connection.autoconnect"], "yes")

    def test_forget_wifi_deletes_only_exact_profile_and_verifies_absence(self):
        self.apply(self.plan("--forget-wifi"))
        self.assertFalse(self.system.profile_exists)
        deletion = [args for args in self.system.calls if args[0] == "nmcli" and "delete" in args]
        self.assertEqual(len(deletion), 1)
        self.assertEqual(deletion[0][-2:], ["uuid", PROFILE])

    def test_shared_config_directory_preserves_unrelated_files_and_backup(self):
        shared = self.home / "shared"
        config = shared / "server.json"
        write(config, SECRET)
        write(shared / "keeper.log", SECRET)
        write(shared / "account_state.json", '{"failures":[1]}')
        write(shared / "notes.txt", "do not delete")
        write(shared / "server.json.bak", SECRET)
        plan = self.plan("--config", str(config))
        self.apply(plan)
        self.assertFalse(config.exists())
        self.assertFalse((shared / "keeper.log").exists())
        self.assertFalse((shared / "account_state.json").exists())
        self.assertTrue((shared / "notes.txt").exists())
        self.assertTrue((shared / "server.json.bak").exists())

    def test_explicit_backups_and_known_custom_report_are_removed(self):
        report_root = self.home / "reports"
        child = report_root / "test-20261103-fixture"
        write(child / "report.json", json.dumps({"format_version": 1, "checks": []}))
        write(child / "report.txt", "safe")
        backup = self.home / "campus-backup.json"
        write(backup, SECRET)
        write(report_root / "notes.txt", "keep")
        plan = self.plan("--report-dir", str(report_root), "--extra-file", str(backup))
        self.apply(plan)
        self.assertFalse(backup.exists())
        self.assertFalse(child.exists())
        self.assertTrue((report_root / "notes.txt").exists())

    def test_report_with_other_documents_is_not_recursively_deleted(self):
        parent = self.home / "reports"
        child = parent / "test-important"
        write(child / "report.json", '{"format_version":1,"checks":[]}')
        write(child / "important.txt", "keep")
        with self.assertRaises(c.CleanupError):
            self.plan("--report-dir", str(parent))
        self.assertTrue((child / "important.txt").exists())

    def test_protected_roots_system_files_and_unverified_project_are_rejected(self):
        for path, kind in ((self.home, "tree"), (self.system.root, "tree"), (self.system.path("/etc/passwd"), "file"), (self.home / ".ssh/id_rsa", "file"), (self.home / "other", "project")):
            with self.subTest(path=path), self.assertRaises(c.CleanupError):
                c.validate_target({"path": str(path), "kind": kind}, self.home, self.project, self.system)
        with self.assertRaises(c.CleanupError):
            self.plan("--receipt", str(self.project / "inside/receipt.json"), "--remove-project")

    def test_mount_points_are_never_crossed(self):
        folder = self.home / "jnu_network_keeper"
        write(folder / "keep.txt", "keep")
        with patch.object(c.os.path, "ismount", return_value=True), self.assertRaises(c.CleanupError):
            c.remove_target({"path": str(folder), "kind": "tree"})
        self.assertTrue((folder / "keep.txt").exists())

    def test_foreign_unit_and_helper_are_not_deleted(self):
        unit = self.system.path("/etc/systemd/system/" + c.UNITS[2])
        write(unit, "[Service]\nExecStart=/other/project")
        with self.assertRaises(c.CleanupError):
            self.plan()
        self.assertTrue(unit.exists())
        self.assertTrue(self.config.exists())

    def test_checker_detects_recreated_files_and_unknown_manager(self):
        plan = self.plan()
        self.apply(plan)
        write(self.config, SECRET)
        self.assertEqual(c.verify(plan, self.system)[0], 1)
        self.system.unknown_manager = True
        self.assertEqual(c.verify(plan, self.system)[0], 2)

    def test_old_receipt_also_checks_new_account_state_in_shared_directory(self):
        shared = self.home / "custom"
        config = shared / "custom.json"
        write(config, SECRET)
        plan = self.plan("--config", str(config))
        self.apply(plan)
        old = json.loads(self.receipt.read_text())
        old["targets"] = [item for item in old["targets"] if not item["path"].endswith("account_state.json")]
        self.receipt.write_text(json.dumps(old))
        write(shared / "account_state.json", '{"failures":[1]}')
        self.assertEqual(c.verify(c.load_receipt(self.receipt), self.system)[0], 1)

    def test_receipt_can_be_removed_only_as_separate_final_action(self):
        self.apply(self.plan())
        c.forget_receipt(self.receipt)
        self.assertFalse(self.receipt.exists())
        self.assertFalse((self.receipt.parent / "check-cleanup.py").exists())

    def test_checker_does_not_forget_receipt_if_residue_exists(self):
        self.apply(self.plan())
        write(self.config, SECRET)
        # Read under the real fixture owner before simulating the CLI's root
        # gate; mocked geteuid must not mislabel an unprivileged Linux fixture.
        trusted_receipt = c.load_receipt(self.receipt)
        with patch.object(c.sys, "platform", "linux"), patch.object(c.os, "geteuid", return_value=0, create=True):
            with patch.object(c, "System", return_value=self.system), patch.object(c, "load_receipt", return_value=trusted_receipt), contextlib.redirect_stdout(io.StringIO()) as output:
                code = c.main(["check", "--receipt", str(self.receipt), "--forget-receipt"])
        self.assertEqual(code, 1)
        self.assertTrue(self.receipt.exists())
        self.assertNotIn(SECRET, output.getvalue())
        self.assertNotIn(PROFILE, output.getvalue())

    def test_cli_preview_can_extend_prior_scope_without_losing_old_targets(self):
        plan = self.plan("--keep-wifi-settings")
        self.apply(plan)
        extra = self.home / "old-backup.json"
        write(extra, SECRET)
        trusted_receipt = c.load_receipt(self.receipt)
        with patch.object(c.sys, "platform", "linux"), patch.object(c.os, "geteuid", return_value=0, create=True):
            with patch.object(c, "System", return_value=self.system), patch.object(c, "account", return_value=self.home), patch.object(c, "load_receipt", return_value=trusted_receipt):
                with patch.object(c, "apply_plan") as apply, contextlib.redirect_stdout(io.StringIO()) as output:
                    code = c.main(["uninstall", "--receipt", str(self.receipt), "--extra-file", str(extra), "--apply"])
        self.assertEqual(code, 1)  # Mocked apply leaves the explicit extra file.
        merged = apply.call_args.args[0]
        self.assertEqual(merged["wifi_mode"], "keep")
        self.assertTrue(set(item["path"] for item in plan["targets"]).issubset(item["path"] for item in merged["targets"]))
        self.assertNotIn(SECRET, output.getvalue())

    def test_service_query_failure_is_unknown_not_success(self):
        self.system.unknown_manager = True
        with patch.object(c.sys, "platform", "linux"), patch.object(c.os, "geteuid", return_value=0, create=True):
            with patch.object(c, "System", return_value=self.system), contextlib.redirect_stderr(io.StringIO()) as output:
                code = c.main(["uninstall", "--receipt", str(self.receipt), "--project-dir", str(self.project), "--apply"])
        self.assertEqual(code, 2)
        self.assertTrue(self.config.exists())
        self.assertNotIn(SECRET, output.getvalue())

    @unittest.skipIf(os.name == "nt", "Real symlink and flock protection is Linux-only")
    def test_symlink_boundary_and_real_instance_lock(self):
        external = self.root / "outside"
        write(external / "keep.txt", "must survive")
        link = self.home / "jnu_network_keeper"
        link.symlink_to(external, target_is_directory=True)
        with self.assertRaises(c.CleanupError):
            c.validate_target({"path": str(link), "kind": "tree"}, self.home, self.project, self.system)
        self.assertTrue((external / "keep.txt").exists())
        import fcntl
        lock = self.config.parent / "keeper.lock"
        with lock.open("r+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with ExitStack() as stack, self.assertRaises(c.CleanupError):
                c.hold_lock(stack, lock)
        self.assertTrue(lock.exists())


if __name__ == "__main__":
    unittest.main()
