from __future__ import annotations

from dataclasses import dataclass
import http.client
from http.cookiejar import CookieJar
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (HTTPHandler, HTTPRedirectHandler, HTTPSHandler, HTTPCookieProcessor,
                            ProxyHandler, Request, build_opener)

from .config import Settings


class NetworkError(RuntimeError):
    pass


@dataclass
class Response:
    status: int
    url: str
    headers: object
    body: bytes

    def text(self) -> str:
        charset = self.headers.get_content_charset() if hasattr(self.headers, "get_content_charset") else None
        for encoding in (charset, "utf-8-sig", "gb18030"):
            if encoding:
                try:
                    return self.body.decode(encoding)
                except (UnicodeError, LookupError):
                    pass
        return self.body.decode("utf-8", errors="replace")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    """Direct, bounded requests. No automatic redirect or environment/system proxy."""

    def __init__(self, settings: Settings):
        self.settings = settings
        host = urlsplit(settings.portal_url).hostname
        context = ssl.create_default_context(cafile=settings.ca_file or None)
        # This JNU gateway negotiates TLS 1.2 AES128-SHA256 (static RSA).
        # Keep certificate/hostname validation and the OpenSSL security level.
        context.set_ciphers("DEFAULT")

        def connect(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **kwargs):
            target_host, port = address
            if target_host.lower() == host.lower() and settings.gateway_ip:
                target_host = settings.gateway_ip
            source = (settings.source_address, 0) if settings.source_address else source_address
            # The HTTPSConnection host remains the DNS name, retaining SNI and certificate checks.
            return socket.create_connection((target_host, port), timeout, source, **kwargs)

        class DirectHTTP(HTTPHandler):
            def http_open(self, req):
                def factory(*args, **kwargs):
                    connection = http.client.HTTPConnection(*args, **kwargs)
                    connection._create_connection = connect
                    return connection
                return self.do_open(factory, req)

        class DirectHTTPS(HTTPSHandler):
            def https_open(self, req):
                def factory(*args, **kwargs):
                    connection = http.client.HTTPSConnection(*args, **kwargs)
                    connection._create_connection = connect
                    return connection
                return self.do_open(factory, req, context=context)

        self.opener = build_opener(ProxyHandler({}), DirectHTTP(), DirectHTTPS(), NoRedirect(), HTTPCookieProcessor(CookieJar()))

    def request(self, url: str, data: bytes | None = None, *, referer: str = "") -> Response:
        headers = {"User-Agent": "JNU-Network-Keeper/1.0", "Accept": "*/*", "Accept-Encoding": "identity"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        if referer:
            headers["Referer"] = referer
            parsed = urlsplit(referer)
            headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        request = Request(url, data=data, headers=headers)
        try:
            try:
                response = self.opener.open(request, timeout=self.settings.timeout)
            except HTTPError as exc:
                response = exc
            with response:
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise NetworkError("Response exceeds the 1 MiB limit.")
                return Response(response.code, response.geturl(), response.headers, body)
        except (URLError, OSError, http.client.HTTPException) as exc:
            # URLs and exception messages can contain session IDs. Expose only the type.
            reason = getattr(exc, "reason", exc)
            raise NetworkError(f"Network request failed ({type(reason).__name__}); check campus connection, DNS, proxy/TUN and TLS.") from None
