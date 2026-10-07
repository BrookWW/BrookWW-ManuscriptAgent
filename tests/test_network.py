import socket
import json
import errno
import http.client
import io
import sys
import threading
import time
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from network import (CodexProxy, ProxyPolicyError, _doh_query, _FixedDoHConnection,
                     parse_destination, resolve_destination, resolve_doh, resolve_public)


def answer(address, family=socket.AF_INET):
    endpoint = (address, 443) if family == socket.AF_INET else (address, 443, 0, 0)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", endpoint)


class DestinationTests(unittest.TestCase):
    def test_exact_hosts_and_port_only(self):
        for host in ("api.openai.com", "chatgpt.com", "auth.openai.com"):
            self.assertEqual(parse_destination(host + ":443"), host)
        self.assertEqual(parse_destination("API.OPENAI.COM:443"), "api.openai.com")
        for target in ("localhost:443", "127.0.0.1:443", "2130706433:443",
                       "0x7f000001:443", "[::1]:443", "api.openai.com:80",
                       "api.openai.com:0443", "api.openai.com:", "api.openai.com",
                       "api.openai.com.:443", "api.openai.com.evil.example:443",
                       "evil.api.openai.com:443", "ab.chatgpt.com:443",
                       "https://api.openai.com:443", "api.openai.com:443/path",
                       "user@api.openai.com:443", "api.openai.com@localhost:443",
                       "api.openai.com:443?x", "api.openai.com:443\n",
                       "аpi.openai.com:443", "api。openai.com:443"):
            with self.subTest(target=target), self.assertRaises(ProxyPolicyError):
                parse_destination(target)

    @mock.patch("network.socket.getaddrinfo")
    def test_all_dns_answers_must_be_public(self, resolver):
        unsafe = [("127.0.0.1", socket.AF_INET), ("10.0.0.1", socket.AF_INET),
                  ("169.254.169.254", socket.AF_INET), ("192.168.1.1", socket.AF_INET),
                  ("100.64.0.1", socket.AF_INET), ("224.0.0.1", socket.AF_INET),
                  ("0.0.0.0", socket.AF_INET), ("240.0.0.1", socket.AF_INET),
                  ("::1", socket.AF_INET6), ("fe80::1", socket.AF_INET6),
                  ("ff02::1", socket.AF_INET6), ("fc00::1", socket.AF_INET6),
                  ("::", socket.AF_INET6), ("::ffff:127.0.0.1", socket.AF_INET6),
                  ("64:ff9b::7f00:1", socket.AF_INET6), ("2002:7f00:1::", socket.AF_INET6),
                  ("fe80::1%lo0", socket.AF_INET6)]
        for address, family in unsafe:
            resolver.return_value = [answer("1.1.1.1"), answer(address, family)]
            with self.subTest(address=address), self.assertRaises(ProxyPolicyError):
                resolve_public("api.openai.com")

    @mock.patch("network.socket.getaddrinfo")
    def test_public_dns_results_are_deduplicated(self, resolver):
        resolver.return_value = [answer("1.1.1.1"), answer("1.1.1.1"),
                                 answer("2606:4700::1111", socket.AF_INET6)]
        self.assertEqual(resolve_public("api.openai.com"),
                         [(socket.AF_INET, ("1.1.1.1", 443)),
                          (socket.AF_INET6, ("2606:4700::1111", 443, 0, 0))])
        resolver.assert_called_once_with("api.openai.com", 443,
                                         type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)

    @mock.patch("network.socket.getaddrinfo")
    def test_empty_dns_and_unallowlisted_host_fail_closed(self, resolver):
        resolver.return_value = []
        with self.assertRaises(ProxyPolicyError):
            resolve_public("api.openai.com")
        resolver.reset_mock()
        with self.assertRaises(ProxyPolicyError):
            resolve_public("localhost")
        resolver.assert_not_called()

    @mock.patch("network.socket.socket")
    @mock.patch("network.socket.getaddrinfo")
    def test_connect_uses_the_checked_numeric_endpoint(self, resolver, socket_factory):
        resolver.return_value = [answer("1.1.1.1")]
        connection = socket_factory.return_value
        proxy = CodexProxy()
        self.addCleanup(proxy.close)
        self.assertIs(proxy._connect("api.openai.com"), connection)
        connection.connect.assert_called_once_with(("1.1.1.1", 443))
        resolver.assert_called_once()

    @mock.patch("network.resolve_doh", side_effect=ProxyPolicyError("Unsafe DNS"))
    @mock.patch("network.socket.socket")
    @mock.patch("network.socket.getaddrinfo")
    def test_rebinding_on_next_connection_is_rechecked(self, resolver, socket_factory, _doh):
        resolver.side_effect = [[answer("1.1.1.1")], [answer("127.0.0.1")]]
        proxy = CodexProxy()
        self.addCleanup(proxy.close)
        proxy._connect("api.openai.com")
        with self.assertRaises(ProxyPolicyError):
            proxy._connect("api.openai.com")
        self.assertEqual(socket_factory.call_count, 1)


class DnsOverHttpsTests(unittest.TestCase):
    @staticmethod
    def message(kind, addresses):
        return {"Status": 0, "TC": False,
                "Question": [{"name": "api.openai.com.", "type": kind}],
                "Answer": [{"name": "api.openai.com.", "type": kind, "data": value}
                           for value in addresses]}

    @mock.patch("network.resolve_doh")
    @mock.patch("network.socket.getaddrinfo")
    def test_private_system_gateway_is_discarded_for_verified_public_lookup(self, resolver, doh):
        resolver.return_value = [answer("172.19.0.15")]
        doh.return_value = [(socket.AF_INET, ("1.1.1.1", 443))]
        self.assertEqual(resolve_destination("api.openai.com"), doh.return_value)
        doh.assert_called_once_with("api.openai.com")

    @mock.patch("network.resolve_doh")
    @mock.patch("network.socket.getaddrinfo")
    def test_public_system_answers_need_no_fallback(self, resolver, doh):
        resolver.return_value = [answer("1.1.1.1")]
        self.assertEqual(resolve_destination("api.openai.com"), [(socket.AF_INET, ("1.1.1.1", 443))])
        doh.assert_not_called()

    @mock.patch("network._doh_query")
    def test_doh_accepts_public_ipv4_and_ipv6(self, query):
        query.side_effect = [self.message(1, ["1.1.1.1"]), self.message(28, ["2606:4700::1111"])]
        self.assertEqual(resolve_doh("api.openai.com"),
                         [(socket.AF_INET, ("1.1.1.1", 443)),
                          (socket.AF_INET6, ("2606:4700::1111", 443, 0, 0))])
        self.assertEqual(query.call_args_list, [mock.call("api.openai.com", 1), mock.call("api.openai.com", 28)])

    @mock.patch("network._doh_query")
    def test_doh_private_or_mismatched_answers_are_rejected(self, query):
        for message in (self.message(1, ["1.1.1.1", "127.0.0.1"]),
                        self.message(1, ["172.19.0.15"]), self.message(1, ["::1"]),
                        {**self.message(1, ["1.1.1.1"]), "Status": 3},
                        {**self.message(1, ["1.1.1.1"]), "TC": True},
                        {**self.message(1, ["1.1.1.1"]), "Question": [{"name": "localhost.", "type": 1}]},
                        {**self.message(1, ["1.1.1.1"]), "Question": [{"name": 123, "type": 1}]},
                        {**self.message(1, ["1.1.1.1"]), "Answer": "malformed"}):
            query.return_value = message
            with self.subTest(message=message), self.assertRaises(ProxyPolicyError):
                resolve_doh("api.openai.com")

    @mock.patch("network._FixedDoHConnection")
    def test_doh_request_has_fixed_path_type_and_no_redirects(self, factory):
        connection = factory.return_value
        response = connection.getresponse.return_value
        response.status = 200
        response.isclosed.return_value = False
        response.length = None
        response.getheader.side_effect = lambda name, default="": {"Content-Type": "application/dns-json"}.get(name, default)
        message = self.message(1, ["1.1.1.1"])
        response.read1.side_effect = [json.dumps(message).encode(), b""]
        self.assertEqual(_doh_query("api.openai.com", 1), message)
        connection.request.assert_called_once_with("GET", "/dns-query?name=api.openai.com&type=1",
                    headers={"Accept": "application/dns-json", "Connection": "close"})
        connection.close.assert_called_once()
        response.status = 302
        with self.assertRaises(ProxyPolicyError):
            _doh_query("api.openai.com", 1)

    @mock.patch("network._FixedDoHConnection")
    def test_doh_response_is_bounded_and_closed_on_failure(self, factory):
        connection = factory.return_value
        response = connection.getresponse.return_value
        response.status = 200
        response.isclosed.return_value = False
        response.length = None
        response.getheader.side_effect = lambda name, default="": {"Content-Type": "application/dns-json"}.get(name, default)
        response.read1.side_effect = [b"x" * 4096] * 16 + [b"x"]
        with self.assertRaisesRegex(ProxyPolicyError, "size limit"):
            _doh_query("api.openai.com", 1)
        response.close.assert_called_once()
        connection.close.assert_called_once()

    @mock.patch("network._FixedDoHConnection")
    def test_doh_stops_before_touching_transport_closed_at_content_length(self, factory):
        message = self.message(1, ["1.1.1.1"])
        body = json.dumps(message).encode()
        wire = (b"HTTP/1.1 200 OK\r\nContent-Type: application/dns-json\r\n"
                b"Connection: close\r\nContent-Length: " + str(len(body)).encode()
                + b"\r\n\r\n" + body)
        source = mock.Mock()
        source.makefile.return_value = io.BytesIO(wire)
        response = http.client.HTTPResponse(source)
        response.begin()
        connection = factory.return_value
        connection.getresponse.return_value = response

        def timeout_only_while_open(_remaining):
            if response.isclosed():
                raise OSError(errno.EBADF, "Bad file descriptor")

        connection.sock.settimeout.side_effect = timeout_only_while_open
        self.assertEqual(_doh_query("api.openai.com", 1), message)
        self.assertTrue(response.isclosed())
        connection.sock.settimeout.assert_called_once()
        connection.close.assert_called_once()

    @mock.patch("network._FixedDoHConnection")
    def test_doh_cannot_be_used_as_an_arbitrary_resolver(self, factory):
        for host, kind in (("localhost", 1), ("example.com", 1), ("api.openai.com", 16)):
            with self.subTest(host=host, kind=kind), self.assertRaises(ProxyPolicyError):
                _doh_query(host, kind)
        factory.assert_not_called()

    @mock.patch("public_dns.time.monotonic", side_effect=[0, 6])
    @mock.patch("network._FixedDoHConnection")
    def test_doh_body_deadline_still_raises_transport_timeout(self, factory, _clock):
        response = factory.return_value.getresponse.return_value
        response.status = 200
        response.isclosed.return_value = False
        response.length = None
        response.getheader.side_effect = lambda name, default="": {"Content-Type": "application/dns-json"}.get(name, default)
        with self.assertRaises(TimeoutError):
            _doh_query("api.openai.com", 1)
        response.read1.assert_not_called()
        response.close.assert_called_once()
        factory.return_value.close.assert_called_once()

    @mock.patch("public_dns.ssl.create_default_context")
    @mock.patch("network.socket.socket")
    def test_doh_connects_fixed_numeric_endpoint_and_verifies_tls_name(self, factory, tls):
        connection = _FixedDoHConnection()
        connection.connect()
        factory.return_value.connect.assert_called_once_with(("1.1.1.1", 443))
        tls.return_value.wrap_socket.assert_called_once_with(factory.return_value, server_hostname="1.1.1.1")
        self.assertEqual(connection.host, "1.1.1.1")
        connection.close()


class RelayTests(unittest.TestCase):
    def request(self, proxy, request):
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=2) as connection:
            connection.sendall(request)
            return connection.recv(4096)

    @mock.patch("network.socket.getaddrinfo")
    def test_loopback_relay_denies_invalid_requests_without_resolving(self, resolver):
        # Avoid the patched resolver for the client's numeric connection.
        def request(proxy, payload):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(("127.0.0.1", proxy.port))
                connection.sendall(payload)
                return connection.recv(4096)

        with CodexProxy() as proxy:
            self.assertEqual(proxy.url, f"http://127.0.0.1:{proxy.port}")
            for payload in (
                    b"GET http://localhost/private HTTP/1.1\r\n\r\n",
                    b"CONNECT localhost:443 HTTP/1.1\r\n\r\n",
                    b"CONNECT api.openai.com:80 HTTP/1.1\r\n\r\n",
                    b"CONNECT api.openai.com:443 HTTP/1.1\r\nContent-Length: 0\r\n\r\n",
                    b"CONNECT api.openai.com:443 HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
                    b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: localhost:443\r\n\r\n",
                    b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\nHost: api.openai.com:443\r\n\r\n",
                    b"CONNECT api.openai.com:443 HTTP/1.1\r\nX-Header: value\nInjected: value\r\n\r\n",
                    b"CONNECT api.openai.com:443 HTTP/1.1\r\n\r\nbody"):
                with self.subTest(payload=payload):
                    reply = request(proxy, payload)
                    self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply)
                    self.assertNotIn(b"Server:", reply)
        resolver.assert_not_called()

    def test_close_stops_listener_and_active_header_reader(self):
        with CodexProxy(header_timeout=30) as proxy:
            connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=2)
            self.addCleanup(connection.close)
            connection.sendall(b"CONNECT api.openai.com:443 HTTP/1.1\r\n")
            deadline = time.monotonic() + 1
            while not proxy._workers and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertFalse(proxy._thread.is_alive())
        self.assertFalse(proxy._workers)
        proxy.close()  # Idempotent.
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", proxy.port), timeout=0.2)

    def test_connection_limit_rejects_additional_clients(self):
        with CodexProxy(max_connections=1, header_timeout=5) as proxy:
            first = socket.create_connection(("127.0.0.1", proxy.port), timeout=2)
            self.addCleanup(first.close)
            first.sendall(b"CONNECT api.openai.com:443 HTTP/1.1\r\n")
            deadline = time.monotonic() + 1
            while not proxy._workers and time.monotonic() < deadline:
                time.sleep(0.01)
            reply = self.request(proxy, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
            self.assertTrue(reply.startswith(b"HTTP/1.1 503"), reply)

    def test_connect_success_uses_tunnel_without_http_banner(self):
        remote, upstream = socket.socketpair()
        self.addCleanup(remote.close)
        self.addCleanup(upstream.close)
        remote.settimeout(2)
        with CodexProxy() as proxy, mock.patch.object(proxy, "_connect", return_value=upstream):
            proxy._track(upstream)
            with socket.create_connection(("127.0.0.1", proxy.port), timeout=2) as client:
                client.sendall(b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\n\r\n")
                self.assertEqual(client.recv(4096), b"HTTP/1.1 200 Connection Established\r\n\r\n")
                client.sendall(b"opaque TLS bytes")
                self.assertEqual(remote.recv(4096), b"opaque TLS bytes")
                remote.sendall(b"opaque response")
                self.assertEqual(client.recv(4096), b"opaque response")
        self.assertFalse(proxy._workers)

    def test_idle_tunnel_expires(self):
        client, local = socket.socketpair()
        remote, upstream = socket.socketpair()
        for connection in (client, local, remote, upstream):
            self.addCleanup(connection.close)
        proxy = CodexProxy(idle_timeout=0.02)
        self.addCleanup(proxy.close)
        worker = threading.Thread(target=proxy._relay, args=(local, upstream))
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_tunnel_relays_both_directions_and_preserves_half_close(self):
        client, local = socket.socketpair()
        remote, upstream = socket.socketpair()
        for connection in (client, local, remote, upstream):
            self.addCleanup(connection.close)
            connection.settimeout(2)
        proxy = CodexProxy(idle_timeout=2)
        self.addCleanup(proxy.close)
        worker = threading.Thread(target=proxy._relay, args=(local, upstream))
        worker.start()
        try:
            client.sendall(b"request")
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(remote.recv(4096), b"request")
            self.assertEqual(remote.recv(4096), b"")
            remote.sendall(b"response")
            remote.shutdown(socket.SHUT_WR)
            self.assertEqual(client.recv(4096), b"response")
            self.assertEqual(client.recv(4096), b"")
        finally:
            proxy.close()
            worker.join(2)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
