"""Render the owner gateway without shell expansion of bcrypt hashes or tokens."""

import argparse
import ipaddress
import json
import os
import pathlib
import re


def render(template, control, username, bcrypt_hash, termux_home):
    address = ipaddress.IPv4Address(control["lan_ip"])
    if (
        not address.is_private or address.is_loopback or address.is_link_local
        or address.is_unspecified or address.is_reserved or address.is_multicast
    ):
        raise ValueError("Expected a private LAN address")
    if control["origin"] != f"https://{address}:8443":
        raise ValueError("Origin does not match the LAN address")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", username):
        raise ValueError("Invalid owner username")
    if not re.fullmatch(r"\$2[aby]\$[0-9]{2}\$[A-Za-z0-9./]{53}", bcrypt_hash):
        raise ValueError("Expected one bcrypt hash")
    token = control["token"]
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
        raise ValueError("Invalid owner token")
    if (
        not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(termux_home))
        or ".." in termux_home.parts
    ):
        raise ValueError("Invalid Termux home path")
    values = {
        "__LAN_IP__": str(address),
        "__TERMUX_HOME__": str(termux_home),
        "__OWNER_USERNAME__": username,
        "__OWNER_BCRYPT_HASH__": bcrypt_hash,
        "__CONTROL_TOKEN__": token,
    }
    for marker, value in values.items():
        if marker not in template:
            raise ValueError(f"Missing template marker {marker}")
        template = template.replace(marker, value)
    if re.search(r"__[A-Z_]+__", template):
        raise ValueError("Unrendered template marker")
    return template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", type=pathlib.Path, required=True)
    parser.add_argument("--hash-file", type=pathlib.Path, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--termux-home", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    source = pathlib.Path(__file__).with_name("Caddyfile.template").read_text()
    content = render(
        source,
        json.loads(args.control.read_text()),
        args.username,
        args.hash_file.read_text().strip(),
        args.termux_home,
    )
    os.umask(0o077)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        file.write(content)


if __name__ == "__main__":
    main()
