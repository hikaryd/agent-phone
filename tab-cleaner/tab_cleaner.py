"""Close page targets after 12 observed hours; never persist browsing content."""

import argparse
import fcntl
import http.client
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

TTL_SECONDS = 12 * 60 * 60
MAX_BYTES = 1024 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def parse_targets(data):
    items = json.loads(data)
    if not isinstance(items, list):
        raise ValueError("invalid target list")  # noqa: TRY004 - invalid serialized CDP value
    seen, pages = set(), set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid target")  # noqa: TRY004 - invalid serialized CDP value
        target, kind = item.get("id"), item.get("type")
        if not isinstance(target, str) or not ID.fullmatch(target):
            raise ValueError("invalid target id")
        if target in seen or not isinstance(kind, str):
            raise ValueError("duplicate id or invalid type")
        seen.add(target)
        if kind == "page":
            pages.add(target)
    return pages


def load_state(path):
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        state = json.loads(path.read_bytes())
        if not isinstance(state, dict) or set(state) != {
            "version",
            "boot",
            "mono",
            "targets",
        }:
            return None
        if state["version"] != 1 or not isinstance(state["boot"], str):
            return None
        if not state["boot"] or not number(state["mono"]):
            return None
        targets = state["targets"]
        if not isinstance(targets, dict):
            return None
        if any(
            not ID.fullmatch(key) or not number(age) or age > TTL_SECONDS
            for key, age in targets.items()
        ):
            return None
        return state
    except (OSError, ValueError, TypeError):
        return None


def advance(previous, targets, boot, mono):
    """A reboot/monotonic rollback preserves ages but adds no unknown interval."""
    elapsed = 0
    old = {}
    if previous is not None:
        old = previous["targets"]
        if previous["boot"] == boot and mono >= previous["mono"]:
            elapsed = mono - previous["mono"]
    ages = {
        key: min(TTL_SECONDS, old[key] + elapsed) if key in old else 0
        for key in targets
    }
    return {"version": 1, "boot": boot, "mono": mono, "targets": ages}


def write_state(path, state):
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def request(path):
    # Literal loopback, no proxy environment, redirects, shell, or external URL.
    connection = http.client.HTTPConnection("127.0.0.1", 19222, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read(MAX_BYTES + 1)
        if response.status != 200 or len(body) > MAX_BYTES:
            raise ValueError("invalid CDP response")
        return body
    finally:
        connection.close()


def tick(directory, boot, mono, fetch=request):
    path = directory / "state.json"
    # Validate complete snapshot before touching state or issuing any close.
    targets = parse_targets(fetch("/json/list"))
    state = advance(load_state(path), targets, boot, mono)
    write_state(path, state)
    eligible = sorted(
        key for key, age in state["targets"].items() if age >= TTL_SECONDS
    )
    closed = failed = 0
    for target in eligible:
        try:
            answer = fetch("/json/close/" + target)
            if answer.strip() != b"Target is closing":
                raise ValueError("unconfirmed close")
            del state["targets"][target]
            closed += 1
        except (OSError, ValueError, http.client.HTTPException):
            failed += 1
    write_state(path, state)
    print(
        f"pages={len(targets)} eligible={len(eligible)} closed={closed} failed={failed}"
    )
    return failed == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir", type=Path, default=Path("/var/lib/phone-tab-cleaner")
    )
    args = parser.parse_args()
    os.umask(0o077)
    try:
        args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (args.state_dir / "lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("skipped=1")
                return 0
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if not boot:
                raise ValueError("missing boot id")
            return 0 if tick(args.state_dir, boot, time.monotonic()) else 1
    except (OSError, ValueError, http.client.HTTPException):
        # Deliberately omit exception messages: CDP content may contain private data.
        print("aborted=1")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
