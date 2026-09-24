"""Cooperative scheduling inside the actual browser-harness CLI process."""

import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

READ = "# phone:read-v1"
FINISH = "# phone:finish-v1"
READ_CAPACITY = 4
# No caller-supplied JavaScript is accepted by the parallel read path.
EXTRACT = """JSON.stringify({ready:document.readyState,title:document.title.slice(0,2048),
url:location.href.slice(0,8192),text:(document.body ? document.body.innerText : '').slice(0,__MAX_CHARS__)})"""


def classify(source):
    first, _, body = source.partition("\n")
    if first == READ:
        data = json.loads(body)
        if not isinstance(data, dict) or set(data) - {"url", "max_chars"}:
            raise ValueError("read job accepts only url and max_chars")
        url = data.get("url")
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("read URL must be HTTP(S), without credentials")
        limit = data.get("max_chars", 20000)
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise ValueError("max_chars must be 1..100000")
        return "read", data
    if source.strip() == FINISH:
        return "finish", None
    if first.startswith("# phone:"):
        raise ValueError("unknown or malformed Phone directive")
    return "legacy", source


def key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def acquire(fd, deadline, operation=fcntl.LOCK_EX):
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Phone browser busy; retry after owner session or current job finishes"
                )
            time.sleep(0.03)


@contextlib.contextmanager
def admission(root, name, mode, timeout, session_fd=None):
    """Turnstile prevents new readers from overtaking a waiting writer."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with contextlib.ExitStack() as stack:

        def lock(filename, operation=fcntl.LOCK_EX):
            fd = stack.enter_context(open(root / filename, "a+"))
            acquire(fd, deadline, operation)
            return fd

        if session_fd is None:
            lock("session-" + key(name) + ".lock")
        else:
            expected = os.stat(root / ("session-" + key(name) + ".lock"))
            actual = os.fstat(session_fd)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise RuntimeError("invalid inherited session lock")
            acquire(session_fd, deadline)
        turn = lock("turn.lock")
        if mode != "read":
            lock("slot-0.lock")
            lock("slot-1.lock")
        else:
            # Keep the old two-slot protocol as outer read/write gates. Shared
            # acquisition of BOTH gates drains old exclusive readers first and
            # makes old and new writers exclude every new capacity slot.
            lock("slot-0.lock", fcntl.LOCK_SH)
            lock("slot-1.lock", fcntl.LOCK_SH)
            slots = [
                stack.enter_context(open(root / f"read-capacity-{i}.lock", "a+"))
                for i in range(READ_CAPACITY)
            ]
            while True:
                found = False
                for fd in slots:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        found = True
                        break
                    except BlockingIOError:
                        pass
                if found:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Phone read capacity exhausted")
                time.sleep(0.03)
        fcntl.flock(turn, fcntl.LOCK_UN)
        yield


def identity(pid):
    """Linux process start ticks distinguish a live worker from PID reuse."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def save(path, value):
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value))
    tmp.chmod(0o600)
    os.replace(tmp, path)


def target_list(cdp):
    response = cdp("Target.getTargets", _response_timeout=1)
    if not isinstance(response, dict) or not isinstance(
        response.get("targetInfos"), list
    ):
        raise TypeError("cannot validate browser target list")
    targets = response["targetInfos"]
    if any(
        not isinstance(t, dict)
        or not isinstance(t.get("targetId"), str)
        or not t["targetId"]
        for t in targets
    ):
        raise RuntimeError("invalid browser target record")
    return targets


def close_owned(cdp, path, target):
    # A successful close acknowledgement is not proof of target disappearance.
    close_error = None
    try:
        cdp("Target.closeTarget", targetId=target, _response_timeout=1)
    except (RuntimeError, OSError) as error:
        close_error = error
    deadline = time.monotonic() + 2
    while True:
        if not any(t["targetId"] == target for t in target_list(cdp)):
            path.unlink(missing_ok=True)
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("owned tab did not close") from close_error
        time.sleep(0.05)


def reap(cdp, root):
    for path in root.glob("read-*.json"):
        try:
            record = json.loads(path.read_text())
        except FileNotFoundError:
            # A live worker may finish between glob and read.
            continue
        current = identity(record["pid"])
        if current is not None and current == record["start"]:
            continue
        close_owned(cdp, path, record["target"])


def attach(cdp, meta, target):
    sid = cdp("Target.attachToTarget", targetId=target, flatten=True).get("sessionId")
    if not sid:
        raise RuntimeError("cannot attach owned tab")
    meta({"meta": "set_session", "session_id": sid, "target_id": target})


def create(cdp, path, kind):
    target = cdp("Target.createTarget", url="about:blank", background=True).get(
        "targetId"
    )
    if not target:
        raise RuntimeError("browser did not create owned tab")
    save(
        path,
        {
            "target": target,
            "pid": os.getpid(),
            "start": identity(os.getpid()),
            "kind": kind,
        },
    )
    return target


def read_job(cdp, meta, path, spec):
    target = create(cdp, path, "read")
    try:
        attach(cdp, meta, target)
        result = cdp("Page.navigate", url=spec["url"])
        if result.get("errorText"):
            raise RuntimeError("navigation failed: " + result["errorText"])
        deadline = time.monotonic() + 30
        while True:
            result = cdp(
                "Runtime.evaluate",
                expression=EXTRACT.replace(
                    "__MAX_CHARS__", str(spec.get("max_chars", 20000))
                ),
                returnByValue=True,
            )
            if result.get("exceptionDetails"):
                raise RuntimeError("read extraction failed")
            data = result.get("result", {}).get("value")
            if isinstance(data, str):
                data = json.loads(data)
            if (
                isinstance(data, dict)
                and data.get("ready") in ("interactive", "complete")
                and data.get("url") != "about:blank"
            ):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("page did not become readable")
            time.sleep(0.1)
        data["text"] = data.get("text", "")[: spec.get("max_chars", 20000)]
        data["target_id"] = target
        return data
    finally:
        close_owned(cdp, path, target)


def preexisting_sessions(config):
    names = config.get("preexisting_sessions", [])
    if not isinstance(names, list) or any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
        for name in names
    ):
        raise ValueError("preexisting_sessions must be a list of valid session names")
    return names


def execute(namespace, source, config, meta=None):
    """Called after harness initialization; locks live in its exec process."""
    if meta is None:
        from browser_harness.helpers import _send

        meta = _send

    os.umask(0o077)
    preserved_names = preexisting_sessions(config)
    mode, payload = classify(source)
    name = os.environ.get("BU_NAME", "default")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("invalid session name")
    if mode in ("read", "finish") and name == "default":
        raise ValueError("read/finish requires an explicit unique named session")
    root = Path(config["state_dir"]).expanduser()
    cdp = namespace["cdp"]
    queued = time.monotonic()
    inherited = os.environ.pop("PHONE_SESSION_LOCK_FD", None)
    session_fd = int(inherited) if inherited is not None else None
    with admission(root, name, mode, config.get("wait_seconds", 60), session_fd):
        admitted = time.monotonic()
        # Different workers may reap simultaneously; serialize record inspection.
        with open(root / "reaper.lock", "a+") as fd:
            acquire(fd, time.monotonic() + 10)
            reap(cdp, root)
        if mode == "read":
            path = root / ("read-" + key(name) + ".json")
            if path.exists():
                raise RuntimeError("session has an unresolved owned read target")
            result = read_job(cdp, meta, path, payload)
            result.update(
                session=name,
                wait_seconds=round(admitted - queued, 3),
                run_seconds=round(time.monotonic() - admitted, 3),
            )
            print(json.dumps(result, ensure_ascii=False))
            return
        path = root / ("legacy-" + key(name) + ".json")
        preserved = name in preserved_names and not path.exists()
        if mode == "finish":
            if preserved:
                print(
                    json.dumps(
                        {
                            "session": name,
                            "preserved_unmanaged": True,
                            "daemon_shutdown_requested": False,
                        }
                    )
                )
                return
            if path.exists():
                close_owned(cdp, path, json.loads(path.read_text())["target"])
            meta({"meta": "shutdown"})
            print(json.dumps({"finished": name, "daemon_shutdown_requested": True}))
            return
        if name != "default" and not preserved:
            target = json.loads(path.read_text())["target"] if path.exists() else None
            if target:
                targets = target_list(cdp)
                if not any(t.get("targetId") == target for t in targets):
                    path.unlink()
                    target = None
            if not target:
                target = create(cdp, path, "legacy")
            attach(cdp, meta, target)
        exec(compile(payload, "<phone-legacy>", "exec"), namespace)  # noqa: S102 -- required legacy browser_exec Python API


def discover_websocket(endpoint):
    """Discover only through the configured origin: no proxies or redirects."""
    origin = urlsplit(endpoint)
    if origin.scheme in ("ws", "wss"):
        if (
            not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or not re.fullmatch(r"/devtools/browser(?:/[A-Za-z0-9_-]+)?", origin.path)
        ):
            raise ValueError("invalid explicit browser WebSocket endpoint")
        return endpoint
    if (
        origin.scheme not in ("http", "https")
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        raise ValueError("direct read requires an HTTP(S) CDP origin")
    connection = (
        http.client.HTTPSConnection
        if origin.scheme == "https"
        else http.client.HTTPConnection
    )(origin.hostname, origin.port, timeout=5)
    try:
        connection.request("GET", "/json/version")
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("CDP discovery unavailable; retry after owner mode ends")
        body = response.read(65537)
        if len(body) > 65536:
            raise ValueError("CDP discovery response too large")
        advertised = urlsplit(json.loads(body)["webSocketDebuggerUrl"])
        if (
            advertised.scheme not in ("ws", "wss")
            or not re.fullmatch(
                r"/devtools/browser(?:/[A-Za-z0-9_-]+)?", advertised.path
            )
            or advertised.query
            or advertised.fragment
        ):
            raise ValueError("invalid advertised browser WebSocket path")
        return urlunsplit(
            (
                "wss" if origin.scheme == "https" else "ws",
                origin.netloc,
                advertised.path,
                "",
                "",
            )
        )
    finally:
        connection.close()


class DirectCDP:
    """One synchronous socket, sequential request IDs, one owned page session."""

    def __init__(self, socket):
        self.socket = socket
        self.sequence = 0
        self.session = None

    def meta(self, request):
        if (
            request.get("meta") != "set_session"
            or not isinstance(request.get("session_id"), str)
            or not request["session_id"]
        ):
            raise ValueError("invalid direct CDP session selection")
        self.session = request["session_id"]

    def cdp(self, method, _response_timeout=5, **params):
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params}
        if not method.startswith("Target."):
            if not self.session:
                raise RuntimeError("page operation requires an attached owned session")
            request["sessionId"] = self.session
        self.socket.send(json.dumps(request))
        deadline = time.monotonic() + _response_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("CDP request timed out")
            response = json.loads(self.socket.recv(timeout=remaining))
            if not isinstance(response, dict):
                raise TypeError("invalid CDP response")
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                raise RuntimeError("CDP command failed: " + str(response["error"]))
            if not isinstance(response.get("result"), dict):
                raise TypeError("invalid CDP command result")
            return response["result"]


def direct_read(source, config):
    from websockets.sync.client import connect

    if classify(source)[0] != "read":
        raise ValueError("direct CDP accepts only structured read jobs")
    endpoints = [
        os.environ.get(k) for k in ("BU_CDP_URL", "BU_CDP_WS") if os.environ.get(k)
    ]
    if len(endpoints) != 1 or endpoints[0] not in config["phone_cdp_urls"]:
        raise ValueError("ambiguous or unconfigured direct CDP endpoint")
    endpoint = discover_websocket(endpoints[0])
    with connect(
        endpoint,
        proxy=None,
        open_timeout=5,
        close_timeout=1,
        max_size=1048576,
        max_queue=16,
    ) as socket:
        client = DirectCDP(socket)
        execute({"cdp": client.cdp}, source, config, meta=client.meta)
