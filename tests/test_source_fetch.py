import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import source_fetch
from source_fetch import SourceFetcher, SourceFetchError, checked_url
from source_tools import fetch_source, SourceToolError


def response(body=b"paper", status=200, headers=None):
    headers = headers or {"Content-Type": "text/plain"}
    wire = f"HTTP/1.1 {status} Test\r\n".encode()
    wire += b"".join(f"{key}: {value}\r\n".encode() for key, value in headers.items())
    wire += b"Connection: close\r\n\r\n" + body
    transport = mock.Mock()
    transport.makefile.return_value = io.BytesIO(wire)
    result = http.client.HTTPResponse(transport)
    result.begin()
    return result


class PublicSourceTests(unittest.TestCase):
    def test_urls_reject_local_schemes_credentials_and_ambiguous_hosts(self):
        for value in ("http://zbmath.org/a", "file:///tmp/main.tex", "https://localhost/a",
                      "https://localhost./a", "https://127.0.0.1/a", "https://[::1]/a",
                      "https://[::ffff:127.0.0.1]/a", "https://10.0.0.1/a", "https://2130706433/a",
                      "https://169.254.169.254/latest", "https://example.com:8080/a",
                      "https://name:secret@example.com/a", "https://example.com\\@localhost/a",
                      "https://example.com/\r\nInjected", "https://example.local/a",
                      "https://example.com/%x y", "https://example.com../a",
                      "https://example.com:bad/a", "//example.com/a"):
            with self.subTest(value=value), self.assertRaises(SourceFetchError):
                checked_url(value)

    def test_url_normalizes_hostname_and_encodes_unicode_path(self):
        self.assertEqual(checked_url("https://ZBMATH.org:443/é?x=1#part"),
                         ("https://zbmath.org/%C3%A9?x=1", "zbmath.org", "/%C3%A9?x=1"))

    @mock.patch("source_fetch._FixedDoHConnection")
    def test_doh_is_fixed_public_and_rejects_entire_mixed_answer(self, factory):
        for address in ("127.0.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                        "::1", "fc00::1", "::ffff:127.0.0.1", "2002:7f00:1::"):
            message = {"Status": 0, "TC": False, "Question": [{"name": "example.com.", "type": 1}],
                       "Answer": [{"type": 1, "data": "1.1.1.1"}, {"type": 1, "data": address}]}
            factory.return_value.getresponse.return_value = response(json.dumps(message).encode(), headers={"Content-Type": "application/dns-json"})
            with self.subTest(address=address), self.assertRaises(SourceFetchError):
                source_fetch.resolve_public("example.com", time.monotonic() + 5)
        factory.return_value.request.assert_called_with("GET", "/dns-query?name=example.com&type=1",
                headers={"Accept": "application/dns-json", "Connection": "close"})

    @mock.patch("source_fetch._FixedDoHConnection")
    def test_doh_rejects_mismatched_question_redirect_and_empty_answer(self, factory):
        for result in (
            response(status=302, headers={"Location": "https://localhost/"}),
            response(json.dumps({"Status": 0, "TC": False, "Question": [{"name": "evil.com", "type": 1}]}).encode(), headers={"Content-Type": "application/dns-json"}),
            response(json.dumps({"Status": 3, "TC": False, "Question": [{"name": "example.com", "type": 1}]}).encode(), headers={"Content-Type": "application/dns-json"}),
        ):
            factory.return_value.getresponse.return_value = result
            with self.assertRaises(SourceFetchError):
                source_fetch.resolve_public("example.com", time.monotonic() + 5)

    @mock.patch("source_fetch._FixedDoHConnection")
    def test_doh_returns_pinned_public_ipv4_ipv6(self, factory):
        messages = [{"Status": 0, "TC": False, "Question": [{"name": "example.com", "type": kind}],
                     "Answer": [{"type": 5, "data": "cdn.example.com"}, {"type": kind, "data": ip}]}
                    for kind, ip in [(1, "1.1.1.1"), (28, "2606:4700::1111")]]
        factory.return_value.getresponse.side_effect = [response(json.dumps(m).encode(), headers={"Content-Type": "application/dns-json"}) for m in messages]
        self.assertEqual(source_fetch.resolve_public("example.com", time.monotonic() + 5),
                         [(socket.AF_INET, ("1.1.1.1", 443)), (socket.AF_INET6, ("2606:4700::1111", 443, 0, 0))])

    @mock.patch("source_fetch._FixedDoHConnection")
    def test_doh_error_codes_and_connection_cleanup_are_preserved(self, factory):
        cases = [(b"not JSON", "dns_failure", True),
                 (b"[]", "policy_rejected", False),
                 (b"x" * 65537, "response_limit", False)]
        for body, code, retryable in cases:
            with self.subTest(code=code):
                connection = factory.return_value = mock.Mock()
                result = response(body, headers={"Content-Type": "application/dns-json"})
                connection.getresponse.return_value = result
                with self.assertRaises(SourceFetchError) as raised:
                    source_fetch.resolve_public("example.com", time.monotonic() + 5)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertTrue(result.isclosed())
                connection.close.assert_called_once()

    @mock.patch("source_fetch._FixedDoHConnection")
    def test_doh_expired_deadline_does_not_start_connection(self, factory):
        with self.assertRaises(SourceFetchError) as raised:
            source_fetch.resolve_public("example.com", time.monotonic() - 1)
        self.assertEqual(raised.exception.code, "timeout")
        self.assertTrue(raised.exception.retryable)
        factory.assert_not_called()

    @mock.patch("public_dns.threading.Timer")
    @mock.patch("public_dns.time.monotonic", side_effect=[0, 0, 1, 3])
    @mock.patch("source_fetch._FixedDoHConnection")
    def test_doh_body_uses_source_deadline_and_cancels_timer(self, factory, _clock, timer):
        result = response(b"{}", headers={"Content-Type": "application/dns-json"})
        factory.return_value.getresponse.return_value = result
        with self.assertRaises(SourceFetchError) as raised:
            source_fetch.resolve_public("example.com", 2)
        self.assertEqual(raised.exception.code, "timeout")
        self.assertTrue(raised.exception.retryable)
        self.assertTrue(result.isclosed())
        factory.return_value.close.assert_called_once()
        timer.return_value.cancel.assert_called_once()

    @mock.patch("public_dns.threading.Timer")
    @mock.patch("source_fetch._FixedDoHConnection")
    def test_doh_deadline_interrupts_transport_and_retains_dns_failure_code(self, factory, timer):
        connection = factory.return_value

        def interrupted_request(*_args, **_kwargs):
            # Fire the scheduled deadline while the transport is blocked.
            _, abort = timer.call_args.args
            abort(*timer.call_args.kwargs["args"])
            raise ConnectionResetError("interrupted")

        connection.request.side_effect = interrupted_request
        with self.assertRaises(SourceFetchError) as raised:
            source_fetch.resolve_public("example.com", time.monotonic() + 1)
        self.assertEqual(raised.exception.code, "dns_failure")
        self.assertTrue(raised.exception.retryable)
        connection.sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        connection.sock.close.assert_called_once()
        connection.close.assert_called_once()
        timer.return_value.cancel.assert_called_once()

    @mock.patch("source_fetch._FixedDoHConnection")
    def test_doh_invalid_second_family_discards_first_family(self, factory):
        messages = [{"Status": 0, "TC": False, "Question": [{"name": "example.com", "type": kind}],
                     "Answer": [{"type": kind, "data": address}]}
                    for kind, address in [(1, "1.1.1.1"), (28, "1.1.1.1")]]
        factory.return_value.getresponse.side_effect = [
            response(json.dumps(message).encode(), headers={"Content-Type": "application/dns-json"})
            for message in messages]
        with self.assertRaises(SourceFetchError) as raised:
            source_fetch.resolve_public("example.com", time.monotonic() + 5)
        self.assertEqual(raised.exception.code, "policy_rejected")
        self.assertFalse(raised.exception.retryable)

    @mock.patch("source_fetch.ssl.create_default_context")
    @mock.patch("source_fetch.socket.socket")
    def test_connection_pins_numeric_ip_but_verifies_original_hostname(self, socket_factory, context_factory):
        connection = source_fetch._PinnedHTTPSConnection("zbmath.org", (socket.AF_INET, ("1.1.1.1", 443)), 2)
        connection.connect()
        socket_factory.return_value.connect.assert_called_once_with(("1.1.1.1", 443))
        context_factory.return_value.wrap_socket.assert_called_once_with(socket_factory.return_value, server_hostname="zbmath.org")
        self.assertEqual(context_factory.return_value.minimum_version, ssl.TLSVersion.TLSv1_2)

    @mock.patch("source_fetch.socket.socket")
    def test_connection_rechecks_unsafe_endpoint_before_opening_socket(self, socket_factory):
        connection = source_fetch._PinnedHTTPSConnection("example.com", (socket.AF_INET, ("127.0.0.1", 443)), 2)
        with self.assertRaises(SourceFetchError):
            connection.connect()
        socket_factory.assert_not_called()

    def fetch(self, responses, *, broker=None, url="https://zbmath.org/bibtex/1.bib"):
        broker = broker or SourceFetcher()
        with mock.patch("source_fetch.resolve_public", return_value=[(socket.AF_INET, ("1.1.1.1", 443))]) as resolver, \
                mock.patch("source_fetch._PinnedHTTPSConnection") as factory:
            factory.return_value.getresponse.side_effect = responses
            record, body = broker.fetch(url, "reviewer-c")
        return broker, record, body, resolver, factory

    def test_returns_source_digest_without_cookies_or_mutable_cache_alias(self):
        raw = b"@article{entry,title={An article},author={A. Name}}\n"
        broker, record, body, _, factory = self.fetch([response(raw, headers={"Content-Type": "application/x-bibtex", "Set-Cookie": "secret=value"})])
        self.assertEqual(body, raw)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["source_kind"], "zbmath")
        self.assertEqual(record["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(record["path"], f".audit-work/sources/{record['source_id']}.bib")
        self.assertNotIn("Cookie", factory.return_value.request.call_args.kwargs["headers"])
        record["sha256"] = "forged"
        record["redirects"].append({"url": "forged"})
        with mock.patch("source_fetch.resolve_public") as resolver:
            cached, cached_body = broker.fetch(record["requested_url"], "reviewer-a")
        resolver.assert_not_called()
        self.assertEqual(cached["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(cached["redirects"], [])
        self.assertEqual(cached["status"], "ok")
        self.assertEqual(cached_body, raw)

    def test_redirects_revalidate_and_attribute_final_origin_only(self):
        broker, record, body, resolver, _ = self.fetch([
            response(status=302, headers={"Location": "https://numdam.org/paper.bib"}),
            response(b"@article{x,title={Paper}}", headers={"Content-Type": "text/plain"})])
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["final_url"], "https://numdam.org/paper.bib")
        self.assertEqual(record["source_kind"], "public_https")
        self.assertEqual([c.args[0] for c in resolver.call_args_list], ["zbmath.org", "numdam.org"])
        self.assertEqual(len(record["redirects"]), 1)
        _, spoof, _, _, _ = self.fetch([response()], url="https://zbmath.org.evil.com/bib")
        self.assertEqual(spoof["source_kind"], "public_https")

    def test_redirect_cannot_access_local_or_other_schemes(self):
        for location in ("file:///etc/passwd", "http://example.com/a", "https://127.0.0.1/a", "https://user@example.com/a"):
            with self.subTest(location=location):
                broker, record, _, resolver, _ = self.fetch([response(status=302, headers={"Location": location})])
                self.assertEqual(record["status"], "error")
                self.assertIsNone(record["path"])
                self.assertEqual(resolver.call_count, 1)

    def test_redirect_dns_rebinding_is_rechecked(self):
        with mock.patch("source_fetch.resolve_public", side_effect=[[(socket.AF_INET, ("1.1.1.1", 443))], SourceFetchError("Nonpublic endpoint")]) as resolver, \
                mock.patch("source_fetch._PinnedHTTPSConnection") as factory:
            factory.return_value.getresponse.return_value = response(status=302, headers={"Location": "/second"})
            record, _ = SourceFetcher().fetch("https://example.com/first", "c")
        self.assertEqual(record["status"], "error")
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(factory.call_count, 1)

    def test_declared_actual_and_session_sizes_fail_closed(self):
        cases = [
            (SourceFetcher(max_body_bytes=4), response(b"long content")),
            (SourceFetcher(max_body_bytes=4), response(b"x", headers={"Content-Type": "text/plain", "Content-Length": "100"})),
            (SourceFetcher(max_session_bytes=4), response(b"long content")),
        ]
        for broker, result in cases:
            _, record, body, _, _ = self.fetch([result], broker=broker)
            self.assertEqual(record["status"], "error")
            self.assertEqual(body, b"")
            self.assertIsNone(record["sha256"])

    def test_compression_wrong_pdf_binary_and_http_error_are_not_success(self):
        for result in (response(b"gz", headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"}),
                       response(b"wrong", headers={"Content-Type": "application/pdf"}),
                       response(b"\x00elf", headers={"Content-Type": "application/octet-stream"}),
                       response(b"no", status=403)):
            _, record, body, _, _ = self.fetch([result])
            self.assertEqual(record["status"], "error")
            self.assertEqual(body, b"")
        _, pdf, _, _, _ = self.fetch([response(b"%PDF-1.4\ncontent", headers={"Content-Type": "application/pdf"})])
        self.assertEqual(pdf["status"], "ok")
        self.assertTrue(pdf["path"].endswith(".pdf"))

    def test_redirect_and_request_quota(self):
        _, record, _, resolver, _ = self.fetch([response(status=302, headers={"Location": "/again"})], broker=SourceFetcher(max_redirects=0))
        self.assertEqual(record["status"], "error")
        self.assertEqual(resolver.call_count, 1)
        broker = SourceFetcher(max_requests=1)
        self.fetch([response()], broker=broker)
        with mock.patch("source_fetch.resolve_public") as resolver:
            with self.assertRaises(source_fetch.SourceQuotaError):
                broker.fetch("https://example.com/second", "c")
        resolver.assert_not_called()

    def test_actual_local_broker_authentication_and_client_roundtrip(self):
        with SourceFetcher() as broker, mock.patch("source_fetch.resolve_public", return_value=[(socket.AF_INET, ("1.1.1.1", 443))]), \
                mock.patch("source_fetch._PinnedHTTPSConnection") as factory:
            factory.return_value.getresponse.return_value = response(b"@article{x,title={Paper}}")
            record, body = fetch_source(broker.config, "https://zbmath.org/item.bib", "reviewer-c")
            self.assertEqual(record["thread_id"], "reviewer-c")
            self.assertEqual(record["status"], "ok")
            self.assertEqual(body, b"@article{x,title={Paper}}")
            with self.assertRaises(SourceToolError):
                fetch_source({**broker.config, "source_fetch_token": "z" * 43}, "https://zbmath.org/item.bib", "c")
            factory.return_value.request.assert_called_once()
        self.assertFalse(broker._thread.is_alive())
        broker.close()

    def test_repeated_sources_use_round_cache_with_distinct_caller_records(self):
        broker, first, body, _, _ = self.fetch([response(b"@article{x,title={Paper}}")])
        with mock.patch("source_fetch.resolve_public") as resolver:
            second, second_body = broker.fetch(first["requested_url"], "reviewer-a")
        resolver.assert_not_called()
        self.assertEqual(body, second_body)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["cached_from_source_id"], first["source_id"])
        self.assertEqual(second["thread_id"], "reviewer-a")
        self.assertNotEqual(second["source_id"], first["source_id"])
        self.assertNotEqual(second["path"], first["path"])
        self.assertEqual(second["sha256"], first["sha256"])
        # Another round is another broker and never sees this cache.
        other, fresh, _, resolver, _ = self.fetch([response(b"new version")])
        self.assertFalse(fresh["cache_hit"])
        self.assertEqual(resolver.call_count, 1)

    def test_blocked_and_temporary_http_responses_have_distinct_codes(self):
        cases = [(400, {}, "http_bad_request", False),
                 (403, {}, "http_forbidden", False),
                 (403, {"cf-mitigated": "challenge"}, "http_challenge", False),
                 (404, {}, "http_not_found", False),
                 (429, {"Retry-After": "60"}, "http_rate_limited", True),
                 (503, {}, "http_server_error", True)]
        for status, headers, code, retryable in cases:
            with self.subTest(status=status, code=code):
                broker, record, body, _, _ = self.fetch([response(b"untrusted error body", status=status, headers=headers)])
                self.assertEqual(record["error_code"], code)
                self.assertEqual(record["retryable"], retryable)
                self.assertEqual(body, b"")
                self.assertNotIn("untrusted", json.dumps(record))
                with mock.patch("source_fetch.resolve_public") as resolver:
                    again, _ = broker.fetch(record["requested_url"], "c")
                resolver.assert_not_called()
                self.assertTrue(again["cache_hit"])
                if status == 429:
                    self.assertEqual(record["retry_after_seconds"], 60)

    def test_expired_failure_cache_admits_a_new_fetch(self):
        broker, first, _, _, _ = self.fetch([response(status=503)])
        key = first["requested_url"]
        _, record, body = broker._cache[key]
        broker._cache[key] = (time.monotonic() - 1, record, body)
        _, next_record, next_body, resolver, _ = self.fetch([response(b"recovered")], broker=broker)
        self.assertFalse(next_record["cache_hit"])
        self.assertEqual(next_body, b"recovered")
        self.assertEqual(resolver.call_count, 1)

    def test_certificate_failure_uses_system_trust_at_same_public_endpoint(self):
        endpoint = (socket.AF_INET, ("1.1.1.1", 443))
        with mock.patch("source_fetch.resolve_public", return_value=[endpoint]), \
                mock.patch("source_fetch._PinnedHTTPSConnection") as factory, \
                mock.patch("source_fetch._native_https_get") as native:
            factory.return_value.request.side_effect = ssl.SSLCertVerificationError("test")
            native.return_value = source_fetch._BufferedResponse(200, {"content-type": "application/json"}, b'{"result":[]}')
            record, body = SourceFetcher().fetch("https://api.zbmath.org/v1/document/_search?search_string=test", "c")
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["transport"], "macos_system_verified_tls")
        self.assertEqual(native.call_args.args[1], endpoint)
        self.assertEqual(body, b'{"result":[]}')

    def test_other_connection_failure_does_not_fall_back_to_native_tls(self):
        with mock.patch("source_fetch.resolve_public", return_value=[(socket.AF_INET, ("1.1.1.1", 443))]), \
                mock.patch("source_fetch._PinnedHTTPSConnection") as factory, \
                mock.patch("source_fetch._native_https_get") as native:
            factory.return_value.request.side_effect = ConnectionRefusedError("test")
            record, _ = SourceFetcher().fetch("https://api.zbmath.org/v1/document/1", "c")
        native.assert_not_called()
        self.assertEqual(record["error_code"], "transport_error")

    def _native(self, wire, *, exit_code=0, limit=1024, delay=0, timeout=3, closed=False):
        real_popen = subprocess.Popen
        calls, children = [], []
        def start(command, **kwargs):
            calls.append((command, kwargs))
            child = real_popen([sys.executable, "-c",
                f"import os,time; time.sleep({delay!r}); os.write(1,{wire!r}); raise SystemExit({exit_code!r})"],
                **kwargs)
            children.append(child)
            return child
        try:
            with mock.patch("source_fetch.sys.platform", "darwin"), \
                    mock.patch("source_fetch.subprocess.Popen", side_effect=start):
                result = source_fetch._native_https_get("https://api.zbmath.org/v1/document/1",
                    (socket.AF_INET, ("1.1.1.1", 443)), deadline=time.monotonic() + timeout,
                    limit=limit, consume=lambda _n: None, closed=lambda: closed)
                return result, calls
        finally:
            for process in children:
                self.assertIsNotNone(process.poll())
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)

    def test_native_command_verifies_tls_and_excludes_environment_bypasses(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:8888",
                                         "CURL_CA_BUNDLE": "/untrusted/cert.pem", "HOME": "/untrusted"}):
            result, calls = self._native(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\npaper")
        command, kwargs = calls[0]
        self.assertEqual(command[:2], ["/usr/bin/curl", "-q"])
        self.assertEqual(command[command.index("--resolve") + 1], "api.zbmath.org:443:1.1.1.1")
        self.assertEqual(command[command.index("--proto") + 1], "=https")
        self.assertEqual(command[command.index("--noproxy") + 1], "*")
        self.assertEqual(command[command.index("--proxy") + 1], "")
        for unsafe in ("-k", "--insecure", "-L", "--location", "--netrc", "--config", "--cookie"):
            self.assertNotIn(unsafe, command)
        self.assertNotIn("HTTPS_PROXY", kwargs["env"])
        self.assertNotIn("CURL_CA_BUNDLE", kwargs["env"])
        self.assertNotIn("HOME", kwargs["env"])
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(result.read1(10), b"paper")

    def test_native_tls_failure_size_header_timeout_and_close_are_bounded(self):
        cases = [(b"", {"exit_code": 60}, "tls_verification"),
                 (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n" + b"x" * 100,
                  {"limit": 5}, "response_limit"),
                 (b"HTTP/1.1 200 OK\r\nX-Long: " + b"x" * 70000,
                  {}, "response_limit"),
                 (b"", {"delay": 2, "timeout": 0.05}, "timeout"),
                 (b"", {"delay": 2, "closed": True}, "broker_closed")]
        for wire, options, code in cases:
            with self.subTest(code=code), self.assertRaises(SourceFetchError) as raised:
                self._native(wire, **options)
            self.assertEqual(raised.exception.code, code)

    def test_native_rejects_local_endpoints_before_spawning(self):
        with mock.patch("source_fetch.subprocess.Popen") as factory:
            with self.assertRaises(SourceFetchError):
                source_fetch._native_https_get("https://api.zbmath.org/v1/document/1",
                    (socket.AF_INET, ("127.0.0.1", 443)), deadline=time.monotonic() + 1,
                    limit=100, consume=lambda _n: None, closed=lambda: False)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
