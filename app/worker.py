from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

import redis

from app import settings
from app.analysis import AnalysisError, Cancelled, probe_media, run_deep, run_quick
from app.url_import import (
    SourceDownloadCancelled,
    SourceDownloadError,
    SourceDownloadTimeout,
    download_page_video,
)
from app.url_security import UnsafeMediaURL, validate_media_url

logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
logger = logging.getLogger("dopplertrack-worker")
client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
ADSB_QUERY_MIN_INTERVAL_SECONDS = 300


def load_job(job_id: str) -> dict | None:
    raw = client.get(f"job:{job_id}")
    return json.loads(raw) if raw else None


def save_job(job: dict) -> None:
    current = load_job(job["id"])
    if current and current.get("cancel_requested"):
        job["cancel_requested"] = True
        if job.get("status") == "completed":
            job["status"] = "cancelled"
    try:
        expiry = datetime.fromisoformat(job["expires_at"]).timestamp()
        ttl = max(1, min(settings.RETENTION_SECONDS, int(expiry - time.time())))
    except (KeyError, ValueError, TypeError):
        ttl = settings.RETENTION_SECONDS
    client.setex(f"job:{job['id']}", ttl, json.dumps(job, separators=(",", ":")))


def cancel_requested(job_id: str) -> bool:
    job = load_job(job_id)
    return not job or bool(job.get("cancel_requested")) or job.get("status") == "cancelled"


def set_progress(job: dict, progress: int) -> None:
    job["status"] = "processing"
    job["progress"] = max(0, min(99, progress))
    save_job(job)


def reserve_adsb_query() -> bool:
    """Enforce one shared historical query every five minutes across workers."""
    try:
        return bool(client.set(
            "adsb-history:request-slot",
            "1",
            ex=ADSB_QUERY_MIN_INTERVAL_SECONDS,
            nx=True,
        ))
    except redis.RedisError:
        return False


def process_job(job_id: str) -> None:
    job = load_job(job_id)
    if not job or job.get("status") == "cancelled" or cancel_requested(job_id):
        return
    source_dir = Path(settings.UPLOAD_DIR) / job_id
    sources = [path for path in source_dir.glob("source.*") if path.suffix.lower() in settings.ALLOWED_EXTENSIONS]
    source_url_file = source_dir / ".source_url"
    is_url_source = job.get("source_type") == "url"
    if not sources and not (is_url_source and source_url_file.is_file()):
        job["status"] = "failed"
        job["error"] = "The temporary video source is no longer available."
        save_job(job)
        return

    report_dir = Path(settings.DATA_DIR) / job_id
    report_dir.mkdir(parents=True, exist_ok=True)
    created_marker = report_dir / ".created_at"
    if not created_marker.exists():
        try:
            created_marker.write_text((source_dir / ".created_at").read_text(encoding="ascii"), encoding="ascii")
        except OSError:
            created_marker.write_text(str(time.time()), encoding="ascii")
    job["status"] = "processing"
    job["progress"] = 3
    job["error"] = None
    save_job(job)
    client.set("jobs:active", job_id, ex=settings.ANALYSIS_TIMEOUT_SECONDS + 60)
    started = time.monotonic()

    def progress(value: int) -> None:
        job["progress"] = value
        save_job(job)

    def remove_partial_import() -> None:
        for path in source_dir.glob("source.*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        if is_url_source and not sources:
            if cancel_requested(job_id):
                raise Cancelled
            remove_partial_import()
            job["phase"] = "import"
            progress(3)
            raw_url = source_url_file.read_text(encoding="utf-8")
            safe_url = validate_media_url(raw_url, resolve_dns=False)
            source = download_page_video(
                safe_url,
                source_dir,
                lambda: cancel_requested(job_id),
                started,
                progress,
            )
            if cancel_requested(job_id):
                raise Cancelled
            job["media"] = probe_media(source)
            source_url_file.unlink(missing_ok=True)
            sources = [source]
            job["phase"] = "quick"
            progress(20)
        elif is_url_source and not job.get("media"):
            job["media"] = probe_media(sources[0])
            source_url_file.unlink(missing_ok=True)

        if not sources:
            sources = list(source_dir.glob("source.*"))
        if not sources or not job.get("media"):
            raise AnalysisError("The video source could not be prepared for analysis.")

        if job.get("phase") == "deep":
            progress(10)
            result = run_deep(
                sources[0],
                job["media"],
                report_dir,
                job.get("recording_datetime"),
                job.get("search_area"),
                int(job.get("time_uncertainty_seconds", 30)),
                bool(job.get("flight_lookup_consent")),
                lambda: cancel_requested(job_id),
                started,
                progress=progress,
                request_permit=reserve_adsb_query,
                site_conditions=job.get("site_conditions"),
            )
            if cancel_requested(job_id):
                raise Cancelled
            job["deep_result"] = result
        else:
            progress(10)
            result = run_quick(
                sources[0],
                job["media"],
                report_dir,
                lambda: cancel_requested(job_id),
                started,
                progress=progress,
            )
            if cancel_requested(job_id):
                raise Cancelled
            job["quick_result"] = result
        job["status"] = "completed"
        job["progress"] = 100
        job["error"] = None
        save_job(job)
    except (Cancelled, SourceDownloadCancelled):
        shutil.rmtree(source_dir, ignore_errors=True)
        shutil.rmtree(report_dir, ignore_errors=True)
        job["status"] = "cancelled"
        job["error"] = None
        save_job(job)
    except SourceDownloadTimeout:
        remove_partial_import()
        if int(job.get("attempt", 1)) >= 2:
            shutil.rmtree(source_dir, ignore_errors=True)
        job["status"] = "failed"
        job["error"] = "Video import timed out. Try a shorter video or a direct media link."
        save_job(job)
    except (SourceDownloadError, UnsafeMediaURL) as exc:
        remove_partial_import()
        if int(job.get("attempt", 1)) >= 2:
            shutil.rmtree(source_dir, ignore_errors=True)
        job["status"] = "failed"
        job["error"] = str(exc) or "The public video page could not be downloaded."
        save_job(job)
    except AnalysisError as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        save_job(job)
    except Exception as exc:  # noqa: BLE001 -- isolate job failures and keep the worker alive.
        logger.error("Analysis failed (type=%s)", type(exc).__name__)
        job["status"] = "failed"
        job["error"] = "Analysis failed unexpectedly. You may retry once."
        save_job(job)
    finally:
        client.delete("jobs:active")


def cleanup_expired() -> int:
    now = time.time()
    cutoff = now - settings.RETENTION_SECONDS
    removed = 0
    for root_value in (settings.UPLOAD_DIR, settings.DATA_DIR):
        root = Path(root_value)
        root.mkdir(parents=True, exist_ok=True)
        for child in root.iterdir():
            if not child.is_dir():
                continue
            marker = child / ".created_at"
            try:
                created = float(marker.read_text(encoding="ascii")) if marker.exists() else child.stat().st_mtime
            except (OSError, ValueError):
                created = child.stat().st_mtime
            session_marker = child / ".upload_session"
            if session_marker.exists():
                try:
                    session_created = float(session_marker.read_text(encoding="ascii"))
                except (OSError, ValueError):
                    session_created = child.stat().st_mtime
                if session_created < now - settings.UPLOAD_SESSION_TTL_SECONDS:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
                continue
            if created < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
    return removed


def main() -> None:
    Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    Path(settings.DATA_DIR).mkdir(parents=True, exist_ok=True)
    last_cleanup = 0.0
    logger.info("Worker ready")
    while True:
        now = time.monotonic()
        if now - last_cleanup >= 600:
            try:
                count = cleanup_expired()
                if count:
                    logger.info("Retention cleanup removed %d expired directories", count)
            except OSError as exc:
                logger.error("Retention cleanup failed (type=%s)", type(exc).__name__)
            last_cleanup = now
        try:
            item = client.blpop("jobs:queue", timeout=5)
            if item:
                process_job(item[1])
        except redis.RedisError as exc:
            logger.error("Queue unavailable (type=%s)", type(exc).__name__)
            time.sleep(3)


if __name__ == "__main__":
    main()
