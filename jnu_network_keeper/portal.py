from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit

from .config import Settings
from .transport import NetworkError, Response, Transport


DISCOVERY_URLS = ("http://123.123.123.123/", "http://www.msftconnecttest.com/connecttest.txt")
# Exact results, not merely HTTP 200 (a captive login page also returns 200).
PROBES = (("http://www.msftconnecttest.com/connecttest.txt", 200, b"Microsoft Connect Test"),
          ("http://connect.rom.miui.com/generate_204", 204, b""))


class PortalError(RuntimeError):
    pass


class AuthenticationRejected(PortalError):
    """An explicit rejection requires operator attention, not password retries."""


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inputs = {}
        self.options = []
        self.redirect = ""
        self._service = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input":
            self.inputs[attrs.get("id") or attrs.get("name", "")] = attrs.get("value", "")
        if tag == "select":
            self._service = attrs.get("id") == "net_access_type"
        if tag == "option" and self._service and "value" in attrs:
            self.options.append((attrs["value"], "selected" in attrs))
        if tag == "meta" and attrs.get("http-equiv", "").lower() == "refresh":
            match = re.search(r"url\s*=\s*(.+)", attrs.get("content", ""), re.I)
            if match:
                self.redirect = match[1].strip(" \"'")

    def handle_endtag(self, tag):
        if tag == "select":
            self._service = False


def js_encode(value: str) -> str:
    return quote(value, safe="~()*!.'-_")


def login_form(fields: dict[str, str]) -> bytes:
    # The actual JNU login_bch.js applies encodeURIComponent twice to every field.
    return "&".join(f"{key}={js_encode(js_encode(value))}" for key, value in fields.items()).encode("ascii")


def rsa_password(password: str, mac: str, exponent: str, modulus: str) -> str:
    """JNU's RSAUtils wire format, independently implemented using Python integers."""
    try:
        e, n = int(exponent, 16), int(modulus, 16)
    except ValueError:
        raise PortalError("Invalid RSA public key on the login page.") from None
    if n.bit_length() < 512 or n.bit_length() > 8192 or e <= 1:
        raise PortalError("Invalid RSA key size or exponent on the login page.")
    chunk = 2 * ((n.bit_length() - 1) // 16)
    raw = (password + ">" + (mac or "111111111")).encode("utf-16-le", errors="surrogatepass")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)][::-1]
    # RSAUtils handles browser charCodeAt values as byte pairs. Non-byte values
    # have undefined carry behavior in the vendor implementation; fail explicitly.
    if any(unit > 255 for unit in units):
        raise PortalError("This RSA login variant requires a password using byte-range characters; use the browser for this password.")
    units.extend([0] * (-len(units) % chunk))
    output = []
    for i in range(0, len(units), chunk):
        block = int.from_bytes(bytes(units[i:i + chunk]), "little")
        encrypted = format(pow(block, e, n), "x")
        output.append(encrypted.zfill((len(encrypted) + 3) // 4 * 4))
    return " ".join(output)


@dataclass
class Session:
    user_index: str
    keepalive_minutes: float = 0
    last_heartbeat: float = 0


@dataclass
class Context:
    url: str
    body: str
    session: Session | None = None

    @property
    def query(self) -> str:
        return urlsplit(self.url).query


class PortalClient:
    def __init__(self, settings: Settings, transport: Transport):
        self.settings = settings
        self.transport = transport

    def trusted_url(self, candidate: str) -> str | None:
        parsed = urlsplit(candidate)
        base = urlsplit(self.settings.portal_url)
        if parsed.username or parsed.password or parsed.scheme not in ("http", "https"):
            return None
        allowed = {base.hostname}
        if self.settings.gateway_ip:
            allowed.add(self.settings.gateway_ip)
        if parsed.hostname not in allowed or not parsed.path.startswith("/eportal/"):
            return None
        # Some gateway redirects advertise HTTP/8080; credentials always use the
        # configured, certificate-validated HTTPS origin.
        return urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, ""))

    def _walk(self, start: str) -> Context | None:
        current = start
        allowed_discovery_hosts = {urlsplit(url).hostname for url in DISCOVERY_URLS}
        for _ in range(6):
            trusted = self.trusted_url(current)
            if trusted:
                current = trusted
                parsed = urlsplit(current)
                query = parse_qs(parsed.query)
                if parsed.path.endswith("success.jsp") and query.get("userIndex"):
                    try:
                        minutes = float(query.get("keepaliveInterval", ["0"])[0])
                        if not 0 <= minutes <= 1440:
                            minutes = 0
                    except ValueError:
                        minutes = 0
                    return Context(current, "", Session(query["userIndex"][0], minutes, time.monotonic()))
            elif urlsplit(current).hostname not in allowed_discovery_hosts:
                return None
            response = self.transport.request(current)
            if 300 <= response.status < 400:
                location = response.headers.get("Location", "")
                if not location:
                    return None
                current = urljoin(current, location)
                continue
            if response.status != 200:
                return None
            body = response.text()
            if trusted:
                query = parse_qs(urlsplit(current).query)
                if any(key in query for key in ("wlanuserip", "userip", "wlanacname")):
                    return Context(current, body)
            page = PageParser()
            page.feed(body)
            redirect = page.redirect
            if not redirect:
                match = re.search(r"(?:location(?:\.href)?\s*=\s*|location\.(?:replace|assign)\s*\(\s*)[\"']([^\"']+)", body, re.I)
                if match:
                    redirect = match[1].replace("\\/", "/").replace("\\u0026", "&")
            if not redirect:
                return None
            current = urljoin(current, unescape(redirect))
        return None

    def discover(self) -> Context:
        targets = (self.settings.portal_url + "redirectortosuccess.jsp",) + DISCOVERY_URLS
        for target in targets:
            try:
                context = self._walk(target)
                if context:
                    return context
            except NetworkError:
                continue
        raise PortalError("No current-device ePortal redirect found. Check campus Wi-Fi/Ethernet, campus DNS and proxy/TUN; do not reuse a success URL from another device.")

    def api(self, method: str, fields: dict[str, str], *, double: bool = False, referer: str = "", before_request=None) -> dict:
        data = login_form(fields) if double else urlencode(fields).encode("ascii")
        if before_request:
            before_request()
        response = self.transport.request(self.settings.portal_url + "InterFace.do?method=" + method,
                                          data, referer=referer or self.settings.portal_url)
        if response.status != 200:
            raise PortalError(f"ePortal {method}: HTTP {response.status}; no redirects followed.")
        if not response.body.strip() and method == "keepalive":
            return {}
        try:
            payload = json.loads(response.text())
        except ValueError:
            raise PortalError(f"ePortal {method} returned non-JSON content.") from None
        if not isinstance(payload, dict):
            raise PortalError(f"ePortal {method} returned an invalid JSON structure.")
        return payload

    def login(self, context: Context, *, before_request=None) -> Session:
        if context.session:
            return context.session
        if not self.settings.username or not self.settings.password:
            raise PortalError("Missing credentials. Run init, or set JNU_NET_USERNAME and JNU_NET_PASSWORD.")
        page = PageParser()
        page.feed(context.body)
        encryption = self.settings.password_encryption
        flag = page.inputs.get("passwordEncrypt", "").lower()
        if encryption == "auto" and flag not in ("true", "false"):
            raise PortalError("Login page has no recognized passwordEncrypt field. Check the actual login page; configure rsa/plain only after verifying it.")
        use_rsa = encryption == "rsa" or (encryption == "auto" and flag == "true")
        password = self.settings.password
        if use_rsa:
            query = parse_qs(context.query)
            password = rsa_password(password, query.get("mac", [""])[0],
                                    page.inputs.get("publicKeyExponent", ""), page.inputs.get("publicKeyModulus", ""))
        service = self.settings.service
        options = [option for option in page.options if option[0]]
        if not service and options:
            selected = [value for value, checked in options if checked]
            if len(selected) == 1:
                service = selected[0]
            elif len(options) == 1:
                service = options[0][0]
            else:
                raise PortalError("Multiple ePortal services found. Set service in config to the value selected in the browser.")
        payload = self.api("login", {"userId": self.settings.username, "password": password, "service": service,
                                   "queryString": context.query, "operatorPwd": "", "operatorUserId": "",
                                   "validcode": "", "passwordEncrypt": str(use_rsa).lower()},
                           double=True, referer=context.url, before_request=before_request)
        if payload.get("result") != "success" or not payload.get("userIndex"):
            # Never put server-supplied raw text (which can echo credentials) in logs.
            message = str(payload.get("message", ""))
            if any(word in message.lower() for word in ("密码", "password", "帐号", "账号", "用户不存在")):
                detail = "account/password or account policy rejected; verify credentials before retrying"
            elif payload.get("validCodeUrl") or "验证码" in message:
                detail = "captcha required; complete authentication in the browser"
            else:
                detail = "authentication rejected; inspect the browser's login message and service selection"
            raise AuthenticationRejected("ePortal " + detail + ".")
        try:
            minutes = float(payload.get("keepaliveInterval") or 0)
            if not 0 <= minutes <= 1440:
                minutes = 0
        except (ValueError, TypeError):
            minutes = 0
        return Session(str(payload["userIndex"]), minutes, time.monotonic())

    def heartbeat(self, session: Session) -> None:
        payload = self.api("keepalive", {"userIndex": session.user_index})
        if payload.get("result") in ("fail", "failed", "error"):
            raise PortalError("ePortal heartbeat rejected; rediscover the current device session.")
        session.last_heartbeat = time.monotonic()


def is_online(transport: Transport) -> bool:
    for url, expected_status, expected_body in PROBES:
        try:
            response = transport.request(url)
            if response.status == expected_status and response.body.strip() == expected_body:
                return True
        except NetworkError:
            continue
    return False
