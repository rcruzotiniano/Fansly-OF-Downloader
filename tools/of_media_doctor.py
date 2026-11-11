"""OnlyFans Media Doctor

Finds and repairs the "video is actually a picture" problem in folders that were
downloaded by older versions of this program.

Background
----------
OnlyFans does not offer a plain MP4 for every video. For DRM protected videos
(typically the longer ones) the API only exposes an encrypted HLS/DASH stream
plus a preview image. Older program versions fell back to that preview image
and saved it with a ``.mp4`` extension - so the file "plays" as a single frame.

This tool scans a folder, detects such files (and truncated downloads) and tries
to re-download the real video from OnlyFans.

Usage::

    # show what is broken (read only)
    python tools/of_media_doctor.py scan "C:\\Users\\me\\Downloads\\creator-of\\Timeline"

    # repair everything found
    python tools/of_media_doctor.py repair "...\\Timeline"

    # only show what would be done
    python tools/of_media_doctor.py repair "...\\Timeline" --dry-run

    # show the raw media data of a single post (for bug reports)
    python tools/of_media_doctor.py dump 123456789
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app_version import APP_VERSION
from config.onlyfans_config import OnlyFansConfig, load_onlyfans_config
from download_of.media_download import (
    build_cookie_header,
    build_media_headers,
    check_media_file,
    get_media_cookies,
    media_duration_hint,
    primary_candidate,
    probe_media_url,
    sniff_signature,
)
from errors import DRMProtectedError, DownloadError, MediaError
from textio import print_error, print_info, print_warning


VIDEO_SUFFIXES = {'.mp4', '.m4v', '.mov', '.webm', '.mkv', '.ts', '.avi'}

#: ``<post_id>_<media_id>`` (timeline/single) or ``msg_<message_id>_<media_id>``
MEDIA_FILE_NAME = re.compile(
    r'^(?P<prefix>msg_)?(?P<container>\d+)_(?P<media>[0-9a-zA-Z]+)$'
)


@dataclass
class BrokenFile:
    path: Path
    reason: str
    detected: str
    duration: Optional[float]
    is_message: bool
    container_id: str
    media_id: str

    @property
    def stem(self) -> str:
        return self.path.stem


#region Scanning


def scan_folder(folder: Path) -> list[BrokenFile]:
    """Returns all media files in a folder that are broken or suspicious."""

    broken: list[BrokenFile] = []

    for path in sorted(folder.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue

        match = MEDIA_FILE_NAME.match(path.stem)

        if not match:
            continue

        check = check_media_file(path)

        if check.ok:
            continue

        broken.append(
            BrokenFile(
                path=path,
                reason=check.detail,
                detected=check.kind,
                duration=check.duration,
                is_message=bool(match.group('prefix')),
                container_id=match.group('container'),
                media_id=match.group('media'),
            )
        )

    return broken


def print_report(broken: list[BrokenFile], folder: Path) -> None:
    """Prints the scan result."""

    print()
    print_info(f"Scanned: {folder}")

    if not broken:
        print_info("No broken media files found. Everything looks fine.")
        return

    print_warning(f"Found {len(broken)} broken file(s):")
    print()

    for item in broken:
        size_bytes = item.path.stat().st_size
        size_text = (f'{size_bytes / 1024:.0f} KB'
                     if size_bytes >= 1024 else f'{size_bytes} bytes')
        print(f"  {item.path.name}")
        print(f"      size     : {size_text}")
        print(f"      detected : {item.detected}")
        print(f"      problem  : {item.reason}")

        if item.is_message:
            print(f"      source   : message {item.container_id}, media {item.media_id}")
        else:
            print(f"      source   : post {item.container_id}, media {item.media_id}")

        print()


#endregion


#region API helpers


def load_config() -> OnlyFansConfig:
    """Loads the OnlyFans configuration (credentials + download folder)."""

    config = OnlyFansConfig(program_version=APP_VERSION)
    config.config_path = REPO_ROOT / 'onlyfans_config.ini'

    load_onlyfans_config(config)

    if not config.has_credentials():
        raise SystemExit(
            'OnlyFans credentials are missing in onlyfans_config.ini '
            '(sess, auth_id, user_agent, x_bc).'
        )

    return config


def find_media_raw(config: OnlyFansConfig, item: BrokenFile) -> Optional[dict]:
    """Fetches the original media JSON for a broken file."""

    api = config.get_api()

    if item.is_message:
        return _find_message_media(config, api, item)

    post = api.get_post(item.container_id)

    for media in post.get('media') or []:
        if str(media.get('id')) == str(item.media_id):
            return media

    return None


def _find_message_media(config: OnlyFansConfig, api, item: BrokenFile) -> Optional[dict]:
    """Searches the configured creators' chats for a message media item."""

    for creator in sorted(config.user_names or []):

        try:
            user = api.get_user_by_username(creator)
            chat_id = str(user.get('id'))

        except Exception as error:
            print_warning(f"  Could not resolve creator '{creator}': {error}")
            continue

        try:
            response = api.get_chat_messages(
                chat_id=chat_id,
                limit=10,
                message_id=item.container_id,
            )

        except Exception as error:
            print_warning(f"  Could not read chat of '{creator}': {error}")
            continue

        for message in response.get('list') or []:
            if str(message.get('id')) != str(item.container_id):
                continue

            for media in message.get('media') or []:
                if str(media.get('id')) == str(item.media_id):
                    return media

    return None


#endregion


#region Repair


def repair_file(config: OnlyFansConfig, item: BrokenFile, dry_run: bool) -> bool:
    """Tries to re-download a single broken file. Returns True on success."""

    from download_of.media_download import download_media_source

    print_info(f"Repairing {item.path.name} ...")

    if dry_run:
        print_info("  (dry run - nothing downloaded)")
        return False

    media_raw = find_media_raw(config, item)

    if media_raw is None:
        print_warning(
            "  Could not find this media on OnlyFans any more "
            "(deleted, paywall locked or not from this account)."
        )
        return False

    api = config.get_api()

    try:
        outcome = download_media_source(
            session=api.session,
            media_raw=media_raw,
            dest_dir=item.path.parent,
            stem=item.stem,
            headers=build_media_headers(config),
            cookies=get_media_cookies(api),
            cookie_header=build_cookie_header(config),
            media_type=str(media_raw.get('type') or 'video').lower(),
            expected_duration=media_duration_hint(media_raw),
            max_retries=config.max_retries,
        )

    except DRMProtectedError:
        print_warning("  Not repairable: OnlyFans only offers a DRM protected stream.")
        _demote_to_preview(item)
        return False

    except (MediaError, DownloadError) as error:
        print_error(f"  Repair failed: {error}")
        return False

    if outcome.kind != 'video':
        print_warning("  Only a preview image is available - the real video is DRM protected.")
        _demote_to_preview(item)
        return False

    print_info(f"  ✓ Repaired: {outcome.path.name if outcome.path else item.stem}")
    return True


def _demote_to_preview(item: BrokenFile) -> None:
    """Renames a fake video file to a real image name, if it holds an image."""

    try:
        with open(item.path, 'rb') as file:
            head = file.read(4096)
    except OSError:
        return

    signature = sniff_signature(head)

    if not signature.startswith('image/'):
        print_info("  Keeping the file as it is (it does not contain an image either).")
        return

    extension = signature.rsplit('/', 1)[-1].replace('jpeg', 'jpg')
    target = item.path.with_suffix(f'.preview.{extension}')

    try:
        item.path.replace(target)
        print_info(f"  Renamed to {target.name} so it is no longer mistaken for a video.")
    except OSError as error:
        print_warning(f"  Could not rename the file: {error}")


#endregion


#region Diagnostics


def dump_post(post_id: str) -> None:
    """Prints the media structure of a post - handy for bug reports."""

    config = load_config()
    api = config.get_api()
    post = api.get_post(post_id)

    print()
    print_info(f"Post {post_id}")
    print()

    for media in post.get('media') or []:
        candidate = primary_candidate(media, str(media.get('type') or 'video'))
        sanitized = _sanitize(media)

        print(json.dumps(sanitized, indent=2, ensure_ascii=False)[:4000])
        print()
        print_info("  best candidate:")

        if candidate is None:
            print_warning("    none found - OnlyFans returned no URL at all")
            continue

        probe = probe_media_url(api.session, candidate.url, build_media_headers(config))
        print(f"    kind        : {candidate.kind} ({candidate.label})")
        print(f"    http status : {probe.status}")
        print(f"    content-type: {probe.content_type or 'unknown'}")
        print(f"    real content: {probe.signature}")
        print(f"    size        : {probe.total_bytes or 'unknown'} bytes")
        print()


def _sanitize(value, depth: int = 0):
    """Removes signatures/query strings and truncates long strings."""

    if depth > 6:
        return '...'

    if isinstance(value, dict):
        return {key: _sanitize(child, depth + 1) for key, child in value.items()}

    if isinstance(value, list):
        return [_sanitize(child, depth + 1) for child in value[:5]]

    if isinstance(value, str) and value.startswith('http'):
        return value.split('?', 1)[0] + ('?<signed>' if '?' in value else '')

    return value


#endregion


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Find and repair OnlyFans videos that were saved as images.',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    scan_parser = subparsers.add_parser('scan', help='report broken files')
    scan_parser.add_argument('folder', type=Path)

    repair_parser = subparsers.add_parser('repair', help='re-download broken files')
    repair_parser.add_argument('folder', type=Path)
    repair_parser.add_argument('--dry-run', action='store_true',
                               help='only show what would be repaired')

    dump_parser = subparsers.add_parser('dump', help='dump media data of a post')
    dump_parser.add_argument('post_id')

    args = parser.parse_args()

    if args.command == 'dump':
        dump_post(args.post_id)
        return 0

    folder = args.folder.expanduser().resolve()

    if not folder.exists():
        print_error(f'Folder does not exist: {folder}')
        return 2

    broken = scan_folder(folder)
    print_report(broken, folder)

    if args.command == 'scan' or not broken:
        return 0

    config = load_config()
    repaired = 0

    for item in broken:
        if repair_file(config, item, dry_run=args.dry_run):
            repaired += 1

    print()
    print_info(f"Repaired {repaired} of {len(broken)} broken file(s).")

    return 0 if repaired == len(broken) else 1


if __name__ == '__main__':
    sys.exit(main())
