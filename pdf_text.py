#!/usr/bin/env python3
"""Offline PDF text fallback, executed only inside the manuscript OS sandbox.

A small pdftotext-compatible interface for plain text.
It uses the bundled pdfplumber runtime and does not execute PDF actions, fetch
URLs, run TeX, or load external application configuration.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import os
import stat
import sys

MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_PAGES = 2000


class PDFTextError(ValueError):
    pass


def _input(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_PDF_BYTES:
            raise PDFTextError('PDF input must be a unique regular file no larger than 64 MiB.')
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def _output(path: Path, content: bytes, source: Path):
    if path.absolute() == source.absolute():
        raise PDFTextError('PDF input and text output must be different files.')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PDFTextError('Text output must be a unique regular file.')
        if (info.st_dev, info.st_ino) == (source.stat().st_dev, source.stat().st_ino):
            raise PDFTextError('PDF input and text output must be different files.')
        os.ftruncate(fd, 0)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(content)
    finally:
        os.close(fd)


def extract(path: Path, *, first: int = 1, last: int | None = None,
            layout: bool = False, page_breaks: bool = True) -> bytes:
    import pdfplumber

    texts = []
    with _input(path) as stream, pdfplumber.open(stream) as pdf:
        count = len(pdf.pages)
        if not 0 < count <= MAX_PAGES:
            raise PDFTextError('PDF page count must be between 1 and 2000.')
        final = count if last is None else min(last, count)
        if not 1 <= first <= final:
            raise PDFTextError('Requested page range is empty or outside the PDF.')
        for number in range(first, final + 1):
            page = pdf.pages[number - 1]
            texts.append(page.extract_text(layout=layout, x_tolerance=2, y_tolerance=3) or '')
            page.close()
    separator = '\n\f\n' if page_breaks else '\n'
    return (separator.join(texts) + '\n').encode('utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-layout', action='store_true', help='Preserve spatial layout in plain text.')
    parser.add_argument('-enc', default='UTF-8', help='Only UTF-8 output is supported.')
    parser.add_argument('-f', type=int, default=1, dest='first', help='First page, one-based.')
    parser.add_argument('-l', type=int, dest='last', help='Last page, inclusive.')
    parser.add_argument('-nopgbrk', action='store_true')
    parser.add_argument('input', type=Path)
    parser.add_argument('output', nargs='?', help="Output filename, or '-' for stdout.")
    args = parser.parse_args(argv)
    try:
        if args.enc.lower().replace('-', '') != 'utf8':
            raise PDFTextError('Only UTF-8 output is supported.')
        raw = extract(args.input, first=args.first, last=args.last,
                      layout=args.layout, page_breaks=not args.nopgbrk)
        if args.output == '-':
            sys.stdout.buffer.write(raw)
        else:
            target = Path(args.output) if args.output else args.input.with_suffix('.txt')
            _output(target, raw, args.input)
        return 0
    except Exception as exc:
        print(f'PDF text extraction failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
