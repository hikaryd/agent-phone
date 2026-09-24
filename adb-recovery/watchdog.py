"""Ask the local helper to restore wireless ADB after a sustained disconnect."""

import fcntl
import json
import pathlib
import re
import subprocess
import time

HOME = pathlib.Path.home() / "agent-phone"
ACTION = "dev.agentphone.adbrecovery.RECOVER"
COMPONENT = "dev.agentphone.adbrecovery/.RecoverReceiver"


def request_due(now, failure_since, last_request):
    return failure_since is not None and now - failure_since >= 20 and now - last_request >= 60


def run(*args, timeout=8):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def write_status(**values):
    temporary = HOME / "recovery-status.tmp"
    temporary.write_text(json.dumps({"time": time.time(), **values}) + "\n")
    temporary.replace(HOME / "recovery-status.json")


def ask_helper():
    token = (HOME / "recovery-token").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("invalid recovery token")
    return run("am", "broadcast", "-a", ACTION, "-n", COMPONENT, "--es", "token", token)


def main():
    import sys

    sys.path.insert(0, str(pathlib.Path.home() / "phone-admin"))
    from owner_control import inspect_lease

    with (HOME / "recovery-watchdog.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        failure_since = None
        last_request = float("-inf")
        while True:
            now = time.monotonic()
            error = None
            try:
                endpoint = (HOME / "adb-endpoint").read_text().strip()
                adb = run("adb", "-s", endpoint, "get-state", timeout=6)
                connected = adb.returncode == 0 and adb.stdout.strip() == "device"
                if connected:
                    failure_since = None
                else:
                    if failure_since is None:
                        failure_since = now
                    if request_due(now, failure_since, last_request):
                        lease, _ = inspect_lease(HOME / "display-owner-lease")
                        if lease is None and not (HOME / "adb-recovery-paused").exists():
                            last_request = now
                            result = ask_helper()
                            if result.returncode != 0:
                                error = "helper broadcast failed"
                write_status(
                    adb_connected=connected,
                    failure_seconds=round(now - failure_since) if failure_since else 0,
                    last_request_monotonic=round(last_request) if last_request > 0 else None,
                    paused=(HOME / "adb-recovery-paused").exists(),
                    error=error,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                write_status(adb_connected=False, error=str(exc)[:160])
            time.sleep(10)


if __name__ == "__main__":
    main()
