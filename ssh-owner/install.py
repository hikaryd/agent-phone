"""Run inside Termux. Inputs are public keys; private keys never leave Phone."""

import argparse
import ipaddress
import json
import os
import pathlib
import re
import shutil
import subprocess


def public_key(text):
    parts = text.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("Expected an Ed25519 public key")
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", parts[1]):
        raise ValueError("Invalid public key encoding")
    return " ".join(parts[:2])


def config(home, username, lan):
    if not re.fullmatch(r"u0_a[0-9]+", username):
        raise ValueError("Unexpected Termux user")
    address = ipaddress.IPv4Address(lan)
    if (
        not address.is_private
        or address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    ):
        raise ValueError("Expected exact private LAN bind address")
    return f"""Port 8022
AddressFamily inet
ListenAddress 127.0.0.1
ListenAddress {lan}
HostKey {home}/host-key
PidFile {home}/sshd.pid
AuthorizedKeysFile {home}/authorized_keys
AllowUsers {username}
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PermitRootLogin no
StrictModes yes
DisableForwarding yes
PermitUserEnvironment no
PermitUserRC no
LoginGraceTime 20
MaxAuthTries 3
MaxStartups 3:30:10
LogLevel ERROR
Subsystem sftp internal-sftp
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-key", type=pathlib.Path, required=True)
    parser.add_argument("--vps-host-key", type=pathlib.Path, required=True)
    parser.add_argument("--inventory", type=pathlib.Path, required=True)
    args = parser.parse_args()
    from supervisor import validate_inventory

    inventory = validate_inventory(json.loads(args.inventory.read_text()))
    os.umask(0o077)
    home = pathlib.Path.home() / "phone-owner"
    # Fail closed on reinstall: do not replace an owner's live keys/config.
    home.mkdir(mode=0o700)
    owner = public_key(args.owner_key.read_text())
    vps = public_key(args.vps_host_key.read_text())
    username = subprocess.check_output(["whoami"], text=True).strip()
    for name in ("host-key", "link-key"):
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "phone-owner-" + name,
                "-f",
                str(home / name),
            ],
            check=True,
        )
    (home / "authorized_keys").write_text(owner + " phone-owner\n")
    (home / "known_hosts").write_text(inventory["vps_host"] + " " + vps + "\n")
    (home / "inventory.json").write_text(json.dumps(inventory))
    (home / "sshd_config").write_text(config(home, username, inventory["lan_ip"]))
    subprocess.run(["sshd", "-t", "-f", str(home / "sshd_config")], check=True)
    source = pathlib.Path(__file__).parent
    shutil.copyfile(source / "supervisor.py", home / "supervisor.py")
    boot = pathlib.Path.home() / ".termux/boot/21-phone-owner-ssh"
    boot.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if boot.exists():
        raise RuntimeError("Owner boot entry already exists")
    shutil.copyfile(source / boot.name, boot)
    boot.chmod(0o700)
    descriptor = {
        "user": username,
        "host_key": public_key((home / "host-key.pub").read_text()),
        "link_key": public_key((home / "link-key.pub").read_text()),
    }
    (home / "public.json").write_text(json.dumps(descriptor))
    subprocess.Popen(
        [str(boot)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("PHONE_OWNER_SSH_INSTALLED", flush=True)
    subprocess.run(["ssh-keygen", "-lf", str(home / "host-key.pub")], check=True)


if __name__ == "__main__":
    main()
