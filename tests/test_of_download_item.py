"""Tests for the per-item OnlyFans download logic (download_media_item).

Covers the counters shown in the GUI (Downloaded / Duplicates), skipping files
that already exist, repairing files that were saved as "video" but contain the
preview image, and skipping DRM protected media without writing anything.
"""

import threading
import unittest
import uuid

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from download.downloadstate import DownloadState
from download_of.media_download import build_media_entry
from download_of.timeline import download_media_item


TEST_TMP_ROOT = Path(__file__).resolve().parent.parent / '.testtmp'
TEST_TMP_ROOT.mkdir(exist_ok=True)

JPEG_BYTES = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 2048


def make_mp4(duration_seconds: float = 30.0, timescale: int = 1000) -> bytes:
    """Minimal MP4 with a known duration and enough bytes to look real."""

    def box(box_type: bytes, payload: bytes) -> bytes:
        return (8 + len(payload)).to_bytes(4, 'big') + box_type + payload

    mvhd = (
        bytes([0, 0, 0, 0])
        + b'\x00' * 8
        + timescale.to_bytes(4, 'big')
        + int(duration_seconds * timescale).to_bytes(4, 'big')
        + b'\x00' * 80
    )

    return (
        box(b'ftyp', b'isom' + b'\x00' * 16)
        + box(b'moov', box(b'mvhd', mvhd))
        + box(b'mdat', b'\x00' * (96 * 1024))
    )


REAL_VIDEO = make_mp4(30)


class Handler(BaseHTTPRequestHandler):
    routes = {
        '/photo.jpg': (JPEG_BYTES, 'image/jpeg'),
        '/video.mp4': (REAL_VIDEO, 'video/mp4'),
    }

    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802
        body, content_type = self.routes.get(self.path.split('?', 1)[0], (None, None))

        if body is None:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()

        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class FakeAuth:
    def get_cookies(self):
        return {'sess': 'dummy', 'auth_id': '1'}


class FakeApi:
    def __init__(self):
        self.session = requests.Session()
        self.auth = FakeAuth()


class FakeConfig:
    def __init__(self):
        self.show_downloads = False
        self.show_skipped_downloads = True
        self.gui_mode = False
        self.max_retries = 1
        self.stop_flag = None
        self.sess = 'dummy'
        self.auth_id = '1'
        self.auth_uid = None
        self.user_agent = 'test-agent'
        self.x_bc = 'test-bc'
        self._api = FakeApi()

    def get_api(self):
        return self._api


class DownloadItemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.prefix = f'{uuid.uuid4().hex[:10]}_'
        self.folder = TEST_TMP_ROOT
        self.config = FakeConfig()
        self.state = DownloadState(creator_name='tester')
        self.state.base_path = self.folder
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for item in self.folder.glob(f'{self.prefix}*'):
            try:
                item.unlink()
            except OSError:
                pass

    def url(self, path: str) -> str:
        return f'{self.base}{path}'

    def make_item(self, media_id: int, media: dict):
        return build_media_entry(media, container_id=f'{self.prefix}post')

    def test_new_photo_is_downloaded_and_counted(self):
        media = {
            'id': 101,
            'type': 'photo',
            'files': {'full': {'url': self.url('/photo.jpg')}},
        }
        item = build_media_entry(media, container_id=f'{self.prefix}post')
        stats = {'downloaded': 0, 'skipped': 0, 'drm': 0, 'failed': 0}

        self.assertTrue(download_media_item(self.config, self.state, item, stats))
        self.assertEqual(stats['downloaded'], 1)
        self.assertTrue((self.folder / item['filename']).exists())

    def test_existing_file_is_skipped_and_counted_as_duplicate(self):
        media = {
            'id': 102,
            'type': 'photo',
            'files': {'full': {'url': self.url('/photo.jpg')}},
        }
        item = build_media_entry(media, container_id=f'{self.prefix}post')
        (self.folder / item['filename']).write_bytes(JPEG_BYTES)

        stats = {'downloaded': 0, 'skipped': 0, 'drm': 0, 'failed': 0}

        self.assertFalse(download_media_item(self.config, self.state, item, stats))
        self.assertEqual(stats['skipped'], 1)

    def test_drm_video_is_skipped_without_writing(self):
        media = {
            'id': 103,
            'type': 'video',
            'duration': 1500,
            'files': {
                'preview': {'url': self.url('/photo.jpg')},
                'drm': {'manifest': {'hls': self.url('/video.mp4')}, 'signature': {}},
            },
        }
        item = build_media_entry(media, container_id=f'{self.prefix}post')
        stats = {'downloaded': 0, 'skipped': 0, 'drm': 0, 'failed': 0}

        self.assertFalse(download_media_item(self.config, self.state, item, stats))
        self.assertEqual(stats['drm'], 1)
        self.assertFalse(list(self.folder.glob(f"{Path(item['filename']).stem}*")))

    def test_fake_mp4_on_disk_is_replaced_by_the_real_video(self):
        media = {
            'id': 104,
            'type': 'video',
            'duration': 30,
            'files': {'full': {'url': self.url('/video.mp4')}},
        }
        item = build_media_entry(media, container_id=f'{self.prefix}post')
        target = self.folder / item['filename']
        target.write_bytes(JPEG_BYTES)  # what older versions produced

        stats = {'downloaded': 0, 'skipped': 0, 'drm': 0, 'failed': 0}

        self.assertTrue(download_media_item(self.config, self.state, item, stats))
        self.assertEqual(stats['downloaded'], 1)
        self.assertGreater(target.stat().st_size, 96 * 1024)


if __name__ == '__main__':
    unittest.main()
