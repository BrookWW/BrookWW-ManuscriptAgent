"""Fixed-resolver DNS transport and validation shared by the parent services.

Callers retain their own hostname policies. Every returned endpoint is a checked
public numeric HTTPS address; no environment proxy or system resolver is used.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlencode

MAX_DNS_BYTES = 65536


class DNSError(ValueError):
    """A resolver response failed policy, size, or elapsed-time validation."""

    def __init__(self, message: str, *, code: str = "policy_rejected"):
        super().__init__(message)
        self.code = code


def public_address(value: str) -> bool:
    # Scoped IPv6 addresses are unnecessary for public API endpoints.
    if "%" in value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or any((address.is_loopback, address.is_private,
            address.is_link_local, address.is_multicast, address.is_unspecified,
            address.is_reserved, getattr(address, "is_site_local", False))):
        return False
    # Public-looking transition addresses can encapsulate private IPv4 targets.
    # Public HTTPS endpoints do not require 6to4 or Teredo tunneling.
    if getattr(address, "sixtofour", None) or getattr(address, "teredo", None):
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped is None or public_address(str(mapped))


class FixedDoHConnection(http.client.HTTPSConnection):
    """Direct TLS to a fixed public resolver; no system DNS or proxy routing."""

    def __init__(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__("1.1.1.1", 443, timeout=5, context=context)

    def connect(self) -> None:
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        try:
            raw.settimeout(self.timeout)
            raw.connect(("1.1.1.1", 443))
            self.sock = self._context.wrap_socket(raw, server_hostname="1.1.1.1")
        except BaseException:
            raw.close()
            raise


def abort_connection(connection) -> None:
    sock = connection.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def query_json(host: str, kind: int, *, connection_factory=FixedDoHConnection,
               deadline: float | None = None) -> dict:
    """Fetch one bounded DNS message, optionally within a source-wide deadline.

    JSON decoding and transport errors remain distinct from policy failures so
    each caller can preserve its own retry/error classification.
    """
    if kind not in (1, 28):
        raise DNSError("Unsupported resolver question.")
    if deadline is not None and time.monotonic() >= deadline:
        raise DNSError("DNS lookup exceeded its total time limit.", code="timeout")
    connection = connection_factory()
    response = timer = None
    try:
        if deadline is not None:
            timer = threading.Timer(max(0.01, min(10, deadline - time.monotonic())),
                                    abort_connection, args=(connection,))
            timer.daemon = True
            timer.start()
        connection.request("GET", "/dns-query?" + urlencode({"name": host, "type": kind}),
                           headers={"Accept": "application/dns-json", "Connection": "close"})
        transport = connection.sock
        response = connection.getresponse()
        if (response.status != 200
                or response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/dns-json"
                or response.getheader("Content-Encoding", "identity").lower() != "identity"):
            raise DNSError("Resolver response was not permitted DNS JSON.")
        body_deadline = time.monotonic() + 5
        if deadline is not None:
            body_deadline = min(deadline, body_deadline)
        data = bytearray()
        # Stop at known EOF before touching a transport HTTPResponse has closed.
        while not response.isclosed() and response.length != 0:
            remaining = body_deadline - time.monotonic()
            if remaining <= 0:
                raise DNSError("Resolver response timed out.", code="timeout")
            if transport is not None:
                transport.settimeout(remaining)
            # One underlying read keeps the deadline effective for trickle feeds.
            chunk = response.read1(min(65536, MAX_DNS_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_DNS_BYTES:
                raise DNSError("Resolver response exceeded its size limit.", code="response_limit")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise DNSError("Resolver returned an invalid DNS message.")
        return result
    finally:
        if timer is not None:
            timer.cancel()
        try:
            if response is not None:
                response.close()
        finally:
            connection.close()


def resolve_answers(host: str, query) -> list[tuple[int, tuple]]:
    """Validate and deduplicate the complete A/AAAA response set."""
    endpoints: list[tuple[int, tuple]] = []
    for kind in (1, 28):
        message = query(host, kind)
        question = message.get("Question")
        if (message.get("Status") != 0 or message.get("TC") is not False
                or not isinstance(question, list) or len(question) != 1
                or not isinstance(question[0], dict)
                or not isinstance(question[0].get("name"), str)
                or question[0].get("name", "").lower() not in (host, host + ".")
                or question[0].get("type") != kind):
            raise DNSError("Resolver response did not match the requested question.")
        records = message.get("Answer", [])
        if not isinstance(records, list):
            raise DNSError("Resolver returned malformed answer records.")
        for record in records:
            if not isinstance(record, dict):
                raise DNSError("Resolver returned malformed answer records.")
            if record.get("type") == 5:  # CNAME: addresses arrive in this same answer set.
                continue
            address = record.get("data")
            if record.get("type") != kind or not isinstance(address, str) or not public_address(address):
                raise DNSError("Resolver returned an unsafe endpoint.")
            parsed = ipaddress.ip_address(address)
            if parsed.version != (4 if kind == 1 else 6):
                raise DNSError("Resolver returned an address of the wrong family.")
            endpoint = ((socket.AF_INET, (str(parsed), 443)) if kind == 1 else
                        (socket.AF_INET6, (str(parsed), 443, 0, 0)))
            if endpoint not in endpoints:
                endpoints.append(endpoint)
    if not endpoints:
        raise DNSError("Resolver returned no permitted endpoints.")
    return endpoints
