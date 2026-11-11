"""FFmpeg Launcher Module"""


import shutil
import subprocess


def get_ffmpeg_bin() -> str:
    """Returns a usable ffmpeg binary path.

    A system ffmpeg (PATH) is preferred: it starts faster, respects the user's
    configuration and does not need a writable ``~/.pyffmpeg`` folder. The
    pyffmpeg bundled binary is used as a fallback for systems without ffmpeg.
    """

    ffmpeg_bin = shutil.which("ffmpeg")

    if ffmpeg_bin:
        return ffmpeg_bin

    from pyffmpeg import FFmpeg
    ffmpeg = FFmpeg(enable_log=False)
    ffmpeg_bin = ffmpeg.get_ffmpeg_bin()

    return ffmpeg_bin


def run_ffmpeg(args: list[str]) -> bool:
    proc_args = [get_ffmpeg_bin()]

    proc_args += args

    result = subprocess.run(
        proc_args,
        encoding='utf-8',
        capture_output=True,
        check=True,
        timeout=300,
    )

    return result.returncode == 0
