"""Initial root-only VPS install. Keys are public; existing identities are refused."""

import argparse
import ipaddress
import os
import pathlib
import pwd
import subprocess

from install import public_key


def verify_effective(user, client_address):
    expected = {
        "authenticationmethods": "publickey",
        "passwordauthentication": "no",
        "kbdinteractiveauthentication": "no",
        "allowstreamlocalforwarding": "no",
        "gatewayports": "no",
        "permittty": "no",
        "x11forwarding": "no",
        "allowagentforwarding": "no",
        "permituserrc": "no",
        "maxsessions": "0",
    }
    if user == "phone-owner-link":
        expected.update(
            allowtcpforwarding="remote",
            permitlisten="127.0.0.1:19223",
            permitopen="none",
        )
    elif user == "phone-owner-jump":
        expected.update(
            allowtcpforwarding="local",
            permitlisten="none",
            permitopen="127.0.0.1:19223",
        )
    else:
        raise ValueError("Unexpected identity")
    output = subprocess.check_output(
        [
            "/usr/sbin/sshd",
            "-T",
            "-C",
            f"user={user},host={client_address},addr={client_address}",
        ],
        text=True,
    )
    actual = dict(line.split(None, 1) for line in output.splitlines() if " " in line)
    for name, value in expected.items():
        if actual.get(name) != value:
            raise RuntimeError(f"Unsafe effective {name} for {user}; keys not granted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-key", type=pathlib.Path, required=True)
    parser.add_argument("--link-key", type=pathlib.Path, required=True)
    args = parser.parse_args()
    client_address = str(ipaddress.ip_address(os.environ["SSH_CONNECTION"].split()[0]))
    if os.geteuid() != 0:
        raise SystemExit("Requires VPS root")
    os.umask(0o077)
    owner = public_key(args.owner_key.read_text())
    link = public_key(args.link_key.read_text())
    target = pathlib.Path("/etc/ssh/sshd_config.d/60-phone-owner.conf")
    if target.exists():
        raise SystemExit(
            "Owner SSH config already exists; inspect instead of overwrite"
        )
    for user in ("phone-owner-link", "phone-owner-jump"):
        try:
            pwd.getpwnam(user)
        except KeyError:
            continue
        raise SystemExit("Owner identity already exists; inspect instead of overwrite")
    # Refuse to capture a port already owned by an unrelated service.
    listeners = subprocess.check_output(["ss", "-H", "-ltn"], text=True)
    if any(line.split()[3].endswith(":19223") for line in listeners.splitlines()):
        raise SystemExit("Port 19223 already in use")
    target.write_text(pathlib.Path(__file__).with_name("vps-sshd.conf").read_text())
    try:
        subprocess.run(["/usr/sbin/sshd", "-t"], check=True)
    except subprocess.CalledProcessError:
        target.unlink()
        raise
    # Load restrictions before granting a key to either new account.
    subprocess.run(["systemctl", "reload", "ssh"], check=True)
    grants = (
        ("phone-owner-link", link, 'permitlisten="127.0.0.1:19223"'),
        ("phone-owner-jump", owner, 'permitopen="127.0.0.1:19223"'),
    )
    for user, _key, _constraint in grants:
        subprocess.run(
            ["useradd", "--create-home", "--shell", "/usr/sbin/nologin", user],
            check=True,
        )
    # Resolve new users/groups and prove all restrictions before either grant.
    for user, _key, _constraint in grants:
        verify_effective(user, client_address)
    for user, key, constraint in grants:
        entry = pwd.getpwnam(user)
        folder = pathlib.Path(entry.pw_dir) / ".ssh"
        folder.mkdir(mode=0o700)
        auth = folder / "authorized_keys"
        auth.write_text(
            f'restrict,port-forwarding,{constraint},command="/usr/bin/false" {key}\n'
        )
        auth.chmod(0o600)
        for path in (folder, auth):
            os.chown(path, entry.pw_uid, entry.pw_gid)
    print("PHONE_OWNER_VPS_INSTALLED")


if __name__ == "__main__":
    main()
