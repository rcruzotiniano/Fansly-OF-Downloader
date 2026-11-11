"""OnlyFans Media Downloading

Robust download pipeline for OnlyFans media.

Why this module exists
----------------------
OnlyFans exposes two very different kinds of video media:

* normal videos - a plain, progressive MP4 (``files.source``/``source.source``)
* everything else - DRM protected streams. For those the API only hands out
  encrypted HLS/DASH manifests under ``files["drm"]`` plus a preview/thumb
  **image**; no plain MP4 exists at all.

The previous code path took "the first URL it could find" and, when no MP4 was
available, silently fell back to that preview image - while keeping the
``.mp4`` extension. The result is exactly the reported symptom: longer videos
(OnlyFans decides server side, users typically see the cut-off around ~14
minutes) end up as "an image in an mp4 file".

This module fixes that by:

* collecting **every** URL a media object offers, ordered by usefulness
* probing a URL before using it and refusing to store images as video files
* supporting both progressive MP4 (resumable, with retries) and HLS (ffmpeg)
* verifying the result (magic bytes + real duration via ffprobe/mvhd)
* reporting DRM protected media instead of writing a fake video file
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from time import sleep
from typing import Any, Iterator, Optional

import requests

from errors import DRMProtectedError, DownloadError, MediaError
from textio import print_debug, print_info
from utils.ffmpeg import get_ffmpeg_bin


#region Constants

#: Timeout for the small probe request (connect, read). Short on purpose: the
#: probe is only advisory and every media item costs one.
PROBE_TIMEOUT: tuple[int, int] = (10, 20)

#: Timeout for actual media transfers (connect, read). Generous read timeout,
#: because a long video can pause for a while mid-stream on a slow CDN edge.
DOWNLOAD_TIMEOUT: tuple[int, int] = (30, 180)

#: Progress log interval while streaming.
PROGRESS_LOG_STEP: int = 64 * 1024 * 1024

#: A "video" file smaller than this is considered broken; a real video of any
#: usable length never is this small.
MIN_PLAUSIBLE_VIDEO_BYTES: int = 64 * 1024

IMAGE_SUFFIXES: frozenset = frozenset(
    {'.jpg', '.jpeg', '.jpe', '.png', '.webp', '.gif', '.bmp', '.avif', '.heic'}
)
HLS_SUFFIXES: frozenset = frozenset({'.m3u8', '.m3u'})
DASH_SUFFIXES: frozenset = frozenset({'.mpd'})
VIDEO_SUFFIXES: frozenset = frozenset(
    {'.mp4', '.m4v', '.mov', '.webm', '.mkv', '.ts', '.avi', '.flv', '.wmv'}
)

#: Extensions for the containers we can recognize from magic bytes.
SIGNATURE_EXTENSIONS: dict = {
    'video/mp4': 'mp4',
    'video/webm': 'webm',
    'video/mp2t': 'ts',
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/webp': 'webp',
    'image/gif': 'gif',
}

#endregion


#region Data types


class StopRequested(Exception):
    """Raised internally when the user asked the downloader to stop."""


@dataclass(frozen=True)
class MediaCandidate:
    """A single URL a media object offers for download."""

    url: str
    kind: str  # 'video' | 'hls' | 'manifest' | 'image' | 'unknown'
    label: str  # human readable origin, e.g. 'files.full.url'


@dataclass(frozen=True)
class ProbeResult:
    """Result of inspecting a media URL without downloading it fully."""

    status: int
    content_type: str
    signature: str
    total_bytes: Optional[int]
    error: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def is_image(self) -> bool:
        if self.signature.startswith('image/'):
            return True
        return self.content_type.startswith('image/')

    @property
    def is_hls(self) -> bool:
        if self.signature == 'application/vnd.apple.mpegurl':
            return True
        content_type = self.content_type
        return 'mpegurl' in content_type or 'm3u8' in content_type

    @property
    def is_video(self) -> bool:
        if self.is_image or self.is_hls:
            return False
        return (
            self.signature.startswith('video/')
            or self.content_type.startswith('video/')
            or self.content_type in ('application/octet-stream', 'binary/octet-stream')
        )


@dataclass
class FileCheck:
    """Result of verifying a downloaded file on disk."""

    ok: bool
    kind: str  # 'video' | 'image' | 'playlist' | 'unknown'
    duration: Optional[float] = None
    detail: str = ''


@dataclass
class DownloadOutcome:
    """What a media download attempt produced."""

    path: Optional[Path]
    kind: str  # 'video' | 'image' | 'none'
    detail: str = ''
    candidates_tried: list = field(default_factory=list)


#endregion


#region URL helpers


def _suffix_of(url: str) -> str:
    """Returns the lowercase file suffix of a URL path ('' when unknown)."""

    path = url.split('?', 1)[0].split('#', 1)[0]
    suffix = os.path.splitext(path.rsplit('/', 1)[-1])[1]

    return suffix.lower()


def classify_url(url: str) -> str:
    """Guesses what a URL points to, based on its path suffix."""

    suffix = _suffix_of(url)

    if suffix in HLS_SUFFIXES:
        return 'hls'

    if suffix in DASH_SUFFIXES:
        return 'manifest'

    if suffix in IMAGE_SUFFIXES:
        return 'image'

    if suffix in VIDEO_SUFFIXES:
        return 'video'

    return 'unknown'


def _iter_urls(value: Any, label: str = '') -> Iterator:
    """Recursively yields (label, url) for every HTTP URL in a JSON value."""

    if isinstance(value, str):
        if value.startswith('http'):
            yield label, value
        return

    if isinstance(value, dict):
        for key, child in value.items():
            child_label = f'{label}.{key}' if label else str(key)
            yield from _iter_urls(child, child_label)
        return

    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _iter_urls(child, f'{label}[{index}]')


#endregion


#region File type sniffing


def sniff_signature(data: bytes) -> str:
    """Identifies a file from its first bytes.

    Returns a MIME-ish string: ``image/jpeg``, ``video/mp4``,
    ``application/vnd.apple.mpegurl``, ``unknown`` and so on.
    """

    if not data:
        return 'unknown'

    if data[0:3] == b'\xff\xd8\xff':
        return 'image/jpeg'

    if data[0:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'

    if data[0:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'

    if data[0:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'

    if data[0:4] == b'\x1a\x45\xdf\xa3':
        return 'video/webm'

    if data[4:8] == b'ftyp':
        return 'video/mp4'

    if len(data) > 188 and data[0] == 0x47 and data[188] == 0x47:
        return 'video/mp2t'

    stripped = data.lstrip()[:16]

    if stripped.startswith(b'#EXTM3U'):
        return 'application/vnd.apple.mpegurl'

    if stripped.startswith((b'{', b'<!DOCTYPE', b'<html', b'<?xml')):
        return 'text/error'

    return 'unknown'


def extension_for_signature(signature: str, default: str = 'mp4') -> str:
    """Maps a sniffed signature to a file extension."""

    return SIGNATURE_EXTENSIONS.get(signature, default)


#endregion


#region MP4 duration parsing


def _iter_boxes(data: bytes, start: int = 0, end: Optional[int] = None) -> Iterator:
    """Iterates ISO-BMFF boxes: yields (type, body_start, body_end)."""

    limit = len(data) if end is None else min(end, len(data))
    position = start

    while position + 8 <= limit:
        size = int.from_bytes(data[position:position + 4], 'big')
        box_type = data[position + 4:position + 8]
        header_size = 8

        if size == 1:
            if position + 16 > limit:
                return
            size = int.from_bytes(data[position + 8:position + 16], 'big')
            header_size = 16

        elif size == 0:
            size = limit - position

        if size < header_size:
            return

        yield box_type, position + header_size, min(position + size, limit)

        position += size


def _mvhd_duration(payload: bytes) -> Optional[float]:
    """Extracts the duration of an ``mvhd`` box payload in seconds."""

    if len(payload) < 20:
        return None

    if payload[0] == 1:
        timescale = int.from_bytes(payload[20:24], 'big')
        duration = int.from_bytes(payload[24:32], 'big')
    else:
        timescale = int.from_bytes(payload[12:16], 'big')
        duration = int.from_bytes(payload[16:20], 'big')

    if not timescale:
        return None

    # 0 means "unknown" for fragmented MP4, 0xFFFFFFFF means the same for v0
    if duration in (0, 0xFFFFFFFF):
        return None

    return duration / timescale


def mp4_duration_seconds(path: Path) -> Optional[float]:
    """Reads the duration of an MP4 from its ``moov/mvhd`` box.

    Fallback for systems without ``ffprobe``. Only works when ``moov`` is at
    the start of the file (how OnlyFans ships progressive downloads) and not
    for fragmented MP4.
    """

    try:
        with open(path, 'rb') as file:
            head = file.read(2 * 1024 * 1024)
    except OSError:
        return None

    for box_type, body_start, body_end in _iter_boxes(head):
        if box_type != b'moov':
            continue

        moov = head[body_start:body_end]

        for inner_type, inner_start, inner_end in _iter_boxes(moov):
            if inner_type == b'mvhd':
                return _mvhd_duration(moov[inner_start:inner_end])

        return None

    return None


@lru_cache(maxsize=1)
def find_ffprobe() -> Optional[str]:
    """Locates an ``ffprobe`` binary (PATH first, then next to ffmpeg)."""

    binary = shutil.which('ffprobe')

    if binary:
        return binary

    try:
        ffmpeg_bin = get_ffmpeg_bin()
    except Exception:
        return None

    if not ffmpeg_bin:
        return None

    name = 'ffprobe.exe' if os.name == 'nt' else 'ffprobe'
    candidate = Path(ffmpeg_bin).with_name(name)

    return str(candidate) if candidate.exists() else None


def ffprobe_duration(path: Path) -> Optional[float]:
    """Returns the container duration in seconds via ffprobe (or None)."""

    binary = find_ffprobe()

    if not binary:
        return None

    try:
        result = subprocess.run(
            [
                binary,
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None

    return duration if duration > 0 else None


def media_duration_hint(media_raw: dict) -> Optional[float]:
    """Reads the expected duration (seconds) from an OnlyFans media object."""

    candidates: list = [media_raw.get('duration')]

    for key in ('video', 'source', 'files'):
        container = media_raw.get(key)

        if isinstance(container, dict):
            candidates.append(container.get('duration'))

            for value in container.values():
                if isinstance(value, dict):
                    candidates.append(value.get('duration'))

    for value in candidates:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue

        if seconds > 0:
            return seconds

    return None


def check_media_file(
        path: Path,
        expected_duration: Optional[float] = None,
        expect: str = 'video',
    ) -> FileCheck:
    """Verifies that a downloaded file really has the expected content."""

    try:
        size = path.stat().st_size

        with open(path, 'rb') as file:
            head = file.read(8192)

    except OSError as error:
        return FileCheck(False, 'unknown', None, f'cannot read file: {error}')

    signature = sniff_signature(head)

    if signature == 'application/vnd.apple.mpegurl':
        return FileCheck(False, 'playlist', None, 'file is an HLS playlist, not a video')

    if signature == 'text/error' or signature == 'unknown' and size == 0:
        return FileCheck(False, 'unknown', None, 'file contains text/error data')

    if expect == 'image':
        if signature.startswith('image/'):
            return FileCheck(True, 'image', None, f'{signature}, {size} bytes')

        return FileCheck(False, 'unknown', None, f'expected an image, got {signature}')

    # expect == 'video'
    if signature.startswith('image/'):
        return FileCheck(
            False, 'image', None,
            f'file contains an image ({signature}), not a video'
        )

    duration = ffprobe_duration(path)

    if duration is None:
        duration = mp4_duration_seconds(path)

    if not signature.startswith('video/') and duration is None:
        return FileCheck(
            False, 'unknown', None,
            f'unrecognized file content ({signature or "no signature"})'
        )

    if expected_duration and expected_duration > 5:
        if duration is None and size < MIN_PLAUSIBLE_VIDEO_BYTES:
            return FileCheck(
                False, 'video', duration,
                f'file is suspiciously small ({size} bytes)'
            )

        if duration is not None and duration < max(2.0, expected_duration * 0.8):
            return FileCheck(
                False, 'video', duration,
                f'video is truncated: {duration:.0f}s of ~{expected_duration:.0f}s'
            )

    return FileCheck(True, 'video', duration, 'ok')


def is_fake_video_file(path: Path) -> bool:
    """Cheap check for files saved as video but actually holding an image.

    Used to self-heal folders that previous program versions filled.
    """

    try:
        size = path.stat().st_size

        with open(path, 'rb') as file:
            head = file.read(4096)

    except OSError:
        return False

    signature = sniff_signature(head)

    if signature.startswith('image/'):
        return True

    if signature == 'application/vnd.apple.mpegurl':
        return True

    return size < MIN_PLAUSIBLE_VIDEO_BYTES


#endregion


#region DRM detection


def _files_container(media_raw: dict) -> dict:
    files = media_raw.get('files')

    return files if isinstance(files, dict) else {}


def drm_details(media_raw: dict) -> dict:
    """Returns the DRM manifest/signature data of a media object (if any)."""

    containers: list = []

    files = _files_container(media_raw)
    if files:
        containers.append(files)

    source = media_raw.get('source')
    if isinstance(source, dict):
        containers.append(source)

    for container in containers:
        drm = container.get('drm')

        if isinstance(drm, dict) and drm:
            manifests = drm.get('manifest') or {}
            signatures = drm.get('signature') or {}

            return {
                'manifests': manifests if isinstance(manifests, dict) else {},
                'signatures': signatures if isinstance(signatures, dict) else {},
            }

    return {}


def has_drm(media_raw: dict) -> bool:
    """Whether OnlyFans marked this media as DRM protected."""

    if media_raw.get('hasDRM') or media_raw.get('hasDrm'):
        return True

    return bool(drm_details(media_raw))


def drm_error_message(media_raw: dict, name: str = 'This video') -> str:
    """Builds an explanatory message for DRM protected media."""

    details = drm_details(media_raw)
    streams = ', '.join(sorted(details.get('manifests') or {})) or 'HLS/DASH'
    duration = media_duration_hint(media_raw)
    duration_text = ''

    if duration:
        duration_text = f' (duration {int(duration // 60)}:{int(duration % 60):02d})'

    return (
        f'{name}{duration_text} is DRM protected.\n'
        f'  OnlyFans serves it as an encrypted {streams} stream only and does not\n'
        f'  expose a plain MP4, so it cannot be saved by a normal download.\n'
        f'  Older versions stored the preview image as ".mp4" instead; that is\n'
        f'  now detected and skipped.\n'
        f'  Options: watch/download it in the browser, or use a tool with\n'
        f'  Widevine/CDM support (e.g. OF-Scraper with a CDM configured).\n'
        f'  Note: bypassing DRM may violate the OnlyFans terms and local law.'
    )


_DRM_NOTICE_SHOWN = False
_DRM_DETAIL_SHOWN = False


def print_drm_notice_once() -> None:
    """Explains the DRM situation once per program run (not once per video)."""

    global _DRM_NOTICE_SHOWN

    if _DRM_NOTICE_SHOWN:
        return

    _DRM_NOTICE_SHOWN = True

    print_info(
        '  Note: OnlyFans marks some (usually longer) videos as DRM protected and\n'
        '  offers no plain MP4 for them, so they cannot be downloaded. They are\n'
        '  skipped instead of being saved as a broken video file. See\n'
        '  docs/OnlyFans-Long-Videos.md and tools/of_media_doctor.py.'
    )


def print_drm_detail_once(message: str) -> None:
    """Prints the long DRM explanation once per run (avoids log spam)."""

    global _DRM_DETAIL_SHOWN

    if _DRM_DETAIL_SHOWN:
        return

    _DRM_DETAIL_SHOWN = True
    print_info(message)


#endregion


#region Candidate collection


def _add_candidate(
        candidates: list,
        seen: set,
        url: Any,
        label: str,
        kind_hint: Optional[str] = None,
    ) -> None:

    if not isinstance(url, str):
        return

    url = url.strip()

    if not url.startswith('http') or url in seen:
        return

    seen.add(url)
    candidates.append(
        MediaCandidate(url=url, kind=kind_hint or classify_url(url), label=label)
    )


def _add_entry(candidates: list, seen: set, entry: Any, label: str) -> None:
    """Adds a ``files`` quality entry (dict with url/sources or a plain string)."""

    if isinstance(entry, str):
        _add_candidate(candidates, seen, entry, label)
        return

    if not isinstance(entry, dict):
        return

    _add_candidate(candidates, seen, entry.get('url'), f'{label}.url')
    _add_candidate(candidates, seen, entry.get('source'), f'{label}.source')

    for index, (_, url) in enumerate(_iter_urls(entry.get('sources'))):
        _add_candidate(candidates, seen, url, f'{label}.sources[{index}]')


_QUALITY_KEY = re.compile(r'^(\d{3,4})p?$')


def collect_candidates(media_raw: dict, media_type: str = 'video') -> list:
    """Collects every download URL of a media object, best candidates first.

    Order of preference:
      1. ``files.full`` / ``files.source`` (the plain original file)
      2. resolution variants (``files.720`` & friends)
      3. ``videoSources`` (what the web player uses)
      4. legacy ``source.source`` / ``source.url``
      5. any remaining MP4/HLS URL anywhere in the object
      6. preview/thumb images (only usable as a last resort)
    """

    candidates: list = []
    seen: set = set()

    if not isinstance(media_raw, dict):
        return candidates

    files = _files_container(media_raw)

    if files:
        for key in ('full', 'source'):
            _add_entry(candidates, seen, files.get(key), f'files.{key}')

        quality_keys = [
            key for key in files if _QUALITY_KEY.match(str(key))
        ]
        quality_keys.sort(
            key=lambda key: int(_QUALITY_KEY.match(str(key)).group(1)),
            reverse=True,
        )

        for key in quality_keys:
            _add_entry(candidates, seen, files[key], f'files.{key}')

    source = media_raw.get('source')

    if isinstance(source, dict):
        _add_candidate(candidates, seen, source.get('source'), 'source.source')
        _add_candidate(candidates, seen, source.get('url'), 'source.url')

        for index, (_, url) in enumerate(_iter_urls(source.get('sources'))):
            _add_candidate(candidates, seen, url, f'source.sources[{index}]')

    elif isinstance(source, str):
        _add_candidate(candidates, seen, source, 'source')

    video_sources = media_raw.get('videoSources')

    if isinstance(video_sources, dict):
        for name, url in video_sources.items():
            _add_candidate(candidates, seen, url, f'videoSources.{name}')

    elif isinstance(video_sources, (list, tuple)):
        for index, (_, url) in enumerate(_iter_urls(video_sources)):
            _add_candidate(candidates, seen, url, f'videoSources[{index}]')

    # Unknown fields: accept any URL that clearly is a video or stream
    for label, url in _iter_urls(media_raw):
        if classify_url(url) in ('video', 'hls', 'manifest'):
            _add_candidate(candidates, seen, url, label)

    # Preview/thumb images last
    if files:
        for key in ('thumb', 'preview', 'squarePreview'):
            _add_entry(candidates, seen, files.get(key), f'files.{key}')

    for key in ('preview', 'thumb', 'squarePreview'):
        _add_candidate(candidates, seen, media_raw.get(key), key)

    for label, url in _iter_urls(media_raw):
        if classify_url(url) == 'image':
            _add_candidate(candidates, seen, url, label)

    videos = [item for item in candidates if item.kind != 'image']
    images = [item for item in candidates if item.kind == 'image']

    if media_type in ('photo', 'image'):
        return images + videos

    return videos + images


def primary_candidate(media_raw: dict, media_type: str = 'video') -> Optional[MediaCandidate]:
    """Returns the best guess URL for a media object."""

    candidates = collect_candidates(media_raw, media_type)

    return candidates[0] if candidates else None


def extension_for_candidate(candidate: Optional[MediaCandidate], media_type: str) -> str:
    """Initial file extension for a media item, before it is downloaded."""

    if candidate is None:
        return 'jpg' if media_type in ('photo', 'image') else 'mp4'

    if candidate.kind == 'hls':
        return 'mp4'  # HLS is remuxed into MP4

    suffix = _suffix_of(candidate.url).lstrip('.')

    if candidate.kind in ('video', 'manifest', 'unknown'):
        return suffix if suffix in VIDEO_SUFFIXES else 'mp4'

    return suffix if suffix in IMAGE_SUFFIXES else 'jpg'


def build_media_entry(
        media_raw: dict,
        container_id: Any,
        prefix: str = '',
        media_type: Optional[str] = None,
    ) -> Optional[dict]:
    """Turns an OnlyFans media object into the downloader's media item dict."""

    if not isinstance(media_raw, dict):
        return None

    resolved_type = str(media_type or media_raw.get('type') or 'unknown').lower()
    candidate = primary_candidate(media_raw, resolved_type)
    media_id = media_raw.get('id', 'unknown')
    extension = extension_for_candidate(candidate, resolved_type)

    return {
        'id': media_id,
        'type': resolved_type,
        'url': candidate.url if candidate else '',
        'filename': f'{prefix}{container_id}_{media_id}.{extension}',
        'container_id': container_id,
        'duration': media_duration_hint(media_raw),
        'drm': has_drm(media_raw),
        'raw': media_raw,
    }


#endregion


#region HTTP


def build_media_headers(config: Any) -> dict:
    """Headers for CDN requests (OnlyFans signs the URLs themselves)."""

    headers = {
        'Accept': '*/*',
        'Accept-Encoding': 'identity',
        'Referer': 'https://onlyfans.com/',
        'Origin': 'https://onlyfans.com',
    }

    user_agent = getattr(config, 'user_agent', None)

    if user_agent:
        headers['User-Agent'] = user_agent

    x_bc = getattr(config, 'x_bc', None)

    if x_bc:
        headers['x-bc'] = x_bc

    return headers


def build_cookie_header(config: Any) -> Optional[str]:
    """Cookie header string, used for ffmpeg (which needs plain headers)."""

    parts: list = []
    sess = getattr(config, 'sess', None)
    auth_id = getattr(config, 'auth_id', None)
    auth_uid = getattr(config, 'auth_uid', None)

    if sess:
        parts.append(f'sess={sess}')

    if auth_id:
        parts.append(f'auth_id={auth_id}')
        parts.append(f'auth_uid_={auth_uid or auth_id}')

    return '; '.join(parts) if parts else None


def cookies_to_header(cookies: Optional[dict]) -> Optional[str]:
    """Joins a cookies dict into an ``a=1; b=2`` header string."""

    if not cookies:
        return None

    return '; '.join(f'{key}={value}' for key, value in cookies.items())


def get_media_cookies(api: Any) -> Optional[dict]:
    """Session cookies of the API client (or None)."""

    try:
        return api.auth.get_cookies()
    except Exception:
        return None


def probe_media_url(
        session: requests.Session,
        url: str,
        headers: Optional[dict] = None,
        cookies: Optional[dict] = None,
    ) -> ProbeResult:
    """Fetches only the first bytes of a URL to learn what it really is."""

    request_headers = dict(headers or {})
    request_headers['Range'] = 'bytes=0-4095'

    try:
        with session.get(
                    url,
                    headers=request_headers,
                    cookies=cookies,
                    stream=True,
                    timeout=PROBE_TIMEOUT,
                    allow_redirects=True,
                ) as response:

            status = response.status_code
            content_type = (response.headers.get('Content-Type') or '')
            content_type = content_type.split(';', 1)[0].strip().lower()
            total_bytes = _total_from_headers(response.headers, 0)
            head = b''

            if 200 <= status < 300:
                for chunk in response.iter_content(4096):
                    head = chunk or b''
                    break

            return ProbeResult(
                status=status,
                content_type=content_type,
                signature=sniff_signature(head),
                total_bytes=total_bytes,
            )

    except requests.RequestException as error:
        return ProbeResult(
            status=0,
            content_type='',
            signature='unknown',
            total_bytes=None,
            error=str(error),
        )


def _total_from_headers(headers: Any, resume_from: int) -> Optional[int]:
    """Total size of a (possibly resumed) response, when the server tells us."""

    content_range = headers.get('Content-Range')

    if content_range and '/' in content_range:
        tail = content_range.rsplit('/', 1)[1].strip()

        if tail.isdigit():
            return int(tail)

    content_length = headers.get('Content-Length')

    if content_length and content_length.strip().isdigit():
        return int(content_length) + resume_from

    return None


#endregion


#region Downloaders


def download_progressive(
        session: requests.Session,
        url: str,
        dest: Path,
        headers: Optional[dict] = None,
        cookies: Optional[dict] = None,
        stop_flag: Optional[threading.Event] = None,
        max_retries: int = 3,
        chunk_size: int = 1024 * 1024,
    ) -> Path:
    """Downloads a plain file, resuming interrupted transfers."""

    part_path = dest.with_name(dest.name + '.part')
    last_error: Optional[str] = None
    attempts = max(1, int(max_retries or 1))
    completed = False

    for attempt in range(1, attempts + 2):
        resume_from = part_path.stat().st_size if part_path.exists() else 0
        request_headers = dict(headers or {})

        if resume_from:
            request_headers['Range'] = f'bytes={resume_from}-'

        try:
            with session.get(
                        url,
                        headers=request_headers,
                        cookies=cookies,
                        stream=True,
                        timeout=DOWNLOAD_TIMEOUT,
                        allow_redirects=True,
                    ) as response:

                if response.status_code == 416:
                    completed = True
                    break

                if response.status_code >= 400:
                    raise DownloadError(f'HTTP {response.status_code} from the CDN')

                if response.status_code == 200:
                    resume_from = 0  # server ignored our Range request
                    mode = 'wb'
                else:
                    mode = 'ab'

                total_bytes = _total_from_headers(response.headers, resume_from)
                received = resume_from
                next_report = received + PROGRESS_LOG_STEP

                with open(part_path, mode) as file:
                    for chunk in response.iter_content(chunk_size):
                        if stop_flag is not None and stop_flag.is_set():
                            raise StopRequested()

                        if not chunk:
                            continue

                        file.write(chunk)
                        received += len(chunk)

                        if received >= next_report:
                            next_report = received + PROGRESS_LOG_STEP
                            _log_progress(dest.name, received, total_bytes)

                if total_bytes is not None and received < total_bytes:
                    raise DownloadError(
                        f'incomplete transfer: {received} of {total_bytes} bytes'
                    )

                if received == 0:
                    raise DownloadError('the CDN returned an empty file')

            completed = True
            break

        except StopRequested:
            part_path.unlink(missing_ok=True)
            raise

        except (requests.RequestException, DownloadError, OSError) as error:
            last_error = str(error) or error.__class__.__name__

            if attempt > attempts:
                break

            delay = min(2 ** attempt, 30)
            print_debug(
                f'    ! attempt {attempt} failed ({last_error}); retrying in {delay}s'
            )
            sleep(delay)

    if completed and part_path.exists() and part_path.stat().st_size > 0:
        part_path.replace(dest)
        return dest

    part_path.unlink(missing_ok=True)

    raise DownloadError(f'could not download file: {last_error or "unknown error"}')


def download_hls(
        url: str,
        dest: Path,
        headers: Optional[dict] = None,
        cookie_header: Optional[str] = None,
    ) -> Path:
    """Downloads an HLS (m3u8) stream and remuxes it into a single MP4."""

    ffmpeg_bin = get_ffmpeg_bin()
    header_lines = ''.join(f'{key}: {value}\r\n' for key, value in (headers or {}).items())

    if cookie_header:
        header_lines += f'Cookie: {cookie_header}\r\n'

    args = [
        ffmpeg_bin,
        '-y',
        '-loglevel', 'error',
        '-nostdin',
        '-headers', header_lines,
        '-i', url,
        '-c', 'copy',
        str(dest),
    ]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=None,  # long videos legitimately take a while
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DownloadError(f'ffmpeg could not start: {error}') from error

    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        stderr = (result.stderr or '').strip().splitlines()
        tail = ' | '.join(stderr[-3:]) if stderr else 'no output'
        raise DownloadError(f'ffmpeg failed (exit {result.returncode}): {tail}')

    return dest


def _log_progress(name: str, received: int, total: Optional[int]) -> None:
    megabytes = received / (1024 * 1024)

    if total:
        percent = min(100.0, received / total * 100)
        print_debug(f'    {name}: {percent:.0f}% ({megabytes:.0f} MB)')
    else:
        print_debug(f'    {name}: {megabytes:.0f} MB')


#endregion


#region Orchestration


def _normalize_to_signature(path: Path) -> Path:
    """Renames a download so the extension matches its real content."""

    try:
        with open(path, 'rb') as file:
            head = file.read(8192)
    except OSError:
        return path

    extension = extension_for_signature(sniff_signature(head))

    if extension and path.suffix.lower().lstrip('.') != extension:
        target = path.with_suffix(f'.{extension}')

        try:
            path.replace(target)
            return target
        except OSError:
            return path

    return path


def download_media_source(
        session: requests.Session,
        media_raw: dict,
        dest_dir: Path,
        stem: str,
        headers: Optional[dict] = None,
        cookies: Optional[dict] = None,
        media_type: str = 'video',
        expected_duration: Optional[float] = None,
        stop_flag: Optional[threading.Event] = None,
        max_retries: int = 3,
        cookie_header: Optional[str] = None,
    ) -> DownloadOutcome:
    """Downloads the best available source of a media object.

    Tries every candidate URL, skipping preview images, and verifies the
    result. Raises :class:`DRMProtectedError` when OnlyFans only offers an
    encrypted stream and no downloadable file is available.
    """

    if not isinstance(media_raw, dict):
        raise MediaError('invalid media data (not an object)')

    candidates = collect_candidates(media_raw, media_type)
    tried: list = []

    if not candidates:
        raise MediaError(
            'OnlyFans returned no usable media URL for this item '
            '(it may be paywall locked or still processing).'
        )

    want_video = media_type == 'video'
    drm_protected = has_drm(media_raw)
    drm_urls = {
        value
        for value in (drm_details(media_raw).get('manifests') or {}).values()
        if isinstance(value, str)
    }
    cookie_header = cookie_header or cookies_to_header(cookies)
    poster_path: Optional[Path] = None

    def attempt(candidate: MediaCandidate, probe: Optional[ProbeResult]) -> Optional[DownloadOutcome]:
        """Tries one candidate URL. Returns an outcome or None on failure."""

        nonlocal poster_path

        if candidate.kind == 'hls' or (probe is not None and probe.is_hls):
            dest = dest_dir / f'{stem}.mp4'

            try:
                download_hls(
                    candidate.url,
                    dest,
                    headers=headers,
                    cookie_header=cookie_header,
                )
            except DownloadError as error:
                print_debug(f'    - {candidate.label}: HLS download failed ({error})')
                dest.unlink(missing_ok=True)
                return None

            check = check_media_file(dest, expected_duration, expect='video')

            if check.ok:
                dest = _normalize_to_signature(dest)
                print_info(f'  ✓ {dest.name} (HLS, {check.duration or 0:.0f}s)')
                return DownloadOutcome(dest, 'video', check.detail, tried)

            print_debug(f'    - {candidate.label}: {check.detail}')
            dest.unlink(missing_ok=True)
            return None

        suffix = extension_for_candidate(candidate, media_type)
        dest = dest_dir / f'{stem}.{suffix}'

        # What the URL promised decides how the result is verified: photos are
        # images, videos (and OnlyFans GIFs, which are MP4s) are videos.
        probe_is_image = probe is not None and probe.is_image
        expect = 'image' if probe_is_image or candidate.kind == 'image' else 'video'

        try:
            download_progressive(
                session,
                candidate.url,
                dest,
                headers=headers,
                cookies=cookies,
                stop_flag=stop_flag,
                max_retries=max_retries,
            )
        except StopRequested:
            raise
        except (DownloadError, OSError) as error:
            print_debug(f'    - {candidate.label}: download failed ({error})')
            dest.unlink(missing_ok=True)
            return None

        check = check_media_file(
            dest,
            expected_duration if expect == 'video' else None,
            expect=expect,
        )

        if check.ok:
            dest = _normalize_to_signature(dest)
            detail = f'{check.duration:.0f}s' if check.duration else 'ok'
            print_info(f'  ✓ {dest.name} ({detail})')
            return DownloadOutcome(dest, expect, check.detail, tried)

        if check.kind == 'image':
            # Never keep an image with a video extension
            if poster_path is None:
                poster_path = dest_dir / f'{stem}.preview.jpg'
                dest.replace(poster_path)
            else:
                dest.unlink(missing_ok=True)

            print_debug(
                f'    - {candidate.label}: served an image although a video '
                f'was expected'
            )
            return None

        print_debug(f'    - {candidate.label}: {check.detail}')
        dest.unlink(missing_ok=True)
        return None

    unprobed: list = []
    logged_encrypted = False
    logged_preview = False

    for candidate in candidates:
        if stop_flag is not None and stop_flag.is_set():
            raise StopRequested()

        if candidate.url in drm_urls:
            if not logged_encrypted:
                logged_encrypted = True
                print_debug(
                    f'    - {candidate.label}: encrypted DRM stream, skipped'
                )
            continue

        tried.append(candidate.label)
        probe = probe_media_url(session, candidate.url, headers, cookies)

        if not probe.usable:
            # The probe is advisory: some CDNs/proxies reject range requests or
            # slow down when the connection is reused. Download such candidates
            # directly instead of skipping them.
            print_debug(
                f'    - {candidate.label}: probe failed '
                f'({probe.error or f"HTTP {probe.status}"}), will try anyway'
            )
            unprobed.append(candidate)
            continue

        if probe.is_image and want_video:
            # A preview image is never a substitute for a video
            if not logged_preview:
                logged_preview = True
                print_debug(
                    f'    - {candidate.label}: preview image, not a video'
                )
            continue

        outcome = attempt(candidate, probe)

        if outcome is not None:
            return outcome

    for candidate in unprobed:
        if stop_flag is not None and stop_flag.is_set():
            raise StopRequested()

        print_debug(f'    ~ {candidate.label}: direct attempt without probe')
        outcome = attempt(candidate, None)

        if outcome is not None:
            return outcome

    if want_video and drm_protected:
        if poster_path is not None:
            poster_path.unlink(missing_ok=True)

        raise DRMProtectedError(drm_error_message(media_raw, stem))

    if poster_path is not None and poster_path.exists():
        return DownloadOutcome(
            poster_path,
            'image',
            'OnlyFans only served a preview image for this item',
            tried,
        )

    raise DownloadError(
        f'no working media URL for {stem} (tried: {", ".join(tried) or "none"})'
    )


#endregion
