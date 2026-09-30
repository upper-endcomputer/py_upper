from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
DEFAULT_TIMEOUT = 45.0
DEFAULT_RETRIES = 4
MAX_BACKOFF = 20.0


def _env_int(name: str, default: int, minimum: int = 0, maximum: int = 10) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float = 1.0, maximum: float = 300.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after:
        try:
            return min(float(retry_after), MAX_BACKOFF)
        except (TypeError, ValueError):
            pass
    return min(2.0**attempt, MAX_BACKOFF)


def open_url(request: urllib.request.Request) -> BinaryIO:
    retries = _env_int("PY_UPPER_HTTP_RETRIES", DEFAULT_RETRIES)
    timeout = _env_float("PY_UPPER_HTTP_TIMEOUT", DEFAULT_TIMEOUT)
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt >= retries:
                raise
            delay = _retry_delay(exc, attempt)
            print(
                f"Network request failed (HTTP {exc.code}) for {request.full_url}; "
                f"retrying in {delay:g}s ({attempt + 1}/{retries})"
            )
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= retries:
                raise
            delay = min(2.0**attempt, MAX_BACKOFF)
            print(
                f"Network request failed for {request.full_url}: {exc}; "
                f"retrying in {delay:g}s ({attempt + 1}/{retries})"
            )
            time.sleep(delay)

    assert last_error is not None
    raise last_error


def http_json(url: str, headers: dict[str, str] | None = None) -> Any:
    request_headers = {
        "User-Agent": "py_upper",
        "Accept": "application/vnd.github+json",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    with open_url(request) as response:
        return json.load(response)


def download(url: str, destination: Path, headers: dict[str, str] | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    request_headers = {"User-Agent": "py_upper"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    partial = destination.with_name(destination.name + ".part")
    if partial.exists():
        partial.unlink()
    try:
        with open_url(request) as response, partial.open("wb") as stream:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                stream.write(chunk)
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
