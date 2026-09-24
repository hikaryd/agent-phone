import pathlib
import unittest
from typing import ClassVar

from render_caddy import render


class RenderCaddyTests(unittest.TestCase):
    template = (
        "__LAN_IP__ __TERMUX_HOME__ __OWNER_USERNAME__ "
        "__OWNER_BCRYPT_HASH__ __CONTROL_TOKEN__"
    )
    control: ClassVar[dict[str, str]] = {
        "lan_ip": "192.168.1.50",
        "origin": "https://192.168.1.50:8443",
        "token": "A" * 32,
    }

    def test_bcrypt_dollar_signs_remain_literal(self):
        bcrypt_hash = "$2a$12$" + "A" * 53
        output = render(
            self.template,
            self.control,
            "owner",
            bcrypt_hash,
            pathlib.Path("/data/data/com.termux/files/home"),
        )
        self.assertIn(bcrypt_hash, output)
        self.assertNotIn("__", output)

    def test_foreign_origin_and_unrendered_markers_fail_closed(self):
        bcrypt_hash = "$2a$12$" + "A" * 53
        invalid = dict(self.control, origin="https://elsewhere.example")
        with self.assertRaises(ValueError):
            render(
                self.template,
                invalid,
                "owner",
                bcrypt_hash,
                pathlib.Path("/data/data/com.termux/files/home"),
            )
        with self.assertRaises(ValueError):
            render(
                self.template + " __UNKNOWN__",
                self.control,
                "owner",
                bcrypt_hash,
                pathlib.Path("/data/data/com.termux/files/home"),
            )


if __name__ == "__main__":
    unittest.main()
