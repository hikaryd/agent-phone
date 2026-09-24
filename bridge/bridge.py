"""One Android device's CDP reverse tunnel with loopback-only listeners."""

import ctypes
import fcntl
import json
import os
import pathlib
import re
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path.home() / "phone-admin"))
from owner_control import (
    handback_if_unowned,
    inspect_lease,
    parse_keyguard_showing,
    while_unowned,
)
from policy import may_foreground_chrome

HOME = pathlib.Path.home() / "agent-phone"
HOME.mkdir(mode=0o700, exist_ok=True)
lock = (HOME / "bridge.lock").open("w")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(0)
os.umask(0o077)
config = json.loads((HOME / "config.json").read_text())
serial = config["serial"]
ssh_target = config["ssh_target"]
foreground_packages = config.get("foreground_recovery_packages", ["com.android.chrome"])
if (
    not isinstance(foreground_packages, list)
    or not foreground_packages
    or any(
        not isinstance(package, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package)
        is None
        for package in foreground_packages
    )
):
    raise ValueError("invalid foreground recovery packages")
ssh = None
stop = False
start = time.monotonic()
off = None
off_requested = False
owner_active = False
owner_handback = False
endpoint = (HOME / "adb-endpoint").read_text().strip()
last_discover = last_foreground = 0
http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(*args, timeout=12):
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )


def log(event, **kw):
    print(
        json.dumps(
            dict(
                time=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                event=event,
                uptime=round(time.monotonic() - start),
                **kw,
            )
        ),
        flush=True,
    )


def adb(*args):
    return run("adb", "-s", endpoint, *args)


def parse_display_off(output):
    """Only one unambiguous physical committed state is usable evidence."""
    states = re.findall(r"\bmCommittedState=([^\s,}]*)", output)
    if len(states) != 1 or states[0] not in ("OFF", "ON"):
        return None
    return states[0] == "OFF"


def display_off_decision(uptime, display_off, off_requested, maintenance):
    requested = off_requested or display_off is True or uptime >= 1800
    return requested, requested and display_off is not True and not maintenance


def read_display_off():
    result = adb("shell", "dumpsys", "display")
    return parse_display_off(result.stdout) if result.returncode == 0 else None


def kill_tunnel():
    global ssh
    if ssh and ssh.poll() is None:
        ssh.terminate()
        try:
            ssh.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ssh.kill()
            ssh.wait()
    ssh = None


def child_lifetime(parent):
    def configure():
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            os._exit(126)
        if os.getppid() != parent:
            os._exit(125)

    return configure


def status_write(**kw):
    tmp = HOME / "status.tmp"
    tmp.write_text(
        json.dumps(dict(time=time.time(), uptime=round(time.monotonic() - start), **kw))
    )
    tmp.replace(HOME / "status.json")


def finish(*_):
    global stop
    stop = True


signal.signal(signal.SIGTERM, finish)
signal.signal(signal.SIGINT, finish)


def discover():
    # Standard DNS-SD: accept only this device's TLS-connect SRV record.
    def name(data, pos, depth=0):
        if depth > 12:
            raise ValueError("DNS loop")
        labels = []
        while True:
            n = data[pos]
            pos += 1
            if n == 0:
                return ".".join(labels), pos
            if n & 192 == 192:
                more, _ = name(data, ((n & 63) << 8) | data[pos], depth + 1)
                return ".".join(labels + [more]), pos + 1
            labels.append(data[pos : pos + n].decode("ascii"))
            pos += n

    q = (
        b"".join(
            bytes([len(p)]) + p.encode() for p in ["_adb-tls-connect", "_tcp", "local"]
        )
        + b"\0"
    )
    packet = struct.pack("!6H", 0, 0, 1, 0, 0, 0) + q + struct.pack("!HH", 12, 0x8001)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(1)
        s.sendto(packet, ("224.0.0.251", 5353))
        end = time.monotonic() + 4
        while time.monotonic() < end:
            try:
                data, _ = s.recvfrom(9000)
            except TimeoutError:
                continue
            try:
                _, _, qd, an, ns, ar = struct.unpack("!6H", data[:12])
                p = 12
                for _ in range(qd):
                    _, p = name(data, p)
                    p += 4
                for _ in range(an + ns + ar):
                    owner, p = name(data, p)
                    typ, _cl, _ttl, n = struct.unpack("!HHIH", data[p : p + 10])
                    p += 10
                    if (
                        typ == 33
                        and owner.startswith("adb-" + serial + "-")
                        and owner.endswith("._adb-tls-connect._tcp.local")
                    ):
                        return "127.0.0.1:" + str(
                            struct.unpack("!HHH", data[p : p + 6])[2]
                        )
                    p += n
            except (ValueError, IndexError, UnicodeError, struct.error):
                continue
    return None


run("termux-wake-lock")
log("started", endpoint=endpoint, pid=os.getpid())
try:
    while not stop:
        off = None
        try:
            lease, revoked = inspect_lease(HOME / "display-owner-lease")
            owner_handback = lease is None and (
                owner_handback or revoked or owner_active
            )
            owner_active = lease is not None
            off_requested = off_requested or owner_active or owner_handback
            if owner_active or owner_handback:
                kill_tunnel()
            if adb("get-state").stdout.strip() != "device":
                kill_tunnel()
                connection = run("adb", "connect", endpoint)
                if adb("get-state").stdout.strip() != "device":
                    if time.monotonic() - last_discover > 60:
                        last_discover = time.monotonic()
                        found = discover()
                        if found:
                            endpoint = found
                            (HOME / "adb-endpoint").write_text(endpoint + "\n")
                            log("discovered", endpoint=endpoint)
                    detail = connection.stdout.strip().replace("\n", " ")[:120]
                    raise RuntimeError(
                        "local authenticated ADB disconnected"
                        + (": " + detail if detail else "")
                    )
            if adb("shell", "getprop", "ro.serialno").stdout.strip() != serial:
                raise RuntimeError("Android device identity mismatch")
            # handback verifies PIN itself and always powers OFF, even if locked.
            if owner_handback:
                owner_active = not handback_if_unowned()
                owner_handback = False
            policy = adb("shell", "dumpsys", "window", "policy")
            if policy.returncode != 0 or "KeyguardServiceDelegate" not in policy.stdout:
                raise RuntimeError("Cannot verify Android lock state")
            keyguard = parse_keyguard_showing(policy.stdout)
            if keyguard is True:
                raise RuntimeError(
                    "Phone locked: unlock manually; PIN bypass is not configured"
                )
            if keyguard is not False:
                raise RuntimeError("Unknown Android lock state")
            if owner_active:
                off = read_display_off()
                raise RuntimeError("Owner control active; browser paused")
            # Enforce the panel policy even when background Chrome stops CDP.
            off = read_display_off()
            off_requested, power_off = display_off_decision(
                time.monotonic() - start,
                off,
                off_requested,
                (HOME / "display-keep-on").exists(),
            )
            if power_off:
                # A successful command is not evidence that the panel stayed off.
                off = None

                def power_off_panel():
                    result = adb("shell", "cmd", "display", "power-off", "0")
                    if result.returncode != 0:
                        raise RuntimeError("display power-off: " + result.stdout[:120])

                if not while_unowned(power_off_panel):
                    owner_active = True
                    raise RuntimeError("Owner control active; browser paused")
                off = read_display_off()
                log("display_power_off_requested", display_off=off)
            result = adb("forward", "tcp:19222", "localabstract:chrome_devtools_remote")
            if result.returncode != 0:
                raise RuntimeError("ADB forward failed: " + result.stdout[:120])
            try:
                with http.open("http://127.0.0.1:19222/json/version", timeout=5) as r:
                    v = json.load(r)
            except Exception:
                kill_tunnel()
                # Owner may have started a lease while the CDP request timed out.
                lease, revoked = inspect_lease(HOME / "display-owner-lease")
                owner_handback = lease is None and (
                    owner_handback or revoked or owner_active
                )
                owner_active = lease is not None
                off_requested = off_requested or owner_active or owner_handback
                if (
                    not owner_active
                    and not owner_handback
                    and time.monotonic() - last_foreground > 60
                    and not (HOME / "display-keep-on").exists()
                ):
                    try:

                        def foreground_if_safe():
                            # Recheck inside the lease lock: a new owner session,
                            # lock screen, or panel wake can invalidate prior reads.
                            policy = adb("shell", "dumpsys", "window", "policy")
                            if (
                                policy.returncode != 0
                                or parse_keyguard_showing(policy.stdout) is not False
                            ):
                                raise RuntimeError(
                                    "Chrome recovery skipped: keyguard not verified unlocked"
                                )
                            display_off = read_display_off()
                            activities = adb(
                                "shell", "dumpsys", "activity", "activities"
                            )
                            if activities.returncode != 0 or not may_foreground_chrome(
                                activities.stdout,
                                (HOME / "display-keep-on").exists(),
                                display_off,
                                foreground_packages,
                            ):
                                raise RuntimeError(
                                    "Chrome recovery skipped: display or activity not eligible"
                                )
                            launch = adb(
                                "shell",
                                "am",
                                "start",
                                "-n",
                                "com.android.chrome/com.google.android.apps.chrome.Main",
                            )
                            if launch.returncode != 0:
                                raise RuntimeError(
                                    "Chrome recovery failed: " + launch.stdout[:120]
                                )

                        last_foreground = time.monotonic()
                        if while_unowned(foreground_if_safe):
                            log("chrome_recovery_started")
                        else:
                            log("chrome_recovery_skipped", error="owner control active")
                    except (
                        OSError,
                        ValueError,
                        RuntimeError,
                        subprocess.SubprocessError,
                    ) as recovery:
                        log("chrome_recovery_skipped", error=str(recovery)[:240])
                # Keep the original CDP failure as the reported error.
                raise
            if v.get("Android-Package") != "com.android.chrome":
                raise RuntimeError("Chrome identity mismatch")
            if not ssh or ssh.poll() is not None:
                kill_tunnel()
                ssh = subprocess.Popen(
                    [
                        "ssh",
                        "-N",
                        "-T",
                        "-i",
                        str(HOME / "tunnel-key"),
                        "-o",
                        "IdentitiesOnly=yes",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "StrictHostKeyChecking=yes",
                        "-o",
                        "UserKnownHostsFile=" + str(HOME / "known_hosts"),
                        "-o",
                        "ExitOnForwardFailure=yes",
                        "-o",
                        "ServerAliveInterval=15",
                        "-o",
                        "ServerAliveCountMax=3",
                        "-o",
                        "ConnectTimeout=10",
                        "-R",
                        "127.0.0.1:19222:127.0.0.1:19222",
                        ssh_target,
                    ],
                    preexec_fn=child_lifetime(os.getpid()),  # noqa: PLW1509 - single-threaded supervisor
                )
                log("tunnel_started", pid=ssh.pid)
            off = read_display_off()
            status_write(
                endpoint=endpoint,
                display_off=off,
                display_off_requested=off_requested,
                owner_control_active=owner_active,
                ssh_alive=ssh.poll() is None,
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
            kill_tunnel()
            status_write(
                endpoint=endpoint,
                display_off=off,
                display_off_requested=off_requested,
                owner_control_active=owner_active,
                ssh_alive=False,
                error=str(e)[:240],
            )
            log("retry", error=str(e)[:240])
        for _ in range(10):
            if stop:
                break
            time.sleep(1)
finally:
    kill_tunnel()
    log("stopped")
