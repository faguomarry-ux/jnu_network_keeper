from email.message import Message
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote

from jnu_network_keeper.config import Settings, default_config_path, load_settings, save_settings
from jnu_network_keeper.keeper import InstanceLock, Keeper, Observation
from jnu_network_keeper.portal import (Context, PageParser, PortalClient, PortalError, Session,
                                       is_online, login_form, rsa_password)
from jnu_network_keeper.transport import NetworkError, Response, Transport


BASE = "https://webauthsa.jnu.edu.cn:8443/eportal/"
LOGIN = BASE + "index.jsp?wlanuserip=10.0.0.7&mac=aa%3Abb&nasip=10.0.0.1&url=a%2Bb"
PAGE = '<input value="false" id="passwordEncrypt"><select id="net_access_type"><option value="internet">Campus</option></select>'


def response(url, status=200, body=b"", location="", charset="utf-8"):
    headers = Message()
    headers["Content-Type"] = "text/html; charset=" + charset
    if location:
        headers["Location"] = location
    return Response(status, url, headers, body)


class ConfigTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows AppData virtualization regression")
    def test_windows_default_survives_appdata_virtualization(self):
        with patch.dict(os.environ, {"JNU_NET_CONFIG": "", "LOCALAPPDATA": "C:/one-profile"}):
            first = default_config_path()
        with patch.dict(os.environ, {"JNU_NET_CONFIG": "", "LOCALAPPDATA": "C:/packaged-app/virtualized"}):
            second = default_config_path()
        self.assertEqual(first, second)

    def test_private_roundtrip_and_no_cleartext_on_windows(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "config.json"
            settings = Settings(username="demo", password="dummy-test-secret&+%")
            save_settings(path, settings)
            loaded = load_settings(path)
            self.assertEqual(loaded.password, settings.password)
            self.assertEqual(loaded.username, "demo")
            self.assertNotIn(settings.password, repr(settings))
            if os.name == "nt":
                self.assertNotIn(settings.password, path.read_text())
            else:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_env_credentials_without_file(self):
        with patch.dict(os.environ, {"JNU_NET_USERNAME": "demo", "JNU_NET_PASSWORD": "test"}, clear=True):
            self.assertEqual(load_settings(Path("not-present.json")).username, "demo")

    def test_settings_reject_unsafe_or_invalid_values(self):
        cases = [{"portal_url": BASE.replace("https:", "http:")}, {"portal_url": BASE + "success.jsp?userIndex=token"},
                 {"interval": 0}, {"interval": 9}, {"timeout": float("nan")}, {"max_backoff": 1},
                 {"gateway_ip": "not-an-ip"}, {"password_encryption": "unknown"}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                Settings(**case).validate()

    def test_instance_lock_rejects_duplicate_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keeper.lock"
            with InstanceLock(path):
                with self.assertRaises(ValueError):
                    with InstanceLock(path):
                        pass
            with InstanceLock(path):
                pass


class ProtocolTests(unittest.TestCase):
    def test_double_encoding_preserves_special_characters(self):
        fields = {"userId": "demo+@", "password": "a&b+=% 中", "queryString": "mac=aa%3Abb&url=a%2Bb"}
        body = login_form(fields).decode()
        once = parse_qs(body, keep_blank_values=True)
        self.assertIn("%2526", body)
        self.assertEqual({key: unquote(value[0]) for key, value in once.items()}, fields)

    def test_discovery_handles_http_then_javascript_and_meta(self):
        wire = Mock()
        wire.request.side_effect = [response(BASE + "redirectortosuccess.jsp", 302, location="http://123.123.123.123/"),
                                    response("http://123.123.123.123/", body=("<script>top.self.location.href='" + LOGIN + "';</script>").encode()),
                                    response(LOGIN, body=PAGE.encode())]
        context = PortalClient(Settings(), wire).discover()
        self.assertEqual(context.url, LOGIN)
        self.assertIsNone(context.session)
        parser = PageParser()
        parser.feed('<meta content="0; URL=' + LOGIN.replace("&", "&amp;") + '" http-equiv="refresh">')
        self.assertEqual(parser.redirect, LOGIN)

    def test_success_session_is_obtained_from_device_not_hardcoded(self):
        wire = Mock()
        wire.request.return_value = response(BASE, 302, location=BASE + "./success.jsp?userIndex=test-session&keepaliveInterval=2")
        session = PortalClient(Settings(), wire).discover().session
        self.assertEqual(session.user_index, "test-session")
        self.assertEqual(session.keepalive_minutes, 2)
        self.assertEqual(wire.request.call_count, 1)

    def test_untrusted_redirect_never_receives_credentials(self):
        wire = Mock()
        wire.request.return_value = response(BASE, 302, location="https://evil.example/eportal/index.jsp?wlanuserip=10.0.0.7")
        portal = PortalClient(Settings(username="demo", password="secret"), wire)
        with self.assertRaises(PortalError):
            portal.discover()
        self.assertTrue(all(call.args[0].startswith((BASE, "http://123.123.123.123/", "http://www.msftconnecttest.com/"))
                            for call in wire.request.call_args_list))

    def test_http_gateway_redirect_is_upgraded_to_configured_https(self):
        client = PortalClient(Settings(gateway_ip="192.168.11.68"), Mock())
        self.assertEqual(client.trusted_url("http://192.168.11.68:8080/eportal/index.jsp?a=b"), BASE + "index.jsp?a=b")
        self.assertIsNone(client.trusted_url("https://webauthsa.jnu.edu.cn.evil.example/eportal/index.jsp"))
        self.assertIsNone(client.trusted_url("https://user:secret@webauthsa.jnu.edu.cn/eportal/index.jsp"))

    def test_plain_login_uses_fresh_query_and_selected_service(self):
        wire = Mock()
        wire.request.return_value = response(BASE, body=b'{"result":"success","userIndex":"new-session","keepaliveInterval":0}')
        client = PortalClient(Settings(username="demo", password="p&+%"), wire)
        session = client.login(Context(LOGIN, PAGE))
        self.assertEqual(session.user_index, "new-session")
        call = wire.request.call_args
        self.assertEqual(call.args[0], BASE + "InterFace.do?method=login")
        fields = {k: unquote(v[0]) for k, v in parse_qs(call.args[1].decode(), keep_blank_values=True).items()}
        self.assertEqual(fields["password"], "p&+%")
        self.assertEqual(fields["service"], "internet")
        self.assertEqual(fields["queryString"], LOGIN.split("?", 1)[1])
        self.assertEqual(fields["passwordEncrypt"], "false")

    def test_unknown_encryption_or_multiple_services_fails_before_post(self):
        for body in ("<html>not a login page</html>", PAGE.replace('</select>', '<option value="telecom">Other</option></select>')):
            wire = Mock()
            with self.assertRaises(PortalError):
                PortalClient(Settings(username="demo", password="secret"), wire).login(Context(LOGIN, body))
            wire.request.assert_not_called()

    def test_authentication_failure_is_redacted(self):
        wire = Mock()
        wire.request.return_value = response(BASE, body=json.dumps({"result": "fail", "message": "password secret-test-value"}).encode())
        with self.assertRaises(PortalError) as caught:
            PortalClient(Settings(username="demo", password="secret-test-value"), wire).login(Context(LOGIN, PAGE))
        self.assertNotIn("secret-test-value", str(caught.exception))

    def test_rsa_matches_independent_known_vector(self):
        # One reversed little-endian RSA block, checked against JNU's security.js.
        modulus = "c7" + "a9" * 126 + "cd"
        result = rsa_password("demo&+%", "aa:bb", "10001", modulus)
        expected = "8b904efb6f1ef11b46219e569fb2a60b251a8703bcec66b637eddc9c66a4d4d11183b0fd716f84232c9917d53f70bb4ef1bc15f560ee524d99005a365b965e5cff2c4b6d5a2f370a83628481e8aa12c6c7e36a01264c039a8712f94e595edd7efad3246bf9b92fc06358e91678839bc8cad2c2ebfb90f622c411c62e77ff4da0"
        self.assertEqual(result, expected)

    def test_rsa_login_reads_page_key_and_mac(self):
        wire = Mock()
        wire.request.return_value = response(BASE, body=b'{"result":"success","userIndex":"new-session","keepaliveInterval":1}')
        modulus = "c7" + "a9" * 126 + "cd"
        body = PAGE.replace('value="false"', 'value="true"') + f'<input id="publicKeyExponent" value="10001"><input id="publicKeyModulus" value="{modulus}">'
        PortalClient(Settings(username="demo", password="secret"), wire).login(Context(LOGIN, body))
        fields = {k: unquote(v[0]) for k, v in parse_qs(wire.request.call_args.args[1].decode(), keep_blank_values=True).items()}
        self.assertEqual(fields["password"], rsa_password("secret", "aa:bb", "10001", modulus))
        self.assertEqual(fields["passwordEncrypt"], "true")

    def test_gbk_messages_and_empty_keepalive(self):
        text = '{"message":"认证失败"}'
        self.assertEqual(response(BASE, body=text.encode("gbk"), charset="gbk").text(), text)
        wire = Mock()
        wire.request.return_value = response(BASE)
        PortalClient(Settings(), wire).heartbeat(Session("dummy"))

    def test_captcha_response_requires_browser(self):
        wire = Mock()
        wire.request.return_value = response(BASE, body=b'{"result":"fail","validCodeUrl":"captcha.jsp"}')
        with self.assertRaisesRegex(PortalError, "captcha required"):
            PortalClient(Settings(username="demo", password="test"), wire).login(Context(LOGIN, PAGE))

    def test_malformed_json_or_redirect_cannot_succeed_as_login(self):
        for result in (response(BASE, body=b'<html>login</html>'),
                       response(BASE, 302, location="https://evil.example/"),
                       response(BASE, body=b'[]')):
            wire = Mock()
            wire.request.return_value = result
            with self.assertRaises(PortalError):
                PortalClient(Settings(username="demo", password="test"), wire).login(Context(LOGIN, PAGE))

    def test_html_captive_page_does_not_count_as_online(self):
        wire = Mock()
        wire.request.return_value = response(BASE, body=b'<html>login</html>')
        self.assertFalse(is_online(wire))
        wire.request.return_value = response(BASE, body=b'Microsoft Connect Test')
        self.assertTrue(is_online(wire))


class LifecycleTests(unittest.TestCase):
    def test_online_never_sends_login(self):
        portal = Mock()
        portal.discover.return_value = Context(BASE, "", Session("dummy"))
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=lambda: True)
        self.assertTrue(keeper.step().online)
        portal.login.assert_not_called()

    def test_disconnect_discovers_then_logs_in_and_verifies(self):
        portal = Mock()
        portal.discover.return_value = Context(LOGIN, PAGE)
        portal.login.return_value = Session("new-session")
        probe = Mock(side_effect=[False, False, True])
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=probe, sleep=lambda _: None)
        self.assertEqual(keeper.step().state, "reconnected")
        portal.login.assert_called_once()
        self.assertEqual(probe.call_count, 3)

    def test_upstream_failure_does_not_resubmit_password(self):
        portal = Mock()
        portal.discover.return_value = Context(BASE, "", Session("existing"))
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=lambda: False)
        observed = keeper.step()
        self.assertEqual(observed.state, "portal_online_internet_unreachable")
        portal.login.assert_not_called()

    def test_no_query_no_login(self):
        portal = Mock()
        portal.discover.side_effect = PortalError("No redirect")
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=lambda: False)
        with self.assertRaises(PortalError):
            keeper.step()
        portal.login.assert_not_called()

    def test_zero_disables_heartbeat_and_positive_uses_minutes(self):
        portal = Mock()
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=lambda: True)
        keeper.session = Session("dummy", 0, 0)
        keeper.step()
        portal.heartbeat.assert_not_called()
        keeper.session = Session("dummy", 2, time.monotonic() - 121)
        keeper.step()
        portal.heartbeat.assert_called_once()

    def test_rediscovering_same_session_does_not_postpone_heartbeat(self):
        portal = Mock()
        portal.discover.return_value = Context(BASE, "", Session("dummy", 0))
        keeper = Keeper(Settings(), transport=Mock(), portal=portal, probe=lambda: False)
        previous_heartbeat = time.monotonic() - 60
        keeper.session = Session("dummy", 2, previous_heartbeat)
        keeper.step()
        self.assertEqual(keeper.session.last_heartbeat, previous_heartbeat)
        self.assertEqual(keeper.session.keepalive_minutes, 2)

    def test_backoff_grows_caps_and_resets_after_recovery(self):
        keeper = Keeper(Settings(interval=10, max_backoff=40), transport=Mock(), wait=lambda stop, delay: stop.wait(delay))
        keeper.step = Mock(side_effect=[PortalError("no redirect")] * 4 + [Observation("online", True, True)])

        class Stop:
            delays = []
            def is_set(self):
                return len(self.delays) == 5
            def wait(self, delay):
                self.delays.append(delay)

        stop = Stop()
        with tempfile.TemporaryDirectory() as directory, self.assertLogs(level=logging.INFO):
            path = Path(directory) / "state.json"
            keeper.run(path, stop)
            self.assertEqual(stop.delays, [10, 20, 40, 40, 10])
            self.assertEqual(json.loads(path.read_text())["state"], "online")


class TransportTests(unittest.TestCase):
    def test_local_server_redirect_not_followed(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            requests = []
            def do_GET(self):
                self.requests.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            settings = Settings(source_address="127.0.0.1")
            with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1"}):
                result = Transport(settings).request(f"http://127.0.0.1:{server.server_port}/start")
            self.assertEqual(result.status, 302)
            self.assertEqual(Handler.requests, ["/start"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
