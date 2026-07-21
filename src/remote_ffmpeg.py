# Upload Assistant © 2025 Audionut & wastaken7 — Licensed under UAPL v1.0
import asyncio
import contextlib
import os
import struct
import tempfile
import zlib
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Optional

import httpx

from src.console import console

FFmpegResult = tuple[Optional[int], bytes, bytes]
MAX_REMOTE_PNG_BYTES = 64 * 1024 * 1024
TRANSIENT_WORKER_STATUSES = {409, 429, 503}
REMOTE_BUSY_ATTEMPTS = 3


def _setting(settings: Optional[Mapping[str, object]], key: str, env_name: str, default: object) -> object:
    if settings and key in settings:
        return settings[key]
    return os.getenv(env_name, str(default))


def _flag(settings: Optional[Mapping[str, object]], key: str, env_name: str, default: bool) -> bool:
    value = _setting(settings, key, env_name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def remote_ffmpeg_enabled(settings: Optional[Mapping[str, object]] = None) -> bool:
    return _flag(settings, "remote_ffmpeg_enabled", "UA_REMOTE_FFMPEG_ENABLED", False)


def _relative_media_path(path: str, configured_root: str) -> str:
    path_root = os.path.realpath(configured_root)
    source_path = os.path.realpath(path)
    try:
        if os.path.commonpath((path_root, source_path)) != path_root:
            raise ValueError
    except ValueError as error:
        raise ValueError(f"Path is outside UA_REMOTE_FFMPEG_PATH_ROOT: {path}") from error
    return os.path.relpath(source_path, path_root)


def _validate_png(content: bytes) -> None:
    if len(content) > MAX_REMOTE_PNG_BYTES:
        raise ValueError("Remote FFmpeg response exceeds the maximum PNG size")
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Remote FFmpeg response is not a PNG")

    offset = 8
    chunk_index = 0
    found_idat = False
    found_iend = False
    while offset < len(content):
        if offset + 12 > len(content):
            raise ValueError("Remote FFmpeg response contains a truncated PNG chunk")
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        chunk_type = content[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(content):
            raise ValueError("Remote FFmpeg response contains a truncated PNG chunk")
        chunk_data = content[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", content[offset + 8 + length : chunk_end])[0]
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            raise ValueError("Remote FFmpeg response contains an invalid PNG checksum")

        if chunk_index == 0:
            if chunk_type != b"IHDR" or length != 13:
                raise ValueError("Remote FFmpeg response has an invalid PNG header")
            width, height = struct.unpack(">II", chunk_data[:8])
            if not 0 < width <= 32768 or not 0 < height <= 32768:
                raise ValueError("Remote FFmpeg response has invalid PNG dimensions")
        elif chunk_type == b"IDAT":
            found_idat = True
        elif chunk_type == b"IEND":
            if length != 0 or chunk_end != len(content):
                raise ValueError("Remote FFmpeg response has an invalid PNG terminator")
            found_iend = True
            break

        offset = chunk_end
        chunk_index += 1

    if not found_idat or not found_iend:
        raise ValueError("Remote FFmpeg response is not a complete PNG")


def _write_remote_png(content: bytes, output_path: str) -> None:
    _validate_png(content)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_path = tempfile.mkstemp(prefix=f".{output.name}.remote-", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(file_descriptor, "wb") as image_file:
            image_file.write(content)
            image_file.flush()
            os.fsync(image_file.fileno())
        os.replace(temp_path, output)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(temp_path)


async def run_screenshot_ffmpeg(
    local_runner: Callable[[], Awaitable[FFmpegResult]],
    path: str,
    output_path: str,
    parameters: Mapping[str, object],
    settings: Optional[Mapping[str, object]] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FFmpegResult:
    """Run an eligible screenshot remotely, preserving the supplied local fallback."""
    if not remote_ffmpeg_enabled(settings):
        return await local_runner()

    try:
        url = str(_setting(settings, "remote_ffmpeg_url", "UA_REMOTE_FFMPEG_URL", "")).strip()
        token = str(_setting(settings, "remote_ffmpeg_token", "UA_REMOTE_FFMPEG_TOKEN", "")).strip()
        if not url or not token:
            raise RuntimeError("UA_REMOTE_FFMPEG_URL and UA_REMOTE_FFMPEG_TOKEN are required")
        try:
            timeout = float(_setting(settings, "remote_ffmpeg_timeout", "UA_REMOTE_FFMPEG_TIMEOUT", 180))
            if timeout <= 0:
                raise ValueError
        except ValueError as error:
            raise ValueError("UA_REMOTE_FFMPEG_TIMEOUT must be a positive number") from error

        payload = dict(parameters)
        payload["path"] = _relative_media_path(path, str(_setting(settings, "remote_ffmpeg_path_root", "UA_REMOTE_FFMPEG_PATH_ROOT", "/media/torrents")))
        headers = {"Authorization": f"Bearer {token}", "Accept": "image/png"}
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            for attempt in range(REMOTE_BUSY_ATTEMPTS):
                response = await client.post(f"{url.rstrip('/')}/v1/ffmpeg", headers=headers, json=payload)
                if response.status_code not in TRANSIENT_WORKER_STATUSES or attempt == REMOTE_BUSY_ATTEMPTS - 1:
                    response.raise_for_status()
                    break
                delay = 0.5 * (2**attempt)
                console.print(
                    f"[yellow]Remote FFmpeg worker busy/unavailable (HTTP {response.status_code}); "
                    f"retrying in {delay:.1f}s[/yellow]"
                )
                await asyncio.sleep(delay)
        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "image/png":
            raise ValueError("Remote FFmpeg response has an unexpected content type")
        await asyncio.to_thread(_write_remote_png, response.content, output_path)
        return 0, b"", b""
    except Exception as error:
        console.print(f"[bold red]Error using remote FFmpeg: {error}")
        if not _flag(settings, "remote_ffmpeg_fallback", "UA_REMOTE_FFMPEG_FALLBACK", True):
            raise
        console.print("[yellow]Falling back to local FFmpeg")
        return await local_runner()
