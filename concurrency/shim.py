#!/usr/bin/env python3
"""Install optionally as $AGENT_HOME/bin/browser-use; configure privately."""

import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path


def exec_input(argv, source):
    with tempfile.TemporaryFile(mode="w+b") as stdin:
        stdin.write(source.encode())
        stdin.seek(0)
        os.dup2(stdin.fileno(), 0)
        os.execvpe(argv[0], argv, os.environ)


def main():
    os.umask(0o077)
    path = Path(
        os.environ.get(
            "PHONE_SCHEDULER_CONFIG", "~/.config/phone-browser/scheduler.json"
        )
    ).expanduser()
    config = json.loads(path.read_text())
    argv = config["upstream_argv"] + sys.argv[1:]
    endpoints = config["phone_cdp_urls"]
    if (
        not isinstance(endpoints, list)
        or not endpoints
        or any(not isinstance(v, str) or not v.strip() for v in endpoints)
    ):
        raise ValueError("phone_cdp_urls must be a nonempty list of nonempty strings")
    values = [
        os.environ.get(k) for k in ("BU_CDP_URL", "BU_CDP_WS") if os.environ.get(k)
    ]
    matched = any(value in endpoints for value in values)
    if matched and len(values) > 1:
        raise ValueError("ambiguous Phone CDP environment: set exactly one endpoint")
    if sys.argv[1:]:
        os.execvpe(argv[0], argv, os.environ)
    source = sys.stdin.read()
    if not matched:
        if re.search(r"(?m)^\s*# phone:", source):
            raise ValueError(
                "Phone directive requires an explicitly configured CDP endpoint"
            )
        exec_input(argv, source)
    name = os.environ.get("BU_NAME", "default")
    preamble = Path(config["preamble_file"]).expanduser().read_text()
    if name != "default" and (not preamble or not source.startswith(preamble)):
        raise RuntimeError(
            "named Phone calls require the exact configured upstream preamble"
        )
    if preamble and source.startswith(preamble):
        source = source[len(preamble) :]
    # Validate before starting a daemon; runtime repeats validation defensively.
    module = config["runtime_file"]
    spec = importlib.util.spec_from_file_location("phone_scheduler", module)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    runtime.preexisting_sessions(config)
    mode, _ = runtime.classify(source)
    name = os.environ.get("BU_NAME", "default")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or (
        mode in ("read", "finish") and name == "default"
    ):
        raise ValueError("read/finish requires a valid explicit named session")
    root = Path(config["state_dir"]).expanduser()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Serialize same-session daemon initialization before the harness starts.
    fd = os.open(
        root / ("session-" + runtime.key(name) + ".lock"), os.O_CREAT | os.O_RDWR, 0o600
    )
    runtime.acquire(fd, time.monotonic() + config.get("wait_seconds", 60))
    os.set_inheritable(fd, True)
    os.environ["PHONE_SESSION_LOCK_FD"] = str(fd)
    os.environ["BH_TAB_MARKER"] = "off"
    bootstrap = "\n".join(
        [
            "import importlib.util as _phone_import",
            '_phone_spec = _phone_import.spec_from_file_location("phone_scheduler", '
            + repr(module)
            + ")",
            "_phone_runtime = _phone_import.module_from_spec(_phone_spec)",
            "_phone_spec.loader.exec_module(_phone_runtime)",
            "_phone_runtime.execute(globals(), "
            + repr(source)
            + ", "
            + repr(config)
            + ")",
        ]
    )
    if mode == "read":
        read_python = config["read_python"]
        if not isinstance(read_python, str) or not Path(read_python).is_absolute():
            raise ValueError("read_python must be an absolute installed Python path")
        direct_bootstrap = (
            "import importlib.util, json, sys\n"
            "job = json.load(sys.stdin)\n"
            "spec = importlib.util.spec_from_file_location('phone_scheduler', job['config']['runtime_file'])\n"
            "runtime = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(runtime)\n"
            "runtime.direct_read(job['source'], job['config'])\n"
        )
        argv = [read_python, "-c", direct_bootstrap]
        bootstrap = json.dumps({"source": source, "config": config})
    # os.exec keeps subprocess.run's timeout aimed at the actual CLI process.
    exec_input(argv, bootstrap)


if __name__ == "__main__":
    main()
