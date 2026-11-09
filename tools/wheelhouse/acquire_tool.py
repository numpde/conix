# Ported from numpde/magnet-deploy@fcb9459ca1facd8a6fc18a5acda271549f3a5e6e
# scripts/acquire-wheelhouse-tool.py; same published wheelhouse-tool v0.5.14.
"""Acquire the pinned shared tool before it can acquire wheelhouse artifacts."""

import fcntl
import errno
import hashlib
import http.client
import json
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class InsecureRedirect(ValueError):
    """The published tool's HTTPS-only retrieval policy was violated."""


class TransientDownload(OSError):
    """A classified failure that permits a bounded retry of acquisition."""

    def __init__(self, detail, retry_after=0):
        super().__init__(detail)
        self.retry_after = retry_after


def retry_after_seconds(headers):
    """Parse a bounded Retry-After hint without trusting arbitrary headers."""
    value = headers.get('Retry-After') if headers else None
    if not isinstance(value, str) or len(value) > 128:
        return 0
    try:
        if value.isdecimal():
            return int(value)
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            return 0
        return max(0, (when - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 0


def network_failure(error):
    """Classify only known transient transport faults for acquisition retries."""
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    if isinstance(error, ssl.SSLError):
        return 'TLS verification or protocol failed', False
    if isinstance(error, socket.gaierror):
        return ('temporary DNS lookup failure', True) if error.errno == socket.EAI_AGAIN else ('DNS lookup failed', False)
    if isinstance(error, OSError) and error.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        return f'network connection failed ({errno.errorcode[error.errno]})', True
    if isinstance(error, OSError) and error.errno is None:
        tunnel = re.match(r'^Tunnel connection failed: ([0-9]{3})(?:\s|$)', str(error))
        if tunnel:
            status = int(tunnel[1])
            if status in (429, 503):
                return f'proxy tunnel returned HTTP {status}; Retry-After is unavailable', False
            return f'proxy tunnel returned HTTP {status}', status in (408, 500, 502, 504)
    if isinstance(error, TimeoutError):
        return 'network operation timed out', True
    if isinstance(error, (ConnectionResetError, ConnectionRefusedError, ConnectionAbortedError, BrokenPipeError,
                          http.client.RemoteDisconnected, http.client.IncompleteRead)):
        return 'network connection was interrupted', True
    return 'network connection failed', False


def download(url, target, expected):
    """Stream a bounded HTTPS asset and verify its complete pinned digest."""
    try:
        source = urllib.request.build_opener(HttpsRedirects()).open(url, timeout=60)
    except urllib.error.HTTPError as error:
        detail = f'the release server returned HTTP {error.code}'
        delay = retry_after_seconds(error.headers) if error.code in (429, 503) else 0
        error.close()
        if error.code in (408, 429, 500, 502, 503, 504):
            if delay > 30:
                raise OSError(f'{detail}; Retry-After exceeds 30 seconds') from None
            raise TransientDownload(detail, delay) from None
        raise OSError(detail) from None
    except (OSError, http.client.HTTPException) as error:
        detail, transient = network_failure(error)
        if transient:
            raise TransientDownload(detail) from None
        raise OSError(detail) from None
    with source:
        digest = hashlib.sha256()
        size = 0
        deadline = time.monotonic() + 300
        length = source.headers.get('Content-Length')
        length = int(length) if isinstance(length, str) and length.isdecimal() else None
        while True:
            if time.monotonic() >= deadline:
                raise TransientDownload('the release download exceeded its read time limit')
            try:
                block = source.read1(1024 * 1024)
            except (OSError, http.client.HTTPException) as error:
                detail, transient = network_failure(error)
                if transient:
                    raise TransientDownload(detail) from None
                raise OSError(detail) from None
            if not block:
                break
            size += len(block)
            if size > 16 * 1024 * 1024:
                raise ValueError('the release asset exceeds 16 MiB')
            digest.update(block)
            target.write(block)
        if length is not None and size < length:
            raise TransientDownload('the release download ended before its declared length')
        if digest.hexdigest() != expected:
            raise ValueError('downloaded tool bytes differ from the pinned release')


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse an HTTPS-to-HTTP redirect before any follow-up request."""

    def redirect_request(self, request, fp, code, message, headers, new_url):
        if urllib.parse.urlsplit(new_url).scheme != "https":
            raise InsecureRedirect("the release asset redirected to a non-HTTPS URL")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def cached(directory, name, expected):
    """Admit an existing regular file only by exact released SHA-256."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("the existing tool is not a regular file")
        if metadata.st_size > 16 * 1024 * 1024:
            raise ValueError("the existing tool exceeds 16 MiB; it was preserved")
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError("the existing tool differs from the pinned release; it was preserved")
    return True


def acquire(pool, pin):
    """Publish one pinned tool without clobbering concurrent pool users."""
    release = json.loads(pin.read_text(encoding="utf-8"))
    version, expected = release["version"], release["sha256"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("the tool release pin is malformed")
    name = f"wheelhouse-tool-{version}.pyz"
    directory = os.open(pool, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Check under a shared lock; upgrade to exclusive only for a missing
        # tool, then recheck because another producer may have won the race.
        fcntl.flock(directory, fcntl.LOCK_SH)
        if cached(directory, name, expected):
            return
        fcntl.flock(directory, fcntl.LOCK_UN)
        fcntl.flock(directory, fcntl.LOCK_EX)
        if cached(directory, name, expected):
            return
        url = f"https://github.com/numpde/nmr-provider-tools/releases/download/v{version}/{name}"
        for attempt in range(1, 4):
            temporary = f".wheelhouse-tool-{secrets.token_hex(16)}.part"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            transient = None
            try:
                with os.fdopen(descriptor, "wb") as target:
                    download(url, target, expected)
                    target.flush()
                    os.fchmod(target.fileno(), 0o644)
                    os.fsync(target.fileno())
                # Hardlink creation is atomic and cannot overwrite a cache
                # entry published by another acquisition worker.
                try:
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                except FileExistsError:
                    if not cached(directory, name, expected):
                        raise FileNotFoundError("the competing tool file disappeared")
                os.fsync(directory)
            except TransientDownload as error:
                transient = error
            finally:
                primary_failure = sys.exc_info()[0] is not None
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    if transient is not None:
                        raise OSError(f"{transient}; the temporary download could not be removed and may remain in WHEELHOUSE") from None
                    if not primary_failure:
                        raise
                    print("Cannot remove the temporary wheelhouse-tool download; it may remain in WHEELHOUSE.", file=sys.stderr)
            if transient is None:
                return
            if attempt == 3:
                raise OSError(f"{transient} after {attempt} attempts") from None
            delay = max(attempt, transient.retry_after)
            print(f"Shared wheelhouse tool download: {transient}; retrying in {delay:g} seconds (attempt {attempt + 1} of 3).", file=sys.stderr)
            time.sleep(delay)
    finally:
        os.close(directory)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("supply the wheelhouse and pinned tool release file")
        acquire(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
        detail = os.strerror(error.errno) if isinstance(error, OSError) and error.errno else str(error)
        print(f"Cannot acquire the shared wheelhouse tool: {detail}.", file=sys.stderr)
        sys.exit(2)
