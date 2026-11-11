# OnlyFans: long videos download as a picture in an `.mp4` file

## Symptom

Videos up to roughly 14 minutes download fine. Anything longer produces a file
that ends in `.mp4`, has a few dozen kilobytes, and "plays" as a single frozen
frame (or does not open at all).

## Why it happens

OnlyFans does not serve every video the same way:

| Video | What the API returns |
| --- | --- |
| "Normal" videos (usually shorter) | A plain, progressive MP4 in `files.source` / `files.full` (old format: `source.source`). |
| DRM protected videos (usually longer) | Only an encrypted HLS/DASH stream in `files.drm` (`manifest.hls`, `manifest.dash`) **plus a preview/thumbnail image**. There is no plain MP4. |

OnlyFans decides on their servers which videos get DRM protection, so the
cut-off point (users report it around 14 minutes) can differ per creator and
per video.

Older versions of this program took "the first URL the media object offered"
and fell back to the preview image when no MP4 field existed - but kept the
`.mp4` extension. That is why the result looks like an image inside an mp4
container: it *is* a JPEG (or WebP) that was named `.mp4`.

## What the program does now

1. **Collects every URL** the media object offers, in order of usefulness
   (`files.source`, `files.full`, resolution variants, `videoSources`, the
   legacy `source.source`, then `thumb`/`preview` images last).
2. **Probes each URL** with a small range request and checks the *real* content
   (magic bytes, not just `Content-Type`). A JPEG is never stored as a video.
3. **Downloads** progressive MP4 with resume-and-retry, or an HLS stream when
   only that is available (remuxed to MP4 with ffmpeg).
4. **Verifies** the result: the file must contain video, and its duration
   (via `ffprobe`, or the MP4 `mvhd` box as a fallback) must roughly match the
   duration the API announced. Truncated downloads are rejected and retried
   with the next candidate.
5. **Skips DRM protected media with a clear warning** instead of writing a fake
   video, and **repairs existing broken files** on the next run when it
   encounters them.

Progress details are written to `fansly_downloader_ng.log` with the `DEBUG`
level (the `    - files.full.url: preview image, not a video` lines show what
each candidate turned out to be).

## Repairing files you already downloaded

The repository ships a doctor tool. It works on a folder (for example
`<Downloads>\<creator>-of\Timeline` or `...\Messages`) and only touches files
whose name matches `postId_mediaId.*` or `msg_messageId_mediaId.*`.

```bash
# 1. See what is broken (read only)
python tools/of_media_doctor.py scan "C:\Users\me\Downloads\creator-of\Timeline"

# 2. Check what would be repaired
python tools/of_media_doctor.py repair "C:\Users\me\Downloads\creator-of\Timeline" --dry-run

# 3. Repair for real (re-downloads from OnlyFans)
python tools/of_media_doctor.py repair "C:\Users\me\Downloads\creator-of\Timeline"
```

What the repair does per file:

* looks the media up again through the OnlyFans API (post ID or message ID from
  the file name),
* downloads it with the new pipeline (including HLS and verification),
* if the video turns out to be DRM protected, renames the fake `.mp4` to
  `<name>.preview.jpg` so it is no longer mistaken for a video.

Repairing messages requires the creator to be listed in
`onlyfans_config.ini`; the tool searches the configured creators' chats for the
message ID.

## Inspecting a single post

For bug reports (or curiosity) you can dump what OnlyFans returned for a post:

```bash
python tools/of_media_doctor.py dump 123456789
```

The output shows the media JSON (with signed URLs stripped), the best candidate
URL, its HTTP status, the announced `Content-Type` **and** the real detected
content. When `files.drm` is present and no MP4 candidate exists, the video is
DRM protected.

## Can DRM protected videos be downloaded at all?

Not with a plain HTTP download. The stream is encrypted with Widevine and the
decryption keys are delivered by a license server, which a normal downloader
cannot talk to.

Options:

* watch / download the video in the browser (the player has a license),
* use a tool that supports Widevine with your own CDM (`pywidevine` + `mp4decrypt`),
  for example [OF-Scraper](https://github.com/datawhores/OF-Scraper) — the
  decrypted output still has to be muxed, and it needs device keys you must
  obtain yourself,
* for personal backups, ask the creator whether they can provide the file
  directly.

Note that bypassing DRM can violate the OnlyFans terms of service and may be
illegal in your country (DMCA §1201 in the US, EUCD in the EU). This project
does not implement DRM circumvention; it only detects the situation and stops
producing broken files.

## Verifying the pipeline

These checks ship with the repository:

```bash
# unit tests (no network, no credentials needed)
python -m unittest discover -s tests -p "test_*.py"

# end-to-end check: builds real MP4/JPEG/HLS files with ffmpeg, serves them from
# a local HTTP server and exercises the whole download pipeline
python tools/of_media_e2e_check.py

# GUI smoke check: verifies the Console Log window shows OnlyFans messages
python tools/gui_log_smoke_check.py
```

The end-to-end check covers the exact bug from this document (a "video" URL that
returns a JPEG), HLS remuxing, truncated transfers, wrong durations, DRM
detection and resuming partial downloads.

If downloads stop working with an API error (`400`, `401`, "Failed to get account
info"), the credentials are the first thing to rule out:

```bash
python tools/of_auth_check.py
```

It performs a real signed request against OnlyFans, prints their own error
message and a hint. Note that OnlyFans' WAF occasionally rejects valid requests
with `400 - Please refresh the page`; the client now retries those a few times
before giving up.

## Technical notes

* **HLS**: when OnlyFans serves a non-DRM video as HLS, the playlist is
  downloaded with `ffmpeg -c copy` (remux only, no quality loss). A system
  `ffmpeg` on `PATH` is used when present; otherwise the binary bundled with
  `pyffmpeg`.
* **Duration check**: `ffprobe` is used when available (a system one from
  `PATH`, or the one shipped next to the `pyffmpeg` binary). Otherwise the
  `moov/mvhd` box of the MP4 is parsed directly. A download shorter than ~80 %
  of the announced duration is treated as truncated.
* **Minimum size**: a video file smaller than 64 KiB is considered broken - a
  real video is always bigger.
* **Self-healing**: if an existing `.mp4` contains an image or is suspiciously
  small, it is deleted and downloaded again instead of being skipped.
