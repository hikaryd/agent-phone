"""Initial root-only VPS setup for one dedicated Chrome CDP reverse tunnel."""

import argparse
import os
import pathlib
import pwd
import re
import subprocess


def public_key(text):
    parts = text.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("Expected an Ed25519 public key")
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", parts[1]):
        raise ValueError("Invalid public key encoding")
    return " ".join(parts[:2])


def verify_effective(client_address):
    expected = {
        "authenticationmethods": "publickey",
        "passwordauthentication": "no",
        "kbdinteractiveauthentication": "no",
        "allowtcpforwarding": "remote",
        "allowstreamlocalforwarding": "no",
        "permitlisten": "127.0.0.1:19222",
        "permitopen": "none",
        "gatewayports": "no",
        "permittty": "no",
        "x11forwarding": "no",
        "allowagentforwarding": "no",
        "permituserrc": "no",
        "maxsessions": "0",
    }
    output = subprocess.check_output(
        [
            "/usr/sbin/sshd", "-T", "-C",
            f"user=browser-link,host={client_address},addr={client_address}",
        ],
        text=True,
    )
    actual = dict(line.split(None, 1) for line in output.splitlines() if " " in line)
    for name, value in expected.items():
        if actual.get(name) != value:
            raise RuntimeError(f"Unsafe effective {name}; key not granted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--link-key", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Requires VPS root")
    if "SSH_CONNECTION" not in os.environ:
        raise SystemExit("Run over SSH to verify effective Match rules")
    client_address = os.environ["SSH_CONNECTION"].split()[0]
    os.umask(0o077)
    link = public_key(args.link_key.read_text())
    target = pathlib.Path("/etc/ssh/sshd_config.d/61-agent-phone-browser.conf")
    if target.exists():
        raise SystemExit("Browser SSH config already exists; inspect instead of overwrite")
    try:
        pwd.getpwnam("browser-link")
    except KeyError:
        pass
    else:
        raise SystemExit("Browser identity already exists; inspect instead of overwrite")
    listeners = subprocess.check_output(["ss", "-H", "-ltn"], text=True)
    if any(line.split()[3].endswith(":19222") for line in listeners.splitlines()):
        raise SystemExit("Port 19222 already in use")
    target.write_text(pathlib.Path(__file__).with_name("vps-sshd.conf").read_text())
    try:
        subprocess.run(["/usr/sbin/sshd", "-t"], check=True)
    except subprocess.CalledProcessError:
        target.unlink()
        raise
    subprocess.run(["systemctl", "reload", "ssh"], check=True)
    subprocess.run(
        ["useradd", "--create-home", "--shell", "/usr/sbin/nologin", "browser-link"],
        check=True,
    )
    verify_effective(client_address)
    entry = pwd.getpwnam("browser-link")
    folder = pathlib.Path(entry.pw_dir) / ".ssh"
    folder.mkdir(mode=0o700)
    auth = folder / "authorized_keys"
    auth.write_text(
        'restrict,port-forwarding,permitlisten="127.0.0.1:19222",'
        'command="/usr/bin/false" ' + link + "\n"
    )
    auth.chmod(0o600)
    for path in (folder, auth):
        os.chown(path, entry.pw_uid, entry.pw_gid)
    print("BROWSER_LINK_INSTALLED")


if __name__ == "__main__":
    main()
