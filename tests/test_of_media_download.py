"""Tests for the OnlyFans media download pipeline.

Covers the "long videos are saved as an image in an mp4 file" bug: OnlyFans
returns a preview image when it has no plain MP4 for a DRM protected video, and
the downloader must never store that image under a video extension.

The tests only create *files* in ``.testtmp`` (never nested directories),
because sandboxed runs cannot access directories created by a child process.
"""

import threading
import unittest
import uuid

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests

from download_of.media_download import (
    StopRequested,
    build_media_entry,
    check_media_file,
    collect_candidates,
    download_media_source,
    download_progressive,
    drm_details,
    has_drm,
    is_fake_video_file,
    mp4_duration_seconds,
    sniff_signature,
)
from errors import DRMProtectedError, DownloadError


#region Fixtures


TEST_TMP_ROOT = Path(__file__).resolve().parent.parent / '.testtmp'
TEST_TMP_ROOT.mkdir(exist_ok=True)


class TempWorkspace:
    """Writes uniquely named files into a writable folder and cleans up."""

    def __init__(self):
        self.prefix = f'{uuid.uuid4().hex[:12]}_'
        self.root = TEST_TMP_ROOT

    def path(self, name: str) -> Path:
        return self.root / f'{self.prefix}{name}'

    def stem(self, name: str) -> str:
        return f'{self.prefix}{name}'

    def cleanup(self) -> None:
        for item in self.root.glob(f'{self.prefix}*'):
            try:
                item.unlink()
            except OSError:
                pass

    def leftover_files(self) -> list:
        return sorted(
            item.name for item in self.root.glob(f'{self.prefix}*')
            if item.is_file()
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup()


JPEG_BYTES = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00' + b'\x00' * 4096


def _box(box_type: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, 'big') + box_type + payload


def make_mp4(duration_seconds: float = 12.0, timescale: int = 1000,
             payload_bytes: int = 96 * 1024) -> bytes:
    """Builds a minimal MP4 (ftyp + moov/mvhd + mdat) with a known duration."""

    duration = int(duration_seconds * timescale)
    mvhd = (
        bytes([0, 0, 0, 0])  # version 0 + flags
        + b'\x00' * 8  # creation / modification time
        + timescale.to_bytes(4, 'big')
        + duration.to_bytes(4, 'big')
        + b'\x00' * 80
    )

    return (
        _box(b'ftyp', b'isom' + b'\x00' * 16)
        + _box(b'moov', _box(b'mvhd', mvhd))
        + _box(b'mdat', b'\x00' * payload_bytes)
    )


REAL_VIDEO = make_mp4(12.0)


class MediaHandler(BaseHTTPRequestHandler):
    """Tiny CDN stand-in reproducing the behaviour that caused the bug."""

    routes = {
        # a "video" URL that actually serves a JPEG - the reported bug
        '/fake.mp4': (JPEG_BYTES, 'video/mp4'),
        '/real.mp4': (REAL_VIDEO, 'video/mp4'),
        '/poster.jpg': (JPEG_BYTES, 'image/jpeg'),
        '/playlist.m3u8': (b'#EXTM3U\n#EXT-X-VERSION:3\n', 'application/vnd.mpegurl'),
    }

    def log_message(self, *args):  # silence test output
        return

    def do_GET(self):  # noqa: N802 - http.server API
        path = self.path.split('?', 1)[0]

        # Mimics CDNs that reject range requests: the probe must be advisory
        if path == '/range-blocked.mp4' and self.headers.get('Range'):
            self.send_error(403)
            return

        if path == '/range-blocked.mp4':
            body, content_type = REAL_VIDEO, 'video/mp4'

        else:
            body, content_type = self.routes.get(path, (None, None))

        if body is None:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()

        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


#endregion


class SniffingTests(unittest.TestCase):
    def test_detects_image_and_video_signatures(self):
        self.assertEqual(sniff_signature(JPEG_BYTES), 'image/jpeg')
        self.assertEqual(sniff_signature(b'\x89PNG\r\n\x1a\n' + b'\x00' * 32), 'image/png')
        self.assertEqual(sniff_signature(REAL_VIDEO), 'video/mp4')
        self.assertEqual(
            sniff_signature(b'#EXTM3U\n#EXT-X-VERSION:3\n'),
            'application/vnd.apple.mpegurl',
        )

    def test_mp4_duration_is_parsed_from_mvhd(self):
        with TempWorkspace() as workspace:
            path = workspace.path('video.mp4')
            path.write_bytes(make_mp4(95.5))

            self.assertAlmostEqual(mp4_duration_seconds(path), 95.5, places=2)

    def test_fake_video_file_detects_jpeg_in_mp4(self):
        with TempWorkspace() as workspace:
            bad = workspace.path('bad.mp4')
            bad.write_bytes(JPEG_BYTES)

            self.assertTrue(is_fake_video_file(bad))

            good = workspace.path('good.mp4')
            good.write_bytes(REAL_VIDEO)

            self.assertFalse(is_fake_video_file(good))


class CheckMediaFileTests(unittest.TestCase):
    def test_image_saved_as_mp4_is_rejected(self):
        with TempWorkspace() as workspace:
            path = workspace.path('video.mp4')
            path.write_bytes(JPEG_BYTES)

            check = check_media_file(path, expected_duration=900)

            self.assertFalse(check.ok)
            self.assertEqual(check.kind, 'image')
            self.assertIn('image', check.detail)

    def test_truncated_video_is_rejected(self):
        with TempWorkspace() as workspace:
            path = workspace.path('video.mp4')
            path.write_bytes(make_mp4(12))

            with patch('download_of.media_download.find_ffprobe', return_value=None):
                check = check_media_file(path, expected_duration=900)

            self.assertFalse(check.ok)
            self.assertIn('truncated', check.detail)

    def test_complete_video_passes(self):
        with TempWorkspace() as workspace:
            path = workspace.path('video.mp4')
            path.write_bytes(make_mp4(900))

            with patch('download_of.media_download.find_ffprobe', return_value=None):
                check = check_media_file(path, expected_duration=900)

            self.assertTrue(check.ok, check.detail)


class CandidateTests(unittest.TestCase):
    def test_plain_mp4_is_preferred_over_preview(self):
        media = {
            'id': 1,
            'type': 'video',
            'files': {
                'full': {'url': 'https://cdn.test/full.mp4'},
                'preview': {'url': 'https://cdn.test/preview.jpg'},
                'thumb': {'url': 'https://cdn.test/thumb.jpg'},
            },
        }

        candidates = collect_candidates(media)

        self.assertEqual(candidates[0].url, 'https://cdn.test/full.mp4')
        self.assertEqual(candidates[0].kind, 'video')
        self.assertEqual([item.kind for item in candidates[1:]], ['image', 'image'])

    def test_legacy_source_and_video_sources_are_used(self):
        media = {
            'id': 2,
            'type': 'video',
            'source': {'source': 'https://cdn.test/legacy.mp4'},
            'videoSources': {'720': 'https://cdn.test/720.mp4'},
        }

        urls = [item.url for item in collect_candidates(media)]

        self.assertIn('https://cdn.test/legacy.mp4', urls)
        self.assertIn('https://cdn.test/720.mp4', urls)

    def test_resolution_variants_are_ordered_by_quality(self):
        media = {
            'id': 3,
            'type': 'video',
            'files': {
                '480': {'url': 'https://cdn.test/480.mp4'},
                '720': {'url': 'https://cdn.test/720.mp4'},
                '240': {'url': 'https://cdn.test/240.mp4'},
            },
        }

        urls = [item.url for item in collect_candidates(media)]

        self.assertEqual(
            urls,
            [
                'https://cdn.test/720.mp4',
                'https://cdn.test/480.mp4',
                'https://cdn.test/240.mp4',
            ],
        )

    def test_drm_media_is_detected(self):
        media = {
            'id': 3,
            'type': 'video',
            'files': {
                'preview': {'url': 'https://cdn.test/p.jpg'},
                'drm': {
                    'manifest': {
                        'hls': 'https://cdn.test/x.m3u8',
                        'dash': 'https://cdn.test/x.mpd',
                    },
                    'signature': {'hls': {'CloudFront-Policy': 'abc'}},
                },
            },
        }

        self.assertTrue(has_drm(media))
        self.assertEqual(set(drm_details(media)['manifests']), {'hls', 'dash'})

    def test_media_entry_filename_and_duration(self):
        media = {
            'id': 98765,
            'type': 'video',
            'duration': 905,
            'files': {'full': {'url': 'https://cdn.test/full.mp4'}},
        }

        entry = build_media_entry(media, container_id=1234)

        self.assertEqual(entry['filename'], '1234_98765.mp4')
        self.assertEqual(entry['duration'], 905)
        self.assertFalse(entry['drm'])

        message_entry = build_media_entry(media, container_id=555, prefix='msg_')
        self.assertEqual(message_entry['filename'], 'msg_555_98765.mp4')

    def test_photo_entry_uses_jpg(self):
        media = {
            'id': 4,
            'type': 'photo',
            'files': {'full': {'url': 'https://cdn.test/photo.jpg'}},
        }

        entry = build_media_entry(media, container_id=99)

        self.assertEqual(entry['filename'], '99_4.jpg')
        self.assertEqual(entry['type'], 'photo')


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), MediaHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f'http://127.0.0.1:{cls.server.server_address[1]}'
        cls.session = requests.Session()

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.workspace = TempWorkspace()
        self.dest_dir = TEST_TMP_ROOT
        self.addCleanup(self.workspace.cleanup)

    def url(self, path: str) -> str:
        return f'{self.base_url}{path}'

    def test_falls_back_to_next_candidate_when_cdn_serves_an_image(self):
        """Reproduces the bug: a "video" URL returning a JPEG must be skipped."""

        stem = self.workspace.stem('1234_1')
        media = {
            'id': 1,
            'type': 'video',
            'files': {
                'full': {'url': self.url('/fake.mp4')},
                'source': {'url': self.url('/real.mp4')},
                'preview': {'url': self.url('/poster.jpg')},
            },
        }

        outcome = download_media_source(
            session=self.session,
            media_raw=media,
            dest_dir=self.dest_dir,
            stem=stem,
            media_type='video',
        )

        self.assertEqual(outcome.kind, 'video')
        self.assertEqual(outcome.path.name, f'{stem}.mp4')
        self.assertFalse(self.workspace.path('1234_1.preview.jpg').exists())
        self.assertNotIn(f'{stem}.preview.jpg', self.workspace.leftover_files())

    def test_never_writes_an_image_with_a_video_extension(self):
        """Only images are available - no fake .mp4 may appear on disk."""

        stem = self.workspace.stem('1234_1')
        media = {
            'id': 1,
            'type': 'video',
            'files': {
                'full': {'url': self.url('/fake.mp4')},
                'preview': {'url': self.url('/poster.jpg')},
            },
        }

        with self.assertRaises(DownloadError):
            download_media_source(
                session=self.session,
                media_raw=media,
                dest_dir=self.dest_dir,
                stem=stem,
                media_type='video',
            )

        self.assertEqual(self.workspace.leftover_files(), [])

    def test_drm_protected_video_raises_instead_of_saving_a_poster(self):
        stem = self.workspace.stem('1234_1')
        media = {
            'id': 1,
            'type': 'video',
            'duration': 1500,
            'files': {
                'preview': {'url': self.url('/poster.jpg')},
                'drm': {'manifest': {'hls': self.url('/playlist.m3u8')}, 'signature': {}},
            },
        }

        with self.assertRaises(DRMProtectedError) as context:
            download_media_source(
                session=self.session,
                media_raw=media,
                dest_dir=self.dest_dir,
                stem=stem,
                media_type='video',
            )

        self.assertIn('DRM protected', str(context.exception))
        self.assertEqual(self.workspace.leftover_files(), [])

    def test_truncated_download_is_rejected(self):
        stem = self.workspace.stem('1234_1')
        media = {
            'id': 1,
            'type': 'video',
            'files': {'full': {'url': self.url('/real.mp4')}},
        }

        with patch('download_of.media_download.find_ffprobe', return_value=None):
            with self.assertRaises(DownloadError):
                download_media_source(
                    session=self.session,
                    media_raw=media,
                    dest_dir=self.dest_dir,
                    stem=stem,
                    media_type='video',
                    expected_duration=900,
                )

        self.assertEqual(self.workspace.leftover_files(), [])

    def test_photo_download_succeeds(self):
        stem = self.workspace.stem('99_2')
        media = {
            'id': 2,
            'type': 'photo',
            'files': {'full': {'url': self.url('/poster.jpg')}},
        }

        outcome = download_media_source(
            session=self.session,
            media_raw=media,
            dest_dir=self.dest_dir,
            stem=stem,
            media_type='photo',
        )

        self.assertEqual(outcome.kind, 'image')
        self.assertEqual(outcome.path.name, f'{stem}.jpg')

    def test_stop_flag_aborts_and_cleans_up(self):
        stem = self.workspace.stem('1234_1')
        media = {
            'id': 1,
            'type': 'video',
            'files': {'full': {'url': self.url('/real.mp4')}},
        }

        stop_flag = threading.Event()
        stop_flag.set()

        with self.assertRaises(StopRequested):
            download_media_source(
                session=self.session,
                media_raw=media,
                dest_dir=self.dest_dir,
                stem=stem,
                media_type='video',
                stop_flag=stop_flag,
            )

        self.assertNotIn(f'{stem}.mp4.part', self.workspace.leftover_files())

    def test_downloads_even_when_probe_request_is_rejected(self):
        """CDNs that refuse range requests must not block the download."""

        stem = self.workspace.stem('1234_9')
        media = {
            'id': 9,
            'type': 'video',
            'files': {'full': {'url': self.url('/range-blocked.mp4')}},
        }

        outcome = download_media_source(
            session=self.session,
            media_raw=media,
            dest_dir=self.dest_dir,
            stem=stem,
            media_type='video',
        )

        self.assertEqual(outcome.kind, 'video')
        self.assertEqual(outcome.path.name, f'{stem}.mp4')

    def test_progressive_download_writes_file(self):
        destination = self.workspace.path('file.mp4')

        download_progressive(
            session=self.session,
            url=self.url('/real.mp4'),
            dest=destination,
        )

        self.assertTrue(destination.exists())
        self.assertEqual(destination.read_bytes(), REAL_VIDEO)


if __name__ == '__main__':
    unittest.main()
