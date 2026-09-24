#!/usr/bin/env python3
"""Authenticated, fixed-action Phone owner display lease; also imported by bridge."""

import contextlib
import fcntl
import hmac
import html
import http.server
import ipaddress
import json
import math
import os
import pathlib
import re
import secrets
import stat as stat_module
import subprocess
import time

HOME = pathlib.Path.home()
BRIDGE = HOME / "agent-phone"
LEASE = BRIDGE / "display-owner-lease"
HOST = "127.0.0.1:8766"
MAX_LEASE = 900
CHROME = "com.android.chrome/com.google.android.apps.chrome.Main"


def read_config():
    config = json.loads((HOME / "phone-admin/control.json").read_text())
    token, origin, serial = config["token"], config["origin"], config["serial"]
    address = ipaddress.IPv4Address(config["lan_ip"])
    if (
        not address.is_private
        or address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    ):
        raise ValueError("invalid LAN address")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
        raise ValueError("invalid control token")
    if origin != f"https://{address}:8443":
        raise ValueError("invalid control origin")
    if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", serial):
        raise ValueError("invalid device serial")
    return config


def boot_id():
    return pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def valid_lease(value, boot, now):
    if not isinstance(value, dict) or set(value) != {
        "boot_id",
        "lease_id",
        "expires_monotonic",
    }:
        return False
    expiry = value["expires_monotonic"]
    return (
        value["boot_id"] == boot
        and isinstance(value["lease_id"], str)
        and re.fullmatch(r"[0-9a-f]{32}", value["lease_id"]) is not None
        and type(expiry) in (int, float)
        and now < expiry <= now + MAX_LEASE
        and math.isfinite(expiry)
    )


@contextlib.contextmanager
def lease_lock(path):
    # Serializes metadata and bounded owner ADB transitions across both processes.
    lock = path.with_name(path.name + ".lock")
    with os.fdopen(os.open(lock, os.O_RDWR | os.O_CREAT, 0o600), "r+") as stream:
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("owner transition busy") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def inspect_lease(path=LEASE, *, boot=None, now=None):
    with lease_lock(path):
        return _inspect_lease_locked(path, boot=boot, now=now)


def revoke_lease(path=LEASE):
    with lease_lock(path):
        path.unlink(missing_ok=True)


def _inspect_lease_locked(path=LEASE, *, boot=None, now=None):
    """Return (active lease or None, revoked invalid/expired lease)."""
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None, False
    try:
        if (
            not stat_module.S_ISREG(stat.st_mode)
            or stat.st_uid != os.getuid()
            or stat.st_mode & 0o077
        ):
            raise ValueError("unsafe lease file")
        with path.open() as stream:
            value = json.loads(stream.read(4097))
        if stat.st_size > 4096 or not valid_lease(
            value,
            boot if boot is not None else boot_id(),
            now if now is not None else time.monotonic(),
        ):
            raise ValueError("invalid or expired lease")
        return value, False
    except (ValueError, OSError):
        path.unlink(missing_ok=True)
        return None, True


def write_lease(path=LEASE):
    with lease_lock(path):
        return _write_lease_locked(path)


def _write_lease_locked(path):
    value = {
        "boot_id": boot_id(),
        "lease_id": secrets.token_hex(16),
        "expires_monotonic": time.monotonic() + MAX_LEASE,
    }
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def adb(*args):
    endpoint = (BRIDGE / "adb-endpoint").read_text().strip()
    if not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", endpoint):
        raise RuntimeError("invalid local ADB endpoint")
    return subprocess.run(
        ["adb", "-s", endpoint, *args],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )


def parse_keyguard_showing(output):
    sections = output.split("KeyguardServiceDelegate")
    if len(sections) != 2:
        return None
    showing = re.findall(r"\bshowing=([^\s,}]+)", sections[1])
    if showing == ["false"]:
        return False
    if showing == ["true"]:
        return True
    return None


def unlocked(command=adb):
    identity = command("shell", "getprop", "ro.serialno")
    if identity.returncode != 0 or identity.stdout.strip() != read_config()["serial"]:
        return False
    result = command("shell", "dumpsys", "window", "policy")
    return result.returncode == 0 and parse_keyguard_showing(result.stdout) is False


def panel_on(command=adb):
    result = command("shell", "dumpsys", "display")
    return result.returncode == 0 and re.findall(
        r"\bmCommittedState=([^\s,}]*)", result.stdout
    ) == ["ON"]


def handback(command=adb):
    """No unlocking/locking: restore fixed Chrome only with verified unlocked PIN."""
    try:
        if unlocked(command):
            result = command("shell", "am", "start", "-n", CHROME)
            if result.returncode != 0:
                raise RuntimeError("Chrome handback failed")
    finally:
        result = command("shell", "cmd", "display", "power-off", "0")
        if result.returncode != 0:
            raise RuntimeError("display power-off failed")


def while_unowned(action, path=LEASE):
    """Recheck under the transition lock; never act over a newly acquired lease."""
    with lease_lock(path):
        active, _ = _inspect_lease_locked(path)
        if active is not None:
            return False
        action()
        return True


def handback_if_unowned(command=adb, path=LEASE):
    return while_unowned(lambda: handback(command), path)


def start_owner(command=adb, path=LEASE):
    with lease_lock(path):
        try:
            if not unlocked(command):
                raise RuntimeError(
                    "Unlock the phone manually before starting owner mode"
                )
            value = _write_lease_locked(path)
            result = command("shell", "cmd", "display", "power-reset", "0")
            if result.returncode != 0 or not panel_on(command):
                raise RuntimeError("Cannot verify physical display ON")
        except (OSError, RuntimeError, subprocess.SubprocessError):
            try:
                path.unlink(missing_ok=True)
            finally:
                command("shell", "cmd", "display", "power-off", "0")
            raise
        return value


def stop_owner(command=adb, path=LEASE):
    with lease_lock(path):
        try:
            path.unlink(missing_ok=True)
        finally:
            handback(command)


def record_failure(code, path=HOME / "phone-admin/control-last-error.json"):
    if code not in ("owner_start_failed", "owner_stop_failed"):
        raise ValueError("unknown failure code")
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as stream:
            json.dump({"code": code, "timestamp": time.time()}, stream)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def lease_snapshot(path=LEASE):
    """Read only: GET must never consume expiry notification needed by bridge."""
    try:
        info = path.lstat()
        if (
            not stat_module.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
            or info.st_size > 4096
        ):
            raise ValueError("invalid lease file")
        with path.open() as stream:
            value = json.loads(stream.read(4097))
        now = time.monotonic()
        if valid_lease(value, boot_id(), now):
            return {
                "active": True,
                "remaining": min(900, math.ceil(value["expires_monotonic"] - now)),
            }
    except (OSError, ValueError, TypeError):
        pass
    return {"active": False, "remaining": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    timeout = 5

    def log_message(self, *_):
        pass

    def reply(self, code, body, content_type="text/html; charset=utf-8"):
        content = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)
        self.close_connection = True

    def authorized(self, post=False):
        if self.headers.get_all("Host") != [HOST]:
            return False
        tokens = self.headers.get_all("X-Phone-Control", [])
        if len(tokens) != 1 or not hmac.compare_digest(
            tokens[0].encode(), self.server.token.encode()
        ):
            return False
        if self.headers.get_all("Transfer-Encoding") is not None:
            return False
        lengths = self.headers.get_all("Content-Length", [])
        if lengths not in ([], ["0"]):
            return False
        return not post or self.headers.get_all("Origin") == [self.server.origin]

    def do_GET(self):
        if not self.authorized():
            self.reply(403, "Forbidden")
            return
        if self.path not in ("/owner", "/owner/status"):
            self.reply(404, "Not found")
            return
        state = lease_snapshot(LEASE)
        if self.path == "/owner/status":
            self.reply(200, json.dumps(state), "application/json; charset=utf-8")
            return
        remaining = state["remaining"]
        vnc_link = (
            "<p><a href='/vnc.html?autoconnect=true&amp;resize=scale&amp;path=websockify' target='_blank' rel='noopener noreferrer'>Открыть экран телефона (noVNC)</a></p>"
            if state["active"]
            else ""
        )
        self.reply(
            200,
            "<!doctype html><meta charset='utf-8'><title>Phone owner</title>"
            "<h1>Управление телефоном</h1>"
            "<p>Владелец имеет приоритет перед агентом. Режим управления длится до 15 минут. "
            f"Осталось: {remaining} секунд. После истечения работающий мост вернёт Chrome и выключит экран.</p>"
            "<p>Для живого изображения noVNC экран телефона должен быть включён. "
            "Нажмите «Начать 15 минут»: включится экран и откроется noVNC. "
            "При выключенном экране VNC может показывать старый кадр.</p>"
            "<p>PIN снимается только вручную. Не нажимайте кнопку блокировки.</p>"
            "<form method='post' action='/owner/start'><button>Начать 15 минут</button></form>"
            "<form method='post' action='/owner/stop'><button>Завершить сейчас</button></form>"
            + vnc_link
            + "<p><a href='/owner'>Обновить оставшееся время</a></p>",
        )

    def do_POST(self):
        if not self.authorized(post=True):
            self.reply(403, "Forbidden")
            return
        if self.path not in ("/owner/start", "/owner/stop"):
            self.reply(404, "Not found")
            return
        try:
            if self.path == "/owner/start":
                start_owner()
            else:
                stop_owner()
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            try:
                record_failure(
                    "owner_start_failed"
                    if self.path == "/owner/start"
                    else "owner_stop_failed"
                )
            except OSError:
                pass
            # Errors contain only our fixed diagnostics, never commands/configuration.
            self.reply(
                503,
                "Owner control unavailable: "
                + html.escape(type(error).__name__)
                + ". Unlock manually if necessary.",
            )
            return
        self.send_response(303)
        self.send_header(
            "Location",
            "/vnc.html?autoconnect=true&resize=scale&path=websockify"
            if self.path == "/owner/start"
            else "/owner",
        )
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True


def main():
    config = read_config()
    token, origin = config["token"], config["origin"]
    with http.server.HTTPServer(("127.0.0.1", 8766), Handler) as server:
        server.token = token
        server.origin = origin
        server.serve_forever()


if __name__ == "__main__":
    main()
