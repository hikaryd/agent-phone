"""Supervise only the owner's sshd and private reverse tunnel, never Agent."""

import ipaddress
import json
import logging
import logging.handlers
import os
import pathlib
import signal
import socket
import subprocess
import threading
import time

HOME = pathlib.Path.home() / "phone-owner"


def validate_inventory(value):
    if not isinstance(value, dict) or set(value) != {"lan_ip", "vps_host"}:
        raise ValueError("Expected explicit LAN/VPS inventory")
    lan = ipaddress.IPv4Address(value["lan_ip"])
    vps = ipaddress.IPv4Address(value["vps_host"])
    if (
        not lan.is_private
        or lan.is_loopback
        or lan.is_unspecified
        or lan.is_link_local
        or lan.is_reserved
        or lan.is_multicast
    ):
        raise ValueError("Expected exact private LAN bind address")
    if vps.is_unspecified or vps.is_loopback or vps.is_multicast or vps.is_link_local:
        raise ValueError("Expected exact VPS IPv4 address")
    return {"lan_ip": str(lan), "vps_host": str(vps)}


def inventory():
    return validate_inventory(json.loads((HOME / "inventory.json").read_text()))


def commands():
    return {
        "sshd": ["sshd", "-D", "-e", "-f", str(HOME / "sshd_config")],
        "tunnel": [
            "ssh",
            "-F",
            "/dev/null",
            "-N",
            "-T",
            "-i",
            str(HOME / "link-key"),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "ForwardAgent=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + str(HOME / "known_hosts"),
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-R",
            "127.0.0.1:19223:127.0.0.1:8022",
            "phone-owner-link@" + inventory()["vps_host"],
        ],
    }


def lan_available():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((inventory()["lan_ip"], 0))
        return True
    except OSError:
        return False


def note_exit(state, code):
    state.update(
        last_exit=code, last_exit_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )


def drain(pipe, logger):
    with pipe:
        for line in iter(pipe.readline, ""):
            logger.warning(line.rstrip()[:2048])


def main():
    os.umask(0o077)
    children = {}
    records = {name: {"starts": 0, "last_exit": None} for name in commands()}
    retries = {name: 0 for name in commands()}
    next_start = {name: 0.0 for name in commands()}
    started = {}
    loggers = {}
    for name in commands():
        logger = logging.getLogger("phone-owner." + name)
        logger.setLevel(logging.WARNING)
        logger.addHandler(
            logging.handlers.RotatingFileHandler(
                HOME / (name + ".log"), maxBytes=65536, backupCount=1
            )
        )
        logger.propagate = False
        loggers[name] = logger
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        while not stopping:
            lan = lan_available()
            for name, command in commands().items():
                child = children.get(name)
                if child is not None and child.poll() is not None:
                    note_exit(records[name], child.returncode)
                    retries[name] = (
                        0
                        if time.monotonic() - started[name] >= 60
                        else retries[name] + 1
                    )
                    delay = min(300, 15 * (2 ** min(retries[name], 5)))
                    next_start[name] = time.monotonic() + delay
                    records[name]["retry_delay_seconds"] = delay
                    del children[name]
                    child = None
                # Do not allow sshd to silently start with only its loopback bind.
                enabled = name != "tunnel" or (HOME / "tunnel-enabled").exists()
                if (
                    child is None
                    and lan
                    and enabled
                    and time.monotonic() >= next_start[name]
                ):
                    child = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    children[name] = child
                    started[name] = time.monotonic()
                    records[name]["starts"] += 1
                    threading.Thread(
                        target=drain, args=(child.stderr, loggers[name]), daemon=True
                    ).start()
                records[name].update(
                    pid=child.pid if child else None,
                    running=child is not None and child.poll() is None,
                )
            state = {
                "services": records,
                "lan_available": lan,
                "readiness": "external_authenticated_check_required",
            }
            temp = HOME / "status.next"
            temp.write_text(json.dumps(state))
            temp.replace(HOME / "status.json")
            for _ in range(10):
                if stopping:
                    break
                time.sleep(1)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
