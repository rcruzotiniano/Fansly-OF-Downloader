"""OnlyFans Timeline Scraping

Download timeline posts from OnlyFans creators.
Handles photos, videos, and GIFs.
"""

import requests
import time
from pathlib import Path
from typing import Dict, List, Optional
from config.onlyfans_config import OnlyFansConfig
from download.downloadstate import DownloadState
from errors import DRMProtectedError, DownloadError, MediaError
from textio import print_debug, print_info, print_warning, print_error
from .media_download import (
    StopRequested,
    build_cookie_header,
    build_media_entry,
    build_media_headers,
    download_media_source,
    get_media_cookies,
    is_fake_video_file,
    print_drm_detail_once,
    print_drm_notice_once,
)


def download_timeline(config: OnlyFansConfig, state: DownloadState) -> None:
    """
    Download OnlyFans timeline posts

    Args:
        config: OnlyFans configuration
        state: Download state for this creator
    """
    # Track totals for progress reporting
    total_media = 0

    # Per-run counters (also shown in the GUI and the final summary)
    stats = {'downloaded': 0, 'skipped': 0, 'drm': 0, 'failed': 0}

    # GUI progress callback helper
    def send_progress(current, total, filename='', status='running'):
        if config.gui_mode and config.progress_callback:
            config.progress_callback({
                'type': 'timeline',
                'current': current,
                'total': total,
                'current_file': filename,
                'status': status,
                'duplicates': stats['skipped'],
                'downloaded': stats['downloaded']
            })

    try:
        api = config.get_api()

        print_info(f"\nDownloading timeline for: {state.creator_name}")

        # Ensure we have creator ID
        if not state.account_id:
            print_error("Creator ID not set. Run get_creator_account_info first.")
            return

        # Set download path: Downloads/CreatorName-of/Timeline/
        creator_folder = config.creator_folder_name(state.creator_name)
        timeline_folder = config.download_directory / creator_folder / "Timeline"
        timeline_folder.mkdir(parents=True, exist_ok=True)

        state.base_path = timeline_folder

        # Check if creator has download history (folder exists and has files)
        has_history = timeline_folder.exists() and any(timeline_folder.iterdir())

        # Check if post limit should be applied
        # Only apply to new creators when NOT in incremental mode
        apply_post_limit = (
            config.max_posts_per_creator is not None
            and not config.incremental_mode
            and not has_history
        )

        if apply_post_limit:
            print_info(f"Post limit enabled: Will download up to {config.max_posts_per_creator} newest posts for this new creator")

        before_cursor = None
        total_posts = 0

        while True:
            try:
                # Fetch posts
                response = api.get_timeline(
                    user_id=state.account_id,
                    limit=100,
                    before_publish_time=before_cursor
                )

                posts = response.get('list', [])

                if not posts:
                    print_info("No more posts to fetch")
                    break

                print_info(f"Processing {len(posts)} posts...")

                # Process each post
                for post in posts:
                    # Check stop flag before processing each post
                    if config.stop_flag and config.stop_flag.is_set():
                        print_warning("Download stopped by user")
                        break

                    total_posts += 1
                    post_id = post.get('id')

                    if config.show_downloads:
                        print_info(f"Post {total_posts}: ID {post_id}")

                    # Parse media from post
                    media_items = parse_post_media(post, state)

                    # Download each media item
                    for media in media_items:
                        # Check stop flag before each download
                        if config.stop_flag and config.stop_flag.is_set():
                            print_warning("Download stopped by user")
                            break

                        media_type = media.get('type', 'unknown')

                        # Skip audio entirely (user preference)
                        if media_type == 'audio':
                            continue

                        # Skip media types user doesn't want
                        if media_type in ('photo', 'gif') and not config.download_photos:
                            continue
                        if media_type == 'video' and not config.download_videos:
                            continue

                        # Send progress update before download
                        send_progress(
                            current=total_media + 1,
                            total=total_media + len(media_items),
                            filename=media.get('filename', '')
                        )

                        if download_media_item(config, state, media, stats):
                            total_media += 1
                            send_progress(
                                current=total_media,
                                total=total_media + len(media_items),
                                filename=media.get('filename', '')
                            )

                    # If stopped during media downloads, break from post loop too
                    if config.stop_flag and config.stop_flag.is_set():
                        break

                # Check if post limit reached
                if apply_post_limit and total_posts >= config.max_posts_per_creator:
                    print_info(f"Reached post limit ({config.max_posts_per_creator} posts). Stopping timeline download.")
                    print_info(f"Downloaded content from {total_posts} posts for this new creator.")
                    break

                # Check for more posts
                has_more = response.get('hasMore', False)

                if not has_more:
                    print_info("Reached end of timeline")
                    break

                # Update cursor for next page
                # Use last post's publish time
                if posts:
                    last_post = posts[-1]
                    before_cursor = last_post.get('postedAtPrecise') or last_post.get('createdAt')

                # Rate limiting
                if config.rate_limit_delay > 0:
                    time.sleep(config.rate_limit_delay)

                # Check for stop flag (GUI support)
                if config.stop_flag and config.stop_flag.is_set():
                    print_warning("Download stopped by user")
                    break

            except requests.HTTPError as e:
                if e.response.status_code == 429:
                    # Rate limited
                    print_warning("Rate limited. Waiting 60 seconds...")
                    time.sleep(60)
                    continue
                else:
                    raise

        print_info(f"\n✓ Timeline download complete!")
        print_info(f"  Posts processed: {total_posts}")
        print_info(f"  Media downloaded: {stats['downloaded']}")
        print_info(f"  Already present (skipped): {stats['skipped']}")

        if stats['drm']:
            print_warning(f"  Skipped (DRM protected): {stats['drm']}")
            print_info(
                "  DRM protected videos have no downloadable file on OnlyFans - "
                "see docs/OnlyFans-Long-Videos.md"
            )

        if stats['failed']:
            print_warning(f"  Failed: {stats['failed']}")

        # Send completion progress
        send_progress(
            current=total_media,
            total=total_media,
            filename='',
            status='complete'
        )

        state.pic_count = total_media

    except Exception as e:
        print_error(f"Timeline download failed: {e}")
        raise


def parse_post_media(post: Dict, state: DownloadState) -> List[Dict]:
    """
    Parse media items from OnlyFans post

    Args:
        post: Post data from API
        state: Download state

    Returns:
        List of media item dicts with url, filename, type, raw media data
    """
    media_items = []
    post_id = post.get('id', 'unknown')

    # OF posts have 'media' array
    media_array = post.get('media', [])

    for idx, media in enumerate(media_array):
        media_item = build_media_entry(media, container_id=post_id)

        if media_item is None or not media_item['url']:
            media_id = media.get('id', idx) if isinstance(media, dict) else idx
            print_warning(f"Could not find media URL for {media_id}")
            continue

        media_items.append(media_item)

    return media_items


def download_media_item(config: OnlyFansConfig, state: DownloadState,
                        media: Dict, stats: Optional[Dict] = None) -> bool:
    """
    Download single media item

    Args:
        config: OnlyFans configuration
        state: Download state
        media: Media item dict

    Returns:
        True if downloaded successfully, False if skipped or failed
    """
    def note(kind: str) -> None:
        """Counts the outcome of this item (when a stats dict was provided)."""
        if stats is not None:
            stats[kind] = stats.get(kind, 0) + 1

    filename = media.get('filename') or ''
    media_type = media.get('type', 'unknown')
    base_path = state.base_path

    if base_path is None:
        print_error("Internal error: download path not set")
        note('failed')
        return False

    stem = Path(filename).stem
    file_path = base_path / filename

    # Skip if exists - but repair files older versions saved as "video" that
    # actually contain only the preview image of a DRM protected video.
    if file_path.exists():
        if media_type == 'video' and is_fake_video_file(file_path):
            print_warning(f"  ↻ Replacing broken file (no video inside): {filename}")
            file_path.unlink(missing_ok=True)
        else:
            if config.show_skipped_downloads:
                print_info(f"  ⊘ Skipping (exists): {filename}")
            note('skipped')
            return False

    # Remove leftover preview images from earlier broken attempts
    if media_type == 'video':
        for leftover in base_path.glob(f"{stem}.preview.*"):
            leftover.unlink(missing_ok=True)

    try:
        if config.show_downloads:
            print_info(f"  ↓ Downloading: {filename}")

        api = config.get_api()

        outcome = download_media_source(
            session=api.session,
            media_raw=media.get('raw') or {'source': {'source': media.get('url')}},
            dest_dir=base_path,
            stem=stem,
            headers=build_media_headers(config),
            cookies=get_media_cookies(api),
            cookie_header=build_cookie_header(config),
            media_type=media_type,
            expected_duration=media.get('duration'),
            stop_flag=config.stop_flag,
            max_retries=config.max_retries,
        )

        if outcome.kind == 'video':
            note('downloaded')
            return True

        if outcome.kind == 'image' and media_type in ('photo', 'image', 'gif'):
            note('downloaded')
            return True

        print_warning(
            f"  ⊘ No downloadable video for {filename} - "
            f"OnlyFans only served a preview image"
        )
        note('failed')
        return False

    except StopRequested:
        print_warning(f"  ⊘ Download stopped: {filename}")
        return False  # not counted: the run was interrupted

    except DRMProtectedError as error:
        print_warning(f"  ⊘ Skipped (DRM protected): {filename}")
        print_drm_notice_once()
        print_drm_detail_once(str(error))
        note('drm')
        return False

    except (MediaError, DownloadError) as error:
        print_error(f"  ✗ Failed to download {filename}: {error}")
        note('failed')
        return False

    except Exception as error:
        print_error(f"  ✗ Failed to download {filename}: {error}")
        note('failed')
        return False


def get_media_extension(media_type: str, url: str) -> str:
    """
    Determine file extension from media type and URL

    Args:
        media_type: Type from API ('photo', 'video', 'gif', 'audio')
        url: Media URL

    Returns:
        File extension without dot
    """
    # Type-based mapping
    type_map = {
        'photo': 'jpg',
        'video': 'mp4',
        'gif': 'gif',
        'audio': 'mp3',
    }

    # Try type first
    if media_type in type_map:
        return type_map[media_type]

    # Try to extract from URL
    if '.' in url:
        parts = url.split('.')
        # Get last part before query string
        ext = parts[-1].split('?')[0].lower()
        # Validate it looks like an extension
        if len(ext) <= 4 and ext.isalnum():
            return ext

    # Default fallback
    return 'bin'
