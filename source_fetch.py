# Trusted-parent, bounded HTTPS retrieval of public literature.
# The sandbox receives only an authenticated loopback fetch endpoint. It never
# receives a CONNECT tunnel, filesystem operation, cookies, or credentials. Every
# remote hop is resolved through fixed public DoH and pinned to a validated public
# address; TLS still verifies the requested hostname. Fetched text is untrusted
# source material, never instructions or executable code. 

from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import os
import re
import secrets
import selectors
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
import uuid

from public_dns import (DNSError, FixedDoHConnection as _FixedDoHConnection,
                        abort_connection as _abort_connection, public_address as _public_address,
                        query_json, resolve_answers)

MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 16384
MAX_HEADER_BYTES = 65536
MAX_CACHE_BYTES = 8 * 1024 * 1024
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
TEXT_TYPES = {"application/x-bibtex", "application/bibtex", "application/json",
              "application/xml", "application/xhtml+xml", "application/octet-stream"}


class SourceFetchError(ValueError):
    """A source URL or remote response violates the public retrieval policy."""

    def __init__(self, message, *, code="policy_rejected", retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class SourceQuotaError(SourceFetchError):
    """No new attempt is admitted once the finite session budget is exhausted."""


class _SourceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._clients = set()
        self._client_lock = threading.Lock()
        self._client_slots = threading.BoundedSemaphore(12)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self._client_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._client_lock:
            self._clients.add(request)
        try:
            super().process_request(request, address)
        except BaseException:
            self._release(request)
            raise

    def _release(self, request):
        with self._client_lock:
            self._clients.discard(request)
        self._client_slots.release()

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self._release(request)

    def close_clients(self):
        with self._client_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()


def checked_url(value: str) -> tuple[str, str, str]:
    """Return normalized URL, hostname, and request target, with no local scheme."""
    if (not isinstance(value, str) or not value or len(value) > 8192
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or "\\" in value):
        raise SourceFetchError("Expected an HTTPS URL without whitespace or control characters.")
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise SourceFetchError("Only HTTPS URLs without user information are permitted.")
        if parsed.port not in (None, 443):
            raise SourceFetchError("Only HTTPS port 443 is permitted.")
        host = parsed.hostname or ""
        host = (host[:-1] if host.endswith(".") else host).encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError) as exc:
        raise SourceFetchError("Malformed source URL.") from exc
    # Literals are unnecessary for publication sources and complicate transition
    # address/TLS handling. DNS names are validated again after resolution.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SourceFetchError("Publication sources must use a public DNS hostname, not an IP literal.")
    labels = host.split(".")
    if (len(host) > 253 or len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in labels)
            or labels[-1].isdigit()
            or labels[-1] in {"localhost", "local", "internal", "home", "test", "invalid", "example", "onion"}):
        raise SourceFetchError("Source hostname is not a permitted public DNS name.")
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
    normalized = urlunsplit(("https", host, path, query, ""))
    return normalized, host, path + (("?" + query) if query else "")


def _read_response(response, transport, *, limit: int, deadline: float,
                   consume=None) -> bytes:
    data = bytearray()
    while not response.isclosed() and response.length != 0:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceFetchError("Source response exceeded its total time limit.", code="timeout", retryable=True)
        if transport is not None:
            transport.settimeout(remaining)
        chunk = response.read1(min(65536, limit + 1 - len(data)))
        if not chunk:
            break
        if consume is not None:
            consume(len(chunk))
        data.extend(chunk)
        if len(data) > limit:
            raise SourceFetchError("Source response exceeded its byte limit.", code="response_limit")
    return bytes(data)


def resolve_public(host: str, deadline: float) -> list[tuple[int, tuple]]:
    """Resolve only through the pinned public resolver; validate the full set."""
    def query(name, kind):
        return query_json(name, kind, connection_factory=_FixedDoHConnection, deadline=deadline)

    try:
        return resolve_answers(host, query)
    except DNSError as exc:
        raise SourceFetchError(str(exc), code=exc.code, retryable=exc.code == "timeout") from exc
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise SourceFetchError("Public source DNS lookup failed.", code="dns_failure", retryable=True) from exc


def _check_endpoint(endpoint):
    family, address = endpoint
    if (family not in (socket.AF_INET, socket.AF_INET6)
            or len(address) != (2 if family == socket.AF_INET else 4)
            or address[1] != 443 or not _public_address(address[0])
            or (family == socket.AF_INET6 and address[2:] != (0, 0))):
        raise SourceFetchError("Unsafe source connection endpoint.")


class _BufferedResponse:
    """A decoded native response using the same bounded-reader interface."""

    def __init__(self, status, headers, body):
        self.status, self.headers = status, headers
        self._body = io.BytesIO(body)
        self.length = len(body)

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read1(self, size):
        chunk = self._body.read(size)
        self.length -= len(chunk)
        return chunk

    def isclosed(self):
        return self._body.closed

    def close(self):
        self._body.close()


def _native_https_get(url, endpoint, *, deadline, limit, consume, closed):
    """macOS system trust fallback, never an unrestricted curl invocation.

    Use only after Python rejects a certificate chain. The system curl verifies
    TLS with SecureTransport; the checked DNS endpoint stays pinned. There is
    no shell, inherited proxy/config/credential environment, automatic redirect,
    URL globbing, cookie jar, or caller-controlled argument or filesystem path.
    Both streams, bytes and elapsed time are bounded by the parent as well as
    curl. The caller still validates each redirect and response content.
    """
    url, host, _ = checked_url(url)
    _check_endpoint(endpoint)
    if sys.platform != "darwin":
        raise SourceFetchError("TLS certificate verification failed.", code="tls_verification")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SourceFetchError("Source retrieval exceeded its total time limit.", code="timeout", retryable=True)
    address = endpoint[1][0]
    if ":" in address:
        address = "[" + address + "]"
    command = ["/usr/bin/curl", "-q", "--silent", "--show-error", "--globoff",
               "--http1.1", "--include", "--proto", "=https", "--proto-redir", "=https",
               "--noproxy", "*", "--proxy", "", "--max-redirs", "0",
               "--resolve", f"{host}:443:{address}", "--tlsv1.2",
               "--connect-timeout", str(min(10, remaining)), "--max-time", str(remaining),
               "--max-filesize", str(limit), "--header", "Accept-Encoding: identity",
               "--header", "Accept: application/pdf, application/x-bibtex, text/plain, text/html, */*;q=0.1",
               "--header", "Connection: close", "--user-agent", "ManuscriptAgent-Literature/1.0",
               "--url", url]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, close_fds=True, cwd="/",
                               env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    output, errors = bytearray(), bytearray()
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, output)
        selector.register(process.stderr, selectors.EVENT_READ, errors)
        header_end = None
        while selector.get_map():
            if closed():
                raise SourceFetchError("Session source broker closed.", code="broker_closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceFetchError("Source retrieval exceeded its total time limit.", code="timeout", retryable=True)
            for key, _ in selector.select(min(0.2, remaining)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                destination = key.data
                destination.extend(chunk)
                if destination is errors:
                    if len(errors) > 8192:
                        raise SourceFetchError("Source TLS transport returned excessive diagnostics.", code="transport_error")
                    continue
                consume(len(chunk))
                if header_end is None:
                    marker = output.find(b"\r\n\r\n")
                    if marker >= 0:
                        header_end = marker + 4
                    if (header_end or len(output)) > MAX_HEADER_BYTES:
                        raise SourceFetchError("Source response exceeded its header limit.", code="response_limit")
                if len(output) > limit + (header_end or MAX_HEADER_BYTES):
                    raise SourceFetchError("Source response exceeded its byte limit.", code="response_limit")
        returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if returncode:
            # Do not expose arbitrary remote-derived stderr, headers or cookies.
            if returncode in {35, 51, 58, 59, 60, 77, 80, 82, 83, 90, 91}:
                raise SourceFetchError("System TLS certificate verification failed.", code="tls_verification")
            if returncode == 28:
                raise SourceFetchError("Source retrieval exceeded its total time limit.", code="timeout", retryable=True)
            if returncode == 63:
                raise SourceFetchError("Source response exceeded its byte limit.", code="response_limit")
            raise SourceFetchError(f"System source transport failed (code {returncode}).", code="transport_error", retryable=True)
        if header_end is None:
            raise SourceFetchError("Source response omitted HTTP headers.", code="transport_error")
        first, *lines = bytes(output[:header_end - 4]).split(b"\r\n")
        match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: .*)?", first)
        if not match or not 200 <= int(match[1]) <= 599:
            raise SourceFetchError("Source response had invalid HTTP framing.", code="transport_error")
        headers = {}
        for line in lines:
            name, separator, value = line.partition(b":")
            if not separator or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise SourceFetchError("Source response had invalid HTTP headers.", code="transport_error")
            name, value = name.decode("ascii").lower(), value.decode("iso-8859-1").strip()
            if name in headers and name in {"content-length", "content-encoding", "content-type", "location"}:
                raise SourceFetchError("Source response had ambiguous HTTP headers.", code="transport_error")
            headers[name] = value
        return _BufferedResponse(int(match[1]), headers, bytes(output[header_end:]))
    except subprocess.TimeoutExpired as exc:
        raise SourceFetchError("Source retrieval exceeded its total time limit.", code="timeout", retryable=True) from exc
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, endpoint: tuple[int, tuple], timeout: float):
        ca_source = os.environ.get("CODEX_CA_CERTIFICATE") or os.environ.get("SSL_CERT_FILE")
        context = ssl.create_default_context(**({"cafile": ca_source} if ca_source else {}))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(host, port=443, timeout=timeout, context=context)
        self.endpoint = endpoint

    def connect(self):
        family, address = self.endpoint
        # Revalidate at the actual connection boundary even if a resolver or
        # testing adapter accidentally returned an invalid endpoint.
        _check_endpoint(self.endpoint)
        raw = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        self.sock = raw  # The absolute deadline timer can interrupt connect/TLS.
        try:
            raw.settimeout(self.timeout)
            raw.connect(address)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _kind(host: str) -> str:
    return "zbmath" if host == "zbmath.org" or host.endswith(".zbmath.org") else "public_https"


def _extension(content_type: str, body: bytes) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime == "application/pdf" or body.startswith(b"%PDF-"):
        if not body.startswith(b"%PDF-"):
            raise SourceFetchError("Declared PDF response did not contain a PDF header.")
        return "pdf"
    if not (mime.startswith("text/") or mime in TEXT_TYPES):
        raise SourceFetchError("Source media type is outside text, BibTeX, HTML, and PDF retrieval.")
    if b"\x00" in body:
        raise SourceFetchError("Non-PDF binary response is not permitted.")
    if re.search(rb"(?m)^\s*@[a-zA-Z]+\s*[({]", body):
        return "bib"
    return "html" if mime in {"text/html", "application/xhtml+xml"} else "txt"


def _http_error(response):
    status = response.status
    # Cloudflare also carries ordinary publisher refusals. Only its explicit
    # challenge marker justifies classifying a response as an anti-bot gate.
    challenge = response.getheader("cf-mitigated", "").lower() == "challenge"
    if challenge:
        return SourceFetchError("Source access was blocked by an upstream challenge; use an official API or another authorized source.", code="http_challenge")
    codes = {400: "http_bad_request", 401: "http_unauthorized", 403: "http_forbidden",
             404: "http_not_found", 405: "http_bad_request", 422: "http_bad_request",
             429: "http_rate_limited"}
    code = codes.get(status, "http_server_error" if status >= 500 else "http_error")
    return SourceFetchError(f"Source returned HTTP {status}.", code=code,
                            retryable=status == 429 or status >= 500)


class SourceFetcher:
    """Authenticated source broker with a bounded, session-scoped cache.

    Use with SourceFetcher() as broker; stage broker.config in the sealed round
    configuration. Returned metadata describes source bytes and their origin,
    not scholarly truth.
    """
    def __init__(self, *, max_requests: int = 96, max_session_bytes: int = 128 * 1024 * 1024,
                 max_body_bytes: int = MAX_BODY_BYTES, timeout: float = 45,
                 max_redirects: int = 5, max_connections: int = 3):
        if min(max_requests, max_session_bytes, max_body_bytes, timeout, max_connections) <= 0 or not 0 <= max_redirects <= 10:
            raise ValueError("Source broker limits must be positive; redirects must be between 0 and 10.")
        self.max_requests, self.max_session_bytes = max_requests, max_session_bytes
        self.max_body_bytes, self.timeout, self.max_redirects = max_body_bytes, timeout, max_redirects
        self.token = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_connections)
        self._cache = {}
        self._cache_bytes = 0
        self._requests = self._bytes = 0
        self._server = self._thread = None
        self._closed = False

    @property
    def port(self):
        if self._server is None:
            raise RuntimeError("Enter SourceFetcher before using its port.")
        return self._server.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/fetch"

    @property
    def config(self):
        return {"source_fetch_url": self.url, "source_fetch_token": self.token}

    def __enter__(self):
        if self._server is not None or self._closed:
            raise RuntimeError("SourceFetcher can only be entered once.")
        broker = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            def log_message(self, *_args):
                pass
            def setup(self):
                super().setup()
                self.connection.settimeout(5)
            def _reply(self, code, value):
                raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass
            def do_POST(self):
                if self.path != "/fetch" or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + broker.token):
                    self._reply(403, {"error": "Forbidden."})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if not 0 < length <= MAX_REQUEST_BYTES or self.headers.get("Transfer-Encoding"):
                    self._reply(400, {"error": "Invalid request size or framing."})
                    return
                try:
                    value = json.loads(self.rfile.read(length))
                    if (not isinstance(value, dict) or set(value) != {"url", "thread_id"}
                            or not isinstance(value["url"], str) or len(value["url"]) > 8192
                            or not isinstance(value["thread_id"], str)
                            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value["thread_id"])):
                        raise ValueError("Malformed request.")
                except (ValueError, UnicodeError, OSError):
                    self._reply(400, {"error": "Malformed fetch request."})
                    return
                if not broker._slots.acquire(blocking=False):
                    self._reply(429, {"error": "Concurrent source fetch limit reached."})
                    return
                try:
                    try:
                        record, body = broker.fetch(value["url"], value["thread_id"])
                    except SourceQuotaError:
                        self._reply(429, {"error": "Session source retrieval limit reached."})
                        return
                    self._reply(200, {"record": record, "content_base64": base64.b64encode(body).decode("ascii")})
                finally:
                    broker._slots.release()
        self._server = _SourceHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.1}, daemon=True,
                                        name="manuscript-public-sources")
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self._closed = True
        if self._server is not None:
            self._server.shutdown()
            self._server.close_clients()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _consume(self, count):
        with self._lock:
            if self._closed:
                raise SourceFetchError("Session source broker closed.", code="broker_closed")
            self._bytes += count
            if self._bytes > self.max_session_bytes:
                raise SourceFetchError("Session source bandwidth limit reached.", code="session_limit")

    def _retrieve(self, url, record):
        deadline = time.monotonic() + self.timeout
        for hop in range(self.max_redirects + 1):
            url, host, target = checked_url(url)
            record["final_url"] = url
            record["source_kind"] = _kind(host)
            endpoints = resolve_public(host, deadline)
            response = connection = None
            last_error = None
            for endpoint in endpoints:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceFetchError("Source retrieval exceeded its total time limit.", code="timeout", retryable=True)
                connection = _PinnedHTTPSConnection(host, endpoint, min(10, remaining))
                timer = threading.Timer(remaining, _abort_connection, args=(connection,))
                timer.daemon = True
                timer.start()
                native = False
                try:
                    try:
                        connection.request("GET", target, headers={"Accept": "application/pdf, application/x-bibtex, text/plain, text/html, */*;q=0.1", "Accept-Encoding": "identity", "User-Agent": "ManuscriptAgent-Literature/1.0", "Connection": "close"})
                        transport = connection.sock
                        response = connection.getresponse()
                        record["transport"] = "python_verified_tls"
                    except ssl.SSLCertVerificationError:
                        timer.cancel()
                        connection.close()
                        response = _native_https_get(url, endpoint, deadline=deadline,
                                                     limit=self.max_body_bytes, consume=self._consume,
                                                     closed=lambda: self._closed)
                        transport = None
                        native = True
                        record["transport"] = "macos_system_verified_tls"
                    record["http_status"] = response.status
                    record["content_type"] = response.getheader("Content-Type", "").strip()[:256]
                    if response.status in REDIRECT_STATUSES:
                        location = response.getheader("Location")
                        if not location or hop == self.max_redirects:
                            raise SourceFetchError("Source redirect is missing a location or exceeds its hop limit.")
                        next_url, _, _ = checked_url(urljoin(url, location))
                        record["redirects"].append({"url": url, "status": response.status, "location": next_url})
                        url = next_url
                        break
                    if not 200 <= response.status < 300:
                        retry_after = response.getheader("Retry-After", "")
                        if retry_after.isdigit():
                            record["retry_after_seconds"] = min(3600, int(retry_after[:8]))
                        raise _http_error(response)
                    if response.getheader("Content-Encoding", "identity").strip().lower() != "identity":
                        raise SourceFetchError("Compressed source responses are not permitted.")
                    length = response.getheader("Content-Length")
                    if length is not None and (not length.isdigit() or int(length) > self.max_body_bytes):
                        raise SourceFetchError("Source declared an invalid or excessive byte length.")
                    body = _read_response(response, transport, limit=self.max_body_bytes,
                                          deadline=deadline, consume=None if native else self._consume)
                    if length is not None and len(body) != int(length):
                        raise SourceFetchError("Source body did not match its declared byte length.")
                    if not body:
                        raise SourceFetchError("Source returned an empty body.")
                    extension = _extension(record["content_type"], body)
                    record.update(status="ok", sha256=hashlib.sha256(body).hexdigest(),
                                  size_bytes=len(body), path=f".audit-work/sources/{record['source_id']}.{extension}")
                    return body
                except SourceFetchError:
                    raise
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    last_error = exc
                    if response is not None:
                        raise SourceFetchError("Source response transport failed.", code="transport_error", retryable=True) from exc
                finally:
                    timer.cancel()
                    if response is not None:
                        response.close()
                    connection.close()
            else:
                if isinstance(last_error, ssl.SSLError):
                    raise SourceFetchError("Source TLS verification or negotiation failed.", code="tls_verification") from last_error
                if isinstance(last_error, TimeoutError):
                    raise SourceFetchError("Source retrieval timed out.", code="timeout", retryable=True) from last_error
                raise SourceFetchError("Source connection failed.", code="transport_error", retryable=True) from last_error
        raise SourceFetchError("Source exceeded its redirect limit.")

    def fetch(self, url: str, thread_id: str):
        """Internal parent interface; production child access uses authenticated HTTP."""
        # Invalid URLs and failed requests consume the same attempt budget.
        with self._lock:
            if self._closed or self._requests >= self.max_requests or self._bytes >= self.max_session_bytes:
                raise SourceQuotaError("Session source retrieval limit reached or broker closed.")
            self._requests += 1
        source_id = uuid.uuid4().hex
        record = {"schema_version": 1, "source_id": source_id, "requested_url": url,
                  "final_url": None, "sha256": None, "content_type": None,
                  "status": "error", "http_status": None,
                  "fetched_at": datetime.now(timezone.utc).isoformat(),
                  "thread_id": thread_id, "size_bytes": 0, "path": None,
                  "metadata_path": f".audit-work/sources/{source_id}.json", "error": None,
                  "source_kind": None, "redirects": [], "error_code": None,
                  "retryable": False, "transport": None, "cache_hit": False}
        body = b""
        cache_key = None
        try:
            cache_key, _, _ = checked_url(url)
            with self._lock:
                cached = self._cache.get(cache_key)
                if cached is not None and cached[0] < time.monotonic():
                    self._cache_bytes -= len(cached[2])
                    del self._cache[cache_key]
                    cached = None
            if cached is not None:
                _, original, body = cached
                # Each caller gets independent metadata and a unique filename.
                # The cache only contains acquisitions from this round.
                identity = {key: record[key] for key in ("source_id", "thread_id", "requested_url", "metadata_path")}
                record.update(copy.deepcopy(original))
                record.update(identity, cache_hit=True, cached_from_source_id=original["source_id"],
                              served_at=datetime.now(timezone.utc).isoformat())
                if record["path"]:
                    extension = record["path"].rsplit(".", 1)[1]
                    record["path"] = f".audit-work/sources/{source_id}.{extension}"
            else:
                body = self._retrieve(url, record)
        except (SourceFetchError, OSError, ValueError, http.client.HTTPException) as exc:
            code = getattr(exc, "code", "tls_verification" if isinstance(exc, ssl.SSLError) else "transport_error")
            record.update(status="error", error=str(exc)[:512], error_code=code,
                          retryable=getattr(exc, "retryable", isinstance(exc, (OSError, http.client.HTTPException))),
                          path=None, sha256=None, size_bytes=0)
            body = b""
        with self._lock:
            if (cache_key is not None and not record["cache_hit"]
                    and len(body) <= 2 * 1024 * 1024
                    and self._cache_bytes + len(body) <= MAX_CACHE_BYTES):
                old = self._cache.get(cache_key)
                if old:
                    self._cache_bytes -= len(old[2])
                ttl = 30 if record["retryable"] else 300
                self._cache[cache_key] = (time.monotonic() + ttl, copy.deepcopy(record), body)
                self._cache_bytes += len(body)
        return record, body
