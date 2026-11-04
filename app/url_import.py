from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from app import settings

logger = logging.getLogger("dopplertrack.url-import")


class SourceDownloadError(Exception):
    pass


class SourceDownloadCancelled(SourceDownloadError):
    pass


class SourceDownloadTimeout(SourceDownloadError):
    pass


class _QuietLogger:
    def debug(self, _message: str) -> None:
        return

    def warning(self, _message: str) -> None:
        return

    def error(self, _message: str) -> None:
        return


def download_page_video(
    url: str,
    destination: Path,
    cancelled: Callable[[], bool],
    started: float,
    progress: Callable[[int], None],
) -> Path:
    """Download one public video with yt-dlp through the validated HTTP egress proxy."""
    if not settings.URL_IMPORT_PROXY:
        raise SourceDownloadError("Video page imports are not configured on this server.")
    from yt_dlp import YoutubeDL

    rejection_reason: str | None = None

    def filter_video(info: dict, incomplete: bool = False) -> str | None:
        nonlocal rejection_reason
        duration = info.get("duration")
        if isinstance(duration, (int, float)) and duration > settings.MAX_DURATION_SECONDS:
            rejection_reason = "duration"
            return f"Video exceeds the {settings.MAX_DURATION_SECONDS // 60}-minute limit"
        size = info.get("filesize") or info.get("filesize_approx")
        if isinstance(size, (int, float)) and size > settings.MAX_UPLOAD_BYTES:
            rejection_reason = "size"
            return f"Video exceeds the {settings.MAX_UPLOAD_MB} MiB limit"
        return None

    def progress_hook(data: dict) -> None:
        if cancelled():
            raise SourceDownloadCancelled
        if time.monotonic() - started > settings.ANALYSIS_TIMEOUT_SECONDS:
            raise SourceDownloadTimeout
        downloaded = int(data.get("downloaded_bytes") or 0)
        total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
        candidates = {data.get("filename"), data.get("tmpfilename")}
        for name in candidates:
            if not name:
                continue
            try:
                downloaded = max(downloaded, Path(name).stat().st_size)
            except OSError:
                continue
        if downloaded > settings.MAX_UPLOAD_BYTES or total > settings.MAX_UPLOAD_BYTES:
            raise SourceDownloadError(f"The video exceeds the {settings.MAX_UPLOAD_MB} MiB limit.")
        reserve = settings.MIN_FREE_DISK_GB * 1024 * 1024 * 1024
        if shutil.disk_usage(destination).free < reserve:
            raise SourceDownloadError("The download was stopped to preserve the server's configured free-disk reserve.")
        if data.get("status") == "downloading" and total:
            progress(min(18, 3 + int((downloaded / total) * 15)))
        elif data.get("status") == "finished":
            progress(18)

    options = {
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mkv",
        "outtmpl": str(destination / "source.%(ext)s"),
        "proxy": settings.URL_IMPORT_PROXY,
        "noplaylist": True,
        "max_filesize": settings.MAX_UPLOAD_BYTES,
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "js_runtimes": {"deno": {"path": "/usr/local/bin/deno"}},
        "match_filter": filter_video,
        "progress_hooks": [progress_hook],
        "logger": _QuietLogger(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "cachedir": False,
        "overwrites": False,
        "writethumbnail": False,
        "writeinfojson": False,
        "writedescription": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "ignoreerrors": False,
    }
    try:
        with YoutubeDL(options) as downloader:
            result = downloader.download([url])
        if result:
            if rejection_reason == "duration":
                raise SourceDownloadError(f"The video exceeds the {settings.MAX_DURATION_SECONDS // 60}-minute limit.")
            if rejection_reason == "size":
                raise SourceDownloadError(f"The video exceeds the {settings.MAX_UPLOAD_MB} MiB limit.")
            raise SourceDownloadError
    except SourceDownloadError:
        raise
    except Exception as exc:  # yt-dlp errors can contain the supplied URL; do not log or return them.
        if cancelled():
            raise SourceDownloadCancelled from exc
        if time.monotonic() - started > settings.ANALYSIS_TIMEOUT_SECONDS:
            raise SourceDownloadTimeout from exc
        logger.info("Video page import failed (type=%s)", type(exc).__name__)
        raise SourceDownloadError("The public video page could not be downloaded. Try a direct video link or upload the file.") from exc

    video_files = [
        path
        for path in destination.glob("source.*")
        if path.is_file() and path.suffix.lower() in settings.ALLOWED_EXTENSIONS
    ]
    if len(video_files) != 1:
        if rejection_reason == "duration":
            raise SourceDownloadError(f"The video exceeds the {settings.MAX_DURATION_SECONDS // 60}-minute limit.")
        if rejection_reason == "size":
            raise SourceDownloadError(f"The video exceeds the {settings.MAX_UPLOAD_MB} MiB limit.")
        raise SourceDownloadError("The page did not provide one supported video file. Try a direct video link or upload the file.")
    source = video_files[0]
    if source.stat().st_size > settings.MAX_UPLOAD_BYTES:
        source.unlink(missing_ok=True)
        raise SourceDownloadError(f"The video exceeds the {settings.MAX_UPLOAD_MB} MiB limit.")
    return source
