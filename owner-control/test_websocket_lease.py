"""The VNC path must close independently of the browser bridge."""

import base64
import hashlib
import http.client
import http.server
import pathlib
import socket
import threading
import time
import unittest
from unittest.mock import patch

import owner_control


class WebsocketLeaseTests(unittest.TestCase):
    def test_websocket_is_denied_without_a_lease(self):
        with http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), owner_control.Handler
        ) as server:
            server.token = "A" * 32
            server.origin = "https://192.168.1.50:8443"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(owner_control, "current_lease", return_value=None):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_port
                    )
                    connection.request(
                        "GET",
                        "/websockify",
                        headers={
                            "Host": owner_control.HOST,
                            "X-Phone-Control": server.token,
                            "Origin": server.origin,
                        },
                    )
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()
                    connection.close()
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_existing_websocket_closes_at_expiry_without_bridge(self):
        self.assertIn(
            "reverse_proxy @websocket 127.0.0.1:8766",
            pathlib.Path(owner_control.__file__)
            .with_name("Caddyfile.template")
            .read_text(),
        )
        client, client_peer = socket.socketpair()
        upstream, upstream_peer = socket.socketpair()
        for sock in (client, client_peer, upstream, upstream_peer):
            self.addCleanup(sock.close)
            sock.settimeout(1)
        lease = {"lease_id": "a" * 32}
        expiry = time.monotonic() + 0.2
        with patch.object(owner_control, "current_lease", return_value=lease):
            worker = threading.Thread(
                target=owner_control.relay_websocket,
                args=(client, upstream, lease["lease_id"], expiry),
            )
            worker.start()
            client_peer.sendall(b"frame")
            self.assertEqual(upstream_peer.recv(5), b"frame")
            worker.join(timeout=1)
            self.assertFalse(
                worker.is_alive(), "VNC remained connected past lease expiry"
            )

    def test_upgraded_websocket_closes_when_lease_expires(self):
        with (
            socket.socket() as vnc,
            http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), owner_control.Handler
            ) as server,
        ):
            vnc.bind(("127.0.0.1", 0))
            vnc.listen()
            server.token = "A" * 32
            server.origin = "https://192.168.1.50:8443"
            lease = {"lease_id": "b" * 32, "expires_monotonic": time.monotonic() + 1}
            received = []

            def upstream():
                with vnc.accept()[0] as peer:
                    peer.settimeout(2)
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        request.extend(peer.recv(4096))
                    key = next(
                        line.split(": ", 1)[1]
                        for line in request.decode().split("\r\n")
                        if line.startswith("Sec-WebSocket-Key:")
                    )
                    accept = base64.b64encode(
                        hashlib.sha1(
                            (key + owner_control.WEBSOCKET_GUID).encode()
                        ).digest()
                    ).decode()
                    peer.sendall(
                        (
                            "HTTP/1.1 101 Switching Protocols\r\n"
                            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                        ).encode()
                    )
                    received.append(peer.recv(5))
                    time.sleep(1.5)

            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            upstream_thread = threading.Thread(target=upstream, daemon=True)
            server_thread.start()
            upstream_thread.start()
            try:
                with (
                    patch.object(owner_control, "VNC_PORT", vnc.getsockname()[1]),
                    patch.object(owner_control, "current_lease", return_value=lease),
                    socket.create_connection(
                        ("127.0.0.1", server.server_port)
                    ) as client,
                ):
                    client.settimeout(2)
                    client.sendall(
                        (
                            "GET /websockify HTTP/1.1\r\n"
                            f"Host: {owner_control.HOST}\r\n"
                            f"X-Phone-Control: {server.token}\r\n"
                            f"Origin: {server.origin}\r\n"
                            "Connection: Upgrade\r\nUpgrade: websocket\r\n"
                            "Sec-WebSocket-Version: 13\r\n"
                            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
                        ).encode()
                    )
                    response = bytearray()
                    while b"\r\n\r\n" not in response:
                        response.extend(client.recv(4096))
                    self.assertIn(b" 101 Switching Protocols", response)
                    client.sendall(b"frame")
                    time.sleep(0.1)
                    self.assertEqual(received, [b"frame"])
                    self.assertEqual(client.recv(1), b"")
            finally:
                server.shutdown()
                server_thread.join(timeout=2)
                upstream_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
