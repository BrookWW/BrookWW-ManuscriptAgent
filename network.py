"""Narrow CONNECT relay for the unsandboxed AuditAgent driver.

The sandbox may reach this relay only. The relay never interprets application
data or reads files: it accepts three exact CONNECT authorities, checks every
DNS address, and connects to a checked public numeric endpoint. TLS remains
opaque, so this is not a guarantee about SNI or HTTP Host on shared CDN IPs. It
does not use environment proxies or log requests, headers, or tunnel bytes.
"""
from __future__ import annotations

import http.client
import re
import select
import socket
import threading
import time

from public_dns import (DNSError, FixedDoHConnection as _FixedDoHConnection,
                        public_address as _public_address, query_json, resolve_answers)


ALLOWED_HOSTS = frozenset({"api.openai.com", "chatgpt.com", "auth.openai.com"})
MAX_HEADER_BYTES = 16384
MAX_BUFFER_BYTES = 262144


class ProxyPolicyError(ValueError):
    """A destination or CONNECT request is outside the fixed relay policy."""


def parse_destination(authority: str) -> str:
    """Return the exact allowed hostname; reject URLs and ambiguous authorities."""
    if not isinstance(authority, str) or not authority.isascii():
        raise ProxyPolicyError("Destination must be an ASCII host:port authority.")
    match = re.fullmatch(r"([A-Za-z0-9.-]+):443", authority)
    if not match:
        raise ProxyPolicyError("Only an allowed hostname on port 443 is permitted.")
    host = match.group(1).lower()
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ProxyPolicyError("Invalid hostname.") from exc
    if host != ascii_host or host not in ALLOWED_HOSTS:
        raise ProxyPolicyError("Destination is not allowlisted.")
    return host


def resolve_public(host: str) -> list[tuple[int, tuple]]:
    """Reject the whole answer set if any resolved endpoint is nonpublic."""
    if host not in ALLOWED_HOSTS:
        raise ProxyPolicyError("Destination is not allowlisted.")
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM,
                                 proto=socket.IPPROTO_TCP)
    endpoints: list[tuple[int, tuple]] = []
    for family, kind, protocol, _canonical, address in answers:
        if (family not in (socket.AF_INET, socket.AF_INET6)
                or kind != socket.SOCK_STREAM or protocol != socket.IPPROTO_TCP
                or not isinstance(address, tuple) or len(address) not in (2, 4)
                or address[1] != 443 or not _public_address(address[0])
                or (family == socket.AF_INET and len(address) != 2)
                or (family == socket.AF_INET6 and (len(address) != 4 or address[2:] != (0, 0)))):
            raise ProxyPolicyError("DNS returned an unsafe endpoint.")
        endpoint = (family, address)
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    if not endpoints:
        raise ProxyPolicyError("DNS returned no permitted endpoints.")
    return endpoints


def _doh_query(host: str, kind: int) -> dict:
    if host not in ALLOWED_HOSTS or kind not in (1, 28):
        raise ProxyPolicyError("Unsupported resolver question.")
    try:
        return query_json(host, kind, connection_factory=_FixedDoHConnection)
    except DNSError as exc:
        if exc.code == "timeout":
            raise TimeoutError(str(exc)) from exc
        raise ProxyPolicyError(str(exc)) from exc
    except (ValueError, UnicodeError) as exc:
        raise ProxyPolicyError("Resolver returned malformed JSON.") from exc


def resolve_doh(host: str) -> list[tuple[int, tuple]]:
    """Fallback for environments that map API names to private DNS gateways."""
    if host not in ALLOWED_HOSTS:
        raise ProxyPolicyError("Destination is not allowlisted.")
    try:
        return resolve_answers(host, _doh_query)
    except DNSError as exc:
        raise ProxyPolicyError(str(exc)) from exc


def resolve_destination(host: str) -> list[tuple[int, tuple]]:
    if host not in ALLOWED_HOSTS:
        raise ProxyPolicyError("Destination is not allowlisted.")
    try:
        return resolve_public(host)
    except (ProxyPolicyError, OSError):
        # Discard the entire system answer set. Never tunnel to private gateway
        # addresses, even when they are this machine's normal API routing path.
        return resolve_doh(host)


class CodexProxy:
    """Context-managed loopback relay; use .port and .url after entering it."""

    def __init__(self, *, max_connections: int = 16, header_timeout: float = 5,
                 connect_timeout: float = 10, idle_timeout: float = 300,
                 lifetime: float = 7200):
        if not 1 <= max_connections <= 64:
            raise ValueError("max_connections must be between 1 and 64.")
        if min(header_timeout, connect_timeout, idle_timeout, lifetime) <= 0:
            raise ValueError("Relay timeouts must be positive.")
        self.header_timeout = header_timeout
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.lifetime = lifetime
        self._slots = threading.BoundedSemaphore(max_connections)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        self._workers: set[threading.Thread] = set()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._port: int | None = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("Enter the CodexProxy context before using its port.")
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> CodexProxy:
        if self._stop.is_set() or self._listener is not None:
            raise RuntimeError("A CodexProxy context can only be entered once.")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(32)
            listener.settimeout(0.2)
        except BaseException:
            listener.close()
            raise
        self._listener = listener
        self._port = listener.getsockname()[1]
        self._thread = threading.Thread(target=self._accept, name="audit-connect-relay", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @staticmethod
    def _close_socket(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

    def _track(self, connection: socket.socket) -> bool:
        with self._lock:
            if not self._stop.is_set():
                self._sockets.add(connection)
                return True
        self._close_socket(connection)
        return False

    def _untrack(self, connection: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(connection)
        self._close_socket(connection)

    def close(self) -> None:
        self._stop.set()
        if self._listener is not None:
            self._close_socket(self._listener)
        with self._lock:
            connections = list(self._sockets)
        for connection in connections:
            self._close_socket(connection)
        deadline = time.monotonic() + 2
        if self._thread is not None:
            self._thread.join(max(0, deadline - time.monotonic()))
        with self._lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        # A platform DNS lookup cannot be cancelled by stdlib. A lingering DNS
        # worker is daemonized, has no client socket, and checks _stop before
        # opening any upstream connection when its resolver returns.

    @staticmethod
    def _reply(connection: socket.socket, status: str) -> None:
        try:
            connection.sendall((f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n"
                                "Connection: close\r\n\r\n").encode("ascii"))
        except OSError:
            pass

    def _accept(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                client, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            client.settimeout(self.header_timeout)
            if not self._track(client):
                return
            if not self._slots.acquire(blocking=False):
                self._reply(client, "503 Service Unavailable")
                self._untrack(client)
                continue
            worker = threading.Thread(target=self._serve, args=(client,),
                                      name="audit-connect-tunnel", daemon=True)
            with self._lock:
                self._workers.add(worker)
            worker.start()

    def _read_request(self, client: socket.socket) -> str:
        header = bytearray()
        deadline = time.monotonic() + self.header_timeout
        while b"\r\n\r\n" not in header:
            if self._stop.is_set():
                raise OSError("Relay is closed.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("CONNECT header timed out.")
            # Short polls also stop an incomplete header when cross-thread
            # shutdown does not wake a macOS socket immediately.
            client.settimeout(min(0.2, remaining))
            try:
                data = client.recv(min(4096, MAX_HEADER_BYTES + 1 - len(header)))
            except socket.timeout:
                continue
            if not data:
                raise ProxyPolicyError("Incomplete CONNECT header.")
            header.extend(data)
            if len(header) > MAX_HEADER_BYTES:
                raise ProxyPolicyError("CONNECT header is too large.")
        head, extra = bytes(header).split(b"\r\n\r\n", 1)
        if extra:
            raise ProxyPolicyError("CONNECT request bodies are not permitted.")
        try:
            lines = head.decode("ascii").split("\r\n")
        except UnicodeDecodeError as exc:
            raise ProxyPolicyError("CONNECT headers must be ASCII.") from exc
        request = lines[0].split(" ")
        if len(request) != 3 or request[0] != "CONNECT" or request[2] not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ProxyPolicyError("Only CONNECT requests are permitted.")
        host = parse_destination(request[1])
        host_seen = False
        for line in lines[1:]:
            if ":" not in line:
                raise ProxyPolicyError("Malformed CONNECT header.")
            name, value = line.split(":", 1)
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise ProxyPolicyError("Malformed CONNECT header name.")
            if any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value):
                raise ProxyPolicyError("Malformed CONNECT header value.")
            if name.lower() in {"content-length", "transfer-encoding", "expect", "trailer"}:
                raise ProxyPolicyError("CONNECT request bodies are not permitted.")
            if name.lower() == "host":
                if host_seen or parse_destination(value.strip()) != host:
                    raise ProxyPolicyError("Ambiguous CONNECT Host header.")
                host_seen = True
        return host

    def _connect(self, host: str) -> socket.socket:
        endpoints = resolve_destination(host)
        deadline = time.monotonic() + self.connect_timeout
        for family, address in endpoints:
            if self._stop.is_set():
                raise OSError("Relay is closed.")
            connection = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            if not self._track(connection):
                raise OSError("Relay is closed.")
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Upstream connection timed out.")
                connection.settimeout(remaining)
                connection.connect(address)  # Numeric, already checked; no second DNS lookup.
                return connection
            except OSError:
                self._untrack(connection)
        raise OSError("No permitted upstream endpoint was reachable.")

    def _serve(self, client: socket.socket) -> None:
        upstream = None
        tunnel_started = False
        try:
            host = self._read_request(client)
            upstream = self._connect(host)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            tunnel_started = True
            self._relay(client, upstream)
        except ProxyPolicyError:
            if not tunnel_started:
                self._reply(client, "403 Forbidden")
        except (OSError, ValueError, http.client.HTTPException):
            if not tunnel_started:
                self._reply(client, "502 Bad Gateway")
        finally:
            if upstream is not None:
                self._untrack(upstream)
            self._untrack(client)
            with self._lock:
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        sockets = (client, upstream)
        peer = {client: upstream, upstream: client}
        buffers = {client: bytearray(), upstream: bytearray()}
        readable = set(sockets)
        shut_write: set[socket.socket] = set()
        for connection in sockets:
            connection.setblocking(False)
        started = last_activity = time.monotonic()
        while not self._stop.is_set() and (readable or any(buffers.values())):
            now = time.monotonic()
            timeout = min(0.2, self.idle_timeout - (now - last_activity),
                          self.lifetime - (now - started))
            if timeout <= 0:
                return
            readers = [connection for connection in readable
                       if len(buffers[peer[connection]]) < MAX_BUFFER_BYTES]
            writers = [connection for connection in sockets if buffers[connection]]
            ready_read, ready_write, _ = select.select(readers, writers, [], timeout)
            for connection in ready_read:
                try:
                    data = connection.recv(min(65536, MAX_BUFFER_BYTES - len(buffers[peer[connection]])))
                except BlockingIOError:
                    continue
                if data:
                    buffers[peer[connection]].extend(data)
                    last_activity = time.monotonic()
                else:
                    readable.discard(connection)
            for connection in ready_write:
                try:
                    sent = connection.send(buffers[connection])
                except BlockingIOError:
                    continue
                if sent <= 0:
                    return
                del buffers[connection][:sent]
                last_activity = time.monotonic()
            for connection in sockets:
                if (peer[connection] not in readable and not buffers[connection]
                        and connection not in shut_write):
                    connection.shutdown(socket.SHUT_WR)
                    shut_write.add(connection)
