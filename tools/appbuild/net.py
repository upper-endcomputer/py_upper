# 中文维护说明：下载公开构建输入并处理可重试错误；日志与公开请求不应携带账户凭据。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations

import json
import math
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
    """读取并钳制整数环境选项；解析失败使用默认值。"""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float = 1.0, maximum: float = 300.0) -> float:
    """读取并钳制浮点环境选项；解析失败使用默认值。"""
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    """优先采用合法 Retry-After 秒数，否则指数退避；等待不超过上限。"""
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after:
        try:
            delay = float(retry_after)
            if math.isfinite(delay) and delay >= 0:
                return min(delay, MAX_BACKOFF)
        except (TypeError, ValueError):
            pass
    return min(2.0**attempt, MAX_BACKOFF)


def open_url(request: urllib.request.Request) -> BinaryIO:
    """按有界超时和重试策略打开请求；调用者负责关闭返回响应。"""
    retries = _env_int("APPBUILD_HTTP_RETRIES", DEFAULT_RETRIES)
    timeout = _env_float("APPBUILD_HTTP_TIMEOUT", DEFAULT_TIMEOUT)
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
    """发送公开 JSON 请求并解析响应；自定义 headers 覆盖默认值。"""
    request_headers = {
        "User-Agent": "appbuild",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    with open_url(request) as response:
        return json.load(response)


def http_text(url: str) -> str:
    """下载文本并按 UTF-8 解码；自动关闭响应。"""
    request = urllib.request.Request(url, headers={"User-Agent": "appbuild", "Accept": "text/plain"})
    with open_url(request) as response:
        return response.read().decode("utf-8")


def download(url: str, destination: Path, headers: dict[str, str] | None = None) -> None:
    """流式下载到同目录临时文件并原子替换；已有文件直接复用，摘要由调用方校验。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    request_headers = {"User-Agent": "appbuild"}
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
