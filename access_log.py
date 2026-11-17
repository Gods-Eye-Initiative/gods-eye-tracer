"""Terminal access logs for GET's Werkzeug listeners."""

import ipaddress
import logging
import os
import secrets
import sys
from datetime import datetime
from time import perf_counter

from werkzeug.serving import WSGIRequestHandler

REQUEST_ID_KEY = "get.request_id"
access_log = logging.getLogger("get.access")

STATUS_STYLES = {
    "1": ("INFO", "36", logging.INFO),
    "2": ("OK", "32", logging.INFO),
    "3": ("REDIRECT", "36", logging.INFO),
    "4": ("CLIENT_ERROR", "33", logging.WARNING),
    "5": ("SERVER_ERROR", "31", logging.ERROR),
}

BROWSERS = (
    ("edg/", "Edge"),
    ("edga/", "Edge"),
    ("edgios/", "Edge"),
    ("edge/", "Edge"),
    ("opr/", "Opera"),
    ("samsungbrowser/", "Samsung Internet"),
    ("firefox/", "Firefox"),
    ("fxios/", "Firefox"),
    ("chrome/", "Chrome"),
    ("crios/", "Chrome"),
    ("chromium/", "Chromium"),
    ("safari/", "Safari"),
    ("curl/", "curl"),
    ("python-requests/", "Requests"),
)


def supports_color(stream):
    if "NO_COLOR" in os.environ or not stream.isatty():
        return False
    if os.name != "nt":
        return os.environ.get("TERM") != "dumb"

    import ctypes
    import msvcrt
    from ctypes import wintypes

    handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
    mode = wintypes.DWORD()
    kernel = ctypes.windll.kernel32
    return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode))
                and kernel.SetConsoleMode(handle, wintypes.DWORD(mode.value | 0x0004)))


class AccessLogFormatter(logging.Formatter):
    def __init__(self, color):
        super().__init__()
        self.color = color

    def format(self, record):
        timestamp = datetime.fromtimestamp(record.created).astimezone()
        style = record.http_outcome
        if self.color:
            style = f"\x1b[{record.http_color}m{style}\x1b[0m"
        return f"{timestamp.isoformat(sep=' ', timespec='milliseconds')} {style} {record.getMessage()}"


def configure_access_log():
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(AccessLogFormatter(supports_color(handler.stream)))
    access_log.handlers[:] = [handler]
    access_log.setLevel(logging.INFO)
    access_log.propagate = False


def describe_client(user_agent):
    ua = user_agent.lower()
    if any(marker in ua for marker in ("bot", "crawler", "spider", "slurp")):
        return "bot", "-", "-"
    if "ipad" in ua or ("macintosh" in ua and "mobile/" in ua):
        device, system = "tablet", "iPadOS"
    elif "iphone" in ua or "ipod" in ua:
        device, system = "mobile", "iOS"
    elif "android" in ua:
        device, system = ("mobile" if "mobile" in ua else "tablet"), "Android"
    elif "windows" in ua:
        device, system = "desktop", "Windows"
    elif "macintosh" in ua:
        device, system = "desktop", "macOS"
    elif "cros" in ua:
        device, system = "desktop", "ChromeOS"
    elif "linux" in ua:
        device, system = "desktop", "Linux"
    else:
        device, system = "-", "-"

    browser = next((name for marker, name in BROWSERS if marker in ua), "-")
    return device, system, browser


class AccessRequestHandler(WSGIRequestHandler):
    listener = "locale"

    def handle_one_request(self):
        self.request_id = secrets.token_hex(8)
        self.response_status = None
        started = perf_counter()
        try:
            super().handle_one_request()
        finally:
            if self.response_status is not None:
                self.write_access_log((perf_counter() - started) * 1000)

    def make_environ(self):
        environ = super().make_environ()
        environ[REQUEST_ID_KEY] = self.request_id
        return environ

    def end_headers(self):
        self.send_header("X-Request-ID", self.request_id)
        super().end_headers()

    def log_request(self, code="-", size="-"):
        # Werkzeug calls this when it sends the HTTP status.
        self.response_status = str(code)

    def write_access_log(self, duration_ms):
        status = self.response_status
        label, color, level = STATUS_STYLES.get(status[:1], ("HTTP", "36", logging.INFO))
        headers = getattr(self, "headers", {})
        device, system, browser = describe_client(headers.get("User-Agent", ""))
        forwarded, source = self.forwarded_ip(headers)

        # Keep untrusted characters off the terminal and omit query parameters.
        method = ascii((self.command or "-")[:24])
        path = getattr(self, "path", "").partition("?")[0]
        safe_path = ascii(path[:300] + ("..." if len(path) > 300 else ""))
        access_log.log(
            level,
            "%s %s HTTP=%s durata=%.1fms accesso=%s ip_connessione=%s "
            "ip_dichiarato=%s header=%s dispositivo=%s os=%s browser=%r richiesta=%s",
            method, safe_path, status, duration_ms, self.listener, self.address_string(),
            forwarded, source, device, system, browser, self.request_id,
            extra={"http_outcome": label, "http_color": color},
        )

    def forwarded_ip(self, headers):
        return "-", "-"


class TunnelRequestHandler(AccessRequestHandler):
    listener = "pubblica"

    def forwarded_ip(self, headers):
        for source in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
            value = headers.get(source, "")
            if source == "X-Forwarded-For":
                # The closest proxy is the last item in the chain.
                value = value.rsplit(",", 1)[-1]
            value = value.strip()
            try:
                address = ipaddress.ip_address(value)
                return str(address).split("%", 1)[0], source
            except ValueError:
                continue
        return "-", "-"

