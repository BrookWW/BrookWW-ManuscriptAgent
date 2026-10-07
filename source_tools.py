"""Round-local tools for fetching sources, reading PDFs, and preparing BibTeX.

Public HTTPS goes through the parent broker. All agents in the current round
share the downloaded sources and generated files; there are no ownership or
receipt requirements. Parsing happens inside the existing OS sandbox.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid

from safeio import SafeIOError, _root_fd, safe_read

MAX_RESPONSE_BYTES = 46 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 4 * 1024 * 1024


class SourceToolError(ValueError):
    pass


class SourceBibliographyUnavailable(SourceToolError):
    """A valid research request requires another source or candidate selection."""
    def __init__(self, message: str, status: str = "bibliography_unavailable"):
        super().__init__(message)
        self.status = status


def _write_once(directory: int, name: str, content: bytes):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SourceToolError("Source artifact is not a unique regular file.")
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise SourceToolError("Could not finish writing source artifact.")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def save_source(work_root: Path, record: dict, content: bytes):
    """Append fixed-name source artifacts through no-follow directory descriptors."""
    source_id = record.get("source_id")
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-f0-9]{32}", source_id):
        raise SourceToolError("Broker returned an invalid source identifier.")
    expected_prefix = f".audit-work/sources/{source_id}"
    if record.get("metadata_path") != expected_prefix + ".json":
        raise SourceToolError("Broker returned an invalid metadata path.")
    path = record.get("path")
    if record.get("status") == "ok":
        if (path not in {expected_prefix + suffix for suffix in (".pdf", ".html", ".bib", ".txt")}
                or not content or len(content) > MAX_SOURCE_BYTES):
            raise SourceToolError("Invalid source path or source size.")
    elif record.get("status") != "error" or content or path is not None:
        raise SourceToolError("Invalid failed-source response.")
    directory = _root_fd(work_root)
    try:
        for component in (".audit-work", "sources"):
            try:
                os.mkdir(component, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        if path:
            _write_once(directory, Path(path).name, content)
        _write_once(directory, source_id + ".json", (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    finally:
        os.close(directory)


def fetch_source(config: dict, url: str, thread_id: str = "round-local") -> tuple[dict, bytes]:
    endpoint = config.get("source_fetch_url")
    token = config.get("source_fetch_token")
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.path != "/fetch" or parsed.query or parsed.fragment or parsed.username is not None
                or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)):
            raise ValueError("Bad endpoint.")
    except (TypeError, ValueError) as exc:
        raise SourceToolError("Missing or invalid sealed source broker configuration.") from exc
    # http.client talks straight to the sealed loopback endpoint, ignoring all
    # HTTP_PROXY/HTTPS_PROXY variables and all external host/proxy settings.
    connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=60)
    response = None
    try:
        request = json.dumps({"url": url, "thread_id": thread_id}).encode()
        connection.request("POST", "/fetch", body=request,
                           headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Connection": "close"})
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES or response.status != 200:
            raise SourceToolError(f"Source broker rejected the request (HTTP {response.status}).")
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("record"), dict) or not isinstance(value.get("content_base64"), str):
            raise SourceToolError("Malformed source broker reply.")
        record = value["record"]
        if record.get("requested_url") != url:
            raise SourceToolError("Source broker reply does not match this request.")
        return record, base64.b64decode(value["content_base64"], validate=True)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        if isinstance(exc, SourceToolError):
            raise
        raise SourceToolError("Source broker communication failed.") from exc
    finally:
        if response is not None:
            response.close()
        connection.close()


def _bounded_read(root: Path, relative: str, limit: int) -> bytes:
    try:
        return safe_read(root, relative, max_bytes=limit)
    except SafeIOError as exc:
        # Preserve the source tool's distinction between OS access failures and
        # rejected artifacts while sharing the descriptor-based read checks.
        if isinstance(exc.__cause__, OSError):
            raise exc.__cause__ from None
        raise SourceToolError(str(exc)) from exc


def _pdf_limits():
    # These limits apply only to the in-sandbox extractor subprocess. The
    # original PDF snapshot has already been written by this helper.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_TEXT_BYTES + 65536,) * 2)


def extract_source(config: dict, source_id: str, *, first: int = 1,
                   last: int | None = None) -> dict:
    """Extract a shared PDF in the existing OS sandbox with bounded resources."""
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-f0-9]{32}", source_id):
        raise SourceToolError("Expected one fetched source identifier, not a file path.")
    if (type(first) is not int or not 1 <= first <= 2000
            or (last is not None and (type(last) is not int or not first <= last <= 2000))):
        raise SourceToolError("PDF pages must be ordered one-based integers between 1 and 2000.")
    work = Path(config["work_root"])
    expected = f".audit-work/sources/{source_id}.pdf"
    raw = _bounded_read(work, expected, MAX_SOURCE_BYTES)
    if not raw.startswith(b"%PDF-"):
        raise SourceToolError("Expected a PDF source file.")
    command = config.get("pdf_text_command")
    if (not isinstance(command, list) or not command or len(command) > 4
            or not all(isinstance(part, str) and part and "\x00" not in part for part in command)
            or not Path(command[0]).is_absolute()):
        raise SourceToolError("PDF text extraction is unavailable; configure pdftotext or the bundled PDF text runtime.")
    timeout = config.get("build_timeout", 180)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise SourceToolError("PDF extraction timeout must be positive.")
    timeout = min(timeout, 180)
    # Use the sandbox's current temporary area; never a host archival location.
    temporary_root = Path(os.environ.get("TMPDIR") or work / ".audit-work")
    directory = _root_fd(temporary_root)
    os.close(directory)
    env = {key: os.environ[key] for key in ("HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "PATH")
           if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="source-pdf-text-", dir=temporary_root) as name:
        temporary = Path(name)
        directory = _root_fd(temporary)
        try:
            _write_once(directory, "input.pdf", raw)
        finally:
            os.close(directory)
        args = [*command, "-enc", "UTF-8", "-f", str(first)]
        if last is not None:
            args += ["-l", str(last)]
        args += [str(temporary / "input.pdf"), str(temporary / "output.txt")]
        with tempfile.TemporaryFile(dir=temporary) as errors:
            try:
                result = subprocess.run(args, cwd=temporary, env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=errors, timeout=timeout,
                                        check=False, preexec_fn=_pdf_limits)
            except subprocess.TimeoutExpired as exc:
                raise SourceToolError("PDF text extraction exceeded its time limit.") from exc
            except subprocess.SubprocessError as exc:
                raise SourceToolError("Could not run the constrained PDF text extractor.") from exc
            if result.returncode:
                errors.seek(0)
                detail = errors.read(2048).decode("utf-8", "replace").strip()
                raise SourceToolError(f"PDF text extraction failed (exit {result.returncode}): {detail}")
        content = _bounded_read(temporary, "output.txt", MAX_TEXT_BYTES)
        if not content.strip():
            raise SourceToolError("PDF extraction produced no readable text.")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceToolError("PDF extraction did not produce UTF-8 text.") from exc
    text_path = _save_derivative(work, "text.txt", content)
    return {"source_id": source_id, "text_path": text_path,
            "first_page": first, "last_page": last}


def _save_derivative(work: Path, suffix: str, content: bytes) -> str:
    """Save one directly usable file without a separate acceptance receipt."""
    name = uuid.uuid4().hex + "-" + suffix
    directory = _root_fd(work / ".audit-work/sources")
    try:
        _write_once(directory, name, content)
    finally:
        os.close(directory)
    return f".audit-work/sources/{name}"


def generate_zbmath_bib(config: dict, source_id: str, *, key: str,
                        document_id: str | None = None) -> dict:
    """Prepare a BibTeX candidate from a shared official API response."""
    from zbmath import (MAX_JSON_BYTES, ZbMathError,
                        deterministic_bibtex, is_api_url, parse_response)
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-f0-9]{32}", source_id):
        raise SourceToolError("Expected a fetched source identifier, not a file path.")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}", key):
        raise SourceToolError("Invalid bibliography key; use letters, digits, underscore, dot, colon, plus or hyphen.")
    if document_id is not None and not re.fullmatch(r"[1-9][0-9]{0,11}", str(document_id)):
        raise SourceToolError("Invalid --document-id; expected a positive integer.")
    work = Path(config["work_root"])
    metadata_path = f".audit-work/sources/{source_id}.json"
    source = json.loads(_bounded_read(work, metadata_path, 128 * 1024))
    prefix = f".audit-work/sources/{source_id}"
    if (not isinstance(source, dict) or source.get("status") != "ok"
            or source.get("path") != prefix + ".txt"
            or not is_api_url(source.get("final_url", ""))):
        raise SourceToolError("BibTeX conversion requires a successful official zbMATH API record.")
    raw = _bounded_read(work, source["path"], MAX_JSON_BYTES)
    parsed = parse_response(raw, source["final_url"])
    candidates = parsed["candidates"]
    if parsed["status"] not in ("single", "multiple"):
        raise SourceBibliographyUnavailable("The API response is not a usable publication record: " + parsed["status"] + ".")
    if document_id is not None:
        candidates = [candidate for candidate in candidates if candidate["document_id"] == str(document_id)]
    if len(candidates) != 1 or (parsed["status"] == "multiple" and document_id is None):
        raise SourceBibliographyUnavailable("Choose one verified candidate with --document-id; ambiguous searches cannot silently select a citation.",
                                           "selection_required")
    candidate = candidates[0]
    try:
        content = deterministic_bibtex(candidate, key).encode("utf-8")
    except ZbMathError as exc:
        raise SourceBibliographyUnavailable(str(exc)) from exc
    bibtex_path = _save_derivative(work, "bib.bib", content)
    return {"source_id": source_id, "source_url": source["final_url"],
            "provider": "zbmath_api", "origin": "api_generated",
            "document_id": candidate["document_id"], "bibtex_key": key,
            "bibtex_path": bibtex_path}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch", help="Retrieve one public HTTPS source via the sealed broker.")
    fetch.add_argument("url")
    extract = subparsers.add_parser("extract", help="Extract PDF text inside the manuscript sandbox.")
    extract.add_argument("source_id")
    extract.add_argument("--first", type=int, default=1)
    extract.add_argument("--last", type=int)
    search = subparsers.add_parser("zbmath-search", help="Search official zbMATH API metadata; query syntax includes doi:, an:, arxiv:, and ti:.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--page", type=int, default=0)
    lookup = subparsers.add_parser("zbmath-record", help="Retrieve one official zbMATH document ID through the sealed broker.")
    lookup.add_argument("document_id")
    bib = subparsers.add_parser("zbmath-bib", help="Generate an explicitly API-derived BibTeX candidate from an already fetched official record.")
    bib.add_argument("source_id")
    bib.add_argument("--key", required=True)
    bib.add_argument("--document-id")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        control = Path(__file__).absolute().parent
        config = json.loads(safe_read(control, "round-config.json"))
        if args.command == "extract":
            record = extract_source(config, args.source_id, first=args.first, last=args.last)
            print("SOURCE_TEXT " + json.dumps(record, sort_keys=True, separators=(",", ":")))
            return 0
        if args.command == "zbmath-bib":
            try:
                record = generate_zbmath_bib(config, args.source_id, key=args.key, document_id=args.document_id)
            except SourceBibliographyUnavailable as exc:
                print("ZBMATH_RESULT " + json.dumps({"provider": "zbmath_api", "status": exc.status,
                      "source_id": args.source_id, "warnings": [str(exc)]}, sort_keys=True, separators=(",", ":")))
                return 0
            print("SOURCE_BIB " + json.dumps(record, sort_keys=True, separators=(",", ":")))
            return 0
        work_root = Path(config["work_root"])
        if args.command in ("zbmath-search", "zbmath-record"):
            from zbmath import parse_response, record_url, search_url
            url = search_url(args.query, args.limit, args.page) if args.command == "zbmath-search" else record_url(args.document_id)
        else:
            url = args.url
        record, content = fetch_source(config, url)
        save_source(work_root, record, content)
        if args.command in ("zbmath-search", "zbmath-record"):
            if record["status"] == "ok":
                summary = parse_response(content, record.get("final_url", url))
            else:
                summary = {"provider": "zbmath_api", "status": "retrieval_unavailable", "candidates": [],
                           "error_code": record.get("error_code", "transport_error"),
                           "retryable": record.get("retryable", False),
                           "warnings": ["Use an alternative authoritative source when this lookup is unavailable; do not claim an empty search."]}
            summary["source_id"] = record["source_id"]
            print("ZBMATH_RESULT " + json.dumps(summary, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        print("SOURCE_FETCH " + json.dumps(record, sort_keys=True, separators=(",", ":")))
        # Unavailable API lookups are research outcomes; generic fetch keeps
        # its nonzero exit status so callers can choose how to retry.
        if args.command in ("zbmath-search", "zbmath-record"):
            return 0
        return 0 if record["status"] == "ok" else 2
    except (SourceToolError, SafeIOError, OSError, ValueError, KeyError) as exc:
        print(f"Source retrieval failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
