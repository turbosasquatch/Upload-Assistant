import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from src.torrentcreate import TorrentCreator


class RemoteMkbrrTests(unittest.IsolatedAsyncioTestCase):
    async def test_processing_settings_enable_remote_mkbrr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'release.mkv'
            source.write_bytes(b'media')
            (root / 'tmp' / 'test-id').mkdir(parents=True)
            output = root / 'tmp' / 'test-id' / 'BASE.torrent'
            meta = {
                'base_dir': os.fspath(root), 'uuid': 'test-id', 'mkbrr': True, 'mkbrr_threads': '0',
                'keep_folder': False, 'isdir': False, 'is_disc': False, 'filelist': [os.fspath(source)],
                'randomized': 0, 'trackers': [], 'debug': False,
            }
            settings = {
                'remote_mkbrr_enabled': True, 'remote_mkbrr_url': 'http://worker:8080',
                'remote_mkbrr_token': 'from-processing-tab', 'remote_mkbrr_path_root': os.fspath(root),
            }
            valid_torrent = SimpleNamespace(metainfo={'info': {'name': 'release', 'piece length': 4_194_304, 'pieces': b'x' * 20}})
            with patch.dict(os.environ, {'UA_REMOTE_MKBRR_ENABLED': 'false'}, clear=True), patch.object(
                TorrentCreator, 'create_remote_mkbrr', autospec=True
            ) as remote, patch('src.torrentcreate.Torrent.read', return_value=valid_torrent):
                TorrentCreator.set_processing_config(settings)
                try:
                    await TorrentCreator.create_torrent(meta, source, 'BASE')
                    remote.assert_awaited_once()
                    self.assertEqual(remote.await_args.args[3]['remote_mkbrr_token'], 'from-processing-tab')
                finally:
                    TorrentCreator.set_processing_config({})

    async def test_remote_failure_with_fallback_runs_local_mkbrr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'release.mkv'
            source.write_bytes(b'media')
            (root / 'tmp' / 'test-id').mkdir(parents=True)
            binary = root / 'mkbrr'
            binary.write_bytes(b'')
            commands: list[list[str]] = []

            class FakeProcess:
                stdout: list[str] = []

                def __init__(self, command: list[str], **_kwargs: object) -> None:
                    commands.append(command)
                    Path(command[command.index('-o') + 1]).write_bytes(b'local torrent')

                def wait(self) -> int:
                    return 0

            meta = {
                'base_dir': os.fspath(root),
                'uuid': 'test-id',
                'mkbrr': True,
                'mkbrr_threads': '0',
                'keep_folder': False,
                'isdir': False,
                'is_disc': False,
                'filelist': [os.fspath(source)],
                'randomized': 0,
                'trackers': [],
                'debug': False,
            }
            environment = {
                'UA_REMOTE_MKBRR_ENABLED': 'true',
                'UA_REMOTE_MKBRR_FALLBACK': 'true',
            }
            with patch.dict(os.environ, environment, clear=False), patch.object(
                TorrentCreator, 'create_remote_mkbrr', side_effect=httpx.ConnectError('offline')
            ), patch.object(TorrentCreator, 'get_mkbrr_path', return_value=os.fspath(binary)), patch(
                'src.torrentcreate.subprocess.Popen', side_effect=FakeProcess
            ):
                result = await TorrentCreator.create_torrent(meta, source, 'BASE')

            self.assertEqual(result, os.fspath(root / 'tmp' / 'test-id' / 'BASE.torrent'))
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][:3], [os.fspath(binary), 'create', os.fspath(source)])

    async def test_posts_structured_request_and_writes_valid_torrent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'media' / 'release'
            source.mkdir(parents=True)
            output = root / 'result.torrent'

            async def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.headers['Authorization'], 'Bearer secret')
                self.assertEqual(request.url.path, '/v1/mkbrr')
                payload = json.loads(request.content)
                self.assertEqual(payload['path'], 'media/release')
                self.assertEqual(payload['piece_length'], 22)
                return httpx.Response(200, content=b'torrent bytes', headers={'content-type': 'application/x-bittorrent'})

            environment = {
                'UA_REMOTE_MKBRR_URL': 'http://worker:8080/',
                'UA_REMOTE_MKBRR_TOKEN': 'secret',
                'UA_REMOTE_MKBRR_PATH_ROOT': os.fspath(root),
                'UA_REMOTE_MKBRR_TIMEOUT': '10',
            }
            valid_torrent = SimpleNamespace(metainfo={'info': {'name': 'release', 'piece length': 4_194_304, 'pieces': b'x' * 20}})
            with patch.dict(os.environ, environment, clear=False), patch('src.torrentcreate.Torrent.read', return_value=valid_torrent):
                await TorrentCreator.create_remote_mkbrr(
                    source,
                    os.fspath(output),
                    {'piece_length': 22, 'tracker_url': '', 'randomized': False, 'include': [], 'exclude': []},
                    transport=httpx.MockTransport(handler),
                )

            self.assertEqual(output.read_bytes(), b'torrent bytes')

    async def test_http_failure_does_not_write_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'release'
            source.mkdir()
            output = root / 'result.torrent'

            async def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(503, json={'error': 'unavailable', 'message': 'try later'})

            environment = {
                'UA_REMOTE_MKBRR_URL': 'http://worker:8080',
                'UA_REMOTE_MKBRR_TOKEN': 'secret',
                'UA_REMOTE_MKBRR_PATH_ROOT': os.fspath(root),
            }
            with patch.dict(os.environ, environment, clear=False), self.assertRaises(httpx.HTTPStatusError):
                await TorrentCreator.create_remote_mkbrr(
                    source,
                    os.fspath(output),
                    {},
                    transport=httpx.MockTransport(handler),
                )

            self.assertFalse(output.exists())

    async def test_invalid_torrent_does_not_replace_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'release'
            source.mkdir()
            output = root / 'result.torrent'
            output.write_bytes(b'existing')

            async def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, content=b'not a torrent')

            environment = {
                'UA_REMOTE_MKBRR_URL': 'http://worker:8080',
                'UA_REMOTE_MKBRR_TOKEN': 'secret',
                'UA_REMOTE_MKBRR_PATH_ROOT': os.fspath(root),
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                'src.torrentcreate.Torrent.read', side_effect=ValueError('invalid')
            ), self.assertRaises(ValueError):
                await TorrentCreator.create_remote_mkbrr(
                    source,
                    os.fspath(output),
                    {},
                    transport=httpx.MockTransport(handler),
                )

            self.assertEqual(output.read_bytes(), b'existing')
            self.assertFalse(Path(f'{output}.remote.tmp').exists())


if __name__ == '__main__':
    unittest.main()
