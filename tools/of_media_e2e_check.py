"""End-to-end media pipeline check for the OnlyFans downloader.

Generates real media files with ffmpeg (a short MP4, a JPEG poster and an HLS
playlist), serves them from a local HTTP server that mimics the OnlyFans CDN -
including the "video URL that actually returns a JPEG" behaviour - and verifies
that the download pipeline:

* downloads a plain MP4 and verifies its duration,
* skips a preview image served under a video URL,
* remuxes an HLS stream into a playable MP4,
* rejects truncated transfers and wrong durations,
* refuses DRM-only media without writing anything,
* resumes an interrupted download.

Run it after changing anything in ``download_of/media_download.py``:

    .\\.venv\\Scripts\\python.exe tools\\of_media_e2e_check.py
"""

import re
import shutil
import subprocess
import sys
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402 - needs the sys.path entry above

from download_of.media_download import (  # noqa: E402
    check_media_file,
    download_hls,
    download_media_source,
    download_progressive,
    ffprobe_duration,
)

WORK = REPO_ROOT / '.e2e'
FAILED = []


def run_ffmpeg(*args) -> None:
    result = subprocess.run(
        ['ffmpeg', '-y', '-loglevel', 'error', *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f'ffmpeg failed: {result.stderr[-500:]}')


def prepare() -> dict:
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    cache = WORK / 'cache'
    cache.mkdir()

    short = cache / 'short.mp4'
    run_ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=15:duration=4',
               '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4',
               '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
               '-shortest', str(short))

    poster = cache / 'poster.jpg'
    run_ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=64x64:duration=1',
               '-frames:v', '1', str(poster))

    hls_dir = cache / 'hls'
    hls_dir.mkdir()
    run_ffmpeg('-i', str(short), '-c', 'copy', '-f', 'hls', '-hls_time', '1',
               '-hls_list_size', '0',
               '-hls_segment_filename', str(hls_dir / 'seg%03d.ts'),
               str(hls_dir / 'index.m3u8'))

    return {
        'short': short.read_bytes(),
        'poster': poster.read_bytes(),
        'hls_dir': hls_dir,
    }


FIXTURES = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802
        path = self.path.split('?', 1)[0]
        video = FIXTURES['short']
        poster = FIXTURES['poster']

        if path == '/real.mp4':
            self._serve_with_ranges(video, 'video/mp4')
            return

        if path == '/fake.mp4':
            # A "video" URL that really serves a JPEG (the original bug)
            self._send(poster, 'video/mp4')
            return

        if path == '/poster.jpg':
            self._send(poster, 'image/jpeg')
            return

        if path == '/truncated.mp4':
            # Announces the full size but sends only part of it
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(video)))
            self.end_headers()
            try:
                self.wfile.write(video[: len(video) // 3])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return

        match = re.fullmatch(r'/hls/(index\.m3u8|seg\d+\.ts)', path)
        if match:
            file_path = FIXTURES['hls_dir'] / match.group(1)
            content_type = ('application/vnd.apple.mpegurl'
                            if file_path.suffix == '.m3u8' else 'video/mp2t')
            self._send(file_path.read_bytes(), content_type)
            return

        self.send_error(404)

    def _serve_with_ranges(self, body: bytes, content_type: str) -> None:
        range_header = self.headers.get('Range')
        start = 0

        if range_header:
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', range_header.strip())
            if match:
                start = int(match.group(1))
                body = body[start:]
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {start}-{start + len(body) - 1}/{start + len(body)}')
            else:
                self.send_response(200)
        else:
            self.send_response(200)

        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def check(name: str, condition: bool, detail: str = '') -> None:
    status = 'PASS' if condition else 'FAIL'
    print(f'  [{status}] {name}' + (f' — {detail}' if detail else ''))
    if not condition:
        FAILED.append(name)


def main() -> int:
    global FIXTURES

    print('Preparing real media with ffmpeg ...')
    FIXTURES = prepare()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    session = requests.Session()
    dest = WORK / 'out'
    dest.mkdir()

    print('\n1) Progressive download of a real MP4')
    outcome = download_media_source(
        session=session,
        media_raw={'id': 1, 'type': 'video', 'duration': 4,
                   'files': {'full': {'url': f'{base}/real.mp4'}}},
        dest_dir=dest, stem='real', media_type='video', expected_duration=4,
    )
    duration = ffprobe_duration(outcome.path)
    check('file downloaded', outcome.path.name == 'real.mp4')
    check('ffprobe duration ~4s', duration is not None and 3.5 <= duration <= 4.5,
          f'{duration}')

    print('\n2) JPEG served from a "video" URL is skipped')
    outcome = download_media_source(
        session=session,
        media_raw={'id': 2, 'type': 'video',
                   'files': {'full': {'url': f'{base}/fake.mp4'},
                             'source': {'url': f'{base}/real.mp4'},
                             'preview': {'url': f'{base}/poster.jpg'}}},
        dest_dir=dest, stem='fallback', media_type='video',
    )
    check('picked the real MP4', outcome.path.name == 'fallback.mp4',
          str(outcome.path))
    check('no .preview.jpg leftover', not (dest / 'fallback.preview.jpg').exists())

    print('\n3) HLS stream is remuxed to MP4')
    hls_source = dest / 'from_hls.mp4'
    download_hls(f'{base}/hls/index.m3u8', hls_source)
    hls_duration = ffprobe_duration(hls_source)
    check('playable mp4 from m3u8', hls_duration is not None and hls_duration > 3,
          f'{hls_duration}s')

    print('\n4) Truncated transfer is rejected')
    try:
        download_media_source(
            session=session,
            media_raw={'id': 4, 'type': 'video',
                       'files': {'full': {'url': f'{base}/truncated.mp4'}}},
            dest_dir=dest, stem='truncated', media_type='video', max_retries=1,
        )
        check('raised DownloadError', False, 'no exception')
    except Exception as error:  # noqa: BLE001 - diagnostic script
        check('raised DownloadError', type(error).__name__ == 'DownloadError',
              type(error).__name__)
    check('no leftover file', not list(dest.glob('truncated*')))

    print('\n5) Wrong duration (announced 600s, real 4s) is rejected')
    try:
        download_media_source(
            session=session,
            media_raw={'id': 5, 'type': 'video', 'duration': 600,
                       'files': {'full': {'url': f'{base}/real.mp4'}}},
            dest_dir=dest, stem='wrong_duration', media_type='video',
            expected_duration=600,
        )
        check('raised DownloadError', False, 'no exception')
    except Exception as error:  # noqa: BLE001
        check('raised DownloadError', type(error).__name__ == 'DownloadError',
              type(error).__name__)

    print('\n6) DRM-only media raises and writes nothing')
    try:
        download_media_source(
            session=session,
            media_raw={'id': 6, 'type': 'video', 'duration': 1500,
                       'files': {'preview': {'url': f'{base}/poster.jpg'},
                                 'drm': {'manifest': {'hls': f'{base}/hls/index.m3u8'},
                                         'signature': {}}}},
            dest_dir=dest, stem='drm', media_type='video',
        )
        check('raised DRMProtectedError', False, 'no exception')
    except Exception as error:  # noqa: BLE001
        check('raised DRMProtectedError',
              type(error).__name__ == 'DRMProtectedError', type(error).__name__)
    check('nothing written', not list(dest.glob('drm*')))

    print('\n7) Resuming a partial download')
    full = FIXTURES['short']
    resume_dest = dest / 'resume.mp4'
    part = dest / 'resume.mp4.part'
    part.write_bytes(full[: len(full) // 2])
    download_progressive(session, f'{base}/real.mp4', resume_dest)
    check('bytes identical after resume',
          resume_dest.read_bytes() == full, f'{resume_dest.stat().st_size} bytes')

    print('\n8) Sanity: signature/duration helpers')
    check('real file passes check_media_file',
          check_media_file(dest / 'real.mp4', expected_duration=4).ok)
    jpeg_as_mp4 = dest / 'jpeg_as_mp4.mp4'
    jpeg_as_mp4.write_bytes(FIXTURES['poster'])
    verdict = check_media_file(jpeg_as_mp4)
    check('jpeg named .mp4 is rejected',
          not verdict.ok and verdict.kind == 'image', verdict.detail)

    server.shutdown()
    server.server_close()
    session.close()

    print()
    if FAILED:
        print(f'FAILED: {len(FAILED)} check(s): {", ".join(FAILED)}')
        return 1

    print('All end-to-end checks passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
