import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import httpx

from src.remote_ffmpeg import run_screenshot_ffmpeg


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)


def valid_png() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x00\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", pixels) + png_chunk(b"IEND", b"")


class RemoteFFmpegTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_uses_unchanged_local_runner(self) -> None:
        calls = 0

        async def local_runner() -> tuple[int, bytes, bytes]:
            nonlocal calls
            calls += 1
            return 7, b"local stdout", b"local stderr"

        with patch.dict(os.environ, {"UA_REMOTE_FFMPEG_ENABLED": "false"}, clear=True):
            result = await run_screenshot_ffmpeg(local_runner, "/media/torrents/movie.mkv", "/tmp/unused.png", {})

        self.assertEqual(result, (7, b"local stdout", b"local stderr"))
        self.assertEqual(calls, 1)

    async def test_success_posts_structured_request_and_atomically_writes_png(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "media" / "movie.mkv"
            source.parent.mkdir()
            source.write_bytes(b"media")
            output = root / "output.png"
            output.write_bytes(b"old")
            local_calls = 0

            async def local_runner() -> tuple[int, bytes, bytes]:
                nonlocal local_calls
                local_calls += 1
                return 1, b"", b"failed"

            async def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.path, "/v1/ffmpeg")
                self.assertEqual(request.headers["Authorization"], "Bearer secret")
                payload = json.loads(request.content)
                self.assertEqual(
                    payload,
                    {
                        "path": "media/movie.mkv",
                        "seek_seconds": 123.5,
                        "width": 1920.0,
                        "height": 1080.0,
                        "sar_width": 1.0,
                        "sar_height": 1.0,
                        "tonemap": "zscale",
                        "tonemap_algorithm": "mobius",
                        "desaturation": 10.0,
                        "compression_level": 6,
                    },
                )
                return httpx.Response(200, content=valid_png(), headers={"content-type": "image/png", "cache-control": "no-store"})

            environment = {
                "UA_REMOTE_FFMPEG_ENABLED": "true",
                "UA_REMOTE_FFMPEG_URL": "http://worker:8080/",
                "UA_REMOTE_FFMPEG_TOKEN": "secret",
                "UA_REMOTE_FFMPEG_PATH_ROOT": os.fspath(root),
                "UA_REMOTE_FFMPEG_TIMEOUT": "10",
            }
            parameters = {
                "seek_seconds": 123.5,
                "width": 1920.0,
                "height": 1080.0,
                "sar_width": 1.0,
                "sar_height": 1.0,
                "tonemap": "zscale",
                "tonemap_algorithm": "mobius",
                "desaturation": 10.0,
                "compression_level": 6,
            }
            with patch.dict(os.environ, environment, clear=True):
                result = await run_screenshot_ffmpeg(local_runner, os.fspath(source), os.fspath(output), parameters, transport=httpx.MockTransport(handler))

            self.assertEqual(result, (0, b"", b""))
            self.assertEqual(output.read_bytes(), valid_png())
            self.assertEqual(local_calls, 0)
            self.assertEqual(list(root.glob(".output.png.remote-*.tmp")), [])

    async def test_worker_outage_falls_back_to_local_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.mkv"
            source.write_bytes(b"media")
            output = root / "output.png"
            calls = 0

            async def local_runner() -> tuple[int, bytes, bytes]:
                nonlocal calls
                calls += 1
                output.write_bytes(b"local")
                return 0, b"", b""

            async def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(503, json={"error": "unavailable", "message": "try later"})

            environment = {
                "UA_REMOTE_FFMPEG_ENABLED": "true",
                "UA_REMOTE_FFMPEG_URL": "http://worker:8080",
                "UA_REMOTE_FFMPEG_TOKEN": "secret",
                "UA_REMOTE_FFMPEG_PATH_ROOT": os.fspath(root),
                "UA_REMOTE_FFMPEG_FALLBACK": "true",
            }
            with patch.dict(os.environ, environment, clear=True):
                result = await run_screenshot_ffmpeg(local_runner, os.fspath(source), os.fspath(output), {}, transport=httpx.MockTransport(handler))

            self.assertEqual(result, (0, b"", b""))
            self.assertEqual(output.read_bytes(), b"local")
            self.assertEqual(calls, 1)

    async def test_invalid_helper_output_does_not_replace_existing_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.mkv"
            source.write_bytes(b"media")
            output = root / "output.png"
            output.write_bytes(b"existing")
            local_calls = 0

            async def local_runner() -> tuple[int, bytes, bytes]:
                nonlocal local_calls
                local_calls += 1
                return 0, b"", b""

            async def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, content=b"<script>not an image</script>", headers={"content-type": "image/png"})

            environment = {
                "UA_REMOTE_FFMPEG_ENABLED": "true",
                "UA_REMOTE_FFMPEG_URL": "http://worker:8080",
                "UA_REMOTE_FFMPEG_TOKEN": "secret",
                "UA_REMOTE_FFMPEG_PATH_ROOT": os.fspath(root),
                "UA_REMOTE_FFMPEG_FALLBACK": "false",
            }
            with patch.dict(os.environ, environment, clear=True), self.assertRaises(ValueError):
                await run_screenshot_ffmpeg(local_runner, os.fspath(source), os.fspath(output), {}, transport=httpx.MockTransport(handler))

            self.assertEqual(output.read_bytes(), b"existing")
            self.assertEqual(local_calls, 0)
            self.assertEqual(list(root.glob(".output.png.remote-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
